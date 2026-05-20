# Especificación Módulo Reproducción — CrianzaApp (v1)

Fecha: 2026-05-13  
Última actualización: 2026-05-13 (Proceso 3 incorporado)  
Estado: **Aprobado para implementación**  
Objetivo: Agregar la sección "Reproducción" al sistema CrianzaApp como nuevo menú en el sidebar, debajo de Alimentación, para gestionar la selección y seguimiento de peces reproductores.

---

## 1. Resumen funcional

El módulo permite:
1. Seleccionar cualquier pez individual como candidato reproductor desde la vista fish.
2. Gestionar un período de selección activa de 30 días con registro de fármacos y monitoreos.
3. Al finalizar los 30 días, aplicar reglas de transición según uso de fármacos: si hubo fármacos se agrega sufijo `_R` al pit-tag, si no, el pez vuelve a estado normal.
4. Mantener una lista de "Reproductores" (Tabla B) con peces que tienen sufijo `_R` como banco de candidatos prioritarios para futuras selecciones.
5. Registrar eventos de desove (hembras). Los machos no tienen acción de "Usar" en esta vista; dicha acción queda dentro de la vista de desove (v1.1).
6. El sufijo `_R` actúa como indicador de historial de uso de fármacos. La condición de "seleccionado" es independiente del sufijo (un pez sin `_R` puede estar en selección activa).
7. Monitorear incubadoras activas hasta la eclosión de cada una (**Proceso 3**). Al cerrar la eclosión de una incubadora, los peces ingresan a un estanque Hatchery con un nuevo lote en estado pendiente de primer recuento.

---

## 2. Modelo de datos propuesto

### 2.1 Tabla `reproductor_selections`
Registra cada período de selección de un pez como reproductor activo.

| Campo | Tipo | Descripción |
|---|---|---|
| id | BigInteger PK | |
| fish_id | BigInteger FK→fish.id | Pez seleccionado |
| start_date | Date | Fecha de inicio de la selección |
| end_date | Date | Fecha calculada de término (start_date + 30 días) |
| status | String(30) | `active` / `completed` / `failed` / `spawned` |
| sex_at_selection | String(20) | Copia del sexo del pez al momento de selección |
| closed_at | Timestamp | Cuándo se cerró (manual o automático) |
| notes | Text | Notas opcionales |
| created_at | Timestamp | |
| updated_at | Timestamp | |

### 2.2 Tabla `reproductor_drug_logs`
Registro de fármacos usados durante una selección.

| Campo | Tipo | Descripción |
|---|---|---|
| id | BigInteger PK | |
| selection_id | BigInteger FK→reproductor_selections.id | |
| fish_id | BigInteger FK→fish.id | Desnormalizado para consultas |
| log_date | Date | Fecha de aplicación |
| log_time | Time | Hora de aplicación |
| drug_name | String(200) | Nombre del fármaco |
| dose | String(100) | Dosis (texto libre, ej: "5ml/kg") |
| water_temp | Numeric(5,2) | Temperatura del agua (°C) |
| created_at | Timestamp | |

### 2.3 Tabla `reproductor_monitoring_logs`
Monitoreos reproductivos periódicos. Estructura diferente según sexo.

| Campo | Tipo | Descripción |
|---|---|---|
| id | BigInteger PK | |
| selection_id | BigInteger FK→reproductor_selections.id | |
| fish_id | BigInteger FK→fish.id | Desnormalizado para consultas |
| log_date | Date | Fecha del monitoreo |
| sex | String(20) | Copia del sexo para facilitar queries |
| — *Campos machos* — | | |
| sperm_density | String(10) | `low` / `medium` / `high` |
| sperm_motility | String(10) | `low` / `medium` / `high` |
| sperm_time | String(10) | `low` / `medium` / `high` |
| — *Campos hembras* — | | |
| ip_value | Integer | Índice de Polarización (0–100) |
| observation | String(100) | Observación libre (máx 100 caracteres) |
| created_at | Timestamp | |

### 2.4 Tabla `reproductor_spawning_events`
Registra el proceso de desove como un segundo proceso de reproducción, asociado a la hembra seleccionada.

