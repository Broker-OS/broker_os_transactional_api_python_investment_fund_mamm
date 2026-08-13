"""
Perfiles de leader y cuenta PAYMENT (spec §5 pasos 2-3, §11.2, §11.3).

El perfil NO crea otra cuenta: extiende una cuenta MAM existente con lo necesario
para ORIGINAR allocations. Una cuenta que solo va a recibir operaciones no lo
necesita.

La spec separa habilitar la capacidad (PATCH de la cuenta) de crear el perfil
(POST /mam/leaders). Aca se hacen las dos en una sola operacion: pedirle al
integrador que recuerde el orden es una fuente de errores gratuita, y un perfil
sobre una cuenta sin `can_be_leader` es rechazado por el motor.

CUENTA PAYMENT (spec §4.5): cada leader tiene una cuenta MT5 aparte que solo
recibe sus performance fees. Se crea sola si NO se manda `payment_account_login`.
Mandar cadena vacia, 0 o el login operativo no equivale a omitir: rompe la
creacion automatica. Por eso aca solo se envia cuando hay un login real.
"""
from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import (
    LeaderProfileAlreadyExistsError,
    LeaderProfileNotFoundError,
    PaymentAccountUnavailableError,
    ProviderOperationInProgressError,
)
from app.models.api_user import ROLE_ADMIN
from app.models.mam import (
    ACCOUNT_ACTIVE,
    LEADER_ACTIVE,
    MamFeeConfigChange,
    MamLeaderProfile,
)
from app.models.movement import Movement
from app.repositories.mam_repository import MamRepository
from app.repositories.movement_repository import MovementRepository
from app.services.mam_account_service import MamAccountService, _as_int
from app.services.mam_client import get_mam_client

logger = logging.getLogger(__name__)


