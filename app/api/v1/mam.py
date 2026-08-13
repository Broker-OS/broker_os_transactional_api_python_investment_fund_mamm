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
    PaymentAccountWithdrawRead,
    PaymentAccountWithdrawRequest,
)
from app.services.mam_account_service import MamAccountService
from app.services.mam_leader_service import MamLeaderService

router = APIRouter()

_CUENTAS = ["4. MAM · Cuentas"]
_LEADERS = ["5. MAM · Estrategias (leader)"]


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
        can_be_follower=body.can_be_follower, status=body.status, caller=caller)
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
