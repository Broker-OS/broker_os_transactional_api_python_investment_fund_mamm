"""Registro operativo de cada movimiento (cara al socio).

Todo el capital se mueve contra cuentas MT5 reales del motor MAM. A diferencia
del rail PAMM, aca los endpoints financieros SI aceptan `idempotency_key`
(spec §11.1/§12), asi que un reintento con la misma key es seguro y no genera
un doble cargo. `AMBIGUOUS` deja de ser el caso normal ante un timeout y pasa a
ser la excepcion: solo queda asi cuando se agotaron los reintentos idempotentes.

Igual se distingue lo SOLICITADO de lo EFECTIVAMENTE movido: el retiro de un
follower cobra primero el performance fee vencido (spec §10.1) y la API rechaza
la operacion si el free margin restante no cubre el fee mas el monto pedido.
"""
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base
from app.models._helpers import now_utc, uuid_str

# Direcciones. FUNDING es externo -> cuenta maestra; PAYMENT_WITHDRAWAL es el
# retiro de fees ya acreditados en la cuenta PAYMENT del leader (spec §11.3),
# que usa un endpoint distinto al retiro del follower y no recalcula el fee.
DIRECTIONS = (
    "DEPOSIT", "WITHDRAWAL", "FUNDING",
    "SUBSCRIBE", "UNSUBSCRIBE", "PERF_FEE", "PAYMENT_WITHDRAWAL",
)
STATUSES = ("PENDING", "COMPLETED", "FAILED", "AMBIGUOUS", "REJECTED")


class Movement(Base):
    __tablename__ = "movements"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    trader_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("traders.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    direction: Mapped[str] = mapped_column(String(20), nullable=False)
    # Monto contable del movimiento: el EFECTIVO cuando ya se conoce.
    amount: Mapped[Numeric] = mapped_column(Numeric(20, 8), nullable=False)
    currency: Mapped[str] = mapped_column(String(10), nullable=False, default="USD")
    # Spec §12: key determinista derivada del id de transaccion del CRM. Es la
    # MISMA que viaja al proveedor, para que reintentar no duplique nada.
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False, unique=True, index=True)
    # REJECTED: el proveedor respondio OK pero no movio nada.
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING", index=True)

    # ── cuenta MAM sobre la que actua el movimiento ──
    mam_account_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("mam_accounts.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    mt5_login: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    # Para SUBSCRIBE / UNSUBSCRIBE / PERF_FEE: relacion afectada.
    allocation_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)

    # ── conciliacion ──
    # Referencia unica del CRM, estable entre reintentos.
    crm_reference: Mapped[str | None] = mapped_column(String(120), nullable=True, unique=True, index=True)
    # Lo que pidio el socio vs lo que el proveedor realmente movio.
    requested_amount: Mapped[Numeric | None] = mapped_column(Numeric(20, 8), nullable=True)
    effective_amount: Mapped[Numeric | None] = mapped_column(Numeric(20, 8), nullable=True)
    # Performance fee cobrado justo antes del retiro (spec §10.1).
    perf_fee_at_request: Mapped[Numeric | None] = mapped_column(Numeric(20, 8), nullable=True)
    balance_before: Mapped[Numeric | None] = mapped_column(Numeric(20, 8), nullable=True)
    balance_after: Mapped[Numeric | None] = mapped_column(Numeric(20, 8), nullable=True)
    mt5_deal_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)

    # ── resolucion manual de un movimiento AMBIGUOUS ──
    resolved_by_api_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("api_users.id", ondelete="RESTRICT"), nullable=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Spec §11.3: el retiro idempotente repetido devuelve result="ALREADY_PROCESSED"
    # sin volver a debitar MT5. Se guarda para poder distinguir un cobro real de
    # una repeticion cuando se concilia.
    provider_result: Mapped[str | None] = mapped_column(String(30), nullable=True)
    provider_reference: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ledger_tx_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "direction IN (" + ",".join(f"'{d}'" for d in DIRECTIONS) + ")",
            name="ck_movements_direction",
        ),
        CheckConstraint(
            "status IN (" + ",".join(f"'{s}'" for s in STATUSES) + ")",
            name="ck_movements_status",
        ),
        Index("ix_movements_trader_created", "trader_id", "created_at"),
        # Panel de pendientes de conciliar.
        Index("ix_movements_status_direction", "status", "direction"),
        # Un solo movimiento en vuelo POR CUENTA MT5. En MAM el cupo no es por
        # cliente: un mismo trader puede tener varias cuentas operando distintas
        # estrategias a la vez, y bloquear todas porque una tiene un deposito en
        # curso seria incorrecto. Lo que no puede haber son dos operaciones
        # simultaneas sobre el MISMO login.
        Index("uq_movements_one_pending_per_account", "mam_account_id", unique=True,
              postgresql_where=text("status = 'PENDING' AND mam_account_id IS NOT NULL")),
    )
