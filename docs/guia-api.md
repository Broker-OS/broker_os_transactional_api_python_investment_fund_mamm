# Guía completa de la API

Todo lo que hace este servicio, en un solo documento: qué es una cuenta MAM, el
flujo que hay que seguir, y la referencia de las **69 operaciones**.

- **Base URL:** `https://transactionalapi.branchtech.co/mam/`
- **Swagger:** https://transactionalapi.branchtech.co/mam/docs
- **OpenAPI:** `/mam/openapi.json`
- **Salud:** `GET /mam/health` — sin API key

> La spec funcional del proveedor está en [`mam-api-spec.md`](mam-api-spec.md).
> Este documento describe **nuestra** API, no la de él. Son contratos distintos:
> nosotros hablamos con el motor MAM, y el CRM habla con nosotros.

---

## 1. Qué es este servicio, en un párrafo

Es la capa transaccional entre tu CRM y el motor MAM del proveedor. El CRM nunca
habla directo con el proveedor: toda operación pasa por acá, **queda asentada en
un libro contable de doble entrada** sobre una cuenta maestra pre-fondeada, y se
puede conciliar después.

Hay dos cosas que este servicio sabe y el motor MAM **no**:

1. **Quién es el cliente.** El motor solo conoce cuentas MT5 sueltas. Que tres
   cuentas sean de la misma persona es un dato que vive únicamente acá.
2. **Cuánto dinero es de quién.** El motor mueve saldos en MT5; el libro contable
   dice de dónde salió cada peso y a quién se le atribuye.

---

## 2. Qué es una cuenta MAM

Esto es lo que más confunde al venir de PAMM, así que vale la pena detenerse.

**En PAMM había tipos rígidos de cuenta:** una cuenta nacía siendo *Master* o
*Investor*, y eso no cambiaba. **En MAM no existen esos tipos.** Solo hay
*cuentas MAM* — cada una es una cuenta MT5 identificada por su `mt5_login` — y
dos permisos independientes que puede tener activados a la vez:

| Flag | Qué autoriza |
|---|---|
| `can_be_leader` | La cuenta puede **originar** operaciones que otras copian. |
| `can_be_follower` | La cuenta puede **recibir** las operaciones de otra. |

La consecuencia práctica: **`leader` y `follower` no son tipos de cuenta, son
papeles dentro de una relación concreta.** La misma cuenta puede seguir a una
estrategia superior y, al mismo tiempo, ser seguida por otras. Cuando leas
`leader_login` y `follower_login` en una suscripción, están describiendo la
*dirección de la copia en esa relación puntual*, nada más.

Dos condiciones no negociables para participar en copy trading: la cuenta debe
estar `ACTIVE` y usar `account_mode = "HEDGING"`.

### Las cinco piezas del dominio

| Pieza | Qué es | Dónde vive |
|---|---|---|
| **Cliente (`trader`)** | La persona. Se identifica con un `external_reference` numérico de 12 dígitos que **asigna este servicio**, no vos. | Solo acá |
| **Cuenta MAM** | Una cuenta MT5 con sus dos flags de capacidad. | Motor + acá |
| **Perfil de leader** | Configuración que se le **agrega** a una cuenta para que pueda originar operaciones: nombre de estrategia, fee, mínimo de entrada, propagación. **No es otra cuenta.** | Motor + acá |
| **Allocation** | La relación de copy trading entre dos cuentas: modo de cálculo, límites, política de baja, fee. | Motor + acá |
| **Cuenta PAYMENT** | Cuenta MT5 aparte de cada leader que **solo** recibe sus performance fees. La crea el motor solo. | Motor |

**Por qué la cuenta PAYMENT existe:** si los fees cobrados cayeran en la cuenta
operativa del leader, se mezclarían con el capital que usa para operar y sus
métricas de rendimiento quedarían infladas por dinero que no ganó operando. Su
login vuelve en la respuesta al crear el perfil y hay que **conservarlo**: es
dato de conciliación y ya no se puede reasignar después.

### Una diferencia que cuesta cara si se pasa por alto

`can_be_leader = true` **no alcanza** para que una cuenta origine operaciones.
Además necesita un **perfil de leader** (`POST /mam/leaders`). El flag es el
permiso; el perfil es la configuración. Una cuenta con el flag pero sin perfil
rechaza cualquier intento de suscribirle un cliente.

