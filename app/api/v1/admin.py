"""Endpoints de administración: gestión de api_users (solo ADMIN)."""
from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.database import get_db
from app.schemas.api_user import (
    ApiUserCreatedResponse,
    ApiUserCreateRequest,
    ApiUserListResponse,
    ApiUserRead,
    ApiUserUpdateRequest,
)
from app.schemas.common import APIResponse
from app.services.api_user_service import ApiUserService

router = APIRouter(tags=["1. Configuración · Administradores y claves"])


@router.post("/admin/api-users", response_model=APIResponse, status_code=status.HTTP_201_CREATED,
             summary="Crear un api_user y emitir su API key (solo ADMIN)",
             description=(
                 "Crea un consumidor de la API con rol **ADMIN** o **USER** y devuelve su "
                 "API key **una sola vez** (en BD solo queda el hash). Un USER solo podrá "
                 "operar sus propios traders; un ADMIN accede a todo."
             ))
async def create_api_user(body: ApiUserCreateRequest, db: AsyncSession = Depends(get_db)):
    data = await ApiUserService(db).create(email=str(body.email), name=body.name, role=body.role)
    payload = ApiUserCreatedResponse(
        api_user=ApiUserRead.model_validate(data["api_user"]), api_key=data["api_key"])
    return APIResponse(
        success=True, http_status=201,
        message="api_user creado. Guardá la API key: se muestra una sola vez.",
        data=payload.model_dump(mode="json"))


@router.get("/admin/api-users", response_model=APIResponse,
            summary="Listar api_users (solo ADMIN)",
            description="Lista los consumidores de la API con su rol y estado.")
async def list_api_users(db: AsyncSession = Depends(get_db)):
    users = await ApiUserService(db).list()
    payload = ApiUserListResponse(total=len(users), items=[ApiUserRead.model_validate(u) for u in users])
    return APIResponse(success=True, http_status=200, message="api_users obtenidos correctamente",
                       data=payload.model_dump(mode="json"))


@router.patch("/admin/api-users/{api_user_id}", response_model=APIResponse,
              summary="Editar un api_user (nombre/email/rol/avisos) — la key NO cambia",
              description=(
                  "Actualiza `name`, `email`, `role` y/o `receives_notifications`. "
                  "**No toca la API key.** No permite quitar el rol ADMIN al último admin "
                  "activo (`409`). Email duplicado → `409`.\n\n"
                  "`receives_notifications=false` silencia los avisos por email dirigidos a "
                  "los ADMIN. Es para las **cuentas de servicio**: el usuario de los cron "
                  "jobs necesita rol ADMIN para operar, pero nadie lee su casilla."
              ))
async def update_api_user(api_user_id: str, body: ApiUserUpdateRequest,
                          db: AsyncSession = Depends(get_db)):
    user = await ApiUserService(db).update(
        api_user_id=api_user_id, name=body.name,
        email=str(body.email) if body.email else None, role=body.role,
        receives_notifications=body.receives_notifications)
    return APIResponse(success=True, http_status=200, message="api_user actualizado correctamente",
                       data=ApiUserRead.model_validate(user).model_dump(mode="json"))


@router.post("/admin/api-users/{api_user_id}/regenerate-key", response_model=APIResponse,
             summary="Regenerar la API key de un api_user (solo ADMIN)",
             description=(
                 "Revoca la(s) key(s) activa(s) del api_user y emite una **nueva**, que se "
                 "devuelve **una sola vez**. La key anterior deja de funcionar de inmediato."
             ))
async def regenerate_api_user_key(api_user_id: str, db: AsyncSession = Depends(get_db)):
    data = await ApiUserService(db).regenerate_key(api_user_id=api_user_id)
    payload = ApiUserCreatedResponse(
        api_user=ApiUserRead.model_validate(data["api_user"]), api_key=data["api_key"])
    return APIResponse(success=True, http_status=200,
                       message="API key regenerada. Guardá la nueva: se muestra una sola vez.",
                       data=payload.model_dump(mode="json"))


@router.delete("/admin/api-users/{api_user_id}", response_model=APIResponse,
               summary="Eliminar un api_user (solo ADMIN)",
               description=(
                   "Borra el api_user y sus API keys. **Bloqueado** si tiene traders "
                   "asociados o si es el único ADMIN activo (`409`)."
               ))
async def delete_api_user(api_user_id: str, db: AsyncSession = Depends(get_db)):
    await ApiUserService(db).delete(api_user_id=api_user_id)
    return APIResponse(success=True, http_status=200, message="api_user eliminado correctamente",
                       data={"id": api_user_id, "deleted": True})
