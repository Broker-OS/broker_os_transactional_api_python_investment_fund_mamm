"""
Modelo del motor MAM: cuentas MT5, perfiles de leader, allocations y performance fee.

DIFERENCIA CLAVE CON PAMM. El MAM API no tiene entidades separadas "master" e
"investor" (spec §1, NOTA). Crea SIEMPRE cuentas MAM, y `leader` / `follower`
describen el papel que dos cuentas juegan DENTRO de una allocation concreta. Una
misma cuenta puede ser follower de una estrategia y leader de otra al mismo
tiempo. Por eso aca no hay columna `kind`: hay dos flags independientes.

Varias reglas de la spec se codifican como constraints de BD, no solo como
validaciones de servicio — asi no hay forma de violarlas ni por un bug ni por
una carga manual:

* §4.5  la PAYMENT nunca puede ser la cuenta operativa  -> ck_mam_leader_payment_distinct
* §4.5  una PAYMENT no puede pertenecer a dos leaders   -> uq_mam_leader_payment_login
* §8    una cuenta no puede seguirse a si misma         -> ck_mam_allocations_not_self
* §8    una sola allocation VIVA por pareja leader/foll -> uq_mam_allocations_live_pair
* §6    FIXED y SCALED exigen mode_parameter            -> ck_mam_allocations_mode_param_required
* §6    mode_parameter siempre > 0                      -> ck_mam_allocations_mode_param_positive
* §7    performance_fee_rate es decimal entre 0 y 1     -> ck_mam_*_rate
* §11.6 un webhook no se procesa dos veces              -> uq_mam_webhook_events_event_id
"""
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.database import Base
from app.models._helpers import now_utc, uuid_str

# ── Estados de cuenta (spec §4.1: para operar debe estar ACTIVE) ──
ACCOUNT_ACTIVE = "ACTIVE"
ACCOUNT_INACTIVE = "INACTIVE"
ACCOUNT_DELETED = "DELETED"
ACCOUNT_STATUSES = (ACCOUNT_ACTIVE, ACCOUNT_INACTIVE, ACCOUNT_DELETED)

# ── Modo de cuenta MT5 (spec §4.1: el copy trading exige HEDGING) ──
MODE_HEDGING = "HEDGING"
MODE_NETTING = "NETTING"

# ── Estados del perfil de leader ──
LEADER_ACTIVE = "ACTIVE"
LEADER_INACTIVE = "INACTIVE"

# ── Estados de allocation (spec §9) ──
ALLOC_PAUSED = "PAUSED"
ALLOC_ACTIVE = "ACTIVE"
ALLOC_STOPPING = "STOPPING"
ALLOC_CANCELLED = "CANCELLED"
ALLOC_ERROR = "ERROR"
ALLOC_STATUSES = (ALLOC_PAUSED, ALLOC_ACTIVE, ALLOC_STOPPING, ALLOC_CANCELLED, ALLOC_ERROR)
# Spec §8: "ACTIVE, PAUSED y STOPPING cuentan como allocations vivas". Es el
# conjunto que consume el cupo de `max_active_leaders_per_follower`.
ALLOC_LIVE_STATES = (ALLOC_ACTIVE, ALLOC_PAUSED, ALLOC_STOPPING)

# ── Modos de asignacion (spec §6) ──
MODE_FIXED = "FIXED"
MODE_SCALED = "SCALED"
MODE_EQUITY = "EQUITY"
MODE_EQUITY_ROUND_DOWN = "EQUITY_ROUND_DOWN"
MODE_BALANCE = "BALANCE"
ALLOCATION_MODES = (MODE_FIXED, MODE_SCALED, MODE_EQUITY, MODE_EQUITY_ROUND_DOWN, MODE_BALANCE)
# Spec §6: en estos dos, omitir mode_parameter es 422. En los otros tres equivale a 1.
MODES_REQUIRING_PARAM = (MODE_FIXED, MODE_SCALED)

# ── Politica de desuscripcion (spec §9) ──
POLICY_KEEP_OPEN = "KEEP_OPEN"
POLICY_CLOSE_ON_UNSUBSCRIBE = "CLOSE_ON_UNSUBSCRIBE"

