# Especificación Módulo Molienda y Ensilaje — CrianzaApp (v1)

Fecha: 2026-08-10
Última actualización: 2026-08-10 (captura por **carga** con folio automático; un pH único al lograr el objetivo; recepción como campo escrito; el registro oficial pasa de grilla mensual a **tabla de cargas**)
Estado: **Aprobado para implementación** — PR1 y PR2 desbloqueados
Origen: digitalización del registro en papel **PC 03.2 "Control de Molienda y Ensilaje"**, versión 2.0 (28-11-22), Programa de Crianza, Acuícola Alas S.A.
Objetivo: llevar a CrianzaApp el control de molienda de mortalidades y el control semanal de la zona de ensilaje, con captura remota offline por el operador, recepción firmada por un segundo usuario y trazabilidad de tambores y ácido.

---

## 1. Resumen funcional

### 1.1 Qué registra el formato en papel

**A. Registro diario** (una fila por día, lunes a domingo):

| Columna del papel | Naturaleza |
|---|---|
| Fecha / día | fecha |
| Mortalidad ✓/X | ¿hubo mortalidad que moler ese día? |
| Kg Mortalidad | kilos pesados en la moledora |
| Uso de ácido ✓/X | ¿se aplicó ácido al tambor? |
| Límites pH (bajo 4 ✓ / sobre 4 X) | resultado del control de pH |
| Ácido adicional ✓/X | corrección aplicada cuando el pH quedó sobre 4 |
| Tambor N° | tambor que recibió la carga |
| Cierre tambor ✓/X | el tambor se cerró/selló ese día |

**B. Inspección semanal** (una columna por semana). Pese a que el encabezado dice ✓/X, en el
formato lleno las celdas son **valores**, no checks:

| Ítem del papel | Naturaleza |
|---|---|
| Limpieza y orden | ✓/X |
| Tambores sellados y sin fugas | conteo (ej. 4) |
| Cantidad ácido utilizado lts | litros (ej. 5) |
| Cant ácido disponible lts | litros (ej. 431) |
| Cant tambores utilizados | conteo (ej. 4) |
| Cant tambores disponibles | conteo (ej. 23) |
| Lote ácido | texto/identificador |
| Responsable / Inspección | firmas |

### 1.2 Decisiones de alcance (2026-08-10)

1. **App remota = PWA offline**, clon del patrón ya probado en `/captura` (`app/field_client/`)
   y `/sexado_offline` (`app/sexado_client/`): captura sin señal, cola local, sincronización
   idempotente por `client_uuid`.
2. **Tambores = entidad con ciclo de vida** (apertura → cargas → cierre/sellado → retiro), no un
   número suelto. **Un solo tambor abierto a la vez.**
3. **Ácido = inventario** (ingresos + consumo → disponible calculado), no captura manual.
4. **Kg de mortalidad = independientes ahora**, con panel de conciliación contra la mortalidad
   declarada en CrianzaApp en una fase posterior (nunca bloqueante).
5. **Un segundo ingreso el mismo día es aditivo.** La unidad de captura es la **carga** (cada vez
   que se muele y se descarga al tambor), no el día.
6. **Se registra una sola vez, al lograr el objetivo.** El operador dosifica y mide hasta alcanzar
   pH < 4, y recién entonces registra. No hay remedición ni ciclo de corrección en la app (§4.3).
7. **Folio automático por carga**, asignado por el servidor. El folio mensual del papel pierde
   sentido al cargar desde la app (§3.2).
8. **Dos usuarios**: el operador registra desde la tablet; **otro usuario recibe** el registro
   desde la app. En v1 la recepción es un **campo escrito**, no una firma acreditada (§4.5).
9. **El registro oficial deja de ser la grilla del papel**: pasa a ser una **tabla de cargas**, una
   línea por carga. Un día con dos tambores son simplemente dos líneas (§4.1).

---

## 2. Punto de partida en el código

- **No existe nada** de molienda/ensilaje en la app (`grep -i "ensilaje|molienda|silage"` sin
  resultados). Es un módulo nuevo completo, sin migración de datos previos.
- La mortalidad actual se registra **en peces, no en kilos**: `mortality_reports`,
  `marked/unmarked_mortality_registries`, `ponds_movements.fish_quantity`. Ningún campo de peso
  → los kg del PC 03.2 son un dato nuevo.
- **Precedente directo para la recepción firmada**: `mortality_reports` ya tiene el patrón de doble
  validación (`supervisor_validation`, `general_validation`, `*_validation_time`,
  `supervisor_user_id`). El módulo lo reutiliza en vez de inventar otro esquema (§4.5).
- Se copia el molde del módulo **calidad de agua** (el más reciente): modelos + umbrales
  configurables en BD + servicio puro + router `/views/ui/...` + PWA + QR + sección en
  `app/api/config.py::CONFIG_SECTIONS`.
