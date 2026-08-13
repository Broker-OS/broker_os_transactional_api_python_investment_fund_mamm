"""Endpoints de clientes (traders): registrar y listar.

El alta es local: el motor MAM no tiene usuarios, solo cuentas MT5 (spec §2). El
cliente empieza a existir del lado del proveedor recien cuando se le crea una
cuenta MAM.
"""
from math import ceil
from typing import Optional

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_api_key
from app.db.database import get_db
from app.models.api_user import ApiUser
from app.schemas.common import APIResponse
from app.schemas.trader import (
    TraderListItem,
    TraderListResponse,
    TraderOwner,
    TraderRead,
    TraderRegisterRequest,
)
from app.services.trader_service import TraderService

router = APIRouter(tags=["3. Clientes"])


@router.post("/traders", response_model=APIResponse, status_code=status.HTTP_201_CREATED,
             summary="Registrar un cliente",
             description=(
                 "Da de alta al cliente en este servicio. **No crea nada en el motor MAM**: "
                 "el proveedor solo conoce cuentas MT5, así que el cliente existe allá "
                 "recién cuando se le crea una cuenta.\n\n"
                 "El **`external_reference` NO se envía**: este servicio asigna un **id "
                 "numérico único de 12 dígitos** y lo devuelve en `data.external_reference` "
                 "— el socio identifica al cliente por ese id en las demás llamadas.\n\n"
                 "`max_active_leaders` fija cuántas estrategias puede seguir a la vez. Ese "
                 "límite **no vive en el motor MAM**: se resuelve acá y se envía en cada "
                 "suscripción."
             ))
async def register_trader(body: TraderRegisterRequest, db: AsyncSession = Depends(get_db),
                          caller: ApiUser = Depends(require_api_key)):
    trader = await TraderService(db).register_trader(
        email=str(body.email), first_name=body.first_name, last_name=body.last_name,
        max_active_leaders=body.max_active_leaders, owner_api_user_id=caller.id)
    return APIResponse(success=True, http_status=201, message="Cliente registrado correctamente",
                       data=TraderRead.model_validate(trader).model_dump(mode="json"))


@router.get("/traders", response_model=APIResponse,
            summary="Listar clientes registrados (con quién los creó)",
            description=(
                "Lista los clientes creados a través de la API (más nuevo primero), con el "
                "`owner` = api_user que lo creó. **ADMIN** ve todos y puede filtrar por creador "
                "con `owner_api_user_id`; **USER** ve solo los suyos. Paginado con `page`/`limit`."
            ))
async def list_traders(
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
    owner_api_user_id: Optional[str] = Query(
        None, description="Solo ADMIN: filtrar por el api_user que creó los clientes."),
    db: AsyncSession = Depends(get_db),
    caller: ApiUser = Depends(require_api_key),
):
    rows, total = await TraderService(db).list_traders(
        caller=caller, page=page, limit=limit, owner_api_user_id=owner_api_user_id)
    items = []
    for trader, owner in rows:
        item = TraderListItem.model_validate(trader)
        if owner is not None:
            item.owner = TraderOwner(id=owner.id, name=owner.name,
                                     email=owner.email, role=owner.role)
        items.append(item)
    payload = TraderListResponse(
        total=total, page=page, limit=limit,
        pages=ceil(total / limit) if (total and limit) else 0, items=items)
    return APIResponse(success=True, http_status=200, message="Clientes obtenidos correctamente",
                       data=payload.model_dump(mode="json"))


@router.get("/traders/{external_reference}", response_model=APIResponse,
            summary="Detalle de un cliente",
            description="Un **USER** que consulta un cliente ajeno recibe `404` (no se revela su existencia).")
async def get_trader(external_reference: str, db: AsyncSession = Depends(get_db),
                     caller: ApiUser = Depends(require_api_key)):
    trader = await TraderService(db).get_trader(external_reference, caller=caller)
    return APIResponse(success=True, http_status=200, message="Cliente obtenido correctamente",
                       data=TraderRead.model_validate(trader).model_dump(mode="json"))