| Campo | Tipo | Descripción |
|---|---|---|
| id | BigInteger PK | |
| selection_id | BigInteger FK→reproductor_selections.id | Selección activa de la hembra que origina el desove |
| fish_id | BigInteger FK→fish.id | Hembra base del proceso |
| status | String(30) | `draft` / `partial` / `completed` / `cancelled` |
| event_date | Date | Fecha de inicio o edición principal del proceso |
| event_time | Time | Hora de ingreso en UI |
| water_temp | Numeric(5,2) | Temperatura ambiental registrada |
| anesthesia_drug_name | String(200) | Fármaco usado en la preparación de anestesia |
| anesthesia_dilution | String(100) | Dilución de la anestesia (texto libre) |
| total_ova_weight_g | Numeric(10,2) | Peso total de ovas en gramos |
| ova_count_per_5g | Integer | Recuento de ovas en 5 gramos |
| ova_diameter_mm | Numeric(4,2) | Diámetro de las ovas en mm |
| viability_pct | Numeric(5,2) | Viabilidad de 0 a 100 |
| notes | Text | Observaciones generales del proceso |
| created_at | Timestamp | |
| updated_at | Timestamp | |

### 2.5 Tabla `reproductor_spawning_males`
Relación entre el desove y uno o más machos seleccionados.

| Campo | Tipo | Descripción |
|---|---|---|
| id | BigInteger PK | |
| spawning_event_id | BigInteger FK→reproductor_spawning_events.id | Desove al que pertenece |
| male_selection_id | BigInteger FK→reproductor_selections.id | Selección activa del macho usado |
| fish_id | BigInteger FK→fish.id | Macho involucrado |
| created_at | Timestamp | |

### 2.6 Tabla `reproductor_spawning_incubators`
Distribución del peso de ovas en incubadoras.

| Campo | Tipo | Descripción |
|---|---|---|
| id | BigInteger PK | |
| spawning_event_id | BigInteger FK→reproductor_spawning_events.id | Desove asociado |
| incubator_id | Integer | Identificador de incubadora (1 a 20) |
| ova_weight_g | Numeric(10,2) | Peso asignado a la incubadora |
| created_at | Timestamp | |

### 2.7 Tabla `reproductor_recovery_reports`
Resultado de recuperación posterior al desove para cada reproductor involucrado.

| Campo | Tipo | Descripción |
|---|---|---|
| id | BigInteger PK | |
| spawning_event_id | BigInteger FK→reproductor_spawning_events.id | |
| fish_id | BigInteger FK→fish.id | Hembra o macho involucrado |
| role | String(20) | `female` / `male` |
| recovery_result | String(20) | `successful` / `sacrifice` |
| notes | Text | Observaciones opcionales |
| created_at | Timestamp | |

### 2.8 Tabla `incubator_batches`
Representa el proceso 3 de monitoreo de incubadoras, asociado a un desove.

| Campo | Tipo | Descripción |
|---|---|---|
| id | BigInteger PK | |
| spawning_event_id | BigInteger FK→reproductor_spawning_events.id | Desove que originó las incubadoras |
| lot_id | BigInteger FK→lots.id nullable | Lote creado para los peces que eclosionen. Puede ser nulo hasta asignación. |
| lot_name | String(120) | Nombre del lote ingresado por el usuario |
| lot_code | String(3) | Código interno del lote, máximo 3 caracteres |
| status | String(30) | `monitoring` / `completed` / `awaiting_first_count` |
| created_at | Timestamp | |
| updated_at | Timestamp | |

> **Regla:** No es posible cerrar el proceso 3 (`status = completed`) sin que `lot_name` y `lot_code` estén definidos.

### 2.9 Tabla `incubator_units`
Representa cada incubadora activa dentro de un proceso 3. Incluye las unidades originales del desove y las particiones generadas.

