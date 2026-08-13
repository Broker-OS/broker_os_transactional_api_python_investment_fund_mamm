"""Punto de entrada del servicio Broker OS · Investment Fund MAM."""
import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.exceptions import AppException
from app.core.log_redaction import install as install_log_redaction
from app.core.startup_checks import log_findings, run_checks, summary
from app.schemas.common import APIResponse, ErrorDetail, error_response

logging.basicConfig(level=logging.INFO)
# Red de seguridad: ningun secreto (API key, contrasenas MT5) llega a un log.
install_log_redaction()

_DESCRIPTION = """
API **B2B server-to-server** para operar copy trading MetaTrader 5 sobre el
**motor MAM**, con **libro contable de doble entrada** sobre una **cuenta maestra
pre-fondeada**. Toda operación pasa por esta API: el consumidor nunca habla
directo con el proveedor.

---

## Autenticación
Todos los endpoints bajo `/api/v1` requieren el header **`X-API-Key`**.
En Swagger tocá **Authorize** (arriba a la derecha) y pegá tu key.

## El modelo MAM en una frase
El motor **no tiene cuentas "master" ni "investor"**. Crea **cuentas MAM**, y
`leader` / `follower` describen el **papel que dos cuentas juegan dentro de una
allocation**. Una misma cuenta puede seguir una estrategia y, a la vez, ser
seguida por otras.

| Concepto | Qué es |
|---|---|
| **Cuenta MAM** | Una cuenta MT5. Se identifica por `mt5_login`. Dos flags independientes: `can_be_leader` y `can_be_follower`. |
| **Perfil de leader** | Configuración que se le agrega a una cuenta para que pueda **originar** operaciones: estrategia, fee, mínimo, propagación. No es otra cuenta. |
| **Allocation** | La relación de copy trading entre dos cuentas: modo de asignación, límites, política de baja y fee. |
| **Cuenta PAYMENT** | Cuenta MT5 aparte de cada leader que **solo recibe sus performance fees**. La crea el motor sola. |

## Dos tipos de usuario (no se mezclan)
- **`api_user`** — quien **consume** esta API. Autentica con API key, tiene **rol**
  (`ADMIN` / `USER`) y es dueño de su key.
- **`trader`** — el **cliente final**. Es un *recurso* que gestionás a través de
  la API; **no autentica**. El motor MAM no lo conoce: solo conoce sus cuentas MT5.

## Roles
| Capacidad | `ADMIN` | `USER` |
|---|:---:|:---:|
| Fondear la cuenta maestra (OTP a su email) | Sí | No (`403`) |
| Gestionar api_users y sus API keys | Sí | No (`403`) |
| Reportes globales y ledger | Sí | No (`403`) |
| Registrar y operar clientes | Sí (todos) | Sí (solo los suyos) |

> Un `USER` que consulta un cliente ajeno recibe **`404`** (no se revela su existencia).

## Formato de respuesta (sobre uniforme)
- **OK** → `{ "success": true, "http_status": 200, "message": "...", "data": { … } }`
- **Error** → `{ "success": false, "http_status": 4xx, "error": { "code", "message", "detail" } }`

## Modelo contable (doble entrada)
- `MASTER_ACCOUNT` — cuenta maestra pre-fondeada; de acá sale/entra la plata.
- `TRADER_HOLDINGS(trader)` — neto colocado en cada cliente.
- `EXTERNAL_FUNDING` — contrapartida del fondeo.
- `PERF_FEE_PAID` — acumulado de performance fees cedidos al leader. **No es
  plata nuestra**, por eso no toca la cuenta maestra.

| Operación | Asiento contable |
|---|---|
| Fondear cuenta maestra (manual u on-chain) | `debit MASTER_ACCOUNT` / `credit EXTERNAL_FUNDING` |
| Depositar en cuenta del cliente | *hold* → `credit MASTER_ACCOUNT` / `debit TRADER_HOLDINGS` |
| Retirar de cuenta del cliente | `debit MASTER_ACCOUNT` / `credit TRADER_HOLDINGS` |
| Performance fee cobrado | `debit PERF_FEE_PAID` / `credit TRADER_HOLDINGS` |

## Idempotencia
Los endpoints financieros del motor MAM **aceptan `idempotency_key`**, así que un
reintento con la misma key no duplica el movimiento. La key se deriva del id de
la transacción (`deposit:<id>`, `withdrawal:<id>`) y **no se regenera al
reintentar**. Lo que **no** es idempotente es crear cuentas MT5, perfiles y
allocations: ante un timeout hay que **consultar antes de repetir**.
"""

