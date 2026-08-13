"""Endpoints del motor MAM: cuentas MT5 y perfiles de estrategia (leader)."""
from math import ceil
from typing import Optional

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_api_key
from app.db.database import get_db
from app.models.api_user import ApiUser
from app.schemas.common import APIResponse
from app.schemas.mam import (
    AllocationCreateRequest,
    AllocationListResponse,
    AllocationRead,
    AllocationStatusRequest,
    AllocationUpdateRequest,
    CapitalMovementRead,
    CapitalOperationRequest,
    DeletionOperationListResponse,
    DeletionOperationRead,
    DeletionRequest,
    EligibilityRead,
    EligibilityRequest,
    LeaderProfileCreateRequest,
    LeaderProfileListResponse,
    LeaderProfileRead,
    LeaderProfileUpdateRequest,
    MamAccountCreateRequest,
    MamAccountCredentialsRead,
    MamAccountImportRequest,
    MamAccountListResponse,
    MamAccountMetricsRead,
    MamAccountRead,
    MamAccountRegisterRequest,
    MamAccountUpdateRequest,
    PaymentAccountBalanceRead,
    PerfFeePaymentListResponse,
    PerfFeePaymentRead,
    PerfFeeReconcileAllRead,
    PerfFeeReconcileRead,
    PerfFeeReconcileRequest,
    PerfFeeVerifyRead,
    PendingRead,
    PerformanceQueryRequest,
    ReadinessRead,
    TraderOverviewRead,
    WebhookEventListResponse,
    WebhookEventRead,
    PaymentAccountWithdrawRead,
    PaymentAccountWithdrawRequest,
)
from app.services.mam_account_service import MamAccountService
from app.services.mam_allocation_service import MamAllocationService
from app.services.mam_analytics_service import MamAnalyticsService
from app.services.mam_capital_service import MamCapitalService
from app.services.mam_deletion_service import MamDeletionService
from app.services.mam_ops_service import MamOpsService
from app.services.mam_leader_service import MamLeaderService
from app.services.mam_perf_fee_service import MamPerfFeeService
from app.services.mam_webhook_service import MamWebhookService

router = APIRouter()

_CUENTAS = ["4. MAM · Cuentas"]
_LEADERS = ["5. MAM · Estrategias (leader)"]
_ALLOC = ["6. MAM · Suscripciones (allocations)"]
_CAPITAL = ["7. MAM · Capital (depósito/retiro)"]
_FEE = ["8. MAM · Performance fee"]
_BAJAS = ["9. MAM · Baja de cuentas"]
_ANALYTICS = ["10. MAM · Rendimiento"]
_OPS = ["11. Operación (incidentes)"]


async def _read(svc: MamAccountService, acc) -> dict:
    out = MamAccountRead.model_validate(acc)
    out.has_leader_profile = await svc.has_leader_profile(acc.id)
    return out.model_dump(mode="json")


# ══════════════════════════════════════════════════════════════════════
# Cuentas
# ══════════════════════════════════════════════════════════════════════

@router.post("/mam/accounts", response_model=APIResponse, tags=_CUENTAS,
             status_code=status.HTTP_201_CREATED,
             summary="Crear una cuenta MT5 nueva y registrarla en MAM",
             description=(
                 "Crea la cuenta **en el servidor MT5 del broker** y la registra en el motor.\n\n"
                 "El motor no tiene tipos de cuenta: `can_be_leader` y `can_be_follower` son "
                 "**independientes** y pueden estar los dos activos. Para que la cuenta pueda "
                 "originar operaciones no alcanza con el flag — además necesita un **perfil de "
                 "estrategia** (`POST /mam/leaders`).\n\n"
                 "`rights_profile` es la máscara de permisos MT5 y **solo se puede fijar acá**: "
                 "ni el registro de una cuenta existente ni la edición posterior la cambian.\n\n"
                 "⚠️ **No es idempotente.** Ante un timeout, consultá por el login antes de "
                 "reintentar: repetir a ciegas crea una segunda cuenta real en MT5.\n\n"
                 "La respuesta **no** incluye las contraseñas; se piden aparte en "
                 "`/mam/accounts/{login}/credentials`."
             ))
async def create_account(body: MamAccountCreateRequest, db: AsyncSession = Depends(get_db),
                         caller: ApiUser = Depends(require_api_key)):
    svc = MamAccountService(db)
    acc = await svc.create_account(
        external_reference=body.external_reference, first_name=body.first_name,
        last_name=body.last_name, name=body.name, username=body.username,
        can_be_leader=body.can_be_leader, can_be_follower=body.can_be_follower,
        rights_profile=body.rights_profile, leverage=body.leverage,
        currency=body.currency, platform_group=body.platform_group, caller=caller)
    return APIResponse(success=True, http_status=201, message="Cuenta creada correctamente",
                       data=await _read(svc, acc))


@router.post("/mam/accounts/register", response_model=APIResponse, tags=_CUENTAS,
             status_code=status.HTTP_201_CREATED,
             summary="Registrar en MAM una cuenta MT5 que ya existe",
             description=(
                 "Registra una cuenta **existente**. No la crea en MT5 ni modifica sus permisos.\n\n"
                 "El motor acepta cualquier número y responde `201`, así que un login mal "
                 "tipeado quedaría registrado como activo pero sin saldo consultable. Por eso "
                 "después del alta se consultan las métricas en vivo: si vienen en `null`, "
                 "**ese login no existe en el servidor del broker** y hay que darlo de baja."
             ))
async def register_account(body: MamAccountRegisterRequest, db: AsyncSession = Depends(get_db),
                           caller: ApiUser = Depends(require_api_key)):
    svc = MamAccountService(db)
    acc, metrics = await svc.register_account(
        external_reference=body.external_reference, mt5_login=body.mt5_login,
        name=body.name, currency=body.currency, can_be_leader=body.can_be_leader,
        can_be_follower=body.can_be_follower, caller=caller)
    data = await _read(svc, acc)
    data["metrics"] = metrics
    data["mt5_reachable"] = metrics is not None
    msg = ("Cuenta registrada correctamente" if metrics is not None else
           "Cuenta registrada, pero MT5 no devolvió sus métricas: verificá que el login exista")
    return APIResponse(success=True, http_status=201, message=msg, data=data)


@router.post("/mam/accounts/import", response_model=APIResponse, tags=_CUENTAS,
             status_code=status.HTTP_201_CREATED,
             summary="Importar una cuenta que ya está registrada en el motor",
             description=(
                 "Trae a este servicio una cuenta que **el motor MAM ya conoce**.\n\n"
                 "No confundir con `/register`: ese registra en el motor una cuenta MT5 que "
                 "el motor todavía no tiene, y devuelve `409` si ya la tiene. Este endpoint "
                 "es para el caso contrario — cuentas creadas antes de la integración o por "
                 "otro sistema, que de otro modo quedarían inalcanzables desde acá.\n\n"
                 "Las capacidades, el estado y el modo se toman **del motor**, no de lo que "
                 "mande el cliente: ahí está la verdad. Si el motor devuelve credenciales "
                 "guardadas, se cifran y quedan disponibles en `/credentials`."
             ))
