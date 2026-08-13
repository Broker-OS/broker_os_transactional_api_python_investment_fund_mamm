# broker_os_transactional_api_python_investment_fund_mamm

API transaccional B2B para operar **copy trading MetaTrader 5 sobre el motor MAM**,
con **libro contable de doble entrada** sobre una **cuenta maestra pre-fondeada**.

El consumidor (CRM, panel, app) nunca habla directo con el proveedor: toda
operación pasa por acá, queda asentada en el ledger y se puede conciliar.

> **Servicio y base de datos independientes.** Comparte estructura con el bridge
> PAMM (ledger, cuenta maestra, clientes, depósitos on-chain, OTP) pero **no
> comparte esquema ni datos**: el contrato del motor MAM es otro y mezclarlos
> daría dos libros contables del mismo dinero.

---

## El modelo MAM en una frase

El motor **no tiene cuentas "master" ni "investor"**. Crea **cuentas MAM**, y
`leader` / `follower` describen el **papel que dos cuentas juegan dentro de una
allocation**. Una misma cuenta puede seguir una estrategia y, a la vez, ser
seguida por otras.

| Concepto | Qué es |
|---|---|
| **Cuenta MAM** | Una cuenta MT5, identificada por `mt5_login`. Dos flags independientes: `can_be_leader` y `can_be_follower`. |
| **Perfil de leader** | Configuración que se le agrega a una cuenta para poder **originar** operaciones: estrategia, fee, mínimo, propagación. No es otra cuenta. |
| **Allocation** | La relación de copy trading entre dos cuentas: modo de asignación, límites, política de baja y fee. |
| **Cuenta PAYMENT** | Cuenta MT5 aparte de cada leader que **solo recibe sus performance fees**. La crea el motor sola. |

Esto es lo que más cambia respecto de una integración PAMM: no hay tipos rígidos
de cuenta, un follower puede seguir a **varios** leaders, y puede volver a
suscribirse después de darse de baja.

---

## Estado actual

| Pieza | Estado |
|---|---|
| Contabilidad de doble entrada (ledger, cuenta maestra, movimientos) | ✅ funcionando |
| Clientes (`traders`), api_users, API keys y roles | ✅ funcionando |
| Fondeo de la cuenta maestra con OTP por email | ✅ funcionando |
| Depósitos on-chain en USDC (verificados contra la cadena) | ✅ funcionando |
| Reportes contables y conciliación | ✅ funcionando |
| Modelo de dominio MAM + migración inicial | ✅ funcionando |
| Cliente HTTP del MAM API (§11 completo de la spec) | ✅ funcionando |
| Servicios de cuentas / perfiles / allocations | ⏳ pendiente |
| Endpoints `/api/v1/mam/*` | ⏳ pendiente |
| Receptor del webhook de terminación (HMAC) | ⏳ pendiente |
| Operaciones de borrado de cuentas | ⏳ pendiente |

La especificación funcional del proveedor está en
[`docs/mam-api-spec.md`](docs/mam-api-spec.md) (mismo contenido que el PDF de la
guía, **sin las credenciales**).

---

## Puesta en marcha

```bash
python -m venv venv
venv/Scripts/activate          # Linux/macOS: source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env           # completar MAM_API_BASE_URL, MAM_API_KEY y el grupo MT5
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
#   ↑ pegar el resultado en MT5_CREDENTIALS_ENCRYPTION_KEY

createdb brokeros_mam
python -m alembic upgrade head

python -m uvicorn app.main:app --reload --port 8200
```

Swagger en `http://localhost:8200/docs`. Un `GET /health` sin API key resume las
observaciones de configuración detectadas al arrancar (solo códigos, nunca valores).

### Tests

```bash
python tests/run_all.py              # solo lo que no necesita BD
python tests/run_all.py --with-db    # también las suites de BD (DESTRUCTIVO)
```

Las suites `test_db_*` **recrean el esquema**, así que exigen `TEST_DATABASE_URL`
apuntando a una base cuyo nombre contenga `test`.

---

## Autenticación y roles

Todos los endpoints bajo `/api/v1` requieren el header **`X-API-Key`**.

- **`api_user`** — quien **consume** esta API. Tiene rol `ADMIN` o `USER`.
- **`trader`** — el **cliente final**. Es un recurso, **no autentica**. El motor
  MAM no lo conoce: solo conoce sus cuentas MT5.

Lo único exclusivo de `ADMIN` es **fondear la cuenta maestra** y **gestionar
api_users y sus keys** — abrir eso dejaría que cualquier `USER` se creara un
`ADMIN` y fondeara. Todo lo demás lo hace cualquier api_user autenticado, con los
clientes scopeados por dueño: un `USER` que consulta un cliente ajeno recibe
`404`, no `403` (no se revela su existencia).

---

## Modelo contable

| Cuenta | Qué representa |
|---|---|
| `MASTER_ACCOUNT` | Tesorería pre-fondeada. De acá sale y entra todo el capital. |
| `TRADER_HOLDINGS(trader)` | Neto colocado en cada cliente. |
| `EXTERNAL_FUNDING` | Contrapartida del fondeo externo. |
| `PERF_FEE_PAID` | Acumulado de performance fees cedidos al leader. **No es plata nuestra**, por eso no toca la cuenta maestra. |

| Operación | Asiento |
|---|---|
| Fondear cuenta maestra | `debit MASTER_ACCOUNT` / `credit EXTERNAL_FUNDING` |
| Depositar en cuenta del cliente | *hold* → `credit MASTER_ACCOUNT` / `debit TRADER_HOLDINGS` |
| Retirar de cuenta del cliente | `debit MASTER_ACCOUNT` / `credit TRADER_HOLDINGS` |
| Performance fee cobrado | `debit PERF_FEE_PAID` / `credit TRADER_HOLDINGS` |

