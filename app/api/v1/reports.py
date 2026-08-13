"""Endpoints de reportes: cuentas con saldo, libro diario, saldos por fecha, resumen diario."""
from datetime import date
from math import ceil
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.database import get_db
from app.schemas.common import APIResponse
from app.schemas.report import (
    AccountBalanceRow,
    AccountListResponse,
    AccountRead,
    BalancesAsOfResponse,
    DailySummaryResponse,
    DailySummaryRow,
    LedgerTransactionListResponse,
    LedgerTransactionRead,
)
from app.services.report_service import ReportService

router = APIRouter(tags=["9. Consultas y reportes"])


@router.get("/ledger/accounts", response_model=APIResponse,
            summary="Cuentas del ledger con su saldo",
            description=(
                "Lista TODAS las cuentas contables que existen (cuenta maestra, funding "
                "y holdings por trader) con `balance`, `pending_debit` y `available`. "
                "El saldo se recomputa desde los asientos (`ledger_entries`)."
            ))
async def list_accounts(db: AsyncSession = Depends(get_db)):
    accounts = await ReportService(db).list_accounts()
    payload = AccountListResponse(
        total=len(accounts), currency=settings.LEDGER_CURRENCY,
        items=[AccountRead(**a) for a in accounts])
    return APIResponse(success=True, http_status=200, message="Cuentas obtenidas correctamente",
                       data=payload.model_dump(mode="json"))


@router.get("/ledger/transactions", response_model=APIResponse,
            summary="Libro diario: TODAS las transacciones contables con sus asientos",
            description=(
                "El libro contable completo, transacción por transacción, con sus **asientos "
                "de doble entrada** (débito/crédito por cuenta). Reporta **todo lo que tocó el "
                "ledger**: fondeos de la cuenta maestra, depósitos, retiros (por el monto efectivo) "
                "y performance fees conciliados — incluidos los estados `PENDING` y `FAILED`.\n\n"
                "Cada línea del asiento trae **`balance_before` y `balance_after`** de su "
                "cuenta: se ve cuánto tenía y cuánto quedó la **cuenta maestra** en cada "
                "depósito o retiro, sin tener que ir sumando. Siempre se cumple "
                "`balance_after = balance_before + debit − credit`.\n\n"
                "Recordá el sentido: **depositar a un Investor baja** la cuenta maestra "
                "(`credit`) y **retirar la sube** (`debit`) — el dinero sale del fondo común "
                "y vuelve a él.\n\n"
                "Más nueva primero. Filtros: `trader` (external_reference), `kind` "
                "(`TRADER_DEPOSIT`/`TRADER_WITHDRAWAL`/`MASTER_ACCOUNT_FUNDING`/`PERF_FEE`), "
                "`status` (`PENDING`/`POSTED`/`FAILED`), `date_from`, `date_to`, `page`, `limit`.\n\n"
                "Nota: el P&L del trading **no** es un asiento (no es un movimiento de caja); se "
                "ve en `/pamm/reports/reconciliation`. Los retiros `REJECTED` y los fees sin "
                "atribuir se ven en `/movements` y `/pamm/perf-fee/payments` respectivamente."
            ))
async def list_ledger_transactions(
    trader: Optional[str] = Query(None, description="external_reference del trader"),
    kind: Optional[str] = Query(None, pattern="^(TRADER_DEPOSIT|TRADER_WITHDRAWAL|MASTER_ACCOUNT_FUNDING|PERF_FEE)$"),
    status_filter: Optional[str] = Query(None, alias="status", pattern="^(PENDING|POSTED|FAILED)$"),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
):
    items, total = await ReportService(db).list_ledger_transactions(
        trader_external_reference=trader, kind=kind, status=status_filter,
        date_from=date_from, date_to=date_to, page=page, limit=limit)
    payload = LedgerTransactionListResponse(
        total=total, page=page, limit=limit,
        pages=ceil(total / limit) if (total and limit) else 0,
        items=[LedgerTransactionRead(**it) for it in items])
    return APIResponse(success=True, http_status=200,
                       message="Libro diario obtenido correctamente",
                       data=payload.model_dump(mode="json"))


@router.get("/reports/balances", response_model=APIResponse,
            summary="Saldos de las cuentas a una fecha (point-in-time)",
            description=(
                "Saldo de cada cuenta considerando **solo los asientos hasta la fecha "
                "`as_of`** (inclusive, YYYY-MM-DD). Si se omite, usa la fecha de hoy. "
                "Útil para ver el estado contable histórico a un día dado."
            ))
async def balances_as_of(
    as_of: date = Query(default=None, description="YYYY-MM-DD (default: hoy)"),
    db: AsyncSession = Depends(get_db),
):
    aod = as_of or date.today()
    rows = await ReportService(db).balances_as_of(aod)
    payload = BalancesAsOfResponse(
        as_of=aod, currency=settings.LEDGER_CURRENCY,
        items=[AccountBalanceRow(**r) for r in rows])
    return APIResponse(success=True, http_status=200, message="Saldos a la fecha obtenidos correctamente",
                       data=payload.model_dump(mode="json"))


@router.get("/reports/daily", response_model=APIResponse,
            summary="Resumen diario de movimientos por rango de fechas",
            description=(
                "Sumatorias por día (movimientos `COMPLETED`): `deposits` (maestra→trader), "
                "`withdrawals` (trader→maestra), `funding` (externo→maestra) y `movements_count`. "
                "Fechas YYYY-MM-DD (inclusivas)."
            ))
async def daily_summary(
    date_from: date = Query(..., description="YYYY-MM-DD"),
    date_to: date = Query(..., description="YYYY-MM-DD"),
    db: AsyncSession = Depends(get_db),
):
    rows = await ReportService(db).daily_summary(date_from=date_from, date_to=date_to)
    payload = DailySummaryResponse(
        date_from=date_from, date_to=date_to, currency=settings.LEDGER_CURRENCY,
        rows=[DailySummaryRow(**r) for r in rows])
    return APIResponse(success=True, http_status=200, message="Resumen diario obtenido correctamente",
                       data=payload.model_dump(mode="json"))