- Convenciones: routers HTML bajo `/views/ui/*` con `jinja_env` + `_sidebar.html`; API versionada
  bajo `/api/<dominio>/v1`; migraciones `YYYYMMDD_NN_slug.py`.
- ⚠️ **CrianzaApp no tiene capa de autenticación** (los routers son explícitos al respecto). Esto
  choca de frente con el requisito de firma digital: ver §4.5.

---

## 3. Modelo de datos propuesto

### 3.1 `silage_drums` — tambores (ciclo de vida)

| Campo | Tipo | Descripción |
|---|---|---|
| id | BigInteger PK | |
| drum_number | Integer | N° de tambor rotulado en terreno |
| status | String(20) | `available` / `open` / `sealed` / `dispatched` |
| opened_at | Date | Primera carga |
| sealed_at | Date | Cierre/sellado |
| dispatched_at | Date | Retiro de planta (§3.6) |
| dispatch_id | BigInteger FK→silage_drum_dispatches.id nullable | Guía con que salió |
| total_kg | Numeric(10,2) | Kg acumulados (derivado de las cargas) |
| acid_lts | Numeric(10,2) | Ácido total aplicado a este tambor |
| observation | String(255) | |
| created_at / updated_at | Timestamp | |

**Sin `capacity_kg` por tambor.** El volumen del tambor es fijo y se configura centralizado, así
que vive como umbral único `drum_capacity_kg` (§3.5). Un solo lugar donde cambiarlo si mañana se
compran tambores de otro formato, y ningún riesgo de tambores con capacidades divergentes.

**Un solo tambor abierto a la vez.** Se garantiza en la BD con índice único parcial, no solo en la
lógica de aplicación:

```sql
CREATE UNIQUE INDEX uniq_silage_drum_open ON silage_drums ((true)) WHERE status = 'open';
```

Consecuencia operativa: cerrar un tambor y abrir el siguiente es una transacción única —
`sealed` al que se va, `open` al que entra— porque el índice no admite el estado intermedio con
dos abiertos. La PWA offline debe reflejar esa secuencia en la cola de sincronización.

### 3.2 `silage_grinding_events` — cargas de molienda (unidad de captura)

Cada vez que el operador muele y descarga al tambor registra una **carga**; un día puede tener
varias y los kg se suman. Es la única tabla de captura del registro diario.

| Campo | Tipo | Descripción |
|---|---|---|
| id | BigInteger PK | |
| folio | BigInteger **unique**, nullable hasta sincronizar | Correlativo **automático del servidor** (§3.3) |
| event_datetime | Timestamp **not null**, index | Momento de la carga |
| log_date | Date **not null**, index (**no** único) | Fecha de la carga; agrupa el resumen diario |
| had_mortality | Boolean not null | `True` = carga con kg. `False` = marca explícita de "hoy no hubo mortalidad" (§4.2) |
| mortality_kg | Numeric(10,2) nullable | Kg de **esta** carga (NULL si `had_mortality = False`) |
| acid_used | Boolean | "Uso de ácido" en esta carga |
| acid_lts | Numeric(8,2) nullable | Litros **totales** aplicados, digitados por el operador (alimenta el inventario) |
| additional_acid | Boolean | `True` si necesitó ácido adicional para bajar de 4 (dato retrospectivo) |
| ph_value | Numeric(4,2) nullable | pH **alcanzado** al cerrar la carga. Se registra una sola vez, ya conforme (§4.3) |
| ph_below_limit | Boolean | Derivado de `ph_value < ph_limit` |
| drum_id | BigInteger FK→silage_drums.id nullable | Tambor que recibió **esta** carga |
| drum_number | Integer nullable | Copia textual del N° (resiliencia histórica) |
| drum_closed | Boolean | El tambor se cerró al terminar esta carga |
| operator_id | BigInteger FK→users.id nullable | Quien molió y registró (selector, no firma) |
| received_by_name | String(120) nullable | **Texto escrito** de quién recibió. No acredita identidad (§4.5) |
| received_at | Timestamp nullable | Momento de la recepción |
| reception_note | String(255) nullable | Observación del receptor |
| observation | String(255) | Observación del operador |
| voided_at / voided_by / void_reason | Timestamp / FK / String(255) | Anulación (borrado lógico) |
| client_uuid | String(64) **unique** | Idempotencia de sincronización PWA; NULL si vino del web |
| created_at / updated_at | Timestamp | |

Consecuencias de que la captura sea aditiva:

- **No hay unicidad por fecha**, así que el reenvío de un `client_uuid` ya presente es un
  **duplicado que se ignora** — exactamente la semántica de
  `POST /api/field/v1/oxygen-readings`. Sin upsert, sin conflictos de fecha, sin bandeja de
  reconciliación.
- Una **corrección** (kg mal digitados) es una operación distinta y explícita: anular la carga
  (`voided_at`) y registrar la correcta. El original queda visible en la auditoría.