| Campo | Tipo | Descripción |
|---|---|---|
| id | BigInteger PK | |
| incubator_batch_id | BigInteger FK→incubator_batches.id | |
| display_id | String(30) | Identificador compuesto visible. Ej: `2`, `6ex2`, `7ex2`, `6ex2ex6` |
| base_incubator_id | Integer | ID numérico de la incubadora original (1-20) |
| current_weight_g | Numeric(10,2) | Peso de ovas en esta unidad al momento de creación o partición |
| parent_unit_id | BigInteger FK→incubator_units.id nullable | Si viene de partición, referencia a la unidad madre |
| status | String(20) | `active` / `hatched` |
| target_pond_id | BigInteger FK→ponds.id nullable | Estanque Hatchery donde eclosionó |
| hatched_at | Timestamp | Fecha y hora de eclosión |
| created_at | Timestamp | |
| updated_at | Timestamp | |

### 2.10 Tabla `incubator_monitoring_logs`
Registros de seguimiento periódico por incubadora.

| Campo | Tipo | Descripción |
|---|---|---|
| id | BigInteger PK | |
| incubator_unit_id | BigInteger FK→incubator_units.id | |
| log_datetime | Timestamp | Fecha y hora del monitoreo |
| size_mm | Numeric(5,2) | Tamaño en mm |
| viability_pct | Numeric(5,2) | Viabilidad 0–100 |
| observation | Text | Observaciones libres |
| created_at | Timestamp | |

### 2.11 Tabla `incubator_split_events`
Registra cada partición de una incubadora en N sub-unidades.

| Campo | Tipo | Descripción |
|---|---|---|
| id | BigInteger PK | |
| source_unit_id | BigInteger FK→incubator_units.id | Unidad que se particiona |
| split_datetime | Timestamp | Cuándo ocurrió la partición |
| notes | Text | Observaciones opcionales |
| created_at | Timestamp | |

---

## 3. Reglas de negocio

### 3.1 Ciclo de vida de una selección

```
[Pez normal] → [Seleccionado] → (30 días o cierre manual)
                                    ↓
                        ¿Hubo fármacos?
                        Sí → internal_id += "_R" → aparece en Tabla B
                        No → vuelve a estado normal (sin cambios en internal_id)
```

- **`status = active`**: el pez está en período de selección activo.
- **`status = completed`**: la selección terminó por vencimiento de 30 días (cierre lazy al cargar la vista).
- **`status = failed`**: el operador marcó "Fallido" manualmente antes del vencimiento.
- **`status = spawned`**: la selección terminó por desove exitoso (solo hembras).

### 3.2 Lógica del sufijo `_R`

- Al cerrar una selección con `status = completed` o `status = spawned`, el sistema verifica si existen registros en `reproductor_drug_logs` para esa `selection_id`.
- Si existen registros de fármacos → se modifica `fish.internal_id` agregando el sufijo `_R` (ej: `ABC123` → `ABC123_R`).
- Si ya tiene sufijo `_R` (reselección), no se duplica.
- Si no hubo fármacos → `fish.internal_id` no se modifica; el pez vuelve a estado normal.
- Si `status = failed` → nunca se agrega `_R`, independientemente de los fármacos registrados durante esa selección.

> **Nota:** El sufijo `_R` NO es un estado de "selección activa". Un pez `_R` puede estar actualmente seleccionado (Tabla A) o no. Un pez sin `_R` también puede estar seleccionado. El sufijo solo indica que el pez tuvo fármacos en alguna selección previa.

### 3.3 Definición de "Reproductor" (Tabla B — candidatos)

Un pez aparece en la Tabla B si:
- Su `internal_id` termina en `_R`.
- `fish.state = 'alive'`.
- No tiene ninguna selección `status = active` en este momento.

La Tabla B es una **ayuda de selección**: muestra los peces que ya tienen historial de uso de fármacos y por tanto no se "manchan" al usar fármacos nuevamente. Cualquier pez (con o sin `_R`) puede entrar en selección desde la vista fish; la Tabla B simplemente prioriza los candidatos habituales.

### 3.4 Selección múltiple

Un pez puede seleccionarse múltiples veces a lo largo de su vida. Cada selección genera un nuevo registro en `reproductor_selections`. No puede haber dos selecciones `active` para el mismo pez simultáneamente.

### 3.5 Expiración automática

Se usa **Opción A (lazy)**: al cargar la vista principal `/views/ui/reproduccion`, el sistema evalúa todas las selecciones `active` cuyo `end_date < hoy`, aplica la lógica del sufijo `_R` y cambia su `status` a `completed` antes de renderizar la tabla.

