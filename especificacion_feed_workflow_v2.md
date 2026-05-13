# Especificación Módulo Alimentación — Rediseño UX/Flujo (v2)

Fecha: 2026-05-11  
Estado: **Aprobado para implementación** — preguntas respondidas por el usuario  
Objetivo: Rediseñar el flujo de trabajo en `/views/ui/feed` para separar claramente la **Confirmación de ejecución** (vista principal) de la **Creación de programa** (acción lateral), con navegación intuitiva y tabla congelada.

---

## 1. Contexto actual (qué existe hoy)

- `/views/ui/feed` → `feed_inventory_main.html` → muestra matriz estanque × tipo-alimento con inputs y botones "Guardar" / "Confirmar" por fila.
- Los botones "Confirmar" son cosméticos (sin endpoint de backend).
- No hay separación visual entre "planificar" y "confirmar".
- No hay vista dedicada a crear un programa nuevo.
- El spec v1 (`especificacion_sistema_alimentacion_v1.md`) define el modelo de datos y reglas de negocio, pero la implementación UI quedó incompleta.

---

## 2. Flujo de trabajo — Especificación consolidada

### 2.1 Vista principal — Confirmación de ejecución (`/views/ui/feed`)

**Entrada:** El usuario ingresa a `/views/ui/feed`.

**Caso A — Existe programa vigente (status = open):**

- Se muestra la tabla con todos los estanques padre que tienen **lotes activos** (estanques sin peces no se muestran).
- Cada celda contiene:
  - **Input X** (editable): sacos que el usuario confirma en esta sesión. Inicializado en `0` en cada nueva sesión.
  - **Texto fijo "de Y de Z"**: Y = sacos ya confirmados acumulados en el programa; Z = sacos planificados totales.
  - Lectura completa por celda: *"Confirmo [X] sacos, van [Y] sacos de [Z] programados"*.
- **Código de color por fila** (indicador de avance del programa):
  - 🟩 Verde: suma ejecutada acumulada (Y) ≥ Z planificado (fila completa).
  - 🟨 Amarillo: Y > 0 pero Y < Z (confirmación parcial).
  - ⬜ Sin color: Y = 0, sin confirmaciones aún.
- **Fila 1 fija (encabezado):** nombres de tipos de alimento.
- **Fila 2 fija (stock disponible):** stock disponible en inventario por tipo de alimento (se actualiza tras cada confirmación).
- **Columna 1 fija:** nombre del estanque (y unidad de cultivo).
- El usuario puede confirmar **por fila** (botón "Confirmar fila", sin recargar página) o el **formulario completo** (botón "Confirmar todo").
- **Consumo fuera de programa ("X de 0"):** permitido si `stock_disponible[tipo] >= X`. Se muestra un **banner de advertencia** en la parte superior de la tabla mientras existan confirmaciones fuera de programa en el programa vigente. El banner persiste hasta que se cree un nuevo programa.
- Validación en línea (JavaScript): si X > stock disponible, se bloquea el botón de confirmar de esa fila y se muestra mensaje de error.

**Caso B — No existe programa vigente:**

- La tabla se muestra vacía (sin filas de estanques, sin columnas de alimentos).
- Se despliega mensaje prominente: **"No hay programas de alimentación cargados"**.
- El panel lateral de acciones sigue visible para permitir crear un programa.

### 2.2 Panel lateral de acciones

- Tarjeta: **"Crear programa"** → navega a `/views/ui/feed/new-program`.
- Tarjeta: **"Historial de programas"** → navega a `/views/ui/feed/history`.
- (Otras tarjetas existentes se mantienen: Inventario, Ajustes.)

### 2.3 Vista de creación de programa (`/views/ui/feed/new-program`)

