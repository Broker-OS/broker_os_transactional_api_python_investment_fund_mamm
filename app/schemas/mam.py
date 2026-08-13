"""Schemas del motor MAM: cuentas, perfiles de leader y cuenta PAYMENT."""
from datetime import datetime
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

# Spec §4.1: sin HEDGING la cuenta no puede participar en copy trading.
AccountMode = Literal["HEDGING", "NETTING"]
AccountStatus = Literal["ACTIVE", "INACTIVE", "DELETED"]
FeePeriod = Literal["OFF", "MINUTELY", "HOURLY", "DAILY", "WEEKLY", "SEMIMONTHLY", "MONTHLY"]
PropagationMode = Literal["ORIGINAL_ONLY", "CASCADE"]
RightsProfile = Literal["TRADING_ENABLED", "TRADING_DISABLED"]


# ══════════════════════════════════════════════════════════════════════
# Cuentas
# ══════════════════════════════════════════════════════════════════════

class MamAccountCreateRequest(BaseModel):
    """Crea la cuenta en MT5 y la registra en MAM (spec §5, paso 1)."""

    external_reference: Optional[str] = Field(
        default=None,
        description=("Cliente dueño de la cuenta. Si se omite, la cuenta queda sin "
                     "cliente asociado (estrategia propia del broker)."))
    first_name: str = Field(max_length=120, description="MT5 exige nombre del titular.")
    last_name: str = Field(max_length=120)
    name: str = Field(max_length=160, description="Nombre visible de la cuenta.")
    username: str = Field(max_length=255, description="Identificador del titular en MT5 (suele ser su email).")
    can_be_leader: bool = Field(
        default=False,
        description="Autoriza a la cuenta a ORIGINAR operaciones. No basta: además necesita perfil de leader.")
    can_be_follower: bool = Field(
        default=True, description="Autoriza a la cuenta a RECIBIR operaciones de otra.")
    rights_profile: RightsProfile = Field(
        default="TRADING_ENABLED",
        description=("Máscara de permisos MT5. `TRADING_ENABLED` (9073) o `TRADING_DISABLED` "
                     "(8981). Solo se puede fijar al crear: ni el registro ni la edición "
                     "posterior la cambian."))
    leverage: Optional[int] = Field(default=None, ge=1)
    currency: str = Field(default="USD", max_length=10)
    platform_group: Optional[str] = Field(
        default=None, description="Grupo MT5. Si se omite se usa el configurado en el servicio.")

    model_config = ConfigDict(json_schema_extra={"example": {
        "external_reference": "438434273005",
        "first_name": "Ana", "last_name": "García",
        "name": "Cuenta de Ana", "username": "ana@example.com",
        "can_be_leader": False, "can_be_follower": True,
        "rights_profile": "TRADING_ENABLED", "leverage": 100, "currency": "USD",
    }})


class MamAccountRegisterRequest(BaseModel):
    """Registra en MAM una cuenta que YA existe en MT5 (spec §11.1).

    No crea el usuario en MT5 ni toca sus permisos: el `rights` de la cuenta
    queda como esté.
    """

    external_reference: Optional[str] = None
    mt5_login: str = Field(max_length=40)
    name: Optional[str] = Field(default=None, max_length=160)
    currency: str = Field(default="USD", max_length=10)
    can_be_leader: bool = False
    can_be_follower: bool = True

    model_config = ConfigDict(json_schema_extra={"example": {
        "external_reference": "438434273005",
        "mt5_login": "146502", "name": "Cuenta de Ana",
        "currency": "USD", "can_be_leader": False, "can_be_follower": True,
    }})


class MamAccountImportRequest(BaseModel):
    """Trae una cuenta que el motor MAM ya tiene registrada.

    Las capacidades y el estado se leen del motor, no se envían: ahí está la
    fuente de verdad.
    """

    external_reference: Optional[str] = None
    mt5_login: str = Field(max_length=40)

    model_config = ConfigDict(json_schema_extra={"example": {"mt5_login": "7918229"}})