- Dos cargas del mismo día pueden ir a **tambores distintos**; en la tabla de cargas son
  simplemente dos líneas, sin ambigüedad (§4.1).

### 3.3 El folio: automático y **asignado en el servidor**

El folio es un correlativo por carga, respaldado por una secuencia Postgres y expuesto con formato
`EN-2026-000123`.

Punto crítico: **la PWA no puede generar el folio offline.** Dos tablets sin señal producirían
correlativos colisionantes o con huecos. Por eso el folio se asigna en el `INSERT` del servidor, al
sincronizar, y hasta entonces la carga vive en el teléfono identificada solo por su `client_uuid`.
La UI offline debe mostrar "folio pendiente" en vez de un número provisorio, o el operador anotará
en su cuaderno un folio que después no existe.

### 3.4 `silage_weekly_inspections` — inspección semanal

| Campo | Tipo | Descripción |
|---|---|---|
| id | BigInteger PK | |
| week_start | Date **unique** | Lunes de la semana ISO |
| cleanliness_ok | Boolean | "Limpieza y orden" |
| drums_sealed_ok | Boolean | "Tambores sellados y sin fugas" (check) |
| drums_sealed_count | Integer | El número que en la práctica se anota en esa celda |
| acid_used_lts | Numeric(10,2) | Declarado por el inspector |
| acid_available_lts | Numeric(10,2) | Declarado por el inspector |
| drums_used | Integer | Declarado |
| drums_available | Integer | Declarado |
| acid_lot | String(60) | "Lote ácido" |
| responsible_name | String(120) | "Responsable" (texto escrito, §4.5) |
| inspector_name | String(120) | "Inspección" (texto escrito, §4.5) |
| inspected_at | Timestamp | |
| observation | String(255) | |
| client_uuid | String(64) unique | |
| created_at / updated_at | Timestamp | |

Los campos declarados **se conservan tal cual los escribe el inspector** (valor legal del
registro). El sistema calcula su propio valor en paralelo (§3.5) y muestra el **descuadre**; nunca
sobrescribe lo declarado.

### 3.5 `silage_acid_lots` y `silage_acid_movements` — inventario de ácido

`silage_acid_lots`: `id`, `lot_code` (unique), `supplier`, `received_date`, `received_lts`,
`concentration`, `notes`, `active`.

`silage_acid_movements`: `id`, `lot_id` FK, `movement_date`, `movement_type`
(`in` / `out` / `adjustment`), `lts` Numeric(10,2), `grinding_event_id` FK nullable (consumo que
originó el egreso), `drum_id` FK nullable, `user_id`, `notes`, `created_at`.

**Disponible = Σ ingresos + Σ ajustes − Σ consumos.** Mismo criterio que el inventario de alimento
de CrianzaApp (memoria `reference_crianza_feed_stock_model.md`), para no inventar una segunda
semántica de stock en la misma app.

### 3.6 `silage_drum_dispatches` — salida de tambores (proceso secundario)

Manejo tipo inventario: se emite una **guía de despacho** y los tambores incluidos se dan de baja.

`id`, `dispatch_date`, `guide_number` (N° de guía), `carrier` / `destination`, `total_drums`,
`total_kg` (suma de los tambores incluidos), `user_id`, `notes`, `created_at`.

Al confirmar la guía, cada tambor pasa a `dispatched` con su `dispatched_at` y `dispatch_id`. Es
un proceso **independiente y secundario** del registro diario: va en PR6 y no bloquea nada.

### 3.7 `silage_thresholds` — parámetros configurables

Espejo de `water_quality_thresholds` (una fila por parámetro, editable desde Configuración sin
tocar código). Sembrados en la migración:

| parámetro | default | uso |
|---|---|---|
| `ph_limit` | 4.0 | **Criterio de aceptación** del ensilaje ("bajo 4 ✓") |
| `drum_capacity_kg` | *(a confirmar)* | Volumen fijo del tambor, central. Alerta de tambor por llenarse |
| `acid_low_stock_lts` | *(a confirmar)* | Alerta de stock bajo de ácido |
| `acid_lts_per_kg_min` | 0.10 | Piso del rango de dosis esperado, para detectar tipeos |
| `acid_lts_per_kg_max` | 0.25 | Techo del rango de dosis esperado |

---

## 4. Reglas de negocio (motor puro `app/services/silage.py`)

Sin acceso a BD, testeable en aislamiento, igual que `app/services/water_quality.py`.

### 4.1 El registro oficial es una tabla de cargas

**No se replica la grilla del papel.** El registro impreso/exportable es un listado, una línea por
carga:

| Folio | Fecha y hora | Kg | Ácido lts | Adicional | pH | Tambor | Cierre | Operador | Recibido por |
|---|---|---|---|---|---|---|---|---|---|
| EN-2026-000121 | 31-03 08:40 | 45,0 | 6,0 | ✓ | 3,8 | 6 | | R. Gatica | A. Vial |
| EN-2026-000122 | 31-03 15:10 | 12,5 | 2,0 | | 3,6 | 6 | ✓ | R. Gatica | A. Vial |