- Página independiente (URL propia), de construcción similar a la vista de confirmación.
- Nombre del programa: **generado automáticamente** al guardar, formato `PROG-YYYYMMDD` (fecha de creación).
- Vigencia por defecto: **30 días** desde la fecha de creación (`week_start_date` = hoy, `week_end_date` = hoy + 30 días).
- Si existe un programa abierto, al guardar el nuevo se cierra automáticamente el anterior: todos los sacos planificados no ejecutados vuelven a estado `disponible`.
- Cada celda es un **input numérico** para ingresar `Z` (sacos planificados).
- **Biomasa y lotes de referencia:** accesibles mediante **tooltip** al pasar el mouse por el nombre del estanque en la columna 1 (no ocupa espacio en tabla).
- **Fila 1 fija:** nombres de tipos de alimento.
- **Fila 2 fija:** stock disponible por tipo (guía al usuario sobre cuánto puede planificar).
- **Columna 1 fija:** nombre del estanque.
- Guardar **por fila**: guarda solo esa fila sin recargar la página.
- Guardar **formulario completo**: guarda todas las filas; si una celda está vacía o en 0, se guarda como `0` (sin sacos reservados para ese par estanque-tipo).

### 2.4 Vista de historial de programas (`/views/ui/feed/history`)

- Página independiente, accesible desde el panel lateral.
- Buscador por **rango de fecha** (fecha inicio del programa).
- Lista de programas cerrados con: código, fecha inicio, fecha cierre, razón de cierre.
- Acción **"Reprogramar"** por cada programa en el historial: carga la vista `/views/ui/feed/new-program` con los valores `Z` del programa seleccionado precopiados en el formulario.
- El reprogramar no crea el programa automáticamente; el usuario revisa los valores y guarda.

---

## 3. Cambios técnicos requeridos

### 3.1 Backend (`feed_endpoints.py`)

| Endpoint | Tipo | Acción |
|---|---|---|
| `GET /ui/feed` | Modificar | Leer `feed_execution_events` para derivar Y (ejecutado acumulado) por línea; pasar X=0 siempre; filtrar estanques sin lotes activos |
| `POST /ui/feed/confirm-row` | **Nuevo** | Confirma valores X de una fila (pond_id); valida stock; registra `feed_execution_events`; devuelve JSON (sin reload) |
| `POST /ui/feed/confirm-all` | **Nuevo** | Confirma todos los valores X del formulario completo; mismas validaciones |
| `GET /ui/feed/new-program` | **Nuevo** | Vista vacía de creación de programa; muestra estanques con lotes activos |
| `POST /ui/feed/new-program` | **Nuevo** | Guarda el programa completo; cierra el anterior si existe; genera nombre automático `PROG-YYYYMMDD` |
| `POST /ui/feed/new-program/save-row` | **Nuevo** | Guarda una fila del borrador del programa sin recargar página |
| `GET /ui/feed/history` | **Nuevo** | Lista de programas cerrados con filtro por rango de fecha |
| `GET /ui/feed/history/{program_id}/reprogramar` | **Nuevo** | Carga vista `new-program` con valores Z precopiados del programa seleccionado |

### 3.2 Lógica de cierre de programa

Al recibir `POST /ui/feed/new-program`:
1. Buscar programa con `status = open`.
2. Para cada `FeedProgramLine` del programa abierto con `remaining_bags > 0`, crear entrada en `FeedStockLedger` con `movement_type = release` (devuelve sacos a disponible).
3. Marcar programa anterior como `status = closed`, `closed_reason = superseded`.
4. Crear nuevo `FeedProgram` con nombre `PROG-YYYYMMDD`, `week_start_date = hoy`, `week_end_date = hoy + 30 días`.
5. Insertar `FeedProgramLine` por cada par (pond_id, feed_type_id) con valor ingresado ≥ 0; crear `FeedStockLedger` con `movement_type = reserve` para los > 0.

### 3.3 Lógica de confirmación

Al recibir `POST /ui/feed/confirm-row` o `confirm-all`:
1. Para cada celda con X > 0:
   - Si `planned_bags > 0` (consumo planificado): descontar de `remaining_bags` de la línea; crear `FeedExecutionEvent`; crear `FeedStockLedger movement_type = execute`.
   - Si `planned_bags = 0` (consumo fuera de programa): validar `available_bags >= X`; crear `FeedExecutionEvent` marcado como `unplanned = True`; crear `FeedStockLedger movement_type = execute` desde disponible.
2. Si después de la confirmación quedan confirmaciones `unplanned` en el programa abierto, el contexto de la vista debe incluir `has_unplanned = True` para mostrar el banner.

### 3.4 Frontend — templates nuevos/modificados

