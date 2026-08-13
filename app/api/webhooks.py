"""
Receptor del webhook del motor MAM.

Cuelga FUERA de `/api/v1` a proposito: ese router exige `X-API-Key`, y el
proveedor no la tiene — autentica firmando el cuerpo con un secreto compartido
(spec §11.6). Meterlo bajo la API key obligaria a darle nuestra clave a un
tercero para que nos avise de algo.
"""
import logging

from fastapi import APIRouter, Header, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends

from app.db.database import get_db
from app.services.mam_webhook_service import MamWebhookService

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Webhooks"])


@router.post("/webhooks/mam", status_code=status.HTTP_204_NO_CONTENT,
             summary="Recibir eventos del motor MAM",
             description=(
                 "Endpoint que llama **el proveedor**, no vos. No requiere API key: "
                 "autentica verificando la firma `HMAC-SHA256` del cuerpo contra el "
                 "secreto compartido.\n\n"
                 "Se verifica sobre los **bytes exactos** del cuerpo antes de "
                 "deserializar el JSON, se compara en tiempo constante, y se rechazan "
                 "los eventos de más de cinco minutos.\n\n"
                 "El evento se **persiste antes de procesarlo** y se responde `204` "
                 "enseguida. Una reentrega del mismo `event_id` responde `204` sin "
                 "repetir efectos.\n\n"
                 "Recibir un evento **no inicia ni autoriza** una baja: informa una "
                 "terminación ya procesada del otro lado."
             ))
async def receive_mam_webhook(
    request: Request,
    x_mam_event_id: str | None = Header(default=None, alias="X-MAM-Event-Id"),
    x_mam_event_type: str | None = Header(default=None, alias="X-MAM-Event-Type"),
    x_mam_timestamp: str | None = Header(default=None, alias="X-MAM-Timestamp"),
    x_mam_signature: str | None = Header(default=None, alias="X-MAM-Signature"),
    db: AsyncSession = Depends(get_db),
):
    # Los bytes crudos: re-serializar el JSON invalidaria la firma.
    raw = await request.body()

    svc = MamWebhookService(db)
    svc.verify_signature(raw_body=raw, timestamp=x_mam_timestamp,
                         signature=x_mam_signature)

    resultado = await svc.receive(
        raw_body=raw, event_id=x_mam_event_id, event_type=x_mam_event_type,
        signature_verified=True)

    if resultado["duplicate"]:
        logger.info("MAM webhook: evento %s reentregado, sin repetir efectos",
                    resultado["event_id"])
    return Response(status_code=status.HTTP_204_NO_CONTENT)
