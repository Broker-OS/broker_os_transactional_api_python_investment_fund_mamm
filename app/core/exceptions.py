"""Jerarquia de excepciones de dominio. El servicio solo lanza AppException."""
from typing import Optional


class AppException(Exception):
    http_status: int = 500
    code: str = "INTERNAL_ERROR"

    def __init__(self, message: str, detail: Optional[str] = None):
        self.message = message
        self.detail = detail
        super().__init__(message)


class NotFoundError(AppException):
    http_status = 404
    code = "RESOURCE_NOT_FOUND"


class ValidationError(AppException):
    http_status = 422
    code = "VALIDATION_ERROR"


class ConflictError(AppException):
    http_status = 409
    code = "RESOURCE_CONFLICT"


class ForbiddenError(AppException):
    http_status = 403
    code = "FORBIDDEN"


class AccountLoginRequiredError(ValidationError):
    """El trader tiene varias cuentas MAM activas: hay que indicar cuál."""
    code = "ACCOUNT_LOGIN_REQUIRED"


class ApiUserNotFoundError(NotFoundError):
    code = "API_USER_NOT_FOUND"


class UnauthorizedError(AppException):
    http_status = 401
    code = "UNAUTHORIZED"


class ServiceUnavailableError(AppException):
    http_status = 503
    code = "SERVICE_UNAVAILABLE"


# ── API key ──
class ApiKeyMissingError(UnauthorizedError):
    code = "API_KEY_MISSING"


class ApiKeyInvalidError(UnauthorizedError):
    code = "API_KEY_INVALID"


# ── MAM API (proveedor externo) ──
class ProviderConfigError(ServiceUnavailableError):
    code = "MAM_INTEGRATION_DISABLED"


class ProviderApiError(ServiceUnavailableError):
    code = "MAM_API_ERROR"


# ── Mapeo fino de los codigos HTTP del MAM API (spec §3.5) ──

class ProviderUncertainResultError(ProviderApiError):
    """Timeout o error de red en una operacion cuyo resultado quedo incierto.

    Con `idempotency_key` (spec §12) reintentar la MISMA key es seguro y no
    duplica nada, asi que esta excepcion queda para lo que NO es idempotente:
    la creacion de cuentas MT5 y de perfiles. Ahi hay que consultar por login o
    por la pareja leader/follower antes de repetir, nunca reintentar a ciegas.
    """
    code = "MAM_RESULT_UNCERTAIN"


class ProviderBusinessRuleError(ValidationError):
    """Regla de negocio incumplida (saldo, posiciones abiertas, minimo, estado)."""
    http_status = 400
    code = "MAM_BUSINESS_RULE"


class ProviderForbiddenError(AppException):
    """403: la API key no tiene acceso al endpoint. No cambiar de key en silencio."""
    http_status = 403
    code = "MAM_FORBIDDEN"


class ProviderOperationInProgressError(ConflictError):
    """409: conflicto de negocio, duplicado o fondos insuficientes (spec §3.5).

    Spec §12: "Un 409 no se debe reintentar ciegamente".
    """
    code = "MAM_OPERATION_CONFLICT"


class ProviderPayloadError(ValidationError):
    """422: parametro invalido o regla de negocio incumplida (spec §3.5)."""
    code = "MAM_PAYLOAD_INVALID"


class ProviderMt5Error(ServiceUnavailableError):
    """502: el MAM API no pudo comunicarse con MT5 o ejecutar la operacion alli.

    Spec §3.5/§11.1. Es distinto de un 500: la peticion llego bien, lo que fallo
    fue el servidor MT5 del broker. Spec §12: los 500 y 502 se pueden reintentar
    con backoff conservando la misma idempotency key cuando hay dinero de por medio.
    """
    code = "MAM_MT5_ERROR"


class MamConfigError(ServiceUnavailableError):
    """Falta configuracion obligatoria para operar (ej. platform_group, API key)."""
    code = "MAM_CONFIG_MISSING"


class Mt5CredentialsEncryptionError(ServiceUnavailableError):
    """No hay clave de cifrado para credenciales MT5; no se persiste en claro."""
    code = "MT5_CREDENTIALS_ENCRYPTION_DISABLED"


class PerfFeeRateInvalidError(ValidationError):
    """El rate de performance fee debe ser decimal entre 0 y 1 (0.3 = 30%).

    Spec §3.7: "performance_fee_rate usa una tasa entre 0 y 1: 0.20 significa
    20 %". Mandar 20 para decir 20% seria 2000%.
    """
    code = "PERF_FEE_RATE_INVALID"