| Template | Acción |
|---|---|
| `feed_confirm.html` | Renombrar/refactorizar `feed_inventory_main.html`; implementar formato "X de Y de Z" + colores por fila |
| `feed_new_program.html` | **Nuevo**; inputs vacíos + tooltip de biomasa/lotes en columna 1 |
| `feed_history.html` | **Nuevo**; tabla de programas cerrados + filtro por fecha + botón Reprogramar |

CSS requerido para congelamiento:
```css
/* Filas 1 y 2 fijas */
thead tr:nth-child(1) th,
thead tr:nth-child(2) th { position: sticky; top: 0; z-index: 3; background: #fff; }
thead tr:nth-child(2) th { top: <altura-fila-1>px; }

/* Columna 1 fija */
td:first-child, th:first-child { position: sticky; left: 0; z-index: 2; background: #fff; }

/* Intersección fila-1/2 + columna-1 */
thead tr th:first-child { z-index: 4; }
```

### 3.5 Modelo de datos

Sin cambios de esquema. Nuevo campo lógico en `FeedExecutionEvent`:  
- `is_unplanned` (boolean, default False) — para detectar consumos fuera de programa sin JOIN adicional.

---

## 4. Decisiones de diseño resueltas

| # | Pregunta | Decisión |
|---|---|---|
| P1 | Estado visual de confirmación | Input X + texto "de Y de Z"; colores: verde=completo, amarillo=parcial, blanco=sin confirmar |
| P2 | Valor de X al reabrir | X = 0 siempre (Y de Z muestra el acumulado); resuelto por P1 |
| P3 | Cierre de programa | Al crear nuevo programa; programas cerrados no se reabren |
| P4 | Consumo fuera de programa | Banner de advertencia visible mientras existan consumos `unplanned` en el programa vigente |
| P5 | Crear programa: misma página o separada | Página independiente (`/views/ui/feed/new-program`) |
| P6 | Estanques a mostrar | Solo estanques padre con lotes activos; sin peces = no aparece |
| P7 | Guardar por fila vs. completo | Por fila: guarda sin recargar; Completo: guarda todas, celdas vacías = 0 |
| P8 | Historial | Vista aparte (`/views/ui/feed/history`), filtro por fecha, acción "Reprogramar" copia valores |
| P9 | Biomasa/lotes en creación | Tooltip al pasar el mouse por el nombre del estanque |
| P10 | Nombre y vigencia del programa | Automático `PROG-YYYYMMDD`; vigencia 30 días desde creación |

---

## 5. Supuestos del rediseño (validados)

1. No se cambia el modelo de datos, excepto agregar `is_unplanned` a `FeedExecutionEvent` (migration simple).
2. `feed_confirm.html` y `feed_new_program.html` son templates separados pero comparten base CSS.
3. El panel lateral de acciones se mantiene en todas las vistas del módulo.
4. Validaciones de stock se hacen en JS (feedback inmediato) y se repiten en el backend.
5. CSS `position: sticky` es suficiente para el congelamiento (compatible con Chrome/Edge de escritorio).

---

## 6. Orden de implementación

| Fase | Descripción | Dependencias |
|---|---|---|
| 1 | Agregar `is_unplanned` a `FeedExecutionEvent` + migration Alembic | — |
| 2 | Ajustar `GET /ui/feed`: leer ejecutado acumulado (Y) y filtrar estanques con lotes activos | Fase 1 |
| 3 | Refactorizar `feed_inventory_main.html` → `feed_confirm.html`: formato "X de Y de Z", colores por fila, banner unplanned, CSS sticky | Fase 2 |
| 4 | Crear `POST /ui/feed/confirm-row` + JS sin reload + validación stock | Fase 3 |
| 5 | Crear `POST /ui/feed/confirm-all` | Fase 4 |
| 6 | Crear `GET /ui/feed/new-program` + `feed_new_program.html` + tooltip biomasa | — |
| 7 | Crear `POST /ui/feed/new-program` (cierre anterior + guardar nuevo) + `POST /save-row` | Fase 6 |
| 8 | Crear `GET /ui/feed/history` + `feed_history.html` + filtro fecha | — |
| 9 | Crear `GET /ui/feed/history/{id}/reprogramar` | Fases 7 y 8 |
| 10 | Conectar tarjetas del panel lateral a los nuevos endpoints | Fases 3, 6, 8 |
