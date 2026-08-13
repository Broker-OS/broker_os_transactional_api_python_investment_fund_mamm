# MAM API Integration Specification

> Especificación funcional para integrar un CRM o backend con la MAM API. Este documento describe el contrato público de integración y omite deliberadamente la arquitectura y los componentes operativos internos.

- **Versión del documento:** 1.0
- **Actualizado:** 2026-08-13
- **Audiencia:** equipos de backend, integradores y agentes de desarrollo

## Accesos

- **Base URL:** `<MAM_API_BASE_URL>`
- **API key:** `<MAM_API_KEY>`
- **Swagger:** `<MAM_API_BASE_URL>/docs`
- **OpenAPI:** `<MAM_API_BASE_URL>/openapi.json`

## 1. Propósito y alcance

La MAM API permite integrar cuentas MetaTrader 5, perfiles de leader, allocations de copy trading, movimientos de saldo, métricas y analytics.

Esta guía explica:

- Cómo autenticar un sistema externo.
- Cómo crear o registrar cuentas MT5 sin asignarles un tipo rígido.
- Cómo habilitar una cuenta para actuar como leader, follower o ambos.
- Cómo agregar un perfil de leader únicamente cuando una cuenta lo necesita.
- Cómo conectar dos cuentas mediante una allocation.
- Cómo configurar y validar el balance mínimo requerido para suscribirse.
- Cómo elegir y configurar el modo de asignación.
- Cómo depositar, retirar, actualizar y desuscribir cuentas.
- Cómo consultar métricas, operaciones, subscribers y performance fees.
- Cómo administrar la cuenta PAYMENT segregada de cada leader.
- Cómo consultar qué investors componen cada pago de performance fee.
- Cómo eliminar de forma segura cuentas master e investor del servicio MAM.
- Cómo registrar y consumir el webhook de terminación de allocations.
> NOTA
> La API no crea entidades separadas llamadas master o investor. Siempre crea o registra cuentas MAM. leader y follower describen el papel que dos cuentas desempeñan dentro de una allocation.

## 2. Modelo de integración

El CRM o backend del cliente consume la MAM API mediante HTTPS y conserva la relación entre sus propios usuarios y el mt5_login de cada cuenta.

En una integración directa, el CRM administra usuarios, productos, límites y permisos en su propio sistema. Por tanto, debe omitir user_id y metatrader_account_product_id: ambos pertenecen a la solución integral de la plataforma y no representan IDs del CRM externo.

Toda llamada debe realizarse desde un backend seguro. La API key no debe exponerse en frontend ni aplicaciones móviles.

### 2.1 Perfil de campos para esta integración

| Campo | Uso correcto en integración directa |
| --- | --- |
| user_id | Omitir. Es una referencia interna de la solución integral; no es el ID del cliente en el CRM. |
| metatrader_account_product_id | Omitir. Referencia productos internos de la solución integral. |
| platform_group | Enviar al crear una cuenta nueva. Debe ser el grupo MT5 real asignado por el broker. |
| mt5_login | Guardar en el CRM. Es el identificador público de la cuenta para consultas y relaciones. |
| leader_id | Guardar. El PATCH del perfil de leader utiliza este ID interno. |
| allocation_id | Guardar. Identifica la relación para consultar, actualizar o desuscribir. |
| external_reference | No pertenece al motor MAM directo. El CRM lo resuelve localmente y lo asocia con mt5_login. |
| payment_account_login | Omitir al crear un leader nuevo, o enviar null. La API crea la PAYMENT y devuelve su login. |

Que Swagger muestre un campo no significa que el cliente deba enviarlo. Si user_id o metatrader_account_product_id se envían, la API intentará resolver referencias internas; no son campos decorativos ni se ignoran silenciosamente.

### 2.2 Omitir, enviar null y enviar valores vacíos

- Omitir un campo opcional permite que la API aplique su valor predeterminado o comportamiento automático.
- Enviar null solo es válido cuando esta guía o el schema OpenAPI lo permiten expresamente.
- Enviar una cadena vacía no equivale a omitir y normalmente produce 422 en logins y nombres.
- Enviar 0 es un valor numérico real; no significa 'sin valor'.
Regla crítica: para crear automáticamente la cuenta PAYMENT, omita payment_account_login o envíe null. Nunca envíe una cadena vacía, 0, el ID interno de la cuenta ni el login operativo del máster.

### 2.3 Información que debe conservar el CRM

| Dato | Uso |
| --- | --- |
| ID o external_reference del cliente | Fuente de verdad local. No se envía como user_id. |
| mt5_login | Consultar, fondear, retirar y relacionar la cuenta. |
| password / investor_password | Entregar y almacenar mediante un mecanismo seguro. |
| leader_id | Actualizar el perfil del leader. |
| payment_account_login | Conciliar fees; los endpoints PAYMENT reciben el login operativo del master. |
| allocation_id | Consultar, actualizar y terminar una relación concreta. |
| idempotency_key | Repetir una operación financiera o eliminación sin duplicarla. |
| event_id | Evitar procesar dos veces el mismo webhook. |

### 2.4 Campos que el cliente debe omitir

- user_id y metatrader_account_product_id en creación, registro, filtros y updates.
- payment_account_login al crear un leader nuevo, salvo una migración expresa de una PAYMENT existente.
- min_free_margin, equity_limit, copy_sl, copy_tp y copy_existing_positions: están reservados o no habilitan una función operativa en el contrato actual.
copy_by_leader_price sí es operativo: cuando está activo, la orden utiliza el precio de referencia del leader. Normalmente se recomienda false para ejecutar al precio disponible del follower.

## 3. URL, autenticación y formato

### 3.1 Base URL

En los ejemplos se utiliza:

```text
https://mam-api.example.com
```

Reemplace este dominio por la URL asignada al ambiente correspondiente.

### 3.2 Autenticación

Todas las rutas protegidas requieren:

```bash
Authorization: Bearer <API_KEY>
Content-Type: application/json
```

Ejemplo:

```bash
curl -X GET "https://mam-api.example.com/api/v1/mam/accounts" \
-H "Authorization: Bearer YOUR_API_KEY"
```

La API key debe permanecer únicamente en servidores backend. No debe incluirse en una aplicación web, aplicación móvil ni repositorio público.

### 3.3 Swagger y ReDoc

```text
Swagger: /docs
ReDoc:   /redoc
OpenAPI: /openapi.json
```

### 3.4 Formato de errores

Los errores HTTP incluyen un identificador para correlacionarlos con los logs:

```json
{
"detail": "MAM account not found",
"request_id": "35de3b73f5bd4d318b3fbaba4c5607f8"
}
```

En errores de validación, detail es una lista con los campos inválidos. La respuesta también incluye el encabezado X-Request-ID.

### 3.5 Códigos HTTP principales