# ── Periodos de cristalizacion del performance fee (spec §10) ──
FEE_PERIODS = ("OFF", "MINUTELY", "HOURLY", "DAILY", "WEEKLY", "SEMIMONTHLY", "MONTHLY")

# ── Propagacion (spec §11.2) ──
PROPAGATION_ORIGINAL_ONLY = "ORIGINAL_ONLY"
PROPAGATION_CASCADE = "CASCADE"


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ",".join(f"'{v}'" for v in values) + ")"


class MamAccount(Base):
    """Cuenta MT5 registrada en el motor MAM.

    Es la UNICA entidad de cuenta que crea o registra el MAM API (spec §4.1). Se
    identifica publicamente por `mt5_login`, no por su id interno.

    `can_be_leader` y `can_be_follower` son independientes y pueden estar activos
    los dos a la vez. No confundirlos con `rights`: esos flags controlan las
    capacidades DENTRO de MAM, mientras `rights` es la mascara de permisos de
    login y trading del usuario en MT5.
    """

    __tablename__ = "mam_accounts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    # Una cuenta puede no pertenecer a ningun trader del socio (estrategia propia
    # del broker), por eso es nullable.
    trader_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("traders.id", ondelete="RESTRICT"), nullable=True, index=True
    )

    # Identificador PUBLICO de la cuenta: es el que usan todas las rutas del MAM
    # API (spec §2.1). El id interno del proveedor no se expone en las URLs.
    mt5_login: Mapped[str] = mapped_column(String(40), nullable=False, unique=True, index=True)
    # Id interno que devuelve el proveedor, si lo informa. Nunca se usa para rutear.
    provider_account_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # Spec §4.1: capacidades dentro de MAM.
    can_be_leader: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    can_be_follower: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)

    name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    currency: Mapped[str] = mapped_column(String(10), nullable=False, default="USD")
    # Spec §4.1: sin HEDGING la cuenta no puede participar en copy trading.
    account_mode: Mapped[str] = mapped_column(String(20), nullable=False, default=MODE_HEDGING)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=ACCOUNT_ACTIVE, index=True)

    # Datos de provisioning MT5 (solo cuando la cuenta la creamos nosotros).
    platform_group: Mapped[str | None] = mapped_column(String(120), nullable=True)
    leverage: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Spec §5/§11.1: mascara MT5 efectivamente aplicada. Null en cuentas
    # registradas con /accounts/add, que no modifica permisos.
    rights: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Servidor MT5 al que apuntan las credenciales (3er dato para la terminal).
    mt5_server: Mapped[str | None] = mapped_column(String(80), nullable=True)

    # Credenciales MT5 CIFRADAS (nunca en claro, nunca a logs).
    mt5_password_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    mt5_investor_password_enc: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False
    )

    trader = relationship("Trader")
    leader_profile = relationship("MamLeaderProfile", back_populates="account", uselist=False)

    __table_args__ = (
        CheckConstraint(_in("status", ACCOUNT_STATUSES), name="ck_mam_accounts_status"),
        CheckConstraint(_in("account_mode", (MODE_HEDGING, MODE_NETTING)),
                        name="ck_mam_accounts_mode"),
        # Spec §5/§11.1: la integracion define DOS perfiles de mascara y solo esos.
        # Un valor inventado no se valida contra MT5 hasta que ya es tarde.
        CheckConstraint("rights IS NULL OR rights IN (9073, 8981)", name="ck_mam_accounts_rights"),
        CheckConstraint("leverage IS NULL OR leverage > 0", name="ck_mam_accounts_leverage"),
        Index("ix_mam_accounts_trader_status", "trader_id", "status"),
        # Para resolver rapido "que cuentas pueden originar operaciones".
        Index("ix_mam_accounts_capabilities", "can_be_leader", "can_be_follower", "status"),
    )


