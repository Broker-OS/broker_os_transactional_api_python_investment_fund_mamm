"""Acceso a datos de movimientos."""
from datetime import datetime
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.movement import Movement
from app.models.trader import Trader


class MovementRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    def add(self, movement: Movement) -> Movement:
        self.db.add(movement)
        return movement

    async def get_by_id(self, movement_id: str) -> Optional[Movement]:
        r = await self.db.execute(select(Movement).where(Movement.id == movement_id))
        return r.scalar_one_or_none()

    async def get_by_idempotency_key(self, idem: str) -> Optional[Movement]:
        r = await self.db.execute(select(Movement).where(Movement.idempotency_key == idem))
        return r.scalar_one_or_none()

    async def unreconciled_for_trader(self, trader_id: str) -> list[Movement]:
        """Movimientos AMBIGUOUS: resultado desconocido, hay que conciliar."""
        r = await self.db.execute(
            select(Movement).where(
                Movement.trader_id == trader_id,
                Movement.status == "AMBIGUOUS",
            ).order_by(Movement.created_at)
        )
        return list(r.scalars().all())

    async def list_unreconciled(self, *, limit: int = 100,
                               owner_api_user_id: Optional[str] = None) -> list[Movement]:
        stmt = select(Movement).where(Movement.status == "AMBIGUOUS")
        if owner_api_user_id:
            stmt = stmt.join(Trader, Movement.trader_id == Trader.id).where(
                Trader.owner_api_user_id == owner_api_user_id)
        r = await self.db.execute(stmt.order_by(Movement.created_at).limit(limit))
        return list(r.scalars().all())

    async def list(
        self, *, trader_id: Optional[str] = None, owner_api_user_id: Optional[str] = None,
        direction: Optional[str] = None, status: Optional[str] = None,
        date_from: Optional[datetime] = None, date_to: Optional[datetime] = None,
        page: int = 1, limit: int = 20,
    ) -> tuple[list[Movement], int]:
        filters = []
        if trader_id:
            filters.append(Movement.trader_id == trader_id)
        if direction:
            filters.append(Movement.direction == direction)
        if status:
            filters.append(Movement.status == status)
        if date_from:
            filters.append(Movement.created_at >= date_from)
        if date_to:
            filters.append(Movement.created_at <= date_to)

        count_stmt = select(func.count()).select_from(Movement)
        rows_stmt = select(Movement)
        # Scoping por dueño: join a traders (excluye FUNDING, que no tiene trader).
        if owner_api_user_id:
            count_stmt = count_stmt.join(Trader, Movement.trader_id == Trader.id)
            rows_stmt = rows_stmt.join(Trader, Movement.trader_id == Trader.id)
            filters.append(Trader.owner_api_user_id == owner_api_user_id)

        total = (await self.db.execute(count_stmt.where(*filters))).scalar_one()
        rows = (
            await self.db.execute(
                rows_stmt.where(*filters)
                .order_by(Movement.created_at.desc())
                .offset((page - 1) * limit).limit(limit)
            )
        ).scalars().all()
        return list(rows), total
