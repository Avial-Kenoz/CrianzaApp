# Especificación Módulo Mantenimiento de Maquinaria — CrianzaApp (núcleo) + PlantaApp (v1)

Fecha: 2026-09-29
Última actualización: 2026-09-29 (**sistema único**: el encargado de mantenimiento es el mismo para
Crianza y Planta, así que el núcleo vive solo en CrianzaApp y PlantaApp es un satélite que da de alta
sus equipos y ve sus OT a través de una API firmada. Reemplaza la idea anterior de copiar el módulo a
PlantaApp.)
Estado: **Especificación cerrada** — decisiones de §10 resueltas. **PR1 hecho** (2026-09-29);
siguiente: PR2 (OT en la web)
Origen: hoy no hay registro alguno ("un paño sucio, un overall y una caja de herramientas").
Dolor principal: **los tiempos de mantenimiento son muy largos** y nadie sabe dónde se va el tiempo.
Objetivo: registrar fallas en 30 segundos desde terreno, gestionar la orden de trabajo (OT) hasta
su cierre y **medir cada tramo del tiempo** (gestión, contratista, repuesto, ejecución), para
Crianza y Planta **en un solo lugar**, de modo que el encargado no tenga que navegar entre sistemas.

---

## 1. Decisiones de alcance

1. **Un solo sistema, con el núcleo en CrianzaApp.** Las tablas, el bot, la gestión de OT, el parte
   diario y los indicadores viven solo en CrianzaApp. **PlantaApp es un satélite** (§8): da de alta
   y edita **sus** equipos y ve **sus** OT, siempre a través de una API firmada contra CrianzaApp.
   PlantaApp no tiene tablas de mantenimiento.
2. **Dos sitios: Crianza y Planta.** Cada equipo pertenece a un sitio, y las OT e indicadores se
   filtran por sitio. El encargado ve ambos juntos.
3. **Telegram hace solo lo que resuelve bien:** el **aviso de falla** (QR → bot → botones + foto) y
   las **notificaciones**. **Nada de gestión de OT en Telegram.** Es **un solo bot para los dos
   sitios**: el equipo escaneado determina el sitio.
4. **La OT se gestiona en la web de CrianzaApp**: aceptar el aviso, asignar, cambiar de estado,
   cerrar y consultar indicadores.
5. **Los contratistas no usan el sistema.** Reportan al **encargado de mantenimiento**, y es él quien
   registra en la app (llegada, espera de repuesto, término). Los contratistas son un catálogo, no
   usuarios.
6. **El catálogo de activos es de mantenimiento, no contable.** No reemplaza ni replica el módulo de
   activo fijo del ERP: no hay valor, depreciación, centro de costo ni número de inventario contable.
   Solo lo que sirve para reparar: qué es, dónde está, qué lo respalda y a quién se llama.
7. **La criticidad se define en la UI al dar de alta el equipo**, mediante una encuesta de preguntas
   fijas. La primera pregunta cambia según el sitio. El sistema calcula el resultado; nadie elige
   "A" a mano (§4).
8. **Alcance:** unos 50 equipos en Crianza (quizás algo más), más los de Planta.
9. **Fuera de alcance:** repuestos, inventario, compras y costos. A lo sumo, texto libre de
   "repuestos usados" al cerrar.
10. **Quién recibe depende de la prioridad y del sitio** (§6.4). Un P1 es **alarma** y llega de
    inmediato, a cualquier hora:
    - en **Crianza**, al **semanero de turno** y al **encargado de mantenimiento**;
    - en **Planta**, al **supervisor de Planta** y al **encargado de mantenimiento**.

    Las fallas menores (P2/P3) van en un **resumen** al encargado, dentro de su **horario
    configurable en la app**.