Y hay un tercer campo que se confunde con estos dos: **`rights_profile`**, que no
tiene nada que ver con MAM — gobierna qué puede hacer el titular en la terminal
MT5. Solo admite dos valores y **se fija únicamente al crear la cuenta**. Está
explicado en detalle en el [Paso 2](#paso-2--crear-la-cuenta-mt5).

---

## 3. El flujo, de principio a fin

Este es el camino completo. Los pasos 1 a 3 se hacen una vez por estrategia; los
pasos 4 a 7, una vez por cliente.

```
  [1] Registrar el cliente          POST /traders
       └─ devuelve external_reference (12 dígitos)
            │
  [2] Crear su cuenta MT5           POST /mam/accounts
       └─ can_be_follower: true
            │
  [3] (solo estrategias)            POST /mam/leaders
       Perfil de leader              └─ crea la cuenta PAYMENT sola
            │
  [4] Fondear la maestra            POST /master-account/funding  (OTP)
       └─ o /crypto-deposits            + /funding/{id}/verify
            │
  [5] Depositar en la cuenta        POST /mam/accounts/{login}/deposits
            │
  [6] ¿Puede suscribirse?           POST /mam/allocations/eligibility
            │
  [7] Suscribir                     POST /mam/allocations
            │
       ══ operando ══
            │
  [8] Consultar                     GET /mam/traders/{ref}/overview
  [9] Conciliar fees                POST /mam/perf-fee/reconcile
 [10] Dar de baja                   POST /mam/allocations/{id}/unsubscribe
```

### Paso 1 · Registrar el cliente

```bash
curl -X POST "$BASE/api/v1/traders" \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"email":"ana@example.com","first_name":"Ana","last_name":"García",
       "max_active_leaders":1}'
```

**El `external_reference` no se envía: lo asigna el servicio** y vuelve en
`data.external_reference`. Guardalo — es como identificás al cliente en todo lo
demás.

`max_active_leaders` es el cupo de estrategias simultáneas que autoriza su plan.
Este dato **no existe en el motor MAM**: se resuelve acá y se le manda al motor
en cada suscripción.

El alta es puramente local. El cliente todavía no existe del lado del proveedor
—empieza a existir cuando se le crea una cuenta MT5.

### Paso 2 · Crear la cuenta MT5

```bash
curl -X POST "$BASE/api/v1/mam/accounts" \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"external_reference":"438434273005",
       "first_name":"Ana","last_name":"García",
       "name":"Cuenta de Ana","username":"ana@example.com",
       "can_be_leader":false,"can_be_follower":true,
       "rights_profile":"TRADING_ENABLED","leverage":100,"currency":"USD"}'
```

⚠️ **No es idempotente.** Ante un timeout, consultá por el login **antes** de
reintentar: repetir a ciegas crea una segunda cuenta real en MT5.

**`external_reference` es obligatorio acá.** Una cuenta sin cliente no puede
mover capital —el libro contable asienta por cliente— y ningún `USER` la ve. Si
la estrategia es del propio fondo, dale de alta un cliente que lo represente: no
perdés nada y te destraba depósitos, resumen y trazabilidad.

En `/register` y `/import` **sí es opcional**, porque esas cuentas ya existían y
su dueño a veces todavía no se sabe. Las que queden sin asignar aparecen en
`/mam/ops/pending` como `accounts_without_client`, y se resuelven con
`PATCH /mam/accounts/{login}`.

La respuesta **no incluye las contraseñas**. Se piden aparte en
`GET /mam/accounts/{login}/credentials`.

#### `rights_profile`: solo hay dos valores posibles

| Valor | Máscara MT5 | Qué puede hacer el titular |
|---|---|---|
| `TRADING_ENABLED` | 9073 (`0x2371`) | Permisos base MAM + trailing stop + Expert Advisors. **Puede operar.** |
| `TRADING_DISABLED` | 8981 (`0x2315`) | Permisos base MAM + flag *Trade Disabled*. **Entra y ve, pero no puede abrir operaciones.** |

**No hay un tercer valor.** Cualquier otra cosa se rechaza con `422` antes de
salir a la red, y hay una segunda validación en el cliente HTTP por si algún día
alguien arma el request por otro camino.

Técnicamente `rights` es un entero y MT5 acepta cualquier combinación de flags.
La integración se limita a dos porque la spec del proveedor lo pide así: *«No se
deben inventar ni combinar flags sin validarlos previamente en el servidor MT5
del broker»*.

El motivo importa: **una máscara mal armada no falla al crear la cuenta.** La
cuenta se crea perfecta, con su login y su contraseña. El problema aparece cuando
el cliente entra a la terminal y no puede operar — o puede hacer algo que no
debería. Del otro lado, sin ningún error que lo explique.

##### No confundirlo con `can_be_leader` / `can_be_follower`

Suenan parecido y son cosas distintas:

| Campo | Gobierna |
|---|---|
| `rights_profile` | Qué puede hacer el titular **en la terminal MT5**: entrar, operar |
| `can_be_leader` / `can_be_follower` | Qué papel juega la cuenta **dentro del copy trading MAM** |

Se combinan libremente, y hay una combinación que para un fondo suele ser la
correcta: **`TRADING_DISABLED` + `can_be_follower: true`**. La cuenta recibe todas
las operaciones que copia el motor, pero el titular **no puede operar por su
cuenta**. Es exactamente lo que querés cuando el cliente pone capital y no toca
nada — evita que arruine la estrategia abriendo posiciones por su lado.

##### Dos advertencias

**No se puede cambiar después.** `rights` solo se fija en `POST /mam/accounts`. Ni
`/register` (que no toca los permisos de una cuenta que ya existe) ni el `PATCH`
de cuenta lo modifican. Si te equivocás, corregirlo es un procedimiento
administrativo del broker sobre el servidor MT5.

**Si omitís el campo, esta API usa `TRADING_ENABLED`.** Es el default del schema.
El detalle fino: si el campo no se le mandara al *proveedor*, él usaría `1`
(`0x1`), que **no es ninguno de los dos perfiles administrados**. Por eso este
servicio siempre lo manda explícito, aunque vos no lo envíes.

**Variantes según de dónde venga la cuenta:**

| Situación | Endpoint |
|---|---|
| La cuenta no existe en ningún lado | `POST /mam/accounts` |
| Existe en MT5, el motor no la conoce | `POST /mam/accounts/register` |
| El motor ya la tiene registrada | `POST /mam/accounts/import` |

`/register` merece una advertencia: **el motor acepta cualquier número y
responde 201**, así que un login mal tipeado queda registrado como activo pero
sin saldo consultable. Por eso el servicio consulta las métricas en vivo justo
después: si vuelven en `null`, ese login no existe en el servidor del broker y
hay que darlo de baja.

### Paso 3 · Convertir una cuenta en estrategia

Solo para las cuentas que van a originar operaciones.

```bash
curl -X POST "$BASE/api/v1/mam/leaders" \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"account_login":"139682",
       "strategy_name":"Momentum Global",
       "min_deposit":1000,
       "performance_fee_rate":0.20,
       "performance_fee_period":"MONTHLY",
       "propagation_mode":"ORIGINAL_ONLY"}'
```

Si la cuenta todavía no tiene `can_be_leader`, **se habilita automáticamente**
antes de crear el perfil: son dos llamadas al motor, un solo paso desde afuera.

`performance_fee_rate` va **entre 0 y 1**. `0.20` es 20 %. Mandar `20` sería
2000 % — el schema lo rechaza, pero conviene tenerlo presente.

Dejá `payment_account_login` vacío y el motor crea la cuenta PAYMENT solo.
**Conservá el login que devuelve.**

### Paso 4 · Fondear la cuenta maestra

Todo el capital que después se coloca en cuentas de clientes sale de la cuenta
maestra. Dos vías para llenarla:

**a) Fondeo manual con OTP** (solo ADMIN, dos pasos):

