"""Schemas de reportes: cuenta maestra, cuentas, saldos por fecha, resumen diario."""
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field


class MasterAccountRead(BaseModel):
    currency: str
    balance: Decimal
    pending_debit: Decimal
    available: Decimal


class AccountRead(BaseModel):
    """Una cuenta del plan contable con su saldo actual."""
    id: str
    code: str
    kind: str
    trader_id: Optional[str] = None
    trader_external_reference: Optional[str] = None
    currency: str
    balance: Decimal
    pending_debit: Decimal
    available: Decimal
    created_at: datetime


class AccountListResponse(BaseModel):
    total: int
    currency: str
    items: list[AccountRead]


class AccountBalanceRow(BaseModel):
    code: str
    kind: str
    trader_external_reference: Optional[str] = None
    balance: Decimal


class BalancesAsOfResponse(BaseModel):
    """Saldos de todas las cuentas a una fecha (point-in-time)."""
    as_of: date
    currency: str
    items: list[AccountBalanceRow]


class DailySummaryRow(BaseModel):
    day: date
    deposits: Decimal        # cuenta maestra → traders
    withdrawals: Decimal     # traders → cuenta maestra
    funding: Decimal         # externo → cuenta maestra
    movements_count: int


class DailySummaryResponse(BaseModel):
    date_from: date
    date_to: date
    currency: str
    rows: list[DailySummaryRow]


# ── Libro diario: cada transacción contable con sus asientos (doble entrada) ──

class LedgerEntryLine(BaseModel):
    """Una línea del asiento: débito o crédito sobre una cuenta."""
    account_code: str
    account_kind: str
    trader_external_reference: Optional[str] = None
    debit: Decimal
    credit: Decimal
    currency: str
    balance_before: Decimal = Field(
        description="Saldo de la cuenta **antes** de aplicar esta línea.")
    balance_after: Decimal = Field(
        description=("Saldo **después**. Siempre `balance_before + debit − credit`: "
                     "permite ver de un vistazo cuánto tenía y cuánto quedó la cuenta "
                     "maestra en cada depósito o retiro."))


class LedgerTransactionRead(BaseModel):
    id: str
    kind: str                      # TRADER_DEPOSIT | TRADER_WITHDRAWAL | MASTER_ACCOUNT_FUNDING | PERF_FEE
    status: str                    # PENDING | POSTED | FAILED
    amount: Decimal
    currency: str
    trader_external_reference: Optional[str] = None
    idempotency_key: str
    description: Optional[str] = None
    created_at: datetime
    posted_at: Optional[datetime] = None
    entries: list[LedgerEntryLine]  # las patas del asiento (débitos = créditos)


class LedgerTransactionListResponse(BaseModel):
    total: int
    page: int
    limit: int
    pages: int
    items: list[LedgerTransactionRead]