# ── Cuentas MAM ──

class MamAccountAlreadyExistsError(ConflictError):
    """El mt5_login ya esta registrado en este servicio o en el motor MAM."""
    code = "MAM_ACCOUNT_ALREADY_EXISTS"


class MamAccountNotFoundError(NotFoundError):
    code = "MAM_ACCOUNT_NOT_FOUND"


class AccountNotHedgingError(ValidationError):
    """La cuenta no usa HEDGING y por lo tanto no puede hacer copy trading.

    Spec §4.1: "Para participar en copy trading, las cuentas deben estar ACTIVE
    y usar account_mode='HEDGING'".
    """
    http_status = 400
    code = "ACCOUNT_NOT_HEDGING"


class AccountNotActiveError(ValidationError):
    """La cuenta no esta ACTIVE (spec §8)."""
    http_status = 400
    code = "ACCOUNT_NOT_ACTIVE"


class LeaderCapabilityMissingError(ValidationError):
    """La cuenta no tiene can_be_leader=true (spec §8)."""
    http_status = 400
    code = "LEADER_CAPABILITY_MISSING"


class FollowerCapabilityMissingError(ValidationError):
    """La cuenta no tiene can_be_follower=true (spec §8)."""
    http_status = 400
    code = "FOLLOWER_CAPABILITY_MISSING"


# ── Perfil de leader ──

class LeaderProfileNotFoundError(NotFoundError):
    """La cuenta no tiene perfil de leader; sin el no puede originar allocations."""
    code = "LEADER_PROFILE_NOT_FOUND"


class LeaderProfileAlreadyExistsError(ConflictError):
    """Esa cuenta ya tiene perfil de leader (spec §4.2: el perfil extiende UNA cuenta)."""
    code = "LEADER_PROFILE_ALREADY_EXISTS"


class PaymentAccountUnavailableError(ConflictError):
    """El leader no tiene una cuenta PAYMENT dedicada valida.

    Spec §4.5: los endpoints de saldo y retiro PAYMENT responden 409 si no existe.
    """
    code = "PAYMENT_ACCOUNT_UNAVAILABLE"


class PaymentAccountRoleError(ValidationError):
    """Se intento operar una cuenta PAYMENT como si fuera operativa.

    Spec §10.1: "No deposite fondos manualmente en la cuenta operativa del leader
    para simular performance fees. Tampoco retire directamente desde MT5 la
    cuenta PAYMENT".
    """
    code = "PAYMENT_ACCOUNT_ROLE_INVALID"


# ── Allocations ──

class AllocationNotFoundError(NotFoundError):
    code = "ALLOCATION_NOT_FOUND"


class AllocationAlreadyLiveError(ConflictError):
    """Ya existe una allocation viva para esa pareja leader/follower.

    Spec §8: "Solo puede existir una allocation viva para la misma pareja".
    """
    code = "ALLOCATION_ALREADY_LIVE"


class SelfFollowError(ValidationError):
    """Una cuenta no puede seguirse a si misma (spec §8)."""
    http_status = 400
    code = "SELF_FOLLOW_FORBIDDEN"


class MaxActiveLeadersReachedError(ConflictError):
    """El follower ya alcanzo el cupo de allocations vivas de su plan.

    Spec §7.1: el limite lo resuelve este servicio desde el producto del cliente
    y se manda en cada POST; el motor cuenta ACTIVE, PAUSED y STOPPING.
    """
    code = "MAX_ACTIVE_LEADERS_REACHED"


class MinDepositNotMetError(ValidationError):
    """El balance del follower no alcanza el min_deposit del perfil del leader.

    Spec §5 paso 5: la validacion usa EXCLUSIVAMENTE el balance actual en MT5;
    equity, credit y free margin no cuentan.
    """
    http_status = 400
    code = "MIN_DEPOSIT_NOT_MET"


class ModeParameterRequiredError(ValidationError):
    """FIXED y SCALED exigen mode_parameter; omitirlo produce 422 (spec §6)."""
    code = "MODE_PARAMETER_REQUIRED"


class ModeParameterInvalidError(ValidationError):
    """mode_parameter debe ser estrictamente mayor que 0 en los cinco modos (spec §6)."""
    code = "MODE_PARAMETER_INVALID"