11. **En Planta, solo el rol supervisor** (y administrador) da de alta y edita equipos.
12. **El encargado interactúa a lo más una vez al día por OT** (§5.3). La herramienta tiene que
    sentirse como una ayuda, no como una carga: las acciones se agrupan ("recibí todo esto y lo estoy
    atendiendo") y hay un parte diario de una sola pantalla.
13. **Los indicadores se muestran desde el primer día**, aunque tengan pocos datos, con un aviso
    visible mientras la historia sea corta (§9).
14. **Texto libre primero y catálogo después.** El síntoma, la causa y la acción se escriben
    libremente. Cuando haya historia suficiente, se arma un **diccionario "natural"** con lo que la
    gente efectivamente escribe, que cubra el 80 % con botones; el 20 % restante sigue como texto (§5.6).

---

## 2. Punto de partida en el código

- **No existe nada** de mantenimiento ni de equipos en CrianzaApp ni en PlantaApp. Es un módulo
  nuevo, sin migrar datos.
- **Telegram hoy solo envía.** `app/services/notify_telegram.py` expone `enviar`/`actualizar`
  (`sendMessage`/`editMessageText`) y no recibe mensajes. El aviso por bot requiere **leer**
  mensajes, que es una pieza nueva (§6).
- **CrianzaApp no tiene autenticación.** En su web, quien actúa se elige de una lista de personas,
  **sin acreditarla** (el mismo criterio que el "Recibido por" de ensilaje). En Telegram, en
  cambio, el `from.id` del mensaje **sí identifica** a quien reporta, y en PlantaApp la sesión
  identifica al supervisor.
- **La integración firmada entre ambas apps ya existe y funciona en los dos sentidos:**
  - PlantaApp llama a CrianzaApp con `CRIANZA_BASE_URL` + `INTEGRATION_SHARED_SECRET`, por ejemplo
    `/fish/integration/v1/confirm-faena-crotales`. La verificación HMAC está en `app/api/fish.py`.
  - CrianzaApp llama a PlantaApp (`planta_yield.py`, `cultivation_declarations.py`).

  El satélite de Planta reutiliza este mecanismo; no se inventa otro.
- **PlantaApp** tiene login con roles (`operador`, `supervisor`, `auditor`, `contador`,
  `administrador`) y permisos por prefijo de ruta en `core/auth.py::resolve_allowed_roles_for_path`.
  Está en plena modularización (`routers/`, `services/`), así que el satélite nace como un router
  propio.
- **Moldes a copiar en CrianzaApp:**
  - Los QR imprimibles de estanques (`calidad_agua_qr.html`, con `segno`).
  - Los destinatarios de Telegram en la BD (`water_quality_alert_recipients`), con el `chat_id`
    como texto.
  - La rotación de semaneros (`WaterQualityOncallWeek`, `wq_notify.semanero_de`).
  - Los jobs de APScheduler en el proceso (`wq_alerts_scheduler.py`).

---

## 3. Modelo de datos (solo en CrianzaApp)

Todas las tablas llevan el prefijo **`mnt_`** y viven en `fastapp_etapa1`. No tienen claves
foráneas a tablas de otros módulos (`users`, `ponds`): los vínculos a estanques o biofiltros son
referencias blandas, y a los usuarios de PlantaApp se los identifica por su `username`. El módulo
queda desacoplado del resto de CrianzaApp y no hay que tocar tablas ajenas.

### 3.1 Catálogo

**`mnt_sistemas`**: agrupador funcional. Por ejemplo: aireación, bombeo, eléctrico/generación,
biofiltros, alimentación, frío, proceso.

| Campo | Tipo | Descripción |
|---|---|---|
| id | PK | |
| nombre | String(80) unique | |
| sitio | String(10) nullable | `crianza` / `planta` / NULL = ambos (p. ej. "eléctrico") |
| orden | Integer | Orden en listas y en el bot |
| activo | Boolean | |

**`mnt_tipos_equipo`**: soplador, bomba, generador, tablero, motor, compresor, cámara de frío…
En v1 solo tiene `id`, `nombre` y `activo`; sirve para filtrar y agrupar. **Sin lista de
síntomas**: el diccionario llega después y vive en su propia tabla (§3.6).

**`mnt_equipos`**: el activo desde el punto de vista del mantenimiento.

| Campo | Tipo | Descripción |
|---|---|---|
| id | PK | |
| codigo | String(20) unique | `EQ-001`… Correlativo **único para ambos sitios**. Va impreso en el QR y en la placa física |
| sitio | String(10) **not null**, index | `crianza` / `planta` |
| nombre | String(120) | "Soplador 3 Nor-Central", "Cámara de frío 2" |
| tipo_id / sistema_id | FK | |
| ubicacion_texto | String(120) | Dónde está físicamente |
| marca, modelo, serie | String | |
| potencia_kw, voltaje | Numeric / String | |
| fecha_instalacion | Date nullable | |
| respaldo_equipo_id | FK→mnt_equipos nullable | Qué equipo lo cubre si falla (del mismo sitio) |
| contratista_habitual_id | FK→mnt_contratistas nullable | A quién se llama |
| foto / foto_mime | bytea / String(40) nullable | Foto de la placa o del equipo (§6.5). En la web se reduce a ~1280 px en el navegador antes de subir (sin Pillow en el servidor) |
| notas | Text | Datos útiles: correa, rodamientos, filtro, etc. |
| estado | String(20) | `operativo` / `detenido` / `degradado` / `baja` (derivado de las OT abiertas, salvo `baja`) |
| criticidad | String(1) | `A`/`B`/`C`, **copia** del resultado vigente de la encuesta (§4) |
| alta_origen | String(10) | `crianza` / `planta` (por dónde se creó) |
| alta_por | String(120) | En Planta, el `username` **acreditado** del supervisor. En Crianza, la persona elegida (sin acreditar) |
| created_at / updated_at | Timestamp | |

Explícitamente **sin**: valor, depreciación, centro de costo, cuenta contable, número de activo fijo.
Si algún día hace falta cruzar con el ERP, basta un campo `codigo_erp` opcional.

**`mnt_equipo_destinos`**: a qué atiende un equipo de Crianza. **Varios por equipo**, porque los
sopladores centralizados atienden todos los estanques. Se agregó en la migración `20260929_07` y
reemplaza el vínculo único `ref_ubicacion` de PR1. Tiene tres niveles, para no marcar decenas de
estanques uno por uno:

| ref_tipo:ref_id | Significa |
|---|---|
| `sitio:crianza` | Todo el sitio (sopladores centralizados) |
| `unit:<id>` | Una unidad de cultivo completa |
| `pond:<id>` | Un estanque suelto |

Al guardar se normaliza: lo cubierto por un nivel más amplio sobra (`rules.normalizar_destinos`).
Es una referencia blanda, sin FK. Se usa en F4 para asociar una baja de oxígeno disuelto a la
falla de un equipo que atiende ese estanque.

**Redundancias (equipos gemelos).** Desde la ficha, **"Crear redundancia"** abre el alta
precargada con la copia del equipo:

- Nombre con el número siguiente libre ("Soplador 3" → "Soplador 4").
- Se copian los datos técnicos, los destinos, el contratista y las respuestas de criticidad.
- No se copian la serie, la foto ni la fecha de instalación, que son de cada unidad.
- La copia queda respaldada por el original y se ofrece el **respaldo mutuo**, que nunca pisa un
  respaldo que el original ya tuviera.
- Si un equipo tiene respaldo y su encuesta dice "sin respaldo", la ficha avisa que hay que
  reevaluar.

**Marca y modelo con memoria.** Los campos sugieren lo ya ingresado; el modelo se filtra por marca.
Si la combinación ya existe, se ofrece copiar tipo, potencia y voltaje, solo en los campos vacíos.

**Catálogo de sistemas y tipos que crece.** Es una lista plana: se pueden crear sistemas y tipos
desde la misma ficha ("+ Nuevo…") y **fusionar** duplicados en Catálogos. Fusionar mueve los
equipos al destino y desactiva el sobrante, sin borrarlo. Se descartó una jerarquía de dos niveles:
dónde está cada equipo ya lo dicen su ubicación y sus destinos.

### 3.2 Personas y contratistas

**`mnt_personas`**: quién reporta, gestiona, ejecuta o recibe alarmas. Es propia del módulo, no la
tabla `users`.

| Campo | Tipo | Descripción |
|---|---|---|
| id | PK | |
| nombre | String(120) | |
| rol | String(20) | `reportante` / `encargado` / `tecnico` (interno) / `supervisor_planta` |
| sitio | String(10) nullable | Sitio al que pertenece (informativo); NULL = ambos |
| telegram_user_id | String(40) unique nullable | Se vincula desde la bandeja de "no vinculados" (§6.3) |
| bot_iniciado | Boolean | La persona le escribió al bot al menos una vez. Sin esto el bot **no puede** escribirle (§6.4) |
| alarmas_sitios | String(20) nullable | Sitios cuyas alarmas P1 recibe siempre (`crianza`, `planta`, `crianza,planta`). El encargado tiene ambos; el supervisor de Planta, `planta`. El semanero de Crianza **no** se configura aquí: sale de la rotación (§6.4) |
| recibe_resumen | Boolean | Recibe el resumen de fallas menores y OT fuera de plazo |
| horario_dias / horario_desde / horario_hasta | String(20) / Numeric(4,2) / Numeric(4,2) | Horario en que recibe el resumen (0 = lunes; 8,5 = 08:30), **editable en la UI**. Mismo formato que `water_quality_alert_recipients` |
| planta_username | String(80) nullable | `username` de PlantaApp (`user_accounts`), para atribuir lo que llega firmado desde Planta |
| activo | Boolean | |

**`mnt_contratistas`**: empresa, contacto, teléfono, especialidad y notas. No son usuarios. Se
comparten entre sitios.

### 3.3 Avisos y OT

**`mnt_avisos`**: lo que llega desde terreno. Nunca se rechaza en la entrada (§6.2).

| Campo | Tipo | Descripción |
|---|---|---|
| id | PK | Folio del aviso `A-0001` |
| equipo_id | FK | El sitio sale del equipo |
| origen | String(12) | `telegram` / `web` (registrado por el encargado: llamada, contratista, bot caído) / `planta_web` (supervisor desde PlantaApp, §8.3) |
| reportado_por_id | FK→mnt_personas nullable | NULL + `reportante_texto` si el remitente aún no está vinculado |
| reportante_texto | String(120) | Nombre visible de Telegram, `username` de Planta, o lo que escriba el encargado |
| detectado_at | Timestamp | Cuándo se vio. Por defecto, cuando se envió el aviso |
| condicion | String(20) | `detenido` / `degradado` (funciona con problemas) / `anomalia` (algo raro, sigue funcionando) |
| respaldo_entro | String(10) nullable | `si` / `no` / `no_se` (solo si el equipo tiene respaldo y está detenido) |
| descripcion | Text nullable | **Lo que la persona escribió**, tal cual |
| sintoma_codigo | FK→mnt_diccionario nullable | Vacío en v1. Se llena al elegir un botón (cuando exista el diccionario) o al recodificar la historia (§5.6) |
| foto | bytea nullable | |
| audio | bytea nullable | Nota de voz de Telegram (ogg, pocos KB). La escucha el encargado; no se transcribe |
| prioridad_sugerida | String(2) | Calculada (§5) |
| estado | String(15) | `nuevo` / `aceptado` (→ OT) / `unido` (duplicado de un aviso abierto) / `descartado` |
| ot_id | FK nullable | |
| motivo_descarte | String(200) | Obligatorio si se descarta |
| created_at | Timestamp | Hora de servidor en que llegó (≠ `detectado_at`) |

**`mnt_ots`**: órdenes de trabajo.

| Campo | Tipo | Descripción |
|---|---|---|
| id | PK | Folio `OT-0001` (correlativo único para ambos sitios) |
| tipo | String(12) | `correctiva` (F1) / `preventiva` (F3) |
| equipo_id | FK | El sitio sale del equipo |
| prioridad | String(2) | `P1`/`P2`/`P3`. Por defecto, la sugerida |
| prioridad_motivo | String(200) | Obligatorio si difiere de la sugerida |
| plazo_horas | Numeric | Plazo de su prioridad **congelado al aceptar**; si cambia la prioridad, se toma el plazo vigente en ese momento |
| estado | String(20) | Ver §5.2 |
| ejecutor_tipo | String(12) | `interno` / `contratista` |
| tecnico_id / contratista_id | FK nullable | |
| inicio_at | Timestamp | = `detectado_at` del primer aviso. **El reloj parte en la detección, no cuando se acepta** |
| cierre_at | Timestamp nullable | |
| equipo_detenido_desde / equipo_en_servicio_at | Timestamp nullable | Tiempo fuera de servicio del equipo, que puede diferir del tiempo de la OT |
| causa_texto, accion_texto | Text | Texto libre al cerrar (§5.4) |
| causa_codigo, accion_codigo | FK→mnt_diccionario nullable | Vacíos en v1 (§5.6) |
| provisorio | Boolean | Arreglo provisorio (§5.4) |
| ot_origen_id | FK→mnt_ots nullable | OT del arreglo definitivo → la provisoria que la originó |
| repuestos_texto | Text | Libre. Sin inventario |
| trabajo_realizado | Text | |

**`mnt_ot_eventos`**: la bitácora de estados y la **fuente de todos los tiempos**.

| Campo | Tipo | Descripción |
|---|---|---|
| id | PK | |
| ot_id | FK | |
| estado_desde / estado_hasta | String(20) | |
| ocurrido_at | Timestamp | **Cuándo pasó**, editable. Por defecto, ahora |
| registrado_at | Timestamp | Cuándo se anotó (automático, no editable) |
| actor_id | FK→mnt_personas | Quién lo registró |
| actor_texto | String(120) | Nombre anotado cuando no hay persona vinculada (web de Crianza sin login, `username` de Planta) |
| nota | String(300) | "Contratista dice que llega el jueves" |

La separación `ocurrido_at` / `registrado_at` es clave. El contratista le cuenta al encargado a las
16:00 que llegó a las 10:00, y el encargado lo anota así. El tiempo queda bien medido y queda la
huella de que se registró tarde. Si el desfase supera un umbral, se marca en el detalle.

### 3.4 Criticidad

**`mnt_criticidad_evaluaciones`**: una fila por evaluación, con historial. Guarda **las respuestas**,
no solo el resultado. Así, si mañana cambia la regla, se puede recalcular sin volver a preguntar.

| Campo | Tipo | Descripción |
|---|---|---|
| id | PK | |
| equipo_id | FK | |
| respuestas | JSON | `{impacto, respaldo, reposicion, seguridad_ambiente}`. El texto de `impacto` depende del sitio (§4), la escala no |
| resultado | String(1) | A/B/C |
| tolerancia_horas | Numeric nullable | Ventana de tolerancia derivada de `impacto` |
| regla_version | String(10) | Versión de la tabla de reglas con que se calculó |
| evaluado_por | String(120) | Persona (Crianza) o `username` acreditado (Planta) |
| evaluado_at | Timestamp | |

### 3.5 Parámetros

**`mnt_parametros`**: clave/valor editable en la UI. Incluye:

- Plazos objetivo por prioridad, editables. La migración siembra P1 = 4 h, P2 = 24 h y P3 = 7 días.
  Un cambio de plazo **no** recalcula el "fuera de plazo" de OT ya cerradas: cada OT guarda el plazo
  vigente al aceptarse (`plazo_horas`), para que los indicadores históricos no cambien de golpe.
- Umbral de "registrado tarde".
- Horas del resumen (una o más; solo se envía si caen dentro del horario del destinatario).
- Horas sin movimiento antes de repetir una alarma P1.
- Fecha de inicio del registro (para el banner de "pocos datos", §9).

Los destinatarios y sus horarios van en `mnt_personas` (§3.2), no aquí.

### 3.6 Diccionario (llega en la fase del diccionario, §7)

**`mnt_diccionario`**: se crea **vacío** en la migración inicial, para que las FK de §3.3 existan
desde el principio. Se llena cuando haya historia (§5.6).

| Campo | Tipo | Descripción |
|---|---|---|
| id | PK | |
| dominio | String(10) | `sintoma` / `causa` / `accion` |
| tipo_equipo_id | FK nullable | NULL = aplica a todos los tipos |
| etiqueta | String(60) | Texto del botón: "Bota protecciones" |
| sinonimos | Text | Variantes observadas en el texto libre ("salta el térmico", "se dispara"). Sirven para sugerir y recodificar |
| orden, activo | | |

---

## 4. Criticidad: encuesta al dar de alta

El formulario de alta **no se puede guardar** sin responder la encuesta, ni en CrianzaApp ni en
PlantaApp. Son cuatro preguntas cerradas; **solo el texto de Q1 cambia según el sitio**:

| # | Pregunta | Opciones |
|---|---|---|
| Q1 (Crianza) | Si este equipo se detiene, **¿en cuánto tiempo hay daño o mortalidad en peces?** | a) menos de 2 h · b) menos de 24 h · c) no afecta a peces directamente |
| Q1 (Planta) | Si este equipo se detiene, **¿en cuánto tiempo se compromete producto, cadena de frío o inocuidad?** | a) menos de 2 h · b) menos de 24 h · c) no compromete producto |
| Q2 | **¿Tiene respaldo?** | a) ninguno · b) manual (alguien lo tiene que conectar) · c) automático o redundante |
| Q3 | **¿Cuánto demora reponerlo** (repuesto o técnico)? | a) más de 1 semana · b) de 1 a 7 días · c) menos de 1 día |
| Q4 | **¿Su falla pone en riesgo a personas o al medio ambiente?** | sí / no |