class MamLeaderService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = MamRepository(db)
        self.movement_repo = MovementRepository(db)
        self.accounts = MamAccountService(db)
        self._client = get_mam_client()

    # ══════════════════════════════════════════════════════════════════
    # Perfil
    # ══════════════════════════════════════════════════════════════════

    async def create_profile(
        self, *, account_login: str, strategy_name: str, description: Optional[str] = None,
        leaderboard_visibility: bool = False, restrict_simultaneous_connections: bool = False,
        min_deposit: Decimal = Decimal("0"), performance_fee_rate: Decimal = Decimal("0"),
        performance_fee_period: str = "MONTHLY", propagation_mode: str = "ORIGINAL_ONLY",
        payment_account_login: Optional[str] = None, caller=None,
    ) -> MamLeaderProfile:
        acc = await self.accounts.get_account(account_login, caller=caller)

        existing = await self.repo.get_profile_by_account_id(acc.id)
        if existing is not None:
            raise LeaderProfileAlreadyExistsError(
                message="Esa cuenta ya tiene un perfil de estrategia",
                detail=f"account_login={acc.mt5_login} leader_id={existing.leader_id}")

        # Spec §5 paso 2: sin la capacidad, el motor rechaza el perfil. Se
        # habilita antes en vez de devolver un error que el integrador tendria
        # que resolver con otra llamada.
        if not acc.can_be_leader:
            await self.accounts.update_account(
                mt5_login=acc.mt5_login, can_be_leader=True, caller=caller)
            await self.db.refresh(acc)

        data = await self._client.create_leader_profile(
            account_login=acc.mt5_login, strategy_name=strategy_name,
            description=description, leaderboard_visibility=leaderboard_visibility,
            restrict_simultaneous_connections=restrict_simultaneous_connections,
            min_deposit=min_deposit, performance_fee_rate=performance_fee_rate,
            performance_fee_period=performance_fee_period,
            propagation_mode=propagation_mode,
            payment_account_login=payment_account_login,
        )

        payment_login = data.get("payment_account_login")
        if not payment_login:
            # El motor deberia haberla creado. Sin ella, los endpoints de saldo
            # y retiro PAYMENT responden 409 mas adelante.
            logger.warning(
                "MAM: el perfil de %s se creo SIN cuenta PAYMENT; el cobro de fees "
                "no va a poder conciliarse.", acc.mt5_login)

        profile = MamLeaderProfile(
            account_id=acc.id,
            leader_id=_as_int(data.get("id")),
            account_login=acc.mt5_login,
            payment_account_login=str(payment_login) if payment_login else None,
            strategy_name=data.get("strategy_name") or strategy_name,
            description=data.get("description") or description,
            leaderboard_visibility=bool(data.get("leaderboard_visibility", leaderboard_visibility)),
            restrict_simultaneous_connections=bool(
                data.get("restrict_simultaneous_connections", restrict_simultaneous_connections)),
            min_deposit=_dec(data.get("min_deposit"), min_deposit),
            performance_fee_rate=_dec(data.get("performance_fee_rate"), performance_fee_rate),
            performance_fee_period=data.get("performance_fee_period") or performance_fee_period,
            propagation_mode=data.get("propagation_mode") or propagation_mode,
            status=data.get("status") or LEADER_ACTIVE,
        )
        self.repo.add(profile)
        try:
            await self.db.commit()
        except Exception:
            await self.db.rollback()
            logger.error(
                "MAM: el perfil de leader de %s quedo creado en el proveedor (leader_id=%s) "
                "pero NO se pudo guardar localmente. Recuperarlo con GET /mam/leaders antes "
                "de reintentar: crear otro dejaria dos perfiles.",
                acc.mt5_login, data.get("id"))
            raise
        await self.db.refresh(profile)
        logger.info("MAM: perfil de leader creado para %s (payment=%s)",
                    acc.mt5_login, payment_login or "sin PAYMENT")
        return profile

    async def import_profile(self, *, account_login: str, caller=None) -> MamLeaderProfile:
        """Trae un perfil de estrategia que YA existe en el motor.

        Tercer caso del mismo problema que las cuentas y las allocations: el
        ambiente del proveedor tiene perfiles creados antes de la integracion.
        Y este es el que mas duele si falta — sin el perfil local, cualquier
        intento de suscribir a esa estrategia se rechaza con "no tiene perfil",
        que es exactamente lo contrario de lo que pasa en el motor.

        El perfil se busca por el login OPERATIVO de la cuenta, no por leader_id.
        """
        acc = await self.accounts.get_account(account_login, caller=caller)
        ya = await self.repo.get_profile_by_account_id(acc.id)
        if ya is not None:
            return ya

        page = await self._client.list_leaders(account_login=acc.mt5_login)
        items, _, _ = self._client.iterate_pages(page)
        data = next((i for i in items
                     if str(i.get("account_login")) == acc.mt5_login), None)
        if data is None:
            raise LeaderProfileNotFoundError(
                message="El motor no tiene perfil de estrategia para esa cuenta",
                detail=f"account_login={acc.mt5_login}")

        profile = MamLeaderProfile(
            account_id=acc.id,
            leader_id=_as_int(data.get("id")),
            account_login=acc.mt5_login,
            payment_account_login=(str(data["payment_account_login"])
                                   if data.get("payment_account_login") else None),
            strategy_name=data.get("strategy_name"),
            description=data.get("description"),
            leaderboard_visibility=bool(data.get("leaderboard_visibility", False)),
            restrict_simultaneous_connections=bool(
                data.get("restrict_simultaneous_connections", False)),
            min_deposit=_dec(data.get("min_deposit"), Decimal("0")),
            performance_fee_rate=_dec(data.get("performance_fee_rate"), Decimal("0")),
            performance_fee_period=data.get("performance_fee_period") or "MONTHLY",
            propagation_mode=data.get("propagation_mode") or "ORIGINAL_ONLY",
            status=data.get("status") or LEADER_ACTIVE,
        )
        self.repo.add(profile)
        await self.db.commit()
        await self.db.refresh(profile)
        logger.info("MAM: perfil de leader %s importado para %s",
                    profile.leader_id, acc.mt5_login)
        return profile

    async def get_profile(self, account_login: str, *, caller=None) -> MamLeaderProfile:
        # Pasa por la cuenta para heredar la validacion de propiedad.
        acc = await self.accounts.get_account(account_login, caller=caller)
        profile = await self.repo.get_profile_by_account_id(acc.id)
        if profile is None:
            raise LeaderProfileNotFoundError(
                message="Esa cuenta no tiene perfil de estrategia",
                detail=f"account_login={account_login}")
        return profile

    async def update_profile(
        self, *, account_login: str, note: Optional[str] = None, caller=None, **changes,
    ) -> MamLeaderProfile:
        """Actualiza el perfil y deja auditoria si cambia la configuracion del fee.

        Cambiar la tasa no reemplaza el High-Water Mark ni altera fees ya
        ejecutados, asi que sin este registro no hay forma de reconstruir que
        tasa regia en una fecha dada — ni quien la autorizo.
        """
        profile = await self.get_profile(account_login, caller=caller)
        if profile.leader_id is None:
            raise LeaderProfileNotFoundError(
                message="El perfil no tiene id del proveedor: no se puede actualizar",
                detail=f"account_login={account_login}")

        changes = {k: v for k, v in changes.items() if v is not None}
        if not changes:
            return profile

        await self._client.update_leader_profile(leader_id=profile.leader_id, **changes)

        fee_fields = ("performance_fee_rate", "performance_fee_period")
        if any(f in changes for f in fee_fields):
            self.repo.add(MamFeeConfigChange(
                target_kind="LEADER_PROFILE",
                target_ref=profile.account_login,
                trader_id=(await self.repo.get_account_by_id(profile.account_id)).trader_id,
                changed_by_api_user_id=getattr(caller, "id", None),
                previous_rate=profile.performance_fee_rate,
                previous_period=profile.performance_fee_period,
                new_rate=changes.get("performance_fee_rate", profile.performance_fee_rate),
                new_period=changes.get("performance_fee_period", profile.performance_fee_period),
                note=note,
            ))

        for field, value in changes.items():
            setattr(profile, field, value)
        await self.db.commit()
        await self.db.refresh(profile)
        return profile

    async def list_profiles(self, *, status: Optional[str] = None,
                            page: int = 1, limit: int = 20):
        return await self.repo.list_profiles(status=status, page=page, limit=limit)

    # ══════════════════════════════════════════════════════════════════
    # Cuenta PAYMENT
    # ══════════════════════════════════════════════════════════════════

    async def payment_balance(self, *, account_login: str, caller=None) -> dict:
        """Saldo en vivo de la PAYMENT.

        La URL lleva SIEMPRE el login OPERATIVO del master, nunca el de la
        PAYMENT. Para habilitar un retiro hay que mirar `withdrawable`, que
        excluye el credito MT5; usar `balance` dejaria pasar retiros que MT5
        despues rechaza.
        """
        profile = await self.get_profile(account_login, caller=caller)
        try:
            data = await self._client.get_payment_account_balance(
                master_login=profile.account_login)
        except ProviderOperationInProgressError as exc:
            # 409 = el leader no tiene PAYMENT dedicada valida.
            raise PaymentAccountUnavailableError(
                message="El leader no tiene una cuenta PAYMENT valida",
                detail=exc.detail) from exc
        data.setdefault("master_login", profile.account_login)
        data.setdefault("payment_account_login", profile.payment_account_login)
        return data

    async def payment_withdraw(
        self, *, account_login: str, amount: Decimal,
        idempotency_key: Optional[str] = None, caller=None,
    ) -> dict:
        """Retira fees ya acreditados en la PAYMENT.

        No toca el balance operativo del leader ni recalcula el fee. El
        movimiento se registra para trazabilidad pero NO genera asiento: ese
        dinero es del leader, no nuestro, y ya salio del ledger cuando se cobro
        el fee al cliente.
        """
        profile = await self.get_profile(account_login, caller=caller)
        mv_id = str(uuid.uuid4())
        idem = idempotency_key or f"payment-withdrawal-{mv_id}"

        existing = await self.movement_repo.get_by_idempotency_key(idem)
        if existing is not None:
            # Misma key ya usada: se devuelve lo de antes sin volver a llamar.
            return {
                "result": "ALREADY_PROCESSED",
                "master_login": profile.account_login,
                "payment_account_login": profile.payment_account_login,
                "requested_amount": existing.amount,
                "movement_id": existing.id,
            }

        movement = Movement(
            id=mv_id, trader_id=None, direction="PAYMENT_WITHDRAWAL", amount=amount,
            currency=settings.LEDGER_CURRENCY, idempotency_key=idem, status="PENDING",
            mt5_login=profile.payment_account_login, requested_amount=amount,
            description=f"Retiro de la cuenta PAYMENT del leader {profile.account_login}",
        )
        self.movement_repo.add(movement)
        await self.db.commit()

        try:
            data = await self._client.withdraw_from_payment_account(
                master_login=profile.account_login, amount=amount, idempotency_key=idem)
        except ProviderOperationInProgressError as exc:
            movement.status = "FAILED"
            movement.error_detail = (exc.detail or "")[:500]
            await self.db.commit()
            raise
        except Exception as exc:  # noqa: BLE001
            # Con idempotency_key reintentar es seguro, pero hasta saber que paso
            # el movimiento queda marcado para conciliar.
            movement.status = "AMBIGUOUS"
            movement.error_detail = f"{type(exc).__name__}: {str(exc)[:400]}"
            await self.db.commit()
            raise

        movement.status = "COMPLETED"
        movement.provider_result = data.get("result")
        movement.effective_amount = _dec(data.get("requested_amount"), amount)
        movement.balance_before = _opt_dec(data.get("balance_before"))
        movement.balance_after = _opt_dec(data.get("balance_after"))
        await self.db.commit()

        data.setdefault("master_login", profile.account_login)
        data.setdefault("payment_account_login", profile.payment_account_login)
        data.setdefault("requested_amount", amount)
        data["movement_id"] = mv_id
        return data


def _dec(value, fallback: Decimal) -> Decimal:
    out = _opt_dec(value)
    return out if out is not None else Decimal(str(fallback))


def _opt_dec(value) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None
