"""Servicios de reportes: saldos, cuentas, balances por fecha, resumen diario."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import TraderNotFoundError
from app.models.movement import Movement
from app.repositories.ledger_repository import LedgerRepository
from app.repositories.movement_repository import MovementRepository
from app.repositories.trader_repository import TraderRepository

_ZERO = Decimal("0")


def _day_start(d: date) -> datetime:
    return datetime.combine(d, time.min, tzinfo=timezone.utc)


class ReportService:
    def __init__(self, db: AsyncSession):
        self.ledger_repo = LedgerRepository(db)
        self.movement_repo = MovementRepository(db)
        self.trader_repo = TraderRepository(db)

    # ── cuentas con saldo actual ──
    async def list_accounts(self) -> list[dict]:
        rows = await self.ledger_repo.list_accounts_with_balances()
        out = []
        for r in rows:
            balance = Decimal(str(r.balance))
            pending = Decimal(str(r.pending_debit or 0))
            out.append({
                "id": r.id, "code": r.code, "kind": r.kind, "trader_id": r.trader_id,
                "trader_external_reference": r.trader_external_reference,
                "currency": r.currency, "balance": balance, "pending_debit": pending,
                "available": balance - pending, "created_at": r.created_at,
            })
        return out

    async def master_account(self) -> dict:
        for a in await self.list_accounts():
            if a["code"] == "MASTER_ACCOUNT":
                return {
                    "currency": a["currency"], "balance": a["balance"],
                    "pending_debit": a["pending_debit"], "available": a["available"],
                }
        return {"currency": settings.LEDGER_CURRENCY, "balance": _ZERO,
                "pending_debit": _ZERO, "available": _ZERO}

    # ── saldos a una fecha (point-in-time) ──
    async def balances_as_of(self, as_of: date) -> list[dict]:
        # incluir todos los entries con created_at < inicio del dia siguiente.
        upper = _day_start(as_of + timedelta(days=1))
        rows = await self.ledger_repo.balances_as_of(upper)
        return [{
            "code": r.code, "kind": r.kind,
            "trader_external_reference": r.trader_external_reference,
            "balance": Decimal(str(r.balance)),
        } for r in rows]

    # ── movimientos (con rango de fechas) ──
    async def list_movements(
        self, *, trader_external_reference: Optional[str], direction: Optional[str],
        status: Optional[str], date_from: Optional[date], date_to: Optional[date],
        page: int, limit: int, owner_api_user_id: Optional[str] = None,
    ) -> tuple[list[Movement], int]:
        trader_id = None
        if trader_external_reference:
            tr = await self.trader_repo.get_by_external_reference(trader_external_reference)
            if tr is None:
                raise TraderNotFoundError(message="El trader no existe", detail=trader_external_reference)
            # Si el caller está scopeado por dueño, el trader pedido tiene que ser suyo.
            if owner_api_user_id is not None and tr.owner_api_user_id != owner_api_user_id:
                raise TraderNotFoundError(message="El trader no existe", detail=trader_external_reference)
            trader_id = tr.id
        df = _day_start(date_from) if date_from else None
        dt = _day_start(date_to + timedelta(days=1)) if date_to else None
        return await self.movement_repo.list(
            trader_id=trader_id, owner_api_user_id=owner_api_user_id, direction=direction,
            status=status, date_from=df, date_to=dt, page=page, limit=limit,
        )

    # ── libro diario (todas las transacciones contables con sus asientos) ──
    async def list_ledger_transactions(
        self, *, trader_external_reference: Optional[str], kind: Optional[str],
        status: Optional[str], date_from: Optional[date], date_to: Optional[date],
        page: int, limit: int,
    ) -> tuple[list[dict], int]:
        trader_id = None
        if trader_external_reference:
            tr = await self.trader_repo.get_by_external_reference(trader_external_reference)
            if tr is None:
                raise TraderNotFoundError(message="El trader no existe", detail=trader_external_reference)
            trader_id = tr.id
        df = _day_start(date_from) if date_from else None
        dt = _day_start(date_to + timedelta(days=1)) if date_to else None
        tx_rows, entries_by_tx, total = await self.ledger_repo.list_transactions(
            trader_id=trader_id, kind=kind, status=status,
            date_from=df, date_to=dt, page=page, limit=limit)
        items = []
        for tx, extref in tx_rows:
            items.append({
                "id": tx.id, "kind": tx.kind, "status": tx.status,
                "amount": Decimal(str(tx.amount)), "currency": tx.currency,
                "trader_external_reference": extref,
                "idempotency_key": tx.idempotency_key, "description": tx.description,
                "created_at": tx.created_at, "posted_at": tx.posted_at,
                "entries": entries_by_tx.get(tx.id, []),
            })
        return items, total

    # ── resumen diario ──
    async def daily_summary(self, *, date_from: date, date_to: date) -> list[dict]:
        df = _day_start(date_from)
        dt = _day_start(date_to + timedelta(days=1))
        rows = await self.ledger_repo.daily_summary(df, dt)
        return [{
            "day": r.day,
            "deposits": Decimal(str(r.deposits)),
            "withdrawals": Decimal(str(r.withdrawals)),
            "funding": Decimal(str(r.funding)),
            "movements_count": r.movements_count,
        } for r in rows]
