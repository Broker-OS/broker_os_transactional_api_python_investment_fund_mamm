"""
Laboratorio de pruebas sobre la BNB Smart Chain testnet (chain 97, red publica real).

Despliega el token USDL ("Dolares de Luis"), crea cuentas de prueba con sus
claves publica y privada, las fondea con tBNB (gas) y con USDL, y genera pagos
hacia la address receptora del bridge — devolviendo el tx_hash listo para
presentarlo a la API.

Objetivo: no volver a depender del faucet (10 USDC cada 24 horas).

    python scripts/testnet_lab.py init            # crea la tesoreria y te dice que fondear
    python scripts/testnet_lab.py desplegar       # despliega USDL en la testnet
    python scripts/testnet_lab.py cuentas 5       # 5 cuentas nuevas, con tBNB y USDL
    python scripts/testnet_lab.py estado          # tabla de saldos
    python scripts/testnet_lab.py claves          # direcciones + claves privadas
    python scripts/testnet_lab.py pagar luis-1 25 # esa cuenta paga 25 USDL al bridge

⚠️ SOLO TESTNET. Las claves que genera son descartables y no controlan nada de
   valor. Se guardan en `.testnet-lab.json`, que esta en .gitignore.

NOTA DE DISENO: firmar y enviar transacciones vive SOLO en los scripts. El
cliente que usa la API (`app/services/evm_client.py`) es de solo lectura a
proposito: no puede mover fondos ni aunque alguien lo quisiera.
"""
from __future__ import annotations

import json
import os
import sys
import time
from decimal import Decimal
from pathlib import Path

import httpx
from eth_account import Account
from eth_utils import keccak, to_checksum_address

ROOT = Path(__file__).resolve().parent.parent
ESTADO = ROOT / ".testnet-lab.json"
CONTRATO_SOL = ROOT / "contracts" / "USDL.sol"
SOLC_VERSION = "0.8.20"

# Red publica de pruebas de BNB. Fija a proposito: este script es solo para
# testnet, y asi no depende de como este apuntando el .env en cada momento.
RPC_URL = os.environ.get("LAB_RPC_URL", "https://data-seed-prebsc-1-s1.bnbchain.org:8545")
CHAIN_ID = int(os.environ.get("LAB_CHAIN_ID", "97"))
EXPLORADOR = "https://testnet.bscscan.com"

# A donde cobra el bridge. Tiene que coincidir con EVM_RECEIVING_ADDRESS del server.
RECEPTORA = os.environ.get(
    "LAB_RECEIVING_ADDRESS", "0xed50040f721093d385a74ae4b89ebda46980d700").lower()

DECIMALES = 18
GAS_PRICE_MINIMO = 3 * 10**9  # 3 gwei: piso habitual de BSC testnet


# ────────────────────────── JSON-RPC ──────────────────────────
_cliente = httpx.Client(timeout=60.0)


def rpc(metodo: str, params: list | None = None):
    r = _cliente.post(RPC_URL, json={"jsonrpc": "2.0", "id": 1,
                                     "method": metodo, "params": params or []})
    r.raise_for_status()
    cuerpo = r.json()
    if cuerpo.get("error"):
        raise RuntimeError(f"{metodo}: {cuerpo['error']}")
    return cuerpo.get("result")


def i16(v) -> int:
    return int(v, 16) if isinstance(v, str) else int(v or 0)


def sel(firma: str) -> str:
    """Selector de funcion: primeros 4 bytes del keccak de la firma."""
    return "0x" + keccak(text=firma)[:4].hex()


def pad(v) -> str:
    """Codifica un valor a una palabra de 32 bytes."""
    if isinstance(v, int):
        return format(v, "064x")
    return str(v).lower().replace("0x", "").rjust(64, "0")


def a_crudo(monto) -> int:
    return int(Decimal(str(monto)) * (Decimal(10) ** DECIMALES))


def a_humano(crudo: int) -> Decimal:
    valor = (Decimal(crudo) / (Decimal(10) ** DECIMALES)).normalize()
    # normalize() deja 1000000 como 1E+6: feo e ilegible en una tabla de saldos.
    if valor == valor.to_integral_value():
        return valor.quantize(Decimal(1))
    return valor


# ────────────────────────── estado en disco ──────────────────────────
def cargar() -> dict:
    if ESTADO.exists():
        return json.loads(ESTADO.read_text(encoding="utf-8"))
    return {"tesoreria": None, "contrato": None, "cuentas": []}