```bash
curl -X POST "$BASE/api/v1/master-account/funding" \
  -H "X-API-Key: $ADMIN_KEY" -d '{"amount":50000}'
# → devuelve {id}, y manda un OTP al email del ADMIN autenticado

curl -X POST "$BASE/api/v1/master-account/funding/{id}/verify" \
  -H "X-API-Key: $ADMIN_KEY" -d '{"code":"123456"}'
```

El saldo **no se acredita** hasta el verify. Si el server no tiene transporte de
email configurado, el OTP vuelve en `data.otp_debug` para poder probar el flujo.

**b) Depósito on-chain en USDC:**

```bash
curl -X POST "$BASE/api/v1/crypto-deposits" \
  -H "X-API-Key: $KEY" \
  -d '{"tx_hash":"0xabc...","chain_id":97,"value":"5000"}'
```

No se cree nada de lo declarado: **lo único que vale es lo que dice la cadena.**
Se verifica, en orden, que el `chain_id` sea el configurado, que la transacción
exista y esté minada, que **no haya revertido**, que tenga confirmaciones
suficientes, que sus logs contengan una transferencia del token configurado
**hacia nuestra address receptora**, y que el monto coincida.

### Paso 5 · Depositar en la cuenta del cliente

```bash
curl -X POST "$BASE/api/v1/mam/accounts/146502/deposits" \
  -H "X-API-Key: $KEY" \
  -d '{"amount":5000,"idempotency_key":"deposit:crm-84922"}'
```

Mueve capital **de la cuenta maestra a la cuenta MT5** y lo asienta en el libro.

El orden importa y no es casual: el saldo se **reserva primero** en la maestra,
después se llama al motor, y recién ahí se confirma el asiento. Si el motor
rechaza, la reserva se libera. Sin esa reserva, dos depósitos simultáneos podrían
comprometer dos veces el mismo saldo.

Si el resultado queda incierto (timeout), el movimiento queda `AMBIGUOUS` y **la
reserva se mantiene** — liberarla sería peor: si el depósito sí se ejecutó,
tendrías el mismo dinero comprometido dos veces.

### Paso 6 · Verificar elegibilidad

```bash
curl -X POST "$BASE/api/v1/mam/allocations/eligibility" \
  -H "X-API-Key: $KEY" \
  -d '{"leader_login":"139682","follower_login":"146502"}'
```

Valida **sin crear nada**. Sirve para pedirle fondos al cliente antes de
intentar, en lugar de mostrarle un error después.

Comprueba dos cosas de procedencia distinta:

- **El mínimo de la estrategia** — lo valida el motor contra el balance real en
  MT5. `equity`, crédito y free margin **no cuentan** para alcanzar el mínimo.
- **El cupo del plan del cliente** — lo resuelve este servicio, porque el motor
  no lo conoce.

### Paso 7 · Suscribir

```bash
curl -X POST "$BASE/api/v1/mam/allocations" \
  -H "X-API-Key: $KEY" \
  -d '{"leader_login":"139682","follower_login":"146502",
       "allocation_mode":"EQUITY","mode_parameter":1,
       "equity_stop":1000,
       "unsubscribe_policy":"CLOSE_ON_UNSUBSCRIBE",
       "performance_fee_enabled":true,"activate":true}'
```