| Código | Significado |
| --- | --- |
| 200 | Consulta o actualización correcta. |
| 201 | Recurso creado correctamente. |
| 401 | API key ausente o inválida. |
| 404 | Cuenta, perfil, allocation u otro recurso inexistente. |
| 409 | Conflicto de negocio, duplicado o fondos insuficientes. |
| 422 | Parámetro inválido o regla de negocio incumplida. |
| 500 | Error interno o de base de datos. |
| 502 | Error al comunicarse o ejecutar una operación en MT5. |

### 3.6 Paginación

La mayoría de listados usan cursor:

```text
?limit=50&cursor=120
```

Respuesta típica:

```json
{
"items": [],
"next_cursor": 171,
"has_more": true
}
```

Se debe continuar usando next_cursor mientras has_more sea true. No se debe calcular el cursor manualmente.

### 3.7 Fechas y dinero

- Las fechas se intercambian en ISO 8601.
- Se recomienda enviar UTC con sufijo Z.
- Los valores monetarios y volúmenes se serializan como números decimales.
- performance_fee_rate usa una tasa entre 0 y 1: 0.20 significa 20 %.
- No use números de punto flotante binario para contabilidad; use Decimal o el tipo decimal equivalente del lenguaje.
## 4. Conceptos del dominio

### 4.1 Cuenta MAM

Es la única entidad de cuenta que crea o registra esta API. Representa una cuenta MT5 y se identifica públicamente por mt5_login, no por su ID interno.

Una cuenta no nace como “master” o “investor”. Sus capacidades se controlan con dos flags independientes:

- can_be_leader=true: la cuenta está autorizada para ser el origen de una allocation.
- can_be_follower=true: la cuenta está autorizada para recibir las operaciones de otra cuenta.
Los dos flags pueden estar activos al mismo tiempo. Por ejemplo, una cuenta puede seguir a una estrategia superior y, si su perfil usa CASCADE, también actuar como leader para otras cuentas. La API evita ciclos en la red.

Para participar en copy trading, las cuentas deben estar ACTIVE y usar account_mode="HEDGING".

### 4.2 Perfil de leader

El perfil de leader no crea otra cuenta. Extiende una cuenta MAM existente que tiene can_be_leader=true con la configuración necesaria para originar allocations:

- Nombre y descripción de la estrategia.
- Visibilidad pública.
- Restricción de conexiones simultáneas.
- Balance mínimo requerido para que un follower pueda suscribirse.
- Performance fee y período de cobro.
- Modo de propagación.
- Estado del perfil.
Una cuenta que solo va a recibir operaciones no necesita este perfil.

### 4.3 Roles dentro de una allocation

Cada allocation relaciona dos cuentas existentes:

- leader_login: cuenta que origina las operaciones para esa relación.
- follower_login: cuenta que recibe las operaciones para esa relación.
Estos nombres describen la dirección de copia, no tipos distintos de cuenta.

### 4.4 Allocation

Es la relación de copy trading (suscripción) entre dos cuentas. Contiene el modo de cálculo, límites, política de desuscripción y performance fee aplicable a esa relación.

### 4.5 Cuenta PAYMENT del leader

Cada leader nuevo tiene una cuenta MT5 independiente destinada exclusivamente a recibir sus performance fees. Esta cuenta se identifica mediante payment_account_login y no participa como leader ni como follower dentro del servicio MAM.

La separación evita mezclar en la cuenta operativa del leader:

- El balance usado para trading y cálculo de métricas.
- Los performance fees cobrados a sus investors.
Al crear el perfil de leader, la API resuelve la cuenta PAYMENT así:

- Si payment_account_login se omite, crea automáticamente una cuenta MT5 en el mismo grupo del leader.
- Si se envía, valida que la cuenta exista en MT5, no esté registrada como cuenta operativa MAM y no esté asignada a otro leader. Puede pertenecer a un grupo MT5 diferente al de la cuenta operativa.
payment_account_login se devuelve en el perfil de leader. Debe conservarse como dato sensible de conciliación, aunque las consultas y retiros se realizan usando el login del master.

> COMPATIBILIDAD Los endpoints de saldo y retiro PAYMENT requieren que el leader tenga una cuenta PAYMENT dedicada y correctamente asociada. Si no existe, responden 409.

## 5. Flujo recomendado de integración

### Paso 1: crear o registrar una cuenta MAM

El sistema siempre comienza creando o registrando una cuenta, sin convertirla en una entidad diferente.

Si todavía no existe en MT5, use POST /api/v1/mam/accounts/create:

```bash
curl -X POST "https://mam-api.example.com/api/v1/mam/accounts/create" \
-H "Authorization: Bearer YOUR_API_KEY" \
-H "Content-Type: application/json" \
-d '{
"first_name": "Ana",
"last_name": "García",
"name": "Cuenta de Ana",
"username": "ana@example.com",
"platform_group": "real\\MAM",
"leverage": 100,
"rights": 9073,
"currency": "USD",
"account_mode": "HEDGING",
"can_be_leader": false,
"can_be_follower": true,
"status": "ACTIVE"
}'
```

Respuesta resumida:

```json
{
"mt5_login": "146502",
"name": "Cuenta de Ana",
"can_be_leader": false,
"can_be_follower": true,
"status": "ACTIVE",
"password": "generated-main-password",
"investor_password": "generated-investor-password",
"platform_group": "real\\MAM",
"leverage": 100,
"rights": 9073
}
```

rights es un entero no negativo que representa la máscara de permisos que se asignará directamente al usuario MT5. No debe confundirse con can_be_leader o can_be_follower: esos dos campos controlan las capacidades de la cuenta dentro de MAM, no los permisos de inicio de sesión y trading en MT5.

| Perfil soportado | Decimal | Hexadecimal | Significado |
| --- | --- | --- | --- |
| Trading habilitado | 9073 | 0x2371 | Permisos base MAM más trailing stop y Expert Advisors. La sesión del usuario puede operar. |
| Trading deshabilitado | 8981 | 0x2315 | Permisos base MAM más el flag MT5 Trade Disabled. La sesión del usuario no puede iniciar operaciones. |

Aunque el tipo técnico admite cualquier entero no negativo reconocido por MT5, la integración define y soporta los dos perfiles anteriores. No se deben inventar ni combinar flags sin validarlos previamente en el servidor MT5 del broker. Si rights se omite, el servicio utiliza 1 (0x1), que es el valor técnico predeterminado y no uno de los dos perfiles administrados. Por eso se recomienda enviar siempre 9073 o 8981 de forma explícita.

Los derechos se aplican únicamente mediante POST /api/v1/mam/accounts/create. Registrar una cuenta existente con POST /api/v1/mam/accounts/add no modifica sus permisos MT5, y el endpoint actual de actualización de cuenta tampoco permite cambiar rights.

Si la cuenta ya existe en MT5, use POST /api/v1/mam/accounts/add:

```json
{
"mt5_login": "146502",
"name": "Cuenta de Ana",
"currency": "USD",
"account_mode": "HEDGING",
"can_be_leader": false,
"can_be_follower": true,
"status": "ACTIVE"
}
```

