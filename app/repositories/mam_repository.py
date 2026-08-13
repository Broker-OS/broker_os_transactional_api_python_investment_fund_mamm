"""Acceso a datos del motor MAM: cuentas, perfiles de leader y allocations."""
from typing import Optional

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.mam import (
    ACCOUNT_ACTIVE,
    ALLOC_LIVE_STATES,
    MamAccount,
    MamAllocation,
    MamLeaderProfile,
)
from app.models.trader import Trader


class MamRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    def add(self, obj):
        self.db.add(obj)
        return obj

    # ══════════════════════════════════════════════════════════════════
    # Cuentas
    # ══════════════════════════════════════════════════════════════════

    async def get_account_by_login(self, mt5_login: str) -> Optional[MamAccount]:
        r = await self.db.execute(
            select(MamAccount).where(MamAccount.mt5_login == str(mt5_login)))
        return r.scalar_one_or_none()

    async def get_account_by_id(self, account_id: str) -> Optional[MamAccount]:
        r = await self.db.execute(select(MamAccount).where(MamAccount.id == account_id))
        return r.scalar_one_or_none()

    async def login_is_taken(self, mt5_login: str) -> bool:
        """Un login ya registrado, aunque sea como cuenta PAYMENT de un leader.

        Registrar dos veces el mismo login dejaria dos filas apuntando a la misma
        cuenta MT5, y cualquier operacion posterior tomaria una de las dos al azar.
        """
        login = str(mt5_login)
        r = await self.db.execute(
            select(func.count()).select_from(MamAccount).where(MamAccount.mt5_login == login))
        if r.scalar_one() > 0:
            return True
        r = await self.db.execute(
            select(func.count()).select_from(MamLeaderProfile).where(
                MamLeaderProfile.payment_account_login == login))
        return r.scalar_one() > 0

    async def is_payment_account(self, mt5_login: str) -> bool:
        """La cuenta PAYMENT solo recibe fees: no se le deposita ni opera (spec §10.1)."""
        r = await self.db.execute(
            select(func.count()).select_from(MamLeaderProfile).where(
                MamLeaderProfile.payment_account_login == str(mt5_login)))
        return r.scalar_one() > 0

    async def list_accounts(
        self, *, trader_id: Optional[str] = None, owner_api_user_id: Optional[str] = None,
        can_be_leader: Optional[bool] = None, can_be_follower: Optional[bool] = None,
        status: Optional[str] = None, page: int = 1, limit: int = 20,
    ) -> tuple[list[MamAccount], int]:
        filters = []
        if trader_id:
            filters.append(MamAccount.trader_id == trader_id)
        if can_be_leader is not None:
            filters.append(MamAccount.can_be_leader.is_(can_be_leader))
        if can_be_follower is not None:
            filters.append(MamAccount.can_be_follower.is_(can_be_follower))
        if status:
            filters.append(MamAccount.status == status)

        count_stmt = select(func.count()).select_from(MamAccount)
        rows_stmt = select(MamAccount)
        # Scoping por dueño: un USER solo ve las cuentas de SUS clientes. Las
        # cuentas sin trader (estrategias propias del broker) quedan fuera.
        if owner_api_user_id:
            count_stmt = count_stmt.join(Trader, MamAccount.trader_id == Trader.id)
            rows_stmt = rows_stmt.join(Trader, MamAccount.trader_id == Trader.id)
            filters.append(Trader.owner_api_user_id == owner_api_user_id)

        total = (await self.db.execute(count_stmt.where(*filters))).scalar_one()
        rows = (await self.db.execute(
            rows_stmt.where(*filters)
            .order_by(MamAccount.created_at.desc())
            .offset((page - 1) * limit).limit(limit)
        )).scalars().all()
        return list(rows), total

    async def active_accounts_for_trader(self, trader_id: str) -> list[MamAccount]:
        r = await self.db.execute(
            select(MamAccount).where(
                MamAccount.trader_id == trader_id,
                MamAccount.status == ACCOUNT_ACTIVE,
            ).order_by(MamAccount.created_at)
        )
        return list(r.scalars().all())

    # ══════════════════════════════════════════════════════════════════
    # Perfiles de leader
    # ══════════════════════════════════════════════════════════════════

    async def get_profile_by_account_id(self, account_id: str) -> Optional[MamLeaderProfile]:
        r = await self.db.execute(
            select(MamLeaderProfile).where(MamLeaderProfile.account_id == account_id))
        return r.scalar_one_or_none()

    async def get_profile_by_login(self, account_login: str) -> Optional[MamLeaderProfile]:
        r = await self.db.execute(
            select(MamLeaderProfile).where(
                MamLeaderProfile.account_login == str(account_login)))
        return r.scalar_one_or_none()

    async def get_profile_by_leader_id(self, leader_id: int) -> Optional[MamLeaderProfile]:
        r = await self.db.execute(
            select(MamLeaderProfile).where(MamLeaderProfile.leader_id == leader_id))
        return r.scalar_one_or_none()

    async def payment_login_is_taken(self, payment_login: str) -> bool:
        r = await self.db.execute(
            select(func.count()).select_from(MamLeaderProfile).where(
                MamLeaderProfile.payment_account_login == str(payment_login)))
        return r.scalar_one() > 0

    async def list_profiles(
        self, *, status: Optional[str] = None, page: int = 1, limit: int = 20,
    ) -> tuple[list[MamLeaderProfile], int]:
        filters = [MamLeaderProfile.status == status] if status else []
        total = (await self.db.execute(
            select(func.count()).select_from(MamLeaderProfile).where(*filters))).scalar_one()
        rows = (await self.db.execute(
            select(MamLeaderProfile).where(*filters)
            .order_by(MamLeaderProfile.created_at.desc())
            .offset((page - 1) * limit).limit(limit)
        )).scalars().all()
        return list(rows), total

    # ══════════════════════════════════════════════════════════════════
    # Allocations
    # ══════════════════════════════════════════════════════════════════

    async def get_allocation_by_provider_id(self, allocation_id: int) -> Optional[MamAllocation]:
        r = await self.db.execute(
            select(MamAllocation).where(MamAllocation.allocation_id == allocation_id))
        return r.scalar_one_or_none()

    async def live_allocation_for_pair(
        self, *, leader_login: str, follower_login: str,
    ) -> Optional[MamAllocation]:
        """Spec §8: solo puede existir una allocation viva por pareja."""
        r = await self.db.execute(
            select(MamAllocation).where(
                MamAllocation.leader_login == str(leader_login),
                MamAllocation.follower_login == str(follower_login),
                MamAllocation.status.in_(ALLOC_LIVE_STATES),
            )
        )
        return r.scalar_one_or_none()

    async def count_live_allocations_for_follower(self, follower_login: str) -> int:
        """Cupo consumido del plan del cliente (spec §7.1).

        Cuenta ACTIVE, PAUSED y STOPPING: los tres estados que el motor considera
        vivos al validar el limite.
        """
        r = await self.db.execute(
            select(func.count()).select_from(MamAllocation).where(
                MamAllocation.follower_login == str(follower_login),
                MamAllocation.status.in_(ALLOC_LIVE_STATES),
            )
        )
        return r.scalar_one()

    async def list_allocations(
        self, *, leader_login: Optional[str] = None, follower_login: Optional[str] = None,
        status: Optional[str] = None, owner_api_user_id: Optional[str] = None,
        page: int = 1, limit: int = 20,
    ) -> tuple[list[MamAllocation], int]:
        filters = []
        if leader_login:
            filters.append(MamAllocation.leader_login == str(leader_login))
        if follower_login:
            filters.append(MamAllocation.follower_login == str(follower_login))
        if status:
            filters.append(MamAllocation.status == status)

        count_stmt = select(func.count()).select_from(MamAllocation)
        rows_stmt = select(MamAllocation)
        if owner_api_user_id:
            # Una allocation es visible si el cliente es dueño de CUALQUIERA de
            # las dos puntas: puede estar siguiendo una estrategia ajena, o ser
            # la estrategia que otros siguen.
            leader = MamAccount.__table__.alias("leader_acc")
            follower = MamAccount.__table__.alias("follower_acc")
            sub = select(Trader.id).where(Trader.owner_api_user_id == owner_api_user_id)
            cond = or_(
                MamAllocation.leader_account_id.in_(
                    select(leader.c.id).where(leader.c.trader_id.in_(sub))),
                MamAllocation.follower_account_id.in_(
                    select(follower.c.id).where(follower.c.trader_id.in_(sub))),
            )
            filters.append(cond)

        total = (await self.db.execute(count_stmt.where(*filters))).scalar_one()
        rows = (await self.db.execute(
            rows_stmt.where(*filters)
            .order_by(MamAllocation.created_at.desc())
            .offset((page - 1) * limit).limit(limit)
        )).scalars().all()
        return list(rows), total
