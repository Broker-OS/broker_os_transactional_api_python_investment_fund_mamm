"""
Allocations: la relacion de copy trading entre dos cuentas (spec §5 pasos 5-10, §11.4).

Una allocation conecta una cuenta que ORIGINA operaciones (leader) con una que
las RECIBE (follower). Los roles son de la relacion, no de las cuentas: la misma
cuenta puede ser leader de una allocation y follower de otra.

DOS COSAS QUE ESTE SERVICIO RESUELVE Y EL MOTOR NO:

1. `max_active_leaders_per_follower` (spec §7.1). El motor NO guarda ese limite:
   lo valida contra el valor que le mandamos en ESA solicitud y lo olvida. Si no
   lo resolvieramos desde el plan del cliente, cada llamada podria mandar un
   numero distinto y el cupo no significaria nada.

2. El alta en dos tiempos (spec §13). La allocation se crea en PAUSED y se
   activa con un PATCH aparte, para tener una oportunidad de verificar la
   respuesta antes de que empiece a copiar operaciones reales.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import (
    AccountNotActiveError,
    AccountNotHedgingError,
    AllocationAlreadyLiveError,
    AllocationNotFoundError,
    FollowerCapabilityMissingError,
    LeaderCapabilityMissingError,
    LeaderProfileNotFoundError,
    MaxActiveLeadersReachedError,
    MinDepositNotMetError,
    SelfFollowError,
)
from app.models.api_user import ROLE_ADMIN
from app.models.mam import (
    ACCOUNT_ACTIVE,
    ALLOC_ACTIVE,
    ALLOC_CANCELLED,
    ALLOC_LIVE_STATES,
    ALLOC_PAUSED,
    MODE_HEDGING,
    MamAccount,
    MamAllocation,
    MamFeeConfigChange,
)
from app.repositories.mam_repository import MamRepository
from app.repositories.trader_repository import TraderRepository
from app.services.mam_account_service import MamAccountService, _as_int
from app.services.mam_client import get_mam_client
from app.services.trader_service import TraderService

logger = logging.getLogger(__name__)


class MamAllocationService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = MamRepository(db)
        self.trader_repo = TraderRepository(db)
        self.accounts = MamAccountService(db)
        self.traders = TraderService(db)
        self._client = get_mam_client()

    # ══════════════════════════════════════════════════════════════════
    # Validaciones locales (spec §8)
    # ══════════════════════════════════════════════════════════════════

    @staticmethod
    def _ensure_operable(acc: MamAccount, *, rol: str) -> None:
        """Spec §8: ambas cuentas deben existir, estar ACTIVE y usar HEDGING."""
        if acc.status != ACCOUNT_ACTIVE:
            raise AccountNotActiveError(
                message=f"La cuenta {rol} no esta activa",
                detail=f"mt5_login={acc.mt5_login} status={acc.status}")
        if acc.account_mode != MODE_HEDGING:
            raise AccountNotHedgingError(
                message=f"La cuenta {rol} no usa HEDGING y no puede hacer copy trading",
                detail=f"mt5_login={acc.mt5_login} account_mode={acc.account_mode}")

    async def _resolve_pair(self, leader_login: str, follower_login: str, *, caller=None):
        if str(leader_login).strip() == str(follower_login).strip():
            raise SelfFollowError(
                message="Una cuenta no puede seguirse a si misma",
                detail=f"login={leader_login}")

        leader = await self.accounts.get_account(leader_login, caller=caller)
        follower = await self.accounts.get_account(follower_login, caller=caller)
        self._ensure_operable(leader, rol="leader")
        self._ensure_operable(follower, rol="follower")

        if not leader.can_be_leader:
            raise LeaderCapabilityMissingError(
                message="La cuenta leader no esta autorizada a originar operaciones",
                detail=f"mt5_login={leader.mt5_login}: falta can_be_leader")
        if not follower.can_be_follower:
            raise FollowerCapabilityMissingError(
                message="La cuenta follower no esta autorizada a recibir operaciones",
                detail=f"mt5_login={follower.mt5_login}: falta can_be_follower")

        profile = await self.repo.get_profile_by_account_id(leader.id)
        if profile is None:
            # El flag solo no alcanza: sin perfil el motor no sabe con que fee ni
            # con que minimo opera la estrategia.
            raise LeaderProfileNotFoundError(
                message="La cuenta leader no tiene perfil de estrategia",
                detail=(f"mt5_login={leader.mt5_login}: crear el perfil con "
                        f"POST /mam/leaders antes de suscribir clientes"))
        return leader, follower, profile

    async def _max_active_leaders(self, follower: MamAccount) -> int:
        """Cupo autorizado por el plan del cliente dueño de la cuenta follower."""
        trader = (await self.trader_repo.get_by_id(follower.trader_id)
                  if follower.trader_id else None)
        return self.traders.max_active_leaders_for(trader)

    # ══════════════════════════════════════════════════════════════════
    # Elegibilidad
    # ══════════════════════════════════════════════════════════════════

    async def check_eligibility(self, *, leader_login: str, follower_login: str,
                                caller=None) -> dict:
        """Valida SIN crear la allocation (spec §11.4).

        Sirve para pedirle fondos al cliente antes de intentar la suscripcion, en
        vez de mostrarle un error despues. No reemplaza la validacion del alta:
        al crear, el motor vuelve a consultar el balance en MT5 para no trabajar
        sobre un dato viejo.
        """
        leader, follower, profile = await self._resolve_pair(
            leader_login, follower_login, caller=caller)

        cupo = await self._max_active_leaders(follower)
        vivas = await self.repo.count_live_allocations_for_follower(follower.mt5_login)

        data = await self._client.check_subscription_eligibility(
            leader_login=leader.mt5_login, follower_login=follower.mt5_login)

        # El motor no conoce el plan del cliente: ese chequeo es nuestro.
        cupo_ok = vivas < cupo
        data["max_active_leaders"] = cupo
        data["live_allocations"] = vivas
        data["quota_available"] = cupo_ok
        data["eligible"] = bool(data.get("eligible")) and cupo_ok
        if not cupo_ok:
            data["quota_reason"] = (
                f"El cliente ya tiene {vivas} suscripcion(es) viva(s) y su plan "
                f"autoriza {cupo}.")
        return data

    # ══════════════════════════════════════════════════════════════════
    # Alta
    # ══════════════════════════════════════════════════════════════════

    async def create_allocation(
        self, *, leader_login: str, follower_login: str,
        allocation_mode: str = "EQUITY", mode_parameter: Optional[Decimal] = None,
        equity_stop: Optional[Decimal] = None,
        unsubscribe_policy: str = "CLOSE_ON_UNSUBSCRIBE",
        performance_fee_rate: Optional[Decimal] = None,
        performance_fee_enabled: bool = True, activate: bool = True,
        max_active_leaders: Optional[int] = None, caller=None,
    ) -> MamAllocation:
        """Suscribe una cuenta a una estrategia.

        Se crea en PAUSED y, si `activate`, se pasa a ACTIVE con un PATCH: asi la
        fila local queda escrita antes de que el motor empiece a copiar. Si
        activaramos en el mismo POST y despues fallara el guardado, habria una
        relacion copiando operaciones reales que nuestra base no conoce.
        """
        leader, follower, profile = await self._resolve_pair(
            leader_login, follower_login, caller=caller)

        # Spec §8: una sola allocation viva por pareja. El motor tambien lo
        # valida, pero fallar aca ahorra el round-trip y da mejor mensaje.
        viva = await self.repo.live_allocation_for_pair(
            leader_login=leader.mt5_login, follower_login=follower.mt5_login)
        if viva is not None:
            raise AllocationAlreadyLiveError(
                message="Ese cliente ya esta suscripto a esa estrategia",
                detail=(f"allocation_id={viva.allocation_id} status={viva.status}. "
                        f"Para cambiar la configuracion usar el PATCH."))

        cupo = max_active_leaders if max_active_leaders is not None else \
            await self._max_active_leaders(follower)
        vivas = await self.repo.count_live_allocations_for_follower(follower.mt5_login)
        if vivas >= cupo:
            raise MaxActiveLeadersReachedError(
                message="El cliente alcanzo el limite de estrategias simultaneas de su plan",
                detail=f"vivas={vivas} limite={cupo} follower={follower.mt5_login}")

        try:
            data = await self._client.create_allocation(
                leader_login=leader.mt5_login, follower_login=follower.mt5_login,
                max_active_leaders_per_follower=cupo,
                allocation_mode=allocation_mode, mode_parameter=mode_parameter,
                status=ALLOC_PAUSED, equity_stop=equity_stop,
                unsubscribe_policy=unsubscribe_policy,
                performance_fee_rate=performance_fee_rate,
                performance_fee_enabled=performance_fee_enabled,
            )
        except Exception as exc:  # noqa: BLE001
            detail = str(getattr(exc, "detail", "") or "")
            # El motor devuelve 409 tanto por cupo como por min_deposit; el
            # mensaje generico no le sirve a quien tiene que resolverlo.
            if "min_deposit" in detail.lower() or "balance" in detail.lower():
                raise MinDepositNotMetError(
                    message="El balance del cliente no alcanza el minimo de la estrategia",
                    detail=(f"minimo={profile.min_deposit} follower={follower.mt5_login}. "
                            f"Depositar antes de suscribir. ({detail[:200]})")) from exc
            raise

        allocation_id = _as_int(data.get("id")) or _as_int(data.get("allocation_id"))
        alloc = MamAllocation(
            allocation_id=allocation_id,
            leader_account_id=leader.id, follower_account_id=follower.id,
            leader_login=leader.mt5_login, follower_login=follower.mt5_login,
            status=data.get("status") or ALLOC_PAUSED,
            allocation_mode=data.get("allocation_mode") or allocation_mode,
            mode_parameter=_opt_dec(data.get("mode_parameter")) or mode_parameter,
            equity_stop=_opt_dec(data.get("equity_stop")) or equity_stop,
            unsubscribe_policy=data.get("unsubscribe_policy") or unsubscribe_policy,
            # Si no se envio tasa, hereda la del perfil del leader.
            performance_fee_rate=(_opt_dec(data.get("performance_fee_rate"))
                                  or performance_fee_rate or profile.performance_fee_rate),
            performance_fee_enabled=bool(
                data.get("performance_fee_enabled", performance_fee_enabled)),
            max_active_leaders_requested=cupo,
        )
        self.repo.add(alloc)
        try:
            await self.db.commit()
        except Exception:
            await self.db.rollback()
            logger.error(
                "MAM: la allocation %s (%s -> %s) quedo creada en el motor pero NO se "
                "pudo guardar localmente. Consultarla antes de reintentar: crear otra "
                "daria 409 por pareja duplicada.",
                allocation_id, leader.mt5_login, follower.mt5_login)
            raise
        await self.db.refresh(alloc)

        if activate:
            alloc = await self.set_status(
                allocation_id=alloc.allocation_id, status=ALLOC_ACTIVE, caller=caller)
        logger.info("MAM: allocation %s creada (%s -> %s, %s)",
                    alloc.allocation_id, leader.mt5_login, follower.mt5_login, alloc.status)
        return alloc

    async def import_allocation(self, *, allocation_id: int, caller=None) -> MamAllocation:
        """Trae a nuestra base una allocation que YA existe en el motor.

        Hace falta por lo mismo que el import de cuentas: el ambiente del
        proveedor no arranca vacio. Ademas, una relacion preexistente que no
        conocemos es peor que una cuenta suelta — cuenta para el cupo del plan y
        para la deteccion de ciclos del motor, asi que sin importarla nuestras
        validaciones locales trabajan sobre un mapa incompleto y rechazamos (o
        aceptamos) suscripciones por razones que no podemos explicar.

        Las dos cuentas tienen que estar importadas antes: sin ellas no hay a que
        colgar la relacion.
        """
        ya = await self.repo.get_allocation_by_provider_id(allocation_id)
        if ya is not None:
            return ya

        data = await self._client.get_allocation(allocation_id=allocation_id)
        leader_login = str(data.get("leader_login") or "")
        follower_login = str(data.get("follower_login") or "")

        leader = await self.accounts.get_account(leader_login, caller=caller)
        follower = await self.accounts.get_account(follower_login, caller=caller)

        alloc = MamAllocation(
            allocation_id=allocation_id,
            leader_account_id=leader.id, follower_account_id=follower.id,
            leader_login=leader.mt5_login, follower_login=follower.mt5_login,
            status=data.get("status") or ALLOC_PAUSED,
            allocation_mode=data.get("allocation_mode") or "EQUITY",
            mode_parameter=_opt_dec(data.get("mode_parameter")),
            equity_stop=_opt_dec(data.get("equity_stop")),
            unsubscribe_policy=data.get("unsubscribe_policy") or "CLOSE_ON_UNSUBSCRIBE",
            performance_fee_rate=_opt_dec(data.get("performance_fee_rate")),
            performance_fee_enabled=bool(data.get("performance_fee_enabled", True)),
            started_at=_opt_dt(data.get("started_at")),
            ended_at=_opt_dt(data.get("ended_at")),
        )
        self.repo.add(alloc)
        await self.db.commit()
        await self.db.refresh(alloc)
        logger.info("MAM: allocation %s importada (%s -> %s, %s)",
                    allocation_id, leader_login, follower_login, alloc.status)
        return alloc

    # ══════════════════════════════════════════════════════════════════
    # Consulta y edicion
    # ══════════════════════════════════════════════════════════════════

    async def get_allocation(self, allocation_id: int, *, caller=None) -> MamAllocation:
        alloc = await self.repo.get_allocation_by_provider_id(allocation_id)
        if alloc is None:
            raise AllocationNotFoundError(
                message="La suscripcion no existe", detail=f"allocation_id={allocation_id}")
        if caller is not None and caller.role != ROLE_ADMIN:
            # Visible si es dueño de cualquiera de las dos puntas.
            for acc_id in (alloc.leader_account_id, alloc.follower_account_id):
                acc = await self.repo.get_account_by_id(acc_id)
                if acc is None or acc.trader_id is None:
                    continue
                trader = await self.trader_repo.get_by_id(acc.trader_id)
                if trader is not None and trader.owner_api_user_id == caller.id:
                    return alloc
            raise AllocationNotFoundError(
                message="La suscripcion no existe", detail=f"allocation_id={allocation_id}")
        return alloc

    async def list_allocations(
        self, *, caller=None, leader_login: Optional[str] = None,
        follower_login: Optional[str] = None, status: Optional[str] = None,
        page: int = 1, limit: int = 20,
    ):
        owner = None if (caller is None or caller.role == ROLE_ADMIN) else caller.id
        return await self.repo.list_allocations(
            leader_login=leader_login, follower_login=follower_login, status=status,
            owner_api_user_id=owner, page=page, limit=limit)

    async def set_status(self, *, allocation_id: int, status: str, caller=None) -> MamAllocation:
        """Pausa o reactiva. Al pasar a un estado vivo el motor revalida todo."""
        alloc = await self.get_allocation(allocation_id, caller=caller)
        data = await self._client.update_allocation(
            allocation_id=allocation_id, status=status)
        alloc.status = data.get("status") or status
        if alloc.status == ALLOC_ACTIVE and alloc.started_at is None:
            from app.models._helpers import now_utc
            alloc.started_at = now_utc()
        await self.db.commit()
        await self.db.refresh(alloc)
        return alloc

    async def update_allocation(
        self, *, allocation_id: int, note: Optional[str] = None, caller=None, **changes,
    ) -> MamAllocation:
        """Cambia configuracion de la relacion ya existente.

        Ojo con `status`: para dar de baja NO se usa este endpoint. Un cambio
        directo a CANCELLED no evalua las posiciones abiertas ni cobra el fee
        pendiente — para eso esta `unsubscribe`.
        """
        alloc = await self.get_allocation(allocation_id, caller=caller)
        changes = {k: v for k, v in changes.items() if v is not None}
        if not changes:
            return alloc

        data = await self._client.update_allocation(allocation_id=allocation_id, **changes)

        if "performance_fee_rate" in changes:
            self.repo.add(MamFeeConfigChange(
                target_kind="ALLOCATION",
                target_ref=str(allocation_id),
                changed_by_api_user_id=getattr(caller, "id", None),
                previous_rate=alloc.performance_fee_rate,
                new_rate=changes["performance_fee_rate"],
                previous_enabled=alloc.performance_fee_enabled,
                new_enabled=changes.get("performance_fee_enabled",
                                        alloc.performance_fee_enabled),
                note=note,
            ))

        for field, value in changes.items():
            setattr(alloc, field, value)
        if data.get("status"):
            alloc.status = data["status"]
        await self.db.commit()
        await self.db.refresh(alloc)
        return alloc

    # ══════════════════════════════════════════════════════════════════
    # Baja
    # ══════════════════════════════════════════════════════════════════

    async def unsubscribe(self, *, allocation_id: int, caller=None) -> MamAllocation:
        """Termina la relacion aplicando la politica configurada (spec §9).

        KEEP_OPEN: cobra el fee pendiente, desconecta las posiciones copiadas y
        las deja abiertas en MT5 fuera de la gestion MAM.
        CLOSE_ON_UNSUBSCRIBE: genera los cierres, y cuando terminan cobra el fee.

        Con la segunda politica la respuesta puede quedar en STOPPING mientras se
        cierran las posiciones. NO hay que repetir la llamada: se consulta con
        `sync` hasta ver CANCELLED.
        """
        alloc = await self.get_allocation(allocation_id, caller=caller)
        if alloc.status == ALLOC_CANCELLED:
            return alloc

        data = await self._client.unsubscribe_allocation(allocation_id=allocation_id)
        alloc.status = data.get("status") or alloc.status
        alloc.terminated_reason = data.get("reason") or "USER_UNSUBSCRIBE"
        alloc.terminated_by = data.get("triggered_by") or "USER"
        fee = _opt_dec(data.get("performance_fee_charged"))
        if fee is not None:
            alloc.performance_fee_charged = fee
        if alloc.status == ALLOC_CANCELLED:
            from app.models._helpers import now_utc
            alloc.ended_at = now_utc()
        await self.db.commit()
        await self.db.refresh(alloc)
        logger.info("MAM: allocation %s desuscripta -> %s", allocation_id, alloc.status)
        return alloc

    async def sync(self, *, allocation_id: Optional[int] = None, limit: int = 50) -> dict:
        """Refresca contra el motor las allocations que no terminaron de cerrar.

        Existe porque una desuscripcion con CLOSE_ON_UNSUBSCRIBE no es
        instantanea: queda en STOPPING hasta que se cierran las posiciones. Sin
        esto, nuestra base diria STOPPING para siempre aunque el motor ya la haya
        dado por CANCELLED.
        """
        from app.models._helpers import now_utc

        if allocation_id is not None:
            pendientes = [await self.repo.get_allocation_by_provider_id(allocation_id)]
            pendientes = [a for a in pendientes if a is not None]
        else:
            rows, _ = await self.repo.list_allocations(status="STOPPING", limit=limit)
            pendientes = rows

        revisadas, cambiadas = 0, 0
        for alloc in pendientes:
            if alloc.allocation_id is None:
                continue
            revisadas += 1
            try:
                data = await self._client.get_allocation(allocation_id=alloc.allocation_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("MAM: no se pudo sincronizar la allocation %s (%s)",
                               alloc.allocation_id, type(exc).__name__)
                continue
            nuevo = data.get("status")
            alloc.last_polled_at = now_utc()
            if nuevo and nuevo != alloc.status:
                alloc.status = nuevo
                if nuevo == ALLOC_CANCELLED and alloc.ended_at is None:
                    alloc.ended_at = now_utc()
                cambiadas += 1
        await self.db.commit()
        return {"reviewed": revisadas, "changed": cambiadas}


def _opt_dec(value) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None


def _opt_dt(value):
    """El motor manda ISO 8601, a veces sin zona. Sin tz se asume UTC (spec §3.7)."""
    if not value:
        return None
    from datetime import datetime, timezone
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