async def import_account(body: MamAccountImportRequest, db: AsyncSession = Depends(get_db),
                         caller: ApiUser = Depends(require_api_key)):
    svc = MamAccountService(db)
    acc, metrics = await svc.import_account(
        external_reference=body.external_reference, mt5_login=body.mt5_login, caller=caller)
    data = await _read(svc, acc)
    data["metrics"] = metrics
    data["mt5_reachable"] = metrics is not None
    return APIResponse(success=True, http_status=201, message="Cuenta importada correctamente",
                       data=data)


@router.get("/mam/accounts", response_model=APIResponse, tags=_CUENTAS,
            summary="Listar cuentas MAM",
            description=("Un **USER** solo ve las cuentas de sus clientes. Las cuentas sin "
                         "cliente asociado (estrategias propias del broker) las ve el ADMIN."))
async def list_accounts(
    external_reference: Optional[str] = Query(None, description="Filtrar por cliente."),
    can_be_leader: Optional[bool] = Query(None),
    can_be_follower: Optional[bool] = Query(None),
    account_status: Optional[str] = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    caller: ApiUser = Depends(require_api_key),
):
    svc = MamAccountService(db)
    rows, total = await svc.list_accounts(
        caller=caller, external_reference=external_reference, can_be_leader=can_be_leader,
        can_be_follower=can_be_follower, status=account_status, page=page, limit=limit)
    items = [MamAccountRead.model_validate(a) for a in rows]
    payload = MamAccountListResponse(
        total=total, page=page, limit=limit,
        pages=ceil(total / limit) if (total and limit) else 0, items=items)
    return APIResponse(success=True, http_status=200, message="Cuentas obtenidas correctamente",
                       data=payload.model_dump(mode="json"))


@router.get("/mam/accounts/{mt5_login}", response_model=APIResponse, tags=_CUENTAS,
            summary="Detalle de una cuenta",
            description="Una cuenta de otro cliente responde `404`: no se revela su existencia.")
async def get_account(mt5_login: str, db: AsyncSession = Depends(get_db),
                      caller: ApiUser = Depends(require_api_key)):
    svc = MamAccountService(db)
    acc = await svc.get_account(mt5_login, caller=caller)
    return APIResponse(success=True, http_status=200, message="Cuenta obtenida correctamente",
                       data=await _read(svc, acc))


@router.patch("/mam/accounts/{mt5_login}", response_model=APIResponse, tags=_CUENTAS,
              summary="Actualizar capacidades o estado de una cuenta",
              description=(
                  "Enviar solo lo que se quiere cambiar. El cambio se aplica **primero en el "
                  "motor**: si lo guardáramos antes, un rechazo del proveedor nos dejaría "
                  "afirmando una capacidad que la cuenta no tiene.\n\n"
                  "`rights` no se puede cambiar acá — solo al crear la cuenta."
              ))
async def update_account(mt5_login: str, body: MamAccountUpdateRequest,
                         db: AsyncSession = Depends(get_db),
                         caller: ApiUser = Depends(require_api_key)):
    svc = MamAccountService(db)
    acc = await svc.update_account(
        mt5_login=mt5_login, name=body.name, can_be_leader=body.can_be_leader,
        can_be_follower=body.can_be_follower, status=body.status,
        external_reference=body.external_reference, caller=caller)
    return APIResponse(success=True, http_status=200, message="Cuenta actualizada correctamente",
                       data=await _read(svc, acc))


@router.get("/mam/accounts/{mt5_login}/metrics", response_model=APIResponse, tags=_CUENTAS,
            summary="Balance y equity en vivo desde MT5",
            description=(
                "Consulta directa a MT5, no a nuestra base. Un `502` significa que MT5 no "
                "respondió o no encontró la cuenta — distinto de un `404`, que es del motor.\n\n"
                "Es el dato que decide la elegibilidad de una suscripción: el `min_deposit` "
                "se valida **solo** contra `balance`, sin contar equity, crédito ni free margin."
            ))
async def get_metrics(mt5_login: str, db: AsyncSession = Depends(get_db),
                      caller: ApiUser = Depends(require_api_key)):
    data = await MamAccountService(db).get_metrics(mt5_login=mt5_login, caller=caller)
    return APIResponse(success=True, http_status=200, message="Métricas obtenidas correctamente",
                       data=MamAccountMetricsRead(**{
                           k: v for k, v in data.items()
                           if k in MamAccountMetricsRead.model_fields
                       }).model_dump(mode="json"))


@router.get("/mam/accounts/{mt5_login}/credentials", response_model=APIResponse, tags=_CUENTAS,
            summary="Credenciales MT5 de la cuenta",
            description=(
                "⚠️ **Dato altamente sensible.** Devuelve las contraseñas en claro para "
                "entregárselas al titular. Se guardan cifradas y no se escriben en ningún log.\n\n"
                "Solo hay credenciales de las cuentas que **creó** este servicio. Las "
                "registradas (ya existían en MT5) nunca pasaron por acá: las tiene el broker."
            ))
async def get_credentials(mt5_login: str, db: AsyncSession = Depends(get_db),
                          caller: ApiUser = Depends(require_api_key)):
    data = await MamAccountService(db).get_credentials(mt5_login=mt5_login, caller=caller)
    return APIResponse(success=True, http_status=200, message="Credenciales obtenidas correctamente",
                       data=MamAccountCredentialsRead(**data).model_dump(mode="json"))


# ══════════════════════════════════════════════════════════════════════
# Perfiles de leader
# ══════════════════════════════════════════════════════════════════════

@router.post("/mam/leaders", response_model=APIResponse, tags=_LEADERS,
             status_code=status.HTTP_201_CREATED,
             summary="Convertir una cuenta en estrategia seguible",
             description=(
                 "Crea el **perfil de leader** sobre una cuenta que ya existe. No crea otra "
                 "cuenta: le agrega la configuración para originar operaciones.\n\n"
                 "Si la cuenta todavía no tiene `can_be_leader`, se habilita automáticamente "
                 "antes de crear el perfil.\n\n"
                 "**Cuenta PAYMENT:** dejá `payment_account_login` vacío y el motor crea sola "
                 "una cuenta MT5 aparte que recibirá los fees de esta estrategia. Su login "
                 "vuelve en la respuesta y hay que conservarlo: es dato de conciliación y "
                 "**ya no se puede reasignar** con la edición del perfil.\n\n"
                 "`performance_fee_rate` va entre 0 y 1: `0.20` es 20 %."
             ))
