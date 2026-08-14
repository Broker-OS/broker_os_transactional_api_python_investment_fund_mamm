"""
Libro contable de doble entrada (cuenta maestra pre-fondeada ↔ holdings por trader).

Convencion: balance de una cuenta = SUM(debit) - SUM(credit).
- MASTER_ACCOUNT: funding lo DEBITA (sube); depositar a trader lo ACREDITA (baja);
  retirar de trader lo DEBITA (sube). balance = cash disponible.
- TRADER_HOLDINGS(trader): depositar DEBITA (sube); retirar ACREDITA (baja).
- EXTERNAL_FUNDING: contrapartida del funding.

disponible cuenta maestra = balance - pending_debit. El HOLD (pending_debit) previene
sobre-asignar mas de lo pre-fondeado. Los asientos se aplican bajo FOR UPDATE.
El caller controla commit/rollback.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.ledger import LedgerAccount, LedgerEntry, LedgerTransaction
from app.repositories.ledger_repository import (
    CODE_MASTER_ACCOUNT,
    CODE_EXTERNAL_FUNDING,
    CODE_PERF_FEE_PAID,
    CODE_TRADER_HOLDINGS,
    LedgerRepository,
)

logger = logging.getLogger(__name__)
_ZERO = Decimal("0")


class LedgerService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = LedgerRepository(db)

    # ── helpers ──
    def _apply_entry(self, *, tx: LedgerTransaction, account: LedgerAccount,
                     debit: Decimal = _ZERO, credit: Decimal = _ZERO) -> None:
        self.db.add(LedgerEntry(
            tx_id=tx.id, account_id=account.id,
            debit=debit, credit=credit, currency=settings.LEDGER_CURRENCY,
        ))
        account.balance = Decimal(str(account.balance or 0)) + debit - credit

    def _new_tx(self, *, kind: str, idempotency_key: str, amount: Decimal,
                trader_id: Optional[str], description: str, status: str = "PENDING") -> LedgerTransaction:
        tx = LedgerTransaction(
            kind=kind, status=status, idempotency_key=idempotency_key,
            trader_id=trader_id, amount=amount, currency=settings.LEDGER_CURRENCY,
            description=description,
        )
        self.db.add(tx)
        return tx

    async def _tx_by_id(self, ledger_tx_id: str) -> Optional[LedgerTransaction]:
        return (
            await self.db.execute(select(LedgerTransaction).where(LedgerTransaction.id == ledger_tx_id))
        ).scalar_one_or_none()

    async def master_account_available(self) -> tuple[Decimal, Decimal, Decimal]:
        acc = await self.repo.account_for_update(code=CODE_MASTER_ACCOUNT)
        if acc is None:
            return _ZERO, _ZERO, _ZERO
        balance = await self.repo.recompute_balance(acc.id)
        pending = Decimal(str(acc.pending_debit or 0))
        return balance, pending, balance - pending

    # ── funding (externo → cuenta maestra) ──
    async def fund_master_account(self, *, amount: Decimal, idempotency_key: str, description: str) -> str:
        await self.repo.ensure_global_accounts()
        cold = await self.repo.account_for_update(code=CODE_MASTER_ACCOUNT)
        ext = await self.repo.account_for_update(code=CODE_EXTERNAL_FUNDING)
        if cold is None or ext is None:
            raise RuntimeError("LEDGER_ACCOUNTS_MISSING")
        tx = self._new_tx(kind="MASTER_ACCOUNT_FUNDING", idempotency_key=idempotency_key,
                          amount=amount, trader_id=None, description=description, status="POSTED")
        await self.db.flush()
        self._apply_entry(tx=tx, account=cold, debit=amount)
        self._apply_entry(tx=tx, account=ext, credit=amount)
        tx.posted_at = datetime.now(timezone.utc)
        return tx.id

    # ── deposito a trader (cold → trader), con HOLD ──
    async def create_deposit_hold(self, *, trader_id: str, amount: Decimal, idempotency_key: str) -> str:
        await self.repo.ensure_global_accounts()
        await self.repo.ensure_trader_holdings(trader_id)
        cold = await self.repo.account_for_update(code=CODE_MASTER_ACCOUNT)
        if cold is None:
            raise RuntimeError("LEDGER_ACCOUNTS_MISSING")
        balance = await self.repo.recompute_balance(cold.id)
        pending = Decimal(str(cold.pending_debit or 0))
        if balance - pending < amount:
            raise ValueError(
                f"INSUFFICIENT_MASTER_ACCOUNT: balance={balance} pending={pending} requested={amount}"
            )
        tx = self._new_tx(kind="TRADER_DEPOSIT", idempotency_key=idempotency_key,
                          amount=amount, trader_id=trader_id,
                          description=f"Deposit to trader {trader_id}")
        cold.pending_debit = pending + amount
        await self.db.flush()
        return tx.id

    async def commit_deposit(self, *, ledger_tx_id: str) -> None:
        tx = await self._tx_by_id(ledger_tx_id)
        if tx is None:
            raise ValueError(f"LEDGER_TX_NOT_FOUND: {ledger_tx_id}")
        if tx.status == "POSTED":
            return
        if tx.status != "PENDING":
            raise ValueError(f"LEDGER_TX_NOT_COMMITTABLE: {ledger_tx_id} status={tx.status}")
        amount = Decimal(str(tx.amount))
        cold = await self.repo.account_for_update(code=CODE_MASTER_ACCOUNT)
        holdings = await self.repo.account_for_update(code=CODE_TRADER_HOLDINGS, trader_id=tx.trader_id)
        if cold is None or holdings is None:
            raise RuntimeError("LEDGER_ACCOUNTS_MISSING")
        self._apply_entry(tx=tx, account=cold, credit=amount)
        self._apply_entry(tx=tx, account=holdings, debit=amount)
        cold.pending_debit = Decimal(str(cold.pending_debit or 0)) - amount
        tx.status = "POSTED"
        tx.posted_at = datetime.now(timezone.utc)

    async def release_deposit(self, *, ledger_tx_id: str) -> None:
        tx = await self._tx_by_id(ledger_tx_id)
        if tx is None:
            raise ValueError(f"LEDGER_TX_NOT_FOUND: {ledger_tx_id}")
        if tx.status != "PENDING":
            return
        amount = Decimal(str(tx.amount))
        cold = await self.repo.account_for_update(code=CODE_MASTER_ACCOUNT)
        if cold is not None:
            cold.pending_debit = Decimal(str(cold.pending_debit or 0)) - amount
        tx.status = "FAILED"

    # ── performance fee (trader → payment account del Master) ──
    async def post_perf_fee(self, *, trader_id: str, amount: Decimal,
                            idempotency_key: str, description: str) -> str:
        """Refleja un performance fee que salio de la cuenta MT5 del trader.

        El fee NO vuelve a la cuenta maestra: se acredita en la payment account del
        Master, que es de otro. Por eso la contrapartida es PERF_FEE_PAID, una
        cuenta global que acumula los fees cedidos (guia §9).

            debit  PERF_FEE_PAID        (sube: total de fees cedidos)
            credit TRADER_HOLDINGS      (baja: el trader tiene menos capital)

        Idempotente por `idempotency_key`: la conciliacion puede correr N veces
        sobre los mismos pagos sin duplicar asientos.
        """
        existing = (
            await self.db.execute(
                select(LedgerTransaction).where(LedgerTransaction.idempotency_key == idempotency_key)
            )
        ).scalar_one_or_none()
        if existing is not None and existing.status == "POSTED":
            return existing.id

        await self.repo.ensure_global_accounts()
        await self.repo.ensure_trader_holdings(trader_id)
        pf = await self.repo.account_for_update(code=CODE_PERF_FEE_PAID)
        holdings = await self.repo.account_for_update(code=CODE_TRADER_HOLDINGS, trader_id=trader_id)
        if pf is None or holdings is None:
            raise RuntimeError("LEDGER_ACCOUNTS_MISSING")

        tx = existing or self._new_tx(
            kind="PERF_FEE", idempotency_key=idempotency_key, amount=amount,
            trader_id=trader_id, description=description,
        )
        if existing is None:
            await self.db.flush()
        self._apply_entry(tx=tx, account=pf, debit=amount)
        self._apply_entry(tx=tx, account=holdings, credit=amount)
        tx.status = "POSTED"
        tx.posted_at = datetime.now(timezone.utc)
        return tx.id

    # ── regularizacion de capital preexistente ──
    async def post_capital_regularization(
        self, *, trader_id: str, amount: Decimal, idempotency_key: str,
        description: str, incoming: bool,
    ) -> str:
        """Asienta capital que entro o salio de una cuenta MT5 SIN pasar por acá.

        Es el caso de una cuenta que el broker acredito directo, o de una cuenta
        importada que ya tenia saldo: el dinero existe en MT5 y nuestro libro no
        lo sabe. NO mueve nada en MT5 — solo escribe el asiento que faltaba.

        Los asientos son los mismos que los de un deposito o retiro normal, y
        contra la cuenta maestra: la decision contable es que ese capital lo
        coloco el fondo, aunque haya entrado por un atajo.

        Idempotente por `idempotency_key`, que deriva del id de la transaccion
        del motor: regularizar dos veces la misma no duplica el asiento.
        """
        existing = (
            await self.db.execute(
                select(LedgerTransaction).where(
                    LedgerTransaction.idempotency_key == idempotency_key))
        ).scalar_one_or_none()
        if existing is not None and existing.status == "POSTED":
            return existing.id

        await self.repo.ensure_global_accounts()
        await self.repo.ensure_trader_holdings(trader_id)
        cold = await self.repo.account_for_update(code=CODE_MASTER_ACCOUNT)
        holdings = await self.repo.account_for_update(
            code=CODE_TRADER_HOLDINGS, trader_id=trader_id)
        if cold is None or holdings is None:
            raise RuntimeError("LEDGER_ACCOUNTS_MISSING")

        if incoming:
            # La maestra paga: no puede quedar en descubierto por un asiento
            # retroactivo, que es justo cuando nadie lo estaria mirando.
            balance = await self.repo.recompute_balance(cold.id)
            pending = Decimal(str(cold.pending_debit or 0))
            if balance - pending < amount:
                raise ValueError(
                    f"INSUFFICIENT_MASTER_ACCOUNT: balance={balance} "
                    f"pending={pending} requested={amount}")

        tx = existing or self._new_tx(
            kind="TRADER_DEPOSIT" if incoming else "TRADER_WITHDRAWAL",
            idempotency_key=idempotency_key, amount=amount,
            trader_id=trader_id, description=description,
        )
        if existing is None:
            await self.db.flush()
        if incoming:
            self._apply_entry(tx=tx, account=cold, credit=amount)
            self._apply_entry(tx=tx, account=holdings, debit=amount)
        else:
            self._apply_entry(tx=tx, account=cold, debit=amount)
            self._apply_entry(tx=tx, account=holdings, credit=amount)
        tx.status = "POSTED"
        tx.posted_at = datetime.now(timezone.utc)
        return tx.id

    # ── retiro de trader (trader → cold), posteo directo ──
    async def post_withdrawal(self, *, trader_id: str, amount: Decimal, idempotency_key: str) -> str:
        existing = (
            await self.db.execute(
                select(LedgerTransaction).where(LedgerTransaction.idempotency_key == idempotency_key)
            )
        ).scalar_one_or_none()
        if existing is not None and existing.status == "POSTED":
            return existing.id

        await self.repo.ensure_global_accounts()
        await self.repo.ensure_trader_holdings(trader_id)
        cold = await self.repo.account_for_update(code=CODE_MASTER_ACCOUNT)
        holdings = await self.repo.account_for_update(code=CODE_TRADER_HOLDINGS, trader_id=trader_id)
        if cold is None or holdings is None:
            raise RuntimeError("LEDGER_ACCOUNTS_MISSING")

        tx = existing or self._new_tx(
            kind="TRADER_WITHDRAWAL", idempotency_key=idempotency_key,
            amount=amount, trader_id=trader_id, description=f"Withdrawal from trader {trader_id}",
        )
        if existing is None:
            await self.db.flush()
        self._apply_entry(tx=tx, account=cold, debit=amount)
        self._apply_entry(tx=tx, account=holdings, credit=amount)
        tx.status = "POSTED"
        tx.posted_at = datetime.now(timezone.utc)
        return tx.id
