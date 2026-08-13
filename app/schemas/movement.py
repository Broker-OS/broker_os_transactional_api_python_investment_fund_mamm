"""Schemas de movimientos (deposit/withdraw/funding)."""
from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class AmountRequest(BaseModel):
    amount: Decimal = Field(gt=0, le=Decimal("10000000"))
    # Opcional: si el socio la envia, garantiza idempotencia en reintentos.
    idempotency_key: Optional[str] = Field(default=None, max_length=100)

    model_config = ConfigDict(json_schema_extra={"example": {"amount": "100.00"}})


class MovementRead(BaseModel):
    id: str
    trader_id: Optional[str] = None
    direction: str
    amount: Decimal
    currency: str
    status: str
    provider_result: Optional[str] = None
    provider_reference: Optional[str] = None
    ledger_tx_id: Optional[str] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class MovementListResponse(BaseModel):
    total: int
    page: int
    limit: int
    pages: int
    items: list[MovementRead]