class MamAccountUpdateRequest(BaseModel):
    """Cambia capacidades, estado o el cliente dueño. Enviar solo lo que se modifica."""

    name: Optional[str] = Field(default=None, max_length=160)
    can_be_leader: Optional[bool] = None
    can_be_follower: Optional[bool] = None
    status: Optional[AccountStatus] = None
    external_reference: Optional[str] = Field(
        default=None,
        description=("Cliente dueño de la cuenta. Es un dato **solo de este servicio** — el "
                     "motor no conoce clientes. Hace falta sobre todo en cuentas importadas, "
                     "que llegan sin dueño y sin él no pueden mover capital."))

    model_config = ConfigDict(json_schema_extra={"example": {"can_be_leader": True}})


class MamAccountRead(BaseModel):
    id: str
    trader_id: Optional[str] = None
    mt5_login: str
    name: Optional[str] = None
    currency: str
    account_mode: str
    status: str
    can_be_leader: bool
    can_be_follower: bool
    platform_group: Optional[str] = None
    leverage: Optional[int] = None
    rights: Optional[int] = None
    mt5_server: Optional[str] = None
    has_leader_profile: bool = False
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class MamAccountCredentialsRead(BaseModel):
    """Credenciales MT5 en claro. Tratar como dato ALTAMENTE SENSIBLE."""

    mt5_login: str
    mt5_server: Optional[str] = None
    password: Optional[str] = None
    investor_password: Optional[str] = None


class MamAccountMetricsRead(BaseModel):
    """Datos EN VIVO de MT5 (spec §11.1). No salen de nuestra base."""

    mt5_login: str
    balance: Optional[Decimal] = None
    equity: Optional[Decimal] = None
    margin: Optional[Decimal] = None
    free_margin: Optional[Decimal] = None
    currency: Optional[str] = None


class MamAccountListResponse(BaseModel):
    total: int
    page: int
    limit: int
    pages: int
    items: list[MamAccountRead]


# ══════════════════════════════════════════════════════════════════════
# Perfiles de leader
# ══════════════════════════════════════════════════════════════════════

class LeaderProfileCreateRequest(BaseModel):
    """Convierte una cuenta en estrategia seguible (spec §5, pasos 2 y 3).

    Si la cuenta todavía no tiene `can_be_leader`, se habilita antes de crear el
    perfil: son dos llamadas al motor, pero un solo paso desde afuera.
    """

    account_login: str = Field(max_length=40)
    strategy_name: str = Field(max_length=160)
    description: Optional[str] = None
    leaderboard_visibility: bool = Field(
        default=False, description="Si la estrategia aparece en el ranking público.")
    restrict_simultaneous_connections: bool = Field(
        default=False,
        description=("Si es `true`, esta estrategia no acepta clientes que ya estén "
                     "conectados a otra."))
    min_deposit: Decimal = Field(
        default=Decimal("0"), ge=0,
        description=("Balance MT5 mínimo que debe tener un cliente para suscribirse. "
                     "0 desactiva la restricción. Se valida solo contra el balance: "
                     "equity, crédito y free margin no cuentan."))
    performance_fee_rate: Decimal = Field(
        default=Decimal("0"), ge=0, le=1,
        description="Tasa entre 0 y 1. `0.20` es 20 %. Mandar `20` sería 2000 %.")
    performance_fee_period: FeePeriod = "MONTHLY"
    propagation_mode: PropagationMode = Field(
        default="ORIGINAL_ONLY",
        description=("`ORIGINAL_ONLY`: solo propaga lo que abre esta cuenta. `CASCADE`: "
                     "también propaga lo que recibe de un leader superior."))
    payment_account_login: Optional[str] = Field(
        default=None,
        description=("**Dejar vacío.** El motor crea sola la cuenta PAYMENT que recibirá "
                     "los fees. Solo se envía para migrar una PAYMENT que ya existe."))

    model_config = ConfigDict(json_schema_extra={"example": {
        "account_login": "139682",
        "strategy_name": "Momentum Global",
        "description": "Estrategia diversificada de seguimiento de tendencia.",
        "leaderboard_visibility": False,
        "restrict_simultaneous_connections": False,
        "min_deposit": 1000,
        "performance_fee_rate": 0.20,
        "performance_fee_period": "MONTHLY",
        "propagation_mode": "ORIGINAL_ONLY",
    }})