Esto elimina de raíz tres problemas que arrastraba la grilla: la celda "Tambor N°" que solo admitía
un número, el criterio de cómo combinar varios pH en un solo ✓/X, y la ambigüedad de qué significa
"Ácido adicional" cuando hubo dos cargas. Cada línea se explica sola.

**El resumen diario sobrevive como vista, no como registro**: el panel mensual muestra por fecha
los kg totales, N° de cargas, tambores usados y pH mínimo/máximo, con enlace al detalle. Es
información de gestión, no el documento de auditoría. Suma solo cargas **no anuladas**; las
anuladas aparecen tachadas con su motivo en el detalle del día.

### 4.2 Día sin mortalidad

Un día sin mortalidad **no queda en blanco**: el operador registra una carga con
`had_mortality = False` y `mortality_kg = NULL`, que aparece en la tabla como línea "sin
mortalidad". Así se distingue del día que **nadie registró**, que el panel mensual marca como
registro faltante. Esa diferencia es justamente lo que revisa un auditor.

### 4.3 pH: se registra una sola vez, ya conforme

El operador dosifica, mide, corrige si hace falta y **registra recién cuando alcanzó pH < 4**. En
consecuencia:

- Hay **un solo** `ph_value` por carga: el valor logrado. No existe remedición ni ciclo de
  corrección modelado en la app.
- `additional_acid` es un dato **retrospectivo**: "necesité agregar ácido para llegar", marcado al
  registrar. No dispara ninguna acción pendiente.
- `acid_lts` son los litros **totales** efectivamente usados, incluida la corrección. Es el número
  que consume el inventario.

**Contrapartida asumida, que conviene tener explícita:** el registro deja constancia del resultado,
no del recorrido. Si una carga costó tres correcciones y el triple de ácido, eso se ve en los
litros consumidos, no en un historial de mediciones. Es una simplificación deliberada y coherente
con cómo se llena hoy el papel.

- **Excepción**: si alguna vez se registra `ph_value ≥ ph_limit`, la carga queda marcada
  `no_conforme` y visible en el panel hasta que alguien la resuelva. Es una vía de escape para
  reportar un problema real, no el flujo normal.

### 4.4 El ácido: dosis digitada, pH como criterio

El operador **no** recibe una dosis calculada por el sistema: digita los litros que efectivamente
usó. El único criterio de aceptación es **pH < 4**.

- **`ph_value < ph_limit` es la regla dura.** Determina si la carga está conforme.
- **El rango 0,10–0,25 lts/kg es solo un chequeo de verosimilitud** del número digitado, para cazar
  tipeos (5 lts tecleados como 50). Se emite como aviso `dose_out_of_range` y **nunca bloquea ni
  invalida** la carga: si el operador alcanzó pH 3,8 con 0,08 lts/kg, la carga está conforme y el
  aviso es ruido.

Consecuencia de diseño: la dosis nunca debe presentarse en la UI como error en rojo, o el operador
va a "ajustar" el número digitado para que calce con el rango — corrompiendo justamente el dato de
consumo que alimenta el inventario de ácido.

### 4.5 Recepción

El flujo tiene dos actores: el **operador** registra desde la tablet; un **segundo usuario recibe**
la carga desde la app. La carga tiene entonces dos estados: `registrada` → `recibida` (más
`anulada`).

**Decisión v1: la recepción es un campo de texto escrito**, `received_by_name`, que puede llenar
cualquiera que use la aplicación. CrianzaApp no tiene autenticación, así que ese campo **no
acredita identidad y no debe llamarse firma** ni en la UI ni en el procedimiento: es la anotación
de quién declara haber recibido. Etiqueta sugerida en pantalla: *"Recibido por"*.

Ruta de evolución cuando se quiera convertirlo en firma real, en orden de costo:

1. **PIN por usuario** — `pin_hash` en `users`, validado al recibir. Acotado al módulo; convierte
   el campo en atribuible sin tocar el resto de la app.
2. **Login real en CrianzaApp** — sesión + middleware para toda la app. Es lo correcto a largo
   plazo, pero es un proyecto aparte.

El modelo queda preparado para ambas: agregar `received_by_user_id` nullable más adelante es una
migración trivial que convive con el texto ya registrado, sin reescribir el histórico.

### 4.6 Validaciones y alertas restantes

1. `had_mortality = True` ⟹ `mortality_kg > 0`; `had_mortality = False` ⟹ `mortality_kg` NULL y
   sin tambor ni ácido asociados.
2. Carga con `mortality_kg > 0` ⟹ requiere `drum_id` del tambor en estado `open` (hay uno solo).
3. `drum_closed = True` ⟹ el tambor pasa a `sealed` y se abre el siguiente **en la misma
   transacción** (§3.1). La siguiente carga —aunque sea del mismo día— va al tambor nuevo.