async def create_leader_profile(body: LeaderProfileCreateRequest,
                                db: AsyncSession = Depends(get_db),
                                caller: ApiUser = Depends(require_api_key)):
    profile = await MamLeaderService(db).create_profile(
        account_login=body.account_login, strategy_name=body.strategy_name,
        description=body.description, leaderboard_visibility=body.leaderboard_visibility,
        restrict_simultaneous_connections=body.restrict_simultaneous_connections,
        min_deposit=body.min_deposit, performance_fee_rate=body.performance_fee_rate,
        performance_fee_period=body.performance_fee_period,
        propagation_mode=body.propagation_mode,
        payment_account_login=body.payment_account_login, caller=caller)
    return APIResponse(success=True, http_status=201, message="Estrategia creada correctamente",
                       data=LeaderProfileRead.model_validate(profile).model_dump(mode="json"))


@router.post("/mam/leaders/import", response_model=APIResponse, tags=_LEADERS,
             status_code=status.HTTP_201_CREATED,
             summary="Importar una estrategia que ya existe en el motor",
             description=(
                 "Trae un perfil de estrategia que **el motor ya tiene**, buscándolo por el "
                 "login operativo de la cuenta.\n\n"
                 "Es el import que más duele si falta: sin el perfil local, cualquier intento "
                 "de suscribir un cliente a esa estrategia se rechaza con *«no tiene perfil»* "
                 "— exactamente lo contrario de lo que pasa en el motor."
             ))
async def import_leader_profile(
    account_login: str = Query(..., description="Login operativo de la cuenta leader."),
    db: AsyncSession = Depends(get_db), caller: ApiUser = Depends(require_api_key),
):
    profile = await MamLeaderService(db).import_profile(
        account_login=account_login, caller=caller)
    return APIResponse(success=True, http_status=201, message="Estrategia importada correctamente",
                       data=LeaderProfileRead.model_validate(profile).model_dump(mode="json"))


@router.get("/mam/leaders", response_model=APIResponse, tags=_LEADERS,
            summary="Listar estrategias")
async def list_leader_profiles(
    profile_status: Optional[str] = Query(None, alias="status"),
    page: int = Query(1, ge=1), limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db), caller: ApiUser = Depends(require_api_key),
):
    rows, total = await MamLeaderService(db).list_profiles(
        status=profile_status, page=page, limit=limit)
    payload = LeaderProfileListResponse(
        total=total, page=page, limit=limit,
        pages=ceil(total / limit) if (total and limit) else 0,
        items=[LeaderProfileRead.model_validate(p) for p in rows])
    return APIResponse(success=True, http_status=200, message="Estrategias obtenidas correctamente",
                       data=payload.model_dump(mode="json"))


@router.get("/mam/leaders/{account_login}", response_model=APIResponse, tags=_LEADERS,
            summary="Detalle de una estrategia",
            description="Se identifica por el **login operativo** de la cuenta, no por el `leader_id` interno.")
async def get_leader_profile(account_login: str, db: AsyncSession = Depends(get_db),
                             caller: ApiUser = Depends(require_api_key)):
    profile = await MamLeaderService(db).get_profile(account_login, caller=caller)
    return APIResponse(success=True, http_status=200, message="Estrategia obtenida correctamente",
                       data=LeaderProfileRead.model_validate(profile).model_dump(mode="json"))


@router.patch("/mam/leaders/{account_login}", response_model=APIResponse, tags=_LEADERS,
              summary="Actualizar una estrategia",
              description=(
                  "Enviar solo lo que se quiere cambiar.\n\n"
                  "Cambiar `min_deposit` aplica a suscripciones **nuevas**: no da de baja a "
                  "los clientes que ya estén por debajo del nuevo mínimo.\n\n"
                  "Cambiar la tasa **no** reemplaza el High-Water Mark ni altera fees ya "
                  "cobrados. Cada cambio de fee queda auditado con quién lo hizo y el `note`.\n\n"
                  "La cuenta PAYMENT no se reasigna por acá."
              ))
async def update_leader_profile(account_login: str, body: LeaderProfileUpdateRequest,
                                db: AsyncSession = Depends(get_db),
                                caller: ApiUser = Depends(require_api_key)):
    profile = await MamLeaderService(db).update_profile(
        account_login=account_login, note=body.note, caller=caller,
        strategy_name=body.strategy_name, description=body.description,
        leaderboard_visibility=body.leaderboard_visibility,
        restrict_simultaneous_connections=body.restrict_simultaneous_connections,
        min_deposit=body.min_deposit, performance_fee_rate=body.performance_fee_rate,
        performance_fee_period=body.performance_fee_period,
        propagation_mode=body.propagation_mode, status=body.status)
    return APIResponse(success=True, http_status=200, message="Estrategia actualizada correctamente",
                       data=LeaderProfileRead.model_validate(profile).model_dump(mode="json"))


# ══════════════════════════════════════════════════════════════════════
# Capital
# ══════════════════════════════════════════════════════════════════════

@router.post("/mam/accounts/{mt5_login}/deposits", response_model=APIResponse, tags=_CAPITAL,
             status_code=status.HTTP_201_CREATED,
             summary="Depositar en la cuenta MT5 del cliente",
             description=(
                 "Mueve capital **de la cuenta maestra a la cuenta MT5** del cliente, y lo "
                 "asienta en el libro contable.\n\n"
                 "El saldo se **reserva primero** en la cuenta maestra, después se llama al "
                 "motor, y recién ahí se confirma el asiento. Si el motor rechaza, la reserva "
                 "se libera. Sin esa reserva, dos depósitos simultáneos podrían comprometer "
                 "dos veces el mismo saldo.\n\n"
                 "Es **idempotente**: reintentar con la misma `idempotency_key` no duplica "
                 "el movimiento. Derivala del id de tu transacción y no la regeneres al "
                 "reintentar.\n\n"
                 "Si el resultado queda incierto (timeout), el movimiento queda `AMBIGUOUS` "
                 "y **la reserva se mantiene**: liberarla podría comprometer dos veces el "
                 "mismo saldo si el depósito sí se ejecutó."
             ))
async def deposit(mt5_login: str, body: CapitalOperationRequest,
                  db: AsyncSession = Depends(get_db),
                  caller: ApiUser = Depends(require_api_key)):
    mv = await MamCapitalService(db).deposit(
        mt5_login=mt5_login, amount=body.amount,
        idempotency_key=body.idempotency_key, caller=caller)
    return APIResponse(success=True, http_status=201, message="Depósito realizado correctamente",
                       data=CapitalMovementRead.model_validate(mv).model_dump(mode="json"))


@router.post("/mam/accounts/{mt5_login}/withdrawals", response_model=APIResponse, tags=_CAPITAL,
             status_code=status.HTTP_201_CREATED,
             summary="Retirar de la cuenta MT5 del cliente",
             description=(
                 "Mueve capital **de la cuenta MT5 a la cuenta maestra**.\n\n"
                 "⚠️ **El retiro cobra primero el performance fee vencido.** El motor rechaza "
                 "la operación si el free margin restante no cubre el fee más el monto pedido, "
                 "así que no hay retiros parciales: sale completo o falla.\n\n"
                 "Lo que sí varía es cuánto capital pierde el cliente en total — el monto "
                 "pedido **más** el fee cobrado en el camino. Ese fee viene en "
                 "`perf_fee_at_request` y **no vuelve a la cuenta maestra**: se acredita en la "
                 "cuenta PAYMENT del leader, así que genera un asiento propio contra "
                 "`PERF_FEE_PAID`.\n\n"
                 "Si `provider_result` es `ALREADY_PROCESSED`, el motor reconoció la key y no "
                 "volvió a debitar: no se asienta de nuevo."
             ))
