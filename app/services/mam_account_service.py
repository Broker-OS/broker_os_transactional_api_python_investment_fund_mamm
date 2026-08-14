"""
Cuentas MT5 del motor MAM (spec §5 paso 1, §11.1).

Dos caminos de alta y una diferencia que importa:

  create -> POST /mam/accounts/create   crea la cuenta EN MT5 y la registra.
                                        Es el unico momento en que se pueden
                                        fijar los `rights`.
  add    -> POST /mam/accounts/add      registra una cuenta que YA existe. No
                                        crea el usuario ni toca sus permisos.

ORDEN DE LAS VALIDACIONES: todo lo verificable antes de llamar al proveedor se
verifica antes. Crear una cuenta MT5 no es idempotente ni reversible — si
fallamos despues de crearla queda una cuenta huerfana en el servidor del broker
que solo se limpia por el flujo asincrono de borrado. Por eso se chequea el
cifrado, la unicidad del login y el cliente ANTES de tocar la API; y si aun asi
falla el guardado local, el mt5_login se loguea en ERROR para poder recuperarlo.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import crypto
from app.core.config import settings
from app.core.exceptions import (
    AccountNotFoundOrForbiddenError,
    MamAccountAlreadyExistsError,
    Mt5CredentialsEncryptionError,
    Mt5CredentialsUnavailableError,
    PaymentAccountRoleError,
    TraderNotFoundError,
)
from app.models.api_user import ROLE_ADMIN
from app.models.mam import ACCOUNT_ACTIVE, MamAccount
from app.models.trader import Trader
from app.repositories.mam_repository import MamRepository
from app.repositories.trader_repository import TraderRepository
from app.services.mam_client import get_mam_client

logger = logging.getLogger(__name__)


class MamAccountService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = MamRepository(db)
        self.trader_repo = TraderRepository(db)
        self._client = get_mam_client()

    # ══════════════════════════════════════════════════════════════════
    # helpers
    # ══════════════════════════════════════════════════════════════════

    @staticmethod
    def _ensure_can_store_secrets() -> None:
        """Sin cifrado no se crea la cuenta.

        Falla cerrado a proposito: si creamos la cuenta y despues no podemos
        guardar la contrasena, queda una cuenta MT5 real cuyas credenciales no
        volvemos a ver — el detalle del proveedor las devuelve, pero recuperarlas
        supone saber que la cuenta existe, y justamente eso es lo que se perdio.
        """
        if not crypto.is_configured():
            raise Mt5CredentialsEncryptionError(
                message="No se pueden crear cuentas MT5: falta configurar el cifrado de credenciales",
                detail="Definir MT5_CREDENTIALS_ENCRYPTION_KEY en .env",
            )

    @staticmethod
    def _group_for(can_be_leader: bool) -> str:
        """Grupo MT5 con el que nace la cuenta.

        El grupo se fija SOLO al crear: no se puede mover la cuenta despues. Por
        eso se elige por la capacidad con la que nace, no por la que tenga mas
        adelante — una cuenta que arranca como follower y luego se habilita como
        leader se queda en el grupo de follower, y eso es correcto: cambiarla de
        grupo no esta en nuestras manos.

        Si el broker define un grupo unico para MAM, MAM_MT5_PLATFORM_GROUP lo
        cubre todo y los dos especificos quedan vacios.
        """
        especifico = (settings.MAM_MT5_GROUP_LEADER if can_be_leader
                      else settings.MAM_MT5_GROUP_FOLLOWER)
        return (especifico or "").strip() or settings.MAM_MT5_PLATFORM_GROUP

    async def _resolve_trader(self, external_reference: Optional[str], *,
                             caller=None) -> Optional[Trader]:
        if not external_reference:
            # Cuenta sin cliente: estrategia propia del broker.
            return None
        tr = await self.trader_repo.get_by_external_reference(external_reference)
        if tr is None:
            raise TraderNotFoundError(message="El cliente no existe",
                                      detail=f"external_reference={external_reference}")
        if caller is not None and caller.role != ROLE_ADMIN and tr.owner_api_user_id != caller.id:
            raise TraderNotFoundError(message="El cliente no existe",
                                      detail=f"external_reference={external_reference}")
        return tr

    async def _ensure_login_free(self, mt5_login: str) -> None:
        if await self.repo.login_is_taken(mt5_login):
            raise MamAccountAlreadyExistsError(
                message="El login MT5 ya esta registrado en este servicio",
                detail=f"mt5_login={mt5_login}")

    async def get_account(self, mt5_login: str, *, caller=None) -> MamAccount:
        """Resuelve la cuenta validando propiedad.

        Si la cuenta es de otro cliente se responde el mismo "no existe" que para
        un login inventado: distinguirlos permitiria enumerar cuentas ajenas, y
        el detalle de cuenta expone credenciales MT5.
        """
        acc = await self.repo.get_account_by_login(mt5_login)
        if acc is None:
            raise AccountNotFoundOrForbiddenError(
                message="La cuenta no existe", detail=f"mt5_login={mt5_login}")
        if caller is not None and caller.role != ROLE_ADMIN:
            if acc.trader_id is None:
                raise AccountNotFoundOrForbiddenError(
                    message="La cuenta no existe", detail=f"mt5_login={mt5_login}")
            trader = await self.trader_repo.get_by_id(acc.trader_id)
            if trader is None or trader.owner_api_user_id != caller.id:
                raise AccountNotFoundOrForbiddenError(
                    message="La cuenta no existe", detail=f"mt5_login={mt5_login}")
        return acc

    async def _persist(self, acc: MamAccount, *, what: str) -> MamAccount:
        """Guarda la cuenta; si el commit falla deja rastro del login creado."""
        self.repo.add(acc)
        try:
            await self.db.commit()
        except Exception:
            await self.db.rollback()
            # La cuenta YA existe del lado del proveedor: sin este log se pierde.
            logger.error(
                "MAM: la cuenta %s quedo creada en el proveedor pero NO se pudo guardar "
                "localmente. Recuperarla con GET /mam/accounts/%s antes de reintentar.",
                acc.mt5_login, acc.mt5_login)
            raise
        await self.db.refresh(acc)
        logger.info("MAM: cuenta %s %s", acc.mt5_login, what)
        return acc

    # ══════════════════════════════════════════════════════════════════
    # Alta
    # ══════════════════════════════════════════════════════════════════

    async def create_account(
        self, *, external_reference: Optional[str], first_name: str, last_name: str,
        name: str, username: str, can_be_leader: bool = False, can_be_follower: bool = True,
        rights_profile: str = "TRADING_ENABLED", leverage: Optional[int] = None,
        currency: str = "USD", platform_group: Optional[str] = None, caller=None,
    ) -> MamAccount:
        """Crea la cuenta en MT5 y la registra en MAM."""
        self._ensure_can_store_secrets()
        trader = await self._resolve_trader(external_reference, caller=caller)

        rights = (settings.MAM_MT5_RIGHTS_TRADING_ENABLED
                  if rights_profile == "TRADING_ENABLED"
                  else settings.MAM_MT5_RIGHTS_TRADING_DISABLED)
        group = platform_group or self._group_for(can_be_leader)

        data = await self._client.create_account(
            first_name=first_name, last_name=last_name, name=name, username=username,
            can_be_leader=can_be_leader, can_be_follower=can_be_follower,
            platform_group=group, leverage=leverage, rights=rights,
            currency=currency,
        )
        mt5_login = str(data.get("mt5_login") or "").strip()
        if not mt5_login:
            raise MamAccountAlreadyExistsError(
                message="El proveedor no devolvio el login de la cuenta creada",
                detail=f"respuesta sin mt5_login: {sorted(data)[:12]}")

        acc = MamAccount(
            trader_id=trader.id if trader else None,
            mt5_login=mt5_login,
            provider_account_id=_as_int(data.get("id")),
            name=data.get("name") or name,
            currency=data.get("currency") or currency,
            account_mode=data.get("account_mode") or settings.MAM_ACCOUNT_MODE,
            status=data.get("status") or ACCOUNT_ACTIVE,
            can_be_leader=bool(data.get("can_be_leader", can_be_leader)),
            can_be_follower=bool(data.get("can_be_follower", can_be_follower)),
            # Si el motor no lo devuelve, el respaldo es `group`: el valor que se
            # le mando. El motor NO devuelve platform_group al CONSULTAR una
            # cuenta, asi que este registro es el unico lugar donde queda en que
            # grupo cayo — un respaldo vacio lo perderia para siempre.
            platform_group=data.get("platform_group") or group,
            leverage=_as_int(data.get("leverage")) or leverage or settings.MAM_MT5_DEFAULT_LEVERAGE,
            # `rights` efectivamente aplicado, que puede diferir del pedido.
            rights=_as_int(data.get("rights")) or rights,
            mt5_server=settings.MAM_MT5_SERVER or None,
            mt5_password_enc=crypto.encrypt(data.get("password")),
            mt5_investor_password_enc=crypto.encrypt(data.get("investor_password")),
        )
        return await self._persist(acc, what="creada en MT5 y registrada")

    async def register_account(
        self, *, external_reference: Optional[str], mt5_login: str,
        name: Optional[str] = None, currency: str = "USD",
        can_be_leader: bool = False, can_be_follower: bool = True, caller=None,
    ) -> tuple[MamAccount, Optional[dict]]:
        """Registra en MAM una cuenta que ya existe en MT5.

        Devuelve la cuenta y sus metricas en vivo. La consulta de metricas es la
        comprobacion de que el login existe DE VERDAD: el alta acepta cualquier
        numero y responde 200, asi que un login mal tipeado quedaria registrado
        como ACTIVE para siempre, sin saldo consultable. Si MT5 no lo resuelve,
        se devuelve `metrics=None` y el llamador ve que algo no cierra.
        """
        await self._ensure_login_free(mt5_login)
        trader = await self._resolve_trader(external_reference, caller=caller)

        data = await self._client.add_account(
            mt5_login=mt5_login, name=name, currency=currency,
            can_be_leader=can_be_leader, can_be_follower=can_be_follower,
        )
        acc = MamAccount(
            trader_id=trader.id if trader else None,
            mt5_login=str(data.get("mt5_login") or mt5_login),
            provider_account_id=_as_int(data.get("id")),
            name=data.get("name") or name,
            currency=data.get("currency") or currency,
            account_mode=data.get("account_mode") or settings.MAM_ACCOUNT_MODE,
            status=data.get("status") or ACCOUNT_ACTIVE,
            can_be_leader=bool(data.get("can_be_leader", can_be_leader)),
            can_be_follower=bool(data.get("can_be_follower", can_be_follower)),
            mt5_server=settings.MAM_MT5_SERVER or None,
        )
        acc = await self._persist(acc, what="registrada (ya existia en MT5)")

        metrics = None
        try:
            metrics = await self._client.get_account_metrics(account_login=acc.mt5_login)
        except Exception as exc:  # noqa: BLE001
            # No se aborta: la cuenta ya quedo registrada del lado del proveedor.
            logger.warning(
                "MAM: la cuenta %s se registro pero MT5 no devolvio metricas (%s). "
                "Verificar que el login exista en el servidor del broker.",
                acc.mt5_login, type(exc).__name__)
        return acc, metrics

    async def import_account(
        self, *, external_reference: Optional[str], mt5_login: str, caller=None,
    ) -> tuple[MamAccount, Optional[dict]]:
        """Trae a nuestra base una cuenta que YA esta registrada en el motor MAM.

        No es lo mismo que registrar: `accounts/add` da 409 sobre una cuenta que
        el motor ya conoce. Este camino existe porque el ambiente del proveedor
        casi nunca arranca vacio — hay cuentas creadas antes de la integracion, o
        por otro sistema, y sin esto quedarian inalcanzables desde aca para
        siempre.

        Los valores se toman del proveedor, no del que llama: el motor es la
        fuente de verdad sobre las capacidades y el estado de la cuenta.
        """
        await self._ensure_login_free(mt5_login)
        trader = await self._resolve_trader(external_reference, caller=caller)

        data = await self._client.get_account(account_login=mt5_login)

        acc = MamAccount(
            trader_id=trader.id if trader else None,
            mt5_login=str(data.get("mt5_login") or mt5_login),
            provider_account_id=_as_int(data.get("id")),
            name=data.get("name"),
            currency=data.get("currency") or "USD",
            account_mode=data.get("account_mode") or settings.MAM_ACCOUNT_MODE,
            status=data.get("status") or ACCOUNT_ACTIVE,
            can_be_leader=bool(data.get("can_be_leader", False)),
            can_be_follower=bool(data.get("can_be_follower", True)),
            platform_group=data.get("platform_group"),
            leverage=_as_int(data.get("leverage")),
            rights=_as_int(data.get("rights")),
            mt5_server=settings.MAM_MT5_SERVER or None,
        )
        # Spec §11.1: el detalle "incluye las credenciales almacenadas cuando
        # existen". Si vienen, se cifran ahora; si no hay clave configurada se
        # importa igual pero sin credenciales, porque la cuenta ya existe y
        # bloquear el import no protege nada.
        if crypto.is_configured():
            acc.mt5_password_enc = crypto.encrypt(data.get("password"))
            acc.mt5_investor_password_enc = crypto.encrypt(data.get("investor_password"))
        elif data.get("password"):
            logger.warning(
                "MAM: la cuenta %s se importo SIN guardar sus credenciales "
                "(falta MT5_CREDENTIALS_ENCRYPTION_KEY).", mt5_login)

        acc = await self._persist(acc, what="importada desde el motor")

        metrics = None
        try:
            metrics = await self._client.get_account_metrics(account_login=acc.mt5_login)
        except Exception as exc:  # noqa: BLE001
            logger.warning("MAM: la cuenta %s se importo pero MT5 no devolvio metricas (%s)",
                           acc.mt5_login, type(exc).__name__)
        return acc, metrics

    # ══════════════════════════════════════════════════════════════════
    # Consulta y edicion
    # ══════════════════════════════════════════════════════════════════

    async def list_accounts(
        self, *, caller=None, external_reference: Optional[str] = None,
        can_be_leader: Optional[bool] = None, can_be_follower: Optional[bool] = None,
        status: Optional[str] = None, page: int = 1, limit: int = 20,
    ):
        trader = await self._resolve_trader(external_reference, caller=caller)
        owner = None if (caller is None or caller.role == ROLE_ADMIN) else caller.id
        return await self.repo.list_accounts(
            trader_id=trader.id if trader else None, owner_api_user_id=owner,
            can_be_leader=can_be_leader, can_be_follower=can_be_follower,
            status=status, page=page, limit=limit)

    async def get_metrics(self, *, mt5_login: str, caller=None) -> dict:
        """Balance, equity, margin y free margin EN VIVO desde MT5."""
        acc = await self.get_account(mt5_login, caller=caller)
        data = await self._client.get_account_metrics(account_login=acc.mt5_login)
        data.setdefault("mt5_login", acc.mt5_login)
        data.setdefault("currency", acc.currency)
        return data

    async def get_credentials(self, *, mt5_login: str, caller=None) -> dict:
        """Credenciales MT5 en claro. Solo para entregarselas al titular."""
        acc = await self.get_account(mt5_login, caller=caller)
        password = crypto.decrypt(acc.mt5_password_enc)
        investor = crypto.decrypt(acc.mt5_investor_password_enc)
        if password is None and investor is None:
            # Pasa con cuentas registradas (no creadas) por este servicio: nunca
            # tuvimos sus contrasenas.
            raise Mt5CredentialsUnavailableError(
                message="No hay credenciales MT5 guardadas para esa cuenta",
                detail=("La cuenta fue registrada, no creada por este servicio. "
                        "Las credenciales las tiene el broker."),
            )
        return {
            "mt5_login": acc.mt5_login,
            "mt5_server": acc.mt5_server,
            "password": password,
            "investor_password": investor,
        }

    async def update_account(
        self, *, mt5_login: str, name: Optional[str] = None,
        can_be_leader: Optional[bool] = None, can_be_follower: Optional[bool] = None,
        status: Optional[str] = None, external_reference: Optional[str] = None,
        caller=None,
    ) -> MamAccount:
        """Actualiza capacidades, estado o el cliente dueño.

        Las capacidades se aplican PRIMERO en el motor: si guardaramos antes, un
        rechazo del proveedor nos dejaria diciendo que la cuenta puede ser leader
        cuando en realidad no.

        El cliente dueño, en cambio, es un dato SOLO NUESTRO: el motor no conoce
        clientes (spec §2). Por eso ese cambio no viaja a ningun lado. Hace falta
        sobre todo para las cuentas importadas, que llegan sin dueño y no podrian
        mover capital sin el — el libro contable registra el capital por cliente.
        """
        acc = await self.get_account(mt5_login, caller=caller)
        if await self.repo.is_payment_account(acc.mt5_login):
            raise PaymentAccountRoleError(
                message="Esa cuenta es la PAYMENT de un leader y no se opera directamente",
                detail=f"mt5_login={acc.mt5_login}")

        # Solo se llama al motor si hay algo que le corresponda.
        if any(v is not None for v in (name, can_be_leader, can_be_follower, status)):
            await self._client.update_account(
                account_login=acc.mt5_login, name=name, can_be_leader=can_be_leader,
                can_be_follower=can_be_follower, status=status)

        if external_reference is not None:
            trader = await self._resolve_trader(external_reference, caller=caller)
            acc.trader_id = trader.id if trader else None
        if name is not None:
            acc.name = name
        if can_be_leader is not None:
            acc.can_be_leader = can_be_leader
        if can_be_follower is not None:
            acc.can_be_follower = can_be_follower
        if status is not None:
            acc.status = status
        await self.db.commit()
        await self.db.refresh(acc)
        return acc

    async def has_leader_profile(self, account_id: str) -> bool:
        return await self.repo.get_profile_by_account_id(account_id) is not None


def _as_int(value: Any) -> Optional[int]:
    """Los ids y numeros del proveedor a veces llegan como string."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
