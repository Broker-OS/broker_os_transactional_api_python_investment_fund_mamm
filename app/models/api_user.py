"""
Usuario CONSUMIDOR de la API (opera el bridge). Es una capa distinta del `trader`
de Bridge Markets: el api_user autentica con API key y tiene rol; el trader es el cliente
final que accede a Bridge Markets. No se mezclan.
"""
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.database import Base
from app.models._helpers import now_utc, uuid_str

ROLE_ADMIN = "ADMIN"
ROLE_USER = "USER"
ROLES = (ROLE_ADMIN, ROLE_USER)


class ApiUser(Base):
    __tablename__ = "api_users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    # ADMIN: casa/back-office (fondear cuenta maestra, gestionar api_users, ver todo).
    # USER:  consumidor normal (registrar y operar SUS traders).
    role: Mapped[str] = mapped_column(String(20), nullable=False, default=ROLE_USER)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Si recibe los avisos por email dirigidos a los ADMIN (depositos on-chain).
    # Existe por las cuentas de servicio: el usuario de los cron jobs necesita
    # rol ADMIN para operar, pero su email es un buzon que no existe. Sin esto,
    # cada aviso intenta entregarse a una direccion que siempre rebota.
    receives_notifications: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)

    __table_args__ = (
        CheckConstraint("role IN ('ADMIN','USER')", name="ck_api_users_role"),
    )
