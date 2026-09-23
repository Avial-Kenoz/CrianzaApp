# CrianzaApp — Guía para Claude

App **FastAPI** (Python, español) de crianza de esturión: estanques, peces (PIT),
muestreos, reproducción, **sexado offline**, **calidad de agua** y **molienda/ensilaje**.
Puerto **8002**.

## Base de datos — LEER ANTES DE CUALQUIER SCRIPT ⚠️

Esta app usa **`fastapp_etapa1`** (usuario `fastapp_user`):
`postgresql://fastapp_user:fastapp_pass@localhost/fastapp_etapa1`

**Trampa conocida (causa confusión recurrente):** `app/db/session.py` hace
`load_dotenv()` —que **NO** pisa las variables ya definidas en el SO— y luego
`os.getenv("DATABASE_URL")`. El **SO tiene `DATABASE_URL=produccion_faena`** (la
base de PlantaApp), que **gana sobre el `.env`** de CrianzaApp. Por eso, al correr
scripts con el venv de CrianzaApp **sin forzar** la URL, terminas en
`produccion_faena` (donde las tablas de crianza ni existen).

- **Scripts / diagnóstico** → forzar la URL correcta:
  `DATABASE_URL=postgresql://fastapp_user:fastapp_pass@localhost/fastapp_etapa1 .venv/Scripts/python.exe ...`
- **Alembic** → `alembic.ini` ya fija `sqlalchemy.url = ...fastapp_etapa1` (no usa
  el env), así que `alembic upgrade head` apunta bien.
- **Servidor en producción** → `start_server.cmd` hace `set DATABASE_URL=%CRIANZA_DB_URL%`
  (fastapp_etapa1) antes de lanzar uvicorn; corre bien.

Bases del entorno que **NO** son de CrianzaApp: PlantaApp → `produccion_faena`;
AdminDashboard → lee las tres (produccion_faena + fastapp_etapa1 + dashboard_db);
CasAmaliApp → `casamaliapp`.

## Cómo correr y verificar

- Arranque: `uvicorn app.main:app --host 0.0.0.0 --port 8002` desde la raíz.
- Migraciones: Alembic (`alembic upgrade head`). Ojo con **múltiples heads** si dos
  ramas parten del mismo `down_revision`.
- El `.venv` **no** tiene pytest/httpx (TestClient no usable): verificar fabricando
  un Starlette `Request` y llamando a los handlers directo, con `PYTHONIOENCODING=utf-8`.
  Para APIs completas conviene `start_dev.cmd` (**puerto 8003**, no toca producción) y
  golpear los endpoints con `urllib`. ⚠️ Ojo: dev y producción comparten la **misma BD**,
  así que hay que limpiar los datos de prueba al terminar.


### Dependencias: usa el .venv de ESTA app ⚠️
Hay **cuatro entornos virtuales separados** en el servidor (PlantaAPP, CrianzaApp,
AdminDashboard, CasAmaliApp) y divergieron a propósito: CrianzaApp ya está en
Starlette 1.0 / FastAPI 0.136 mientras las otras siguen en 0.38 / 0.114. No hay que
unificarlos.

La trampa: la terminal suele venir con **el venv de PlantaApp activado**
(`VIRTUAL_ENV`), así que un `pip install` pelado instala ahí sin avisar, estés en
la carpeta que estés. Siempre explícito:

```
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m pytest tests/
```

Correr los tests con el `python` del PATH da falsos verdes: la app pasa en un
entorno y revienta en el suyo. `start_server.cmd` ahora verifica que cada app
importe antes de lanzar uvicorn, justamente por esto.

## Biomasa: qué campo es el bueno ⚠️

Coexisten dos cachés en `ponds` y solo uno refleja el criterio vigente:

- **`biomass_projected_kg`** ← **este**. Suma por estanque de
  `_project_biomass_from_checkpoint(hoy)`: proyección por `(estanque, lote)` desde
  `biomass_checkpoints`, ancla en el mejor checkpoint ≤ hoy, ajustada por los movimientos
  posteriores. Es el mismo número que el KPI "Biomasa" de `/ui/ponds` (`stat_biomass`).