def guardar(datos: dict) -> None:
    ESTADO.write_text(json.dumps(datos, indent=2), encoding="utf-8")


# ────────────────────────── envio de transacciones ──────────────────────────
def esperar_recibo(tx_hash: str, intentos: int = 90) -> dict:
    for _ in range(intentos):
        recibo = rpc("eth_getTransactionReceipt", [tx_hash])
        if recibo and recibo.get("blockNumber"):
            if i16(recibo.get("status")) != 1:
                raise RuntimeError(f"la transaccion revirtio: {EXPLORADOR}/tx/{tx_hash}")
            return recibo
        time.sleep(2)
    raise RuntimeError(f"no se confirmo a tiempo: {EXPLORADOR}/tx/{tx_hash}")


def enviar(clave: str, *, to: str | None = None, data: str = "0x",
           value: int = 0, gas: int | None = None) -> dict:
    cuenta = Account.from_key(clave)
    llamada = {"from": cuenta.address, "data": data, "value": hex(value)}
    if to:
        llamada["to"] = to_checksum_address(to)

    if gas is None:
        gas = int(i16(rpc("eth_estimateGas", [llamada])) * 1.25)
    precio = max(i16(rpc("eth_gasPrice")), GAS_PRICE_MINIMO)
    nonce = i16(rpc("eth_getTransactionCount", [cuenta.address, "pending"]))

    tx = {"chainId": CHAIN_ID, "nonce": nonce, "gas": gas,
          "gasPrice": precio, "data": data, "value": value}
    if to:
        # eth_account rechaza direcciones en minusculas: exige checksum.
        tx["to"] = to_checksum_address(to)

    firmada = Account.sign_transaction(tx, clave)
    crudo = getattr(firmada, "raw_transaction", None) or firmada.rawTransaction
    tx_hash = rpc("eth_sendRawTransaction", ["0x" + crudo.hex().removeprefix("0x")])
    return esperar_recibo(tx_hash)


# ────────────────────────── lecturas del token ──────────────────────────
def saldo_usdl(contrato: str, quien: str) -> Decimal:
    crudo = rpc("eth_call", [{"to": contrato,
                              "data": sel("balanceOf(address)") + pad(quien)}, "latest"])
    return a_humano(i16(crudo))


def saldo_tbnb(quien: str) -> Decimal:
    return (Decimal(i16(rpc("eth_getBalance", [quien, "latest"]))) /
            Decimal(10**18)).quantize(Decimal("0.000001"))


# ────────────────────────── comandos ──────────────────────────
def cmd_init() -> int:
    datos = cargar()
    if datos["tesoreria"]:
        cuenta = datos["tesoreria"]
        print(f"\nLa tesoreria ya existe: {cuenta['address']}")
    else:
        nueva = Account.create()
        datos["tesoreria"] = {"address": nueva.address,
                              "clave": nueva.key.hex()}
        guardar(datos)
        cuenta = datos["tesoreria"]
        print("\nTesoreria creada.")

    saldo = saldo_tbnb(cuenta["address"])
    print("\n" + "=" * 70)
    print("  PASO MANUAL — UNA SOLA VEZ")
    print("=" * 70)
    print(f"\n  Mandale tBNB a esta direccion desde MetaMask:\n")
    print(f"      {cuenta['address']}\n")
    print("  MetaMask -> red BSC Testnet -> Enviar -> pegar esa direccion")
    print("  Monto sugerido: 0.1 tBNB  (te sobra para cientos de operaciones)")
    print(f"\n  Saldo actual de la tesoreria: {saldo} tBNB")
    if saldo > 0:
        print("\n  Ya tiene fondos. Seguí con:")
        print("      python scripts/testnet_lab.py desplegar")
    else:
        print("\n  Cuando llegue, seguí con:")
        print("      python scripts/testnet_lab.py desplegar")
    return 0


def _tesoreria(datos: dict) -> dict:
    if not datos.get("tesoreria"):
        print("Primero corré:  python scripts/testnet_lab.py init")
        raise SystemExit(1)
    return datos["tesoreria"]