**Regla v1** (igual para ambos sitios), escrita como tabla y no como puntaje ponderado, para que se
pueda explicar:

| Q1 \ Q2 | a) sin respaldo | b) manual | c) automático |
|---|---|---|---|
| a) < 2 h | **A** | **A** | B |
| b) < 24 h | **A** | B | C |
| c) sin impacto directo | B | C | C |

- Si Q3 = a (más de una semana de reposición), **sube un nivel** (C→B, B→A).
- Si Q4 = sí, el resultado es **A**, sin importar lo demás.
- La ventana de tolerancia sale de Q1: 2 h, 24 h o sin tolerancia definida.

Se puede reevaluar desde la ficha. Queda en el historial con fecha y autor, y no borra la anterior.

> Aclaración: la criticidad es **del equipo** y es estática. No modela que un soplador importe más
> en una noche de verano que en invierno. El factor del momento entra por la **condición** del aviso
> y por si **entró el respaldo** (§5.1). Así se evita sobre-diseñar.

---

## 5. Flujo de la OT (web de CrianzaApp)

### 5.1 Prioridad sugerida

Resulta de la criticidad del equipo y de la condición reportada:

| Criticidad \ Condición | Detenido sin respaldo operando | Detenido con respaldo operando, o degradado | Anomalía |
|---|---|---|---|
| **A** | **P1** | P2 | P3 |
| **B** | P2 | P2 | P3 |
| **C** | P3 | P3 | P3 |

