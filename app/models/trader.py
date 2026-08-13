"""Cliente final que el socio administra sobre el motor MAM.

El MAM API no tiene concepto de usuario ni de SSO: solo conoce cuentas MT5. La
relacion cliente ↔ cuenta la conserva este servicio (spec §2: "el CRM conserva
la relacion entre sus propios usuarios y el mt5_login de cada cuenta").
"""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base
from app.models._helpers import now_utc, uuid_str


class Trader(Base):
    __tablename__ = "traders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    # api_user (consumidor) dueño del trader. Un USER solo ve/opera los suyos; el
    # ADMIN ve todos. Nullable por compatibilidad; en la práctica siempre seteado.
    owner_api_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("api_users.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    # ID numerico unico que este servicio ASIGNA (el socio no lo provee). Spec
    # §2.1: external_reference NO pertenece al motor MAM; se resuelve localmente
    # y se asocia con el mt5_login. Estable/inmutable.
    external_reference: Mapped[str] = mapped_column(String(120), nullable=False, unique=True, index=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    first_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # Cupo de allocations vivas simultaneas que autoriza el plan del cliente.
    # Spec §7.1: el limite NO vive en el motor MAM; lo resuelve el integrador y
    # lo manda en cada POST /allocations. Null = usar el default del servicio.
    max_active_leaders: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False
    )
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    holdings = relationship("LedgerAccount", back_populates="trader")