La columna **"Días desde selección"** en la Tabla A permite al operador ver en qué punto está cada pez dentro de su período de 30 días.

---

## 4. Rutas propuestas

| Método | Ruta | Descripción |
|---|---|---|
| GET | `/views/ui/reproduccion` | Vista principal del módulo |
| POST | `/api/reproduccion/selecciones` | Crear nueva selección (desde vista fish o tabla reproductores) |
| POST | `/api/reproduccion/selecciones/{id}/fail` | Marcar selección como fallida |
| POST | `/api/reproduccion/selecciones/{id}/drugs` | Agregar registro de fármaco |
| POST | `/api/reproduccion/selecciones/{id}/monitoring` | Agregar monitoreo reproductivo |
| GET | `/views/ui/reproduccion/desove/{selection_id}` | Vista propia del proceso de desove |
| POST | `/views/ui/reproduccion/desove/{selection_id}` | Guardado parcial o final del formulario de desove |
| POST | `/api/reproduccion/desove/{spawning_id}/complete` | Cerrar el proceso 2 y abrir proceso 3 (incubadoras) |
| GET | `/views/ui/reproduccion/incubadoras/{batch_id}` | Vista propia del proceso 3 |
| POST | `/api/reproduccion/incubadoras/{batch_id}/lot` | Crear/actualizar nombre y código del lote |
| POST | `/api/reproduccion/incubadoras/{batch_id}/monitoring` | Guardar monitoreo de una ronda completa |
| POST | `/api/reproduccion/incubadoras/{unit_id}/split` | Registrar partición de una incubadora |
| POST | `/api/reproduccion/incubadoras/{unit_id}/hatch` | Cerrar una incubadora por eclosión |
| POST | `/api/reproduccion/incubadoras/{batch_id}/complete` | Cerrar proceso 3 cuando todas las incubadoras hayan eclosionado |

---

## 5. Vista principal — `/views/ui/reproduccion`

### 5.1 Tabla A — Seleccionados activos

Muestra todos los peces con selección `status = active`.

```
Pit-Tag  | Especie   | Lote    | Sexo | Días desde selección | Acciones
---------|-----------|---------|------|----------------------|----------
ABC123   | Oscietra  | Fátima  | H    | Día 12 / 30          | [Ver] [Desove] [Fallido]
CBA321_R | Oscietra  | Iberia  | M    | Día 23 / 30          | [Ver]         [Fallido]
```

- **Días desde selección**: calculado como `hoy - start_date`, mostrado como "Día X / 30". Se puede acompañar de una barra de progreso visual.
- **[Ver]**: despliega el detalle inline del pez (sub-tablas de fármacos y monitoreo) **dentro de la misma tabla**, en una fila expandida justo debajo del pez.
- **[Desove]** (solo hembras): navega a la vista de desove `/views/ui/reproduccion/desove/{selection_id}`.
- **[Fallido]**: abre un modal de confirmación + nota opcional, cierra selección con `status = failed`.
- Los machos no tienen botón de acción adicional además de [Ver] y [Fallido]. La acción de "Usar" queda dentro de la vista de desove (v1.1).

### 5.2 Detalle inline (expandido al presionar Ver — Tabla A)

Se inserta una fila adicional bajo el pez expandido con dos sub-tablas:

**Sub-tabla 1 — Fármacos usados**

```
[Formulario inline]  Fecha | Hora | Fármaco | Dosis | Temp.°C | [Guardar]
─────────────────────────────────────────────────────────────────────────
2026-05-10          | 09:30 | Ovaprim  | 0.5ml/kg | 12.5°C
2026-04-28          | 14:00 | Ovaprim  | 0.5ml/kg | 11.8°C
```

- Primera fila = formulario de ingreso. Al guardar se recarga la vista manteniendo el detalle del mismo pez abierto.
- Registros siguientes ordenados del más reciente al más antiguo.

**Sub-tabla 2 — Monitoreo reproductivo**

Para machos:
```
[Formulario inline]  Fecha | Densidad           | Motilidad          | Tiempo             | [Guardar]
─────────────────────────────────────────────────────────────────────────────────────────────────
2026-05-10          | Alta               | Media              | Baja               |
```

