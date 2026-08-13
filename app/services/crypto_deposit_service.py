"""
Verificacion de depositos on-chain (USDC sobre una cadena EVM) y aviso a los admin.

Un api_user presenta un comprobante: hash de transaccion, chain id y monto. El
servicio NO le cree nada — lo unico que vale es lo que dice la cadena.

Se verifica, en este orden:

1. el `chain_id` declarado es el de la cadena configurada;
2. el nodo conoce la transaccion y ya esta minada;
3. la transaccion no revirtio (`status = 1`);
4. tiene confirmaciones suficientes (protege contra reorgs);
5. sus logs contienen una transferencia del token configurado HACIA nuestra
   address receptora;
6. el monto que llego coincide con el declarado.

Recien entonces se registra como CONFIRMED, se ACREDITA LA CUENTA MAESTRA y se
avisa a los admin.

La acreditacion es un asiento MASTER_ACCOUNT_FUNDING de doble entrada:

    debit  MASTER_ACCOUNT       (sube: hay mas cash disponible para repartir)
    credit EXTERNAL_FUNDING  (contrapartida: el dinero entro de afuera)

Va en el MISMO commit que el deposito. No puede quedar un deposito acreditado
sin asiento, ni plata en la cuenta maestra sin el comprobante on-chain que la
justifica: o entran las dos cosas, o no entra ninguna. `ledger_tx_id` deja el
rastro en ambos sentidos.

La proteccion contra doble acreditacion es doble: el indice unico parcial sobre
`tx_hash` de los CONFIRMED, y la `idempotency_key` unica del asiento
(`crypto-deposit:{tx_hash}`).

DOS COSAS QUE NO HACE, a proposito:

* No decide a que trader se aplica el dinero. Acreditar la cuenta maestra es
  reconocer que entro plata al pozo comun; repartirla a un trader sigue siendo
  una decision aparte, con su propio flujo.
* No confia en `value` para nada mas que compararlo. El monto que se persiste
  —y el que se acredita— es el leido de la cadena.

⚠️ Se asume 1 token = 1 unidad de LEDGER_CURRENCY, valido para stablecoins
   ancladas al dolar. Ver CRYPTO_DEPOSIT_AUTO_CREDIT en config.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import alerts
from app.core.config import settings
from app.core.exceptions import (
    AmountMismatchError,
    AppException,
    ChainMismatchError,
    DepositAlreadyRegisteredError,
    EvmConfigError,
    LedgerInconsistentError,
    TransferNotFoundError,
    TxFailedOnChainError,
    TxHashInvalidError,
    TxNotConfirmedError,
    TxNotFoundError,
    ValidationError,
)
from app.models.api_user import ROLE_ADMIN
from app.models.crypto_deposit import STATUS_CONFIRMED, STATUS_REJECTED, CryptoDeposit
from app.repositories.api_user_repository import ApiUserRepository
from app.repositories.crypto_deposit_repository import CryptoDepositRepository
from app.services import email_service
from app.services.evm_client import get_evm_client, normalize_address, normalize_tx_hash
from app.services.ledger_service import LedgerService

logger = logging.getLogger(__name__)

_ZERO = Decimal("0")

# Exploradores por chain id, solo para armar el link del mail.
_EXPLORERS = {
    97: "https://testnet.bscscan.com/tx/",
    56: "https://bscscan.com/tx/",
    1: "https://etherscan.io/tx/",
    11155111: "https://sepolia.etherscan.io/tx/",
    137: "https://polygonscan.com/tx/",
}


class CryptoDepositService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = CryptoDepositRepository(db)
        self.api_user_repo = ApiUserRepository(db)
        self._client = get_evm_client()

    # ══════════════════════════════════════════════════════════════════
    # helpers
    # ══════════════════════════════════════════════════════════════════
    @staticmethod
    def _ensure_configured() -> tuple[str, str]:
        """Devuelve (address receptora, contrato del token) ya normalizados."""
        receiver = normalize_address(settings.EVM_RECEIVING_ADDRESS)
        contract = normalize_address(settings.EVM_USDC_CONTRACT)
        if not receiver:
            raise EvmConfigError(
                message="No hay address receptora configurada",
                detail="Definir EVM_RECEIVING_ADDRESS en .env (address de la cadena EVM)")
        if not contract:
            raise EvmConfigError(
                message="No hay contrato de token configurado",
                detail=("Definir EVM_USDC_CONTRACT en .env: en BSC testnet no existe un USDC "
                        "canonico, hay que indicar el contrato que usa el proyecto"))
        return receiver, contract

    @staticmethod
    def _to_decimal(raw: int, decimals: int) -> Decimal:
        """Convierte la unidad minima del token a monto legible.

        `normalize()` saca los ceros sobrantes pero devuelve notacion cientifica
        para los redondos (100 -> 1E+2), que en un JSON de montos es confuso y
        rompe a cualquier cliente que parsee el string. Se vuelve a notacion
        plana.
        """
        valor = (Decimal(raw) / (Decimal(10) ** decimals)).normalize()
        if valor == valor.to_integral_value():
            try:
                return valor.quantize(Decimal(1))
            except InvalidOperation:  # monto gigante: se deja como esta
                return valor
        return valor

    @staticmethod
    def _explorer_url(chain_id: int, tx_hash: str) -> str:
        base = _EXPLORERS.get(chain_id)
        return f"{base}{tx_hash}" if base else ""

    async def _reject(self, *, tx_hash: str, chain_id: int, declared: Decimal,
                      caller, error: AppException, **extra: Any) -> None:
        """Deja constancia del intento fallido y propaga el error."""
        deposit = CryptoDeposit(
            api_user_id=getattr(caller, "id", None), tx_hash=tx_hash, chain_id=chain_id,
            status=STATUS_REJECTED, declared_amount=declared,
            token_symbol=settings.EVM_TOKEN_SYMBOL,
            rejection_code=error.code, rejection_detail=(error.detail or error.message)[:500],
            **extra)
        self.repo.add(deposit)
        await self.db.commit()
        logger.info("Deposito on-chain rechazado: tx=%s motivo=%s", tx_hash, error.code)

    # ══════════════════════════════════════════════════════════════════
    # verificacion
    # ══════════════════════════════════════════════════════════════════
    async def submit(self, *, tx_hash: str, chain_id: int, value: Decimal,
                     caller=None) -> CryptoDeposit:
        receiver, contract = self._ensure_configured()

        normalized = normalize_tx_hash(tx_hash)
        if normalized is None:
            raise TxHashInvalidError(
                message="El hash de transaccion no tiene un formato valido",
                detail="Se espera 0x seguido de 64 caracteres hexadecimales")

        declared = Decimal(str(value))
        if declared <= _ZERO:
            raise AmountMismatchError(
                message="El monto debe ser mayor a 0", detail=f"value={value}")

        # Si ya se acredito, cortamos antes de gastar una llamada al nodo.
        if await self.repo.confirmed_exists(normalized):
            raise DepositAlreadyRegisteredError(
                message="Esa transaccion ya fue registrada como deposito",
                detail=f"tx_hash={normalized}")

        # 1. La cadena declarada tiene que ser la configurada.
        if int(chain_id) != int(settings.EVM_CHAIN_ID):
            err = ChainMismatchError(
                message="El chain_id no corresponde a la cadena configurada",
                detail=f"recibido={chain_id}, esperado={settings.EVM_CHAIN_ID} "
                       f"({settings.EVM_NETWORK_NAME})")
            await self._reject(tx_hash=normalized, chain_id=int(chain_id),
                               declared=declared, caller=caller, error=err)
            raise err

        # 2. y 3. La transaccion tiene que existir, estar minada y no haber revertido.
        receipt = await self._client.get_receipt(normalized)
        if receipt is None:
            err = TxNotFoundError(
                message="La cadena no conoce esa transaccion todavia",
                detail=("Puede seguir pendiente en el mempool. Reintentar cuando este "
                        "minada."))
            await self._reject(tx_hash=normalized, chain_id=chain_id, declared=declared,
                               caller=caller, error=err)
            raise err

        if not receipt.success:
            err = TxFailedOnChainError(
                message="La transaccion revirtio en la cadena: no movio fondos",
                detail=f"tx_hash={normalized} block={receipt.block_number}")
            await self._reject(tx_hash=normalized, chain_id=chain_id, declared=declared,
                               caller=caller, error=err,
                               block_number=receipt.block_number,
                               from_address=receipt.from_address)
            raise err

        # 4. Confirmaciones: protege contra reorganizaciones de la cadena.
        latest = await self._client.block_number()
        confirmations = (latest - receipt.block_number + 1) if latest is not None else 0
        if confirmations < settings.EVM_MIN_CONFIRMATIONS:
            err = TxNotConfirmedError(
                message="La transaccion aun no tiene confirmaciones suficientes",
                detail=(f"{confirmations} de {settings.EVM_MIN_CONFIRMATIONS} requeridas. "
                        f"Reintentar en unos minutos."))
            await self._reject(tx_hash=normalized, chain_id=chain_id, declared=declared,
                               caller=caller, error=err,
                               block_number=receipt.block_number,
                               confirmations=max(confirmations, 0),
                               from_address=receipt.from_address)
            raise err

        # 5. Tiene que haber una transferencia DEL token configurado HACIA nuestra address.
        matches = [
            t for t in receipt.transfers
            if t.token_contract == contract and t.to_address == receiver
        ]
        if not matches:
            err = TransferNotFoundError(
                message="La transaccion no contiene una transferencia del token esperado "
                        "hacia la address receptora",
                detail=(f"token={contract} destino={receiver}. "
                        f"Transferencias encontradas: {len(receipt.transfers)}"))
            await self._reject(tx_hash=normalized, chain_id=chain_id, declared=declared,
                               caller=caller, error=err,
                               block_number=receipt.block_number, confirmations=confirmations,
                               from_address=receipt.from_address, to_address=receipt.to_address)
            raise err

        decimals = settings.EVM_TOKEN_DECIMALS
        raw_total = sum(t.raw_amount for t in matches)
        onchain = self._to_decimal(raw_total, decimals)

        # 6. El monto declarado tiene que coincidir con el que realmente llego.
        if onchain != declared:
            err = AmountMismatchError(
                message="El monto declarado no coincide con el que llego on-chain",
                detail=f"declarado={declared} on-chain={onchain} {settings.EVM_TOKEN_SYMBOL}")
            await self._reject(tx_hash=normalized, chain_id=chain_id, declared=declared,
                               caller=caller, error=err,
                               onchain_amount=onchain, raw_amount=str(raw_total),
                               token_contract=contract, token_decimals=decimals,
                               block_number=receipt.block_number, confirmations=confirmations,
                               from_address=matches[0].from_address, to_address=receiver)
            raise err

        deposit = CryptoDeposit(
            api_user_id=getattr(caller, "id", None), tx_hash=normalized, chain_id=chain_id,
            status=STATUS_CONFIRMED, declared_amount=declared, onchain_amount=onchain,
            raw_amount=str(raw_total), token_symbol=settings.EVM_TOKEN_SYMBOL,
            token_contract=contract, token_decimals=decimals,
            from_address=matches[0].from_address, to_address=receiver,
            block_number=receipt.block_number, confirmations=confirmations,
        )
        self.repo.add(deposit)
        saldo_maestra: Optional[Decimal] = None
        try:
            # El flush dispara ya el indice unico: si otro request acredito este
            # mismo hash, cortamos antes de tocar el ledger.
            await self.db.flush()
            if settings.CRYPTO_DEPOSIT_AUTO_CREDIT:
                deposit.ledger_tx_id, saldo_maestra = await self._credit_master_account(deposit)
            await self.db.commit()
        except IntegrityError as exc:
            # uq_crypto_deposits_confirmed_tx (o la idempotency_key del asiento):
            # otro request la acredito primero.
            await self.db.rollback()
            raise DepositAlreadyRegisteredError(
                message="Esa transaccion ya fue registrada como deposito",
                detail=f"tx_hash={normalized}") from exc
        except (RuntimeError, ValueError) as exc:
            # El ledger no pudo postear. Se cae TODO: preferimos que el usuario
            # reintente antes que dar por bueno un deposito que no entro a los
            # libros. Sin esto, habria plata reconocida y no contabilizada.
            await self.db.rollback()
            logger.exception("No se pudo acreditar la cuenta maestra: tx=%s", normalized)
            raise LedgerInconsistentError(
                message="El deposito se verifico en la cadena pero no se pudo acreditar "
                        "en el libro contable. No se registro nada: reintentar.",
                detail=f"tx_hash={normalized}: {type(exc).__name__}: {exc}") from exc

        logger.info("Deposito on-chain confirmado: tx=%s monto=%s %s bloque=%s asiento=%s",
                    normalized, onchain, settings.EVM_TOKEN_SYMBOL, receipt.block_number,
                    deposit.ledger_tx_id or "(sin acreditar)")
        await self._notify_admins(deposit, saldo_maestra=saldo_maestra)
        return await self.repo.get_by_id(deposit.id) or deposit

    async def _credit_master_account(self, deposit: CryptoDeposit) -> tuple[str, Decimal]:
        """Acredita la cuenta maestra con el monto leido de la cadena.

        Corre dentro de la transaccion abierta por `submit`: el caller commitea.
        La `idempotency_key` derivada del hash hace que la misma transaccion
        on-chain no pueda generar dos asientos ni aunque llegue por dos caminos.
        """
        ledger = LedgerService(self.db)
        monto = Decimal(str(deposit.onchain_amount))
        tx_id = await ledger.fund_master_account(
            amount=monto,
            idempotency_key=f"crypto-deposit:{deposit.tx_hash}",
            description=(f"Deposito on-chain de {monto} {deposit.token_symbol} "
                         f"en {settings.EVM_NETWORK_NAME} (tx {deposit.tx_hash})"),
        )
        # Los asientos se agregaron a la sesion pero todavia no estan en la base:
        # sin este flush, el saldo recomputado no los veria.
        await self.db.flush()
        balance, _pending, _disponible = await ledger.master_account_available()
        return tx_id, balance

    # ══════════════════════════════════════════════════════════════════
    # aviso a los admin
    # ══════════════════════════════════════════════════════════════════
    async def _notify_admins(self, deposit: CryptoDeposit,
                             *, saldo_maestra: Optional[Decimal] = None) -> None:
        """Avisa a todos los api_user ADMIN activos.

        Un fallo de correo NO invalida el deposito: ya esta verificado, guardado
        y acreditado. Se registra cuantos avisos salieron y se alerta si no
        salio ninguno.
        """
        # `receives_notifications` deja afuera a las cuentas de servicio (el
        # usuario de los cron jobs es ADMIN, pero su email no existe).
        admins = [u for u in await self.api_user_repo.list()
                  if u.role == ROLE_ADMIN and u.is_active and u.email
                  and u.receives_notifications]
        if not admins:
            alerts.alert(alerts.PROVIDER_SERVER_ERROR,
                         "Deposito on-chain confirmado pero no hay admins activos a quien avisar",
                         tx_hash=deposit.tx_hash)
            return

        enviados = 0
        explorer = self._explorer_url(deposit.chain_id, deposit.tx_hash)
        for admin in admins:
            try:
                ok = await email_service.send_crypto_deposit_email(
                    to=admin.email, tx_hash=deposit.tx_hash,
                    amount=Decimal(str(deposit.onchain_amount)),
                    symbol=deposit.token_symbol, network=settings.EVM_NETWORK_NAME,
                    from_address=deposit.from_address or "", block=deposit.block_number or 0,
                    explorer=explorer, ledger_tx_id=deposit.ledger_tx_id,
                    saldo_maestra=saldo_maestra)
                enviados += 1 if ok else 0
            except AppException as exc:
                logger.warning("No se pudo avisar a %s del deposito %s: %s",
                               admin.email, deposit.tx_hash, exc.code)

        deposit.notified_admins = enviados
        deposit.notified_at = datetime.now(timezone.utc) if enviados else None
        await self.db.commit()

        if enviados == 0:
            alerts.alert(alerts.PROVIDER_SERVER_ERROR,
                         "Deposito on-chain confirmado pero no se pudo avisar a ningun admin",
                         tx_hash=deposit.tx_hash, admins=len(admins))
        logger.info("Deposito %s: avisados %s de %s admins",
                    deposit.tx_hash, enviados, len(admins))

    # ══════════════════════════════════════════════════════════════════
    # consultas
    # ══════════════════════════════════════════════════════════════════
    async def list(self, *, status: Optional[str] = None, rejection_code: Optional[str] = None,
                   limit: int = 50, offset: int = 0,
                   caller=None) -> tuple[list[CryptoDeposit], int]:
        # Un `status` invalido devolveria una lista vacia en silencio, que se lee
        # como "no hay datos" cuando en realidad el filtro estaba mal escrito.
        if status and status not in (STATUS_CONFIRMED, STATUS_REJECTED):
            raise ValidationError(
                message=f"`status` solo acepta {STATUS_CONFIRMED} o {STATUS_REJECTED}",
                detail=(f"recibido: {status!r}. Si buscabas el MOTIVO de un rechazo "
                        f"(ej. AMOUNT_MISMATCH), usa el parametro `rejection_code`."))
        scope = None if (caller is None or caller.role == ROLE_ADMIN) else caller.id
        return await self.repo.list(status=status, rejection_code=rejection_code,
                                    api_user_id=scope, limit=limit, offset=offset)