class UnsubscribeInProgressError(ConflictError):
    """Ya hay una desuscripcion en curso (estado STOPPING).

    Spec §9: el cierre controlado termina en CANCELLED. No repetir la llamada:
    consultar el estado hasta verlo.
    """
    code = "UNSUBSCRIBE_IN_PROGRESS"


# ── Webhook de terminacion (spec §11.6) ──

class WebhookSignatureInvalidError(UnauthorizedError):
    """La firma HMAC-SHA256 no valida contra el signing_secret."""
    code = "WEBHOOK_SIGNATURE_INVALID"


class WebhookTimestampSkewError(UnauthorizedError):
    """El timestamp firmado esta fuera de la ventana aceptada (5 minutos)."""
    code = "WEBHOOK_TIMESTAMP_SKEW"


class WebhookSecretMissingError(ServiceUnavailableError):
    """No hay signing_secret configurado: no se puede verificar ningun evento.

    Spec §11.6: se entrega en texto plano UNA SOLA VEZ al registrar el webhook.
    """
    code = "WEBHOOK_SECRET_MISSING"


# ── Eliminacion de cuentas (spec §11.1) ──

class DeletionOperationNotFoundError(NotFoundError):
    code = "DELETION_OPERATION_NOT_FOUND"


class DeletionNotEligibleError(ConflictError):
    """El GET /impact reporto conflictos: la cuenta no se puede eliminar todavia."""
    code = "DELETION_NOT_ELIGIBLE"


# ── Depositos on-chain (EVM) ──

class EvmConfigError(ServiceUnavailableError):
    """Falta configuracion para verificar pagos on-chain."""
    code = "EVM_NOT_CONFIGURED"


class EvmRpcError(ServiceUnavailableError):
    """El nodo de la cadena no respondio o rechazo la consulta."""
    code = "EVM_RPC_ERROR"


class TxHashInvalidError(ValidationError):
    code = "TX_HASH_INVALID"


class ChainMismatchError(ValidationError):
    """El chain_id declarado no es el de la cadena configurada."""
    http_status = 400
    code = "CHAIN_MISMATCH"


class TxNotFoundError(NotFoundError):
    """La cadena no conoce la transaccion (o sigue en el mempool)."""
    code = "TX_NOT_FOUND"


class TxNotConfirmedError(ConflictError):
    """La transaccion existe pero le faltan confirmaciones."""
    code = "TX_NOT_CONFIRMED"


class TxFailedOnChainError(ValidationError):
    """La transaccion se mino pero revirtio: no movio nada."""
    http_status = 400
    code = "TX_FAILED_ON_CHAIN"


class TransferNotFoundError(ValidationError):
    """La transaccion no contiene una transferencia del token esperado hacia
    nuestra address receptora."""
    http_status = 400
    code = "TRANSFER_NOT_FOUND"


class AmountMismatchError(ValidationError):
    """El monto declarado no coincide con el que realmente llego on-chain."""
    http_status = 400
    code = "AMOUNT_MISMATCH"


class DepositAlreadyRegisteredError(ConflictError):
    """Esa transaccion ya fue acreditada: no se puede reutilizar."""
    code = "DEPOSIT_ALREADY_REGISTERED"


class OperationInFlightError(ConflictError):
    """Ya hay una operacion en vuelo sobre esa cuenta MT5.

    El cupo es por CUENTA, no por cliente: un trader puede tener varias cuentas
    operando estrategias distintas a la vez. Lo que no puede haber son dos
    operaciones simultaneas sobre el mismo login.
    """
    code = "OPERATION_IN_FLIGHT"


class TraderHasUnreconciledMovementError(ConflictError):
    """El trader tiene un movimiento AMBIGUOUS sin resolver.

    Guia §2/§10: ante un resultado incierto hay que conciliar antes de seguir
    operando. Mover mas dinero sobre un estado desconocido agrava el problema.
    """
    code = "TRADER_HAS_UNRECONCILED_MOVEMENT"


class MovementNotResolvableError(ValidationError):
    """El movimiento no esta en un estado que admita resolucion manual."""
    code = "MOVEMENT_NOT_RESOLVABLE"


class TraderHasNoAccountError(ConflictError):
    """El trader no tiene ninguna cuenta MAM activa: no hay donde mover capital."""
    code = "TRADER_HAS_NO_ACCOUNT"


