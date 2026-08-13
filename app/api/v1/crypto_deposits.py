"""Depósitos on-chain: presentar un comprobante de pago y verificarlo en la cadena."""
from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_api_key
from app.core.config import settings
from app.db.database import get_db
from app.models.api_user import ApiUser
from app.schemas.common import APIResponse
from app.schemas.crypto_deposit import (
    CryptoDepositListResponse,
    CryptoDepositRead,
    CryptoDepositRequest,
)
from app.services.crypto_deposit_service import CryptoDepositService

router = APIRouter(tags=["12. Depósitos on-chain (cripto)"])


@router.post("/crypto-deposits", response_model=APIResponse,
             status_code=status.HTTP_201_CREATED,
             summary="Presentar un comprobante de pago on-chain",
             description=(
                 "Recibe el **hash de la transacción**, el **chain id** y el **monto**, y "
                 "verifica el pago contra la cadena EVM. Si es válido:\n\n"
                 "1. registra el depósito como `CONFIRMED`;\n"
                 "2. **acredita la cuenta maestra** con un asiento `MASTER_ACCOUNT_FUNDING` "
                 "(`debit MASTER_ACCOUNT` / `credit EXTERNAL_FUNDING`), devuelto en "
                 "`ledger_tx_id`;\n"
                 "3. **avisa por email a todos los `api_user` con rol ADMIN**.\n\n"
                 "Los tres pasos viajan en la misma transacción de base de datos: no puede "
                 "quedar un depósito acreditado sin asiento, ni saldo en la cuenta maestra sin "
                 "el comprobante on-chain que lo justifica.\n\n"
                 "No se cree nada de lo enviado: lo único que vale es lo que dice la cadena. "
                 "Se comprueba, en orden, que el `chain_id` sea el configurado, que la "
                 "transacción exista y esté minada, que **no haya revertido**, que tenga "
                 "confirmaciones suficientes, que sus logs contengan una transferencia del "
                 "token configurado **hacia nuestra address receptora**, y que el monto "
                 "coincida con el declarado.\n\n"
                 "**Una transacción se acredita una sola vez**: reenviar el mismo hash "
                 "devuelve `DEPOSIT_ALREADY_REGISTERED`. Un intento rechazado sí se puede "
                 "reintentar — por ejemplo si faltaban confirmaciones.\n\n"
                 "Errores: `CHAIN_MISMATCH`, `TX_NOT_FOUND`, `TX_FAILED_ON_CHAIN`, "
                 "`TX_NOT_CONFIRMED`, `TRANSFER_NOT_FOUND`, `AMOUNT_MISMATCH`, "
                 "`DEPOSIT_ALREADY_REGISTERED`, `EVM_NOT_CONFIGURED`, `EVM_RPC_ERROR`, "
                 "`LEDGER_INCONSISTENT`."
             ))
async def submit_crypto_deposit(body: CryptoDepositRequest,
                                db: AsyncSession = Depends(get_db),
                                caller: ApiUser = Depends(require_api_key)):
    deposit = await CryptoDepositService(db).submit(
        tx_hash=body.tx_hash, chain_id=body.chain_id, value=body.value, caller=caller)
    acreditado = ("Cuenta maestra acreditada."
                  if deposit.ledger_tx_id else "SIN acreditar en el libro contable.")
    return APIResponse(
        success=True, http_status=201,
        message=(f"Deposito verificado en {settings.EVM_NETWORK_NAME}. {acreditado} "
                 f"Avisados {deposit.notified_admins} admin(s)."),
        data=CryptoDepositRead.model_validate(deposit).model_dump(mode="json"))


@router.get("/crypto-deposits", response_model=APIResponse,
            summary="Historial de comprobantes presentados",
            description=(
                "Depósitos on-chain presentados, confirmados y rechazados. Los rechazados "
                "quedan registrados para auditoría, con el motivo.\n\n"
                "**Dos filtros distintos, no confundirlos:**\n\n"
                "- `status` → el resultado: `CONFIRMED` o `REJECTED`\n"
                "- `rejection_code` → el **motivo** del rechazo: `AMOUNT_MISMATCH`, "
                "`CHAIN_MISMATCH`, `TX_NOT_FOUND`, `TX_NOT_CONFIRMED`, `TX_FAILED_ON_CHAIN`, "
                "`TRANSFER_NOT_FOUND`\n\n"
                "Un `USER` ve solo los suyos; un `ADMIN`, todos."
            ))
async def list_crypto_deposits(status_filter: str = Query(
                                   default=None, alias="status",
                                   description="CONFIRMED | REJECTED"),
                               rejection_code: str = Query(
                                   default=None,
                                   description=("Motivo del rechazo, ej. `AMOUNT_MISMATCH`. "
                                                "Útil para auditar intentos fallidos.")),
                               limit: int = Query(default=50, ge=1, le=200),
                               offset: int = Query(default=0, ge=0),
                               db: AsyncSession = Depends(get_db),
                               caller: ApiUser = Depends(require_api_key)):
    rows, total = await CryptoDepositService(db).list(
        status=status_filter, rejection_code=rejection_code,
        limit=limit, offset=offset, caller=caller)
    payload = CryptoDepositListResponse(
        total=total, limit=limit, offset=offset,
        items=[CryptoDepositRead.model_validate(r) for r in rows])
    return APIResponse(success=True, http_status=200,
                       message="Depositos obtenidos correctamente",
                       data=payload.model_dump(mode="json"))
