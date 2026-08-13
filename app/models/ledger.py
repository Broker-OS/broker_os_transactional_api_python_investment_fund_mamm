"""Plan de cuentas + transacciones + asientos (doble entrada)."""
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base
from app.models._helpers import now_utc, uuid_str


class LedgerAccount(Base):
    """Cuenta del plan contable. Balance cacheado con lock a nivel fila."""

    __tablename__ = "ledger_accounts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    code: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    # MASTER_ACCOUNT | EXTERNAL_FUNDING | TRADER_HOLDINGS | PF_PAYABLE
    # PF_PAYABLE: contrapartida de los performance fees que salen del trader
    # hacia la payment account del Master (guia §9). No es plata nuestra: es el
    # acumulado de fees cedidos, y por eso NO toca la cuenta maestra.
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    trader_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("traders.id", ondelete="RESTRICT"), nullable=True
    )
    currency: Mapped[str] = mapped_column(String(10), nullable=False, default="USD")
    balance: Mapped[Numeric] = mapped_column(Numeric(20, 8), nullable=False, default=0)
    pending_debit: Mapped[Numeric] = mapped_column(Numeric(20, 8), nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)

    trader = relationship("Trader", back_populates="holdings")

    __table_args__ = (
        CheckConstraint(
            "kind IN ('MASTER_ACCOUNT','EXTERNAL_FUNDING','TRADER_HOLDINGS','PF_PAYABLE')",
            name="ck_ledger_accounts_kind",
        ),
        Index("uq_ledger_accounts_global_code", "code", unique=True,
              postgresql_where=text("trader_id IS NULL")),
        Index("uq_ledger_accounts_trader_holdings", "trader_id", unique=True,
              postgresql_where=text("trader_id IS NOT NULL")),
    )


class LedgerTransaction(Base):
    __tablename__ = "ledger_transactions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    # TRADER_DEPOSIT | TRADER_WITHDRAWAL | MASTER_ACCOUNT_FUNDING | PERF_FEE
    kind: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING", index=True)
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False, unique=True, index=True)
    trader_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("traders.id", ondelete="RESTRICT"), nullable=True
    )
    amount: Mapped[Numeric] = mapped_column(Numeric(20, 8), nullable=False)
    currency: Mapped[str] = mapped_column(String(10), nullable=False, default="USD")
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    tx_metadata: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    entries = relationship("LedgerEntry", back_populates="transaction", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint(
            "kind IN ('TRADER_DEPOSIT','TRADER_WITHDRAWAL','MASTER_ACCOUNT_FUNDING','PERF_FEE')",
            name="ck_ledger_tx_kind",
        ),
        CheckConstraint("status IN ('PENDING','POSTED','FAILED')", name="ck_ledger_tx_status"),
    )


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    tx_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("ledger_transactions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("ledger_accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    debit: Mapped[Numeric] = mapped_column(Numeric(20, 8), nullable=False, default=0)
    credit: Mapped[Numeric] = mapped_column(Numeric(20, 8), nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String(10), nullable=False, default="USD")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)

    transaction = relationship("LedgerTransaction", back_populates="entries")