# El orden de esta lista es el orden en que Swagger muestra las secciones.
_TAGS_METADATA = [
    {"name": "1. Configuración · Administradores y claves", "description":
        "**Configuración inicial (una vez).** Gestión de api_users y sus API keys: crear, listar, editar, "
        "regenerar y eliminar. **Solo ADMIN.**"},
    {"name": "2. Configuración · Cuenta maestra", "description":
        "**Configuración.** Fondear la cuenta maestra (**2 pasos con OTP** al email del admin) y ver su saldo. "
        "**Solo ADMIN.**"},
    {"name": "3. Clientes", "description":
        "Registrar y listar clientes. Un USER solo ve los suyos."},
    {"name": "4. MAM · Cuentas", "description":
        "Crear cuentas MT5 nuevas o registrar existentes, consultar su balance en vivo, "
        "entregar credenciales y cambiar sus capacidades (`can_be_leader` / `can_be_follower`)."},
    {"name": "5. MAM · Estrategias (leader)", "description":
        "Convertir una cuenta en estrategia seguible: fee, mínimo de suscripción, "
        "visibilidad y propagación. Incluye el saldo y el retiro de la **cuenta PAYMENT** "
        "donde se acumulan los performance fees del leader."},
    {"name": "6. MAM · Suscripciones (allocations)", "description":
        "Conectar un cliente con una estrategia: validar elegibilidad, suscribir, "
        "ajustar el modo de asignación, pausar y dar de baja. La baja aplica la política "
        "configurada y cobra el performance fee pendiente."},
    {"name": "7. MAM · Capital (depósito/retiro)", "description":
        "Mover capital entre la cuenta maestra y las cuentas MT5 de los clientes, con su "
        "asiento contable. El retiro **cobra primero el performance fee vencido**, que no "
        "vuelve a la tesorería sino a la cuenta PAYMENT del leader."},
    {"name": "9. Consultas y reportes", "description":
        "Historial de movimientos, conciliación ledger↔MT5 y reportes contables."},
    {"name": "12. Depósitos on-chain (cripto)", "description":
        "Presentar un comprobante de pago en **USDC sobre una cadena EVM** y verificarlo "
        "contra la cadena. Si es válido **acredita la cuenta maestra** y avisa a los ADMIN "
        "por email. Nada se cree por lo declarado: lo único que vale es lo que dice la cadena."},
    {"name": "Salud", "description": "Chequeo de estado del servicio. **No requiere API key.**"},
]

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.VERSION,
    description=_DESCRIPTION,
    root_path=settings.ROOT_PATH,
    openapi_tags=_TAGS_METADATA,
    contact={"name": "Broker OS · Investment Fund MAM"},
    swagger_ui_parameters={
        "persistAuthorization": True,
        "displayRequestDuration": True,
        "docExpansion": "none",
        "filter": True,
        "tryItOutEnabled": True,
    },
)


@app.exception_handler(AppException)
async def _app_exception_handler(_: Request, exc: AppException):
    return error_response(exc)


@app.exception_handler(RequestValidationError)
async def _validation_handler(_: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content=APIResponse(
            success=False, http_status=422,
            error=ErrorDetail(code="VALIDATION_ERROR", message="Datos de entrada invalidos",
                              detail=str(exc.errors())[:400]),
        ).model_dump(mode="json"),
    )


@app.on_event("startup")
async def _startup_config_check() -> None:
    """Revisa la configuración al arrancar y deja constancia en el log."""
    log_findings(run_checks())


@app.get("/health", tags=["Salud"], summary="Chequeo de estado",
         description=(
             "Verifica que el servicio esté arriba. No requiere API key.\n\n"
             "`config` resume las observaciones de configuración detectadas al arrancar "
             "(solo códigos y conteos, nunca valores): HTTPS del proveedor, secretos "
             "faltantes, grupo MT5 sin definir."
         ))
async def health():
    return {"status": "healthy", "app": settings.APP_NAME, "version": settings.VERSION,
            "config": summary()}


app.include_router(api_router)