- "Respaldo operando" = `respaldo_entro = si`. Si responden `no_se`, se trata como `no`.
- **P1**: alarma, atención dentro de su plazo (4 h por defecto). **P2**: dentro del día. **P3**: se
  planifica.
- El encargado puede cambiar la prioridad, pero debe escribir un motivo, que queda registrado.

### 5.2 Estados

Cada estado dice **quién tiene la pelota**. Esto es lo que permite desglosar el tiempo total:

```
aviso nuevo ──aceptar──▶ pendiente ──▶ en_ejecucion ──▶ reparada ──verificar──▶ cerrada
   │                        │  ▲            │  ▲
   │ descartar / unir       ▼  │            ▼  │
   ▼                  espera_contratista   espera_repuesto
 (descartado)                                            (anulada desde cualquier estado, con motivo)
```

| Estado | Tramo de tiempo que acumula |
|---|---|
| `aviso` (antes de aceptar) | **Reacción**: nadie ha mirado el aviso |
| `pendiente` | **Gestión**: aceptada, pero no hay nadie trabajando ni esperando algo concreto |
| `espera_contratista` | **Contratista**: se llamó y no llega |
| `espera_repuesto` | **Repuesto**: se pidió, no ha llegado |
| `en_ejecucion` | **Ejecución**: alguien está reparando |
| `reparada` | **Verificación**: reparado, falta confirmar que funciona. Opcional, puede cerrarse directo |

