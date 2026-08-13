"""
Despliega `contracts/TestUSDC.sol` en BSC testnet y deja listo el `.env`.

    python scripts/deploy_test_token.py

Primera corrida: genera una wallet de despliegue, la guarda en `.env` como
DEPLOYER_PRIVATE_KEY y te dice que la fondees en el faucet (unico paso manual:
el faucet pide captcha).

Segunda corrida: compila, despliega y te imprime el contrato para EVM_USDC_CONTRACT.

⚠️ SOLO TESTNET. La clave que genera es de una wallet descartable, sin valor
   real. Aun asi vive en `.env`, que esta en .gitignore.

NOTA DE DISENO: firmar y enviar transacciones vive SOLO en este script. El
cliente que usa la API en produccion (`app/services/evm_client.py`) es de solo
lectura a proposito: no puede mover fondos ni aunque alguien lo quisiera.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402
from eth_account import Account  # noqa: E402

from app.core.config import settings  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
CONTRACT = ROOT / "contracts" / "TestUSDC.sol"
SOLC_VERSION = "0.8.20"
FAUCET = "https://www.bnbchain.org/en/testnet-faucet"


# ── JSON-RPC minimo (con capacidad de envio, a diferencia del cliente de la app) ──
async def rpc(method: str, params: list):
    async with httpx.AsyncClient(timeout=60.0) as c:
        r = await c.post(settings.EVM_RPC_URL,
                         json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        r.raise_for_status()
        body = r.json()
    if body.get("error"):
        raise RuntimeError(f"{method}: {body['error']}")
    return body.get("result")


def _hex_to_int(v) -> int:
    return int(v, 16) if isinstance(v, str) else int(v or 0)


# ── clave del deployer ──
def leer_clave() -> str | None:
    clave = os.environ.get("DEPLOYER_PRIVATE_KEY", "").strip()
    if clave:
        return clave
    if ENV_FILE.exists():
        for linea in ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
            if linea.strip().startswith("DEPLOYER_PRIVATE_KEY="):
                return linea.split("=", 1)[1].strip()
    return None


def guardar_clave(clave: str) -> None:
    contenido = ENV_FILE.read_text(encoding="utf-8", errors="replace") if ENV_FILE.exists() else ""
    if not contenido.endswith("\n"):
        contenido += "\n"
    contenido += (
        "\n# ─────────── Deploy del token de prueba (SOLO TESTNET) ───────────\n"
        "# Wallet descartable usada por scripts/deploy_test_token.py.\n"
        "# No tiene valor real. No reutilizar en mainnet.\n"
        f"DEPLOYER_PRIVATE_KEY={clave}\n"
    )
    ENV_FILE.write_text(contenido, encoding="utf-8")


def compilar() -> tuple[str, list]:
    import solcx

    instaladas = [str(v) for v in solcx.get_installed_solc_versions()]
    if SOLC_VERSION not in instaladas:
        print(f"  Descargando el compilador solc {SOLC_VERSION}...")
        solcx.install_solc(SOLC_VERSION)
    compilado = solcx.compile_files(
        [str(CONTRACT)], output_values=["abi", "bin"], solc_version=SOLC_VERSION)
    clave = next(k for k in compilado if k.endswith(":TestUSDC"))
    return compilado[clave]["bin"], compilado[clave]["abi"]


async def main() -> int:
    if not CONTRACT.exists():
        print(f"ERROR: no encuentro {CONTRACT}")
        return 1

    print(f"\nRed: {settings.EVM_NETWORK_NAME} (chain id {settings.EVM_CHAIN_ID})")
    print(f"RPC: {settings.EVM_RPC_URL}")

    clave = leer_clave()
    if not clave:
        cuenta = Account.create()
        guardar_clave(cuenta.key.hex())
        print("\n" + "=" * 68)
        print("  PASO 1 DE 2 — hay que fondear la wallet (unico paso manual)")
        print("=" * 68)
        print(f"\n  Wallet de despliegue creada: {cuenta.address}")
        print("  (guardada en .env como DEPLOYER_PRIVATE_KEY — solo testnet)")
        print(f"\n  1. Entra a {FAUCET}")
        print(f"  2. Pega esta direccion:  {cuenta.address}")
        print("  3. Pedi tBNB (es gratis, tarda ~1 minuto)")
        print("\n  Cuando tengas el tBNB, volve a correr:")
        print("      python scripts/deploy_test_token.py")
        return 0

    cuenta = Account.from_key(clave)
    print(f"Deployer: {cuenta.address}")

    saldo = _hex_to_int(await rpc("eth_getBalance", [cuenta.address, "latest"]))
    print(f"Saldo: {saldo / 10**18:.6f} tBNB")
    if saldo == 0:
        print("\n" + "=" * 68)
        print("  FALTA FONDEAR LA WALLET")
        print("=" * 68)
        print(f"\n  1. Entra a {FAUCET}")
        print(f"  2. Pega esta direccion:  {cuenta.address}")
        print("  3. Pedi tBNB y volve a correr este script.")
        return 1

    print("\nCompilando TestUSDC.sol...")
    bytecode, abi = compilar()
    print(f"  bytecode: {len(bytecode) // 2} bytes")

    nonce = _hex_to_int(await rpc("eth_getTransactionCount", [cuenta.address, "pending"]))
    gas_price = _hex_to_int(await rpc("eth_gasPrice", []))
    data = bytecode if bytecode.startswith("0x") else "0x" + bytecode
    gas = _hex_to_int(await rpc("eth_estimateGas", [{"from": cuenta.address, "data": data}]))
    costo = gas * gas_price
    print(f"  gas estimado: {gas} | precio: {gas_price / 10**9:.2f} gwei "
          f"| costo: ~{costo / 10**18:.6f} tBNB")
    if saldo < costo:
        print("\nERROR: saldo insuficiente para el despliegue. Pedi mas tBNB en el faucet.")
        return 1

    firmada = Account.sign_transaction({
        "chainId": settings.EVM_CHAIN_ID,
        "nonce": nonce,
        "gas": int(gas * 1.2),
        "gasPrice": gas_price,
        "data": data,
        "value": 0,
    }, clave)
    raw = getattr(firmada, "raw_transaction", None) or getattr(firmada, "rawTransaction")

    print("\nEnviando la transaccion de despliegue...")
    tx_hash = await rpc("eth_sendRawTransaction", ["0x" + raw.hex().lstrip("0x")])
    print(f"  tx: {tx_hash}")
    print("  esperando confirmacion...")

    recibo = None
    for _ in range(60):
        recibo = await rpc("eth_getTransactionReceipt", [tx_hash])
        if recibo and recibo.get("blockNumber"):
            break
        await asyncio.sleep(3)
    if not recibo:
        print("\nLa transaccion no se confirmo a tiempo. Revisala en el explorador:")
        print(f"  https://testnet.bscscan.com/tx/{tx_hash}")
        return 1
    if _hex_to_int(recibo.get("status")) != 1:
        print("\nEl despliegue REVIRTIO. Revisalo en el explorador:")
        print(f"  https://testnet.bscscan.com/tx/{tx_hash}")
        return 1

    contrato = str(recibo["contractAddress"]).lower()
    (ROOT / "contracts" / "TestUSDC.abi.json").write_text(
        json.dumps(abi, indent=2), encoding="utf-8")

    print("\n" + "=" * 68)
    print("  TOKEN DESPLEGADO")
    print("=" * 68)
    print(f"\n  Contrato : {contrato}")
    print(f"  Bloque   : {_hex_to_int(recibo['blockNumber'])}")
    print(f"  Explorador: https://testnet.bscscan.com/address/{contrato}")
    print(f"\n  El deployer ({cuenta.address}) recibio 1.000.000 USDC de prueba.")
    print("\n  Pega esto en tu .env:")
    print(f"\n    EVM_USDC_CONTRACT={contrato}")
    print("    EVM_TOKEN_DECIMALS=18")
    print("    EVM_TOKEN_SYMBOL=USDC")
    print("\n  Y definí a donde queres RECIBIR los pagos (puede ser otra wallet):")
    print("\n    EVM_RECEIVING_ADDRESS=0x...")
    print("\n  Verificalo con:")
    print(f"    python scripts/evm_token_info.py {contrato}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