4. Alerta `drum_near_capacity` cuando `total_kg ≥ drum_capacity_kg × 0.9`. Se evalúa **por carga**,
   así que el operador la ve al descargar y puede cerrar el tambor ahí mismo.
5. Alerta `acid_low_stock` cuando el disponible calculado cae bajo `acid_low_stock_lts`.
6. Consolidación **semanal**: kg del período, litros de ácido consumidos, tambores usados /
   sellados / disponibles → se comparan contra lo declarado en la inspección y se emite `variance`
   por ítem (informativo, no bloqueante).
7. **Cargas sin recibir** por más de N días: listado en el panel (N configurable).

---

## 5. API `/api/silage/v1` (`app/api/silage.py`)

### 5.1 `GET /bootstrap`

Lo que la PWA cachea para operar offline: tambor abierto + tambores disponibles, umbrales,
operadores activos (`users.active`), lotes de ácido vigentes, disponible actual, últimas ~30 cargas
(para que el operador vea qué ya registró) y `server_time`.

### 5.2 `POST /grinding-events` (lote, idempotente)

Contrato **idéntico** a `POST /api/field/v1/oxygen-readings`: lista de ítems con `client_uuid`,
respuesta `{created, duplicates, errors, results[]}` con estado por ítem y `begin_nested()` por
fila para que un dato malo no aborte el lote. El **folio se asigna aquí** (§3.3) y vuelve en
`results[]` para que la PWA lo muestre tras sincronizar.

Como las cargas son aditivas y no hay unicidad por fecha, el reenvío de un `client_uuid` ya
presente es simplemente `duplicate`. Varias cargas del mismo día son filas normales, cada una con
su propio `client_uuid`.

### 5.3 `POST /grinding-events/{id}/void`

Anulación de una carga mal registrada (borrado lógico con motivo). Es la única forma de "corregir".

### 5.4 `POST /grinding-events/{id}/receive`

Recepción por el segundo usuario (§4.5): `received_by_name` (texto), `reception_note`. Idempotente:
recibir dos veces no pisa la recepción original.

### 5.5 `POST /weekly-inspections`

La inspección semanal **sí** es un registro único por semana (un inspector, una firma), así que
aquí el reenvío del mismo `client_uuid` **actualiza** la fila. Es la excepción, no la regla.

### 5.6 Efecto lateral

Cada carga con `acid_lts` genera su `silage_acid_movements` de tipo `out` en la misma transacción,
y recalcula `silage_drums.total_kg`. Anular una carga genera el movimiento inverso (`adjustment`)
y descuenta los kg del tambor.

---

## 6. Cliente remoto `app/silage_client/` → `/ensilaje`

Clon del esqueleto de `app/field_client/` (626 líneas de `app.js`, `sw.js` de 43, manifest e
íconos), montado en `main.py` como
`app.mount("/ensilaje", StaticFiles(directory=.../"silage_client", html=True), name="ensilaje")`.

Pantallas: **(1)** nueva carga (formulario corto, prellenado con el tambor abierto y el operador de
la última sesión), **(2)** cargas del día, con el **acumulado de kg** a la vista, el folio —o
"pendiente" si aún no sincroniza— y la acción de anular, **(3)** inspección semanal (aparece solo
al cierre de semana), **(4)** cola de sincronización con estado por ítem.

La pantalla (2) es la que hace visible el modelo aditivo: el operador que vuelve a la moledora por
segunda vez ve "hoy van 45 kg en el tambor 6" y agrega la carga nueva, en vez de dudar si tiene que
sumar mentalmente y reescribir el total del día.

La **recepción firmada no va en la PWA**: es otro usuario, desde la app web, con la carga ya
sincronizada.

El volumen es bajo (una a pocas cargas por día + 1 inspección/semana), así que la PWA se justifica
por la **falta de señal en la zona de molienda**, no por throughput: la cola local debe sobrevivir
días sin conexión y sincronizar cuando el teléfono vuelva a wifi.

Acceso por **QR impreso** en la zona de ensilaje, reutilizando `segno` y el patrón de
`calidad_agua_qr.html`.

---

## 7. Fases de implementación (un PR por fase)

