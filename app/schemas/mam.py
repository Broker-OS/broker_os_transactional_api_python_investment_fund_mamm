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
    """Cambia capacidades o estado. Enviar solo lo que se quiere modificar."""

    name: Optional[str] = Field(default=None, max_length=160)
    can_be_leader: Optional[bool] = None
    can_be_follower: Optional[bool] = None
    status: Optional[AccountStatus] = None

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
