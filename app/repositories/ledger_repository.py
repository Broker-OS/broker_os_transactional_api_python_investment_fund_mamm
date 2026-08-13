"""Acceso a datos del libro contable: cuentas, saldos (incl. por fecha) y agregados."""
from datetime import datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.ledger import LedgerAccount, LedgerEntry, LedgerTransaction
from app.models.movement import Movement
from app.models.trader import Trader

CODE_MASTER_ACCOUNT = "MASTER_ACCOUNT"
CODE_EXTERNAL_FUNDING = "EXTERNAL_FUNDING"
CODE_TRADER_HOLDINGS = "TRADER_HOLDINGS"
# Contrapartida de los performance fees que salen del trader hacia la payment
# account del Master (guia §9). El `kind` no coincide con el `code`.
CODE_PERF_FEE_PAID = "PERF_FEE_PAID"
KIND_PF_PAYABLE = "PF_PAYABLE"

# code → kind de las cuentas globales (sin trader_id).
_GLOBAL_ACCOUNTS = {
    CODE_MASTER_ACCOUNT: CODE_MASTER_ACCOUNT,
    CODE_EXTERNAL_FUNDING: CODE_EXTERNAL_FUNDING,
    CODE_PERF_FEE_PAID: KIND_PF_PAYABLE,
}


class LedgerRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    # ── provisioning de cuentas ──
    async def ensure_global_accounts(self) -> None:
        for code, kind in _GLOBAL_ACCOUNTS.items():
            existing = (
                await self.db.execute(
                    select(LedgerAccount.id).where(
                        LedgerAccount.code == code, LedgerAccount.trader_id.is_(None)
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                self.db.add(LedgerAccount(code=code, kind=kind, currency=settings.LEDGER_CURRENCY))
        await self.db.flush()

    async def ensure_trader_holdings(self, trader_id: str) -> LedgerAccount:
        acc = (
            await self.db.execute(
                select(LedgerAccount).where(
                    LedgerAccount.code == CODE_TRADER_HOLDINGS,
                    LedgerAccount.trader_id == trader_id,
                )
            )
        ).scalar_one_or_none()
        if acc is not None:
            return acc
        acc = LedgerAccount(
            code=CODE_TRADER_HOLDINGS, kind=CODE_TRADER_HOLDINGS,
            trader_id=trader_id, currency=settings.LEDGER_CURRENCY,
        )
        self.db.add(acc)
        await self.db.flush()
        return acc

    async def account_for_update(
        self, *, code: str, trader_id: Optional[str] = None
    ) -> Optional[LedgerAccount]:
        stmt = select(LedgerAccount).where(
            LedgerAccount.code == code, LedgerAccount.is_active.is_(True)
        )
        stmt = (
            stmt.where(LedgerAccount.trader_id.is_(None))
            if trader_id is None
            else stmt.where(LedgerAccount.trader_id == trader_id)
        )
        return (await self.db.execute(stmt.with_for_update())).scalar_one_or_none()

    async def recompute_balance(self, account_id: str) -> Decimal:
        row = (
            await self.db.execute(
                select(
                    func.coalesce(func.sum(LedgerEntry.debit), 0),
                    func.coalesce(func.sum(LedgerEntry.credit), 0),
                ).where(LedgerEntry.account_id == account_id)
            )
        ).one()
        return Decimal(str(row[0])) - Decimal(str(row[1]))

    # ── reportes ──
    async def list_accounts_with_balances(self) -> list:
        """Todas las cuentas con su saldo (recomputado desde entries)."""
        stmt = (
            select(
                LedgerAccount.id, LedgerAccount.code, LedgerAccount.kind,
                LedgerAccount.trader_id, LedgerAccount.currency,
                LedgerAccount.pending_debit, LedgerAccount.created_at,
                Trader.external_reference.label("trader_external_reference"),
                func.coalesce(func.sum(LedgerEntry.debit - LedgerEntry.credit), 0).label("balance"),
            )
            .select_from(LedgerAccount)
            .outerjoin(Trader, Trader.id == LedgerAccount.trader_id)
            .outerjoin(LedgerEntry, LedgerEntry.account_id == LedgerAccount.id)
            .group_by(LedgerAccount.id, Trader.external_reference)
            .order_by(LedgerAccount.code, Trader.external_reference)
        )
        return list((await self.db.execute(stmt)).all())

    async def balances_as_of(self, upper_bound: datetime) -> list:
        """Saldo por cuenta considerando solo entries con created_at < upper_bound."""
        stmt = (
            select(
                LedgerAccount.code, LedgerAccount.kind,
                Trader.external_reference.label("trader_external_reference"),
                func.coalesce(func.sum(LedgerEntry.debit - LedgerEntry.credit), 0).label("balance"),
            )
            .select_from(LedgerAccount)
            .outerjoin(Trader, Trader.id == LedgerAccount.trader_id)
            .outerjoin(
                LedgerEntry,
                (LedgerEntry.account_id == LedgerAccount.id)
                & (LedgerEntry.created_at < upper_bound),
            )
            .group_by(LedgerAccount.id, Trader.external_reference)
            .order_by(LedgerAccount.code, Trader.external_reference)
        )
        return list((await self.db.execute(stmt)).all())

    async def list_transactions(
        self, *, trader_id: Optional[str] = None, kind: Optional[str] = None,
        status: Optional[str] = None, date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None, page: int = 1, limit: int = 50,
    ) -> tuple[list, dict, int]:
        """Libro diario: transacciones (más nueva primero) con sus asientos."""
        filters = []
        if trader_id:
            filters.append(LedgerTransaction.trader_id == trader_id)
        if kind:
            filters.append(LedgerTransaction.kind == kind)
        if status:
            filters.append(LedgerTransaction.status == status)
        if date_from:
            filters.append(LedgerTransaction.created_at >= date_from)
        if date_to:
            filters.append(LedgerTransaction.created_at < date_to)

        total = (
            await self.db.execute(
                select(func.count()).select_from(LedgerTransaction).where(*filters))
        ).scalar_one()
        tx_rows = (
            await self.db.execute(
                select(LedgerTransaction, Trader.external_reference)
                .outerjoin(Trader, Trader.id == LedgerTransaction.trader_id)
                .where(*filters)
                .order_by(LedgerTransaction.created_at.desc())
                .offset((page - 1) * limit).limit(limit)
            )
        ).all()

        entries_by_tx: dict[str, list] = {}
        tx_ids = [t[0].id for t in tx_rows]
        if tx_ids:
            # Saldo acumulado de cada cuenta hasta ese asiento inclusive.
            #
            # Se calcula en la base con una funcion de ventana en vez de
            # guardarlo en una columna: asi tambien sale bien para los asientos
            # anteriores a esta funcionalidad, y no hay dos fuentes de verdad
            # que puedan discrepar. El orden (created_at, id) es determinista
            # porque el id es unico.
            corrido = (
                select(
                    LedgerEntry.id.label("entry_id"),
                    func.sum(LedgerEntry.debit - LedgerEntry.credit)
                    .over(
                        partition_by=LedgerEntry.account_id,
                        order_by=(LedgerEntry.created_at, LedgerEntry.id),
                        rows=(None, 0),          # desde el principio hasta esta fila
                    )
                    .label("saldo_despues"),
                )
                .subquery()
            )
            erows = (
                await self.db.execute(
                    select(LedgerEntry, LedgerAccount.code, LedgerAccount.kind,
                           Trader.external_reference, corrido.c.saldo_despues)
                    .join(LedgerAccount, LedgerAccount.id == LedgerEntry.account_id)
                    .outerjoin(Trader, Trader.id == LedgerAccount.trader_id)
                    .join(corrido, corrido.c.entry_id == LedgerEntry.id)
                    .where(LedgerEntry.tx_id.in_(tx_ids))
                    .order_by(LedgerEntry.created_at)
                )
            ).all()
            for entry, code, acc_kind, extref, saldo_despues in erows:
                debito = Decimal(str(entry.debit))
                credito = Decimal(str(entry.credit))
                despues = Decimal(str(saldo_despues or 0))
                entries_by_tx.setdefault(entry.tx_id, []).append({
                    "account_code": code, "account_kind": acc_kind,
                    "trader_external_reference": extref,
                    "debit": debito, "credit": credito,
                    "currency": entry.currency,
                    "balance_before": despues - (debito - credito),
                    "balance_after": despues,
                })
        return tx_rows, entries_by_tx, total

    async def daily_summary(self, date_from: datetime, date_to: datetime) -> list:
        """Sumatorias diarias por direccion (movimientos COMPLETED)."""
        day = func.date(Movement.created_at)
        stmt = (
            select(
                day.label("day"),
                func.coalesce(func.sum(case((Movement.direction == "DEPOSIT", Movement.amount), else_=0)), 0).label("deposits"),
                func.coalesce(func.sum(case((Movement.direction == "WITHDRAWAL", Movement.amount), else_=0)), 0).label("withdrawals"),
                func.coalesce(func.sum(case((Movement.direction == "FUNDING", Movement.amount), else_=0)), 0).label("funding"),
                func.count().label("movements_count"),
            )
            .where(
                Movement.status == "COMPLETED",
                Movement.created_at >= date_from,
                Movement.created_at < date_to,
            )
            .group_by(day)
            .order_by(day)
        )
        return list((await self.db.execute(stmt)).all())