- Densidad, Motilidad, Tiempo: selector `Baja / Media / Alta`.

Para hembras:
```
[Formulario inline]  Fecha | IP (0-100) | Observación (máx 100 car.)      | [Guardar]
──────────────────────────────────────────────────────────────────────────────────────
2026-05-10          | 72         | Folículos bien formados         |
```

### 5.3 Tabla B — Reproductores (candidatos)

Muestra peces con `internal_id` terminado en `_R` y `state = 'alive'` y sin selección `active`.

```
Pit-Tag   | Especie  | Lote      | Sexo | Estanque actual      | Acciones
----------|----------|-----------|------|----------------------|--------------------
ZYX456_R  | Oscietra | Guadalupe | M    | NP1 - Nor Poniente 1 | [Ver] [Seleccionar]
XYZ654_R  | Oscietra | Iberia    | H    | SO5 - Sur Oriente 5  | [Ver] [Seleccionar]
```

- **[Ver]**: navega a la vista fish del pez (`/views/ui/fish/{fish_id}`).
- **[Seleccionar]**: crea nueva selección directamente sin pasar por la vista fish. Muestra confirmación inline o modal mínimo.

---

## 6. Integración con vista Fish

En la vista individual de un pez (`/views/ui/fish/{fish_id}`), se agregará un botón **"Seleccionar como reproductor"** en el área de acciones del pez.

- Si el pez ya tiene una selección `active`, el botón mostrará **"En reproducción"** (deshabilitado).
- Si el pez **no tiene sexo registrado**, al presionar el botón se muestra un formulario/modal que **fuerza el ingreso del sexo** antes de poder confirmar la selección. El sexo queda guardado en `fish.sex` y también en `reproductor_selections.sex_at_selection`.
- Si el pez tiene sexo registrado, al presionar se abre un modal de confirmación mostrando pit-tag y sexo, y se crea la selección directamente.

---

## 7. Entrada al sidebar

Nuevo ítem en `_sidebar.html` después del ítem de Alimentación:

```html
<a class="sidebar-item" href="/views/ui/reproduccion" data-match="/views/ui/reproduccion" 
   aria-label="Reproducción" title="Reproducción">
  <!-- ícono: p.ej. SVG de célula o pez -->
  <span class="item-label">Reproducción</span>
</a>
```

---

## 8. Vista de Desove — especificación v1.1

La vista de desove se activará al presionar **[Desove]** en una hembra con selección activa.

### 8.1 Naturaleza del proceso

El desove es el **Proceso 2** de reproducción. La selección es el **Proceso 1**. Una vez completado el Proceso 2, comienza el **Proceso 3** de monitoreo de incubadoras, que se detallará en la especificación v1.2.

- El desove queda siempre relacionado a **la hembra seleccionada**.
- El proceso puede guardarse parcialmente y retomarse más tarde.
- La vista de desove tiene acceso propio por URL y no depende de la vista principal.

### 8.2 Flujo general de la vista

1. El operador entra a `/views/ui/reproduccion/desove/{selection_id}`.
2. Se muestra el formulario del proceso con estado `draft` si aún no está finalizado.
3. El operador puede guardar parcial o totalmente la información.
4. Cuando todos los formularios obligatorios estén completos, se marca el proceso como `completed`.
5. Al completar el proceso:
  - la selección activa de la hembra se cierra;
  - los reproductores involucrados salen de selección;
  - comienza el monitoreo de incubadoras del Proceso 3.

### 8.3 Registro ambiental

Campos requeridos:
- Fecha
- Hora de ingreso en UI
- Temperatura

### 8.4 Registro de anestesia

Se debe registrar la preparación de anestesia con:
- fármaco
- dilución

Este registro se agrega también al historial de fármacos de todos los reproductores involucrados en el desove, tanto la hembra como los machos seleccionados.

### 8.5 Registro de machos

- La hembra puede vincularse con uno o más machos.
- Los machos deben elegirse entre los peces que estén actualmente seleccionados.
- Se permite más de un macho por hembra.

### 8.6 Registro de ovas

