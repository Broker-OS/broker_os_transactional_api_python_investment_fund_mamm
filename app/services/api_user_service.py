"""
Alta y listado de api_users (consumidores de la API) + emisión de su API key.

Solo lo usa un ADMIN. Al crear un api_user se emite una API key que se devuelve
UNA sola vez (en BD solo vive el hash Argon2).
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ApiUserNotFoundError, ConflictError, ValidationError
from app.core.security import generate_api_key
from app.models.api_key import ApiKey
from app.models.api_user import ROLE_ADMIN, ROLES, ApiUser
from app.repositories.api_key_repository import ApiKeyRepository
from app.repositories.api_user_repository import ApiUserRepository
from app.repositories.trader_repository import TraderRepository


class ApiUserService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = ApiUserRepository(db)

    async def create(self, *, email: str, name: str, role: str) -> dict:
        role = (role or "USER").upper()
        if role not in ROLES:
            raise ValidationError(message="Rol inválido", detail=f"role debe ser uno de {ROLES}")
        if await self.repo.get_by_email(email) is not None:
            raise ConflictError(message="Ya existe un api_user con ese email", detail=email)

        api_user = ApiUser(email=email, name=name, role=role)
        self.repo.add(api_user)
        try:
            await self.db.flush()
        except IntegrityError:
            await self.db.rollback()
            raise ConflictError(message="Ya existe un api_user con ese email", detail=email)

        full_key, key_prefix, key_hash = generate_api_key()
        self.db.add(ApiKey(
            api_user_id=api_user.id, name=f"{name} ({role})",
            key_prefix=key_prefix, key_hash=key_hash,
        ))
        user_id = api_user.id
        await self.db.commit()
        return {"api_user": await self.repo.get(user_id), "api_key": full_key}

    async def list(self) -> list[ApiUser]:
        return await self.repo.list()

    async def _get_or_404(self, api_user_id: str) -> ApiUser:
        user = await self.repo.get(api_user_id)
        if user is None:
            raise ApiUserNotFoundError(message="El api_user no existe", detail=api_user_id)
        return user

    async def update(
        self, *, api_user_id: str, name: Optional[str], email: Optional[str],
        role: Optional[str], receives_notifications: Optional[bool] = None
    ) -> ApiUser:
        user = await self._get_or_404(api_user_id)

        if role is not None:
            role = role.upper()
            if role not in ROLES:
                raise ValidationError(message="Rol inválido", detail=f"role debe ser uno de {ROLES}")
            # No dejar el sistema sin ningún ADMIN activo.
            if user.role == ROLE_ADMIN and role != ROLE_ADMIN and await self.repo.count_active_admins() <= 1:
                raise ConflictError(
                    message="No se puede quitar el rol ADMIN al único admin activo", detail=None)
            user.role = role

        if email is not None and email != user.email:
            other = await self.repo.get_by_email(email)
            if other is not None and other.id != user.id:
                raise ConflictError(message="Ya existe un api_user con ese email", detail=email)
            user.email = email

        if name is not None:
            user.name = name

        if receives_notifications is not None:
            user.receives_notifications = receives_notifications

        await self.db.commit()
        return await self.repo.get(api_user_id)

    async def regenerate_key(self, *, api_user_id: str) -> dict:
        user = await self._get_or_404(api_user_id)
        # Revoca las keys activas y emite una nueva (la vieja deja de funcionar).
        await ApiKeyRepository(self.db).deactivate_for_user(api_user_id)
        full_key, key_prefix, key_hash = generate_api_key()
        self.db.add(ApiKey(
            api_user_id=user.id, name=f"{user.name} ({user.role})",
            key_prefix=key_prefix, key_hash=key_hash,
        ))
        await self.db.commit()
        return {"api_user": await self.repo.get(api_user_id), "api_key": full_key}

    async def delete(self, *, api_user_id: str) -> None:
        user = await self._get_or_404(api_user_id)
        traders = await TraderRepository(self.db).count_by_owner(api_user_id)
        if traders > 0:
            raise ConflictError(
                message="No se puede eliminar: el api_user tiene traders asociados",
                detail=f"traders={traders}")
        if user.role == ROLE_ADMIN and await self.repo.count_active_admins() <= 1:
            raise ConflictError(message="No se puede eliminar el único api_user ADMIN activo", detail=None)
        await ApiKeyRepository(self.db).delete_for_user(api_user_id)
        await self.repo.delete(user)
        await self.db.commit()