def cmd_desplegar() -> int:
    datos = cargar()
    tes = _tesoreria(datos)

    if datos.get("contrato"):
        codigo = rpc("eth_getCode", [datos["contrato"], "latest"])
        if codigo and codigo != "0x":
            print(f"\nUSDL ya esta desplegado: {datos['contrato']}")
            print(f"  {EXPLORADOR}/address/{datos['contrato']}")
            return 0

    saldo = saldo_tbnb(tes["address"])
    print(f"\nTesoreria: {tes['address']}  ({saldo} tBNB)")
    if saldo == 0:
        print("\nNo tiene tBNB todavia. Mandale desde MetaMask y volvé a correr esto.")
        return 1

    print("\nCompilando USDL.sol...")
    import solcx
    if SOLC_VERSION not in [str(v) for v in solcx.get_installed_solc_versions()]:
        print(f"  descargando solc {SOLC_VERSION}...")
        solcx.install_solc(SOLC_VERSION)
    compilado = solcx.compile_files([str(CONTRATO_SOL)],
                                    output_values=["abi", "bin"],
                                    solc_version=SOLC_VERSION)
    clave_c = next(k for k in compilado if k.endswith(":USDL"))
    bytecode = "0x" + compilado[clave_c]["bin"].removeprefix("0x")
    print(f"  bytecode: {len(bytecode) // 2} bytes")

    print("\nDesplegando en la BNB testnet...")
    recibo = enviar(tes["clave"], data=bytecode)
    contrato = str(recibo["contractAddress"]).lower()

    datos["contrato"] = contrato
    guardar(datos)
    (ROOT / "contracts" / "USDL.abi.json").write_text(
        json.dumps(compilado[clave_c]["abi"], indent=2), encoding="utf-8")

    print("\n" + "=" * 70)
    print("  USDL DESPLEGADO EN LA BNB TESTNET")
    print("=" * 70)
    print(f"\n  Contrato   : {contrato}")
    print(f"  Bloque     : {i16(recibo['blockNumber'])}")
    print(f"  Explorador : {EXPLORADOR}/address/{contrato}")
    print(f"\n  La tesoreria recibio 1.000.000 USDL.")
    print("\n  Falta ponerlo en el servidor:")
    print(f"      EVM_USDC_CONTRACT={contrato}")
    print("      EVM_TOKEN_SYMBOL=USDL")
    print("\n  Seguí con:")
    print("      python scripts/testnet_lab.py cuentas 5")
    return 0


def cmd_cuentas(cantidad: int, usdl: str, tbnb: str) -> int:
    datos = cargar()
    tes = _tesoreria(datos)
    contrato = datos.get("contrato")
    if not contrato:
        print("Primero desplegá el token:  python scripts/testnet_lab.py desplegar")
        return 1

    gas_wei = int(Decimal(str(tbnb)) * Decimal(10**18))
    disponible = saldo_tbnb(tes["address"])
    necesario = Decimal(str(tbnb)) * cantidad + Decimal("0.01")
    if disponible < necesario:
        print(f"\nLa tesoreria tiene {disponible} tBNB y hacen falta ~{necesario}.")
        print(f"Mandale mas desde MetaMask a {tes['address']}")
        return 1

    base = len(datos["cuentas"])
    nuevas = []
    for i in range(cantidad):
        c = Account.create()
        nuevas.append({"alias": f"luis-{base + i + 1}",
                       "address": c.address, "clave": c.key.hex()})
    datos["cuentas"].extend(nuevas)
    guardar(datos)

    print(f"\n{cantidad} cuentas creadas. Fondeando...")

    # 1) tBNB para el gas — una transferencia simple por cuenta.
    for c in nuevas:
        enviar(tes["clave"], to=c["address"], value=gas_wei, gas=21000)
        print(f"  {c['alias']:8s} {c['address']}  <- {tbnb} tBNB")

    # 2) USDL — todas en una sola transaccion con mintBatch.
    monto = a_crudo(usdl)
    direcciones = [c["address"] for c in nuevas]
    data = (sel("mintBatch(address[],uint256)")
            + pad(0x40)                 # offset del array dinamico
            + pad(monto)
            + pad(len(direcciones))
            + "".join(pad(d) for d in direcciones))
    recibo = enviar(tes["clave"], to=contrato, data=data)
    print(f"\n  mintBatch: {usdl} USDL a cada una  (1 sola tx)")
    print(f"  {EXPLORADOR}/tx/{recibo['transactionHash']}")

    print("\n" + "=" * 70)
    print("  CUENTAS LISTAS")
    print("=" * 70)
    for c in nuevas:
        print(f"\n  {c['alias']}")
        print(f"    clave publica  (address) : {c['address']}")
        print(f"    clave privada            : {c['clave']}")
    print(f"\n  Ya pueden pagar:")
    print(f"      python scripts/testnet_lab.py pagar {nuevas[0]['alias']} 25")
    return 0


