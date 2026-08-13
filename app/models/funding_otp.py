"""OTP para fondear la cuenta maestra (verificación por email en 2 pasos)."""
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base
from app.models._helpers import now_utc, uuid_str


class FundingOtp(Base):
    """Guarda la intención de fondeo + el hash del OTP hasta que se valida."""

    __tablename__ = "funding_otps"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    amount: Mapped[Numeric] = mapped_column(Numeric(20, 8), nullable=False)
    currency: Mapped[str] = mapped_column(String(10), nullable=False, default="USD")
    code_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    # PENDING | VERIFIED | CANCELLED (expirado / máx intentos)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING", index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Movimiento de fondeo creado al validar el OTP (idempotencia del verify).
    movement_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING','VERIFIED','CANCELLED')", name="ck_funding_otps_status"
        ),
    )
