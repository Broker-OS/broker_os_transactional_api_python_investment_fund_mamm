"""Schemas de depositos on-chain (USDC sobre una cadena EVM)."""
from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CryptoDepositRequest(BaseModel):
    tx_hash: str = Field(
        min_length=64, max_length=66,
        description=("Hash de la transacción on-chain (`0x` + 64 hexadecimales). "
                     "Se ignoran los espacios: pegarlo cortado en varias líneas funciona."))
    chain_id: int = Field(
        gt=0, description="Id de la cadena EVM. Debe coincidir con la configurada.")
    value: Decimal = Field(
        gt=0,
        description=("Monto que declarás haber transferido. Se **verifica contra la cadena**: "
                     "si no coincide con lo que realmente llegó, se rechaza."))

    @field_validator("tx_hash", mode="before")
    @classmethod
    def _limpiar_hash(cls, v):
        """Quita cualquier espacio en blanco antes de validar el largo.

        Copiar un hash de una terminal o de un chat suele meter espacios o
        saltos de linea en el medio. Rechazarlo por "demasiado largo" es un
        error desconcertante para algo que a simple vista se ve correcto.
        """
        return "".join(v.split()) if isinstance(v, str) else v

    model_config = ConfigDict(json_schema_extra={
        "example": {
            "tx_hash": "0x5f2c1b8a9d3e4f6071829304a5b6c7d8e9f0a1b2c3d4e5f60718293a4b5c6d7e8",
            "chain_id": 97,
            "value": "100.00",
        }
    })


class CryptoDepositRead(BaseModel):
    id: str
    tx_hash: str
    chain_id: int
    status: str = Field(description="CONFIRMED | REJECTED")
    declared_amount: Decimal = Field(description="Lo que declaró quien lo presentó.")
    onchain_amount: Optional[Decimal] = Field(
        default=None, description="Lo que realmente llegó, leído de la cadena.")
    token_symbol: str
    token_contract: Optional[str] = None
    from_address: Optional[str] = Field(
        default=None, description="Address que envió los fondos.")
    to_address: Optional[str] = None
    block_number: Optional[int] = None
    confirmations: Optional[int] = None
    rejection_code: Optional[str] = None
    rejection_detail: Optional[str] = None
    ledger_tx_id: Optional[str] = Field(
        default=None,
        description=("Asiento contable que acreditó la cuenta maestra con este depósito "
                     "(`MASTER_ACCOUNT_FUNDING`). Nulo en los rechazados. Se consulta en "
                     "`GET /reports/ledger/transactions`."))
    notified_admins: int = Field(description="Cuántos admins recibieron el aviso.")
    notified_at: Optional[datetime] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class CryptoDepositListResponse(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[CryptoDepositRead]
