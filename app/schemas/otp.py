"""Schemas del fondeo con OTP por email (cuenta maestra)."""
from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class FundingOtpRequest(BaseModel):
    """Paso 1: solicitar el OTP para fondear `amount`.

    No se pide email: el OTP va SIEMPRE al correo del api_user ADMIN autenticado.
    """

    amount: Decimal = Field(gt=0, le=Decimal("10000000"))

    model_config = ConfigDict(json_schema_extra={"example": {"amount": "100.00"}})


class FundingOtpRequestResponse(BaseModel):
    id: str = Field(description="id de la solicitud de fondeo; usalo en verify/resend")
    email: str
    amount: Decimal
    currency: str
    expires_at: datetime
    email_sent: bool = Field(description="true si el OTP se envió por SMTP")
    otp_debug: Optional[str] = Field(
        default=None,
        description="SOLO en modo dev (SMTP sin configurar): el OTP en claro para poder probar.")


class FundingOtpVerifyRequest(BaseModel):
    """Paso 2: validar el OTP (el `id` va en la URL) y ejecutar el fondeo."""

    code: str = Field(min_length=4, max_length=10)

    model_config = ConfigDict(json_schema_extra={"example": {"code": "123456"}})
