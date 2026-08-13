"""Schemas de api_users (consumidores de la API)."""
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class ApiUserCreateRequest(BaseModel):
    email: EmailStr
    name: str = Field(min_length=1, max_length=120)
    role: Literal["ADMIN", "USER"] = "USER"

    model_config = ConfigDict(json_schema_extra={
        "example": {"email": "socio@bridgemarkets.global", "name": "Socio", "role": "USER"}})


class ApiUserUpdateRequest(BaseModel):
    """Editar datos del api_user (name/email/role). La API key NO se toca acá."""

    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    email: Optional[EmailStr] = None
    role: Optional[Literal["ADMIN", "USER"]] = None
    receives_notifications: Optional[bool] = Field(
        default=None,
        description=("Si recibe los avisos por email dirigidos a los ADMIN. Poner en "
                     "`false` para cuentas de servicio: necesitan rol ADMIN para operar, "
                     "pero nadie lee su casilla."))

    model_config = ConfigDict(json_schema_extra={
        "example": {"name": "Socio Producción", "email": "ops@socio.com", "role": "USER"}})


class ApiUserRead(BaseModel):
    id: str
    email: str
    name: str
    role: str
    is_active: bool
    receives_notifications: bool = Field(
        default=True, description="Si recibe los avisos por email dirigidos a los ADMIN.")
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ApiUserCreatedResponse(BaseModel):
    api_user: ApiUserRead
    api_key: str = Field(description="La API key en claro — se muestra UNA sola vez. Guardala.")


class ApiUserListResponse(BaseModel):
    total: int
    items: list[ApiUserRead]