- **`biomass_current`** ← caché del criterio anterior a la unificación de ago-2026. Se
  ajusta solo de forma incremental (`_adjust_biomass_on_movement`) y ya **no** se rehace en
  cada movimiento. **No usarlo para reportes.** En sep-2026 daba +9,4% sobre el total real
  (192.854 vs 176.339 kg) y hasta ±4,4 t por estanque; AdminDashboard lo sumaba y por eso
  mostraba una biomasa que no cuadraba con esta app.

El caché lo escribe `refresh_projected_biomass_cache()`, que corre dentro de
`rebuild_all_pond_runtime_cache` (job `pond_cache_scheduler`, Lun-Vie 8-18 cada 2h) y al
**cerrar una sesión de muestreo** (cuesta ~0,3 s). Junto con los kg persiste
`biomass_projected_at` (frescura) y `biomass_projected_lots_missing` (lotes con peces y sin
checkpoint: valen 0 kg en la proyección, así que sin ese contador un estanque sin muestreo
se ve igual que uno vacío).

**Es contrato con AdminDashboard**, que lo lee con un `SELECT` para no duplicar el
algoritmo: si cambia el criterio de biomasa, estas columnas tienen que seguir siendo la
salida oficial. Tras una carga masiva o un `alembic upgrade`, poblar con
`scripts/rebuild_pond_runtime_cache.py`.

## Reportes mensuales: una sola fuente para HTML y JSON

`build_reports_payload(month, report, db)` arma los cuatro reportes de `/views/ui/reports`
(existencia y alimento, por lote y por estanque). Encima hay dos envoltorios delgados:
`ui_reports` (HTML) y **`/views/api/reports` (JSON), que consume AdminDashboard**.
Al tocar un reporte, cambiar el payload —no el handler— o el dashboard queda viendo otra
cosa. El JSON devuelve solo el bloque del reporte pedido.

## PWAs offline (mismo patrón, tres veces)

`app/field_client/` → `/captura` (O2) · `app/sexado_client/` → `/sexado_offline` ·
`app/silage_client/` → `/ensilaje` (molienda). Todas: montadas en `main.py` con
**`NoCacheStatic`** (ver abajo), IndexedDB con stores `meta`+`queue`, y una API
`/api/<dominio>/v1` con `GET /bootstrap` + `POST` en lote **idempotente por `client_uuid`**.
Los `sw.js` precachean `icon-192/512.png`: si faltan, la instalación del service worker
falla y se cae el modo offline completo.

### Desplegar un cambio de PWA ⚠️

Los tablets se quedan con código viejo **sin ninguna señal visible**, y diagnosticarlo
a ciegas cuesta horas (pasó el 19/08/2026: un equipo corrió código de días atrás pese a
reinstalar la app). Tres defensas, y las tres tienen que moverse juntas:

1. **`NoCacheStatic`** en `main.py` manda `Cache-Control: no-cache, must-revalidate`
   sobre el shell. Sin eso el navegador aplica caché heurístico y **no revalida nunca**.
2. **`sexado_client` es network-first con timeout de 2,5 s** (`sw.js`): con conexión gana
   el servidor; si la red no contesta —tablet asociado al Wi-Fi pero sin ruta— se sirve el
   caché para que la app abra igual. El fallback usa `ignoreSearch` porque la página pide
   `app.js?v=N` y el precache guarda `app.js` a secas.
3. **Al tocar el cliente, subir los tres a la vez**: `APP_VERSION` en `app.js`, `CACHE` en
   `sw.js` y el `?v=` del `<script>` en `index.html`. La versión se muestra en la barra
   superior del tablet: es la forma de saber qué código corre un equipo sin adivinar.

