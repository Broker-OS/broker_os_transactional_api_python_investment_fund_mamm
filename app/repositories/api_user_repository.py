"""Acceso a datos de api_users (consumidores de la API)."""
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.api_user import ROLE_ADMIN, ApiUser


class ApiUserRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get(self, api_user_id: str) -> Optional[ApiUser]:
        r = await self.db.execute(select(ApiUser).where(ApiUser.id == api_user_id))
        return r.scalar_one_or_none()

    async def get_by_email(self, email: str) -> Optional[ApiUser]:
        r = await self.db.execute(select(ApiUser).where(ApiUser.email == email))
        return r.scalar_one_or_none()

    async def list(self) -> list[ApiUser]:
        r = await self.db.execute(select(ApiUser).order_by(ApiUser.created_at.desc()))
        return list(r.scalars().all())

    def add(self, api_user: ApiUser) -> ApiUser:
        self.db.add(api_user)
        return api_user

    async def count_active_admins(self) -> int:
        r = await self.db.execute(
            select(func.count()).select_from(ApiUser)
            .where(ApiUser.role == ROLE_ADMIN, ApiUser.is_active.is_(True))
        )
        return r.scalar_one()

    async def delete(self, api_user: ApiUser) -> None:
        await self.db.delete(api_user)