async def withdraw(mt5_login: str, body: CapitalOperationRequest,
                   db: AsyncSession = Depends(get_db),
                   caller: ApiUser = Depends(require_api_key)):
    mv = await MamCapitalService(db).withdraw(
        mt5_login=mt5_login, amount=body.amount,
        idempotency_key=body.idempotency_key, caller=caller)
    msg = ("Retiro realizado correctamente" if mv.status == "COMPLETED"
           else "El motor aceptó la solicitud pero no movió fondos")
    return APIResponse(success=True, http_status=201, message=msg,
                       data=CapitalMovementRead.model_validate(mv).model_dump(mode="json"))


@router.get("/mam/accounts/{mt5_login}/balance-transactions", response_model=APIResponse,
            tags=_CAPITAL, summary="Historial contable de la cuenta según el motor",
            description=(
                "Depósitos, retiros, créditos de performance fee y demás movimientos, **tal "
                "como los ve el motor**.\n\n"
                "Es la contraparte de `/movements`: acá aparecen también los que no "
                "originamos nosotros — créditos de fee, ajustes del broker — que en nuestro "
                "libro no existen. Sirve para conciliar.\n\n"
                "Para ver un crédito de fee de un leader: usar su login **operativo** con "
                "`transaction_type=PF_CREDIT` y `status=EXECUTED`."
            ))
async def balance_transactions(
    mt5_login: str,
    transaction_type: Optional[str] = Query(None, description="Por ejemplo `PF_CREDIT`."),
    tx_status: Optional[str] = Query(None, alias="status"),
    cursor: Optional[int] = Query(None),
    limit: Optional[int] = Query(None, ge=1, le=200),
    db: AsyncSession = Depends(get_db), caller: ApiUser = Depends(require_api_key),
):
    data = await MamCapitalService(db).balance_transactions(
        mt5_login=mt5_login, transaction_type=transaction_type, tx_status=tx_status,
        cursor=cursor, limit=limit, caller=caller)
    return APIResponse(success=True, http_status=200, message="Historial obtenido correctamente",
                       data=data)


# ══════════════════════════════════════════════════════════════════════
# Allocations (suscripciones)
# ══════════════════════════════════════════════════════════════════════

@router.post("/mam/allocations/eligibility", response_model=APIResponse, tags=_ALLOC,
             summary="¿Puede este cliente suscribirse a esta estrategia?",
             description=(
                 "Valida **sin crear nada**. Sirve para pedirle fondos al cliente antes de "
                 "intentar la suscripción, en vez de mostrarle un error después.\n\n"
                 "Comprueba dos cosas de distinta procedencia: el **mínimo de la estrategia**, "
                 "que valida el motor contra el balance real en MT5 (equity, crédito y free "
                 "margin **no** cuentan), y el **cupo del plan del cliente**, que resuelve "
                 "este servicio porque el motor no lo conoce.\n\n"
                 "No reemplaza la validación del alta: al crear, el motor vuelve a consultar "
                 "el balance para no trabajar sobre un dato viejo."
             ))
async def check_eligibility(body: EligibilityRequest, db: AsyncSession = Depends(get_db),
                            caller: ApiUser = Depends(require_api_key)):
    data = await MamAllocationService(db).check_eligibility(
        leader_login=body.leader_login, follower_login=body.follower_login, caller=caller)
    return APIResponse(success=True, http_status=200, message="Elegibilidad evaluada",
                       data=EligibilityRead(**{
                           k: v for k, v in data.items() if k in EligibilityRead.model_fields
                       }).model_dump(mode="json"))


@router.post("/mam/allocations", response_model=APIResponse, tags=_ALLOC,
             status_code=status.HTTP_201_CREATED,
             summary="Suscribir un cliente a una estrategia",
             description=(
                 "Conecta dos cuentas: la que **origina** operaciones y la que las **recibe**.\n\n"
                 "Se crea en `PAUSED` y se activa con un PATCH aparte, aunque desde afuera sea "
                 "una sola llamada (`activate: true`). El orden importa: si activáramos en el "
                 "mismo paso y fallara el guardado, quedaría una relación copiando operaciones "
                 "reales que nuestra base no conoce.\n\n"
                 "**`mode_parameter`** cambia de significado según el modo — es obligatorio en "
                 "`FIXED` y `SCALED`, opcional en los demás (equivale a 1). Siempre mayor que 0.\n\n"
                 "El **cupo de estrategias simultáneas** sale del plan del cliente. El motor no "
                 "lo almacena: lo valida contra el valor que le mandamos en esta solicitud.\n\n"
                 "Si el balance no llega al mínimo de la estrategia, responde `MIN_DEPOSIT_NOT_MET`: "
                 "hay que depositar antes de suscribir."
             ))
async def create_allocation(body: AllocationCreateRequest, db: AsyncSession = Depends(get_db),
                            caller: ApiUser = Depends(require_api_key)):
    alloc = await MamAllocationService(db).create_allocation(
        leader_login=body.leader_login, follower_login=body.follower_login,
        allocation_mode=body.allocation_mode, mode_parameter=body.mode_parameter,
        equity_stop=body.equity_stop, unsubscribe_policy=body.unsubscribe_policy,
        performance_fee_rate=body.performance_fee_rate,
        performance_fee_enabled=body.performance_fee_enabled,
        activate=body.activate, max_active_leaders=body.max_active_leaders, caller=caller)
    return APIResponse(success=True, http_status=201, message="Suscripción creada correctamente",
                       data=AllocationRead.model_validate(alloc).model_dump(mode="json"))


@router.post("/mam/allocations/import", response_model=APIResponse, tags=_ALLOC,
             status_code=status.HTTP_201_CREATED,
             summary="Importar una suscripción que ya existe en el motor",
             description=(
                 "Trae a este servicio una relación que **el motor ya tiene**, identificada "
                 "por su `allocation_id`.\n\n"
                 "Importa más de lo que parece: una suscripción preexistente que no conocemos "
                 "consume el cupo del plan del cliente y participa en la detección de ciclos "
                 "del motor. Sin importarla, nuestras validaciones trabajan sobre un mapa "
                 "incompleto y rechazan suscripciones por razones que no podemos explicar.\n\n"
                 "Las dos cuentas tienen que estar importadas antes."
             ))
async def import_allocation(allocation_id: int = Query(..., description="ID de la suscripción en el motor."),
                            db: AsyncSession = Depends(get_db),
                            caller: ApiUser = Depends(require_api_key)):
    alloc = await MamAllocationService(db).import_allocation(
        allocation_id=allocation_id, caller=caller)
    return APIResponse(success=True, http_status=201, message="Suscripción importada correctamente",
                       data=AllocationRead.model_validate(alloc).model_dump(mode="json"))