---

## Reglas del contrato que viven en la base de datos

Varias reglas de la spec están como `CHECK` constraints e índices parciales, no
solo como validaciones de servicio. Así no hay forma de violarlas ni por un bug
ni por una carga manual:

| Regla (spec) | Garantía en BD |
|---|---|
| Una cuenta no puede seguirse a sí misma (§8) | `ck_mam_allocations_not_self` |
| Una sola allocation **viva** por pareja leader/follower (§8) | `uq_mam_allocations_live_pair` |
| `FIXED` y `SCALED` exigen `mode_parameter` (§6) | `ck_mam_allocations_mode_param_required` |
| `mode_parameter` siempre > 0 (§6) | `ck_mam_allocations_mode_param_positive` |
| El performance fee es decimal entre 0 y 1 (§3.7) | `ck_mam_leader_rate`, `ck_mam_allocations_rate` |
| La cuenta PAYMENT no es la operativa (§4.5) | `ck_mam_leader_payment_distinct` |
| Una PAYMENT no se comparte entre leaders (§4.5) | `uq_mam_leader_payment_login` |
| Solo las dos máscaras MT5 soportadas (§5) | `ck_mam_accounts_rights` |
| Un webhook no se procesa dos veces (§11.6) | `ix_mam_webhook_events_event_id` (UNIQUE) |
| Una sola operación en vuelo por cuenta MT5 | `uq_movements_one_pending_per_account` |

---

## Idempotencia y reintentos

Esta es la diferencia operativa más importante frente a una integración PAMM.

**Los endpoints financieros del MAM API aceptan `idempotency_key`** (§11.1, §12).
Reintentar con la **misma** key devuelve el resultado original sin volver a
debitar MT5. La key se deriva del id de la transacción — `deposit:<id>`,
`withdrawal:<id>`, `payment-withdrawal:<id>` — y **no se regenera al reintentar**.

**Crear recursos no es idempotente**: cuentas MT5, perfiles de leader y
allocations. Ante un timeout hay que **consultar por login o por la pareja
leader/follower antes de repetir**; el cliente marca esas llamadas con
`retry_safe=False` y levanta `MAM_RESULT_UNCERTAIN`, que el llamador no debe
reintentar a ciegas.

Un `409` tampoco se reintenta ciegamente. Los `500` y `502` sí admiten backoff,
conservando la misma key cuando hay dinero de por medio.

---

## Detalles que muerden

Cosas de la spec que no son obvias y cuestan caro descubrir tarde:

- **`rights`** — si se omite, el proveedor aplica `1` (0x1), que **no** es
  ninguno de los dos perfiles soportados. Se manda siempre explícito, y solo se
  puede fijar al **crear** la cuenta: ni `accounts/add` ni el PATCH lo cambian
  después. Validar ambas máscaras contra el servidor MT5 del broker antes de
  producción.
- **`payment_account_login`** — para que el motor cree la cuenta PAYMENT sola hay
  que **omitir el campo o mandarlo `null`**. Una cadena vacía, un `0` o el login
  operativo **no** equivalen a omitir y rompen la creación automática.
- **`max_active_leaders_per_follower`** — el motor **no lo guarda**. Lo resuelve
  este servicio desde el plan del cliente y se valida solo contra esa solicitud.
  Mandar `10` hoy no deja un plan de diez conexiones guardado.
- **`min_deposit`** — se valida **exclusivamente** contra el balance MT5 del
  follower. Equity, crédito y free margin no cuentan.
- **Retiros** — cobran primero el performance fee vencido, y se rechazan si el
  free margin restante no cubre el fee más el monto pedido. Hay que conciliar lo
  solicitado contra lo efectivamente movido.
- **El webhook se registra una sola vez** — un segundo registro devuelve `409`,
  no genera eventos retroactivos, y el `signing_secret` se entrega en claro **una
  única vez**.
- **Decimales** — nunca pasan por `float`. El cliente los serializa como números
  JSON exactos; si `0.20` viajara como float, el fee del leader nacería como
  `0.2000000000000000111`.

---

## Seguridad

- La `MAM_API_KEY` vive **solo** en el backend. Nunca al navegador, ni a una app
  móvil, ni al repositorio. El logueo redacta secretos automáticamente.
- El detalle de cuenta del proveedor **devuelve credenciales MT5 en claro**: esa
  respuesta es altamente sensible y no se propaga sin pedirlo explícitamente.
- Las contraseñas MT5 se guardan **cifradas** (Fernet). Sin
  `MT5_CREDENTIALS_ENCRYPTION_KEY` el provisioning falla cerrado en vez de
  guardarlas en claro.
- El **PDF de la guía del proveedor trae la URL del ambiente y la API key en
  texto plano**: está en `.gitignore` y no debe commitearse. La versión sin
  credenciales es `docs/mam-api-spec.md`.

---

## Estructura

```
app/
  api/v1/        endpoints (traders, cuenta maestra, movimientos, reportes, cripto, admin)
  core/          config, excepciones, cifrado, redacción de logs, chequeos de arranque
  db/            engine async + Base declarativa
  models/        SQLAlchemy — mam.py concentra el dominio del motor
  repositories/  acceso a datos
  schemas/       Pydantic (request/response)
  services/      lógica de negocio; mam_client.py es el adaptador HTTP al proveedor
alembic/         migraciones (0001 crea todo el esquema)
docs/            spec funcional del MAM API
tests/           suites de verificación
```
