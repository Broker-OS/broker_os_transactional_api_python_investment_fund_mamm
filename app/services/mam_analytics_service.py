"""
Rendimiento de estrategias y clientes (spec §11.5).

Casi todo esto es pasaje directo al motor: los datos de mercado son suyos y
copiarlos a nuestra base seria garantizar que queden viejos. Lo que si agrega
valor local es el scoping — un USER solo puede pedir rendimiento de SUS cuentas —
y la vista consolidada por cliente, que el motor no puede armar porque no sabe
que cuentas pertenecen a la misma persona.

OJO con un detalle del contrato: `/subscribers` pagina con limit/offset, no con
cursor como el resto. Tratarlo igual que los demas devuelve siempre la primera
pagina.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import TraderNotFoundError
from app.models.api_user import ROLE_ADMIN
from app.models.mam import ACCOUNT_ACTIVE, ALLOC_LIVE_STATES, MamAllocation, MamPerfFeePayment
from app.repositories.ledger_repository import CODE_TRADER_HOLDINGS, LedgerRepository
from app.repositories.mam_repository import MamRepository
from app.repositories.trader_repository import TraderRepository
from app.services.mam_account_service import MamAccountService
from app.services.mam_client import get_mam_client

logger = logging.getLogger(__name__)


class MamAnalyticsService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = MamRepository(db)
        self.trader_repo = TraderRepository(db)
        self.ledger_repo = LedgerRepository(db)
        self.accounts = MamAccountService(db)
        self._client = get_mam_client()

    # ══════════════════════════════════════════════════════════════════
    # Pasaje al motor, con scoping
    # ══════════════════════════════════════════════════════════════════

    async def leader_performance(self, *, account_logins: list[str], caller=None) -> dict:
        """Resumen de una o varias estrategias.

        Se valida cada login contra nuestra base ANTES de consultar: sin eso,
        cualquiera podria pedir el rendimiento de la estrategia de otro socio
        pasando su login.
        """
        validados = [
            (await self.accounts.get_account(l, caller=caller)).mt5_login
            for l in account_logins
        ]
        return await self._client.leaders_performance_summary(account_logins=validados)

    async def follower_performance(self, *, account_logins: list[str], caller=None) -> dict:
        validados = [
            (await self.accounts.get_account(l, caller=caller)).mt5_login
            for l in account_logins
        ]
        return await self._client.followers_performance_summary(account_logins=validados)

    async def leader_trades(self, *, mt5_login: str, limit: Optional[int] = None,
                            cursor: Optional[int] = None, caller=None) -> dict:
        acc = await self.accounts.get_account(mt5_login, caller=caller)
        return await self._client.leader_trade_history(
            account_login=acc.mt5_login, limit=limit, cursor=cursor)

    async def follower_trades(self, *, mt5_login: str, limit: Optional[int] = None,
                              cursor: Optional[int] = None, caller=None) -> dict:
        acc = await self.accounts.get_account(mt5_login, caller=caller)
        return await self._client.follower_trade_history(
            account_login=acc.mt5_login, limit=limit, cursor=cursor)

    async def subscribers(self, *, mt5_login: str, status: Optional[str] = None,
                          limit: Optional[int] = None, offset: Optional[int] = None,
                          caller=None) -> dict:
        """Seguidores de una estrategia. Este endpoint usa limit/offset, no cursor."""
        acc = await self.accounts.get_account(mt5_login, caller=caller)
        return await self._client.leader_subscribers(
            account_login=acc.mt5_login, status=status, limit=limit, offset=offset)

    async def strategy(self, *, mt5_login: str, caller=None) -> dict:
        acc = await self.accounts.get_account(mt5_login, caller=caller)
        return await self._client.leader_strategy(account_login=acc.mt5_login)

    # ══════════════════════════════════════════════════════════════════
    # Vista consolidada (esto no lo puede armar el motor)
    # ══════════════════════════════════════════════════════════════════

    async def trader_overview(self, *, external_reference: str, caller=None) -> dict:
        """Todo lo del cliente en una sola respuesta.

        El motor no puede armarla: no sabe que varias cuentas MT5 son de la misma
        persona — esa relacion vive solo aca (spec §2). Junta tres fuentes:

            nuestra base   que cuentas tiene y a que estrategias sigue
            el ledger      cuanto capital le colocamos, neto
            MT5 en vivo    cuanto vale hoy cada cuenta

        Las metricas se piden por cuenta y se toleran fallas individuales: una
        cuenta que MT5 no puede resolver no deberia hacer fallar el resumen
        completo del cliente.
        """
        trader = await self.trader_repo.get_by_external_reference(external_reference)
        if trader is None:
            raise TraderNotFoundError(message="El cliente no existe",
                                      detail=f"external_reference={external_reference}")
        if caller is not None and caller.role != ROLE_ADMIN \
                and trader.owner_api_user_id != caller.id:
            raise TraderNotFoundError(message="El cliente no existe",
                                      detail=f"external_reference={external_reference}")

        cuentas = await self.repo.active_accounts_for_trader(trader.id)
        logins = [c.mt5_login for c in cuentas]

        # Capital neto colocado, segun el libro.
        holdings = await self.ledger_repo.account_for_update(
            code=CODE_TRADER_HOLDINGS, trader_id=trader.id)
        colocado = (await self.ledger_repo.recompute_balance(holdings.id)) if holdings else Decimal("0")

        # Fees ya cedidos por este cliente.
        fees = (await self.db.execute(
            select(MamPerfFeePayment.amount)
            .where(MamPerfFeePayment.trader_id == trader.id,
                   MamPerfFeePayment.status == "EXECUTED"))).scalars().all()
        fees_total = sum((Decimal(str(f)) for f in fees), Decimal("0"))

        # Suscripciones vivas donde el cliente es quien recibe operaciones.
        subs = (await self.db.execute(
            select(MamAllocation).where(
                MamAllocation.follower_login.in_(logins or [""]),
                MamAllocation.status.in_(ALLOC_LIVE_STATES)))).scalars().all()

        detalle = []
        equity_total = Decimal("0")
        for cuenta in cuentas:
            fila: dict[str, Any] = {
                "mt5_login": cuenta.mt5_login,
                "name": cuenta.name,
                "can_be_leader": cuenta.can_be_leader,
                "can_be_follower": cuenta.can_be_follower,
                "status": cuenta.status,
                "balance": None, "equity": None, "free_margin": None,
                "metrics_error": None,
            }
            try:
                m = await self._client.get_account_metrics(account_login=cuenta.mt5_login)
                fila["balance"] = _dec(m.get("balance"))
                fila["equity"] = _dec(m.get("equity"))
                fila["free_margin"] = _dec(m.get("free_margin"))
                if fila["equity"] is not None:
                    equity_total += fila["equity"]
            except Exception as exc:  # noqa: BLE001
                # Se informa por cuenta en vez de tumbar el resumen entero.
                fila["metrics_error"] = type(exc).__name__
                logger.warning("MAM: sin metricas para %s (%s)",
                               cuenta.mt5_login, type(exc).__name__)
            detalle.append(fila)

        return {
            "external_reference": trader.external_reference,
            "email": trader.email,
            "max_active_leaders": trader.max_active_leaders,
            "accounts": detalle,
            "live_subscriptions": [
                {"allocation_id": s.allocation_id, "leader_login": s.leader_login,
                 "follower_login": s.follower_login, "status": s.status,
                 "allocation_mode": s.allocation_mode,
                 "performance_fee_rate": s.performance_fee_rate}
                for s in subs
            ],
            "capital_placed": colocado,
            "equity_total": equity_total,
            "performance_fees_paid": fees_total,
        }


def _dec(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None