Se puede ir y volver entre estados (ejecución → espera de repuesto → ejecución). El tiempo de cada
tramo es la **suma** de sus intervalos en `mnt_ot_eventos`.

### 5.3 Qué ve el encargado

- **Tablero único**: avisos nuevos arriba (con foto), luego OT abiertas ordenadas por prioridad y
  antigüedad, con una barra de color por tramo, una marca de "fuera de plazo" y una etiqueta de
  **sitio**. Por defecto muestra ambos sitios; se puede filtrar por uno.
- **Unir aviso duplicado**: si ya hay una OT abierta del mismo equipo, el aviso se ofrece para unirlo.

**Regla de diseño: a lo más una interacción al día por OT.** La herramienta tiene que ser una
ayuda para el encargado; si pide clics por cada novedad, se abandona y los tiempos salen mal. Por
eso hay dos mecanismos que agrupan:

1. **Acuse de recibo en lote.** En el tablero, los avisos nuevos traen una casilla y un botón:
   **"Recibí estos avisos y los estoy atendiendo"**. Un clic acepta todos los marcados (de uno o de
   ambos sitios): crea sus OT con la prioridad sugerida, las deja en `pendiente` y cierra el tramo de
   **reacción** con la misma hora. Si en el mismo gesto elige otro estado común ("en ejecución",
   "espera contratista"), se aplica a todas.
2. **Parte diario.** Es una sola pantalla con todas las OT abiertas de ambos sitios, una fila cada
   una, con el estado actual **ya seleccionado**. El encargado solo toca las filas que **cambiaron**
   (estado, hora si no fue "ahora", nota opcional) y guarda **una vez**. Las filas que no tocó siguen
   igual, porque "sin cambios" es la respuesta por defecto y no exige clic. El resumen de Telegram
   enlaza directo a esta pantalla.

Cambiar el estado de una OT suelta desde su detalle sigue siendo posible (un botón + hora + nota),
pero es la excepción.

**Costo asumido:** con registro diario, los tramos se miden con resolución de horas o de un día, no
de minutos. Para el dolor actual (OT que duran días) basta. Los P1 igual tienen hora precisa en el
aviso y en el acuse de recibo, que es donde los minutos importan.

### 5.4 Cierre

Obligatorio:

- **Causa**: texto libre ("qué falló").
- **Acción**: texto libre ("qué se hizo").
- **Hora de equipo en servicio.**
- **¿Arreglo provisorio?** sí/no. Es el único campo estructurado que no espera al diccionario: si es
  sí, al cerrar se ofrece crear la OT del arreglo definitivo, enlazada a la anterior. Esperar el
  diccionario significaría perder esos pendientes.

Opcional: repuestos (texto) y trabajo realizado (detalle).

### 5.5 Indicador central (el dolor)

Una vista **"¿Dónde se va el tiempo?"**: tiempo total de las OT cerradas del período, desglosado por
tramo (reacción, gestión, contratista, repuesto, ejecución), filtrable por **sitio**, prioridad,
sistema y contratista. **Este es el entregable que ataca el problema; lo demás es
infraestructura.** No depende del diccionario: los tramos salen de los estados, no del texto.

### 5.6 Del texto libre al diccionario

El catálogo de síntomas, causas y acciones **no se inventa en un escritorio**; se extrae de lo que la
gente escribe. Etapas:

1. **v1, solo texto libre.** Se guarda tal cual. No se pierde nada, porque los campos `*_codigo`
   ya existen y quedan vacíos.
2. **Armar el diccionario.** Se hace cuando haya volumen suficiente: orientativo **~100 avisos o
   6 meses**, lo que ocurra primero. Se exportan los textos, se agrupan (a mano o con ayuda de
   Claude) y se eligen las ~6–10 etiquetas por dominio que cubran **~80 %** de los casos. Las
   variantes observadas se guardan como `sinonimos`.
3. **Recodificar la historia.** Una pantalla de "sin clasificar" muestra cada texto con la
   etiqueta sugerida por sinónimos, y el encargado acepta o corrige en lote. Así los indicadores de F4
   comparan el antes y el después del diccionario.
4. **Diccionario en uso.** El bot y el cierre muestran botones con las etiquetas, más **"Otro
   (escribir)"** para el 20 %. Cada "Otro" alimenta la próxima revisión del diccionario; si una
   variante se repite, se convierte en etiqueta.

La **condición** del aviso (detenido / con problemas / algo raro) **no** es texto libre ni espera al
diccionario: es lo que calcula la prioridad (§5.1).

---

## 6. Telegram: aviso de falla

### 6.1 Un bot nuevo, único para ambos sitios

- Un **bot propio de mantenimiento**, distinto del de alertas de calidad de agua:
  - Telegram admite **un solo lector de mensajes por token**.
  - Mezclar ambos temas en un mismo chat confunde a quien reporta.