class LeaderProfileUpdateRequest(BaseModel):
    """Enviar solo lo que se quiere cambiar. La cuenta PAYMENT no se reasigna acá."""

    strategy_name: Optional[str] = Field(default=None, max_length=160)
    description: Optional[str] = None
    leaderboard_visibility: Optional[bool] = None
    restrict_simultaneous_connections: Optional[bool] = None
    min_deposit: Optional[Decimal] = Field(default=None, ge=0)
    performance_fee_rate: Optional[Decimal] = Field(default=None, ge=0, le=1)
    performance_fee_period: Optional[FeePeriod] = None
    propagation_mode: Optional[PropagationMode] = None
    status: Optional[Literal["ACTIVE", "INACTIVE"]] = None
    note: Optional[str] = Field(
        default=None, max_length=500,
        description="Motivo del cambio. Queda en la auditoría junto a quién lo hizo.")

    model_config = ConfigDict(json_schema_extra={"example": {
        "performance_fee_rate": 0.25, "note": "Ajuste acordado con el leader",
    }})


class LeaderProfileRead(BaseModel):
    id: str
    leader_id: Optional[int] = None
    account_id: str
    account_login: str
    payment_account_login: Optional[str] = None
    strategy_name: Optional[str] = None
    description: Optional[str] = None
    leaderboard_visibility: bool
    restrict_simultaneous_connections: bool
    min_deposit: Decimal
    performance_fee_rate: Decimal
    performance_fee_period: str
    propagation_mode: str
    status: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class LeaderProfileListResponse(BaseModel):
    total: int
    page: int
    limit: int
    pages: int
    items: list[LeaderProfileRead]


# ══════════════════════════════════════════════════════════════════════
# Capital
# ══════════════════════════════════════════════════════════════════════

class CapitalOperationRequest(BaseModel):
    amount: Decimal = Field(gt=0, description="Monto en la moneda del libro. Se trunca a 2 decimales.")
    idempotency_key: Optional[str] = Field(
        default=None, max_length=120,
        description=("Derivala del id de la transacción de tu sistema (`deposit:1234`) y "
                     "**no la regeneres al reintentar**: con la misma key el motor no "
                     "duplica el movimiento. Si se omite, el servicio genera una."))

    model_config = ConfigDict(json_schema_extra={"example": {
        "amount": 500, "idempotency_key": "deposit:crm-84922"}})


class CapitalMovementRead(BaseModel):
    id: str
    direction: str
    status: str
    mt5_login: Optional[str] = None
    requested_amount: Optional[Decimal] = None
    effective_amount: Optional[Decimal] = Field(
        default=None, description="Lo que el motor movió realmente.")
    perf_fee_at_request: Optional[Decimal] = Field(
        default=None,
        description=("Performance fee cobrado en el mismo acto del retiro. **No vuelve a la "
                     "cuenta maestra**: se acredita en la cuenta PAYMENT del leader."))
    balance_before: Optional[Decimal] = None
    balance_after: Optional[Decimal] = None
    mt5_deal_id: Optional[int] = None
    provider_result: Optional[str] = Field(
        default=None,
        description="`ALREADY_PROCESSED` si el motor reconoció la key y no volvió a debitar.")
    ledger_tx_id: Optional[str] = None
    idempotency_key: str
    error_detail: Optional[str] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ══════════════════════════════════════════════════════════════════════
# Allocations (suscripciones)
# ══════════════════════════════════════════════════════════════════════

AllocationMode = Literal["FIXED", "SCALED", "EQUITY", "EQUITY_ROUND_DOWN", "BALANCE"]
AllocationStatus = Literal["PAUSED", "ACTIVE", "STOPPING", "CANCELLED", "ERROR"]
UnsubscribePolicy = Literal["KEEP_OPEN", "CLOSE_ON_UNSUBSCRIBE"]


