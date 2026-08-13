"""
Levanta un escenario completo de depositos on-chain en una cadena EVM LOCAL.

Sirve para desarrollar y probar sin depender de un faucet ni de una testnet
publica: despliega el token de prueba, hace una transferencia real hacia la
address receptora y te imprime el `.env` y el hash listos para usar.

    # 1. en otra terminal, levantar la cadena local:
    npx ganache --chain.chainId 97 --port 8546 --wallet.deterministic

    # 2. armar el escenario:
    python scripts/local_evm_setup.py

    # opcional: monto a transferir (por defecto 100)
    python scripts/local_evm_setup.py 250.5

    # reusar el token YA desplegado (no cambia EVM_USDC_CONTRACT, no hay que
    # tocar el .env): genera solo una transferencia nueva
    python scripts/local_evm_setup.py 75.5 --reusar

Las cuentas son las deterministas de ganache, siempre las mismas.
"""
from __future__ import annotations

import asyncio
import os
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402
from eth_account import Account  # noqa: E402
from eth_utils import to_checksum_address  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CONTRACT = ROOT / "contracts" / "TestUSDC.sol"
SOLC_VERSION = "0.8.20"

RPC = os.environ.get("LOCAL_EVM_RPC", "http://127.0.0.1:8546")
CHAIN_ID = int(os.environ.get("LOCAL_EVM_CHAIN_ID", "97"))
DECIMALS = 18

# Cuentas deterministas de ganache (--wallet.deterministic): siempre iguales.
DEPLOYER_KEY = "0x4f3edf983ac636a65a842ce7c78d9aa706d3b113bce9c46f30d7d21715b23b1d"
RECEIVER = "0xFFcf8FDEE72ac11b5c542428B35EEF5769C409f0"  # cuenta (1)

# keccak("transfer(address,uint256)")[:4]
TRANSFER_SELECTOR = "0xa9059cbb"


async def rpc(method: str, params: list):
    async with httpx.AsyncClient(timeout=60.0) as c:
        r = await c.post(RPC, json={"jsonrpc": "2.0", "id": 1,
                                    "method": method, "params": params})
        r.raise_for_status()
        body = r.json()
    if body.get("error"):
        raise RuntimeError(f"{method}: {body['error']}")
    return body.get("result")


def _int(v) -> int:
    return int(v, 16) if isinstance(v, str) else int(v or 0)


def _pad(valor: str) -> str:
    """Rellena con ceros a la izquierda hasta 32 bytes (64 hexadecimales)."""
    return valor.lower().replace("0x", "").rjust(64, "0")


async def enviar(cuenta, *, to: str | None, data: str) -> dict:
    nonce = _int(await rpc("eth_getTransactionCount", [cuenta.address, "pending"]))
    gas_price = _int(await rpc("eth_gasPrice", []))
    llamada = {"from": cuenta.address, "data": data}
    if to:
        llamada["to"] = to
    gas = _int(await rpc("eth_estimateGas", [llamada]))

    tx = {"chainId": CHAIN_ID, "nonce": nonce, "gas": int(gas * 1.3),
          "gasPrice": gas_price, "data": data, "value": 0}
    if to:
        # eth_account exige el destino en formato checksum (EIP-55).
        tx["to"] = to_checksum_address(to)

    firmada = Account.sign_transaction(tx, cuenta.key)
    raw = getattr(firmada, "raw_transaction", None) or getattr(firmada, "rawTransaction")
    tx_hash = await rpc("eth_sendRawTransaction", ["0x" + raw.hex().lstrip("0x")])

    for _ in range(40):
        recibo = await rpc("eth_getTransactionReceipt", [tx_hash])
        if recibo and recibo.get("blockNumber"):
            if _int(recibo.get("status")) != 1:
                raise RuntimeError(f"la transaccion {tx_hash} revirtio")
            return recibo
        await asyncio.sleep(0.3)
    raise RuntimeError(f"la transaccion {tx_hash} no se confirmo")