- **El mismo bot atiende Crianza y Planta.** Lo corre CrianzaApp, que es donde están los datos.
- El token va en `server.env` (`MNT_TELEGRAM_TOKEN`).
- Lectura por **long polling** (`getUpdates`): conexión saliente, sin abrir puertos ni exponer el
  servidor.
- ⚠️ **Solo un proceso puede leer mensajes.** El servidor de desarrollo (puerto 8003) **no debe
  arrancar el lector**: se controla con `MNT_BOT_ENABLED=1` solo en producción. Si ambos leen, se
  roban los mensajes (Telegram responde 409) y los avisos se pierden sin que nadie lo note.

### 6.2 Conversación (objetivo: menos de 30 s)

El QR pegado en el equipo contiene `https://t.me/<bot>?start=EQ-012`. Al escanearlo:

```
Bot: 🔧 Soplador 3 — Nor-Central (EQ-012) · Crianza
     ¿Cómo está el equipo?
     [🛑 Detenido] [⚠️ Funciona con problemas] [👀 Algo raro]

(si Detenido y el equipo tiene respaldo)
Bot: ¿Entró el respaldo (Soplador 4)?   [Sí] [No] [No sé]

Bot: ¿Qué pasa? Escríbelo, manda un audio o una foto (o varias cosas).
     [Terminar]

(cuando exista el diccionario, §5.6, esta pregunta pasa a ser:
 [No parte] [Bota protecciones] [Ruido/vibración] … [Otro (escribir)])

Bot: ✅ Aviso A-0042 registrado — prioridad P1. Se avisó al encargado y al semanero.
```

