"""Depositos on-chain (USDC sobre una cadena EVM) reportados por un api_user."""
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

STATUS_CONFIRMED = "CONFIRMED"
STATUS_REJECTED = "REJECTED"


class CryptoDeposit(Base):
    """Una transaccion on-chain presentada como comprobante de pago.

    Se guardan tambien los intentos RECHAZADOS: sirven de auditoria y permiten
    ver quien esta enviando comprobantes invalidos.

    La proteccion contra reutilizacion es el indice unico parcial sobre
    `tx_hash` para las CONFIRMED: un mismo hash no se puede acreditar dos veces,
    pero un intento rechazado (por ejemplo, por falta de confirmaciones) se
    puede reintentar mas tarde.

    Un deposito confirmado acredita la cuenta maestra en el mismo commit, y
    `ledger_tx_id` apunta a ese asiento: de un movimiento contable siempre se
    puede llegar a la transaccion on-chain que lo origino, y al reves.
    """

    __tablename__ = "crypto_deposits"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    # Quien presento el comprobante.
    api_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("api_users.id", ondelete="RESTRICT"), nullable=True, index=True
    )

    tx_hash: Mapped[str] = mapped_column(String(66), nullable=False, index=True)
    chain_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)

    # Lo que declaro el usuario vs lo que realmente se leyo en la cadena.
    declared_amount: Mapped[Numeric] = mapped_column(Numeric(38, 18), nullable=False)
    onchain_amount: Mapped[Numeric | None] = mapped_column(Numeric(38, 18), nullable=True)
    # Monto en la unidad minima del token (sin decimales aplicados).
    raw_amount: Mapped[str | None] = mapped_column(String(80), nullable=True)
    token_symbol: Mapped[str] = mapped_column(String(20), nullable=False, default="USDC")
    token_contract: Mapped[str | None] = mapped_column(String(42), nullable=True)
    token_decimals: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    from_address: Mapped[str | None] = mapped_column(String(42), nullable=True, index=True)
    to_address: Mapped[str | None] = mapped_column(String(42), nullable=True)
    block_number: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    confirmations: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    rejection_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    rejection_detail: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Asiento contable que acredito la cuenta maestra con este deposito.
    # Nulo en los REJECTED, y en los CONFIRMED anteriores a la acreditacion
    # automatica (o si se desactivo con CRYPTO_DEPOSIT_AUTO_CREDIT).
    ledger_tx_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("ledger_transactions.id", ondelete="RESTRICT"),
        nullable=True, index=True
    )

    # Notificacion a los admin.
    notified_admins: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)

    __table_args__ = (
        CheckConstraint(f"status IN ('{STATUS_CONFIRMED}','{STATUS_REJECTED}')",
                        name="ck_crypto_deposits_status"),
        CheckConstraint("declared_amount > 0", name="ck_crypto_deposits_declared_amount"),
        # Una transaccion se acredita UNA sola vez. Los rechazos no bloquean el
        # reintento (un tx sin confirmaciones suficientes hoy puede tenerlas manana).
        Index("uq_crypto_deposits_confirmed_tx", "tx_hash", unique=True,
              postgresql_where=text(f"status = '{STATUS_CONFIRMED}'")),
        Index("ix_crypto_deposits_created", "created_at"),
    )