El endpoint add registra la cuenta; el integrador debe garantizar que el login exista realmente en MT5 y pertenezca al servidor correcto.

Repita este mismo proceso para cada cuenta MT5 que deba participar en el MAM. En el ejemplo del flujo se utilizarán:

- Cuenta 139682: originará operaciones.
- Cuenta 146502: recibirá operaciones.
Las dos siguen siendo cuentas MAM normales. En ambos casos se ve pasar   "account_mode": "HEDGING".

### Paso 2: habilitar una cuenta para ser leader

Si la cuenta 139682 fue creada o registrada sin capacidad de leader, se habilita mediante un update:

```bash
curl -X PATCH "https://mam-api.example.com/api/v1/mam/accounts/139682" \
-H "Authorization: Bearer YOUR_API_KEY" \
-H "Content-Type: application/json" \
-d '{
"can_be_leader": true,
"can_be_follower": true
}'
```

Este paso modifica capacidades; no crea una segunda cuenta. Mantener can_be_follower=true permite que esa misma cuenta también reciba operaciones en otra allocation.

### Paso 3: agregar el perfil de leader a esa cuenta

```bash
curl -X POST "https://mam-api.example.com/api/v1/mam/leaders" \
-H "Authorization: Bearer YOUR_API_KEY" \
-H "Content-Type: application/json" \
-d '{
"account_login": "139682",
"strategy_name": "Momentum Global",
"description": "Estrategia diversificada de seguimiento de tendencia.",
"leaderboard_visibility": false,
"restrict_simultaneous_connections": false,
"min_deposit": 1000,
"performance_fee_rate": 0.20,
"performance_fee_period": "MONTHLY",
"propagation_mode": "ORIGINAL_ONLY",
"status": "ACTIVE"
}'
```

Conserve el id del perfil. Los updates de leader usan leader_id, mientras las allocations y consultas usan account_login.

Al enviar payment_account_login: null o al omitir el campo, la API crea una cuenta PAYMENT en el mismo grupo MT5 del leader y devuelve su login en payment_account_login. Para asociar una cuenta PAYMENT MT5 preexistente, envíe su login en ese campo durante esta creación. El endpoint PATCH del perfil no cambia posteriormente la cuenta PAYMENT.

min_deposit define el balance MT5 mínimo que debe tener una cuenta follower para crear una allocation con este leader. Se expresa en unidades de la moneda de la cuenta y debe ser mayor o igual a 0. Su valor predeterminado es 0, lo que desactiva esta restricción.

La cuenta 139682 continúa siendo la misma cuenta MAM. El perfil únicamente le agrega configuración para originar operaciones.

### Paso 4: verificar la capacidad de follower de la cuenta receptora

La cuenta 146502 debe tener can_be_follower=true. Este es el valor por defecto, pero puede garantizarse explícitamente:

```bash
curl -X PATCH "https://mam-api.example.com/api/v1/mam/accounts/146502" \
-H "Authorization: Bearer YOUR_API_KEY" \
-H "Content-Type: application/json" \
-d '{"can_be_follower":true}'
```

No se crea un perfil adicional para que una cuenta actúe como follower.

### Paso 5: fondear la cuenta que recibirá las operaciones

```bash
curl -X POST "https://mam-api.example.com/api/v1/mam/accounts/146502/deposit" \
-H "Authorization: Bearer YOUR_API_KEY" \
-H "Content-Type: application/json" \
-d '{
"amount": 5000,
"idempotency_key": "crm-deposit-84922"
}'
```

idempotency_key debe ser único por operación financiera. Si el CRM reintenta la misma solicitud debe conservar la misma key.

Después de fondear la cuenta puede validar su elegibilidad antes de crear la allocation:

```bash
curl -X POST \
"https://mam-api.example.com/api/v1/mam/allocations/subscription-eligibility" \
-H "Authorization: Bearer YOUR_API_KEY" \
-H "Content-Type: application/json" \
-d '{
"leader_login": "139682",
"follower_login": "146502"
}'
{
"eligible": true,
"leader_login": "139682",
"follower_login": "146502",
"follower_balance": 5000,
"min_deposit": 1000
}
```

La validación utiliza exclusivamente el balance actual del follower en MT5. equity, credit y free_margin no cuentan para alcanzar el mínimo. Si min_deposit vale 0, la restricción está desactivada y follower_balance puede devolverse como null porque no es necesario consultar MT5.

### Paso 6: crear la allocation entre las dos cuentas

```bash
curl -X POST "https://mam-api.example.com/api/v1/mam/allocations" \
-H "Authorization: Bearer YOUR_API_KEY" \
-H "Content-Type: application/json" \
-d '{
"leader_login": "139682",
"follower_login": "146502",
"status": "ACTIVE",
"allocation_mode": "EQUITY",
"mode_parameter": 1,
"equity_limit": null,
"equity_stop": 1000,
"copy_sl": false,
"copy_tp": false,
"copy_by_leader_price": false,
"copy_existing_positions": false,
"unsubscribe_policy": "CLOSE_ON_UNSUBSCRIBE",
"performance_fee_rate": null,
"performance_fee_enabled": true,
"max_active_leaders_per_follower": 1
}'
```

Si performance_fee_rate es null, se hereda la tasa del perfil del leader. En este ejemplo, el producto o plan de la cuenta receptora permite seguir a un solo leader, por eso max_active_leaders_per_follower vale 1.

Este valor se envía únicamente al crear la allocation. Por ejemplo, un plan que permita hasta diez conexiones debe enviar:

```json
{"max_active_leaders_per_follower":10}
```

La API cuenta las allocations vivas que ya tiene la cuenta receptora y acepta o rechaza la nueva relación usando ese valor.

En esta allocation, y solo dentro de esta relación, 139682 es el leader y 146502 es el follower.

### Paso 7: activar la allocation

```bash
curl -X PATCH "https://mam-api.example.com/api/v1/mam/allocations/321" \
-H "Authorization: Bearer YOUR_API_KEY" \
-H "Content-Type: application/json" \
-d '{"status":"ACTIVE"}'
```

A partir de ese momento, las operaciones nuevas de la cuenta leader pueden replicarse en la cuenta follower según la configuración de la allocation.

### Paso 8: consultar la integración

```text
GET /api/v1/mam/accounts/146502/metrics
GET /api/v1/mam/allocations?follower_login=146502
GET /api/v1/mam/analytics/followers/146502/trade-history
GET /api/v1/mam/analytics/leaders/139682/subscribers
GET /api/v1/mam/leaders/139682/payment-account/balance
GET /api/v1/perf-fee/transactions?master_login=139682
GET /api/v1/perf-fee/master/139682/investor-payments
```

### Paso 9: actualizar una allocation

Solo envíe los campos que desea cambiar:

