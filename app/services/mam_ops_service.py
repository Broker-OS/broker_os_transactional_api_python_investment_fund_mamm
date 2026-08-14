"""
Panel de operacion: lo que quedo a medias y necesita una persona.

El sistema esta lleno de operaciones que no terminan en el mismo instante en que
empiezan — bajas que cierran posiciones, fees que se acreditan por periodo,
eventos que llegan antes que la suscripcion que describen. Cada una tiene su
mecanismo de reintento, pero algunas se atascan por causas que ningun cron puede
resolver solo.

Este servicio no arregla nada: junta en un solo lugar todo lo que necesita una
decision humana, para que no haya que acordarse de mirar cinco tablas distintas.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.startup_checks import run_checks
from app.models.mam import (
    ACCOUNT_DELETED,
    ALLOC_ERROR,
    ALLOC_STOPPING,
    MamAccount,
    MamAllocation,
    MamDeletionOperation,
    MamPerfFeePayment,
    MamWebhookEvent,
)
from app.models.movement import Movement


class MamOpsService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def _count(self, model, *filtros) -> int:
        return (await self.db.execute(
            select(func.count()).select_from(model).where(*filtros))).scalar_one()

    async def pending(self) -> dict:
        """Todo lo que espera una intervencion, con la razon de por que espera."""
        movimientos_inciertos = await self._count(
            Movement, Movement.status == "AMBIGUOUS")
        movimientos_colgados = await self._count(
            Movement, Movement.status == "PENDING")
        fees_sin_asentar = await self._count(
            MamPerfFeePayment,
            MamPerfFeePayment.ledger_tx_id.is_(None),
            MamPerfFeePayment.status == "EXECUTED")
        eventos_sin_aplicar = await self._count(
            MamWebhookEvent, MamWebhookEvent.processed_at.is_(None))
        bajas_parciales = await self._count(
            MamDeletionOperation, MamDeletionOperation.status == "PARTIAL")
        bajas_en_curso = await self._count(
            MamDeletionOperation,
            MamDeletionOperation.status.notin_(("COMPLETED", "FAILED", "PARTIAL")))
        suscripciones_cerrando = await self._count(
            MamAllocation, MamAllocation.status == ALLOC_STOPPING)
        suscripciones_en_error = await self._count(
            MamAllocation, MamAllocation.status == ALLOC_ERROR)
        # El alta exige cliente, pero register/import no pueden: traen cuentas
        # que ya existian y cuyo dueño a veces todavia no se sabe. Sin esto la
        # cuenta queda muda hasta que alguien intenta depositarle y no puede.
        cuentas_sin_cliente = await self._count(
            MamAccount,
            MamAccount.trader_id.is_(None),
            MamAccount.status != ACCOUNT_DELETED)

        items = [
            {"key": "ambiguous_movements", "count": movimientos_inciertos,
             "needs_human": True,
             "what": "Movimientos con resultado incierto",
             "why": ("Se agotaron los reintentos idempotentes sin saber si el dinero se "
                     "movio. Hay que conciliar contra MT5 antes de operar esa cuenta.")},
            {"key": "stuck_movements", "count": movimientos_colgados,
             "needs_human": movimientos_colgados > 0,
             "what": "Movimientos que quedaron en curso",
             "why": ("Un movimiento PENDING bloquea nuevas operaciones sobre esa cuenta. "
                     "Si no avanza, el proceso que lo creo murio a mitad de camino.")},
            {"key": "unposted_fees", "count": fees_sin_asentar,
             "needs_human": True,
             "what": "Fees cobrados sin asiento contable",
             "why": ("El motor los cobro pero no se pudieron atribuir a ningun cliente. "
                     "Importar esas cuentas y volver a conciliar los asienta.")},
            {"key": "unprocessed_events", "count": eventos_sin_aplicar,
             "needs_human": False,
             "what": "Avisos de terminacion sin aplicar",
             "why": ("Casi siempre la suscripcion no estaba importada cuando llego el "
                     "aviso. El reintento los resuelve una vez importada.")},
            {"key": "partial_deletions", "count": bajas_parciales,
             "needs_human": True,
             "what": "Bajas que quedaron a medio camino",
             "why": ("Uno o mas cierres de posiciones no terminaron. Hay que corregir la "
                     "causa y reintentar; no avanzan solas.")},
            {"key": "deletions_in_progress", "count": bajas_en_curso,
             "needs_human": False,
             "what": "Bajas en curso",
             "why": "Avanzan solas; el cron las sigue hasta COMPLETED."},
            {"key": "closing_subscriptions", "count": suscripciones_cerrando,
             "needs_human": False,
             "what": "Suscripciones cerrando posiciones",
             "why": "Terminan solas cuando cierran; el cron las sincroniza."},
            {"key": "subscriptions_in_error", "count": suscripciones_en_error,
             "needs_human": True,
             "what": "Suscripciones en error",
             "why": "El motor las marco ERROR: requieren intervencion operativa."},
            {"key": "accounts_without_client", "count": cuentas_sin_cliente,
             "needs_human": True,
             "what": "Cuentas sin cliente asociado",
             "why": ("Llegaron por register o import, que no siempre saben de quien es la "
                     "cuenta. Sin dueño no pueden mover capital ni las ve un USER. "
                     "Asignarlo con PATCH /mam/accounts/{login}.")},
        ]

        requiere_accion = sum(i["count"] for i in items if i["needs_human"])
        return {
            "needs_attention": requiere_accion,
            "total_pending": sum(i["count"] for i in items),
            "items": items,
        }

    async def readiness(self) -> dict:
        """Si el servicio esta en condiciones de operar de verdad.

        Distinto de `/health`, que solo dice que el proceso responde. Aca se
        mira si falta configuracion que recien se nota cuando alguien intenta
        usar una funcion — por ejemplo, sin grupo MT5 el servicio levanta
        perfecto y falla en la primera creacion de cuenta.
        """
        hallazgos = run_checks()
        criticos = [f.code for f in hallazgos if f.severity == "CRITICAL"]
        avisos = [f.code for f in hallazgos if f.severity == "WARNING"]

        capacidades = {
            "create_accounts": bool(
                settings.MAM_MT5_PLATFORM_GROUP
                or (settings.MAM_MT5_GROUP_LEADER and settings.MAM_MT5_GROUP_FOLLOWER)),
            "store_credentials": bool(settings.MT5_CREDENTIALS_ENCRYPTION_KEY),
            # Crear la cuenta no alcanza: sin el nombre del servidor MT5 el
            # cliente recibe login y contrasena pero no a donde conectarse.
            "deliver_credentials": bool(settings.MAM_MT5_SERVER.strip()),
            "receive_webhooks": bool(settings.MAM_WEBHOOK_SIGNING_SECRET),
            "talk_to_engine": bool(settings.MAM_API_BASE_URL and settings.MAM_API_KEY),
            # Depositos on-chain. Las dos hacen falta: con una sola, el endpoint
            # rechaza todo con EVM_NOT_CONFIGURED.
            "verify_crypto_deposits": bool(
                settings.EVM_USDC_CONTRACT.strip()
                and settings.EVM_RECEIVING_ADDRESS.strip()
                and settings.EVM_RPC_URL.strip()),
        }
        pendiente = await self.pending()

        return {
            "ready": not criticos and all(capacidades.values()),
            "capabilities": capacidades,
            "critical": criticos,
            "warnings": avisos,
            "needs_attention": pendiente["needs_attention"],
        }
