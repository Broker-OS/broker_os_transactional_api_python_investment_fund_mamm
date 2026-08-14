r"""
Baja de cuentas del servicio MAM (spec §11.1).

NO ES UN DELETE. Una cuenta puede arrastrar suscripciones vivas y posiciones
copiadas abiertas, asi que el motor lo resuelve con una operacion ASINCRONICA
que hay que consultar hasta el final y que puede quedar a medio camino.

    PENDING -> WAITING_CLOSE -> PURGING -> COMPLETED
                            \-> PARTIAL  (corregir la causa y reintentar)
                            \-> FAILED

Tres reglas que el flujo hace cumplir:

  1. NO se crea la operacion sin haber consultado el impacto. El impacto dice
     que suscripciones y posiciones se van a ver afectadas; crear a ciegas puede
     cerrar posiciones de un cliente que no esperaba perderlas.

  2. NO se archiva la cuenta de nuestro lado hasta que el motor confirme
     COMPLETED. Marcarla antes deja una cuenta que aca figura muerta y alla
     sigue copiando operaciones.

  3. Esto elimina la cuenta del SERVICIO MAM, no el usuario del servidor MT5.
     Darlo de baja en MT5 es un procedimiento administrativo del broker.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    DeletionNotEligibleError,
    DeletionOperationNotFoundError,
)
from app.models._helpers import now_utc
from app.models.mam import ACCOUNT_DELETED, MamDeletionOperation
from app.repositories.mam_repository import MamRepository
from app.services.mam_account_service import MamAccountService
from app.services.mam_client import get_mam_client

logger = logging.getLogger(__name__)

KIND_MASTER = "MASTER"
KIND_INVESTOR = "INVESTOR"
# Estados en los que la operacion ya no avanza sola.
_TERMINAL = ("COMPLETED", "FAILED")


class MamDeletionService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = MamRepository(db)
        self.accounts = MamAccountService(db)
        self._client = get_mam_client()

    # ══════════════════════════════════════════════════════════════════
    # Impacto
    # ══════════════════════════════════════════════════════════════════

    async def impact(self, *, mt5_login: str, caller=None) -> dict:
        """Analiza SIN modificar nada.

        La cuenta decide sola por que flujo va: si tiene perfil de estrategia,
        el borrado es de master y puede arrastrar a sus seguidores. Pedirle al
        integrador que elija el flujo correcto seria trasladarle una decision
        que el sistema puede tomar mirando sus propios datos — y equivocarse
        ahi significa consultar el impacto equivocado.
        """
        acc = await self.accounts.get_account(mt5_login, caller=caller)
        perfil = await self.repo.get_profile_by_account_id(acc.id)

        if perfil is not None:
            data = await self._client.master_deletion_impact(master_login=acc.mt5_login)
            data["target_kind"] = KIND_MASTER
        else:
            data = await self._client.investor_deletion_impact(investor_login=acc.mt5_login)
            data["target_kind"] = KIND_INVESTOR
        data.setdefault("target_login", acc.mt5_login)
        return data

    # ══════════════════════════════════════════════════════════════════
    # Alta de la operacion
    # ══════════════════════════════════════════════════════════════════

    async def request(
        self, *, mt5_login: str, scope: Optional[str] = None,
        investor_logins: Optional[list[str]] = None,
        transmitted_positions_policy: str = "CLOSE_TRANSMITTED",
        idempotency_key: Optional[str] = None, force: bool = False, caller=None,
    ) -> MamDeletionOperation:
        """Crea la operacion de borrado, previa consulta de impacto.

        `force` solo saltea el corte por conflictos del impacto — nunca la
        consulta en si: la foto del impacto queda guardada como evidencia de que
        se sabia lo que se estaba haciendo.
        """
        acc = await self.accounts.get_account(mt5_login, caller=caller)
        perfil = await self.repo.get_profile_by_account_id(acc.id)
        kind = KIND_MASTER if perfil is not None else KIND_INVESTOR

        foto = await self.impact(mt5_login=acc.mt5_login, caller=caller)
        conflictos = foto.get("conflicts") or []
        if conflictos and not force:
            raise DeletionNotEligibleError(
                message="La cuenta no se puede eliminar todavia",
                detail=(f"conflictos={conflictos}. Resolverlos, o repetir con force=true "
                        f"si ya se evaluaron."))

        idem = idempotency_key or f"delete-{acc.mt5_login}-{uuid.uuid4().hex[:12]}"
        ya = (await self.db.execute(
            select(MamDeletionOperation).where(
                MamDeletionOperation.idempotency_key == idem))).scalar_one_or_none()
        if ya is not None:
            return ya

        op = MamDeletionOperation(
            target_kind=kind, target_login=acc.mt5_login,
            scope=(scope or "MASTER_ACCOUNT_ONLY") if kind == KIND_MASTER else None,
            investor_logins={"logins": investor_logins or []} if investor_logins else None,
            transmitted_positions_policy=transmitted_positions_policy,
            idempotency_key=idem,
            requested_by=getattr(caller, "email", None),
            requested_by_api_user_id=getattr(caller, "id", None),
            impact_snapshot=foto,
            status="PENDING",
        )
        self.repo.add(op)
        await self.db.commit()

        try:
            if kind == KIND_MASTER:
                data = await self._client.create_master_deletion(
                    master_login=acc.mt5_login, idempotency_key=idem,
                    scope=op.scope, investor_logins=investor_logins,
                    transmitted_positions_policy=transmitted_positions_policy,
                    requested_by=op.requested_by)
            else:
                data = await self._client.create_investor_deletion(
                    investor_login=acc.mt5_login, idempotency_key=idem,
                    transmitted_positions_policy=transmitted_positions_policy,
                    requested_by=op.requested_by)
        except Exception as exc:  # noqa: BLE001
            op.status = "FAILED"
            op.error_message = f"{type(exc).__name__}: {str(exc)[:400]}"
            await self.db.commit()
            raise

        op.operation_id = _str(data.get("operation_id") or data.get("id"), 64)
        op.status = _str(data.get("status"), 20) or "PENDING"
        op.error_message = _str(data.get("error_message"), 500)
        await self.db.commit()
        await self.db.refresh(op)
        logger.info("MAM: baja de %s (%s) creada -> operation_id=%s status=%s",
                    acc.mt5_login, kind, op.operation_id, op.status)
        return op

    # ══════════════════════════════════════════════════════════════════
    # Seguimiento
    # ══════════════════════════════════════════════════════════════════

    async def refresh(self, op: MamDeletionOperation) -> MamDeletionOperation:
        """Consulta el estado en el motor y lo aplica localmente."""
        if op.operation_id is None or op.status in _TERMINAL:
            return op

        if op.target_kind == KIND_MASTER:
            data = await self._client.get_master_deletion(operation_id=op.operation_id)
        else:
            data = await self._client.get_investor_deletion(operation_id=op.operation_id)

        nuevo = _str(data.get("status"), 20) or op.status
        if nuevo != op.status:
            logger.info("MAM: baja %s %s -> %s", op.operation_id, op.status, nuevo)
        op.status = nuevo
        op.error_message = _str(data.get("error_message"), 500)

        if op.status == "COMPLETED" and op.completed_at is None:
            op.completed_at = now_utc()
            # Recien ahora se marca la cuenta de nuestro lado. Hacerlo antes
            # dejaria una cuenta que aca figura muerta y alla sigue copiando.
            cuenta = await self.repo.get_account_by_login(op.target_login)
            if cuenta is not None:
                cuenta.status = ACCOUNT_DELETED
        return op

    async def get(self, operation_id: str, *, caller=None) -> MamDeletionOperation:
        op = (await self.db.execute(
            select(MamDeletionOperation).where(
                MamDeletionOperation.operation_id == operation_id))).scalar_one_or_none()
        if op is None:
            op = (await self.db.execute(
                select(MamDeletionOperation).where(
                    MamDeletionOperation.id == operation_id))).scalar_one_or_none()
        if op is None:
            raise DeletionOperationNotFoundError(
                message="La operacion de baja no existe",
                detail=f"operation_id={operation_id}")
        await self.refresh(op)
        await self.db.commit()
        await self.db.refresh(op)
        return op

    async def retry(self, operation_id: str, *, caller=None) -> MamDeletionOperation:
        """Reintenta una operacion que quedo PARTIAL.

        Solo aplica a PARTIAL: es el estado que la spec define como recuperable
        —uno o mas cierres no terminaron— despues de corregir la causa. Sobre
        FAILED o COMPLETED no hay nada que reintentar.
        """
        op = await self.get(operation_id, caller=caller)
        if op.status != "PARTIAL":
            raise DeletionNotEligibleError(
                message="Solo se puede reintentar una operacion que quedo PARTIAL",
                detail=f"status={op.status}")

        if op.target_kind == KIND_MASTER:
            data = await self._client.retry_master_deletion(operation_id=op.operation_id)
        else:
            data = await self._client.retry_investor_deletion(operation_id=op.operation_id)
        op.status = _str(data.get("status"), 20) or op.status
        op.error_message = _str(data.get("error_message"), 500)
        await self.db.commit()
        await self.db.refresh(op)
        return op

    async def sync(self, *, limit: int = 50) -> dict:
        """Refresca las bajas que siguen en curso. Pensado para el cron."""
        pendientes = (await self.db.execute(
            select(MamDeletionOperation)
            .where(MamDeletionOperation.status.notin_(_TERMINAL))
            .order_by(MamDeletionOperation.created_at)
            .limit(limit))).scalars().all()

        cambiadas = 0
        for op in pendientes:
            antes = op.status
            try:
                await self.refresh(op)
            except Exception as exc:  # noqa: BLE001
                logger.warning("MAM: no se pudo consultar la baja %s (%s)",
                               op.operation_id, type(exc).__name__)
                continue
            if op.status != antes:
                cambiadas += 1
        await self.db.commit()
        return {"reviewed": len(pendientes), "changed": cambiadas}

    async def list_operations(self, *, status: Optional[str] = None,
                              page: int = 1, limit: int = 50):
        filtros = [MamDeletionOperation.status == status] if status else []
        total = (await self.db.execute(
            select(func.count()).select_from(MamDeletionOperation).where(*filtros))).scalar_one()
        filas = (await self.db.execute(
            select(MamDeletionOperation).where(*filtros)
            .order_by(MamDeletionOperation.created_at.desc())
            .offset((page - 1) * limit).limit(limit))).scalars().all()
        return list(filas), total


def _str(value: Any, limit: int) -> Optional[str]:
    return None if value is None else str(value)[:limit]