class MamLeaderProfile(Base):
    """Configuracion que habilita a una cuenta MAM a ORIGINAR allocations (spec §4.2).

    No es otra cuenta: extiende una `MamAccount` que ya tiene can_be_leader=true.
    Una cuenta que solo va a recibir operaciones no necesita este perfil.

    La cuenta PAYMENT (spec §4.5) no se modela como fila propia: es un login MT5
    que solo recibe performance fees y no participa como leader ni follower. Se
    guarda como columna para poder rechazar depositos hacia ella.
    """

    __tablename__ = "mam_leader_profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("mam_accounts.id", ondelete="RESTRICT"),
        nullable=False, unique=True, index=True,
    )
    # Spec §2.1: el PATCH del perfil usa este id interno; las allocations y las
    # consultas usan account_login. Son dos identificadores distintos y ambos
    # hay que conservarlos.
    leader_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    account_login: Mapped[str] = mapped_column(String(40), nullable=False, unique=True, index=True)
    # Spec §4.5: la crea la API sola si se omite el campo. Dato sensible de conciliacion.
    payment_account_login: Mapped[str | None] = mapped_column(String(40), nullable=True)

    strategy_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    leaderboard_visibility: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    restrict_simultaneous_connections: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Spec §5 paso 3: balance MT5 minimo que debe tener un follower para crear una
    # allocation con este leader. 0 desactiva la restriccion.
    min_deposit: Mapped[Numeric] = mapped_column(Numeric(20, 8), nullable=False, default=0)
    # Spec §3.7: tasa entre 0 y 1. 0.20 es 20%.
    performance_fee_rate: Mapped[Numeric] = mapped_column(Numeric(9, 6), nullable=False, default=0)
    performance_fee_period: Mapped[str] = mapped_column(String(20), nullable=False, default="MONTHLY")
    propagation_mode: Mapped[str] = mapped_column(
        String(20), nullable=False, default=PROPAGATION_ORIGINAL_ONLY
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=LEADER_ACTIVE, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False
    )

    account = relationship("MamAccount", back_populates="leader_profile")

    __table_args__ = (
        CheckConstraint(_in("status", (LEADER_ACTIVE, LEADER_INACTIVE)),
                        name="ck_mam_leader_status"),
        CheckConstraint(_in("performance_fee_period", FEE_PERIODS), name="ck_mam_leader_fee_period"),
        CheckConstraint(_in("propagation_mode", (PROPAGATION_ORIGINAL_ONLY, PROPAGATION_CASCADE)),
                        name="ck_mam_leader_propagation"),
        # Spec §3.7/§7: "0.20 significa 20 %". Mandar 20 seria 2000%.
        CheckConstraint("performance_fee_rate >= 0 AND performance_fee_rate <= 1",
                        name="ck_mam_leader_rate"),
        CheckConstraint("min_deposit >= 0", name="ck_mam_leader_min_deposit"),
        # Spec §4.5: la PAYMENT es una cuenta separada de la operativa.
        CheckConstraint(
            "payment_account_login IS NULL OR payment_account_login <> account_login",
            name="ck_mam_leader_payment_distinct",
        ),
        # Spec §4.5: no puede estar asignada a otro leader.
        Index("uq_mam_leader_payment_login", "payment_account_login", unique=True,
              postgresql_where=text("payment_account_login IS NOT NULL")),
    )