def cmd_estado() -> int:
    datos = cargar()
    contrato = datos.get("contrato")
    print(f"\nRed      : BNB Smart Chain testnet (chain {CHAIN_ID}) — publica")
    print(f"RPC      : {RPC_URL}")
    print(f"Bloque   : {i16(rpc('eth_blockNumber'))}")
    print(f"Token    : {contrato or '(sin desplegar)'}")
    print(f"Receptora: {RECEPTORA}")

    if not datos.get("tesoreria"):
        print("\n(sin tesoreria — corré `init`)")
        return 0

    filas = [("tesoreria", datos["tesoreria"]["address"])]
    filas += [(c["alias"], c["address"]) for c in datos["cuentas"]]
    if contrato:
        filas.append(("RECEPTORA", RECEPTORA))

    print(f"\n  {'alias':<12} {'address':<44} {'tBNB':>10} {'USDL':>14}")
    print("  " + "-" * 82)
    for alias, addr in filas:
        u = saldo_usdl(contrato, addr) if contrato else Decimal(0)
        print(f"  {alias:<12} {addr:<44} {saldo_tbnb(addr):>10} {u:>14}")
    return 0


def cmd_claves() -> int:
    datos = cargar()
    if not datos.get("tesoreria"):
        print("\n(sin cuentas — corré `init`)")
        return 0
    print("\n  ⚠️  Claves de TESTNET. No controlan nada de valor real.")
    print(f"\n  tesoreria")
    print(f"    address : {datos['tesoreria']['address']}")
    print(f"    privada : {datos['tesoreria']['clave']}")
    for c in datos["cuentas"]:
        print(f"\n  {c['alias']}")
        print(f"    address : {c['address']}")
        print(f"    privada : {c['clave']}")
    print(f"\n  Para importarlas en MetaMask: Agregar cuenta -> Importar cuenta -> pegar la privada")
    return 0


def cmd_pagar(alias: str, monto: str) -> int:
    datos = cargar()
    contrato = datos.get("contrato")
    if not contrato:
        print("Primero desplegá el token:  python scripts/testnet_lab.py desplegar")
        return 1

    todas = datos["cuentas"] + ([dict(datos["tesoreria"], alias="tesoreria")]
                                if datos.get("tesoreria") else [])
    cuenta = next((c for c in todas if c["alias"] == alias), None)
    if cuenta is None:
        print(f"No existe la cuenta '{alias}'. Disponibles: "
              f"{', '.join(c['alias'] for c in todas)}")
        return 1

    tiene = saldo_usdl(contrato, cuenta["address"])
    if tiene < Decimal(str(monto)):
        print(f"\n{alias} tiene {tiene} USDL y quiere pagar {monto}.")
        return 1
    if saldo_tbnb(cuenta["address"]) == 0:
        print(f"\n{alias} no tiene tBNB para el gas.")
        return 1

    print(f"\n{alias} ({cuenta['address']}) paga {monto} USDL a {RECEPTORA}...")
    data = sel("transfer(address,uint256)") + pad(RECEPTORA) + pad(a_crudo(monto))
    recibo = enviar(cuenta["clave"], to=contrato, data=data)
    tx_hash = recibo["transactionHash"]

    print("\n" + "=" * 70)
    print("  PAGO HECHO — comprobante listo para la API")
    print("=" * 70)
    print(f"\n  {EXPLORADOR}/tx/{tx_hash}")
    print(f"\n  Pegá esto en POST /api/v1/crypto-deposits:\n")
    print(json.dumps({"tx_hash": tx_hash, "chain_id": CHAIN_ID,
                      "value": str(monto)}, indent=2))
    print(f"\n  (el servidor pide 12 confirmaciones: esperá ~30 segundos)")
    return 0


AYUDA = __doc__


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "ayuda"):
        print(AYUDA)
        return 0

    cmd, resto = args[0], args[1:]
    if cmd == "init":
        return cmd_init()
    if cmd == "desplegar":
        return cmd_desplegar()
    if cmd == "cuentas":
        cantidad = int(resto[0]) if resto else 5
        usdl = os.environ.get("LAB_USDL", "10000")
        tbnb = os.environ.get("LAB_TBNB", "0.005")
        return cmd_cuentas(cantidad, usdl, tbnb)
    if cmd == "estado":
        return cmd_estado()
    if cmd == "claves":
        return cmd_claves()
    if cmd == "pagar":
        if len(resto) < 2:
            print("Uso: python scripts/testnet_lab.py pagar <alias> <monto>")
            return 1
        return cmd_pagar(resto[0], resto[1])

    print(f"Comando desconocido: {cmd}")
    print(AYUDA)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
