"""Depositos on-chain: verificacion EVM y aviso a los admin. Contra Postgres REAL.

Lo que se prueba es lo que evita acreditar un pago que no ocurrio: que solo
cuente lo que dice la cadena, que un hash no se pueda reutilizar, y que ninguna
de las validaciones se pueda saltear.
"""
import asyncio
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import settings  # noqa: E402
from app.core.exceptions import AppException  # noqa: E402
from app.db.database import AsyncSessionLocal  # noqa: E402
from app.models.api_user import ApiUser  # noqa: E402
from app.repositories.api_user_repository import ApiUserRepository  # noqa: E402
from app.services import crypto_deposit_service as cds  # noqa: E402
from app.services.crypto_deposit_service import CryptoDepositService  # noqa: E402
from app.services.evm_client import (  # noqa: E402
    TRANSFER_TOPIC,
    EvmClient,
    TokenTransfer,
    TxReceipt,
    normalize_address,
    normalize_tx_hash,
)

fails = []

RECEIVER = "0x1111111111111111111111111111111111111111"
TOKEN = "0x2222222222222222222222222222222222222222"
SENDER = "0x3333333333333333333333333333333333333333"
OTRO_TOKEN = "0x4444444444444444444444444444444444444444"
OTRA_ADDR = "0x5555555555555555555555555555555555555555"
TX = "0x" + "ab" * 32


def check(label, ok, extra=""):
    print(("  [OK] " if ok else "  [FALLO] ") + label + (f"  {extra}" if extra else ""))
    if not ok:
        fails.append(label)


def transfer(to=RECEIVER, token=TOKEN, raw=100 * 10**18, frm=SENDER):
    return TokenTransfer(token_contract=token, from_address=frm, to_address=to,
                         raw_amount=raw, log_index=0)


class FakeEvm:
    """Simula el nodo. La BD y la logica de verificacion son reales."""

    def __init__(self, receipt="default", latest=1000, transfers=None):
        self.latest = latest
        if receipt == "default":
            receipt = TxReceipt(tx_hash=TX, success=True, block_number=900,
                                from_address=SENDER, to_address=TOKEN,
                                transfers=transfers if transfers is not None else [transfer()])
        self._receipt = receipt

    async def get_receipt(self, tx_hash):
        return self._receipt

    async def block_number(self):
        return self.latest


class FakeMailer:
    def __init__(self, falla=False):
        self.enviados = []
        self.falla = falla

    async def send_crypto_deposit_email(self, **kw):
        if self.falla:
            from app.core.exceptions import EmailDeliveryError
            raise EmailDeliveryError(message="sin transporte", detail=None)
        self.enviados.append(kw)
        return True


def build(session, evm=None, mailer=None):
    svc = CryptoDepositService(session)
    svc._client = evm or FakeEvm()
    return svc


async def expect(coro, label, code=None):
    try:
        await coro
        check(label, False, "-> no levanto")
    except AppException as e:
        check(label, code is None or e.code == code, f"-> {e.http_status} {e.code}")


async def seed():
    async with AsyncSessionLocal() as s:
        s.add(ApiUser(id="adm1", email="admin1@test.com", name="Admin1", role="ADMIN"))
        s.add(ApiUser(id="adm2", email="admin2@test.com", name="Admin2", role="ADMIN"))
        s.add(ApiUser(id="adm3", email="viejo@test.com", name="Inactivo", role="ADMIN",
                      is_active=False))
        s.add(ApiUser(id="usr1", email="user@test.com", name="User", role="USER"))
        await s.commit()