class EligibilityRequest(BaseModel):
    leader_login: str = Field(max_length=40)
    follower_login: str = Field(max_length=40)

    model_config = ConfigDict(json_schema_extra={"example": {
        "leader_login": "139682", "follower_login": "146502"}})


class EligibilityRead(BaseModel):
    eligible: bool
    leader_login: Optional[str] = None
    follower_login: Optional[str] = None
    follower_balance: Optional[Decimal] = Field(
        default=None,
        description="Balance MT5 del cliente. `null` si el mínimo es 0 y no hizo falta consultarlo.")
    min_deposit: Optional[Decimal] = None
    max_active_leaders: Optional[int] = Field(
        default=None, description="Cupo que autoriza el plan del cliente.")
    live_allocations: Optional[int] = Field(
        default=None, description="Suscripciones vivas que ya tiene (ACTIVE, PAUSED o STOPPING).")
    quota_available: Optional[bool] = None
    quota_reason: Optional[str] = None


class AllocationCreateRequest(BaseModel):
    leader_login: str = Field(max_length=40, description="Cuenta que ORIGINA las operaciones.")
    follower_login: str = Field(max_length=40, description="Cuenta que las RECIBE.")
    allocation_mode: AllocationMode = Field(
        default="EQUITY",
        description=("Cómo se calcula el volumen que abre el cliente. `EQUITY` copia la "
                     "proporción entre equities; `BALANCE`, entre balances; `SCALED` "
                     "multiplica el lote del leader; `FIXED` usa un lote fijo."))
    mode_parameter: Optional[Decimal] = Field(
        default=None, gt=0,
        description=("**Obligatorio en `FIXED`** (lotes fijos) **y `SCALED`** (multiplicador). "
                     "En `EQUITY`, `EQUITY_ROUND_DOWN` y `BALANCE` es un multiplicador "
                     "opcional: omitirlo equivale a 1. Siempre mayor que 0."))
    equity_stop: Optional[Decimal] = Field(
        default=None, ge=0,
        description=("Equity mínimo del cliente. Si cae hasta ahí, la suscripción se detiene "
                     "y sus operaciones copiadas se cierran. Omitir lo desactiva."))
    unsubscribe_policy: UnsubscribePolicy = Field(
        default="CLOSE_ON_UNSUBSCRIBE",
        description=("Qué pasa con las posiciones abiertas al darse de baja. "
                     "`CLOSE_ON_UNSUBSCRIBE` las cierra; `KEEP_OPEN` las deja abiertas en "
                     "MT5 fuera de la gestión del motor."))
    performance_fee_rate: Optional[Decimal] = Field(
        default=None, ge=0, le=1,
        description="Entre 0 y 1. Si se omite, hereda la tasa de la estrategia.")
    performance_fee_enabled: bool = True
    activate: bool = Field(
        default=True,
        description=("Si es `true` la suscripción queda operando. Si es `false` queda en "
                     "`PAUSED` para activarla después."))
    max_active_leaders: Optional[int] = Field(
        default=None, ge=0,
        description=("Override del cupo del plan **solo para esta llamada**. Normalmente no "
                     "se envía: se resuelve desde el cliente."))

    model_config = ConfigDict(json_schema_extra={"example": {
        "leader_login": "139682", "follower_login": "146502",
        "allocation_mode": "EQUITY", "mode_parameter": 1,
        "equity_stop": 1000, "unsubscribe_policy": "CLOSE_ON_UNSUBSCRIBE",
        "performance_fee_enabled": True, "activate": True,
    }})