```bash
curl -X PATCH "https://mam-api.example.com/api/v1/mam/allocations/321" \
-H "Authorization: Bearer YOUR_API_KEY" \
-H "Content-Type: application/json" \
-d '{
"allocation_mode": "SCALED",
"mode_parameter": 0.50,
"equity_stop": 800
}'
```

### Paso 10: terminar la relación entre las cuentas

```bash
curl -X POST "https://mam-api.example.com/api/v1/mam/allocations/321/unsubscribe" \
-H "Authorization: Bearer YOUR_API_KEY"
```

No se debe cancelar una allocation activa con un update directo sin evaluar las posiciones abiertas. Use el endpoint unsubscribe, que aplica la política configurada y gestiona el performance fee pendiente.

## 6. Modos de allocation

El campo mode_parameter cambia de significado según allocation_mode. Su tipo es decimal. Cuando se envía debe ser estrictamente mayor que 0; 0 y los valores negativos producen una respuesta 422.

| Modo | Significado de mode_parameter | ¿Es obligatorio? | Valor efectivo si se omite |
| --- | --- | --- | --- |
| FIXED | Cantidad fija de lotes que abrirá el follower. | Sí | No aplica; la API rechaza la solicitud. |
| SCALED | Multiplicador directo de los lotes abiertos por el leader. | Sí | No aplica; la API rechaza la solicitud. |
| EQUITY | Multiplicador adicional de la proporción entre equities. | No | 1 |
| EQUITY_ROUND_DOWN | Multiplicador adicional de la proporción entre equities. | No | 1 |
| BALANCE | Multiplicador adicional de la proporción entre balances. | No | 1 |

Por tanto, para EQUITY, EQUITY_ROUND_DOWN y BALANCE, enviar "mode_parameter": 1 significa copiar la proporción calculada sin aumentarla ni reducirla. Enviar 0.5 reduce el resultado a la mitad y enviar 2 lo duplica. También puede omitirse o enviarse como null; en ambos casos el sistema usa 1.

Cada modo define cómo se obtiene el volumen solicitado. El volumen ejecutable respeta el mínimo, máximo y step configurados para el símbolo de la cuenta follower, por lo que puede existir una diferencia de redondeo frente al valor solicitado.

### EQUITY y EQUITY_ROUND_DOWN

Calculan el volumen según la relación de equity entre ambas cuentas y aplican mode_parameter como multiplicador opcional. EQUITY_ROUND_DOWN utiliza un ajuste conservador para no exceder el volumen solicitado.

### BALANCE

Calcula el volumen según la relación de balance entre ambas cuentas y aplica mode_parameter como multiplicador opcional.

### FIXED

mode_parameter representa la cantidad fija de lotes solicitada para el follower y es obligatorio.

### SCALED

mode_parameter es un multiplicador obligatorio aplicado al volumen del leader. Por ejemplo, 1 conserva el volumen, 0.5 solicita la mitad y 2 solicita el doble.

### Validaciones y errores frecuentes

- FIXED y SCALED requieren mode_parameter; omitirlo o enviar null produce 422.
- En cualquiera de los cinco modos, enviar mode_parameter <= 0 produce 422.
- En EQUITY, EQUITY_ROUND_DOWN y BALANCE, omitirlo o enviar null es válido y equivale a 1.
- El multiplicador no evita las restricciones de margen, equity stop, volumen mínimo, volumen máximo ni step del símbolo.
- Un multiplicador mayor que 1 incrementa exposición y riesgo; no representa un porcentaje.
## 7. Parámetros de una allocation

| Campo | Uso |
| --- | --- |
| leader_login | Login MT5 de la cuenta que origina operaciones en esta relación. |
| follower_login | Login MT5 de la cuenta que recibe operaciones. Debe ser distinto al leader. |
| status | ACTIVE, PAUSED, STOPPING, CANCELLED o ERROR. Por defecto PAUSED. |
| allocation_mode | SCALED, FIXED, EQUITY, EQUITY_ROUND_DOWN o BALANCE. |
| mode_parameter | En FIXED, lotes fijos obligatorios. En SCALED, multiplicador obligatorio del lote del leader. En EQUITY, EQUITY_ROUND_DOWN y BALANCE, multiplicador opcional de la proporción; omitido o null equivale a 1. Cuando se envía debe ser mayor que 0. |
| equity_limit | Campo reservado en el contrato actual. |
| equity_stop | Equity mínimo del follower en unidades. Si cae hasta ese valor, la allocation se detiene y sus operaciones copiadas se cierran. null lo desactiva. |
| copy_sl | Campo reservado en el contrato actual. |
| copy_tp | Campo reservado en el contrato actual. |
| copy_by_leader_price | Campo reservado en el contrato actual. |
| copy_existing_positions | Campo reservado en el contrato actual. |
| unsubscribe_policy | KEEP_OPEN o CLOSE_ON_UNSUBSCRIBE. |
| performance_fee_rate | Tasa de 0 a 1; si se omite, hereda la del leader al crear la allocation. |
| performance_fee_enabled | Activa o desactiva el fee para esta allocation. |
| max_active_leaders_per_follower | Entero mayor o igual que 0 enviado por el integrador. Limita cuántas allocations vivas puede tener la cuenta receptora durante esa solicitud. No tiene máximo superior definido en el contrato y no queda almacenado. |

### 7.1 Origen del límite de leaders

El límite no pertenece a la cuenta MAM ni se calcula desde un producto dentro de mam-api.

- En una integración directa con mam-api, el CRM o backend integrador debe resolver el límite desde su propio producto o plan y enviarlo explícitamente en cada creación de allocation.
El campo se usa como validación para esa creación y no queda almacenado como configuración de la cuenta ni de la allocation. Es un parámetro individual de cada solicitud POST /api/v1/mam/allocations.

La API acepta cualquier entero mayor o igual que 0:

- 0: bloquea la creación de cualquier allocation viva para esa cuenta.
- 1: permite como máximo una allocation viva.
- 10: permite como máximo diez allocations vivas.
- Un valor superior también es válido, porque el contrato no define un máximo.
Para validar, la API cuenta allocations en estado ACTIVE, PAUSED o STOPPING. Si el conteo actual es mayor o igual que el límite recibido, responde 409 y no crea la nueva allocation.

El valor puede cambiar entre solicitudes. Por ejemplo, enviar 10 hoy no guarda un plan de diez conexiones en MAM. Si una solicitud posterior envía 2, esa nueva solicitud se validará contra 2. Por eso el CRM o backend debe resolver y enviar siempre el valor autorizado desde su propia fuente de verdad.

El integrador debe enviar siempre max_active_leaders_per_follower con el límite autorizado por su producto o plan.

## 8. Reglas de negocio importantes