Con `sw.js` cache-first (el patrón viejo, que aún usan `/captura` y `/ensilaje`) un deploy
tarda **dos recargas** en llegar: la primera instala el SW nuevo, la segunda recién sirve
el `app.js` nuevo.

## Sexado offline: conflictos y ciclo de sesión

La sesión (`sexado_offline_sessions`) bloquea 1 estanque fuente + hasta 10 destinos
mientras está en `active` o `reconciling`. Al sincronizar, lo que no se puede aplicar
queda como `pending_review` en `sexing_offline_operations`.

- **El tablet resuelve sus propios conflictos** (`GET /conflicts`, `POST /conflicts/{id}/resolve`
  con `retry` | `dismiss` | `supervisor`). Al quedar cero, `close_session_if_clean` cierra
  la sesión y libera el estanque sin pasar por el escritorio.
- **`reason_code`** guarda el motivo como código estable (el `result_message` es texto para
  humanos y cambia). `conflict_severity()` lo traduce a `duplicate` / `data` / `traceability`
  = qué se pierde si el operador descarta. El `duplicate` **no** se congela al sincronizar:
  se calcula al listar comparando contra el pez tal como está hoy.
- **`finalize_session` mira los pendientes reales de la sesión**, no los del batch: un
  segundo sync sin contingencias nuevas no puede cerrar una sesión con pendientes vivas.
- **La bandeja lista toda sesión con pendientes**, aunque esté cerrada; si no, una operación
  se vuelve invisible y solo se toca por endpoint directo.
- **Sesión abandonada**: si el tablet suelta su token, los conflictos solo se alcanzan desde
  el escritorio. Por eso la pantalla de setup ofrece *retomar* las sesiones que siguen
  bloqueando (`GET /sessions` → `POST /resume`).

## Módulo Molienda y Ensilaje (registro PC 03.2)

Ver `especificacion_molienda_ensilaje_v1.md`. **Completo** (PR1–PR6): modelos, motor
(`app/services/silage.py`), API (`app/api/silage.py`), PWA (`app/silage_client/` → `/ensilaje`)
y vistas web (`app/api/silage_views.py` → `/views/ui/ensilaje/{registro,dia,inspeccion,
tambores,acido,despachos,conciliacion}`, con CSS y pestañas en los parciales
`_ensilaje_style.html` / `_ensilaje_tabs.html`).
Tres invariantes que no son evidentes leyendo el código suelto:

- **La captura es aditiva**: la unidad es la *carga*, no el día. Varias cargas por fecha
  suman; `log_date` está indexado pero **no** es único. Corregir = anular (`voided_at`) y
  registrar de nuevo, nunca reenviar.
- **El criterio de aceptación es el pH (<4), no la dosis de ácido.** El rango
  0,20–0,30 lts/kg es solo un chequeo de verosimilitud y jamás bloquea: si se presentara
  como error, el operador ajustaría los litros digitados y corrompería el inventario.
- **Los parámetros salen del procedimiento escrito del PC 03.2**, no de estimaciones:
  dosis nominal 1 lt por 4 kg (=0,25 lts/kg) y cierre del tambor a **4/5** (`drum_close_fill_ratio`).
  Antes de cambiarlos, revisar el texto "Información Importante" del formato.

Además: el **folio** lo asigna el servidor (secuencia `silage_folio_seq`), nunca la PWA;
un índice único parcial (`uniq_silage_drum_open`) garantiza **un solo tambor abierto**; y la
PWA abre con una **ventana de seguridad bloqueante** (se trabaja con ácido) en cada apertura.

⚠️ La ventana de seguridad y el panel de **primeros auxilios** viven en un `<script>` dentro de
`app/silage_client/index.html`, **nunca en `app.js`**: cuando estuvieron en el bundle, un error
ahí dejó el botón "acepto" muerto y al operador encerrado. Es contenido de emergencia y no
puede depender de que cargue el resto de la app. Al tocar la PWA hay que **subir `CACHE` en
`sw.js`** o la tablet sigue sirviendo la versión anterior.