- **Sin QR** (`/falla`): se elige el sitio, luego el sistema y luego el equipo, con botones.
- **Si ya hay un aviso u OT abierta** del equipo, el bot lo dice ("hay una OT abierta desde las
  06:40") y ofrece **agregar información** o **reportar otra falla**. Así se evitan duplicados sin
  impedir reportar.
- Una conversación a medias expira a los 10 minutos. Si hubo al menos la condición, se guarda igual
  como aviso incompleto, porque **un aviso a medias vale más que ninguno**.
- **La entrada nunca rechaza un aviso.** Si el remitente no está vinculado, el aviso se guarda con
  su nombre de Telegram y queda en la bandeja (§6.3).

### 6.3 Vinculación de personas

No hay códigos ni pasos para el operario:

1. La primera vez que alguien escribe, su `telegram_user_id` aparece en la bandeja web **"Usuarios de
   Telegram sin vincular"**.
2. El encargado lo asocia a una persona existente o crea una nueva.

Desde ahí, todos sus avisos quedan atribuidos. Se puede **bloquear** a un remitente.

### 6.4 Notificaciones (salida)

**La prioridad decide el canal y el sitio decide quién.** Hay solo dos canales:

- **Alarma.** Un P1, es decir, lo que requiere atención dentro de su plazo (4 h por defecto), llega
  **de inmediato y a la vez** a sus destinatarios, a cualquier hora. Ningún horario filtra alarmas.
- **Resumen.** Las fallas menores (P2/P3) **no** generan mensajes sueltos: se juntan en un resumen
  que se envía dentro del horario del encargado.

| Evento | A quién | Cuándo |
|---|---|---|
| **Alarma P1 en Crianza** | **Semanero de turno + encargado de mantenimiento** | Inmediato, 24/7 |
| **Alarma P1 en Planta** | **Supervisor(es) de Planta + encargado de mantenimiento** | Inmediato, 24/7 |
| Alarma P1 sin movimiento | Los mismos destinatarios de la alarma | Si pasan X h (parámetro) sin cambio de estado, se repite una vez: el P1 no puede quedar en silencio |
| Aviso P2/P3 (falla menor) | Nadie en el momento | Se acumula para el resumen |
| **Resumen** | Encargado (`recibe_resumen`) | A las horas configuradas, **solo dentro de su horario** (por defecto una vez, al inicio; se puede agregar una segunda, p. ej. 14:00, para que un P2 de 24 h no espere al día siguiente). Incluye ambos sitios: avisos nuevos sin aceptar, OT abiertas por prioridad, OT fuera de plazo y un enlace al parte diario (§5.3) |
| Confirmación al reportante | Quien avisó | Al aceptar la OT y al cerrarla ("EQ-012 reparado 14:20") |

- **Cálculo de destinatarios de una alarma**: las personas con el sitio en `alarmas_sitios` (el
  encargado tiene ambos; los supervisores de Planta, `planta`) **más**, si el sitio es Crianza, el
  semanero de turno de la rotación de calidad de agua (`semanero_de`).
- ⚠️ **Un bot solo puede escribir a quien le escribió antes.** Que un semanero reciba alertas del
  bot de calidad de agua **no** habilita al bot de mantenimiento. Cada semanero, supervisor de Planta
  y encargado debe iniciar el bot de mantenimiento una vez (`/start`). La pantalla de personas marca
  en rojo a los destinatarios de alarmas con `bot_iniciado = False`. Si no, la primera alarma P1
  nocturna se pierde sin aviso.
- **Si falta un destinatario** (no hay semanero, o no inició el bot), la alarma igual llega a los
  demás y el tablero registra el problema. Una alarma P1 nunca queda sin destinatario: el encargado
  está en ambos sitios.
- La confirmación al reportante es barata y cambia la cultura: quien avisa ve que su aviso sirvió.

La salida reutiliza el patrón de `notify_telegram.enviar`, pero con el token del bot de mantenimiento.

### 6.5 Fotos

Telegram entrega cada foto en varios tamaños. Se descarga la versión cercana a **1280 px** (unos
100–300 KB) y se guarda **en la BD (`bytea`)**, no en disco. El respaldo diario
(`backup_databases.ps1`) solo vuelca las bases, así que una foto en disco quedaría **sin respaldo**.
Con pocos avisos a la semana, el volumen es despreciable.

### 6.6 Sin internet

Si se cae internet (típico en un corte de luz, cuando más fallan los equipos), el bot no recibe
nada. Lo que se envió sin señal lo reenvía el propio Telegram cuando vuelve la conexión.

**Respaldo en la red local**: el encargado registra el aviso en la web de CrianzaApp (`origen =
web`), y el supervisor de Planta desde PlantaApp (`origen = planta_web`, §8.3), en ambos casos con
la hora real de detección. **No** se construye una PWA sin conexión para esto.

---

## 7. Fases y PRs

| Fase | PR | App | Contenido |
|---|---|---|---|
| **F1** | PR1 | Crianza | Paquete `app/maintenance/` + modelos + migración (incluye `mnt_diccionario` vacío). Catálogo con **sitio**: sistemas, tipos, equipos, personas con horario y alarmas por sitio, contratistas. **Encuesta de criticidad obligatoria en el alta**, con Q1 según sitio. Hoja de QR imprimible por sitio |
| | PR2 | Crianza | OT en la web: aviso manual, aceptar/unir/descartar, acuse de recibo en lote, estados con `ocurrido_at`, parte diario, cierre con texto libre, tablero con filtro por sitio, desglose de tiempos por OT |
| | PR3 | Crianza | Bot de Telegram: lector, conversación de aviso (condición con botones + texto/audio/foto libre), bandeja de vinculación, notificaciones básicas (aviso nuevo al encargado, cierre al reportante) |
| | PR3P | Crianza + **Planta** | **Satélite de Planta** (§8): API firmada en CrianzaApp + router `/mantenimiento` en PlantaApp con alta/edición de equipos y encuesta, mis OT (solo lectura), aviso de respaldo y QR |
| **F2** | PR4 | Crianza | Matriz de prioridad (§5.1), plazos, **ruteo de alarmas por sitio** y resumen en horario (§6.4), repetición de P1 sin movimiento, vista "¿Dónde se va el tiempo?" |
| **Diccionario** | PR4b | Crianza | Cuando haya ~100 avisos o 6 meses (§5.6): exportación de textos, carga del diccionario, pantalla de recodificación, botones en bot y cierre con "Otro (escribir)" |
| **F3** | PR5 | Crianza | Preventivo por **calendario**: planes por equipo (cada N días) que generan OT preventivas con anticipación; % de cumplimiento. Horómetros no, porque serían lecturas manuales poco confiables. Planta ve sus preventivas en "mis OT" sin cambios en el satélite |
| **F4** | PR6 | Crianza | Indicadores (MTTR por tramo, MTBF por equipo —visibles desde el inicio con banner "pocos datos" y n por valor, §9—, fallas repetidas por causa) + **cruce con calidad de agua y mortalidad** para Crianza: fallas de aireación y bombeo sobre la curva de OD del estanque vinculado, mortalidad en la ventana posterior. Para Planta, el cruce equivalente con los registros de temperatura de cámaras (`chill_logs`) queda como posibilidad futura |

Mientras no esté PR3P, el encargado puede dar de alta los equipos de Planta directamente en
CrianzaApp, eligiendo el sitio.

---

## 8. Integración con PlantaApp (satélite)

### 8.1 Estructura en CrianzaApp

```
app/maintenance/
├── models.py          tablas mnt_*
├── rules.py           criticidad, prioridad, tramos de tiempo. Python puro, sin BD ni FastAPI; con pruebas propias
├── service.py         casos de uso (alta de equipo, aviso, transición, cierre…). Recibe una Session
├── notify.py          cálculo de destinatarios por sitio y envío (§6.4)
├── telegram_bot.py    lector + máquina de estados de la conversación
├── router.py          UI web del encargado (/views/ui/mantenimiento/...)
├── integration.py     API firmada para PlantaApp (/mnt/integration/v1/...)
└── (plantillas en app/templates/mantenimiento_*.html, como el resto de la app)
```

Los tres puntos de entrada (`router.py`, `integration.py` y `telegram_bot.py`) llaman al mismo
`service.py`. Una regla de negocio no se escribe dos veces, venga el cambio desde la web, desde
Planta o desde Telegram.

### 8.2 API firmada en CrianzaApp

Se usa el mismo esquema HMAC que ya valida `app/api/fish.py`: `INTEGRATION_SHARED_SECRET`, clave de
origen `plantaapp`, marca de tiempo con tolerancia y nonce. Cada llamada de escritura incluye en el
cuerpo firmado **quién es el usuario de Planta** (`username`, `full_name`, `role`); CrianzaApp lo
guarda como autor (`alta_por`, `evaluado_por`, `reportante_texto`).

| Método y ruta (`/mnt/integration/v1`) | Uso |
|---|---|
| `GET /catalogos?sitio=planta` | Sistemas, tipos y contratistas para armar los formularios de Planta |
| `GET /equipos?sitio=planta` | Listado de equipos de Planta, con estado y criticidad |
| `POST /equipos` | Alta de equipo **con la encuesta de criticidad**. Fuerza `sitio = planta` |
| `PUT /equipos/{codigo}` | Edición (incluye reevaluar criticidad). Rechaza equipos de Crianza |
| `GET /ots?sitio=planta&estado=abiertas\|cerradas&desde=` | "Mis OT": folio, equipo, prioridad, estado, días abierta, tramos, fuera de plazo |
| `POST /avisos` | Aviso de respaldo cuando no hay Telegram (`origen = planta_web`) |
| `GET /qr?codigos=` | Datos para la hoja de QR de Planta (enlaces `t.me/<bot>?start=...`) |

Validaciones del lado de CrianzaApp, **además** de las de Planta:

- `role` debe ser `supervisor` o `administrador` para escribir.
- Todo lo que llega desde Planta queda restringido a `sitio = planta`.

La autorización real la hace PlantaApp con su login; este chequeo es una segunda barrera.

### 8.3 Lo que se agrega en PlantaApp

- **`routers/mantenimiento.py`** (sigue el patrón de modularización en curso) con prefijo
  **`/mantenimiento`**:
  - **Equipos**: listado, alta y edición con la encuesta de criticidad (Q1 de Planta).
  - **Mis OT**: abiertas y cerradas recientes, **solo lectura**, con el desglose de tiempos.
  - **Reportar falla**: formulario de respaldo para cuando no hay Telegram (§6.6).
  - **Hoja de QR** de sus equipos (con `qrcode`, que PlantaApp ya usa).
- **`services/mantenimiento_client.py`**: el cliente firmado. Reutiliza `CRIANZA_BASE_URL`,
  `INTEGRATION_SHARED_SECRET` y `CRIANZA_INTEGRATION_TIMEOUT_SECONDS` de `config.py`.
- **Permisos**: se agrega `("/mantenimiento", {"supervisor", "administrador"})` a
  `resolve_allowed_roles_for_path` en `core/auth.py`.
- **Sin tablas ni migraciones** en PlantaApp. No guarda copia de nada: siempre consulta a
  CrianzaApp.
- **Si CrianzaApp no responde**, las pantallas muestran "Mantenimiento no disponible en este momento
  (CrianzaApp no responde). Para una falla urgente, usa Telegram o llama al encargado." Nunca un
  error 500.
- `tests/test_smoke.py` se amplía con las rutas nuevas, simulando el cliente.

### 8.4 Qué **no** hace Planta

No acepta avisos, no cambia estados de OT, no cierra OT y no ve indicadores ni OT de Crianza. Todo
eso es del encargado, en CrianzaApp. Si algún día un supervisor de Planta necesita ver los
indicadores de su sitio, se agrega un `GET` más; el diseño lo permite sin cambiar nada de fondo.

---

## 9. Limitantes conocidas

1. **La calidad del dato depende de que se registre.** Por eso el diseño limita la carga del
   encargado a **una interacción al día por OT**, con acuse de recibo en lote y parte diario
   (§5.3). A cambio, los tramos se miden con resolución de horas o de un día. El éxito sigue
   dependiendo más del encargado que del software, pero el software no le cobra más de lo necesario.
2. **La hora de la falla suele ser desconocida** (de noche). Se mide desde la **detección**, no desde
   la falla real. Queda documentado así en los indicadores.
3. **Los indicadores se muestran desde el primer día, aunque tengan pocos datos.** Mientras la
   historia sea corta, todas las vistas de indicadores llevan un banner: *"Pocos datos: los valores
   son orientativos hasta completar 6 meses de registro (desde el dd-mm-aaaa)"*. El banner
   desaparece solo al cumplirse 6 meses desde la primera OT (fecha configurable). Además, cada valor
   muestra su **n** (p. ej. "MTBF 12 d · n = 2"). Así se entiende por qué un número salta de una
   semana a otra y nadie tiene que acordarse de cuándo creerle.
4. **La identidad en la web de CrianzaApp no se acredita** (no tiene login). Telegram sí identifica
   a quien reporta, y lo que llega desde PlantaApp viene firmado con el usuario de su sesión.
5. **PlantaApp depende de CrianzaApp** para sus pantallas de mantenimiento. Están en el mismo
   servidor y el riesgo es bajo; si CrianzaApp cae, Planta muestra un mensaje claro y las alarmas
   por Telegram tampoco salen hasta que vuelva (el bot vive en CrianzaApp).
6. **Dependencia de internet y de Telegram** para los avisos. La mitigación es el aviso web en la red
   local (§6.6).
7. **La criticidad es estática.** No considera la estación ni la hora (§4).
8. **Un segundo lector de mensajes con el mismo token rompe los avisos** sin dar error visible
   (§6.1). Hay que vigilar el despliegue en desarrollo.

---

## 10. Decisiones (todas cerradas, 2026-09-29)

| # | Decisión | Resuelto |
|---|---|---|
| **D1** | Relación con PlantaApp | **Sistema único con el núcleo en CrianzaApp**, porque el encargado de mantenimiento es el mismo para ambos sitios. PlantaApp es un satélite con pantallas propias que consultan a CrianzaApp por API firmada (opción B). *Reemplaza la decisión inicial de copiar el módulo a PlantaApp* |
| **D2** | ¿La encuesta de criticidad va en F1 o en F2? | **F1**, obligatoria en el alta. La matriz de prioridad queda en F2 |
| **D3** | ¿Quién recibe los avisos? | **Según prioridad y sitio**: P1 = **alarma** inmediata 24/7 al **encargado** + **semanero** (Crianza) o **supervisor de Planta** (Planta); fallas menores → **resumen** al encargado dentro de su horario, configurable en la app (§6.4) |
| **D4** | Listas de síntomas, causas y acciones | **Texto libre primero.** Con historia suficiente se arma un diccionario "natural" que cubra el 80 %, y el texto libre resuelve el 20 % (§5.6) |
| **D5** | Plazos objetivo por prioridad | **Configurables en la UI** (`mnt_parametros`). Valores iniciales: P1 = 4 h, P2 = 24 h, P3 = 7 días |
| **D6** | Material de las placas QR | **Fuera de alcance del módulo.** Lo resuelve el equipo de terreno; la app solo entrega la hoja de QR imprimible |
| **D7** | ¿Eventos que no son falla de un equipo (corte de luz, caída de caudal)? | **No, por ahora.** Solo equipos físicos. Si se retoma, basta dar de alta un equipo "virtual" (p. ej. `EQ-RED`), sin cambiar el modelo |
| **D8** | ¿El QR de Planta usa el mismo bot? | **Sí, un solo bot** para ambos sitios; el equipo escaneado determina el sitio |
| **D9** | ¿Quién de Planta da de alta equipos? | **Rol supervisor** (y administrador) de PlantaApp |