- Leader y follower deben existir, estar ACTIVE y usar HEDGING.
- El leader necesita can_be_leader=true y un perfil de leader.
- El follower necesita can_be_follower=true.
- No se permite que una cuenta se siga a sí misma.
- La API rechaza ciclos directos o indirectos de copy trading.
- Solo puede existir una allocation viva para la misma pareja leader/follower.
- ACTIVE, PAUSED y STOPPING cuentan como allocations vivas.
- El límite de leaders vivos depende del producto o plan y debe enviarse como max_active_leaders_per_follower al crear la allocation.
- Si el perfil tiene restrict_simultaneous_connections=true, ese leader no admite followers que ya estén conectados a otro leader vivo.
- performance_fee_rate del perfil y de la allocation se expresa entre 0 y 1.
- Las allocations nuevas heredan el fee del perfil cuando no se envía una tasa.
## 9. Estados y desuscripción

### Estados de allocation

| Estado | Significado |
| --- | --- |
| PAUSED | La relación permanece registrada, pero no replica operaciones mientras esté pausada. No pause una allocation con posiciones abiertas sin definir previamente cómo se administrarán. |
| ACTIVE | Copy trading habilitado. |
| STOPPING | Se está cerrando o finalizando de forma controlada. |
| CANCELLED | Finalizada. |
| ERROR | Requiere intervención operativa. |

### KEEP_OPEN

- Cobra el performance fee pendiente.
- Desconecta las posiciones copiadas abiertas.
- Las posiciones permanecen abiertas en MT5, pero dejan de ser administradas por esa allocation.
- La allocation pasa a CANCELLED.
### CLOSE_ON_UNSUBSCRIBE

- Detiene la relación y evita nuevas operaciones copiadas.
- Genera cierres para las posiciones enlazadas.
- Cuando los cierres terminan, cobra el fee pendiente y pasa a CANCELLED.
## 10. Performance fee

El performance fee se controla por allocation mediante High-Water Mark. La API expone el importe cobrado y su trazabilidad por investor para conciliación.

Períodos disponibles para el perfil del leader:

```text
OFF, MINUTELY, HOURLY, DAILY, WEEKLY, SEMIMONTHLY, MONTHLY
```

SEMIMONTHLY corresponde a dos períodos de calendario por mes:

- Desde el día 1 a las 00:00 hasta el día 16 a las 00:00.
- Desde el día 16 a las 00:00 hasta el día 1 del mes siguiente a las 00:00.
Los cortes se calculan con la zona horaria configurada para el ambiente. El período WEEKLY corta cada domingo a las 00:00; MONTHLY, el primer día de cada mes a las 00:00. MINUTELY, HOURLY y DAILY siguen el límite natural de cada unidad de calendario. OFF desactiva la cristalización periódica.

### 10.1 Flujo de cobro y cuenta PAYMENT

El cobro mantiene trazabilidad por investor aunque el master reciba un único crédito consolidado en su cuenta PAYMENT. Use los endpoints de esta guía para consultar el total acreditado y su composición individual.

No deposite fondos manualmente en la cuenta operativa del leader para simular performance fees. Tampoco retire directamente desde MT5 la cuenta PAYMENT: use el endpoint específico para conservar la trazabilidad y evitar movimientos duplicados.

El retiro de una cuenta que tiene una allocation, cobra primero los performance fees vencidos. La API rechaza el retiro si el free margin restante no cubre el fee más el monto solicitado.

Este retiro corresponde al follower y usa POST /api/v1/mam/accounts/{account_login}/withdraw. El retiro de ganancias ya acreditadas en la cuenta PAYMENT del master utiliza un endpoint diferente: POST /api/v1/mam/leaders/{master_login}/payment-account/withdraw.

## 11. Referencia de endpoints de integración

Todos los endpoints descritos a continuación requieren Bearer API key.

### 11.1 Cuentas

**GET** `/api/v1/mam/accounts`

Lista cuentas MAM. Filtros: user_id, status, can_be_leader, can_be_follower, cursor, limit de 1 a 100 y order=ASC|DESC.

**POST** `/api/v1/mam/accounts/add`

Registra una cuenta que ya existe en MT5. No crea el usuario en MT5. Acepta mt5_login, nombre, moneda, modo, capacidades y estado.

**POST** `/api/v1/mam/accounts/create`

Crea una cuenta en MT5 y la registra en MAM. Requiere datos personales, username, leverage y un platform_group, por favor no enviar metatrader_account_product_id. Devuelve las credenciales generadas.

rights debe ser un entero no negativo. Los perfiles soportados por esta integración son 9073 (0x2371) para permitir trading, trailing stop y Expert Advisors, y 8981 (0x2315) para aplicar el flag MT5 Trade Disabled. Si se omite, el servicio usa 1 (0x1). La respuesta devuelve rights con el valor efectivamente aplicado. Cada broker debe validar las máscaras en su propio servidor antes de producción.

**GET** `/api/v1/mam/accounts/{account_login}`

Obtiene una cuenta por login e incluye las credenciales almacenadas cuando existen. Trate esta respuesta como altamente sensible.

**PATCH** `/api/v1/mam/accounts/{account_login}`

Actualiza parcialmente nombre, moneda, producto, capacidades, modo o estado. Los campos requeridos de configuración no aceptan null.

**GET** `/api/v1/mam/accounts/{account_login}/metrics`

Consulta en vivo balance, equity, margin y free_margin en MT5. Un 502 indica que MT5 no pudo responder o encontrar la cuenta.

**POST** `/api/v1/mam/accounts/{account_login}/deposit`

Acredita saldo en MT5. Body:

```json
{"amount":1000,"idempotency_key":"deposit-unique-id"}
```

La cuenta debe estar activa. Use siempre una idempotency_key estable.

**POST** `/api/v1/mam/accounts/{account_login}/withdraw`

Retira saldo después de calcular y cobrar el performance fee pendiente. El monto debe caber en el free margin disponible después del fee.

```json
{"amount":250,"idempotency_key":"withdrawal-unique-id"}
```

**GET** `/api/v1/mam/accounts/{account_login}/balance-transactions`

Lista el historial contable de depósitos, retiros, performance fees y otros movimientos. Filtros: transaction_type, status, cursor y limit de 1 a 200.

Para consultar un crédito de performance fee use el login operativo del master y los filtros transaction_type=PF_CREDIT y status=EXECUTED. Para conocer su composición por investor, consulte GET /api/v1/perf-fee/master/{master_login}/investor-payments por período o run_id.

#### Eliminación segura de cuentas MAM

La eliminación no usa DELETE directo. Es asíncrona e idempotente porque puede involucrar allocations vivas y posiciones copiadas abiertas. Estos endpoints eliminan la cuenta y sus relaciones en MAM; no eliminan el usuario del servidor MT5.

- Primero consulte el impacto y revise conflictos, allocations y posiciones afectadas.
- Cree la operación con una política explícita y una idempotency_key estable.
- Consulte operation_id hasta COMPLETED. Si queda PARTIAL, corrija la causa y ejecute retry.
#### Eliminación de master

**GET** `/api/v1/mam/account-deletions/impact?master_login={login}`

Analiza sin modificar datos: allocations directas y entrantes, posiciones transmitidas, cascadas e investors que podrían incluirse. No cree la operación hasta revisar la respuesta.