@router.get("/mam/allocations", response_model=APIResponse, tags=_ALLOC,
            summary="Listar suscripciones",
            description=("Un **USER** ve las suscripciones donde su cliente está en cualquiera "
                         "de las dos puntas: siguiendo una estrategia, o siendo la estrategia "
                         "que otros siguen."))
async def list_allocations(
    leader_login: Optional[str] = Query(None),
    follower_login: Optional[str] = Query(None),
    allocation_status: Optional[str] = Query(None, alias="status"),
    page: int = Query(1, ge=1), limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db), caller: ApiUser = Depends(require_api_key),
):
    rows, total = await MamAllocationService(db).list_allocations(
        caller=caller, leader_login=leader_login, follower_login=follower_login,
        status=allocation_status, page=page, limit=limit)
    payload = AllocationListResponse(
        total=total, page=page, limit=limit,
        pages=ceil(total / limit) if (total and limit) else 0,
        items=[AllocationRead.model_validate(a) for a in rows])
    return APIResponse(success=True, http_status=200, message="Suscripciones obtenidas correctamente",
                       data=payload.model_dump(mode="json"))


@router.get("/mam/allocations/{allocation_id}", response_model=APIResponse, tags=_ALLOC,
            summary="Detalle de una suscripción")
async def get_allocation(allocation_id: int, db: AsyncSession = Depends(get_db),
                         caller: ApiUser = Depends(require_api_key)):
    alloc = await MamAllocationService(db).get_allocation(allocation_id, caller=caller)
    return APIResponse(success=True, http_status=200, message="Suscripción obtenida correctamente",
                       data=AllocationRead.model_validate(alloc).model_dump(mode="json"))


@router.patch("/mam/allocations/{allocation_id}", response_model=APIResponse, tags=_ALLOC,
              summary="Cambiar la configuración de una suscripción",
              description=(
                  "Modo de asignación, multiplicador, equity stop, política de baja y fee.\n\n"
                  "⚠️ **No sirve para dar de baja.** Cancelar por acá no evalúa las posiciones "
                  "abiertas ni cobra el fee pendiente — para eso está `/unsubscribe`.\n\n"
                  "Cada cambio de fee queda auditado con quién lo hizo y el `note`."
              ))
async def update_allocation(allocation_id: int, body: AllocationUpdateRequest,
                            db: AsyncSession = Depends(get_db),
                            caller: ApiUser = Depends(require_api_key)):
    alloc = await MamAllocationService(db).update_allocation(
        allocation_id=allocation_id, note=body.note, caller=caller,
        allocation_mode=body.allocation_mode, mode_parameter=body.mode_parameter,
        equity_stop=body.equity_stop, unsubscribe_policy=body.unsubscribe_policy,
        performance_fee_rate=body.performance_fee_rate,
        performance_fee_enabled=body.performance_fee_enabled)
    return APIResponse(success=True, http_status=200, message="Suscripción actualizada correctamente",
                       data=AllocationRead.model_validate(alloc).model_dump(mode="json"))


@router.post("/mam/allocations/{allocation_id}/status", response_model=APIResponse, tags=_ALLOC,
             summary="Pausar o reactivar una suscripción",
             description=(
                 "`PAUSED` deja la relación registrada pero deja de replicar operaciones. "
                 "**No pauses una suscripción con posiciones abiertas** sin definir antes cómo "
                 "se van a administrar: quedan vivas en MT5 sin que nadie las siga.\n\n"
                 "Al volver a un estado vivo el motor revalida cuentas, duplicados y "
                 "restricciones de conexión simultánea."
             ))
async def set_allocation_status(allocation_id: int, body: AllocationStatusRequest,
                                db: AsyncSession = Depends(get_db),
                                caller: ApiUser = Depends(require_api_key)):
    alloc = await MamAllocationService(db).set_status(
        allocation_id=allocation_id, status=body.status, caller=caller)
    return APIResponse(success=True, http_status=200, message="Estado actualizado correctamente",
                       data=AllocationRead.model_validate(alloc).model_dump(mode="json"))


@router.post("/mam/allocations/{allocation_id}/unsubscribe", response_model=APIResponse,
             tags=_ALLOC, summary="Dar de baja una suscripción",
             description=(
                 "**Esta es la forma correcta de terminar una relación**, no un cambio de "
                 "estado: aplica la política configurada y cobra el performance fee pendiente.\n\n"
                 "Con `CLOSE_ON_UNSUBSCRIBE` la baja **no es instantánea**: genera los cierres "
                 "y queda en `STOPPING` hasta que terminan; recién ahí cobra el fee y pasa a "
                 "`CANCELLED`. **No repitas la llamada** — usá `/sync` para seguir el estado.\n\n"
                 "Con `KEEP_OPEN` cobra el fee, desconecta las posiciones copiadas y las deja "
                 "abiertas en MT5 fuera de la gestión del motor."
             ))
async def unsubscribe_allocation(allocation_id: int, db: AsyncSession = Depends(get_db),
                                 caller: ApiUser = Depends(require_api_key)):
    alloc = await MamAllocationService(db).unsubscribe(
        allocation_id=allocation_id, caller=caller)
    msg = ("Suscripción dada de baja correctamente" if alloc.status == "CANCELLED"
           else "Baja en proceso: cerrando posiciones. Consultá el estado con /sync")
    return APIResponse(success=True, http_status=200, message=msg,
                       data=AllocationRead.model_validate(alloc).model_dump(mode="json"))


@router.post("/mam/allocations/sync", response_model=APIResponse, tags=_ALLOC,
             summary="Sincronizar las bajas en curso (cron)",
             description=(
                 "Refresca contra el motor las suscripciones que quedaron en `STOPPING`.\n\n"
                 "Hace falta porque una baja con `CLOSE_ON_UNSUBSCRIBE` no es instantánea: sin "
                 "esto nuestra base diría `STOPPING` para siempre aunque el motor ya la haya "
                 "dado por terminada. Pensado para correr periódicamente."
             ))
async def sync_allocations(
    allocation_id: Optional[int] = Query(None, description="Sincronizar solo esta."),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db), caller: ApiUser = Depends(require_api_key),
):
    data = await MamAllocationService(db).sync(allocation_id=allocation_id, limit=limit)
    return APIResponse(success=True, http_status=200,
                       message=f"Sincronizadas {data['reviewed']}, cambiaron {data['changed']}",
                       data=data)


# ══════════════════════════════════════════════════════════════════════
# Performance fee
# ══════════════════════════════════════════════════════════════════════