class MamAllocation(Base):
    """Relacion de copy trading entre dos cuentas (spec §4.4).

    Ciclo: PAUSED -> ACTIVE -> (unsubscribe) -> STOPPING -> CANCELLED.

    `leader_login` y `follower_login` estan desnormalizados a proposito: la
    desuscripcion y el polling se hacen POR LOGIN y tienen que seguir
    funcionando aunque la fila de la cuenta cambie.
    """

    __tablename__ = "mam_allocations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    # Spec §2.1: identifica la relacion para consultar, actualizar o desuscribir.
    # Null solo mientras el POST no volvio.
    allocation_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, unique=True, index=True)

    leader_account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("mam_accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    follower_account_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("mam_accounts.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    leader_login: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    follower_login: Mapped[str] = mapped_column(String(40), nullable=False, index=True)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default=ALLOC_PAUSED, index=True)

    # Spec §6: el significado de mode_parameter cambia segun allocation_mode.
    allocation_mode: Mapped[str] = mapped_column(String(30), nullable=False, default=MODE_EQUITY)
    mode_parameter: Mapped[Numeric | None] = mapped_column(Numeric(20, 8), nullable=True)
    # Spec §7: equity minimo del follower; si cae hasta ahi la allocation se
    # detiene y sus operaciones copiadas se cierran. Null lo desactiva.
    equity_stop: Mapped[Numeric | None] = mapped_column(Numeric(20, 8), nullable=True)
    unsubscribe_policy: Mapped[str] = mapped_column(
        String(30), nullable=False, default=POLICY_CLOSE_ON_UNSUBSCRIBE
    )

    # Spec §7: si es null, hereda la tasa del perfil del leader al crearse.
    performance_fee_rate: Mapped[Numeric | None] = mapped_column(Numeric(9, 6), nullable=True)
    performance_fee_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Spec §7.1: el limite NO queda almacenado del lado del proveedor; se valida
    # contra el valor que mandamos en ESE request. Se guarda para auditoria:
    # sin esto no hay forma de reconstruir con que cupo se aprobo la allocation.
    max_active_leaders_requested: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Terminacion (spec §11.6): USER_UNSUBSCRIBE o EQUITY_STOP.
    terminated_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    terminated_by: Mapped[str | None] = mapped_column(String(20), nullable=True)
    performance_fee_charged: Mapped[Numeric | None] = mapped_column(Numeric(20, 8), nullable=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_detail: Mapped[str | None] = mapped_column(String(500), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False
    )

    leader_account = relationship("MamAccount", foreign_keys=[leader_account_id])
    follower_account = relationship("MamAccount", foreign_keys=[follower_account_id])

    __table_args__ = (
        CheckConstraint(_in("status", ALLOC_STATUSES), name="ck_mam_allocations_status"),
        CheckConstraint(_in("allocation_mode", ALLOCATION_MODES), name="ck_mam_allocations_mode"),
        CheckConstraint(_in("unsubscribe_policy", (POLICY_KEEP_OPEN, POLICY_CLOSE_ON_UNSUBSCRIBE)),
                        name="ck_mam_allocations_policy"),
        # Spec §8: "No se permite que una cuenta se siga a si misma".
        CheckConstraint("leader_login <> follower_login", name="ck_mam_allocations_not_self"),
        CheckConstraint("leader_account_id <> follower_account_id",
                        name="ck_mam_allocations_not_self_id"),
        # Spec §6: "enviar mode_parameter <= 0 produce 422", en los cinco modos.
        CheckConstraint("mode_parameter IS NULL OR mode_parameter > 0",
                        name="ck_mam_allocations_mode_param_positive"),
        # Spec §6: FIXED y SCALED lo exigen; omitirlo es 422.
        CheckConstraint(
            "allocation_mode NOT IN ('FIXED','SCALED') OR mode_parameter IS NOT NULL",
            name="ck_mam_allocations_mode_param_required",
        ),
        CheckConstraint(
            "performance_fee_rate IS NULL OR "
            "(performance_fee_rate >= 0 AND performance_fee_rate <= 1)",
            name="ck_mam_allocations_rate",
        ),
        CheckConstraint("equity_stop IS NULL OR equity_stop >= 0",
                        name="ck_mam_allocations_equity_stop"),
        CheckConstraint("max_active_leaders_requested IS NULL OR max_active_leaders_requested >= 0",
                        name="ck_mam_allocations_max_leaders"),
        # Spec §8: "Solo puede existir una allocation viva para la misma pareja
        # leader/follower". Se resuelve en BD para que dos requests concurrentes
        # no puedan crear dos, sin ventana de carrera.
        Index("uq_mam_allocations_live_pair", "leader_login", "follower_login", unique=True,
              postgresql_where=text(
                  "status IN ('ACTIVE','PAUSED','STOPPING')")),
        # Contar allocations vivas de un follower (cupo del plan, spec §7.1).
        Index("ix_mam_allocations_follower_status", "follower_login", "status"),
        Index("ix_mam_allocations_status_polled", "status", "last_polled_at"),
    )


class MamWebhookEvent(Base):
    """Evento recibido del webhook de terminacion de allocations (spec §11.6).

    Spec: "Aplique un indice UNIQUE sobre event_id. Un evento repetido debe
    responder 2xx sin repetir efectos". La entrega se puede repetir conservando
    el mismo event_id y payload, asi que la deduplicacion es obligatoria.

    Recibir el evento NO inicia ni autoriza la desuscripcion: informa una
    terminacion YA procesada del lado del proveedor.
    """

    __tablename__ = "mam_webhook_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    # UUID estable e idempotente del evento.
    event_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    event_type: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Datos desempaquetados de `data` para poder consultarlos sin abrir el JSON.
    allocation_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    triggered_by: Mapped[str | None] = mapped_column(String(20), nullable=True)
    allocation_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    leader_login: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    follower_login: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    performance_fee_charged: Mapped[Numeric | None] = mapped_column(Numeric(20, 8), nullable=True)

    # Payload crudo tal como llego: es la evidencia con la que se firmo.
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    signature_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    # Null mientras el evento esta persistido pero todavia no se aplico al dominio.
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    process_error: Mapped[str | None] = mapped_column(String(500), nullable=True)

    __table_args__ = (
        Index("ix_mam_webhook_events_unprocessed", "processed_at", "received_at"),
    )


class MamDeletionOperation(Base):
    """Eliminacion asincrona e idempotente de una cuenta del servicio MAM (spec §11.1).

    No usa DELETE directo porque puede involucrar allocations vivas y posiciones
    copiadas abiertas. Estos endpoints eliminan la cuenta y sus relaciones en
    MAM; NO eliminan el usuario del servidor MT5.

    Spec: "No archive ni elimine la cuenta en sistemas externos hasta recibir
    COMPLETED". Si queda PARTIAL hay que corregir la causa y llamar a retry.
    """

    __tablename__ = "mam_deletion_operations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    # id de la operacion del lado del proveedor; se consulta hasta COMPLETED.
    operation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # MASTER usa /account-deletions; INVESTOR usa /investor-account-deletions.
    target_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    target_login: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    # Solo MASTER: MASTER_ACCOUNT_ONLY | MASTER_AND_INVESTORS.
    scope: Mapped[str | None] = mapped_column(String(30), nullable=True)
    investor_logins: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    transmitted_positions_policy: Mapped[str] = mapped_column(
        String(30), nullable=False, default="CLOSE_TRANSMITTED"
    )

    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False, unique=True, index=True)
    requested_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    requested_by_api_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("api_users.id", ondelete="RESTRICT"), nullable=True, index=True
    )

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING", index=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Foto del GET /impact previo: spec exige revisarlo antes de crear la operacion.
    impact_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(_in("target_kind", ("MASTER", "INVESTOR")),
                        name="ck_mam_deletions_target_kind"),
        CheckConstraint(
            _in("status", ("PENDING", "WAITING_CLOSE", "PARTIAL", "PURGING", "COMPLETED", "FAILED")),
            name="ck_mam_deletions_status",
        ),
        CheckConstraint(_in("transmitted_positions_policy", ("CLOSE_TRANSMITTED", "KEEP_OPEN")),
                        name="ck_mam_deletions_policy"),
        CheckConstraint(
            "scope IS NULL OR scope IN ('MASTER_ACCOUNT_ONLY','MASTER_AND_INVESTORS')",
            name="ck_mam_deletions_scope",
        ),
        Index("ix_mam_deletions_status_created", "status", "created_at"),
    )