**POST** `/api/v1/mam/account-deletions`

```json
{
"master_login": "139682",
"scope": "MASTER_ACCOUNT_ONLY",
"investor_logins": [],
"transmitted_positions_policy": "CLOSE_TRANSMITTED",
"idempotency_key": "delete-master-139682-20260813",
"requested_by": "client-crm"
}
```

scope acepta MASTER_ACCOUNT_ONLY o MASTER_AND_INVESTORS. En el segundo caso, investor_logins debe contener únicamente followers directos elegibles. CLOSE_TRANSMITTED es la política normal: cierra posiciones copiadas antes de purgar. KEEP_OPEN las deja en MT5 bajo gestión manual y fuera del seguimiento MAM.

**GET** `/api/v1/mam/account-deletions/{operation_id}`

**POST** `/api/v1/mam/account-deletions/{operation_id}/retry`

#### Eliminación de investor

**GET** `/api/v1/mam/investor-account-deletions/impact?investor_login={login}`

Devuelve eligible, conflicts, allocations de entrada y salida y posiciones copiadas abiertas. Si la cuenta también funciona como master, responde con IS_ACTIVE_MASTER y se debe usar el flujo de master.

**POST** `/api/v1/mam/investor-account-deletions`

```json
{
"investor_login": "140688",
"transmitted_positions_policy": "CLOSE_TRANSMITTED",
"idempotency_key": "delete-investor-140688-20260813",
"requested_by": "client-crm"
}
```

**GET** `/api/v1/mam/investor-account-deletions/{operation_id}`

**POST** `/api/v1/mam/investor-account-deletions/{operation_id}/retry`

| Estado | Significado |
| --- | --- |
| PENDING | La operación fue creada. |
| WAITING_CLOSE | El cierre de posiciones está en proceso. |
| PARTIAL | Uno o más cierres no finalizaron; corrija la causa y use el endpoint de retry. |
| PURGING | La eliminación se está finalizando. |
| COMPLETED | La eliminación terminó correctamente. |
| FAILED | La operación no pudo completarse; revise error_message. |

No archive ni elimine la cuenta en sistemas externos hasta recibir COMPLETED. La eliminación o desactivación posterior del usuario MT5 pertenece al procedimiento administrativo del broker.

### 11.2 Perfiles de leader

**GET** `/api/v1/mam/leaders`

Lista perfiles de leaders. Filtros: account_login, status, visible_only, cursor, limit de 1 a 100 y order.

**POST** `/api/v1/mam/leaders`

Crea el perfil de leader para una cuenta existente. La cuenta debe permitir esa capacidad y usar HEDGING.

Campos principales:

- account_login
- payment_account_login (opcional al crear; si se omite, se crea automáticamente en MT5)
- strategy_name
- description
- leaderboard_visibility
- restrict_simultaneous_connections
- min_deposit
- performance_fee_rate
- performance_fee_period
- propagation_mode
- status
**GET** `/api/v1/mam/leaders/{leader_id}`

Obtiene el perfil por su ID interno de leader.

**PATCH** `/api/v1/mam/leaders/{leader_id}`

Actualiza parcialmente la estrategia, visibilidad, restricciones, fee, período, propagación o estado.

performance_fee_period acepta OFF, MINUTELY, HOURLY, DAILY, WEEKLY, SEMIMONTHLY y MONTHLY. La cuenta PAYMENT no se reasigna mediante este endpoint.

min_deposit acepta valores mayores o iguales a 0 y puede actualizarse con PATCH /api/v1/mam/leaders/{leader_id}. El cambio aplica a nuevas allocations; no cancela automáticamente allocations existentes. Al crear una allocation, un balance inferior al mínimo devuelve 409; si MT5 no puede suministrar el balance para hacer la validación, devuelve 502.

propagation_mode puede ser:

- ORIGINAL_ONLY: solo propaga operaciones abiertas originalmente por esa cuenta.
- CASCADE: también propaga operaciones recibidas de un leader superior.
La API bloquea allocations que produzcan ciclos.

### 11.3 Cuenta PAYMENT y reportes de performance fee

**GET** `/api/v1/mam/leaders/{master_login}/payment-account/balance`

Consulta en vivo la cuenta PAYMENT asociada al leader. El parámetro master_login siempre es el login de la cuenta operativa del master; no envíe directamente el login PAYMENT en la URL.

```bash
curl -X GET \
"https://mam-api.example.com/api/v1/mam/leaders/139682/payment-account/balance" \
-H "Authorization: Bearer YOUR_API_KEY"
```

Respuesta:

```json
{
"master_login": "139682",
"payment_account_login": "139683",
"balance": 320.47,
"equity": 320.47,
"free_margin": 320.47,
"credit": 0,
"withdrawable": 320.47,
"currency": "USD"
}
```

withdrawable excluye el crédito MT5 y es el límite que debe usarse para habilitar retiros. Un 409 indica que el master no tiene una cuenta PAYMENT dedicada válida; un 502, que no fue posible consultar MT5.

**POST** `/api/v1/mam/leaders/{master_login}/payment-account/withdraw`

Retira fondos directamente de la cuenta PAYMENT del master. No afecta el balance operativo del leader ni ejecuta un nuevo cálculo de performance fee.

```bash
curl -X POST \
"https://mam-api.example.com/api/v1/mam/leaders/139682/payment-account/withdraw" \
-H "Authorization: Bearer YOUR_API_KEY" \
-H "Content-Type: application/json" \
-d '{
"amount": 100,
"idempotency_key": "crm-payment-withdrawal-84721"
}'
```

La idempotency_key es obligatoria, debe tener entre 8 y 120 caracteres y debe representar una única operación del CRM. Repetir exactamente la misma solicitud devuelve result="ALREADY_PROCESSED" sin volver a debitar MT5. Usar la misma key con otro monto devuelve 409.

Respuesta:

```json
{ "result": "COMPLETED", "master_login": "139682", "payment_account_login": "139683", "requested_amount": 100, "currency": "USD", "balance_before": 320.47, "balance_after": 220.47, "withdrawable_before": 320.47 }
```

**GET** `/api/v1/perf-fee/transactions`

Lista los créditos de performance fee ejecutados para un master. Permite construir el historial de ingresos de su cuenta PAYMENT.

Parámetros:

```bash
curl -G "https://mam-api.example.com/api/v1/perf-fee/transactions" \
-H "Authorization: Bearer YOUR_API_KEY" \
--data-urlencode "master_login=139682" \
--data-urlencode "from_at=2026-08-01T00:00:00Z" \
--data-urlencode "to_at=2026-09-01T00:00:00Z" \
--data-urlencode "limit=50"
```

| Parámetro | Uso |
| --- | --- |
| master_login | Obligatorio. Login MT5 operativo del leader. |
| limit | De 1 a 100; predeterminado 5. |
| cursor | Devuelve IDs menores al cursor para continuar la paginación. |
| from_at | Fecha efectiva inicial inclusiva en ISO 8601. |
| to_at | Fecha efectiva final exclusiva en ISO 8601. |