@router.post("/mam/perf-fee/reconcile", response_model=APIResponse, tags=_FEE,
             summary="Conciliar los fees cobrados por una estrategia",
             description=(
                 "Trae el **detalle por cliente** de los performance fees y lo asienta en "
                 "el libro.\n\n"
                 "Hace falta porque el crédito que llega a la cuenta PAYMENT viene "
                 "**consolidado**: una sola fila por acreditación, sin decir cuánto aportó "
                 "cada cliente. Sin este detalle no se puede repartir comisiones a "
                 "sponsors ni a una red de IBs.\n\n"
                 "Es **idempotente**: corrélo las veces que quieras sobre el mismo período. "
                 "Los pagos se deduplican por el id del motor y el asiento se postea con una "
                 "clave derivada de ese id.\n\n"
                 "Si una cuenta cliente no está en esta base, su pago queda **registrado "
                 "pero sin asiento** y aparece en `pending_attribution`: preferimos un pago "
                 "visible sin asiento que un asiento cargado al cliente equivocado.\n\n"
                 "Pensado también para correr periódicamente."
             ))
async def reconcile_perf_fee(body: PerfFeeReconcileRequest, db: AsyncSession = Depends(get_db),
                             caller: ApiUser = Depends(require_api_key)):
    svc = MamPerfFeeService(db)
    if not body.master_login:
        # Modo cron: todas las estrategias. Una que falle no frena a las demas.
        data = await svc.reconcile_all(from_at=body.from_at, to_at=body.to_at, caller=caller)
        msg = (f"{data['reconciled']}/{data['leaders']} estrategias conciliadas, "
               f"{data['posted_to_ledger']} asientos")
        if data["failed"]:
            msg += f", {data['failed']} con error"
        return APIResponse(success=True, http_status=200, message=msg,
                           data=PerfFeeReconcileAllRead(**data).model_dump(mode="json"))

    data = await svc.reconcile(
        master_login=body.master_login, from_at=body.from_at, to_at=body.to_at,
        run_id=body.run_id, post_ledger=body.post_ledger, caller=caller)
    msg = f"Conciliados {data['fetched']} pagos, {data['posted_to_ledger']} asentados"
    if data["pending_attribution"]:
        msg += f", {len(data['pending_attribution'])} sin cliente atribuido"
    return APIResponse(success=True, http_status=200, message=msg,
                       data=PerfFeeReconcileRead(**data).model_dump(mode="json"))


@router.get("/mam/perf-fee/verify", response_model=APIResponse, tags=_FEE,
            summary="¿Cuadra el detalle contra lo que acreditó el motor?",
            description=(
                "Cruza los pagos individuales que tenemos contra el **crédito consolidado** "
                "que el motor depositó en la cuenta PAYMENT.\n\n"
                "La suma de los pagos de una misma acreditación tiene que dar el crédito "
                "correspondiente. Si `matches` es `false`, o faltan pagos por traer, o el "
                "motor acreditó algo que no está detallando — y eso conviene resolverlo "
                "**antes** de repartir comisiones sobre un total que no cierra."
            ))
async def verify_perf_fee(
    master_login: str = Query(..., description="Login operativo del leader."),
    from_at: Optional[str] = Query(None), to_at: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db), caller: ApiUser = Depends(require_api_key),
):
    data = await MamPerfFeeService(db).verify_runs(
        master_login=master_login, from_at=from_at, to_at=to_at, caller=caller)
    msg = "El detalle cuadra con lo acreditado" if data["matches"] else \
          f"Diferencia de {data['difference']} entre lo acreditado y el detalle"
    return APIResponse(success=True, http_status=200, message=msg,
                       data=PerfFeeVerifyRead(**data).model_dump(mode="json"))


@router.get("/mam/perf-fee/payments", response_model=APIResponse, tags=_FEE,
            summary="Pagos de performance fee conciliados",
            description=(
                "Los pagos ya traídos, desde esta base. **Este es el listado que consume el "
                "cálculo de comisiones**: dice cuánto aportó cada cliente a cada "
                "acreditación.\n\n"
                "`only_unposted=true` muestra lo que hay que resolver a mano: cobrado por el "
                "motor pero sin asiento, casi siempre porque la cuenta cliente no está "
                "importada."
            ))
async def list_perf_fee_payments(
    master_login: Optional[str] = Query(None),
    investor_login: Optional[str] = Query(None, description="Filtrar por cuenta del cliente."),
    run_id: Optional[int] = Query(None),
    only_unposted: bool = Query(False, description="Solo lo cobrado y sin asentar."),
    page: int = Query(1, ge=1), limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db), caller: ApiUser = Depends(require_api_key),
):
    rows, total, suma = await MamPerfFeeService(db).list_payments(
        master_login=master_login, investor_login=investor_login, run_id=run_id,
        only_unposted=only_unposted, page=page, limit=limit)
    payload = PerfFeePaymentListResponse(
        total=total, executed_total=suma, page=page, limit=limit,
        pages=ceil(total / limit) if (total and limit) else 0,
        items=[PerfFeePaymentRead.model_validate(p) for p in rows])
    return APIResponse(success=True, http_status=200, message="Pagos obtenidos correctamente",
                       data=payload.model_dump(mode="json"))


# ══════════════════════════════════════════════════════════════════════
# Eventos del webhook (operación)
# ══════════════════════════════════════════════════════════════════════

@router.get("/mam/webhook-events", response_model=APIResponse, tags=_FEE,
            summary="Eventos recibidos del motor",
            description=(
                "Historial de avisos de terminación. `only_pending=true` muestra los que "
                "no se pudieron aplicar — casi siempre porque la suscripción no estaba "
                "importada cuando llegó el evento."
            ))
async def list_webhook_events(
    only_pending: bool = Query(False, description="Solo los que quedaron sin aplicar."),
    page: int = Query(1, ge=1), limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db), caller: ApiUser = Depends(require_api_key),
):
    rows, total = await MamWebhookService(db).list_events(
        only_pending=only_pending, page=page, limit=limit)
    payload = WebhookEventListResponse(
        total=total, page=page, limit=limit,
        pages=ceil(total / limit) if (total and limit) else 0,
        items=[WebhookEventRead.model_validate(ev) for ev in rows])
    return APIResponse(success=True, http_status=200, message="Eventos obtenidos correctamente",
                       data=payload.model_dump(mode="json"))


@router.post("/mam/webhook-events/retry", response_model=APIResponse, tags=_FEE,
             summary="Reintentar los eventos sin aplicar (cron)",
             description=(
                 "Vuelve a aplicar los eventos que quedaron pendientes. Un evento cuya "
                 "suscripción no estaba importada se resuelve solo una vez que la importás.\n\n"
                 "No le pide nada al proveedor: el evento ya está guardado con su firma "
                 "verificada."
             ))
async def retry_webhook_events(
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db), caller: ApiUser = Depends(require_api_key),
):
    data = await MamWebhookService(db).retry_pending(limit=limit)
    return APIResponse(success=True, http_status=200,
                       message=f"Revisados {data['reviewed']}, aplicados {data['processed']}",
                       data=data)


# ══════════════════════════════════════════════════════════════════════
# Baja de cuentas
# ══════════════════════════════════════════════════════════════════════

