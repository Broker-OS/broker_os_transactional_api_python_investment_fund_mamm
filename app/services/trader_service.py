"""
Clientes (traders) y fondeo de la cuenta maestra.

El MAM API no conoce usuarios: solo cuentas MT5 (spec §2). Por eso registrar un
trader es una operacion PURAMENTE LOCAL — no hay nada que provisionar del otro
lado hasta que se le crea una cuenta MAM. Eso tambien quiere decir que un trader
puede existir sin ninguna cuenta, que es el estado normal justo despues del alta.

`external_reference` lo asigna este servicio, no el socio. Spec §2.1: "No
pertenece al motor MAM directo. El CRM lo resuelve localmente y lo asocia con
mt5_login".
"""
from __future__ import annotations

import logging
import secrets
import uuid
from decimal import ROUND_DOWN, Decimal
from typing import Optional

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import (
    AmountInvalidError,
    LedgerInconsistentError,
    TraderAlreadyExistsError,
    TraderNotFoundError,
)
from app.models.api_user import ROLE_ADMIN
from app.models.movement import Movement
from app.models.trader import Trader
from app.repositories.movement_repository import MovementRepository
from app.repositories.trader_repository import TraderRepository
from app.services.ledger_service import LedgerService

logger = logging.getLogger(__name__)

_CENTS = Decimal("0.01")
_EXTREF_DIGITS = 12