class AllocationUpdateRequest(BaseModel):
    """Enviar solo lo que se quiere cambiar.

    Para dar de baja **no** se usa esto: hay que ir a `/unsubscribe`, que evalúa
    las posiciones abiertas y cobra el fee pendiente.
    """

    allocation_mode: Optional[AllocationMode] = None
    mode_parameter: Optional[Decimal] = Field(default=None, gt=0)
    equity_stop: Optional[Decimal] = Field(default=None, ge=0)
    unsubscribe_policy: Optional[UnsubscribePolicy] = None
    performance_fee_rate: Optional[Decimal] = Field(default=None, ge=0, le=1)
    performance_fee_enabled: Optional[bool] = None
    note: Optional[str] = Field(default=None, max_length=500)

    model_config = ConfigDict(json_schema_extra={"example": {
        "allocation_mode": "SCALED", "mode_parameter": 0.5, "equity_stop": 800}})


class AllocationStatusRequest(BaseModel):
    status: Literal["ACTIVE", "PAUSED"] = Field(
        description="Pausar o reactivar. No sirve para dar de baja.")

    model_config = ConfigDict(json_schema_extra={"example": {"status": "PAUSED"}})


class AllocationRead(BaseModel):
    id: str
    allocation_id: Optional[int] = None
    leader_login: str
    follower_login: str
    status: str
    allocation_mode: str
    mode_parameter: Optional[Decimal] = None
    equity_stop: Optional[Decimal] = None
    unsubscribe_policy: str
    performance_fee_rate: Optional[Decimal] = None
    performance_fee_enabled: bool
    max_active_leaders_requested: Optional[int] = None
    terminated_reason: Optional[str] = None
    terminated_by: Optional[str] = None
    performance_fee_charged: Optional[Decimal] = None
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AllocationListResponse(BaseModel):
    total: int
    page: int
    limit: int
    pages: int
    items: list[AllocationRead]


# ══════════════════════════════════════════════════════════════════════
# Performance fee
# ══════════════════════════════════════════════════════════════════════

class PerfFeeReconcileRequest(BaseModel):
    master_login: Optional[str] = Field(
        default=None, max_length=40,
        description=("Login **operativo** del leader. Si se omite, concilia **todas** las "
                     "estrategias registradas — que es como lo usa el proceso periódico."))
    from_at: Optional[str] = Field(
        default=None, description="Inicio inclusivo, ISO 8601 (`2026-08-01T00:00:00Z`).")
    to_at: Optional[str] = Field(default=None, description="Fin exclusivo, ISO 8601.")
    run_id: Optional[int] = Field(
        default=None, description="Conciliar una sola acreditación en vez de un período.")
    post_ledger: bool = Field(
        default=True,
        description=("Poner en `false` para traer los pagos sin asentarlos: sirve para "
                     "revisar antes de tocar el libro."))

    model_config = ConfigDict(json_schema_extra={"example": {
        "master_login": "139682",
        "from_at": "2026-08-01T00:00:00Z", "to_at": "2026-09-01T00:00:00Z"}})


class PerfFeeReconcileAllRead(BaseModel):
    leaders: int
    reconciled: int
    failed: int
    fetched: int
    posted_to_ledger: int
    errors: list[str] = Field(default_factory=list)
    pending_attribution: list[str] = Field(default_factory=list)


class PerfFeeReconcileRead(BaseModel):
    master_login: str
    payment_account_login: Optional[str] = None
    fetched: int = Field(description="Pagos que devolvió el motor.")
    new: int = Field(description="Los que no teníamos.")
    already_known: int
    posted_to_ledger: int
    executed_total: Decimal
    pending_attribution: list[str] = Field(
        default_factory=list,
        description=("Cuentas investor que no están en esta base: sus pagos quedaron "
                     "**registrados pero sin asiento**. Importalas y volvé a conciliar."))


class PerfFeeRunRead(BaseModel):
    run_id: Optional[int] = None
    detail_total: Decimal
    payments: int


class PerfFeeVerifyRead(BaseModel):
    master_login: str
    payment_account_login: Optional[str] = None
    credits_found: int
    credited_total: Decimal = Field(description="Lo que el motor acreditó, consolidado.")
    detail_total: Decimal = Field(description="La suma de los pagos individuales que tenemos.")
    difference: Decimal
    matches: bool = Field(
        description="Si es `false`, faltan pagos por traer o el motor acreditó algo sin detallar.")
    runs: list[PerfFeeRunRead] = Field(default_factory=list)