@router.get("/mam/accounts/{mt5_login}/deletion-impact", response_model=APIResponse,
            tags=_BAJAS, summary="¿Qué pasa si doy de baja esta cuenta?",
            description=(
                "Analiza **sin modificar nada**: qué suscripciones y qué posiciones "
                "copiadas se verían afectadas.\n\n"
                "La cuenta decide sola por qué flujo va — si tiene perfil de estrategia, "
                "la baja puede arrastrar a sus seguidores.\n\n"
                "Consultá esto **antes** de dar de baja: crear la operación a ciegas puede "
                "cerrar posiciones de un cliente que no esperaba perderlas."
            ))
async def deletion_impact(mt5_login: str, db: AsyncSession = Depends(get_db),
                          caller: ApiUser = Depends(require_api_key)):
    data = await MamDeletionService(db).impact(mt5_login=mt5_login, caller=caller)
    return APIResponse(success=True, http_status=200, message="Impacto analizado", data=data)


@router.post("/mam/account-deletions", response_model=APIResponse, tags=_BAJAS,
             status_code=status.HTTP_202_ACCEPTED,
             summary="Dar de baja una cuenta del motor",
             description=(
                 "**No es un borrado inmediato.** Crea una operación asincrónica que puede "
                 "tener que cerrar posiciones antes de purgar:\n\n"
                 "`PENDING` → `WAITING_CLOSE` → `PURGING` → `COMPLETED`\n\n"
                 "El análisis de impacto se ejecuta **siempre** y queda guardado como "
                 "evidencia. Si reporta conflictos, se corta — salvo que mandes `force`.\n\n"
                 "⚠️ Esto elimina la cuenta del **servicio MAM**, no el usuario del servidor "
                 "MT5: eso es un procedimiento administrativo del broker.\n\n"
                 "La cuenta se marca de baja de nuestro lado **recién cuando el motor "
                 "confirma** `COMPLETED`."
             ))
async def request_deletion(body: DeletionRequest, db: AsyncSession = Depends(get_db),
                           caller: ApiUser = Depends(require_api_key)):
    op = await MamDeletionService(db).request(
        mt5_login=body.mt5_login, scope=body.scope, investor_logins=body.investor_logins,
        transmitted_positions_policy=body.transmitted_positions_policy,
        idempotency_key=body.idempotency_key, force=body.force, caller=caller)
    return APIResponse(success=True, http_status=202, message="Baja en curso",
                       data=DeletionOperationRead.model_validate(op).model_dump(mode="json"))


@router.get("/mam/account-deletions", response_model=APIResponse, tags=_BAJAS,
            summary="Listar bajas")
async def list_deletions(
    deletion_status: Optional[str] = Query(None, alias="status"),
    page: int = Query(1, ge=1), limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db), caller: ApiUser = Depends(require_api_key),
):
    rows, total = await MamDeletionService(db).list_operations(
        status=deletion_status, page=page, limit=limit)
    payload = DeletionOperationListResponse(
        total=total, page=page, limit=limit,
        pages=ceil(total / limit) if (total and limit) else 0,
        items=[DeletionOperationRead.model_validate(o) for o in rows])
    return APIResponse(success=True, http_status=200, message="Bajas obtenidas correctamente",
                       data=payload.model_dump(mode="json"))


@router.get("/mam/account-deletions/{operation_id}", response_model=APIResponse, tags=_BAJAS,
            summary="Estado de una baja",
            description="Consulta el estado en el motor y lo refleja acá. **No archives la "
                        "cuenta en otros sistemas hasta ver `COMPLETED`.**")
async def get_deletion(operation_id: str, db: AsyncSession = Depends(get_db),
                       caller: ApiUser = Depends(require_api_key)):
    op = await MamDeletionService(db).get(operation_id, caller=caller)
    return APIResponse(success=True, http_status=200, message="Baja obtenida correctamente",
                       data=DeletionOperationRead.model_validate(op).model_dump(mode="json"))


@router.post("/mam/account-deletions/{operation_id}/retry", response_model=APIResponse,
             tags=_BAJAS, summary="Reintentar una baja que quedó a medias",
             description=(
                 "Solo para las que quedaron en `PARTIAL`: uno o más cierres de posiciones "
                 "no terminaron. **Corregí la causa primero** — reintentar sin eso repite el "
                 "mismo fallo.\n\n"
                 "Sobre `FAILED` o `COMPLETED` no hay nada que reintentar."
             ))
async def retry_deletion(operation_id: str, db: AsyncSession = Depends(get_db),
                         caller: ApiUser = Depends(require_api_key)):
    op = await MamDeletionService(db).retry(operation_id, caller=caller)
    return APIResponse(success=True, http_status=200, message="Reintento enviado",
                       data=DeletionOperationRead.model_validate(op).model_dump(mode="json"))


@router.post("/mam/account-deletions/sync", response_model=APIResponse, tags=_BAJAS,
             summary="Sincronizar las bajas en curso (cron)",
             description="Refresca contra el motor las que no llegaron a un estado final.")
async def sync_deletions(limit: int = Query(50, ge=1, le=200),
                         db: AsyncSession = Depends(get_db),
                         caller: ApiUser = Depends(require_api_key)):
    data = await MamDeletionService(db).sync(limit=limit)
    return APIResponse(success=True, http_status=200,
                       message=f"Revisadas {data['reviewed']}, cambiaron {data['changed']}",
                       data=data)


# ══════════════════════════════════════════════════════════════════════
# Rendimiento
# ══════════════════════════════════════════════════════════════════════

@router.get("/mam/traders/{external_reference}/overview", response_model=APIResponse,
            tags=_ANALYTICS, summary="Todo lo del cliente en una respuesta",
            description=(
                "Junta lo que el motor **no puede** armar solo: él no sabe que varias "
                "cuentas MT5 son de la misma persona — esa relación vive únicamente acá.\n\n"
                "Cruza tres fuentes: qué cuentas tiene y a qué estrategias sigue (nuestra "
                "base), cuánto capital le colocamos (el libro contable), y cuánto vale hoy "
                "cada cuenta (MT5 en vivo).\n\n"
                "Si MT5 no puede responder por una cuenta, esa fila trae `metrics_error` y "
                "el resto del resumen igual se devuelve."
            ))
async def trader_overview(external_reference: str, db: AsyncSession = Depends(get_db),
                          caller: ApiUser = Depends(require_api_key)):
    data = await MamAnalyticsService(db).trader_overview(
        external_reference=external_reference, caller=caller)
    return APIResponse(success=True, http_status=200, message="Resumen obtenido correctamente",
                       data=TraderOverviewRead(**data).model_dump(mode="json"))


@router.post("/mam/analytics/leaders/performance", response_model=APIResponse, tags=_ANALYTICS,
             summary="Rendimiento de una o varias estrategias",
             description="Cada login se valida contra esta base antes de consultar al motor.")
async def leaders_performance(body: PerformanceQueryRequest, db: AsyncSession = Depends(get_db),
                              caller: ApiUser = Depends(require_api_key)):
    data = await MamAnalyticsService(db).leader_performance(
        account_logins=body.account_logins, caller=caller)
    return APIResponse(success=True, http_status=200, message="Rendimiento obtenido", data=data)