class TraderService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.trader_repo = TraderRepository(db)
        self.movement_repo = MovementRepository(db)
        self.ledger = LedgerService(db)

    # ── helpers ──
    @staticmethod
    def _normalize_amount(amount: Decimal) -> Decimal:
        amt = Decimal(amount).quantize(_CENTS, rounding=ROUND_DOWN)
        if amt <= 0:
            raise AmountInvalidError(message="El monto debe ser mayor a 0", detail=f"amount={amount}")
        return amt

    async def get_trader(self, external_reference: str, *, caller=None) -> Trader:
        tr = await self.trader_repo.get_by_external_reference(external_reference)
        if tr is None:
            raise TraderNotFoundError(message="El trader no existe",
                                      detail=f"external_reference={external_reference}")
        # Un USER solo accede a SUS traders. Para no revelar traders ajenos se
        # responde el mismo "no existe" que para un id inventado: distinguirlos
        # permitiria enumerar la cartera de otro socio.
        if caller is not None and caller.role != ROLE_ADMIN and tr.owner_api_user_id != caller.id:
            raise TraderNotFoundError(message="El trader no existe",
                                      detail=f"external_reference={external_reference}")
        return tr

    async def _generate_external_reference(self) -> str:
        """external_reference numerico UNICO de 12 digitos (sin cero inicial)."""
        low = 10 ** (_EXTREF_DIGITS - 1)
        span = 9 * low
        for _ in range(10):
            ext_ref = str(low + secrets.randbelow(span))
            if not await self.trader_repo.external_reference_exists(ext_ref):
                return ext_ref
        raise TraderAlreadyExistsError(
            message="No se pudo generar un identificador de trader unico, reintenta", detail=None)

    # ── alta ──
    async def register_trader(
        self, *, email: str, first_name: Optional[str], last_name: Optional[str],
        max_active_leaders: Optional[int] = None, owner_api_user_id: Optional[str] = None,
    ) -> Trader:
        """Da de alta al cliente. No toca el MAM API: todavia no hay cuenta MT5.

        `max_active_leaders` es el cupo de allocations vivas que autoriza su plan
        (spec §7.1). Si viene vacio se usa el default del servicio al crear cada
        allocation.
        """
        ext_ref = await self._generate_external_reference()
        trader = Trader(external_reference=ext_ref, email=email, first_name=first_name,
                        last_name=last_name, max_active_leaders=max_active_leaders,
                        owner_api_user_id=owner_api_user_id)
        self.trader_repo.add(trader)
        try:
            await self.db.flush()
        except IntegrityError:
            # Colision de external_reference: se reintenta una vez con otro id.
            await self.db.rollback()
            ext_ref = await self._generate_external_reference()
            trader = Trader(external_reference=ext_ref, email=email, first_name=first_name,
                            last_name=last_name, max_active_leaders=max_active_leaders,
                            owner_api_user_id=owner_api_user_id)
            self.trader_repo.add(trader)
            await self.db.flush()
        await self.ledger.repo.ensure_trader_holdings(trader.id)
        await self.db.commit()
        return await self.get_trader(ext_ref)

    async def list_traders(self, *, caller=None, page: int = 1, limit: int = 10,
                           owner_api_user_id: Optional[str] = None):
        # USER: forzado a los suyos. ADMIN: todos, o filtra por creador si lo pasa.
        if caller is not None and caller.role != ROLE_ADMIN:
            owner = caller.id
        else:
            owner = owner_api_user_id
        return await self.trader_repo.list_with_owner(
            owner_api_user_id=owner, page=page, limit=limit)

    def max_active_leaders_for(self, trader: Optional[Trader]) -> int:
        """Cupo de allocations vivas a declarar al crear una allocation (spec §7.1).

        El motor MAM no guarda este limite: lo valida contra el valor que se le
        manda en ESA solicitud. Mandar el default cuando el trader no tiene plan
        explicito es deliberado — el campo es obligatorio y omitirlo dejaria la
        decision en manos del proveedor, que no la tiene.
        """
        if trader is not None and trader.max_active_leaders is not None:
            return trader.max_active_leaders
        return settings.MAM_DEFAULT_MAX_ACTIVE_LEADERS

    # ── fondeo de la cuenta maestra ──
    async def fund_master_account(self, *, amount: Decimal,
                                  idempotency_key: Optional[str]) -> Movement:
        """Acredita la cuenta maestra: externo → MASTER_ACCOUNT.

        Es una operacion 100% contable; no habla con el proveedor. De esta cuenta
        sale despues todo el capital que se reparte a las cuentas MT5.
        """
        amount = self._normalize_amount(amount)
        mv_id = str(uuid.uuid4())
        idem = idempotency_key or f"fund-{mv_id}"
        existing = await self.movement_repo.get_by_idempotency_key(idem)
        if existing is not None:
            return existing

        movement = Movement(id=mv_id, trader_id=None, direction="FUNDING", amount=amount,
                            currency=settings.LEDGER_CURRENCY, idempotency_key=idem,
                            status="PENDING", description="Fondeo de la cuenta maestra")
        self.movement_repo.add(movement)
        try:
            ledger_tx_id = await self.ledger.fund_master_account(
                amount=amount, idempotency_key=f"ldg-{idem}",
                description="Fondeo de la cuenta maestra")
        except (ValueError, RuntimeError) as e:
            await self.db.rollback()
            raise LedgerInconsistentError(
                message="No se pudo fondear la cuenta maestra", detail=str(e)[:400])
        movement.ledger_tx_id = ledger_tx_id
        movement.status = "COMPLETED"
        await self.db.commit()
        return await self.movement_repo.get_by_id(mv_id)

    async def get_movement(self, *, movement_id: str, caller=None) -> Movement:
        from app.core.exceptions import MovementNotFoundError

        mv = await self.movement_repo.get_by_id(movement_id)
        if mv is None:
            raise MovementNotFoundError(message="El movimiento no existe",
                                        detail=f"movement_id={movement_id}")
        if caller is not None and caller.role != ROLE_ADMIN:
            # El movimiento de fondeo no tiene trader: solo lo ve un ADMIN.
            if mv.trader_id is None:
                raise MovementNotFoundError(message="El movimiento no existe",
                                            detail=f"movement_id={movement_id}")
            trader = await self.trader_repo.get_by_id(mv.trader_id)
            if trader is None or trader.owner_api_user_id != caller.id:
                raise MovementNotFoundError(message="El movimiento no existe",
                                            detail=f"movement_id={movement_id}")
        return mv