class PerfFeePaymentRead(BaseModel):
    id: str
    provider_payment_id: int
    run_id: Optional[int] = None
    run_status: Optional[str] = None
    run_period_start: Optional[datetime] = None
    run_period_end: Optional[datetime] = None
    master_login: str
    payment_account_login: Optional[str] = None
    investor_mt5_login: Optional[str] = None
    trader_id: Optional[str] = Field(
        default=None, description="Cliente al que se atribuyó. `null` = sin asentar.")
    amount: Decimal
    currency: str
    status: Optional[str] = None
    executed_at: Optional[datetime] = None
    ledger_tx_id: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class PerfFeePaymentListResponse(BaseModel):
    total: int
    executed_total: Decimal
    page: int
    limit: int
    pages: int
    items: list[PerfFeePaymentRead]


# ══════════════════════════════════════════════════════════════════════
# Eventos del webhook
# ══════════════════════════════════════════════════════════════════════

class WebhookEventRead(BaseModel):
    id: str
    event_id: str
    event_type: str
    occurred_at: Optional[datetime] = None
    allocation_id: Optional[int] = None
    reason: Optional[str] = Field(
        default=None, description="`USER_UNSUBSCRIBE` o `EQUITY_STOP`.")
    triggered_by: Optional[str] = Field(default=None, description="`USER` o `SYSTEM`.")
    allocation_status: Optional[str] = None
    leader_login: Optional[str] = None
    follower_login: Optional[str] = None
    performance_fee_charged: Optional[Decimal] = None
    signature_verified: bool
    received_at: datetime
    processed_at: Optional[datetime] = Field(
        default=None, description="`null` = recibido pero todavía sin aplicar.")
    process_error: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class WebhookEventListResponse(BaseModel):
    total: int
    page: int
    limit: int
    pages: int
    items: list[WebhookEventRead]


# ══════════════════════════════════════════════════════════════════════
# Baja de cuentas
# ══════════════════════════════════════════════════════════════════════

DeletionScope = Literal["MASTER_ACCOUNT_ONLY", "MASTER_AND_INVESTORS"]
PositionsPolicy = Literal["CLOSE_TRANSMITTED", "KEEP_OPEN"]


class DeletionRequest(BaseModel):
    mt5_login: str = Field(max_length=40)
    scope: Optional[DeletionScope] = Field(
        default=None,
        description=("Solo aplica si la cuenta es una estrategia. `MASTER_AND_INVESTORS` "
                     "arrastra a sus seguidores directos, que hay que listar en "
                     "`investor_logins`."))
    investor_logins: Optional[list[str]] = None
    transmitted_positions_policy: PositionsPolicy = Field(
        default="CLOSE_TRANSMITTED",
        description=("`CLOSE_TRANSMITTED` cierra las posiciones copiadas antes de purgar. "
                     "`KEEP_OPEN` las deja vivas en MT5 bajo gestión manual y fuera del "
                     "seguimiento del motor."))
    idempotency_key: Optional[str] = Field(default=None, max_length=120)
    force: bool = Field(
        default=False,
        description=("Continuar aunque el análisis de impacto reporte conflictos. **No** "
                     "saltea el análisis: la foto queda guardada igual."))

    model_config = ConfigDict(json_schema_extra={"example": {
        "mt5_login": "146502", "transmitted_positions_policy": "CLOSE_TRANSMITTED"}})


class DeletionOperationRead(BaseModel):
    id: str
    operation_id: Optional[str] = None
    target_kind: str = Field(description="`MASTER` si la cuenta es una estrategia.")
    target_login: str
    scope: Optional[str] = None
    transmitted_positions_policy: str
    status: str = Field(
        description=("`PENDING` → `WAITING_CLOSE` → `PURGING` → `COMPLETED`. "
                     "`PARTIAL` se reintenta; `FAILED` no."))
    error_message: Optional[str] = None
    requested_by: Optional[str] = None
    idempotency_key: str
    created_at: datetime
    completed_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class DeletionOperationListResponse(BaseModel):
    total: int
    page: int
    limit: int
    pages: int
    items: list[DeletionOperationRead]