Campos requeridos:
- Peso total de ovas en gramos
- Recuento de ovas en 5 gr
- Diámetro en mm, rango orientativo entre 2,5 y 5 mm
- Viabilidad entre 0 y 100 %

Campo Calculado:
- Numero de ovas: recuento/5*peso

Notas:
- El recuento de ovas en 5 gr no se restringe artificialmente a un valor fijo; puede variar.

### 8.7 Registro de incubadoras

- `id_incubadora` entre 1 y 20.
- Peso asignado por incubadora.
- La suma del peso distribuido debe ser igual al peso total de las ovas.

### 8.8 Registro de recuperación de peces

Al finalizar el proceso se debe registrar, para cada reproductor involucrado, si:
- la recuperación fue exitosa, o
- se decidió sacrificar al pez.

### 8.9 Cierre del desove

El proceso 2 se considera cerrado cuando se completa el formulario final del desove.

- En ese momento los reproductores salen de selección.
- El desove no crea ningún registro en PlantaAPP.
- El formulario puede quedar en estado parcial y terminarse más tarde sin perder el acceso propio.

### 8.10 Estados de la vista

- `draft`: el desove fue abierto pero incompleto.
- `partial`: hay datos guardados, pero faltan formularios obligatorios.
- `completed`: el desove quedó cerrado y habilita el Proceso 3.
- `cancelled`: el operador descartó el proceso.

---

## 9. Vista de Monitoreo de Incubadoras — especificación v1.2

### 9.1 Naturaleza del proceso

El monitoreo de incubadoras es el **Proceso 3** de reproducción. Se inicia automáticamente al cerrarse el Proceso 2 (desove). La vista tiene acceso propio por URL: `/views/ui/reproduccion/incubadoras/{batch_id}`.

### 9.2 Lote de peces

- Al entrar al Proceso 3 se debe crear un nuevo lote para los peces que eclosionarán.
- El lote requiere un **nombre** y un **código interno** de máximo 3 caracteres.
- El lote puede asignarse en cualquier momento durante el monitoreo, pero **no es posible cerrar el Proceso 3 sin haberlo definido**.
- El lote queda en estado `awaiting_first_count` hasta que el operador realice el primer recuento de peces (ver §9.6).

### 9.3 Tarjetas KPI

La parte superior de la vista muestra dos tarjetas de indicadores calculados en tiempo real:

**KPI 1 — Viabilidad total**
$$\text{Viabilidad} = \frac{\sum(\text{viabilidad}_i \times \text{peso}_i)}{\sum \text{peso}_i}$$
Promedio ponderado por peso de la última viabilidad registrada en cada incubadora activa.

**KPI 2 — Ovas viables estimadas**
$$\text{Ovas viables} = \left\lfloor \frac{\text{recuento de ovas en 5g}}{5} \times \text{peso total ovas (g)} \times \frac{\text{viabilidad}}{100} \right\rfloor$$
Estimación basada en el recuento del desove y la viabilidad calculada en KPI 1.

### 9.4 Vista de monitoreo rutinario

La tabla de monitoreo muestra las incubadoras activas del proceso. El formulario de ingreso permite registrar **todas las incubadoras a la vez** en una sola sesión.

**Formulario de ingreso (una fila por incubadora activa):**

```
ID Incubadora | Fecha-hora        | Tamaño (mm) | Viabilidad (%) | Observaciones     | [Guardar todo]
2             | 2026-05-14 09:00  | 3.2         | 85             | Sin anomalías     |
6ex2          | 2026-05-14 09:00  | 3.1         | 82             |                   |
7ex2          | 2026-05-14 09:00  | 3.0         | 79             | Leve turbidez     |
```

**Histórico (debajo del formulario):**
- Ordenado de más reciente a más antiguo.
- Dentro de cada ronda, las incubadoras se muestran por `display_id` ascendente.

### 9.5 Partición de incubadoras