class MamFeeConfigChange(Base):
    """Auditoria de cada cambio de configuracion de performance fee.

    En MAM la tasa vive en DOS lugares (spec §7): el perfil del leader, que es la
    tasa por defecto, y cada allocation, que puede pisarla. Por eso el registro
    guarda contra que objetivo se aplico el cambio — sin eso no se puede
    reconstruir que tasa regia una relacion en una fecha dada.

    Cambiar la tasa no reemplaza el High-Water Mark ni altera fees ya ejecutados.
    """

    __tablename__ = "mam_fee_config_changes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    # LEADER_PROFILE (tasa por defecto) | ALLOCATION (tasa de una relacion).
    target_kind: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    # account_login del leader, o allocation_id segun el target.
    target_ref: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    trader_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("traders.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    # Quien autorizo el cambio (api_user que llamo al endpoint).
    changed_by_api_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("api_users.id", ondelete="RESTRICT"), nullable=True, index=True
    )

    previous_rate: Mapped[Numeric | None] = mapped_column(Numeric(9, 6), nullable=True)
    previous_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    previous_period: Mapped[str | None] = mapped_column(String(20), nullable=True)
    new_rate: Mapped[Numeric | None] = mapped_column(Numeric(9, 6), nullable=True)
    new_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    new_period: Mapped[str | None] = mapped_column(String(20), nullable=True)
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)

    __table_args__ = (
        CheckConstraint(_in("target_kind", ("LEADER_PROFILE", "ALLOCATION")),
                        name="ck_mam_fee_changes_target"),
        CheckConstraint("new_rate IS NULL OR (new_rate >= 0 AND new_rate <= 1)",
                        name="ck_mam_fee_changes_rate"),
        Index("ix_mam_fee_changes_target_created", "target_kind", "target_ref", "created_at"),
    )