# ══════════════════════════════════════════════════════════════════════
# Rendimiento
# ══════════════════════════════════════════════════════════════════════

class PerformanceQueryRequest(BaseModel):
    account_logins: list[str] = Field(min_length=1, max_length=500)

    model_config = ConfigDict(json_schema_extra={"example": {
        "account_logins": ["139682", "146502"]}})


class TraderAccountOverview(BaseModel):
    mt5_login: str
    name: Optional[str] = None
    can_be_leader: bool
    can_be_follower: bool
    status: str
    balance: Optional[Decimal] = None
    equity: Optional[Decimal] = None
    free_margin: Optional[Decimal] = None
    metrics_error: Optional[str] = Field(
        default=None, description="Si MT5 no pudo responder por esta cuenta.")


class TraderSubscriptionOverview(BaseModel):
    allocation_id: Optional[int] = None
    leader_login: str
    follower_login: str
    status: str
    allocation_mode: str
    performance_fee_rate: Optional[Decimal] = None


class TraderOverviewRead(BaseModel):
    external_reference: str
    email: str
    max_active_leaders: Optional[int] = None
    accounts: list[TraderAccountOverview] = Field(default_factory=list)
    live_subscriptions: list[TraderSubscriptionOverview] = Field(default_factory=list)
    capital_placed: Decimal = Field(description="Capital neto colocado, según el libro contable.")
    equity_total: Decimal = Field(description="Cuánto vale hoy, sumando sus cuentas en MT5.")
    performance_fees_paid: Decimal


# ══════════════════════════════════════════════════════════════════════
# Operación
# ══════════════════════════════════════════════════════════════════════

class PendingItemRead(BaseModel):
    key: str
    count: int
    needs_human: bool = Field(description="Si no avanza solo con los procesos periódicos.")
    what: str
    why: str


class PendingRead(BaseModel):
    needs_attention: int = Field(description="Suma de lo que no se resuelve solo.")
    total_pending: int
    items: list[PendingItemRead]


class ReadinessRead(BaseModel):
    ready: bool
    capabilities: dict[str, bool] = Field(
        description="Qué puede hacer el servicio hoy, según lo que esté configurado.")
    critical: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    needs_attention: int


# ══════════════════════════════════════════════════════════════════════
# Cuenta PAYMENT
# ══════════════════════════════════════════════════════════════════════

class PaymentAccountBalanceRead(BaseModel):
    """Saldo en vivo de la cuenta que recibe los performance fees (spec §11.3)."""

    master_login: str
    payment_account_login: Optional[str] = None
    balance: Optional[Decimal] = None
    equity: Optional[Decimal] = None
    free_margin: Optional[Decimal] = None
    credit: Optional[Decimal] = None
    withdrawable: Optional[Decimal] = Field(
        default=None,
        description=("Límite real para habilitar un retiro: excluye el crédito MT5. "
                     "Usar este valor, no `balance`."))
    currency: Optional[str] = None


class PaymentAccountWithdrawRequest(BaseModel):
    amount: Decimal = Field(gt=0)
    idempotency_key: Optional[str] = Field(
        default=None, min_length=8, max_length=120,
        description=("Repetir la misma solicitud devuelve `ALREADY_PROCESSED` sin volver "
                     "a debitar. Usar la misma key con otro monto da 409. Si se omite, "
                     "el servicio genera una."))

    model_config = ConfigDict(json_schema_extra={"example": {
        "amount": 100, "idempotency_key": "payment-withdrawal-84721",
    }})


class PaymentAccountWithdrawRead(BaseModel):
    result: Optional[str] = None
    master_login: str
    payment_account_login: Optional[str] = None
    requested_amount: Decimal
    currency: Optional[str] = None
    balance_before: Optional[Decimal] = None
    balance_after: Optional[Decimal] = None
    movement_id: Optional[str] = None
