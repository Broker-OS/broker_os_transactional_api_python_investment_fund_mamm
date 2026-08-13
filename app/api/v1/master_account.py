"""Endpoints de la cuenta maestra: fondeo con OTP por email y saldo."""
from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin
from app.db.database import get_db
from app.models.api_user import ApiUser
from app.schemas.common import APIResponse
from app.schemas.movement import MovementRead
from app.schemas.otp import (
    FundingOtpRequest,
    FundingOtpRequestResponse,
    FundingOtpVerifyRequest,
)
from app.schemas.report import MasterAccountRead
from app.services.funding_otp_service import FundingOtpService
from app.services.report_service import ReportService

router = APIRouter(tags=["2. Configuración · Cuenta maestra"])


@router.post("/master-account/funding", response_model=APIResponse, status_code=status.HTTP_202_ACCEPTED,
             summary="Solicitar fondeo de la cuenta maestra (envía OTP al email del ADMIN)",
             description=(
                 "**Paso 1 de 2.** Registra la intención de fondeo por `amount` y **envía un "
                 "OTP al email del api_user ADMIN autenticado** (no se pide email). Devuelve el "
                 "`id` de la solicitud. El saldo **no** se acredita todavía: confirmalo en "
                 "`POST /master-account/funding/{id}/verify` con el `code`.\n\n"
                 "Si el server no tiene transporte de email (modo dev), el OTP vuelve en "
                 "`data.otp_debug` para poder probar el flujo."
             ))
async def request_funding(body: FundingOtpRequest, db: AsyncSession = Depends(get_db),
                          admin: ApiUser = Depends(require_admin)):
    data = await FundingOtpService(db).request_funding(amount=body.amount, email=admin.email)
    return APIResponse(
        success=True, http_status=202,
        message="Te enviamos un OTP al email. Confirma el fondeo con el código recibido.",
        data=FundingOtpRequestResponse(**data).model_dump(mode="json"))


@router.post("/master-account/funding/{funding_id}/verify", response_model=APIResponse,
             status_code=status.HTTP_201_CREATED,
             summary="Validar OTP y ejecutar el fondeo (externo → cuenta maestra)",
             description=(
                 "**Paso 2 de 2.** Valida el `code` contra la solicitud `{funding_id}`. Si es "
                 "correcto, acredita la cuenta maestra (`debit MASTER_ACCOUNT` / `credit "
                 "EXTERNAL_FUNDING`) y devuelve el movimiento `COMPLETED`. Idempotente: "
                 "revalidar uno ya usado devuelve el mismo movimiento. Errores: `OTP_INVALID`, "
                 "`OTP_EXPIRED`, `OTP_MAX_ATTEMPTS`, `OTP_NOT_FOUND`."
             ))
async def verify_funding(funding_id: str, body: FundingOtpVerifyRequest,
                         db: AsyncSession = Depends(get_db),
                         admin: ApiUser = Depends(require_admin)):
    mv = await FundingOtpService(db).verify_funding(otp_id=funding_id, code=body.code)
    return APIResponse(
        success=True, http_status=201, message="Cuenta maestra fondeada correctamente",
        data=MovementRead.model_validate(mv).model_dump(mode="json"))


@router.post("/master-account/funding/{funding_id}/resend", response_model=APIResponse,
             status_code=status.HTTP_202_ACCEPTED,
             summary="Reenviar el OTP de una solicitud de fondeo existente",
             description=(
                 "Reenvía el OTP de la solicitud `{funding_id}` **regenerando el código**: "
                 "sirve cuando el OTP anterior **caducó** (`OTP_EXPIRED`) o se agotaron los "
                 "intentos. Resetea la expiración y los intentos, y manda el nuevo código al "
                 "mismo email. Si el fondeo ya fue confirmado responde `409`; si el `id` no "
                 "existe, `404`."
             ))
async def resend_funding(funding_id: str, db: AsyncSession = Depends(get_db),
                         admin: ApiUser = Depends(require_admin)):
    data = await FundingOtpService(db).resend_funding(otp_id=funding_id)
    return APIResponse(
        success=True, http_status=202,
        message="Te reenviamos un nuevo OTP al email.",
        data=FundingOtpRequestResponse(**data).model_dump(mode="json"))


@router.get("/master-account", response_model=APIResponse,
            summary="Saldo de la cuenta maestra",
            description=("Devuelve `balance`, `pending_debit` (retenciones) y "
                         "`available = balance − pending_debit`.\n\n"
                         "Es la tesorería de la que sale todo el capital que después se "
                         "coloca en las cuentas MT5 de los clientes."))
async def get_master_account(db: AsyncSession = Depends(get_db)):
    data = await ReportService(db).master_account()
    return APIResponse(success=True, http_status=200, message="Saldo de la cuenta maestra",
                       data=MasterAccountRead(**data).model_dump(mode="json"))
