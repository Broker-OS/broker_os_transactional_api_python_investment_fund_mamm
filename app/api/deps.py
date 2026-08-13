"""Dependencias FastAPI: autenticación por API key (header X-API-Key) + roles.

`require_api_key` resuelve la key → devuelve el `api_user` dueño (con su rol).
`require_admin` exige rol ADMIN. Como FastAPI cachea dependencias por request,
declarar `Depends(require_api_key)` en un endpoint reusa la validación del router.
"""
from typing import Optional

from fastapi import Depends
from fastapi.security import APIKeyHeader
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ApiKeyInvalidError, ApiKeyMissingError, ForbiddenError
from app.core.security import key_prefix_of, verify_api_key
from app.db.database import get_db
from app.models.api_user import ApiUser, ROLE_ADMIN
from app.repositories.api_key_repository import ApiKeyRepository
from app.repositories.api_user_repository import ApiUserRepository

# Esquema de seguridad → Swagger muestra el botón "Authorize" para X-API-Key.
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def require_api_key(
    x_api_key: Optional[str] = Depends(_api_key_header),
    db: AsyncSession = Depends(get_db),
) -> ApiUser:
    if not x_api_key:
        raise ApiKeyMissingError(
            message="Falta el header X-API-Key",
            detail="Enviar la API key en el header X-API-Key",
        )
    repo = ApiKeyRepository(db)
    for candidate in await repo.find_active_by_prefix(key_prefix_of(x_api_key)):
        if verify_api_key(x_api_key, candidate.key_hash):
            await repo.touch_last_used(candidate.id)
            api_user = (
                await ApiUserRepository(db).get(candidate.api_user_id)
                if candidate.api_user_id else None
            )
            if api_user is None or not api_user.is_active:
                raise ApiKeyInvalidError(
                    message="API key sin usuario activo asociado", detail=None)
            return api_user
    raise ApiKeyInvalidError(message="API key inválida o revocada", detail=None)


def require_admin(api_user: ApiUser = Depends(require_api_key)) -> ApiUser:
    if api_user.role != ROLE_ADMIN:
        raise ForbiddenError(
            message="Se requiere rol ADMIN para esta operación",
            detail=f"rol actual={api_user.role}",
        )
    return api_user