Respuesta resumida:

```json
{ "payment_account_login": "139683", "items": [ { "id": 984, "kind": "PF_CREDIT", "amount": 206.55, "currency": "USD", "executed_at": "2026-08-16T00:00:10Z" } ], "next_cursor": null, "has_more": false }
```

En créditos agrupados, investor_mt5_login será null porque una sola fila puede representar pagos de varios investors. Para obtener la segregación use el endpoint siguiente.

**GET** `/api/v1/perf-fee/master/{master_login}/investor-payments`

Devuelve los pagos individuales que explican cuánto aportó cada investor al performance fee de un master. Este es el endpoint de conciliación para repartir comisiones por investor, sponsor o red de IBs.

Parámetros:

| Parámetro | Uso |
| --- | --- |
| run_id | Opcional. Filtra una acreditación específica. |
| limit | De 1 a 500; predeterminado 100. |
| cursor | Continúa la paginación desde el cursor devuelto. |
| from_at | Inicio inclusivo del período consultado. |
| to_at | Fin exclusivo del período consultado. |

Consulta de un período completo:

```bash
curl -G \
"https://mam-api.example.com/api/v1/perf-fee/master/139682/investor-payments" \
-H "Authorization: Bearer YOUR_API_KEY" \
--data-urlencode "from_at=2026-08-01T00:00:00Z" \
--data-urlencode "to_at=2026-09-01T00:00:00Z" \
--data-urlencode "limit=100"
```

Consulta de una acreditación concreta:

```bash
curl -G \
"https://mam-api.example.com/api/v1/perf-fee/master/139682/investor-payments" \
-H "Authorization: Bearer YOUR_API_KEY" \
--data-urlencode "run_id=73" \
--data-urlencode "limit=100"
```

Respuesta:

```json
{ "master_login": "139682", "payment_account_login": "139683", "items": [ { "run_id": 73, "investor_mt5_login": "146537", "amount": 95.47, "currency": "USD", "status": "EXECUTED", "executed_at": "2026-08-16T00:00:08Z" }, { "run_id": 73, "investor_mt5_login": "146540", "amount": 111.08, "currency": "USD", "status": "EXECUTED", "executed_at": "2026-08-16T00:00:09Z" } ], "next_cursor": null, "has_more": false }
```

#### Cómo obtener el run_id de un crédito

Cada item incluye run_id cuando pertenece a una acreditación agrupada. Puede usar ese valor para consultar únicamente los pagos individuales de la misma acreditación.

```bash
curl -G \
"https://mam-api.example.com/api/v1/mam/accounts/139682/balance-transactions" \
-H "Authorization: Bearer YOUR_API_KEY" \
--data-urlencode "transaction_type=PF_CREDIT" \
--data-urlencode "status=EXECUTED" \
--data-urlencode "limit=50"
```

Para consultar la composición del run_id 73 use:

```text
GET /api/v1/perf-fee/master/139682/investor-payments?run_id=73
```

Si no necesita una acreditación específica, omita run_id y consulte directamente los pagos individuales por from_at y to_at.

La suma de amount de los items EXECUTED del mismo run_id debe coincidir con el crédito consolidado correspondiente.

Cuando no se envía run_id, el endpoint permite consultar y paginar todos los pagos individuales del master. from_at es inclusivo y to_at exclusivo. Si se envían ambas fechas, from_at debe ser anterior a to_at.

### 11.4 Allocations

**GET** `/api/v1/mam/allocations`

Lista allocations por leader_login, follower_login, status, cursor y limit de 1 a 100.

**POST** `/api/v1/mam/allocations/subscription-eligibility`

Valida, sin crear una allocation, si el follower alcanza el min_deposit configurado en el perfil del leader:

```json
{
"leader_login": "139682",
"follower_login": "146502"
}
```

Devuelve eligible, follower_balance y min_deposit. También valida que las cuentas existan, estén activas, usen HEDGING y tengan las capacidades de leader y follower correspondientes. Una respuesta eligible=false permite al CRM solicitar fondos antes de intentar la suscripción.

**POST** `/api/v1/mam/allocations`

Crea una relación entre dos cuentas. Valida capacidades, estado, HEDGING, perfil del leader, duplicados, ciclos, límite de leaders del follower y min_deposit. Esta operación vuelve a consultar el balance MT5 aunque se haya llamado antes al endpoint de elegibilidad, evitando crear la allocation con un dato desactualizado. Si el balance resulta insuficiente, devuelve 409.

La combinación de allocation_mode y mode_parameter determina el comportamiento de asignación. Consulte la sección 6 antes de integrar este endpoint.

Si la llamada es directa, envíe siempre max_active_leaders_per_follower con el valor resuelto desde el producto o plan del sistema integrador. Puede ser 10 o cualquier entero mayor o igual que 0; se utiliza solo para validar esta creación y no queda guardado en MAM.

**GET** `/api/v1/mam/allocations/{allocation_id}`

Obtiene la configuración y estado actual de una allocation.

**PATCH** `/api/v1/mam/allocations/{allocation_id}`

Actualiza campos enviados explícitamente. Al cambiar a un estado vivo vuelve a validar cuentas, duplicados y restricciones simultáneas.

**POST** `/api/v1/mam/allocations/{allocation_id}/unsubscribe`

Finaliza la suscripción aplicando KEEP_OPEN o CLOSE_ON_UNSUBSCRIBE y cobra el performance fee correspondiente. Este es el endpoint correcto para una desuscripción iniciada por usuario o CRM.

### 11.5 Analytics

**GET** `/api/v1/mam/analytics/leaders/performance-summary`

Devuelve resumen de performance de leaders. account_login acepta uno o más logins separados por coma. limit admite de 1 a 500.

**POST** `/api/v1/mam/analytics/leaders/performance-summary/query`

Versión POST para consultar una lista de logins sin depender de una query URL larga:

```json
{"account_logins":["139682","139683"]}
```

**GET** `/api/v1/mam/analytics/followers/performance-summary`

Devuelve resumen de performance de followers. Acepta los mismos parámetros de la consulta de leaders.

**POST** `/api/v1/mam/analytics/followers/performance-summary/query`

Versión POST para consultar varios followers:

```json
{"account_logins":["146502","146503"]}
```

**GET** `/api/v1/mam/analytics/leaders/{account_login}/trade-history`

Historial paginado de trades de una cuenta actuando como leader. Parámetros: limit de 1 a 200 y cursor.

**GET** `/api/v1/mam/analytics/followers/{account_login}/trade-history`

Historial paginado de operaciones copiadas y resultados de una cuenta actuando como follower.

**GET** `/api/v1/mam/analytics/leaders/{account_login}/subscribers`

Lista followers conectados al leader. Usa limit, offset y filtro opcional status. Devuelve allocation mode, fee, equity stop y fechas.