async def main():
    settings.EVM_RECEIVING_ADDRESS = RECEIVER
    settings.EVM_USDC_CONTRACT = TOKEN
    settings.EVM_CHAIN_ID = 97
    settings.EVM_TOKEN_DECIMALS = 18
    settings.EVM_MIN_CONFIRMATIONS = 12
    settings.EVM_NETWORK_NAME = "BSC Testnet"
    await seed()

    print("\n1. Normalizacion de entradas")
    check("acepta hash con 0x", normalize_tx_hash(TX) == TX)
    check("acepta hash sin 0x", normalize_tx_hash("ab" * 32) == TX)
    check("normaliza mayusculas", normalize_tx_hash(TX.upper().replace("0X", "0x")) == TX)
    check("rechaza hash corto", normalize_tx_hash("0xabc") is None)
    check("rechaza no hexadecimal", normalize_tx_hash("0x" + "zz" * 32) is None)
    check("address en minusculas",
          normalize_address("0xAAAA111111111111111111111111111111111111")
          == "0xaaaa111111111111111111111111111111111111")

    print("\n2. Decodificacion de logs ERC-20")
    cli = EvmClient()
    padded = lambda a: "0x" + "0" * 24 + a[2:]  # noqa: E731
    raw = {"blockNumber": "0x384", "status": "0x1", "transactionHash": TX,
           "from": SENDER, "to": TOKEN,
           "logs": [
               {"address": TOKEN, "logIndex": "0x0", "data": hex(100 * 10**18),
                "topics": [TRANSFER_TOPIC, padded(SENDER), padded(RECEIVER)]},
               # ERC-721: 4 topics -> debe ignorarse
               {"address": TOKEN, "logIndex": "0x1", "data": "0x0",
                "topics": [TRANSFER_TOPIC, padded(SENDER), padded(RECEIVER), "0x01"]},
               # otro evento cualquiera
               {"address": TOKEN, "logIndex": "0x2", "data": "0x0",
                "topics": ["0xdeadbeef", padded(SENDER), padded(RECEIVER)]},
           ]}

    async def fake_rpc(method, params):
        return raw if method == "eth_getTransactionReceipt" else None
    cli._rpc = fake_rpc
    rec = await cli.get_receipt(TX)
    check("extrae 1 sola transferencia (ignora ERC-721 y otros eventos)",
          len(rec.transfers) == 1, f"-> {len(rec.transfers)}")
    t = rec.transfers[0]
    check("decodifica destino desde el topic", t.to_address == RECEIVER, f"-> {t.to_address}")
    check("decodifica origen desde el topic", t.from_address == SENDER)
    check("decodifica el monto del data", t.raw_amount == 100 * 10**18)
    check("marca la tx como exitosa", rec.success is True)

    print("\n3. Validaciones que rechazan el comprobante")
    async with AsyncSessionLocal() as s:
        usr = await s.get(ApiUser, "usr1")
        await expect(build(s).submit(tx_hash="0x123", chain_id=97, value=Decimal("100"),
                                     caller=usr),
                     "hash con formato invalido", "TX_HASH_INVALID")
        await expect(build(s).submit(tx_hash=TX, chain_id=1, value=Decimal("100"), caller=usr),
                     "chain_id que no es el configurado", "CHAIN_MISMATCH")
        await expect(build(s, FakeEvm(receipt=None)).submit(
            tx_hash=TX, chain_id=97, value=Decimal("100"), caller=usr),
            "transaccion que la cadena no conoce", "TX_NOT_FOUND")

    revertida = TxReceipt(tx_hash=TX, success=False, block_number=900,
                          from_address=SENDER, to_address=TOKEN, transfers=[])
    async with AsyncSessionLocal() as s:
        usr = await s.get(ApiUser, "usr1")
        await expect(build(s, FakeEvm(receipt=revertida)).submit(
            tx_hash=TX, chain_id=97, value=Decimal("100"), caller=usr),
            "transaccion que revirtio en la cadena", "TX_FAILED_ON_CHAIN")
        # 900 -> latest 905 = 6 confirmaciones, se piden 12
        await expect(build(s, FakeEvm(latest=905)).submit(
            tx_hash=TX, chain_id=97, value=Decimal("100"), caller=usr),
            "confirmaciones insuficientes", "TX_NOT_CONFIRMED")

    print("\n4. La transferencia tiene que ser DEL token y HACIA nuestra address")
    async with AsyncSessionLocal() as s:
        usr = await s.get(ApiUser, "usr1")
        await expect(build(s, FakeEvm(transfers=[transfer(to=OTRA_ADDR)])).submit(
            tx_hash=TX, chain_id=97, value=Decimal("100"), caller=usr),
            "transferencia hacia otra address", "TRANSFER_NOT_FOUND")
        await expect(build(s, FakeEvm(transfers=[transfer(token=OTRO_TOKEN)])).submit(
            tx_hash=TX, chain_id=97, value=Decimal("100"), caller=usr),
            "transferencia de otro token", "TRANSFER_NOT_FOUND")
        await expect(build(s, FakeEvm(transfers=[])).submit(
            tx_hash=TX, chain_id=97, value=Decimal("100"), caller=usr),
            "transaccion sin transferencias de token", "TRANSFER_NOT_FOUND")

    print("\n5. El monto declarado tiene que coincidir con el on-chain")
    async with AsyncSessionLocal() as s:
        usr = await s.get(ApiUser, "usr1")
        await expect(build(s).submit(tx_hash=TX, chain_id=97, value=Decimal("999"), caller=usr),
                     "monto declarado mayor al real", "AMOUNT_MISMATCH")
        await expect(build(s).submit(tx_hash=TX, chain_id=97, value=Decimal("50"), caller=usr),
                     "monto declarado menor al real", "AMOUNT_MISMATCH")

    print("\n6. Los rechazos quedan registrados para auditoria")
    async with AsyncSessionLocal() as s:
        rows, total = await CryptoDepositService(s).list()
        check("se guardaron los intentos rechazados", total >= 8, f"-> {total}")
        motivos = {r.rejection_code for r in rows}
        check("con su motivo", "AMOUNT_MISMATCH" in motivos and "CHAIN_MISMATCH" in motivos,
              f"-> {sorted(m for m in motivos if m)}")
        check("ninguno quedo CONFIRMED", all(r.status == "REJECTED" for r in rows))

    print("\n7. Camino feliz: se confirma y se avisa a los admin")
    mailer = FakeMailer()
    original = cds.email_service
    cds.email_service = mailer
    try:
        async with AsyncSessionLocal() as s:
            usr = await s.get(ApiUser, "usr1")
            dep = await build(s).submit(tx_hash=TX, chain_id=97, value=Decimal("100"),
                                        caller=usr)
            check("status CONFIRMED", dep.status == "CONFIRMED")
            check("monto on-chain leido de la cadena", dep.onchain_amount == Decimal("100"),
                  f"-> {dep.onchain_amount}")
            check("guarda el monto en unidad minima", dep.raw_amount == str(100 * 10**18))
            check("guarda origen y destino",
                  dep.from_address == SENDER and dep.to_address == RECEIVER)
            check("guarda bloque y confirmaciones",
                  dep.block_number == 900 and dep.confirmations == 101)
            check("registra quien lo presento", dep.api_user_id == "usr1")

        # Los admins activos son los que sembramos MAS el que crea la migracion
        # inicial; se cuentan de la BD en vez de fijarlos a mano.
        async with AsyncSessionLocal() as s:
            todos = await ApiUserRepository(s).list()
            activos = sorted(u.email for u in todos
                             if u.role == "ADMIN" and u.is_active and u.email)
        check(f"avisa a los {len(activos)} admins ACTIVOS",
              len(mailer.enviados) == len(activos), f"-> {len(mailer.enviados)}")
        destinatarios = sorted(m["to"] for m in mailer.enviados)
        check("los destinatarios son exactamente los admins activos",
              destinatarios == activos, f"-> {destinatarios}")
        check("excluye al admin INACTIVO", "viejo@test.com" not in destinatarios)
        check("no le avisa a los USER", "user@test.com" not in destinatarios)
        check("el mail lleva el monto on-chain",
              mailer.enviados[0]["amount"] == Decimal("100"))
        check("el mail lleva el link al explorador",
              "testnet.bscscan.com" in mailer.enviados[0]["explorer"],
              f"-> {mailer.enviados[0]['explorer']}")

        async with AsyncSessionLocal() as s:
            rows, _ = await CryptoDepositService(s).list(status="CONFIRMED")
            confirmada = [r for r in rows if r.tx_hash == TX][0]
            check("queda registrado a cuantos se aviso",
                  confirmada.notified_admins == len(activos), f"-> {confirmada.notified_admins}")
            check("y cuando", confirmada.notified_at is not None)

        print("\n8. Una transaccion NO se puede reutilizar")
        enviados_antes = len(mailer.enviados)
        async with AsyncSessionLocal() as s:
            usr = await s.get(ApiUser, "usr1")
            await expect(build(s).submit(tx_hash=TX, chain_id=97, value=Decimal("100"),
                                         caller=usr),
                         "mismo hash otra vez", "DEPOSIT_ALREADY_REGISTERED")
        check("no se mandaron mas mails", len(mailer.enviados) == enviados_antes)

        print("\n9. Decimales: leerlos mal cambia el monto por ordenes de magnitud")
        settings.EVM_TOKEN_DECIMALS = 6
        tx6 = "0x" + "cd" * 32
        async with AsyncSessionLocal() as s:
            usr = await s.get(ApiUser, "usr1")
            # el mismo raw con 6 decimales son 100 billones, no 100
            await expect(build(s, FakeEvm(transfers=[transfer(raw=100 * 10**18)])).submit(
                tx_hash=tx6, chain_id=97, value=Decimal("100"), caller=usr),
                "con decimales mal configurados el monto no coincide", "AMOUNT_MISMATCH")
            dep = await build(s, FakeEvm(transfers=[transfer(raw=100 * 10**6)])).submit(
                tx_hash=tx6, chain_id=97, value=Decimal("100"), caller=usr)
            check("con 6 decimales, 100_000_000 son 100", dep.onchain_amount == Decimal("100"))
        settings.EVM_TOKEN_DECIMALS = 18

        print("\n10. Varias transferencias hacia nosotros en la misma tx se suman")
        tx_multi = "0x" + "ef" * 32
        async with AsyncSessionLocal() as s:
            usr = await s.get(ApiUser, "usr1")
            dep = await build(s, FakeEvm(transfers=[
                transfer(raw=60 * 10**18),
                transfer(raw=40 * 10**18),
                transfer(raw=999 * 10**18, to=OTRA_ADDR),  # a otro: no cuenta
            ])).submit(tx_hash=tx_multi, chain_id=97, value=Decimal("100"), caller=usr)
            check("suma solo las dirigidas a nuestra address",
                  dep.onchain_amount == Decimal("100"), f"-> {dep.onchain_amount}")
    finally:
        cds.email_service = original

    print("\n11. Si falla el correo, el deposito NO se pierde")
    fallido = FakeMailer(falla=True)
    cds.email_service = fallido
    try:
        tx_mail = "0x" + "12" * 32
        async with AsyncSessionLocal() as s:
            usr = await s.get(ApiUser, "usr1")
            dep = await build(s).submit(tx_hash=tx_mail, chain_id=97, value=Decimal("100"),
                                        caller=usr)
            check("el deposito queda CONFIRMED igual", dep.status == "CONFIRMED")
            check("y registra que no se aviso a nadie", dep.notified_admins == 0)
    finally:
        cds.email_service = original

    print("\n12. Sin configuracion, falla explicito (no acredita nada)")
    settings.EVM_RECEIVING_ADDRESS = ""
    async with AsyncSessionLocal() as s:
        usr = await s.get(ApiUser, "usr1")
        await expect(build(s).submit(tx_hash=TX, chain_id=97, value=Decimal("100"), caller=usr),
                     "sin address receptora", "EVM_NOT_CONFIGURED")
    settings.EVM_RECEIVING_ADDRESS = RECEIVER
    settings.EVM_USDC_CONTRACT = ""
    async with AsyncSessionLocal() as s:
        usr = await s.get(ApiUser, "usr1")
        await expect(build(s).submit(tx_hash=TX, chain_id=97, value=Decimal("100"), caller=usr),
                     "sin contrato de token", "EVM_NOT_CONFIGURED")
    settings.EVM_USDC_CONTRACT = TOKEN

    print("\n13. Scoping: un USER ve solo sus comprobantes")
    async with AsyncSessionLocal() as s:
        usr = await s.get(ApiUser, "usr1")
        adm = await s.get(ApiUser, "adm1")
        _, total_user = await CryptoDepositService(s).list(caller=usr)
        _, total_admin = await CryptoDepositService(s).list(caller=adm)
        check("el admin ve todo", total_admin >= total_user)
        check("el user ve los suyos", total_user > 0)

    print("\n" + "=" * 66)
    print("FALLOS:", len(fails))
    for f in fails:
        print("  -", f)


asyncio.run(main())
