"""
Fondeo de la cuenta maestra con verificacion OTP por email (2 pasos):

1. request_funding : registra la intencion (monto + hash del OTP + expiracion) y
   envia el codigo por email. NO mueve saldo todavia.
2. verify_funding  : valida el codigo y, si es correcto, ejecuta el fondeo real
   reutilizando TraderService.fund_master_account (movimiento + asiento contable).

El codigo se guarda hasheado (Argon2). Protecciones: expiracion, tope de intentos
y estado (PENDING → VERIFIED / CANCELLED). El verify es idempotente: revalidar un
OTP ya usado devuelve el mismo movimiento de fondeo.
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import (
    AmountInvalidError,
    OtpAlreadyUsedError,
    OtpEmailMissingError,
    OtpExpiredError,
    OtpInvalidError,
    OtpMaxAttemptsError,
    OtpNotFoundError,
)
from app.core.security import hash_code, verify_code
from app.models.funding_otp import FundingOtp
from app.models.movement import Movement
from app.services.trader_service import TraderService
from app.services.email_service import send_otp_email

logger = logging.getLogger(__name__)
_CENTS = Decimal("0.01")


class FundingOtpService:
    def __init__(self, db: AsyncSession):
        self.db = db

    # ── helpers ──
    @staticmethod
    def _normalize_amount(amount: Decimal) -> Decimal:
        amt = Decimal(amount).quantize(_CENTS, rounding=ROUND_DOWN)
        if amt <= 0:
            raise AmountInvalidError(message="El monto debe ser mayor a 0", detail=f"amount={amount}")
        return amt

    @staticmethod
    def _generate_code() -> str:
        return "".join(str(secrets.randbelow(10)) for _ in range(settings.OTP_LENGTH))

    # ── paso 1: solicitar OTP ──
    async def request_funding(self, *, amount: Decimal, email: Optional[str]) -> dict:
        amount = self._normalize_amount(amount)
        # `email` es el del api_user ADMIN autenticado (lo pasa el endpoint).
        to = (email or settings.FUNDING_OTP_EMAIL or "").strip()
        if not to:
            raise OtpEmailMissingError(
                message="El api_user ADMIN no tiene email configurado para el OTP",
                detail=None,
            )
        code = self._generate_code()
        otp = FundingOtp(
            email=to, amount=amount, currency=settings.LEDGER_CURRENCY,
            code_hash=hash_code(code), status="PENDING", attempts=0,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=settings.OTP_EXPIRY_MINUTES),
        )
        self.db.add(otp)
        await self.db.flush()
        otp_id, expires_at = otp.id, otp.expires_at
        # Persistimos el OTP antes de enviar el mail: si el SMTP falla, el OTP ya
        # existe y el error de envio se propaga sin dejar estado inconsistente.
        await self.db.commit()

        email_sent = await send_otp_email(
            to=to, code=code, amount=amount, currency=settings.LEDGER_CURRENCY)
        return {
            "id": otp_id, "email": to, "amount": amount,
            "currency": settings.LEDGER_CURRENCY, "expires_at": expires_at,
            "email_sent": email_sent,
            # Solo en modo dev (SMTP no configurado) devolvemos el codigo en claro.
            "otp_debug": None if email_sent else code,
        }

    # ── reenviar OTP de una solicitud de fondeo existente ──
    async def resend_funding(self, *, otp_id: str) -> dict:
        otp = (
            await self.db.execute(select(FundingOtp).where(FundingOtp.id == otp_id))
        ).scalar_one_or_none()
        if otp is None:
            raise OtpNotFoundError(
                message="No existe una solicitud de fondeo con ese id", detail=otp_id)
        if otp.status == "VERIFIED":
            raise OtpAlreadyUsedError(
                message="Ese fondeo ya fue confirmado; no se puede reenviar el OTP", detail=None)

        # Regenera el código: revive la solicitud aunque el OTP anterior esté
        # caducado/cancelado, y resetea expiración e intentos.
        code = self._generate_code()
        otp.code_hash = hash_code(code)
        otp.expires_at = datetime.now(timezone.utc) + timedelta(minutes=settings.OTP_EXPIRY_MINUTES)
        otp.attempts = 0
        otp.status = "PENDING"
        to, amount, currency, expires_at = otp.email, Decimal(str(otp.amount)), otp.currency, otp.expires_at
        await self.db.commit()

        email_sent = await send_otp_email(to=to, code=code, amount=amount, currency=currency)
        return {
            "id": otp_id, "email": to, "amount": amount, "currency": currency,
            "expires_at": expires_at, "email_sent": email_sent,
            "otp_debug": None if email_sent else code,
        }

    # ── paso 2: validar OTP y ejecutar el fondeo ──
    async def verify_funding(self, *, otp_id: str, code: str) -> Movement:
        otp = (
            await self.db.execute(select(FundingOtp).where(FundingOtp.id == otp_id))
        ).scalar_one_or_none()
        if otp is None:
            raise OtpNotFoundError(message="El OTP no existe", detail=None)

        # Ya usado → idempotente: devolvemos el movimiento de fondeo creado.
        if otp.status == "VERIFIED":
            if otp.movement_id:
                return await TraderService(self.db).get_movement(movement_id=otp.movement_id)
            raise OtpAlreadyUsedError(message="El OTP ya fue utilizado", detail=None)
        if otp.status != "PENDING":
            raise OtpAlreadyUsedError(
                message="El OTP no esta disponible", detail=f"status={otp.status}")

        now = datetime.now(timezone.utc)
        if now >= otp.expires_at:
            otp.status = "CANCELLED"
            await self.db.commit()
            raise OtpExpiredError(message="El OTP expiro, solicita uno nuevo", detail=None)
        if otp.attempts >= settings.OTP_MAX_ATTEMPTS:
            otp.status = "CANCELLED"
            await self.db.commit()
            raise OtpMaxAttemptsError(message="Demasiados intentos, solicita un OTP nuevo", detail=None)

        if not verify_code(code, otp.code_hash):
            otp.attempts += 1
            remaining = max(settings.OTP_MAX_ATTEMPTS - otp.attempts, 0)
            if otp.attempts >= settings.OTP_MAX_ATTEMPTS:
                otp.status = "CANCELLED"
            await self.db.commit()
            raise OtpInvalidError(message="OTP invalido", detail=f"intentos_restantes={remaining}")

        # Codigo correcto → fondeo real (movimiento + ledger, idempotente por OTP).
        movement = await TraderService(self.db).fund_master_account(
            amount=Decimal(str(otp.amount)), idempotency_key=f"fund-otp-{otp.id}")

        otp.status = "VERIFIED"
        otp.verified_at = datetime.now(timezone.utc)
        otp.movement_id = movement.id
        await self.db.commit()
        return movement
