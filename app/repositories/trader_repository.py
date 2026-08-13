"""Acceso a datos de traders."""
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.api_user import ApiUser
from app.models.trader import Trader


class TraderRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_by_id(self, trader_id: str) -> Optional[Trader]:
        r = await self.db.execute(select(Trader).where(Trader.id == trader_id))
        return r.scalar_one_or_none()

    async def get_by_external_reference(self, external_reference: str) -> Optional[Trader]:
        r = await self.db.execute(
            select(Trader).where(
                Trader.external_reference == external_reference,
                Trader.is_deleted.is_(False),
            )
        )
        return r.scalar_one_or_none()

    async def external_reference_exists(self, external_reference: str) -> bool:
        r = await self.db.execute(
            select(Trader.id).where(Trader.external_reference == external_reference)
        )
        return r.scalar_one_or_none() is not None

    async def list_with_owner(
        self, *, owner_api_user_id: Optional[str] = None, page: int = 1, limit: int = 10
    ) -> tuple[list[tuple[Trader, Optional[ApiUser]]], int]:
        """Lista traders (más nuevo primero) con su api_user dueño (quién lo creó)."""
        filters = [Trader.is_deleted.is_(False)]
        if owner_api_user_id:
            filters.append(Trader.owner_api_user_id == owner_api_user_id)
        total = (
            await self.db.execute(select(func.count()).select_from(Trader).where(*filters))
        ).scalar_one()
        rows = (
            await self.db.execute(
                select(Trader, ApiUser)
                .join(ApiUser, Trader.owner_api_user_id == ApiUser.id, isouter=True)
                .where(*filters)
                .order_by(Trader.created_at.desc())
                .offset((page - 1) * limit).limit(limit)
            )
        ).all()
        return [(r[0], r[1]) for r in rows], total

    async def count_by_owner(self, api_user_id: str) -> int:
        r = await self.db.execute(
            select(func.count()).select_from(Trader)
            .where(Trader.owner_api_user_id == api_user_id, Trader.is_deleted.is_(False))
        )
        return r.scalar_one()

    def add(self, trader: Trader) -> Trader:
        self.db.add(trader)
        return trader