**GET** `/api/v1/mam/analytics/leaders/{account_login}/strategy`

Devuelve estrategia, visibilidad, fee, propagación y estado del perfil de leader asociado a la cuenta.

### 11.6 Webhook de terminación de allocations

La API puede notificar al CRM cuando una allocation termina. El evento público es mam.allocation.terminated. Se emite por USER_UNSUBSCRIBE o EQUITY_STOP e informa una terminación ya procesada; recibirlo no inicia ni autoriza la desuscripción.

#### Registro del webhook

**POST** `/api/v1/mam/webhooks`

Registra el único destino permitido. Debe ejecutarse antes de las terminaciones que se quieren recibir; no genera eventos retroactivos. Un segundo registro devuelve 409.

```json
{
"name": "client-crm",
"url": "https://crm.example.com/webhooks/mam"
}
```

Respuesta 201:

```json
{
"id": 1,
"name": "client-crm",
"url": "https://crm.example.com/webhooks/mam",
"status": "ACTIVE",
"signing_secret": "KEtHnuOWtgAN4dTHLGRP5rILSqTD09aSRdfAthOBdto",
"signature_algorithm": "HMAC-SHA256",
"created_at": "2026-07-28T00:17:04.949891Z"
}
```

Guarde signing_secret inmediatamente en el secret manager del CRM: se entrega en texto plano una sola vez y es el valor que valida X-MAM-Signature.

#### Headers y firma

| Header | Contenido |
| --- | --- |
| X-MAM-Event-Id | UUID estable e idempotente del evento. |
| X-MAM-Event-Type | mam.allocation.terminated |
| X-MAM-Timestamp | Unix timestamp en segundos usado para firmar. |
| X-MAM-Signature | sha256=<digest hexadecimal HMAC-SHA256> |

```text
signed_payload = X-MAM-Timestamp + "." + raw_request_body
signature = "sha256=" + HMAC_SHA256(signing_secret, signed_payload).hexdigest()
```

Verifique la firma sobre los bytes exactos del body antes de deserializar JSON, compare en tiempo constante y rechace timestamps con más de cinco minutos. No vuelva a serializar el payload para firmarlo.

#### Payload

```json
{
"event_id": "8b9f1de6-2d11-5465-acc6-6ec7f6a32a28",
"type": "mam.allocation.terminated",
"version": 1,
"occurred_at": "2026-07-28T00:19:05.020369Z",
"data": {
"allocation_id": 64,
"reason": "USER_UNSUBSCRIBE",
"triggered_by": "USER",
"status": "CANCELLED",
"leader_login": "146493",
"follower_login": "146537",
"unsubscribe_policy": "CLOSE_ON_UNSUBSCRIBE",
"allocation_mode": "EQUITY",
"stop_loss_value": null,
"trigger_equity": null,
"closed_positions": 0,
"failed_positions": 0,
"performance_fee_due": "0.00",
"performance_fee_charged": "0E-8",
"terminated_at": "2026-07-28T00:19:05.020369Z"
}
}
```

Para EQUITY_STOP, reason será EQUITY_STOP, triggered_by será SYSTEM y los campos stop_loss_value y trigger_equity informarán el umbral y la equity disparadora cuando estén disponibles.

#### Respuesta, idempotencia y reintentos

- Persista el evento y responda rápidamente con 2xx; 204 No Content es suficiente.
- Aplique un índice UNIQUE sobre event_id. Un evento repetido debe responder 2xx sin repetir efectos.
- La entrega puede repetirse. Procese event_id de forma idempotente y responda 2xx después de persistir el evento.
- Cada nueva entrega conserva el mismo event_id y payload.
## 12. Idempotencia y reintentos

### Operaciones financieras

Genere una key determinista desde el ID de la transacción del CRM:

```text
deposit:<crm_transaction_id>
withdrawal:<crm_transaction_id>
payment-withdrawal:<crm_transaction_id>
```

No genere una key nueva cuando solo está reintentando la misma operación.

### Creación de recursos

- Guarde siempre el mt5_login, leader_id y allocation_id devueltos.
- Antes de reintentar una creación después de un timeout, consulte por login o por la pareja leader/follower para evitar duplicados.
- Un 409 no se debe reintentar ciegamente.
- Los 500 y 502 pueden reintentarse con backoff, conservando la misma idempotency key cuando hay dinero involucrado.
## 13. Ejemplo de orquestación en pseudocódigo

```text
function createMamAccount(customer, product):
account = POST /api/v1/mam/accounts/create {
customer data,
platform_group: product.mt5_group,
can_be_leader: false,
can_be_follower: true,
account_mode: HEDGING
}
save customer.mam_account_login = account.mt5_login
deliver credentials securely
return account

function enableAccountAsLeader(account, strategy):
PATCH /api/v1/mam/accounts/{account.mt5_login} {
can_be_leader: true
}
profile = POST /api/v1/mam/leaders {
account_login: account.mt5_login,
payment_account_login: null,
strategy data,
performance fee configuration
}
save profile.id for account
save profile.payment_account_login for reconciliation
return profile

function connectAccounts(leaderAccount, followerAccount, settings):
verify GET /api/v1/mam/accounts/{followerAccount.mt5_login}/metrics
allocation = POST /api/v1/mam/allocations {
leader_login: leaderAccount.mt5_login,
follower_login: followerAccount.mt5_login,
status: PAUSED,
settings
}
verify allocation response
PATCH /api/v1/mam/allocations/{id} { status: ACTIVE }
return allocation

function unsubscribe(allocationId):
result = POST /api/v1/mam/allocations/{allocationId}/unsubscribe
if result.status == STOPPING:
monitor until allocation.status == CANCELLED
return result
```

## 14. Resumen del flujo mínimo

```text
1. POST /api/v1/mam/accounts/create o /api/v1/mam/accounts/add para registrar cada cuenta.
2. PATCH /api/v1/mam/accounts/{login} para habilitar las capacidades necesarias.
3. POST /api/v1/mam/leaders omitiendo payment_account_login para crear el perfil y su PAYMENT segregada.
4. POST /api/v1/mam/accounts/{follower_login}/deposit para fondear la cuenta receptora.
5. POST /api/v1/mam/allocations/subscription-eligibility para validar min_deposit.
6. POST /api/v1/mam/allocations para relacionar las cuentas.
7. PATCH /api/v1/mam/allocations/{id} para activar o ajustar configuración.
8. POST /api/v1/mam/allocations/{id}/unsubscribe para terminar la relación.
9. GET /api/v1/perf-fee/transactions para consultar créditos recibidos.
10. GET /api/v1/perf-fee/master/{master_login}/investor-payments para segregarlos.
11. POST /api/v1/mam/leaders/{master_login}/payment-account/withdraw para retirar fees.
12. Consultar impact y ejecutar account-deletions o investor-account-deletions cuando se elimine una cuenta del motor.
13. POST /api/v1/mam/webhooks una sola vez y conservar signing_secret.
```