@router.post("/mam/analytics/followers/performance", response_model=APIResponse, tags=_ANALYTICS,
             summary="Rendimiento de una o varias cuentas de clientes")
async def followers_performance(body: PerformanceQueryRequest, db: AsyncSession = Depends(get_db),
                                caller: ApiUser = Depends(require_api_key)):
    data = await MamAnalyticsService(db).follower_performance(
        account_logins=body.account_logins, caller=caller)
    return APIResponse(success=True, http_status=200, message="Rendimiento obtenido", data=data)


@router.get("/mam/analytics/leaders/{mt5_login}/trades", response_model=APIResponse,
            tags=_ANALYTICS, summary="Operaciones originadas por una estrategia")
async def leader_trades(mt5_login: str, limit: Optional[int] = Query(None, ge=1, le=200),
                        cursor: Optional[int] = Query(None),
                        db: AsyncSession = Depends(get_db),
                        caller: ApiUser = Depends(require_api_key)):
    data = await MamAnalyticsService(db).leader_trades(
        mt5_login=mt5_login, limit=limit, cursor=cursor, caller=caller)
    return APIResponse(success=True, http_status=200, message="Historial obtenido", data=data)


@router.get("/mam/analytics/followers/{mt5_login}/trades", response_model=APIResponse,
            tags=_ANALYTICS, summary="Operaciones copiadas en la cuenta de un cliente")
async def follower_trades(mt5_login: str, limit: Optional[int] = Query(None, ge=1, le=200),
                          cursor: Optional[int] = Query(None),
                          db: AsyncSession = Depends(get_db),
                          caller: ApiUser = Depends(require_api_key)):
    data = await MamAnalyticsService(db).follower_trades(
        mt5_login=mt5_login, limit=limit, cursor=cursor, caller=caller)
    return APIResponse(success=True, http_status=200, message="Historial obtenido", data=data)


@router.get("/mam/analytics/leaders/{mt5_login}/subscribers", response_model=APIResponse,
            tags=_ANALYTICS, summary="Clientes conectados a una estrategia",
            description="Ojo: este listado del motor pagina con `limit`/`offset`, no con cursor.")
async def leader_subscribers(mt5_login: str,
                             subscriber_status: Optional[str] = Query(None, alias="status"),
                             limit: Optional[int] = Query(None, ge=1, le=200),
                             offset: Optional[int] = Query(None, ge=0),
                             db: AsyncSession = Depends(get_db),
                             caller: ApiUser = Depends(require_api_key)):
    data = await MamAnalyticsService(db).subscribers(
        mt5_login=mt5_login, status=subscriber_status, limit=limit, offset=offset, caller=caller)
    return APIResponse(success=True, http_status=200, message="Suscriptores obtenidos", data=data)


# ══════════════════════════════════════════════════════════════════════
# Operación
# ══════════════════════════════════════════════════════════════════════

@router.get("/mam/ops/pending", response_model=APIResponse, tags=_OPS,
            summary="Qué quedó a medias",
            description=(
                "Todo lo que espera resolución, en un solo lugar, con la razón de por qué "
                "espera.\n\n"
                "`needs_human` distingue lo que **no** se resuelve solo con los procesos "
                "periódicos: un movimiento con resultado incierto o una baja a medio camino "
                "necesitan una decisión; una suscripción cerrando posiciones no."
            ))
async def ops_pending(db: AsyncSession = Depends(get_db),
                      caller: ApiUser = Depends(require_api_key)):
    data = await MamOpsService(db).pending()
    msg = ("Nada pendiente de intervención" if data["needs_attention"] == 0
           else f"{data['needs_attention']} ítem(s) necesitan intervención")
    return APIResponse(success=True, http_status=200, message=msg,
                       data=PendingRead(**data).model_dump(mode="json"))


@router.get("/mam/ops/readiness", response_model=APIResponse, tags=_OPS,
            summary="¿Está en condiciones de operar?",
            description=(
                "Distinto de `/health`, que solo dice que el proceso responde. Acá se mira "
                "si falta configuración que **recién se nota cuando alguien intenta usar "
                "una función**: sin grupo MT5 el servicio levanta perfecto y falla en la "
                "primera creación de cuenta.\n\n"
                "`capabilities` dice qué puede hacer hoy, según lo que esté configurado."
            ))
async def ops_readiness(db: AsyncSession = Depends(get_db),
                        caller: ApiUser = Depends(require_api_key)):
    data = await MamOpsService(db).readiness()
    return APIResponse(success=True, http_status=200,
                       message="Listo para operar" if data["ready"] else "Falta configuración",
                       data=ReadinessRead(**data).model_dump(mode="json"))


@router.get("/mam/leaders/{account_login}/payment-account", response_model=APIResponse,
            tags=_LEADERS, summary="Saldo de la cuenta que recibe los fees",
            description=(
                "Saldo **en vivo** de la cuenta PAYMENT del leader.\n\n"
                "Para habilitar un retiro usá `withdrawable`, **no** `balance`: excluye el "
                "crédito MT5, que no se puede retirar.\n\n"
                "Un `409` significa que el leader no tiene una cuenta PAYMENT válida asociada."
            ))
async def payment_account_balance(account_login: str, db: AsyncSession = Depends(get_db),
                                  caller: ApiUser = Depends(require_api_key)):
    data = await MamLeaderService(db).payment_balance(account_login=account_login, caller=caller)
    return APIResponse(success=True, http_status=200, message="Saldo obtenido correctamente",
                       data=PaymentAccountBalanceRead(**{
                           k: v for k, v in data.items()
                           if k in PaymentAccountBalanceRead.model_fields
                       }).model_dump(mode="json"))


@router.post("/mam/leaders/{account_login}/payment-account/withdraw", response_model=APIResponse,
             tags=_LEADERS, summary="Retirar fees de la cuenta PAYMENT",
             description=(
                 "Retira dinero **ya acreditado** en la cuenta PAYMENT. No toca el balance "
                 "operativo del leader ni recalcula el performance fee.\n\n"
                 "Es **idempotente**: repetir la misma solicitud con la misma "
                 "`idempotency_key` devuelve `ALREADY_PROCESSED` sin volver a debitar. Usar "
                 "la misma key con **otro monto** devuelve `409`.\n\n"
                 "Queda registrado como movimiento para trazabilidad, pero **no genera "
                 "asiento contable**: ese dinero es del leader, y salió de nuestro libro "
                 "cuando se le cobró el fee al cliente."
             ))
async def payment_account_withdraw(account_login: str, body: PaymentAccountWithdrawRequest,
                                   db: AsyncSession = Depends(get_db),
                                   caller: ApiUser = Depends(require_api_key)):
    data = await MamLeaderService(db).payment_withdraw(
        account_login=account_login, amount=body.amount,
        idempotency_key=body.idempotency_key, caller=caller)
    return APIResponse(success=True, http_status=200, message="Retiro procesado correctamente",
                       data=PaymentAccountWithdrawRead(**{
                           k: v for k, v in data.items()
                           if k in PaymentAccountWithdrawRead.model_fields
                       }).model_dump(mode="json"))
