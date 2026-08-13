"""
Cliente JSON-RPC de una cadena EVM (BNB Smart Chain testnet por defecto).

Habla el protocolo directo sobre httpx en vez de sumar `web3`: solo necesitamos
tres llamadas (`eth_chainId`, `eth_getTransactionReceipt`, `eth_blockNumber`) y
decodificar el evento Transfer de un token ERC-20, que es un formato fijo.

El evento Transfer de ERC-20/BEP-20 es:

    Transfer(address indexed from, address indexed to, uint256 value)

    topics[0] = keccak("Transfer(address,address,uint256)")  (constante conocida)
    topics[1] = from, en 32 bytes con padding de ceros a la izquierda
    topics[2] = to,   idem
    data      = value, uint256 en hexadecimal

Como topics[0] es una constante universal, no hace falta calcular keccak.

Este modulo NO decide nada de negocio: solo lee la cadena y devuelve datos.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from app.core.config import settings
from app.core.exceptions import EvmConfigError, EvmRpcError

logger = logging.getLogger(__name__)

# keccak256("Transfer(address,address,uint256)")
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

_TX_HASH_RE = re.compile(r"^0x[0-9a-f]{64}$")
_ADDRESS_RE = re.compile(r"^0x[0-9a-f]{40}$")


def normalize_tx_hash(value: str) -> Optional[str]:
    """Normaliza un hash de transaccion. None si no tiene forma valida."""
    v = (value or "").strip().lower()
    if not v.startswith("0x"):
        v = "0x" + v
    return v if _TX_HASH_RE.match(v) else None


def normalize_address(value: str) -> Optional[str]:
    """Normaliza una address EVM a minusculas. None si no es valida.

    El checksum EIP-55 es solo presentacion: on-chain las addresses son
    case-insensitive, asi que comparamos siempre en minusculas.
    """
    v = (value or "").strip().lower()
    if not v.startswith("0x"):
        v = "0x" + v
    return v if _ADDRESS_RE.match(v) else None


def _address_from_topic(topic: str) -> Optional[str]:
    """Extrae la address de un topic indexado (32 bytes, padding de ceros)."""
    t = (topic or "").strip().lower()
    if len(t) < 42:
        return None
    return "0x" + t[-40:]


def _decode_string(raw: Optional[str]) -> Optional[str]:
    """Decodifica el retorno de `symbol()` / `name()` de un ERC-20.

    El estandar dice `string` (dinamico: offset + longitud + datos), pero varios
    tokens viejos devuelven `bytes32` fijo. Se soportan los dos.
    """
    if not raw or raw == "0x":
        return None
    body = raw[2:]
    try:
        if len(body) == 64:  # bytes32: texto con relleno de ceros
            return bytes.fromhex(body).rstrip(b"\x00").decode("utf-8", "replace") or None
        # string dinamico: [offset][longitud][datos...]
        length = int(body[64:128], 16)
        data = body[128:128 + length * 2]
        return bytes.fromhex(data).decode("utf-8", "replace") or None
    except (ValueError, IndexError):
        return None


@dataclass(frozen=True)
class TokenTransfer:
    """Una transferencia de token encontrada en los logs de una transaccion."""

    token_contract: str
    from_address: str
    to_address: str
    raw_amount: int
    log_index: int


@dataclass(frozen=True)
class TxReceipt:
    tx_hash: str
    success: bool
    block_number: int
    from_address: Optional[str]
    to_address: Optional[str]
    transfers: list[TokenTransfer]


class EvmClient:
    def __init__(self) -> None:
        self._url = (settings.EVM_RPC_URL or "").strip()
        self._timeout = settings.EVM_HTTP_TIMEOUT_SECONDS

    def _ensure_configured(self) -> None:
        if not self._url:
            raise EvmConfigError(
                message="La verificacion on-chain no esta configurada",
                detail="Definir EVM_RPC_URL en .env")

    async def _rpc(self, method: str, params: list[Any]) -> Any:
        self._ensure_configured()
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(self._url, json=payload)
        except httpx.TimeoutException as exc:
            logger.warning("RPC EVM timeout: %s", method)
            raise EvmRpcError(message="El nodo de la cadena no respondio a tiempo",
                              detail=f"{method}: {type(exc).__name__}") from exc
        except httpx.HTTPError as exc:
            logger.warning("RPC EVM error de red: %s", method)
            raise EvmRpcError(message="Error de comunicacion con el nodo de la cadena",
                              detail=f"{method}: {type(exc).__name__}") from exc

        if resp.status_code >= 400:
            raise EvmRpcError(message="El nodo de la cadena respondio con error",
                              detail=f"{method}: HTTP {resp.status_code}")
        try:
            body = resp.json()
        except ValueError as exc:
            raise EvmRpcError(message="El nodo de la cadena devolvio una respuesta invalida",
                              detail=f"{method}: no es JSON") from exc

        if isinstance(body, dict) and body.get("error"):
            err = body["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise EvmRpcError(message="El nodo de la cadena rechazo la consulta",
                              detail=f"{method}: {str(msg)[:200]}")
        return body.get("result") if isinstance(body, dict) else None

    @staticmethod
    def _to_int(value: Any) -> Optional[int]:
        if value is None:
            return None
        try:
            return int(value, 16) if isinstance(value, str) else int(value)
        except (TypeError, ValueError):
            return None

    # ── consultas ──
    async def chain_id(self) -> Optional[int]:
        return self._to_int(await self._rpc("eth_chainId", []))

    async def block_number(self) -> Optional[int]:
        return self._to_int(await self._rpc("eth_blockNumber", []))

    async def call(self, *, to: str, data: str) -> Optional[str]:
        """`eth_call` de solo lectura. `data` es el selector + argumentos ya codificados."""
        return await self._rpc("eth_call", [{"to": to, "data": data}, "latest"])

    async def token_info(self, contract: str) -> dict[str, Any]:
        """Lee symbol/name/decimals de un contrato ERC-20.

        Sirve para CONFIRMAR que una address es el token que uno cree, y para
        leer los decimales reales en vez de asumirlos.
        """
        out: dict[str, Any] = {"contract": contract, "symbol": None,
                               "name": None, "decimals": None}
        # Selectores de los getters estandar de ERC-20.
        for campo, selector in (("symbol", "0x95d89b41"), ("name", "0x06fdde03")):
            try:
                out[campo] = _decode_string(await self.call(to=contract, data=selector))
            except EvmRpcError:
                out[campo] = None
        try:
            raw = await self.call(to=contract, data="0x313ce567")
            out["decimals"] = self._to_int(raw) if raw and raw != "0x" else None
        except EvmRpcError:
            out["decimals"] = None
        return out

    async def get_receipt(self, tx_hash: str) -> Optional[TxReceipt]:
        """Recibo de la transaccion. None si el nodo aun no la conoce o esta pendiente."""
        raw = await self._rpc("eth_getTransactionReceipt", [tx_hash])
        if not isinstance(raw, dict):
            return None
        block = self._to_int(raw.get("blockNumber"))
        if block is None:
            # Sin bloque asignado: todavia esta en el mempool.
            return None

        transfers: list[TokenTransfer] = []
        for log in raw.get("logs") or []:
            if not isinstance(log, dict):
                continue
            topics = [str(t).lower() for t in (log.get("topics") or [])]
            # Un Transfer ERC-20 tiene exactamente 3 topics; los NFT (ERC-721)
            # usan 4 porque el tokenId tambien va indexado, y no nos sirven.
            if len(topics) != 3 or topics[0] != TRANSFER_TOPIC:
                continue
            frm = _address_from_topic(topics[1])
            to = _address_from_topic(topics[2])
            amount = self._to_int(log.get("data"))
            contract = normalize_address(str(log.get("address") or ""))
            if not (frm and to and contract) or amount is None:
                continue
            transfers.append(TokenTransfer(
                token_contract=contract, from_address=frm, to_address=to,
                raw_amount=amount, log_index=self._to_int(log.get("logIndex")) or 0))

        return TxReceipt(
            tx_hash=str(raw.get("transactionHash") or tx_hash).lower(),
            success=self._to_int(raw.get("status")) == 1,
            block_number=block,
            from_address=normalize_address(str(raw.get("from") or "")),
            to_address=normalize_address(str(raw.get("to") or "")),
            transfers=transfers,
        )


_client: Optional[EvmClient] = None


def get_evm_client() -> EvmClient:
    global _client
    if _client is None:
        _client = EvmClient()
    return _client


def reset_client() -> None:
    """Solo para tests: fuerza releer la configuracion."""
    global _client
    _client = None
