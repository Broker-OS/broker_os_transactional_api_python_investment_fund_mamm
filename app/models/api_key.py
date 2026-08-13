"""Credenciales del socio (hash Argon2)."""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base
from app.models._helpers import now_utc, uuid_str


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    # api_user dueño de la key (hereda su rol). Nullable por compatibilidad con
    # datos previos al modelo de roles; en la práctica siempre está seteado.
    api_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("api_users.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    # Prefijo publico (para localizar el hash sin exponerlo) + hash Argon2.
    key_prefix: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    key_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