class Mt5CredentialsUnavailableError(NotFoundError):
    """No hay contrasenas MT5 guardadas para esa cuenta y el proveedor tampoco
    las devolvio. Pasa con cuentas registradas (no creadas) por el bridge."""
    code = "MT5_CREDENTIALS_UNAVAILABLE"


class Mt5LoginNotFoundError(ValidationError):
    """El login MT5 que se quiere registrar no existe en el servidor del broker.

    Guia §5.2: "Verifique antes que ambas cuentas MT5 existen y tienen el
    proposito correcto". Ni el proveedor ni MT5 rechazan un login inventado al
    registrarlo, asi que la verificacion tiene que hacerse aca: registrar una
    cuenta fantasma deja un ACTIVE que ninguna consulta de saldo puede resolver.
    """
    code = "MT5_LOGIN_NOT_FOUND"


class AmbiguousAccountError(ValidationError):
    """El trader tiene varias cuentas MAM activas y no se indico cual.

    El performance fee y las allocations se configuran POR CUENTA, no por
    cliente. Elegir una por el integrador seria peor que fallar: aplicaria la
    operacion a la estrategia equivocada sin que nadie se entere.
    """
    code = "AMBIGUOUS_ACCOUNT"


class TraderNameRequiredError(ValidationError):
    """MT5 exige nombre y apellido del titular para crear la cuenta."""
    code = "TRADER_NAME_REQUIRED"


class PaymentAccountDepositForbiddenError(ValidationError):
    """Intento de depositar en la cuenta PAYMENT de un leader.

    Spec §10.1: "No deposite fondos manualmente en la cuenta operativa del
    leader para simular performance fees". La PAYMENT solo recibe fees
    acreditados por el motor; un deposito externo rompe la conciliacion.
    """
    code = "PAYMENT_ACCOUNT_DEPOSIT_FORBIDDEN"


class TraderNotFoundError(NotFoundError):
    code = "TRADER_NOT_FOUND"


class TraderAlreadyExistsError(ConflictError):
    code = "TRADER_ALREADY_EXISTS"


class ProviderResourceNotFoundError(NotFoundError):
    """El MAM API respondio 404: cuenta, perfil, allocation u otro recurso inexistente."""
    code = "MAM_RESOURCE_NOT_FOUND"


class AmountInvalidError(ValidationError):
    code = "AMOUNT_INVALID"


class InsufficientMasterAccountError(ValidationError):
    """La cuenta maestra no tiene saldo disponible suficiente para el deposito."""
    code = "INSUFFICIENT_MASTER_ACCOUNT_FUNDS"


class InsufficientAccountFundsError(ValidationError):
    """Saldo insuficiente en la cuenta MT5 para el retiro.

    Spec §10.1: el retiro de una cuenta con allocation cobra primero los
    performance fees vencidos, y se rechaza si el free margin restante no cubre
    el fee mas el monto solicitado.
    """
    code = "INSUFFICIENT_ACCOUNT_FUNDS"


class MovementNotFoundError(NotFoundError):
    code = "MOVEMENT_NOT_FOUND"


class AccountNotFoundOrForbiddenError(NotFoundError):
    """La cuenta MT5 no existe o NO pertenece al trader consultado.

    Se usa el mismo error para ambos casos a proposito: distinguirlos permitiria
    enumerar logins ajenos. Spec §11.1 advierte que el detalle de cuenta
    "incluye las credenciales almacenadas cuando existen" y hay que tratar esa
    respuesta como altamente sensible.
    """
    code = "ACCOUNT_NOT_FOUND"


class LedgerInconsistentError(ServiceUnavailableError):
    code = "LEDGER_INCONSISTENT"


# ── OTP (fondeo de cuenta maestra) ──
class OtpNotFoundError(NotFoundError):
    code = "OTP_NOT_FOUND"


class OtpInvalidError(ValidationError):
    code = "OTP_INVALID"


class OtpExpiredError(ValidationError):
    code = "OTP_EXPIRED"


class OtpMaxAttemptsError(ValidationError):
    code = "OTP_MAX_ATTEMPTS"


class OtpAlreadyUsedError(ConflictError):
    code = "OTP_ALREADY_USED"


class OtpEmailMissingError(ValidationError):
    code = "OTP_EMAIL_MISSING"


class EmailDeliveryError(ServiceUnavailableError):
    code = "EMAIL_DELIVERY_FAILED"