Internamente se crea en `PAUSED` y se activa con un PATCH aparte, aunque desde
afuera sea una sola llamada. Si activáramos en el mismo paso y fallara el
guardado, quedaría una relación copiando operaciones reales que nuestra base no
conoce.

#### Los cinco modos de asignación

`mode_parameter` **cambia de significado según el modo**:

| Modo | Qué significa `mode_parameter` | ¿Obligatorio? |
|---|---|---|
| `FIXED` | Cantidad fija de lotes que abrirá el cliente | **Sí** |
| `SCALED` | Multiplicador directo del lote del leader | **Sí** |
| `EQUITY` | Multiplicador de la proporción entre equities | No → 1 |
| `EQUITY_ROUND_DOWN` | Igual que EQUITY, con redondeo conservador | No → 1 |
| `BALANCE` | Multiplicador de la proporción entre balances | No → 1 |

Siempre **mayor que 0**; `0` o negativo da 422. Un multiplicador mayor que 1
aumenta exposición y riesgo — **no es un porcentaje**.

---

## 4. Dar de baja

Hay dos operaciones distintas que se confunden seguido:

| Quiero… | Endpoint | Qué hace |
|---|---|---|
| Pausar temporalmente | `POST /mam/allocations/{id}/status` | Deja de replicar. La relación sigue registrada. |
| **Terminar la relación** | `POST /mam/allocations/{id}/unsubscribe` | Aplica la política y **cobra el fee pendiente**. |
| Dar de baja la cuenta entera | `POST /mam/account-deletions` | Purga la cuenta del motor. Asincrónico. |

⚠️ **El PATCH de la suscripción no sirve para dar de baja.** Cancelar por ahí no
evalúa las posiciones abiertas ni cobra el fee pendiente.

**Las dos políticas de baja:**

- `CLOSE_ON_UNSUBSCRIBE` — genera los cierres de las posiciones copiadas. **No es
  instantáneo:** queda en `STOPPING` hasta que terminan; recién ahí cobra el fee
  y pasa a `CANCELLED`. **No repitas la llamada** — seguí el estado con `/sync`.
- `KEEP_OPEN` — cobra el fee, desconecta las posiciones copiadas y las deja
  abiertas en MT5, fuera de la gestión del motor.

⚠️ **No pauses una suscripción con posiciones abiertas** sin haber definido antes
cómo se administran: quedan vivas en MT5 sin que nadie las siga.

**Antes de dar de baja una cuenta**, consultá el impacto:
`GET /mam/accounts/{login}/deletion-impact`. Analiza sin modificar nada. Si la
cuenta tiene perfil de estrategia, la baja puede arrastrar a sus seguidores.

La baja de cuenta recorre `PENDING → WAITING_CLOSE → PURGING → COMPLETED`. **No
archives la cuenta en otros sistemas hasta ver `COMPLETED`.** Y ojo: elimina la
cuenta del **servicio MAM**, no el usuario del servidor MT5 — eso es un
procedimiento administrativo del broker.

---

## 5. Autenticación, roles y envelope

### API key

Toda la API v1 exige el header **`X-API-Key`**:

```bash
-H "X-API-Key: tu_api_key"
```

> No confundir con la spec del proveedor, que usa `Authorization: Bearer`. Esa es
> la key con la que **nosotros** hablamos con el motor, y nunca sale de este
> servidor.

Las keys se emiten desde `POST /admin/api-users` y se muestran **una sola vez**
(en base solo queda el hash).

### Roles

| Rol | Alcance |
|---|---|
| `ADMIN` | Todo. Mete plata al fondo, ajusta asientos y emite las keys. |
| `USER` | Toda la operación del negocio, viendo **solo sus propios** clientes. |

Un `USER` hace el trabajo completo: clientes, cuentas, estrategias, suscripciones,
depósitos y retiros de cuentas de clientes, performance fee, bajas, rendimiento y
reportes. Lo único que no puede es **meter plata nueva al fondo** ni **repartir
identidades**.

**Los nueve endpoints exclusivos de ADMIN**, y son solo tres ideas:

```
Fondear la maestra          POST /master-account/funding
                            POST /master-account/funding/{id}/verify
                            POST /master-account/funding/{id}/resend

Ajustar el libro            POST /mam/accounts/{login}/regularize-capital

Emitir identidades          POST   /admin/api-users
                            GET    /admin/api-users
                            PATCH  /admin/api-users/{id}
                            DELETE /admin/api-users/{id}
                            POST   /admin/api-users/{id}/regenerate-key
```

El tercer grupo **no es una restricción aparte: es la cerradura de las otras dos.**
Si un USER pudiera crear api_users, se crearía uno con rol ADMIN, pediría su key y
con esa key fondearía. Lo mismo con el PATCH (se cambiaría el rol a sí mismo) y
con `regenerate-key` (emitiría una key nueva para un ADMIN ajeno). Abrir la
gestión de identidades equivale a abrir todo lo demás.

`GET /master-account` (consultar el saldo) **no** es ADMIN: un socio necesita
saber si hay fondos antes de intentar un depósito.