class MamPerfFeePayment(Base):
    """Pago de performance fee ejecutado, para conciliacion (spec §11.3).

    Se alimenta de /perf-fee/master/{login}/investor-payments — el detalle POR
    INVESTOR. Ese es el endpoint que hay que usar para repartir comisiones a
    sponsors o a la red de IBs; el credito consolidado que aparece en la cuenta
    PAYMENT no identifica de quien vino cada peso.

    Spec: "La suma de amount de los items EXECUTED del mismo run_id debe
    coincidir con el credito consolidado correspondiente".
    """

    __tablename__ = "mam_perf_fee_payments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uuid_str)
    # Clave de deduplicacion: el proveedor puede reenviar la misma pagina.
    provider_payment_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, unique=True, index=True
    )
    # Acreditacion agrupada a la que pertenece el pago. Es la bisagra de la
    # conciliacion: la suma de los pagos EXECUTED de un mismo run_id tiene que
    # dar el credito consolidado que aparece en la cuenta PAYMENT.
    run_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    run_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    # Periodo que cristalizo esa corrida. Sin el no se puede responder "cuanto
    # cobro este leader en marzo" sin adivinar por fecha de ejecucion.
    run_period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    run_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    master_login: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    payment_account_login: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # Null en creditos agregados que no identifican un investor concreto.
    investor_mt5_login: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    allocation_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    # Resuelto por nosotros a partir del login, cuando se puede mapear.
    trader_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("traders.id", ondelete="RESTRICT"), nullable=True, index=True
    )

    amount: Mapped[Numeric] = mapped_column(Numeric(20, 8), nullable=False)
    currency: Mapped[str] = mapped_column(String(10), nullable=False, default="USD")
    # Solo los EXECUTED movieron dinero: los demas no se asientan.
    status: Mapped[str | None] = mapped_column(String(30), nullable=True, index=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Rastros del lado MT5 / cashflow del proveedor. No se usan para deduplicar
    # (para eso esta provider_payment_id) pero son lo unico que permite rastrear
    # un pago concreto si alguna vez hay que discutirlo con el broker.
    mt5_transfer_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    mt5_op_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cashflow_unique_key: Mapped[str | None] = mapped_column(String(160), nullable=True)

    # Asiento contable con el que se reflejo el fee en nuestro ledger. Null
    # mientras el pago esta registrado pero todavia no se asento (p.ej. porque
    # no pudimos atribuirlo a ningun cliente).
    ledger_tx_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now_utc, nullable=False)

    __table_args__ = (
        CheckConstraint("amount >= 0", name="ck_mam_perf_fee_payments_amount"),
        Index("ix_mam_perf_fee_payments_master_executed", "master_login", "executed_at"),
        Index("ix_mam_perf_fee_payments_run", "run_id", "investor_mt5_login"),
        # Panel de pagos que quedaron sin asentar: los que no se pudieron
        # atribuir a un cliente. Sin esto se pierden en la tabla.
        Index("ix_mam_perf_fee_payments_unposted", "ledger_tx_id", "status"),
    )