def compilar() -> str:
    import solcx

    if SOLC_VERSION not in [str(v) for v in solcx.get_installed_solc_versions()]:
        solcx.install_solc(SOLC_VERSION)
    salida = solcx.compile_files([str(CONTRACT)], output_values=["bin"],
                                 solc_version=SOLC_VERSION)
    return salida[next(k for k in salida if k.endswith(":TestUSDC"))]["bin"]


async def main() -> int:
    args = [a for a in sys.argv[1:] if a]
    reusar = "--reusar" in args
    montos = [a for a in args if not a.startswith("--")]
    monto = Decimal(montos[0]) if montos else Decimal("100")

    try:
        chain = _int(await rpc("eth_chainId", []))
    except Exception:
        print(f"\nNo hay ninguna cadena escuchando en {RPC}.")
        print("Levantala con:")
        print("  npx ganache --chain.chainId 97 --port 8546 --wallet.deterministic")
        return 1

    print(f"\nCadena local: {RPC} (chain id {chain})")
    deployer = Account.from_key(DEPLOYER_KEY)
    print(f"Deployer: {deployer.address}")
    print(f"Receptora: {RECEIVER}")

    if reusar:
        from app.core.config import settings

        contrato = (settings.EVM_USDC_CONTRACT or "").strip().lower()
        if not contrato:
            print("\nERROR: --reusar necesita EVM_USDC_CONTRACT cargado en el .env.")
            print("Corre el script sin --reusar para desplegar el token primero.")
            return 1
        codigo = await rpc("eth_getCode", [contrato, "latest"])
        if not codigo or codigo == "0x":
            print(f"\nERROR: en {contrato} no hay ningun contrato en esta cadena.")
            print("Seguro reiniciaste ganache: corre el script SIN --reusar para desplegar de nuevo.")
            return 1
        print(f"\nReusando el token ya desplegado: {contrato}")
    else:
        print("\nCompilando y desplegando TestUSDC...")
        bytecode = compilar()
        recibo = await enviar(deployer, to=None,
                              data=bytecode if bytecode.startswith("0x") else "0x" + bytecode)
        contrato = str(recibo["contractAddress"]).lower()
        print(f"  contrato: {contrato}")

    crudo = int(monto * (Decimal(10) ** DECIMALS))
    print(f"\nTransfiriendo {monto} USDC a la address receptora...")
    data = TRANSFER_SELECTOR + _pad(RECEIVER) + _pad(hex(crudo))
    recibo = await enviar(deployer, to=contrato, data=data)
    tx_hash = str(recibo["transactionHash"]).lower()
    print(f"  tx: {tx_hash}")
    print(f"  bloque: {_int(recibo['blockNumber'])}")

    print("\n" + "=" * 70)
    print("  ESCENARIO LISTO")
    print("=" * 70)
    if reusar:
        print("\n  (se reuso el token existente: NO hace falta tocar el .env)")
    else:
        print("\n  .env para probar contra la cadena local:\n")
        print(f"    EVM_RPC_URL={RPC}")
        print(f"    EVM_CHAIN_ID={CHAIN_ID}")
        print(f"    EVM_RECEIVING_ADDRESS={RECEIVER.lower()}")
        print(f"    EVM_USDC_CONTRACT={contrato}")
        print("    EVM_TOKEN_DECIMALS=18")
        print("    EVM_TOKEN_SYMBOL=USDC")
        print("    EVM_MIN_CONFIRMATIONS=1")
    print("\n  Probar el endpoint con este comprobante:\n")
    print("    curl -X POST \"$BASE/api/v1/crypto-deposits\" \\")
    print("         -H \"X-API-Key: $KEY\" -H \"Content-Type: application/json\" \\")
    print(f"         -d '{{\"tx_hash\":\"{tx_hash}\",\"chain_id\":{CHAIN_ID},"
          f"\"value\":\"{monto}\"}}'")
    print(f"\n  Inspeccionar la transferencia:\n")
    print(f"    python scripts/evm_token_info.py --tx {tx_hash}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