| PR | Contenido | Verificación | Estado |
|---|---|---|---|
| **PR1** | Modelos (§3) + migración `20260810_01_add_silage_module.py` + secuencia de folio + índice único parcial + seeds de umbrales | `alembic upgrade head`; `\d` de las tablas; intentar dos tambores `open` debe fallar | ✅ hecho |
| **PR2** | `app/services/silage.py`: motor puro (§4) | Casos directos sin BD: día de 1 carga, de 3 cargas, con carga anulada, con dos tambores, sin mortalidad, dosis fuera de rango, pH ≥ 4 | ✅ hecho (19 grupos de casos) |
| **PR3** | `app/api/silage.py` → `/api/silage/v1` (§5) | Lotes de prueba: creado / duplicado / error; folio correlativo sin huecos; dos cargas del mismo día suman | ✅ hecho (14 grupos por HTTP) |
| **PR4** | PWA `app/silage_client/` + mount `/ensilaje` (§6) | Captura offline (modo avión) → sincroniza al volver; segunda carga del día no pisa la primera; folio aparece recién al sincronizar | ✅ código listo; **falta la prueba en un teléfono real** (ver §9) |
| **PR5** | Vistas web `/views/ui/ensilaje`: **tabla de cargas** (registro oficial, imprimible/exportable), resumen mensual, detalle del día, **recepción** (§4.5), form de inspección semanal, ítem en `_sidebar.html`, sección en `CONFIG_SECTIONS` | Impresión revisada con el encargado del registro | ✅ hecho |
| **PR6** | Gestión de tambores y lotes de ácido (altas, ingresos, ajustes), **guías de despacho y baja de tambores** (§3.6), export mensual, **panel de conciliación** kg molidos ↔ peces muertos declarados | Cuadratura de disponible; guía deja los tambores en `dispatched` | ✅ hecho |

Dependencias: PR1 → PR2 → PR3 → {PR4, PR5} → PR6.

---

## 8. Decisiones cerradas y preguntas abiertas

**Cerradas (2026-08-10)** — incorporadas al modelo; PR1 a PR4 quedan desbloqueados:

- App remota = **PWA offline**; tambores = **entidad con ciclo de vida**; ácido = **inventario**;
  kg **independientes** de la mortalidad declarada, con conciliación posterior.
- Segundo ingreso del mismo día = **aditivo**; la unidad de captura es la carga.
- **Un solo tambor abierto**, garantizado por índice único parcial.
- **Volumen del tambor fijo y centralizado**: umbral único, no columna por tambor.
- **Dosis digitada** por el operador; rango 0,10–0,25 lts/kg solo como chequeo; criterio de
  aceptación = **pH < 4**.
- **Sin remedición**: se registra una vez alcanzado el objetivo; `additional_acid` es retrospectivo.
- **Folio automático por carga**, asignado por el servidor al sincronizar. Se elimina el folio
  mensual.
- **Recepción por un segundo usuario** como **campo de texto escrito**, sin pretensión de firma;
  ruta de evolución a PIN o login documentada (§4.5).
- **Salida de tambores** = guía de despacho + baja, proceso secundario (PR6).
- **La impresión no replica el papel**: tabla de cargas, un día con dos tambores son dos líneas.

**Abiertas** (ninguna bloquea PR1–PR5):

1. Cuándo convertir la recepción en firma real y con qué mecanismo (§4.5). Diferido a propósito.
2. Valor de **`drum_capacity_kg`** en kg y de **`acid_low_stock_lts`**. Se siembran provisorios en
   la migración y se ajustan desde Configuración sin tocar código.
3. Formato del folio: ¿`EN-2026-000123` o correlativo simple? ¿Se reinicia cada año?
4. ¿La inspección semanal también requiere recepción, o basta con `responsible` + `inspector`?
5. ¿Cuántos días sin recibir antes de que una carga aparezca como pendiente en el panel (§4.6.7)?

---

## 9. Estado de la implementación (2026-08-10)

**PR1–PR4 implementados y verificados.** Archivos:

| Archivo | Rol |
|---|---|
| `app/models/silage_*.py` | 7 modelos; registrados en `app/models/__init__.py` |
| `alembic/versions/20260810_01_add_silage_module.py` | Tablas + `silage_folio_seq` + `uniq_silage_drum_open` + seeds |
| `app/services/silage.py` | Motor puro (§4) |
| `app/api/silage.py` | `/api/silage/v1` (§5); incluido en `app/main.py` |
| `app/silage_client/` | PWA; montada en `/ensilaje` en `app/main.py` |

Verificado: la BD rechaza dos tambores `open` a la vez; el folio es correlativo y
lo asigna el servidor; reenviar un `client_uuid` responde `duplicate` sin
duplicar; dos cargas del mismo día **suman**; una carga inválida no aborta el
lote; la dosis fuera de rango avisa pero no bloquea; el pH sobre el límite se
acepta como `no_conforme`; anular descuenta del tambor y revierte el ácido; la
recepción es idempotente; cerrar el tambor deja abrir el siguiente.

**Pendiente de PR4**: probar la PWA en un teléfono real (instalación, captura en
modo avión y sincronización al recuperar señal). El código se sirve correctamente
en `/ensilaje` y la lógica de cola está escrita, pero el comportamiento offline
del service worker no se ejercitó en un navegador.

---

## 10. Segunda iteración (2026-08-10): procedimiento escrito, seguridad y administración

