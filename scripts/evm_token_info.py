"""
Ayuda a averiguar que poner en EVM_USDC_CONTRACT y EVM_TOKEN_DECIMALS.

Dos modos:

  1) Inspeccionar un contrato: dice que token es y cuantos decimales tiene.

        python scripts/evm_token_info.py 0xCONTRATO

  2) Inspeccionar una transaccion: lista los tokens que se movieron en ella y a
     donde fueron. Es la forma mas practica de descubrir el contrato cuando ya
     te hicieron un pago de prueba.

        python scripts/evm_token_info.py --tx 0xHASH

Usa la red configurada en .env (EVM_RPC_URL / EVM_CHAIN_ID).
"""
from __future__ import annotations

import asyncio
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import settings  # noqa: E402
from app.core.exceptions import AppException  # noqa: E402
from app.services.evm_client import (  # noqa: E402
    EvmClient,
    normalize_address,
    normalize_tx_hash,
)


def _receptor() -> str | None:
    return normalize_address(settings.EVM_RECEIVING_ADDRESS)


def _plano(valor: Decimal) -> Decimal:
    """Evita la notacion cientifica de Decimal (100 -> 1E+2)."""
    valor = valor.normalize()
    if valor == valor.to_integral_value():
        try:
            return valor.quantize(Decimal(1))
        except Exception:
            return valor
    return valor


async def inspeccionar_contrato(client: EvmClient, contrato: str) -> int:
    addr = normalize_address(contrato)
    if not addr:
        print(f"ERROR: '{contrato}' no es una address valida (0x + 40 hexadecimales).")
        return 1

    info = await client.token_info(addr)
    if info["symbol"] is None and info["decimals"] is None:
        print(f"\nEn {addr} no hay un token ERC-20 (o el contrato no existe en esta red).")
        print(f"Red consultada: {settings.EVM_NETWORK_NAME} (chain id {settings.EVM_CHAIN_ID})")
        return 1

    print(f"\n  Contrato : {addr}")
    print(f"  Nombre   : {info['name']}")
    print(f"  Simbolo  : {info['symbol']}")
    print(f"  Decimales: {info['decimals']}")
    print(f"  Red      : {settings.EVM_NETWORK_NAME} (chain id {settings.EVM_CHAIN_ID})")

    simbolo = (info["symbol"] or "").upper()
    if "USDC" not in simbolo:
        print(f"\n  AVISO: el simbolo es '{info['symbol']}', no USDC. Confirma que sea el token")
        print("         que van a usar para cobrar antes de configurarlo.")

    print("\n  Para .env:")
    print(f"    EVM_USDC_CONTRACT={addr}")
    if info["decimals"] is not None:
        print(f"    EVM_TOKEN_DECIMALS={info['decimals']}")
        if info["decimals"] != settings.EVM_TOKEN_DECIMALS:
            print(f"\n  OJO: hoy tenes EVM_TOKEN_DECIMALS={settings.EVM_TOKEN_DECIMALS} y este "
                  f"token usa {info['decimals']}.")
            print("       Con el valor equivocado los montos se leen mal por varios ordenes")
            print("       de magnitud y todos los pagos serian rechazados.")
    if info["symbol"]:
        print(f"    EVM_TOKEN_SYMBOL={info['symbol']}")
    return 0


async def inspeccionar_tx(client: EvmClient, tx: str) -> int:
    h = normalize_tx_hash(tx)
    if not h:
        print(f"ERROR: '{tx}' no es un hash valido (0x + 64 hexadecimales).")
        return 1

    receipt = await client.get_receipt(h)
    if receipt is None:
        print(f"\nLa red {settings.EVM_NETWORK_NAME} no conoce esa transaccion todavia.")
        print("Puede seguir pendiente, o estar en otra red.")
        return 1

    print(f"\n  Transaccion: {receipt.tx_hash}")
    print(f"  Estado     : {'exitosa' if receipt.success else 'REVIRTIO (no movio fondos)'}")
    print(f"  Bloque     : {receipt.block_number}")
    print(f"  Red        : {settings.EVM_NETWORK_NAME} (chain id {settings.EVM_CHAIN_ID})")

    if not receipt.transfers:
        print("\n  No contiene transferencias de tokens ERC-20.")
        print("  Si esperabas un pago en USDC, puede haber sido en la moneda nativa (BNB),")
        print("  que este servicio no valida.")
        return 1

    receptor = _receptor()
    print(f"\n  Transferencias de token encontradas: {len(receipt.transfers)}")
    contratos: dict[str, dict] = {}
    for t in receipt.transfers:
        if t.token_contract not in contratos:
            contratos[t.token_contract] = await client.token_info(t.token_contract)
        info = contratos[t.token_contract]
        dec = info["decimals"] if info["decimals"] is not None else 18
        monto = _plano(Decimal(t.raw_amount) / (Decimal(10) ** dec))
        marca = ""
        if receptor and t.to_address == receptor:
            marca = "   <-- va a TU address receptora"
        print(f"\n    token    : {info['symbol'] or '?'} ({t.token_contract})")
        print(f"    decimales: {info['decimals']}")
        print(f"    de       : {t.from_address}")
        print(f"    para     : {t.to_address}{marca}")
        print(f"    monto    : {monto.normalize()}  (crudo: {t.raw_amount})")

    if receptor:
        hacia_nosotros = [t for t in receipt.transfers if t.to_address == receptor]
        if not hacia_nosotros:
            print(f"\n  AVISO: ninguna transferencia fue hacia EVM_RECEIVING_ADDRESS ({receptor}).")
            print("         Este pago seria rechazado por el servicio.")
        else:
            elegido = contratos[hacia_nosotros[0].token_contract]
            print("\n  Para .env (segun el token que llego a tu address):")
            print(f"    EVM_USDC_CONTRACT={hacia_nosotros[0].token_contract}")
            if elegido["decimals"] is not None:
                print(f"    EVM_TOKEN_DECIMALS={elegido['decimals']}")
    else:
        print("\n  AVISO: EVM_RECEIVING_ADDRESS esta vacio en .env, asi que no se puede")
        print("         verificar si el pago fue hacia tu address.")
    return 0


async def main() -> int:
    args = [a for a in sys.argv[1:] if a]
    if not args:
        print(__doc__)
        return 1
    if not settings.EVM_RPC_URL:
        print("ERROR: falta EVM_RPC_URL en .env")
        return 1

    client = EvmClient()
    try:
        real = await client.chain_id()
        if real is not None and real != settings.EVM_CHAIN_ID:
            print(f"AVISO: el nodo responde chain id {real} pero EVM_CHAIN_ID={settings.EVM_CHAIN_ID}.")
        if args[0] in ("--tx", "-t"):
            if len(args) < 2:
                print("ERROR: falta el hash. Uso: --tx 0xHASH")
                return 1
            return await inspeccionar_tx(client, args[1])
        return await inspeccionar_contrato(client, args[0])
    except AppException as exc:
        print(f"\nERROR consultando la red: {exc.code} — {exc.message}")
        if exc.detail:
            print(f"       {exc.detail}")
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