Una incubadora activa puede particionarse en N sub-unidades. Al particionar:
- El usuario indica cuántas y cuáles incubadoras recibirán las sub-partes, junto con el peso asignado a cada una.
- El ID de la unidad resultante se compone como: `{id_destino}ex{id_origen}`. Si el id de origen ya es compuesto, se concatena otro sufijo (ej: `6ex2ex6`).
- La unidad origen queda con `status = split` (ya no aparece en el formulario de monitoreo) y continúan las unidades resultantes.
- La suma de pesos de las sub-unidades debe ser igual al peso actual de la unidad origen.
- El sistema no impide particiones sucesivas; la trazabilidad se mantiene por la cadena de sufijos.

### 9.6 Eclosión de una incubadora

El monitoreo de cada incubadora individualmente termina con su **eclosión**.

Al registrar la eclosión:
- El usuario selecciona el estanque de destino, que debe ser de tipo **Hatchery:Incubación** o **Hatchery:Sala 2**.
- La unidad pasa a `status = hatched`.
- Los peces eclosionados ingresan al estanque asignado bajo el lote del Proceso 3.

> **Observación:** En esta etapa los peces son muy delicados para manipular. El lote queda sin recuento de peces, en estado `awaiting_first_count`. El primer recuento se realizará cuando los peces estén en condiciones de ser manejados (fuera del alcance del Proceso 3; se resolverá como acción en la vista del lote o del estanque).

### 9.7 Cierre del Proceso 3

El Proceso 3 (`incubator_batch.status = completed`) puede cerrarse cuando:
1. **Todas** las incubadoras del batch tienen `status = hatched`.
2. `lot_name` y `lot_code` están definidos.

Al cerrarse, el Proceso 3 queda archivado y accesible por URL pero ya no se muestran formularios de edición.

### 9.8 Estados del proceso 3

| Estado | Descripción |
|---|---|
| `monitoring` | El proceso está activo. Hay incubadoras en seguimiento. |
| `awaiting_first_count` | Todas las incubadoras eclosionaron, esperando primer recuento de peces. |
| `completed` | Proceso cerrado. Lote definido. |

---

## 10. Decisiones de diseño — resueltas

| # | Pregunta | Decisión |
|---|---|---|
| P1 | ¿Sufijo `_R` modifica `internal_id` o campo separado? | El sufijo `_R` sí modifica `internal_id`. La selección activa es **independiente** del sufijo: un pez sin `_R` puede estar seleccionado. |
| P2 | ¿Sexo obligatorio para selección? | Sí. Si el pez no tiene sexo, se fuerza el ingreso del sexo en el mismo flujo de selección antes de confirmar. |
| P3 | ¿Botón "Usar" en machos? | **Eliminado** de la vista principal. La acción de uso de semen de machos se gestiona dentro de la vista de desove (v1.1). |
| P4 | ¿Expiración lazy aceptable? | Sí. La vista muestra columna "Día X / 30" como indicador visual del estado del período. |
| P5 | ¿Historial de selecciones? | No es necesario. |
| P6 | ¿Peces sin `_R` en Tabla B? | No. La Tabla B es exclusiva para peces con `_R`. Cualquier pez puede ser seleccionado desde la vista fish; la Tabla B solo muestra candidatos habituales (con historial de fármacos). |
| P7 | ¿Vínculo con PlantaAPP? | No hay integración. |

---

## 11. Fases de implementación sugeridas

| Fase | Alcance |
|---|---|
| F1 | Modelos de datos (`reproductor_selections`, `reproductor_drug_logs`, `reproductor_monitoring_logs`, `reproductor_spawning_events`, `reproductor_spawning_males`, `reproductor_spawning_incubators`, `reproductor_recovery_reports`, `incubator_batches`, `incubator_units`, `incubator_monitoring_logs`, `incubator_split_events`) + migraciones Alembic |
| F2 | Sidebar + ruta `/views/ui/reproduccion` + vista principal (Tabla A y Tabla B, solo lectura + lógica lazy de expiración) |
| F3 | API endpoints de selección (`POST /api/reproduccion/selecciones`), fármacos y monitoreo; formularios inline en Tabla A |
| F4 | Integración con vista Fish: botón "Seleccionar como reproductor" + modal con forzado de sexo si aplica |
| F5 | Vista de Desove (especificación v1.1) |
| F6 | Vista de Monitoreo de Incubadoras: KPIs, tabla de monitoreo, partición, eclosión y cierre (especificación v1.2) |
