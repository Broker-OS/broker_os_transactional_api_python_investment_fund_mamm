"""Schemas de traders (clientes finales)."""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class TraderRegisterRequest(BaseModel):
    # El `external_reference` NO se envia: lo genera este servicio (id numerico unico).
    email: EmailStr
    first_name: Optional[str] = Field(default=None, max_length=120)
    last_name: Optional[str] = Field(default=None, max_length=120)
    max_active_leaders: Optional[int] = Field(
        default=None, ge=0,
        description=("Cupo de estrategias simultáneas que autoriza el plan del cliente. "
                     "Se envía al motor MAM en cada suscripción; si se omite, se usa el "
                     "valor por defecto del servicio."),
    )

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "email": "trader1@example.com",
                "first_name": "Juan",
                "last_name": "Perez",
                "max_active_leaders": 1,
            }
        }
    )


class TraderRead(BaseModel):
    id: str
    external_reference: str
    email: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    max_active_leaders: Optional[int] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class TraderOwner(BaseModel):
    """api_user (consumidor) que creó el trader."""

    id: Optional[str] = None
    name: Optional[str] = None
    email: Optional[str] = None
    role: Optional[str] = None


class TraderListItem(BaseModel):
    id: str
    external_reference: str
    email: str
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    max_active_leaders: Optional[int] = None
    created_at: datetime
    owner: Optional[TraderOwner] = None  # quién lo creó

    model_config = ConfigDict(from_attributes=True)


class TraderListResponse(BaseModel):
    total: int
    page: int
    limit: int
    pages: int
    items: list[TraderListItem]
