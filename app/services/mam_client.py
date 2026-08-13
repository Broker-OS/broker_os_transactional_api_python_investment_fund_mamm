"""
Adaptador HTTP al MAM API (spec §11). Solo transporte + mapeo de errores a
AppException; la logica de negocio vive en los services.

DOS CLASES DE OPERACION, y la diferencia importa mucho:

  * Financieras (deposit, withdraw, payment-withdraw) — LLEVAN `idempotency_key`
    (spec §11.1/§12). Reintentar con la MISMA key es seguro: el proveedor
    devuelve el resultado original sin volver a debitar MT5. Por eso van con
    `retry_safe=True` y ante un 5xx se pueden repetir con backoff.

  * Creacion de recursos (cuentas MT5, perfiles de leader, allocations) — NO son
    idempotentes. Spec §12: "Antes de reintentar una creacion despues de un
    timeout, consulte por login o por la pareja leader/follower para evitar
    duplicados". Van con `retry_safe=False`: ante timeout levantan
    ProviderUncertainResultError, que el llamador NO debe reintentar a ciegas.

La API key es de USO EXCLUSIVO DEL BACKEND: nunca al navegador ni a los logs.
Solo se loguea method + path + status; el body puede traer PII, montos y
contrasenas MT5 en claro.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from decimal import Decimal
from typing import Any, Iterable, Optional, Union

import httpx

from app.core.config import settings
from app.core.exceptions import (
    MamConfigError,
    NotFoundError,
    PerfFeeRateInvalidError,
    ProviderApiError,
    ProviderBusinessRuleError,
    ProviderConfigError,
    ProviderForbiddenError,
    ProviderMt5Error,
    ProviderOperationInProgressError,
    ProviderPayloadError,
    ProviderUncertainResultError,
)

logger = logging.getLogger(__name__)

Amount = Union[Decimal, int, str]

# Tipos de timeout: las operaciones que tocan MT5 son mucho mas lentas que una
# consulta, y el provisioning todavia mas.
_READ = "read"
_FINANCIAL = "financial"
_PROVISION = "provision"

_API = "/api/v1"
_MAM = f"{_API}/mam"

# Spec §5/§11.1: los dos unicos perfiles de mascara MT5 que soporta la integracion.
RIGHTS_TRADING_ENABLED = 9073   # 0x2371
RIGHTS_TRADING_DISABLED = 8981  # 0x2315

# Serializacion de decimales como NUMEROS JSON exactos. Spec §3.7: "No use
# numeros de punto flotante binario para contabilidad". Pasar por float haria
# que 0.20 viaje como 0.2000000000000000111 y el fee quede mal en el origen.
#
# La tecnica: `json.dumps` solo sabe emitir Decimal como string, asi que se lo
# envuelve en un marcador y despues se le quitan las comillas al resultado. El
# marcador se genera por proceso y es ASCII puro — si tuviera caracteres de
# control, `json.dumps` los escaparia (\u0000) y la sustitucion no encontraria
# nada, dejando pasar el numero convertido en texto.
_DEC = f"@dec-{uuid.uuid4().hex}@"
_DEC_RE = re.compile(rf'"{re.escape(_DEC)}(-?\d+(?:\.\d+)?){re.escape(_DEC)}"')


def _dumps(payload: Any) -> str:
    """JSON con Decimals emitidos como numeros, sin pasar por float."""

    def _default(obj: Any) -> str:
        if isinstance(obj, Decimal):
            # format 'f' evita notacion exponencial: 1E-8 no es un numero valido
            # para un parser estricto.
            return f"{_DEC}{format(obj, 'f')}{_DEC}"
        raise TypeError(f"No serializable: {type(obj).__name__}")

    raw = json.dumps(payload, default=_default)
    out = _DEC_RE.sub(r"\1", raw)
    if _DEC in out:
        # Un Decimal quedo sin desenvolver: mandarlo asi lo convertiria en un
        # string y el proveedor lo rechazaria (o peor, lo aceptaria mal).
        raise ProviderPayloadError(
            message="No se pudo serializar un valor decimal del payload", detail=None)
    return out


class MamClient:
    def __init__(self) -> None:
        self._base_url = (settings.MAM_API_BASE_URL or "").rstrip("/")

    @staticmethod
    def _headers() -> dict[str, str]:
        api_key = settings.MAM_API_KEY
        if not api_key:
            raise ProviderConfigError(
                message="La integracion con el MAM API no esta configurada",
                detail="Definir MAM_API_KEY en .env",
            )
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # ══════════════════════════════════════════════════════════════════════
    # CUENTAS (spec §11.1)
    # ══════════════════════════════════════════════════════════════════════

    async def list_accounts(
        self, *, status: Optional[str] = None, can_be_leader: Optional[bool] = None,
        can_be_follower: Optional[bool] = None, cursor: Optional[int] = None,
        limit: Optional[int] = None, order: Optional[str] = None,
    ) -> dict:
        """Listado con cursor. Spec §2.4: `user_id` se OMITE en una integracion directa."""
        params = _params(
            status=status, can_be_leader=can_be_leader, can_be_follower=can_be_follower,
            cursor=cursor, limit=limit or settings.MAM_PAGE_SIZE, order=order,
        )
        return await self._request("GET", f"{_MAM}/accounts", params=params)

    async def create_account(
        self, *, first_name: str, last_name: str, name: str, username: str,
        can_be_leader: bool = False, can_be_follower: bool = True,
        platform_group: Optional[str] = None, leverage: Optional[int] = None,
        rights: Optional[int] = None, currency: str = "USD",
        account_mode: Optional[str] = None, status: str = "ACTIVE",
    ) -> dict:
        """Crea la cuenta en MT5 y la registra en MAM (spec §5 paso 1).

        La respuesta trae `password` e `investor_password`: son SECRETOS, hay que
        cifrarlos antes de persistir y no loguearlos nunca.

        `rights` se manda SIEMPRE explicito: si se omite, el servicio usa 1 (0x1),
        que no es ninguno de los dos perfiles administrados por la integracion.
        Ademas es el unico momento en que se pueden fijar — ni `accounts/add` ni
        el PATCH de cuenta permiten cambiarlos despues.
        """
        group = platform_group or settings.MAM_MT5_PLATFORM_GROUP
        self._ensure_group(group)
        mask = rights if rights is not None else settings.MAM_MT5_RIGHTS_TRADING_ENABLED
        self._validate_rights(mask)
        body = {
            "first_name": first_name,
            "last_name": last_name,
            "name": name,
            "username": username,
            "platform_group": group,
            "leverage": leverage or settings.MAM_MT5_DEFAULT_LEVERAGE,
            "rights": mask,
            "currency": currency,
            "account_mode": account_mode or settings.MAM_ACCOUNT_MODE,
            "can_be_leader": can_be_leader,
            "can_be_follower": can_be_follower,
            "status": status,
        }
        # Crear una cuenta MT5 no es idempotente: un reintento a ciegas crea dos.
        return await self._request("POST", f"{_MAM}/accounts/create", body=body,
                                   kind=_PROVISION, retry_safe=False)

    async def add_account(
        self, *, mt5_login: str, name: Optional[str] = None, currency: str = "USD",
        account_mode: Optional[str] = None, can_be_leader: bool = False,
        can_be_follower: bool = True, status: str = "ACTIVE",
    ) -> dict:
        """Registra una cuenta que YA existe en MT5 (spec §11.1).

        No crea el usuario en MT5 ni modifica sus permisos. El integrador debe
        garantizar que el login exista de verdad y pertenezca al servidor
        correcto: registrar un login inventado deja una cuenta ACTIVE que
        ninguna consulta de saldo puede resolver.
        """
        body: dict[str, Any] = {
            "mt5_login": str(mt5_login),
            "currency": currency,
            "account_mode": account_mode or settings.MAM_ACCOUNT_MODE,
            "can_be_leader": can_be_leader,
            "can_be_follower": can_be_follower,
            "status": status,
        }
        if name:
            body["name"] = name
        return await self._request("POST", f"{_MAM}/accounts/add", body=body,
                                   kind=_PROVISION, retry_safe=False)

    async def get_account(self, *, account_login: str) -> dict:
        """Spec §11.1: incluye las credenciales almacenadas. Respuesta ALTAMENTE SENSIBLE."""
        return await self._request("GET", f"{_MAM}/accounts/{account_login}")

    async def update_account(
        self, *, account_login: str, name: Optional[str] = None,
        currency: Optional[str] = None, can_be_leader: Optional[bool] = None,
        can_be_follower: Optional[bool] = None, account_mode: Optional[str] = None,
        status: Optional[str] = None,
    ) -> dict:
        """PATCH parcial. Spec §11.1: los campos requeridos no aceptan null."""
        body = _params(name=name, currency=currency, can_be_leader=can_be_leader,
                       can_be_follower=can_be_follower, account_mode=account_mode,
                       status=status)
        if not body:
            raise ProviderPayloadError(
                message="Debe enviarse al menos un campo editable de la cuenta", detail=None)
        return await self._request("PATCH", f"{_MAM}/accounts/{account_login}", body=body)

    async def get_account_metrics(self, *, account_login: str) -> dict:
        """Balance, equity, margin y free_margin EN VIVO desde MT5 (spec §11.1).

        Un 502 significa que MT5 no pudo responder o no encontro la cuenta; no
        es lo mismo que un 404 del MAM API.
        """
        return await self._request("GET", f"{_MAM}/accounts/{account_login}/metrics")

    async def deposit(self, *, account_login: str, amount: Amount, idempotency_key: str) -> dict:
        """Acredita saldo en MT5 (spec §11.1). La cuenta debe estar activa.

        Spec §12: la key se deriva del id de transaccion del CRM
        (`deposit:<crm_transaction_id>`) y NO se regenera al reintentar.
        """
        return await self._request(
            "POST", f"{_MAM}/accounts/{account_login}/deposit",
            body={"amount": _money(amount),
                  "idempotency_key": _idem(idempotency_key)},
            kind=_FINANCIAL,
        )

    async def withdraw(self, *, account_login: str, amount: Amount, idempotency_key: str) -> dict:
        """Retira saldo DESPUES de cobrar el performance fee pendiente (spec §11.1).

        El monto debe caber en el free margin que queda una vez cobrado el fee;
        si no, la API rechaza la operacion. Por eso conviene conciliar siempre
        lo solicitado contra lo efectivamente retirado.
        """
        return await self._request(
            "POST", f"{_MAM}/accounts/{account_login}/withdraw",
            body={"amount": _money(amount),
                  "idempotency_key": _idem(idempotency_key)},
            kind=_FINANCIAL,
        )

    async def list_balance_transactions(
        self, *, account_login: str, transaction_type: Optional[str] = None,
        status: Optional[str] = None, cursor: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> dict:
        """Historial contable de la cuenta (spec §11.1).

        Para ver un credito de performance fee: login OPERATIVO del master con
        transaction_type=PF_CREDIT y status=EXECUTED. La composicion por investor
        sale de `list_investor_payments`, no de aca.
        """
        params = _params(transaction_type=transaction_type, status=status,
                         cursor=cursor, limit=limit)
        return await self._request(
            "GET", f"{_MAM}/accounts/{account_login}/balance-transactions", params=params)

    # ══════════════════════════════════════════════════════════════════════
    # PERFILES DE LEADER (spec §11.2)
    # ══════════════════════════════════════════════════════════════════════

    async def list_leaders(
        self, *, account_login: Optional[str] = None, status: Optional[str] = None,
        visible_only: Optional[bool] = None, cursor: Optional[int] = None,
        limit: Optional[int] = None, order: Optional[str] = None,
    ) -> dict:
        params = _params(account_login=account_login, status=status, visible_only=visible_only,
                         cursor=cursor, limit=limit or settings.MAM_PAGE_SIZE, order=order)
        return await self._request("GET", f"{_MAM}/leaders", params=params)

    async def create_leader_profile(
        self, *, account_login: str, strategy_name: str,
        description: Optional[str] = None, leaderboard_visibility: bool = False,
        restrict_simultaneous_connections: bool = False,
        min_deposit: Amount = 0, performance_fee_rate: Amount = 0,
        performance_fee_period: str = "MONTHLY",
        propagation_mode: str = "ORIGINAL_ONLY", status: str = "ACTIVE",
        payment_account_login: Optional[str] = None,
    ) -> dict:
        """Crea el perfil que habilita a una cuenta a originar allocations (spec §5 paso 3).

        REGLA CRITICA (spec §2.2): para que la API cree sola la cuenta PAYMENT hay
        que OMITIR `payment_account_login` o mandarlo null. Nunca una cadena
        vacia, ni 0, ni el id interno, ni el login operativo del master — eso no
        equivale a omitir y rompe la creacion automatica.

        El `payment_account_login` que devuelve hay que guardarlo: es dato de
        conciliacion y el PATCH del perfil ya no lo puede cambiar.
        """
        body: dict[str, Any] = {
            "account_login": str(account_login),
            "strategy_name": strategy_name,
            "leaderboard_visibility": leaderboard_visibility,
            "restrict_simultaneous_connections": restrict_simultaneous_connections,
            "min_deposit": _money(min_deposit),
            "performance_fee_rate": _rate(performance_fee_rate),
            "performance_fee_period": performance_fee_period,
            "propagation_mode": propagation_mode,
            "status": status,
        }
        if description:
            body["description"] = description
        # Solo se envia en una migracion expresa de una PAYMENT ya existente.
        if payment_account_login:
            if str(payment_account_login).strip() == str(account_login).strip():
                raise ProviderBusinessRuleError(
                    message="La cuenta PAYMENT debe ser distinta de la cuenta operativa del leader",
                    detail=f"account_login={account_login} == payment_account_login={payment_account_login}",
                )
            body["payment_account_login"] = str(payment_account_login)
        return await self._request("POST", f"{_MAM}/leaders", body=body,
                                   kind=_PROVISION, retry_safe=False)

    async def get_leader_profile(self, *, leader_id: int) -> dict:
        """Spec §11.2: por ID INTERNO de leader, no por login."""
        return await self._request("GET", f"{_MAM}/leaders/{leader_id}")

    async def update_leader_profile(
        self, *, leader_id: int, strategy_name: Optional[str] = None,
        description: Optional[str] = None, leaderboard_visibility: Optional[bool] = None,
        restrict_simultaneous_connections: Optional[bool] = None,
        min_deposit: Optional[Amount] = None, performance_fee_rate: Optional[Amount] = None,
        performance_fee_period: Optional[str] = None, propagation_mode: Optional[str] = None,
        status: Optional[str] = None,
    ) -> dict:
        """PATCH parcial del perfil. La cuenta PAYMENT NO se reasigna por aca.

        Cambiar `min_deposit` aplica a allocations NUEVAS: no cancela las que ya
        existen aunque el follower quede por debajo del nuevo minimo.
        """
        body = _params(
            strategy_name=strategy_name, description=description,
            leaderboard_visibility=leaderboard_visibility,
            restrict_simultaneous_connections=restrict_simultaneous_connections,
            performance_fee_period=performance_fee_period,
            propagation_mode=propagation_mode, status=status,
        )
        if min_deposit is not None:
            body["min_deposit"] = _money(min_deposit)
        if performance_fee_rate is not None:
            body["performance_fee_rate"] = _rate(performance_fee_rate)
        if not body:
            raise ProviderPayloadError(
                message="Debe enviarse al menos un campo editable del perfil", detail=None)
        return await self._request("PATCH", f"{_MAM}/leaders/{leader_id}", body=body)

    # ══════════════════════════════════════════════════════════════════════
    # CUENTA PAYMENT (spec §11.3)
    # ══════════════════════════════════════════════════════════════════════

    async def get_payment_account_balance(self, *, master_login: str) -> dict:
        """Saldo en vivo de la cuenta PAYMENT del leader.

        OJO: `master_login` es SIEMPRE el login de la cuenta OPERATIVA; no se
        pone el login PAYMENT en la URL. Para habilitar retiros hay que usar
        `withdrawable`, que excluye el credito MT5, no `balance`.

        409 = el master no tiene una PAYMENT dedicada valida. 502 = no se pudo
        consultar MT5.
        """
        return await self._request("GET", f"{_MAM}/leaders/{master_login}/payment-account/balance")

    async def withdraw_from_payment_account(
        self, *, master_login: str, amount: Amount, idempotency_key: str,
    ) -> dict:
        """Retira fees ya acreditados en la PAYMENT (spec §11.3).

        No toca el balance operativo del leader ni recalcula el performance fee.
        Repetir exactamente la misma solicitud devuelve result="ALREADY_PROCESSED"
        sin volver a debitar; usar la misma key con OTRO monto devuelve 409.

        La key es obligatoria y debe medir entre 8 y 120 caracteres.
        """
        return await self._request(
            "POST", f"{_MAM}/leaders/{master_login}/payment-account/withdraw",
            body={"amount": _money(amount),
                  "idempotency_key": _idem(idempotency_key, min_len=8)},
            kind=_FINANCIAL,
        )

    # ══════════════════════════════════════════════════════════════════════
    # ALLOCATIONS (spec §11.4)
    # ══════════════════════════════════════════════════════════════════════

    async def list_allocations(
        self, *, leader_login: Optional[str] = None, follower_login: Optional[str] = None,
        status: Optional[str] = None, cursor: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> dict:
        params = _params(leader_login=leader_login, follower_login=follower_login,
                         status=status, cursor=cursor, limit=limit or settings.MAM_PAGE_SIZE)
        return await self._request("GET", f"{_MAM}/allocations", params=params)

    async def check_subscription_eligibility(
        self, *, leader_login: str, follower_login: str,
    ) -> dict:
        """Valida el min_deposit SIN crear la allocation (spec §11.4).

        Tambien verifica que las cuentas existan, esten ACTIVE, usen HEDGING y
        tengan las capacidades correspondientes. Un eligible=false permite pedir
        fondos antes de intentar la suscripcion.

        No reemplaza la validacion del POST: al crear, la API vuelve a consultar
        el balance en MT5 para no trabajar sobre un dato viejo.
        """
        return await self._request(
            "POST", f"{_MAM}/allocations/subscription-eligibility",
            body={"leader_login": str(leader_login), "follower_login": str(follower_login)},
        )

    async def create_allocation(
        self, *, leader_login: str, follower_login: str,
        max_active_leaders_per_follower: int,
        allocation_mode: str = "EQUITY", mode_parameter: Optional[Amount] = None,
        status: str = "PAUSED", equity_stop: Optional[Amount] = None,
        unsubscribe_policy: str = "CLOSE_ON_UNSUBSCRIBE",
        performance_fee_rate: Optional[Amount] = None,
        performance_fee_enabled: bool = True,
    ) -> dict:
        """Conecta dos cuentas (spec §5 paso 6).

        `max_active_leaders_per_follower` es OBLIGATORIO en una integracion
        directa: el motor no guarda ese limite, lo resuelve el integrador desde
        su propio plan y se valida SOLO contra esta solicitud (spec §7.1). Si el
        follower ya tiene tantas allocations vivas como el limite, responde 409.

        `performance_fee_rate=None` hereda la tasa del perfil del leader.

        Se crea en PAUSED por defecto y se activa con un PATCH aparte (spec §13):
        asi hay una oportunidad de verificar la respuesta antes de que empiece a
        copiar operaciones.
        """
        mode = (allocation_mode or "").upper()
        param = self._validate_mode_parameter(mode, mode_parameter)
        body: dict[str, Any] = {
            "leader_login": str(leader_login),
            "follower_login": str(follower_login),
            "status": status,
            "allocation_mode": mode,
            "unsubscribe_policy": unsubscribe_policy,
            "performance_fee_enabled": performance_fee_enabled,
            "max_active_leaders_per_follower": int(max_active_leaders_per_follower),
        }
        if param is not None:
            body["mode_parameter"] = param
        if equity_stop is not None:
            body["equity_stop"] = _money(equity_stop)
        if performance_fee_rate is not None:
            body["performance_fee_rate"] = _rate(performance_fee_rate)
        # Crear una allocation no es idempotente; ante timeout hay que consultar
        # por la pareja leader/follower antes de repetir (spec §12).
        return await self._request("POST", f"{_MAM}/allocations", body=body,
                                   kind=_FINANCIAL, retry_safe=False)

    async def get_allocation(self, *, allocation_id: int) -> dict:
        return await self._request("GET", f"{_MAM}/allocations/{allocation_id}")

    async def update_allocation(
        self, *, allocation_id: int, status: Optional[str] = None,
        allocation_mode: Optional[str] = None, mode_parameter: Optional[Amount] = None,
        equity_stop: Optional[Amount] = None, unsubscribe_policy: Optional[str] = None,
        performance_fee_rate: Optional[Amount] = None,
        performance_fee_enabled: Optional[bool] = None,
    ) -> dict:
        """PATCH parcial (spec §11.4). Al pasar a un estado vivo la API revalida
        cuentas, duplicados y restricciones de conexion simultanea.

        No usar este endpoint para cancelar una allocation activa: hay que ir por
        `unsubscribe`, que aplica la politica y cobra el fee pendiente.
        """
        body = _params(status=status, unsubscribe_policy=unsubscribe_policy,
                       performance_fee_enabled=performance_fee_enabled)
        if allocation_mode is not None:
            mode = allocation_mode.upper()
            body["allocation_mode"] = mode
            param = self._validate_mode_parameter(mode, mode_parameter)
            if param is not None:
                body["mode_parameter"] = param
        elif mode_parameter is not None:
            body["mode_parameter"] = self._validate_mode_parameter(None, mode_parameter)
        if equity_stop is not None:
            body["equity_stop"] = _money(equity_stop)
        if performance_fee_rate is not None:
            body["performance_fee_rate"] = _rate(performance_fee_rate)
        if not body:
            raise ProviderPayloadError(
                message="Debe enviarse al menos un campo editable de la allocation", detail=None)
        return await self._request("PATCH", f"{_MAM}/allocations/{allocation_id}", body=body)

    async def unsubscribe_allocation(self, *, allocation_id: int) -> dict:
        """Termina la relacion aplicando la politica configurada (spec §5 paso 10).

        Con CLOSE_ON_UNSUBSCRIBE la respuesta puede quedar en STOPPING mientras
        se cierran las posiciones: hay que consultar hasta ver CANCELLED, no
        repetir la llamada.
        """
        return await self._request("POST", f"{_MAM}/allocations/{allocation_id}/unsubscribe",
                                   kind=_FINANCIAL, retry_safe=False)

    # ══════════════════════════════════════════════════════════════════════
    # PERFORMANCE FEE (spec §11.3)
    # ══════════════════════════════════════════════════════════════════════

    async def list_perf_fee_transactions(
        self, *, master_login: str, limit: Optional[int] = None,
        cursor: Optional[int] = None, from_at: Optional[str] = None,
        to_at: Optional[str] = None,
    ) -> dict:
        """Creditos PF consolidados del master. `limit` va de 1 a 100 (default 5).

        En creditos agrupados `investor_mt5_login` viene null: una fila puede
        representar pagos de varios investors. Para segregar hay que ir a
        `list_investor_payments`.
        """
        params = _params(master_login=str(master_login), limit=limit,
                         cursor=cursor, from_at=from_at, to_at=to_at)
        return await self._request("GET", f"{_API}/perf-fee/transactions", params=params)

    async def list_investor_payments(
        self, *, master_login: str, run_id: Optional[int] = None,
        limit: Optional[int] = None, cursor: Optional[int] = None,
        from_at: Optional[str] = None, to_at: Optional[str] = None,
    ) -> dict:
        """Detalle POR INVESTOR de cada pago de performance fee (spec §11.3).

        Este es el endpoint de conciliacion para repartir comisiones por investor,
        sponsor o red de IBs. `limit` de 1 a 500 (default 100). `from_at` es
        inclusivo y `to_at` exclusivo.
        """
        params = _params(run_id=run_id, limit=limit, cursor=cursor,
                         from_at=from_at, to_at=to_at)
        return await self._request(
            "GET", f"{_API}/perf-fee/master/{master_login}/investor-payments", params=params)

    # ══════════════════════════════════════════════════════════════════════
    # ANALYTICS (spec §11.5)
    # ══════════════════════════════════════════════════════════════════════

    async def leaders_performance_summary(self, *, account_logins: Iterable[str]) -> dict:
        """Version POST: evita una query URL larga cuando son muchos logins."""
        return await self._request(
            "POST", f"{_MAM}/analytics/leaders/performance-summary/query",
            body={"account_logins": [str(x) for x in account_logins]},
        )

    async def followers_performance_summary(self, *, account_logins: Iterable[str]) -> dict:
        return await self._request(
            "POST", f"{_MAM}/analytics/followers/performance-summary/query",
            body={"account_logins": [str(x) for x in account_logins]},
        )

    async def leader_trade_history(
        self, *, account_login: str, limit: Optional[int] = None, cursor: Optional[int] = None,
    ) -> dict:
        """Historial de trades de la cuenta actuando como leader. `limit` 1..200."""
        return await self._request(
            "GET", f"{_MAM}/analytics/leaders/{account_login}/trade-history",
            params=_params(limit=limit, cursor=cursor))

    async def follower_trade_history(
        self, *, account_login: str, limit: Optional[int] = None, cursor: Optional[int] = None,
    ) -> dict:
        """Operaciones copiadas y resultados de la cuenta actuando como follower."""
        return await self._request(
            "GET", f"{_MAM}/analytics/followers/{account_login}/trade-history",
            params=_params(limit=limit, cursor=cursor))

    async def leader_subscribers(
        self, *, account_login: str, status: Optional[str] = None,
        limit: Optional[int] = None, offset: Optional[int] = None,
    ) -> dict:
        """Followers conectados al leader. OJO: este usa limit/offset, no cursor."""
        return await self._request(
            "GET", f"{_MAM}/analytics/leaders/{account_login}/subscribers",
            params=_params(status=status, limit=limit, offset=offset))

    async def leader_strategy(self, *, account_login: str) -> dict:
        return await self._request("GET", f"{_MAM}/analytics/leaders/{account_login}/strategy")

    # ══════════════════════════════════════════════════════════════════════
    # ELIMINACION DE CUENTAS (spec §11.1)
    # ══════════════════════════════════════════════════════════════════════

    async def master_deletion_impact(self, *, master_login: str) -> dict:
        """Analiza SIN modificar datos. Spec: no crear la operacion sin revisar esto."""
        return await self._request("GET", f"{_MAM}/account-deletions/impact",
                                   params={"master_login": str(master_login)})

    async def create_master_deletion(
        self, *, master_login: str, idempotency_key: str,
        scope: str = "MASTER_ACCOUNT_ONLY", investor_logins: Optional[list[str]] = None,
        transmitted_positions_policy: str = "CLOSE_TRANSMITTED",
        requested_by: Optional[str] = None,
    ) -> dict:
        """Crea la operacion de borrado del master.

        CLOSE_TRANSMITTED es la politica normal: cierra las posiciones copiadas
        antes de purgar. KEEP_OPEN las deja en MT5 bajo gestion manual y fuera
        del seguimiento MAM — solo con esa decision tomada a conciencia.
        """
        body: dict[str, Any] = {
            "master_login": str(master_login),
            "scope": scope,
            "investor_logins": [str(x) for x in (investor_logins or [])],
            "transmitted_positions_policy": transmitted_positions_policy,
            "idempotency_key": _idem(idempotency_key),
        }
        if requested_by:
            body["requested_by"] = requested_by
        return await self._request("POST", f"{_MAM}/account-deletions", body=body,
                                   kind=_PROVISION)

    async def get_master_deletion(self, *, operation_id: str) -> dict:
        return await self._request("GET", f"{_MAM}/account-deletions/{operation_id}")

    async def retry_master_deletion(self, *, operation_id: str) -> dict:
        """Solo para operaciones que quedaron PARTIAL, y despues de corregir la causa."""
        return await self._request("POST", f"{_MAM}/account-deletions/{operation_id}/retry",
                                   kind=_PROVISION)

    async def investor_deletion_impact(self, *, investor_login: str) -> dict:
        """Si la cuenta tambien funciona como master responde IS_ACTIVE_MASTER:
        en ese caso hay que usar el flujo de master, no este."""
        return await self._request("GET", f"{_MAM}/investor-account-deletions/impact",
                                   params={"investor_login": str(investor_login)})

    async def create_investor_deletion(
        self, *, investor_login: str, idempotency_key: str,
        transmitted_positions_policy: str = "CLOSE_TRANSMITTED",
        requested_by: Optional[str] = None,
    ) -> dict:
        body: dict[str, Any] = {
            "investor_login": str(investor_login),
            "transmitted_positions_policy": transmitted_positions_policy,
            "idempotency_key": _idem(idempotency_key),
        }
        if requested_by:
            body["requested_by"] = requested_by
        return await self._request("POST", f"{_MAM}/investor-account-deletions", body=body,
                                   kind=_PROVISION)

    async def get_investor_deletion(self, *, operation_id: str) -> dict:
        return await self._request("GET", f"{_MAM}/investor-account-deletions/{operation_id}")

    async def retry_investor_deletion(self, *, operation_id: str) -> dict:
        return await self._request(
            "POST", f"{_MAM}/investor-account-deletions/{operation_id}/retry", kind=_PROVISION)

    # ══════════════════════════════════════════════════════════════════════
    # WEBHOOK (spec §11.6)
    # ══════════════════════════════════════════════════════════════════════

    async def register_webhook(self, *, name: str, url: str) -> dict:
        """Registra el UNICO destino permitido.

        Un segundo registro devuelve 409. No genera eventos retroactivos: hay que
        registrarlo ANTES de las terminaciones que se quieren recibir.

        La respuesta trae `signing_secret` en texto plano UNA SOLA VEZ. Hay que
        guardarlo en el gestor de secretos en ese mismo momento: es el valor con
        el que se valida X-MAM-Signature y no se puede volver a consultar.
        """
        return await self._request("POST", f"{_MAM}/webhooks",
                                   body={"name": name, "url": url}, kind=_PROVISION)

    # ══════════════════════════════════════════════════════════════════════
    # Paginacion (spec §3.6)
    # ══════════════════════════════════════════════════════════════════════

    @staticmethod
    def iterate_pages(page: dict) -> tuple[list[dict], Optional[int], bool]:
        """Desarma una respuesta paginada: (items, next_cursor, has_more).

        Spec §3.6: "Se debe continuar usando next_cursor mientras has_more sea
        true. No se debe calcular el cursor manualmente".
        """
        items = page.get("items")
        items = [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []
        return items, page.get("next_cursor"), bool(page.get("has_more"))

    async def collect_all(self, fetch, **kwargs) -> list[dict]:
        """Recorre todas las paginas de un listado con cursor.

        El tope de paginas (MAM_MAX_PAGES) evita barrer un listado enorme sin
        querer; si se alcanza, queda registrado en el log en vez de devolver un
        resultado truncado en silencio.
        """
        out: list[dict] = []
        cursor: Optional[int] = None
        for page_no in range(settings.MAM_MAX_PAGES):
            page = await fetch(cursor=cursor, **kwargs)
            items, cursor, has_more = self.iterate_pages(page)
            out.extend(items)
            if not has_more or cursor is None:
                return out
        logger.warning("MAM: se alcanzo el tope de %s paginas en %s; resultado PARCIAL (%s items)",
                       settings.MAM_MAX_PAGES, getattr(fetch, "__name__", "?"), len(out))
        return out

    # ══════════════════════════════════════════════════════════════════════
    # Validaciones locales
    # ══════════════════════════════════════════════════════════════════════

    @staticmethod
    def _ensure_group(group: Optional[str]) -> None:
        """El platform_group lo acuerda el broker; sin el no se puede crear la cuenta."""
        if not (group or "").strip():
            raise MamConfigError(
                message="No se puede crear la cuenta MT5: falta el grupo de la plataforma",
                detail="Definir MAM_MT5_PLATFORM_GROUP en .env (valor acordado con el broker)",
            )

    @staticmethod
    def _validate_rights(mask: int) -> None:
        """Spec §5/§11.1: solo dos perfiles soportados.

        El tipo tecnico admite cualquier entero, pero "no se deben inventar ni
        combinar flags sin validarlos previamente en el servidor MT5 del broker".
        Una mascara mal armada se descubre cuando el cliente ya no puede operar.
        """
        if mask not in (RIGHTS_TRADING_ENABLED, RIGHTS_TRADING_DISABLED):
            raise ProviderPayloadError(
                message="La mascara de permisos MT5 no es uno de los perfiles soportados",
                detail=(f"rights={mask}; la integracion soporta "
                        f"{RIGHTS_TRADING_ENABLED} (trading habilitado) y "
                        f"{RIGHTS_TRADING_DISABLED} (trading deshabilitado)"),
            )

    @staticmethod
    def _validate_mode_parameter(mode: Optional[str], param: Optional[Amount]) -> Optional[Decimal]:
        """Spec §6. FIXED y SCALED exigen el parametro; los otros tres lo tratan
        como multiplicador opcional que por defecto vale 1. En los cinco modos,
        un valor <= 0 es 422."""
        if param is None:
            if mode in ("FIXED", "SCALED"):
                raise ProviderPayloadError(
                    message=f"El modo {mode} exige mode_parameter",
                    detail=("FIXED: cantidad fija de lotes. SCALED: multiplicador del "
                            "lote del leader. Omitirlo produce 422."),
                )
            return None
        value = Decimal(str(param))
        if value <= 0:
            raise ProviderPayloadError(
                message="mode_parameter debe ser estrictamente mayor que 0",
                detail=f"mode_parameter={value}; 0 y los negativos producen 422",
            )
        return value

    # ══════════════════════════════════════════════════════════════════════
    # Transporte
    # ══════════════════════════════════════════════════════════════════════

    def _timeout_for(self, kind: str) -> float:
        return {
            _READ: settings.MAM_TIMEOUT_READ_SECONDS,
            _FINANCIAL: settings.MAM_TIMEOUT_FINANCIAL_SECONDS,
            _PROVISION: settings.MAM_TIMEOUT_PROVISION_SECONDS,
        }.get(kind, settings.MAM_TIMEOUT_READ_SECONDS)

    def _ensure_base_url(self) -> None:
        if not self._base_url:
            raise ProviderConfigError(
                message="La integracion con el MAM API no esta configurada",
                detail="Definir MAM_API_BASE_URL en .env",
            )

    async def _request(
        self, method: str, path: str, *, body: Optional[dict] = None,
        params: Optional[dict] = None, kind: str = _READ, retry_safe: bool = True,
    ) -> dict:
        self._ensure_base_url()
        url = f"{self._base_url}{path}"
        headers = self._headers()
        timeout = self._timeout_for(kind)
        # `content=` en vez de `json=`: el serializador propio emite los Decimals
        # como numeros exactos (ver _dumps).
        content = _dumps(body) if body is not None else None
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.request(method, url, headers=headers,
                                            content=content, params=params)
        except httpx.TimeoutException as exc:
            logger.warning("MAM timeout: %s %s (timeout=%ss, retry_safe=%s)",
                           method, path, timeout, retry_safe)
            raise self._unreachable("El MAM API no respondio a tiempo",
                                    method, path, exc, retry_safe) from exc
        except httpx.HTTPError as exc:
            logger.warning("MAM error de red: %s %s (retry_safe=%s)", method, path, retry_safe)
            raise self._unreachable("Error de comunicacion con el MAM API",
                                    method, path, exc, retry_safe) from exc
        data = self._parse(resp, method, path)
        return data if isinstance(data, dict) else {"data": data}

    @staticmethod
    def _unreachable(message: str, method: str, path: str,
                     exc: Exception, retry_safe: bool) -> ProviderApiError:
        detail = f"{method} {path}: {type(exc).__name__}"
        if retry_safe:
            # Lleva idempotency_key o es de solo lectura: repetir es seguro.
            return ProviderApiError(message=message, detail=detail)
        return ProviderUncertainResultError(
            message=f"{message}. La operacion pudo haberse ejecutado: NO reintentar, conciliar",
            detail=detail,
        )

    @staticmethod
    def _parse(resp: httpx.Response, method: str, path: str) -> Any:
        status = resp.status_code
        # Spec §3.4: el request_id correlaciona el error con los logs del proveedor.
        request_id = resp.headers.get("X-Request-ID")
        logger.info("MAM %s %s -> %s%s", method, path, status,
                    f" (request_id={request_id})" if request_id else "")
        if 200 <= status < 300:
            if status == 204 or not resp.content:
                return {}
            try:
                return resp.json()
            except ValueError:
                return {"raw": resp.text}

        try:
            payload = resp.json()
            raw = payload.get("detail") if isinstance(payload, dict) else None
            msg = raw if raw is not None else resp.text[:200]
        except (ValueError, AttributeError):
            msg = resp.text[:200]
        # En errores de validacion `detail` es una LISTA de campos invalidos.
        if not isinstance(msg, str):
            msg = str(msg)[:400]
        where = f"{method} {path}"
        if request_id:
            where = f"{where} [request_id={request_id}]"

        # Spec §3.5.
        if status == 401:
            raise ProviderConfigError(
                message="El MAM API rechazo la API key",
                detail=f"{where}: {msg}")
        if status == 403:
            raise ProviderForbiddenError(
                message="La API key no tiene acceso a este endpoint del MAM API",
                detail=f"{where}: {msg}")
        if status == 404:
            raise NotFoundError(
                message="El recurso no existe en el MAM API",
                detail=f"{where}: {msg}")
        if status == 409:
            raise ProviderOperationInProgressError(
                message="El MAM API reporta un conflicto de negocio, duplicado o fondos insuficientes",
                detail=f"{where}: {msg}")
        if status == 422:
            raise ProviderPayloadError(
                message="El MAM API rechazo un parametro o una regla de negocio",
                detail=f"{where}: {msg}")
        if status == 400:
            raise ProviderBusinessRuleError(
                message="El MAM API rechazo la operacion por una regla de negocio",
                detail=f"{where}: {msg}")
        if 400 <= status < 500:
            raise ProviderPayloadError(
                message="El MAM API rechazo la solicitud",
                detail=f"{where}: status={status} {msg}")
        if status == 502:
            # No es un fallo del MAM API: es MT5 el que no respondio.
            raise ProviderMt5Error(
                message="El MAM API no pudo ejecutar la operacion en MT5",
                detail=f"{where}: {msg}")
        raise ProviderApiError(
            message="El MAM API respondio con error de servidor",
            detail=f"{where}: status={status}")


# ══════════════════════════════════════════════════════════════════════════
# Helpers de payload
# ══════════════════════════════════════════════════════════════════════════

def _params(**kwargs: Any) -> dict[str, Any]:
    """Descarta los None. Spec §2.2: OMITIR un campo opcional no es lo mismo que
    mandar null, ni que mandar una cadena vacia (eso suele dar 422)."""
    return {k: v for k, v in kwargs.items() if v is not None}


def _money(amount: Amount) -> Decimal:
    """Monto como Decimal exacto; se serializa como numero JSON, nunca como float."""
    try:
        return Decimal(str(amount))
    except (ArithmeticError, ValueError) as exc:
        raise ProviderPayloadError(
            message="El monto no es un numero valido", detail=f"amount={amount!r}") from exc


def _rate(rate: Amount) -> Decimal:
    """Performance fee: decimal entre 0 y 1 (spec §3.7). 0.20 es 20%."""
    try:
        value = Decimal(str(rate))
    except (ArithmeticError, ValueError) as exc:
        raise PerfFeeRateInvalidError(
            message="El performance fee debe ser un numero decimal entre 0 y 1",
            detail=f"rate={rate!r}",
        ) from exc
    if value < 0 or value > 1:
        raise PerfFeeRateInvalidError(
            message="El performance fee debe estar entre 0 y 1 (0.20 = 20%)",
            detail=f"rate={value} fuera de rango; 20 NO significa 20%, significa 2000%",
        )
    return value


def _idem(key: str, *, min_len: int = 1, max_len: int = 120) -> str:
    """Spec §11.3: la key del retiro PAYMENT debe medir entre 8 y 120 caracteres.

    Se valida aca y no en el server para no gastar un round-trip en un 422 que
    ya sabemos que va a pasar.
    """
    value = (key or "").strip()
    if len(value) < min_len or len(value) > max_len:
        raise ProviderPayloadError(
            message="La idempotency_key no tiene un largo valido",
            detail=f"largo={len(value)}; se esperaba entre {min_len} y {max_len}",
        )
    return value


_client: Optional[MamClient] = None


def get_mam_client() -> MamClient:
    global _client
    if _client is None:
        _client = MamClient()
    return _client
