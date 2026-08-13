"""
Receptor del webhook de terminacion de allocations (spec §11.6).

El motor avisa cuando una relacion termina, por `USER_UNSUBSCRIBE` o por
`EQUITY_STOP`. El segundo es el que justifica todo esto: el motor cierra la
suscripcion por su cuenta cuando el equity del cliente toca el piso, y sin
webhook nos enteramos recien en el proximo sondeo — mientras tanto el sistema
muestra como viva una relacion que ya no existe.

Recibir el evento NO inicia ni autoriza la desuscripcion: informa una
terminacion YA procesada del otro lado.

VERIFICACION DE FIRMA — el orden de los pasos no es negociable:

  1. Sobre los BYTES EXACTOS del body, antes de deserializar. Volver a
     serializar el JSON cambia espacios y orden de claves, y la firma deja de
     validar aunque el contenido sea el mismo.
  2. Comparacion en tiempo constante. Un `==` sobre strings corta en el primer
     byte distinto, y esa diferencia de microsegundos alcanza para adivinar la
     firma byte por byte.
  3. Ventana de cinco minutos sobre el timestamp firmado. Sin eso, una captura
     valida se puede reproducir para siempre.

PERSISTIR PRIMERO, PROCESAR DESPUES. La entrega se puede repetir, asi que el
evento se guarda con su `event_id` (UNIQUE en la BD) y se responde 2xx enseguida.
Si el procesamiento falla, el evento ya esta guardado y se puede reintentar sin
pedirle nada al proveedor.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import (
    WebhookSecretMissingError,
    WebhookSignatureInvalidError,
    WebhookTimestampSkewError,
)
from app.models._helpers import now_utc
from app.models.mam import ALLOC_CANCELLED, MamWebhookEvent
from app.repositories.mam_repository import MamRepository

logger = logging.getLogger(__name__)

EVENT_TERMINATED = "mam.allocation.terminated"
_PREFIX = "sha256="


class MamWebhookService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = MamRepository(db)

    # ══════════════════════════════════════════════════════════════════
    # Firma
    # ══════════════════════════════════════════════════════════════════

    @staticmethod
    def verify_signature(*, raw_body: bytes, timestamp: Optional[str],
                         signature: Optional[str]) -> None:
        """Valida X-MAM-Signature. Levanta si no cierra; no devuelve nada."""
        secret = (settings.MAM_WEBHOOK_SIGNING_SECRET or "").strip()
        if not secret:
            # Falla cerrado: sin secreto no se puede distinguir un evento del
            # proveedor de uno que mando cualquiera a una URL publica.
            raise WebhookSecretMissingError(
                message="El webhook no esta configurado: no se puede verificar la firma",
                detail="Definir MAM_WEBHOOK_SIGNING_SECRET en .env")
        if not signature or not timestamp:
            raise WebhookSignatureInvalidError(
                message="Faltan los encabezados de firma del webhook",
                detail="Se esperan X-MAM-Signature y X-MAM-Timestamp")

        try:
            enviado = int(str(timestamp).strip())
        except (TypeError, ValueError):
            raise WebhookTimestampSkewError(
                message="El timestamp del webhook no es valido",
                detail=f"X-MAM-Timestamp={timestamp!r}") from None

        desfase = abs(int(time.time()) - enviado)
        if desfase > settings.MAM_WEBHOOK_MAX_SKEW_SECONDS:
            raise WebhookTimestampSkewError(
                message="El evento esta fuera de la ventana de tiempo aceptada",
                detail=(f"desfase={desfase}s, maximo={settings.MAM_WEBHOOK_MAX_SKEW_SECONDS}s. "
                        f"Un evento viejo puede ser una reproduccion."))

        # Los bytes crudos, no el JSON re-serializado.
        firmado = str(enviado).encode() + b"." + raw_body
        esperado = _PREFIX + hmac.new(
            secret.encode(), firmado, hashlib.sha256).hexdigest()

        if not hmac.compare_digest(esperado, signature.strip()):
            raise WebhookSignatureInvalidError(
                message="La firma del webhook no valida",
                detail="El cuerpo no coincide con la firma, o el secreto es otro")

    # ══════════════════════════════════════════════════════════════════
    # Recepcion
    # ══════════════════════════════════════════════════════════════════

    async def receive(self, *, raw_body: bytes, event_id: Optional[str],
                      event_type: Optional[str], signature_verified: bool = True) -> dict:
        """Persiste el evento y lo aplica. Idempotente por `event_id`."""
        try:
            payload = json.loads(raw_body or b"{}")
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}

        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        eid = (event_id or payload.get("event_id") or "").strip()
        if not eid:
            # Sin id no se puede deduplicar; se rechaza en vez de arriesgar un
            # doble efecto en cada reentrega.
            raise WebhookSignatureInvalidError(
                message="El evento no trae identificador",
                detail="Se espera X-MAM-Event-Id o event_id en el cuerpo")

        ya = (await self.db.execute(
            select(MamWebhookEvent).where(MamWebhookEvent.event_id == eid))
        ).scalar_one_or_none()
        if ya is not None:
            # Reentrega: 2xx sin repetir efectos (spec §11.6).
            return {"event_id": eid, "duplicate": True,
                    "processed": ya.processed_at is not None}

        evento = MamWebhookEvent(
            event_id=eid,
            event_type=(event_type or payload.get("type") or EVENT_TERMINATED)[:60],
            version=_int(payload.get("version")),
            occurred_at=_dt(payload.get("occurred_at")),
            allocation_id=_int(data.get("allocation_id")),
            reason=_str(data.get("reason"), 40),
            triggered_by=_str(data.get("triggered_by"), 20),
            allocation_status=_str(data.get("status"), 20),
            leader_login=_str(data.get("leader_login"), 40),
            follower_login=_str(data.get("follower_login"), 40),
            performance_fee_charged=_dec(data.get("performance_fee_charged")),
            payload=payload,
            signature_verified=signature_verified,
        )
        self.repo.add(evento)
        await self.db.commit()

        aplicado = await self._apply(evento)
        await self.db.commit()
        return {"event_id": eid, "duplicate": False, "processed": aplicado}

    async def _apply(self, evento: MamWebhookEvent) -> bool:
        """Lleva la terminacion al estado local. Devuelve si se pudo aplicar."""
        if evento.event_type != EVENT_TERMINATED or evento.allocation_id is None:
            evento.processed_at = now_utc()
            evento.process_error = None if evento.allocation_id else "evento sin allocation_id"
            return evento.allocation_id is not None

        alloc = await self.repo.get_allocation_by_provider_id(evento.allocation_id)
        if alloc is None:
            # La relacion no esta importada. El evento queda guardado y sin
            # procesar para que un reintento lo tome cuando la importen; perderlo
            # significaria no enterarse nunca de esa terminacion.
            evento.process_error = "la suscripcion no esta en esta base"
            logger.warning(
                "MAM webhook: allocation %s desconocida; el evento %s queda pendiente",
                evento.allocation_id, evento.event_id)
            return False

        alloc.status = evento.allocation_status or ALLOC_CANCELLED
        alloc.terminated_reason = evento.reason
        alloc.terminated_by = evento.triggered_by
        if evento.performance_fee_charged is not None:
            alloc.performance_fee_charged = evento.performance_fee_charged
        if alloc.status == ALLOC_CANCELLED and alloc.ended_at is None:
            alloc.ended_at = _dt(
                (evento.payload or {}).get("data", {}).get("terminated_at")) or now_utc()

        evento.processed_at = now_utc()
        evento.process_error = None
        logger.info("MAM webhook: allocation %s -> %s por %s",
                    evento.allocation_id, alloc.status, evento.reason)
        return True

    # ══════════════════════════════════════════════════════════════════
    # Reintento y consulta
    # ══════════════════════════════════════════════════════════════════

    async def retry_pending(self, *, limit: int = 50) -> dict:
        """Vuelve a aplicar los eventos que quedaron sin procesar.

        Casi siempre son terminaciones de suscripciones que todavia no estaban
        importadas. Una vez importadas, esta pasada las resuelve.
        """
        pendientes = (await self.db.execute(
            select(MamWebhookEvent)
            .where(MamWebhookEvent.processed_at.is_(None))
            .order_by(MamWebhookEvent.received_at)
            .limit(limit))).scalars().all()

        resueltos = 0
        for evento in pendientes:
            if await self._apply(evento):
                resueltos += 1
        await self.db.commit()
        return {"reviewed": len(pendientes), "processed": resueltos}

    async def list_events(self, *, only_pending: bool = False,
                          page: int = 1, limit: int = 50):
        from sqlalchemy import func

        filtros = [MamWebhookEvent.processed_at.is_(None)] if only_pending else []
        total = (await self.db.execute(
            select(func.count()).select_from(MamWebhookEvent).where(*filtros))).scalar_one()
        filas = (await self.db.execute(
            select(MamWebhookEvent).where(*filtros)
            .order_by(MamWebhookEvent.received_at.desc())
            .offset((page - 1) * limit).limit(limit))).scalars().all()
        return list(filas), total


def _int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _str(value: Any, limit: int) -> Optional[str]:
    return None if value is None else str(value)[:limit]


def _dec(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None


def _dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
