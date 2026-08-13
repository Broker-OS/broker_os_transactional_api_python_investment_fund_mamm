"""Acceso a datos de los depositos on-chain."""
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.crypto_deposit import STATUS_CONFIRMED, CryptoDeposit


class CryptoDepositRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    def add(self, deposit: CryptoDeposit) -> CryptoDeposit:
        self.db.add(deposit)
        return deposit

    async def get_by_id(self, deposit_id: str) -> Optional[CryptoDeposit]:
        r = await self.db.execute(select(CryptoDeposit).where(CryptoDeposit.id == deposit_id))
        return r.scalar_one_or_none()

    async def confirmed_exists(self, tx_hash: str) -> bool:
        """True si esa transaccion ya se acredito (proteccion contra reuso)."""
        r = await self.db.execute(
            select(CryptoDeposit.id).where(
                CryptoDeposit.tx_hash == tx_hash,
                CryptoDeposit.status == STATUS_CONFIRMED,
            ).limit(1)
        )
        return r.scalar_one_or_none() is not None

    async def list(self, *, status: Optional[str] = None, rejection_code: Optional[str] = None,
                   api_user_id: Optional[str] = None, limit: int = 50,
                   offset: int = 0) -> tuple[list[CryptoDeposit], int]:
        filters = []
        if status:
            filters.append(CryptoDeposit.status == status)
        if rejection_code:
            filters.append(CryptoDeposit.rejection_code == rejection_code)
        if api_user_id:
            filters.append(CryptoDeposit.api_user_id == api_user_id)
        total = (await self.db.execute(
            select(func.count()).select_from(CryptoDeposit).where(*filters))).scalar_one()
        rows = (await self.db.execute(
            select(CryptoDeposit).where(*filters)
            .order_by(CryptoDeposit.created_at.desc())
            .offset(offset).limit(limit))).scalars().all()
        return list(rows), total
