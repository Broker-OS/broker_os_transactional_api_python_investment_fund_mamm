"""Acceso a datos de API keys."""
from datetime import datetime, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.api_key import ApiKey


class ApiKeyRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def find_active_by_prefix(self, prefix: str) -> list[ApiKey]:
        rows = await self.db.execute(
            select(ApiKey).where(ApiKey.key_prefix == prefix, ApiKey.is_active.is_(True))
        )
        return list(rows.scalars().all())

    async def touch_last_used(self, api_key_id: str) -> None:
        await self.db.execute(
            update(ApiKey).where(ApiKey.id == api_key_id)
            .values(last_used_at=datetime.now(timezone.utc))
        )
        await self.db.commit()

    async def deactivate_for_user(self, api_user_id: str) -> None:
        """Desactiva las keys activas del api_user (no commitea: lo hace el service)."""
        await self.db.execute(
            update(ApiKey).where(ApiKey.api_user_id == api_user_id, ApiKey.is_active.is_(True))
            .values(is_active=False)
        )

    async def delete_for_user(self, api_user_id: str) -> None:
        """Borra todas las keys del api_user (no commitea: lo hace el service)."""
        await self.db.execute(delete(ApiKey).where(ApiKey.api_user_id == api_user_id))