Cuando un `USER` consulta un cliente ajeno recibe **`404`, no `403`**: distinguir
los dos casos permitiría enumerar la cartera de otro socio. Vale para `/traders`,
`/mam/accounts`, `/movements` y `/ledger/transactions` por igual — si una sola de
esas puertas distinguiera, la protección de las otras no serviría de nada.

Los reportes **agregados** (`/ledger/accounts`, `/reports/balances`,
`/reports/daily`, `/master-account`) siguen siendo globales: son el estado del
fondo, no de una cartera.

Un caso que sorprende y es deliberado: los **pagos de fee sin atribuir** —cuya
cuenta pagadora no está en esta base, así que no se sabe de qué cliente son— **no
le aparecen a ningún USER**. No son de nadie, y asignárselos a un socio sería
adivinar. Quedan a la vista del ADMIN y en `/mam/ops/pending`.

### Envelope de respuesta

Éxito:

```json
{ "success": true, "http_status": 200, "message": "...", "data": { } }
```

Error:

```json
{ "success": false, "http_status": 409,
  "error": { "code": "MIN_DEPOSIT_NOT_MET", "message": "...", "detail": "..." } }
```

### Códigos que más vas a ver

| Código | Cuándo |
|---|---|
| `MIN_DEPOSIT_NOT_MET` | El balance no llega al mínimo de la estrategia |
| `MAX_ACTIVE_LEADERS_REACHED` | El cliente agotó el cupo de su plan |
| `ALLOCATION_ALREADY_LIVE` | Ya existe una suscripción viva para ese par |
| `SELF_FOLLOW` | Una cuenta no puede seguirse a sí misma |
| `ACCOUNT_NOT_HEDGING` | La cuenta no usa HEDGING |
| `LEADER_CAPABILITY_MISSING` | Falta `can_be_leader` o el perfil |
| `MODE_PARAMETER_REQUIRED` | Falta el parámetro en `FIXED` / `SCALED` |
| `PAYMENT_ACCOUNT_UNAVAILABLE` | El leader no tiene cuenta PAYMENT válida |
| `PROVIDER_UNCERTAIN_RESULT` | Timeout: no se sabe si el dinero se movió |
| `DEPOSIT_ALREADY_REGISTERED` | Ese hash on-chain ya se acreditó |

---

## 6. Contabilidad

El libro es de **doble entrada**: cada transacción tiene asientos que se
compensan, y siempre se cumple `balance_after = balance_before + debit − credit`.

**Cuentas contables:** `MASTER_ACCOUNT`, `EXTERNAL_FUNDING`, `TRADER_HOLDINGS`,
`PF_PAYABLE`.

**Tipos de transacción:** `TRADER_DEPOSIT`, `TRADER_WITHDRAWAL`,
`MASTER_ACCOUNT_FUNDING`, `PERF_FEE`.

El sentido cuesta al principio: **depositarle a un cliente baja** la cuenta
maestra (`credit`) y **retirarle la sube** (`debit`) — el dinero sale del fondo
común y vuelve a él.

### El P&L del trading NO lleva asiento

Esta es la confusión más cara de la contabilidad del fondo, así que va con
ejemplo. Supongamos una cuenta a la que le depositaste $1.000 y hoy tiene $483,88
porque perdió operando:

| | Dice |
|---|---|
| Saldo MT5 | 483,88 — **cuánto vale hoy** |
| `TRADER_HOLDINGS` del cliente | 1.000,00 — **cuánto se le colocó** |

**Que no coincidan es lo correcto.** El libro contable registra *movimientos de
caja*: dinero que entró o salió del fondo. El P&L no es eso — el dinero no se
movió a ningún lado, cambió de valor.

Si asentaras esa diferencia de −516,12, tu libro estaría inventando una salida de
caja que nunca ocurrió. Y al revés con las ganancias: una cuenta que ganó $292,80
copiando no recibió $292,80 de nadie.

Para ver las dos cifras juntas está `GET /mam/traders/{ref}/overview`, que cruza
el capital colocado según el libro contra el equity en vivo de MT5.

### Capital que entró sin pasar por acá

Distinto del caso anterior, y este **sí** lleva asiento. Una cuenta puede tener
saldo que el libro no conoce: el broker la acreditó directo en MT5, o venía con
saldo de antes de la integración. El motor lo marca con un tipo `EXTERNAL_*`,
distinto de los `DEPOSIT`/`WITHDRAWAL` que originamos nosotros.

```bash
# 1. Simular — no escribe nada
curl -X POST "$BASE/api/v1/mam/accounts/7918234/regularize-capital?apply=false" \
  -H "X-API-Key: $ADMIN_KEY"

# 2. Si el detalle cuadra, aplicar
curl -X POST "$BASE/api/v1/mam/accounts/7918234/regularize-capital?apply=true" \
  -H "X-API-Key: $ADMIN_KEY"
```

⚠️ **No mueve un peso en MT5.** Solo escribe el asiento que faltaba. Para mover
dinero de verdad está `/deposits` — usarlo acá depositaría el importe **otra vez**
y duplicaría el saldo.

El monto **no es un parámetro**: se lee del motor. No se puede inventar una cifra,
solo asentar lo que el motor dice que entró. Es ADMIN e idempotente por el id de
la transacción del motor.

La cuenta necesita **cliente asignado**: el capital se asienta contra un cliente.