Al ver la app en la tablet aparecieron tres necesidades. Además, el texto
**"Información Importante"** del formato en papel aportó datos duros que
**corrigen** los parámetros que se habían sembrado a ojo.

### 10.1 Correcciones que trajo el procedimiento (migración `20260810_02`)

| Dato del procedimiento | Qué estaba mal | Ahora |
|---|---|---|
| "Ácido fórmico en relación **1 litro por 4 kg**" = 0,25 lts/kg | El rango sembrado era 0,10–**0,25**: el valor nominal caía justo en el **techo**, así que toda carga hecha exactamente según el procedimiento quedaba al borde del aviso | Rango **0,20–0,30**, que bracketea el nominal, + `acid_lts_per_kg_nominal` = 0,25 |
| "Cuando el tambor se encuentre en **4/5 partes**, cerrarlo y sellarlo" | El motor avisaba al **90%** (número inventado) | Umbral configurable `drum_close_fill_ratio` = **0,80**; el estado pasó de `near_capacity` ("conviene") a **`should_close`** ("el procedimiento manda") |
| "El pH debe ser **inferior a 4.0**" | — | Confirmado, sin cambios |
| "Seguir con el tambor del **siguiente número correlativo**" | — | El alta de tambores propone el siguiente correlativo |

La dosis nominal ahora se **muestra como referencia** en el formulario ("el
procedimiento indica ~11,3 lts para 45 kg"), pero **no se rellena sola**: el
número que consume el inventario tiene que ser el que el operador usó de verdad.

### 10.2 Ventana de seguridad (PWA)

Al abrir la app se muestra el texto completo del procedimiento —EPP, pasos de
molienda, medición de pH y precauciones, con los cuatro pictogramas— y la única
salida es **"Entiendo y acepto las condiciones"**. No hay botón de cerrar ni
cierre por toque fuera.

Se muestra en **cada apertura**, no una sola vez: se trabaja con ácido fórmico y
un recordatorio que se puede saltar no es un recordatorio. Queda accesible
después desde el botón *⚠️ Seguridad* de la barra superior; sin eso, aceptada la
ventana el texto quedaba inalcanzable.

La aceptación se guarda **local** (`localStorage`), no en la BD: sin
autenticación, un registro de "quién aceptó" tendría el mismo problema que la
recepción (§4.5), así que no se promete lo que no se puede acreditar.

### 10.3 Administración web (`/views/ui/ensilaje`)

Adelantada desde PR6 porque sin ella el módulo no se puede usar:

- **Tambores** (`/tambores`): alta de tambores vacíos **por rango** (llegan de a
  varios), estado de cada uno con barra de llenado, cambio manual de estado y
  KPIs. Omite números que ya existen sin retirar. Avisa antes de intentar abrir
  un segundo tambor, en vez de dejar que reviente el índice único de la BD.
- **Ácido** (`/acido`): ingreso de lotes (crea el lote **y** su movimiento `in`
  en la misma operación, para que no exista un lote recibido que no suma al
  disponible), ajustes manuales con motivo obligatorio, y el histórico de
  movimientos. El disponible **siempre se calcula**; no hay campo de stock
  editable que pueda desalinearse.

Enlazadas desde el sidebar ("Ensilaje") y desde Configuración.

---

## 11. PR5 y PR6 (2026-08-12): el módulo queda completo

### 11.1 Páginas nuevas (`app/api/silage_views.py` + plantillas)

| Ruta | Qué hace |
|---|---|
| `/registro` | **Registro oficial**: KPIs del mes, resumen por día y la tabla de cargas (una línea por carga). Recepción en línea, botón de imprimir y export CSV |
| `/registro.csv` | Export del mes, `;` como separador y BOM (Excel en español no abre bien los acentos sin él) |
| `/dia/{fecha}` | Detalle del día: cada carga con sus acciones de recibir y anular; las anuladas se muestran tachadas con su motivo |
| `/inspeccion` | Inspección semanal + tabla **declarado vs. calculado** con el descuadre por ítem + histórico de 12 semanas |
| `/despachos` | Guía de despacho: selección de tambores **sellados**, total en vivo, y baja de los incluidos |
| `/conciliacion` | Kg molidos contra peces muertos declarados en cultivo, con el peso medio por pez |

Se extrajeron dos parciales, `_ensilaje_style.html` y `_ensilaje_tabs.html`, para
no llevar el mismo CSS y la misma navegación copiados en seis plantillas.

### 11.2 Decisiones que valen la pena registrar

- **La impresión no imita el formato en papel**: sale la tabla de cargas en
  horizontal, con recuadros de firma al pie. El CSS de impresión oculta sidebar,
  pestañas y formularios.
- **Solo se despachan tambores sellados.** El abierto sigue recibiendo cargas;
  sacarlo de planta dejaría el registro del día apuntando a un tambor que ya no
  está.
- **La conciliación lee `ponds_movements` con `movement_reason='mortality'`**, que
  es donde registra la mortalidad el resto de CrianzaApp (las tablas
  `mortality_reports` tienen 4 filas y están en desuso). Es informativa y no
  bloquea: los dos registros los llenan personas distintas en momentos distintos,
  así que un pez que muere el domingo y se muele el lunes descuadra sin que nada
  esté mal. Lo que importa es la **tendencia del peso medio** y los días con
  molienda sin ninguna mortalidad declarada.
- **Días sin registro**: el resumen mensual los cuenta y los marca. Es la
  traducción de "celda en blanco ≠ X" del formato en papel.

### 11.3 Cierre de tambor sin carga (2026-08-12, migración `20260812_01`)

Caso real que el flujo no cubría: **el tambor va casi lleno y la molienda del día
no cabe**, así que el operador la echa en un tambor nuevo y da por terminado el
anterior *sin cargarle nada*. Hasta ahora sellar era solo una casilla de la carga
(«cerré el tambor con esta carga»), que describe el caso opuesto.

- `POST /api/silage/v1/drums/close` — **idempotente por `drum_number`**, no por
  `client_uuid`: no hay tabla de acciones donde guardar el uuid, pero el estado
  del tambor alcanza. Un tambor ya sellado responde `already_closed` con 200, no
  error, porque la PWA reintenta desde la cola offline y un reintento no es falla.
- Columnas nuevas `silage_drums.sealed_by` / `seal_note`: en este cierre no hay
  carga de la que deducir quién lo cerró.
- Pantalla propia en la PWA (botón «Cerrar tambor»), que muestra los kg y el % del
  tambor, pide motivo y autor, y avisa cuál es el siguiente correlativo. Incluye
  la advertencia de **cuándo no usarla** (si el tambor sí recibió la molienda, va
  la casilla de la carga).

**Lo delicado fue el orden de la cola offline.** La cola pasó a llevar dos tipos
de acción (`_kind` = carga | cierre) y se sincroniza en **orden cronológico**,
agrupando solo los tramos consecutivos del mismo tipo. Si se enviaran todas las
cargas primero, cerrar el tambor 6 y luego cargar al 7 haría que la carga se
rechazara por «hay otro tambor abierto». Además:

- `effectiveOpenDrum()` trata el tambor como cerrado en cuanto hay un cierre
  esperando en la cola, para que la app no siga ofreciendo un tambor que para el
  operador ya está sellado.
- Un cierre que responde 404 (el tambor no existe) **sale de la cola** con aviso,
  en vez de quedar trabando indefinidamente todo lo que viene detrás.

### 11.4 Primeros auxilios en la PWA (2026-08-12)

Panel con los primeros auxilios del ácido fórmico (contacto con piel, inhalación,
contacto con ojos, ingestión/aspiración), accesible desde la barra superior y
también **desde dentro de la ventana de seguridad**.

Tres decisiones, las tres por la misma razón —es información de emergencia—:

- Se dibuja **por encima del gate** (`z-index` mayor), así que se puede consultar
  incluso antes de aceptar las condiciones. No debe existir ningún estado de la
  app en que quede inalcanzable.
- La lógica (`openAid` / `closeAid`) vive en el `<script>` de `index.html`, **no
  en `app.js`**, por el mismo motivo que el gate: un error en el bundle no puede
  dejar sin primeros auxilios a quien los necesita.
- El texto va **dentro de `index.html`**, que ya está precacheado por el service
  worker, así que funciona sin señal sin agregar nada al precache.

El texto se transcribió **literal** del documento entregado (verificado palabra
por palabra, 1726 caracteres); solo se marcaron en negrita las instrucciones
críticas (*no quitar la ropa*, *nunca reventar las ampollas*, *15 minutos*, *no
inducir al vómito*, *no administrar nada por vía oral*).

**Emergencias: 131** (ambulancia / SAMU). Va como enlace `tel:131` en un botón
grande, lo primero del panel, así que en la tablet marca directo. Además el número
se muestra **sin abrir el panel**: en la insignia de la barra superior y en el
botón de la ventana de seguridad. El texto repite «asistencia médica inmediata»
cuatro veces; obligar a navegar para encontrar el número contradecía eso.

### 11.5 Qué sigue faltando

- La API sigue **abriendo un tambor al vuelo** si llega una carga con un N° que
  no existe. Tenía sentido cuando no había alta de tambores; ahora que existe,
  conviene decidir si pasa a rechazarse (evita rótulos nacidos de un typo) o se
  deja como red de seguridad para no bloquear la captura en terreno.
- `drum_capacity_kg` (200 kg) y `acid_low_stock_lts` (50 lts) **siguen siendo
  provisorios**: son los dos únicos valores que aún no salen de un dato real.
- La inspección semanal no tiene recepción por un segundo usuario (§8, abierta 4).
- Sigue pendiente convertir la recepción en firma acreditada (§4.5).
