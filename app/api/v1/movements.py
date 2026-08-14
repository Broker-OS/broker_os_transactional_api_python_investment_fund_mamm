"""Endpoints de historial de movimientos (depósito/retiro/fondeo).

Acá NO se mueve capital, solo se consulta. El capital se mueve en tres lugares:
`/mam/accounts/{mt5_login}/deposits` y `/withdrawals` (maestra <-> cuenta MT5 del
cliente), `/master-account/funding` (externo -> maestra, con OTP) y
`/crypto-deposits` (USDC on-chain -> maestra). Este historial los cubre a todos.
"""
from datetime import date
from math import ceil
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_api_key
from app.db.database import get_db
from app.models.api_user import ROLE_ADMIN, ApiUser
from app.schemas.common import APIResponse
from app.schemas.movement import MovementListResponse, MovementRead
from app.services.trader_service import TraderService
from app.services.report_service import ReportService

router = APIRouter(tags=["12. Consultas y reportes"])


def _owner_scope(caller: ApiUser) -> str | None:
    """None si es ADMIN (ve todo); su propio id si es USER (solo lo suyo)."""
    return None if caller.role == ROLE_ADMIN else caller.id


def _pages(total: int, limit: int) -> int:
    return ceil(total / limit) if (total and limit) else 0


@router.get("/movements", response_model=APIResponse,
            summary="Historial de movimientos (paginado, con rango de fechas)",
            description=(
                "Más nuevo primero. Filtros: `trader` (external_reference), `direction`, "
                "`status`, `date_from`, `date_to` (YYYY-MM-DD, inclusivos), `page`, `limit`."
            ))
async def list_movements(
    trader: Optional[str] = Query(None, description="external_reference del trader"),
    direction: Optional[str] = Query(None, pattern="^(DEPOSIT|WITHDRAWAL|FUNDING)$"),
    status_filter: Optional[str] = Query(None, alias="status", pattern="^(PENDING|COMPLETED|FAILED|AMBIGUOUS|REJECTED)$"),
    date_from: Optional[date] = Query(None),
    date_to: Optional[date] = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    caller: ApiUser = Depends(require_api_key),
):
    items, total = await ReportService(db).list_movements(
        trader_external_reference=trader, direction=direction, status=status_filter,
        date_from=date_from, date_to=date_to, page=page, limit=limit,
        owner_api_user_id=_owner_scope(caller))
    payload = MovementListResponse(
        total=total, page=page, limit=limit, pages=_pages(total, limit),
        items=[MovementRead.model_validate(m) for m in items])
    return APIResponse(success=True, http_status=200, message="Movimientos obtenidos correctamente",
                       data=payload.model_dump(mode="json"))


@router.get("/movements/{movement_id}", response_model=APIResponse, summary="Detalle de un movimiento")
async def get_movement(movement_id: str, db: AsyncSession = Depends(get_db),
                       caller: ApiUser = Depends(require_api_key)):
    mv = await TraderService(db).get_movement(movement_id=movement_id, caller=caller)
    return APIResponse(success=True, http_status=200, message="Movimiento obtenido correctamente",
                       data=MovementRead.model_validate(mv).model_dump(mode="json"))