### Idempotencia

Todas las operaciones financieras aceptan `idempotency_key`. **Derivala del id de
tu transacción** (`deposit:1234`) y **no la regeneres al reintentar** — con la
misma key el motor no duplica el movimiento.

Excepción importante: `POST /mam/accounts` (crear cuenta MT5) **no es
idempotente**.

---

## 7. Performance fee

Se controla por suscripción, con High-Water Mark. Períodos disponibles: `OFF`,
`MINUTELY`, `HOURLY`, `DAILY`, `WEEKLY`, `SEMIMONTHLY`, `MONTHLY`.

**El problema que resuelve la conciliación:** el crédito que llega a la cuenta
PAYMENT viene **consolidado** — una sola fila por acreditación, sin decir cuánto
aportó cada cliente. Sin ese detalle no se pueden repartir comisiones a sponsors
ni a una red de IBs.

```bash
# Traer el detalle por cliente y asentarlo
curl -X POST "$BASE/api/v1/mam/perf-fee/reconcile" \
  -H "X-API-Key: $KEY" \
  -d '{"master_login":"139682","from_at":"2026-08-01T00:00:00Z"}'

# ¿Cuadra el detalle contra lo que acreditó el motor?
curl "$BASE/api/v1/mam/perf-fee/verify?master_login=139682" -H "X-API-Key: $KEY"

# Lo cobrado y sin asentar (lo que hay que resolver a mano)
curl "$BASE/api/v1/mam/perf-fee/payments?only_unposted=true" -H "X-API-Key: $KEY"
```

Es **idempotente**: corrélo las veces que quieras sobre el mismo período.

Si una cuenta cliente no está en esta base, su pago queda **registrado pero sin
asiento** y aparece en `pending_attribution`. Es deliberado: preferimos un pago
visible sin asiento que un asiento cargado al cliente equivocado. Se resuelve
importando la cuenta y volviendo a conciliar.

**Si `verify` devuelve `matches: false`**, o faltan pagos por traer o el motor
acreditó algo que no está detallando. Resolvelo **antes** de repartir comisiones
sobre un total que no cierra.

### Dos retiros distintos

| Quién retira | Endpoint | Asiento contable |
|---|---|---|
| El cliente, de su cuenta operativa | `POST /mam/accounts/{login}/withdrawals` | **Sí** |
| El leader, de su cuenta PAYMENT | `POST /mam/leaders/{login}/payment-account/withdraw` | **No** |

El segundo no genera asiento porque **ese dinero ya no es nuestro**: salió del
libro cuando se le cobró el fee al cliente.

⚠️ **El retiro del cliente cobra primero el performance fee vencido.** El motor
rechaza la operación si el free margin restante no cubre el fee más el monto
pedido — no hay retiros parciales: sale completo o falla. Lo que sí varía es
cuánto pierde el cliente en total: el monto pedido **más** el fee cobrado en el
camino, que viene en `perf_fee_at_request`.

Para habilitar un retiro de la cuenta PAYMENT usá **`withdrawable`**, no
`balance`: excluye el crédito MT5, que no se puede retirar.

---

## 8. El webhook del proveedor

`POST /webhooks/mam` — lo llama **el proveedor**, no vos.

Cuelga **fuera de `/api/v1`** a propósito: ese router exige nuestra API key, y el
proveedor no la tiene. Autentica firmando el cuerpo con `HMAC-SHA256` sobre un
secreto compartido. Meterlo bajo la API key obligaría a darle nuestra clave a un
tercero para que nos avise de algo.

La firma se verifica sobre los **bytes exactos** del cuerpo antes de deserializar
el JSON, se compara en tiempo constante, y se rechazan eventos de más de cinco
minutos.

**Recibir un evento no inicia ni autoriza una baja:** informa una terminación que
ya ocurrió del otro lado.

Los eventos que no se pudieron aplicar (casi siempre porque la suscripción no
estaba importada) se ven en `GET /mam/webhook-events?only_pending=true` y se
reprocesan con `POST /mam/webhook-events/retry`.

---

## 9. Procesos periódicos

Cuatro cosas no terminan en el mismo instante en que empiezan y necesitan que
alguien las siga:

| Proceso | Endpoint | Frecuencia |
|---|---|---|
| Bajas de suscripción en curso | `POST /mam/allocations/sync` | 10 min |
| Bajas de cuenta en curso | `POST /mam/account-deletions/sync` | 10 min |
| Eventos del webhook sin aplicar | `POST /mam/webhook-events/retry` | 15 min |
| Conciliación de performance fee | `POST /mam/perf-fee/reconcile` | 2 h |

Se configuran con `scripts/setup_crons.py`.

---

## 10. Operación

Dos endpoints para saber si el servicio está bien, y son distintos:

| | Qué contesta |
|---|---|
| `GET /mam/health` | ¿El proceso responde **y la base contesta**? Sin API key. Devuelve `503` si Postgres está caído. |
| `GET /mam/api/v1/mam/ops/readiness` | ¿Está en condiciones de **operar de verdad**? |

La diferencia importa: falta de configuración **no** tumba el `/health` — el
servicio levanta perfecto y falla recién cuando alguien usa la función. Sin grupo
MT5 configurado, todo se ve bien hasta la primera creación de cuenta.

`GET /mam/api/v1/mam/ops/pending` junta en un solo lugar todo lo que quedó a
medias, con la razón. `needs_human` distingue lo que **no** se resuelve solo con
los procesos periódicos: un movimiento con resultado incierto o una baja a medio
camino necesitan una decisión; una suscripción cerrando posiciones, no.

---

## 11. Referencia completa de endpoints

Todos bajo `https://transactionalapi.branchtech.co/mam/api/v1/` salvo donde se
indique. Todos exigen `X-API-Key`.

### Configuración · Administradores y claves (solo ADMIN)

| Método | Ruta | Qué hace |
|---|---|---|
| POST | `/admin/api-users` | Crear api_user y emitir su key (**se muestra una vez**) |
| GET | `/admin/api-users` | Listar api_users |
| PATCH | `/admin/api-users/{id}` | Editar nombre/email/rol/avisos. **No toca la key** |
| POST | `/admin/api-users/{id}/regenerate-key` | Revocar y emitir nueva key |
| DELETE | `/admin/api-users/{id}` | Eliminar. Bloqueado si tiene clientes o es el último ADMIN |

### Configuración · Cuenta maestra

| Método | Ruta | Qué hace |
|---|---|---|
| POST | `/master-account/funding` | **ADMIN.** Paso 1: solicita fondeo, manda OTP |
| POST | `/master-account/funding/{id}/verify` | **ADMIN.** Paso 2: valida el OTP y acredita |
| POST | `/master-account/funding/{id}/resend` | **ADMIN.** Reenvía el OTP regenerándolo |
| GET | `/master-account` | Saldo: `balance`, `pending_debit`, `available` |

### Clientes

| Método | Ruta | Qué hace |
|---|---|---|
| POST | `/traders` | Registrar cliente. Devuelve el `external_reference` |
| GET | `/traders` | Listar (ADMIN ve todos; USER, los suyos) |
| GET | `/traders/{external_reference}` | Detalle |

### MAM · Cuentas

| Método | Ruta | Qué hace |
|---|---|---|
| POST | `/mam/accounts` | Crear cuenta MT5 nueva. **No idempotente** |
| POST | `/mam/accounts/register` | Registrar una que ya existe en MT5 |
| POST | `/mam/accounts/import` | Importar una que el motor ya conoce |
| GET | `/mam/accounts` | Listar. Filtros: `external_reference`, `can_be_leader`, `can_be_follower`, `status` |
| GET | `/mam/accounts/{mt5_login}` | Detalle |
| PATCH | `/mam/accounts/{mt5_login}` | Capacidades, estado, dueño. **`rights` no se cambia acá** |
| GET | `/mam/accounts/{mt5_login}/metrics` | Balance y equity **en vivo desde MT5** |
| GET | `/mam/accounts/{mt5_login}/credentials` | ⚠️ Contraseñas en claro |

### MAM · Estrategias (leader)

| Método | Ruta | Qué hace |
|---|---|---|
| POST | `/mam/leaders` | Crear perfil. Crea la cuenta PAYMENT sola |
| POST | `/mam/leaders/import` | Importar un perfil que el motor ya tiene |
| GET | `/mam/leaders` | Listar |
| GET | `/mam/leaders/{account_login}` | Detalle (por login **operativo**) |
| PATCH | `/mam/leaders/{account_login}` | Editar. La PAYMENT no se reasigna |
| GET | `/mam/leaders/{account_login}/payment-account` | Saldo en vivo. Usar `withdrawable` |
| POST | `/mam/leaders/{account_login}/payment-account/withdraw` | Retirar fees. Sin asiento |

### MAM · Suscripciones (allocations)

| Método | Ruta | Qué hace |
|---|---|---|
| POST | `/mam/allocations/eligibility` | Validar **sin crear nada** |
| POST | `/mam/allocations` | Suscribir |
| POST | `/mam/allocations/import` | Importar una que el motor ya tiene |
| GET | `/mam/allocations` | Listar. Filtros: `leader_login`, `follower_login`, `status` |
| GET | `/mam/allocations/{id}` | Detalle |
| PATCH | `/mam/allocations/{id}` | Cambiar config. **No sirve para dar de baja** |
| POST | `/mam/allocations/{id}/status` | Pausar / reactivar |
| POST | `/mam/allocations/{id}/unsubscribe` | **Dar de baja correctamente** |
| POST | `/mam/allocations/sync` | Cron: refrescar las `STOPPING` |

### MAM · Capital

| Método | Ruta | Qué hace |
|---|---|---|
| POST | `/mam/accounts/{mt5_login}/deposits` | Maestra → cuenta del cliente. Idempotente |
| POST | `/mam/accounts/{mt5_login}/withdrawals` | Cuenta → maestra. **Cobra el fee vencido primero** |
| GET | `/mam/accounts/{mt5_login}/balance-transactions` | Historial **según el motor** (para conciliar) |
| POST | `/mam/accounts/{mt5_login}/regularize-capital` | **ADMIN.** Asentar capital que entró sin pasar por acá. No toca MT5 |

### MAM · Performance fee

| Método | Ruta | Qué hace |
|---|---|---|
| POST | `/mam/perf-fee/reconcile` | Traer el detalle por cliente y asentarlo. Idempotente |
| GET | `/mam/perf-fee/verify` | ¿Cuadra el detalle contra lo acreditado? |
| GET | `/mam/perf-fee/payments` | Pagos conciliados. **Base del cálculo de comisiones**. Scopeado |
| GET | `/mam/webhook-events` | Eventos recibidos del motor |
| POST | `/mam/webhook-events/retry` | Cron: reintentar los sin aplicar |

### MAM · Baja de cuentas

| Método | Ruta | Qué hace |
|---|---|---|
| GET | `/mam/accounts/{mt5_login}/deletion-impact` | **Consultar antes de dar de baja** |
| POST | `/mam/account-deletions` | Crear la baja (asincrónica) |
| GET | `/mam/account-deletions` | Listar. Scopeado por dueño de la cuenta |
| GET | `/mam/account-deletions/{operation_id}` | Estado |
| POST | `/mam/account-deletions/{operation_id}/retry` | Reintentar una `PARTIAL` |
| POST | `/mam/account-deletions/sync` | Cron: refrescar las no finales |

### MAM · Rendimiento

| Método | Ruta | Qué hace |
|---|---|---|
| GET | `/mam/traders/{external_reference}/overview` | **Todo lo del cliente en una respuesta** |
| POST | `/mam/analytics/leaders/performance` | Rendimiento de una o varias estrategias |
| POST | `/mam/analytics/followers/performance` | Rendimiento de cuentas de clientes |
| GET | `/mam/analytics/leaders/{mt5_login}/trades` | Operaciones originadas |
| GET | `/mam/analytics/followers/{mt5_login}/trades` | Operaciones copiadas |
| GET | `/mam/analytics/leaders/{mt5_login}/subscribers` | Clientes conectados. Pagina con `limit`/`offset` |

### Operación

| Método | Ruta | Qué hace |
|---|---|---|
| GET | `/mam/ops/pending` | Qué quedó a medias y por qué |
| GET | `/mam/ops/readiness` | ¿Está en condiciones de operar? |

### Consultas y reportes

| Método | Ruta | Qué hace |
|---|---|---|
| GET | `/movements` | Historial de movimientos. Scopeado por dueño |
| GET | `/movements/{movement_id}` | Detalle |
| GET | `/ledger/accounts` | Cuentas contables con saldo. **Global** |
| GET | `/ledger/transactions` | **Libro diario** con asientos. Scopeado por dueño |
| GET | `/reports/balances` | Saldos a una fecha (point-in-time) |
| GET | `/reports/daily` | Resumen diario por rango |

### Depósitos on-chain

| Método | Ruta | Qué hace |
|---|---|---|
| POST | `/crypto-deposits` | Presentar comprobante USDC y verificarlo en la cadena |
| GET | `/crypto-deposits` | Historial, incluidos los rechazados con su motivo |

### Fuera de `/api/v1`

| Método | Ruta | Auth |
|---|---|---|
| GET | `/mam/health` | Ninguna |
| POST | `/mam/webhooks/mam` | Firma HMAC del proveedor |

---

## 12. Las trece cosas que más se rompen

1. **Crear una cuenta dos veces.** `POST /mam/accounts` no es idempotente.
   Consultá antes de reintentar.
2. **Registrar un login mal tipeado.** El motor devuelve 201 igual. Verificá que
   las métricas no vengan en `null`.
3. **Poner el fee como `20` en vez de `0.20`.** La tasa va entre 0 y 1.
4. **Dar de baja con el PATCH.** No cobra el fee ni evalúa posiciones abiertas.
   Usá `/unsubscribe`.
5. **Repetir el `/unsubscribe`** porque quedó en `STOPPING`. No es instantáneo:
   seguilo con `/sync`.
6. **Olvidar el perfil de leader.** El flag `can_be_leader` solo no alcanza.
7. **No importar una suscripción preexistente.** Consume cupo del plan y
   participa en la detección de ciclos; sin importarla, las validaciones trabajan
   sobre un mapa incompleto.
8. **Regenerar la `idempotency_key` al reintentar.** Convierte un reintento en un
   movimiento nuevo.
9. **Usar `balance` en vez de `withdrawable`** para habilitar un retiro de la
   cuenta PAYMENT. Incluye crédito MT5, que no se puede retirar.
10. **Repartir comisiones con `verify` en `matches: false`.** El total no cierra.
11. **Querer que el saldo MT5 coincida con el holding contable.** No tienen por
    qué: uno dice cuánto vale hoy, el otro cuánto se colocó. La diferencia es
    P&L, y asentarla sería inventar movimientos de caja.
12. **Usar `/deposits` para regularizar un saldo preexistente.** Deposita el
    dinero de verdad y duplica el saldo. Para eso está `/regularize-capital`.
13. **Equivocarse en `rights_profile` al crear la cuenta.** No se puede corregir
    después por API: es un procedimiento administrativo del broker. Y si querés
    que el cliente no opere por su cuenta, `TRADING_DISABLED` **no** le impide
    recibir las operaciones copiadas — es justo la combinación que suele buscarse.
