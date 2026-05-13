# Análisis desde APP-CULTIVO — Comentarios para revisión en APP-FAENA
**Versión:** 1.0 — 04/05/2026  
**Autor:** Equipo APP-CULTIVO (Kenoz Crianza)  
**Propósito:** Puntos que APP-FAENA debe confirmar, aclarar o ajustar para que la integración funcione end-to-end.

---

## 1. Prerequisito: tabla de Despachos no existe aún en APP-CULTIVO

APP-CULTIVO **no tiene todavía** una entidad "Despacho de Peces" en su modelo. El modelo `transports` existente es incompleto (solo chofer, patente, observaciones — sin asociación a peces ni peso declarado).

**Impacto en APP-FAENA:** No esperar llamadas al endpoint `POST /api/integration/v1/entregas` hasta que APP-CULTIVO construya esa entidad. Estimar 1–2 semanas de desarrollo previo.

---

## 2. Prerequisito: tabla de Declaraciones tampoco existe en APP-CULTIVO

No existe ningún modelo equivalente a garantías sanitarias o declaraciones de cultivo. El endpoint `POST /api/integration/v1/declaraciones` tampoco se podrá llamar hasta implementarlo.

---

## 3. Formato del campo `pit_tag` — posible problema de datos legacy

APP-CULTIVO almacena identificadores de peces en `fish.internal_id` (String 120). Los datos cargados desde el sistema anterior **no garantizan** que todos sean exactamente 15 dígitos numéricos. Hay valores con sufijos (`_R`), letras, y longitudes distintas.

**Pregunta para APP-FAENA:**
- ¿El endpoint `POST /entregas` retorna `422` con detalle de qué `pit_tag` falló la validación?
- ¿O rechaza el despacho entero sin indicar cuál falló?

Necesitamos el detalle por ítem para poder mostrar al operario exactamente qué pez tiene el dato incorrecto antes de enviar.

---

## 4. Unidad de peso: ¿gramos o kilogramos en los registros de APP-FAENA?

El campo `peso_vivo_kg` de la spec dice "kilogramos con hasta 3 decimales". APP-CULTIVO almacena pesos de muestreo en `fish_samplings.weight` cuya unidad original (legacy) **no está confirmada** — puede ser gramos.

**Acción requerida:** APP-CULTIVO verificará internamente la unidad y convertirá si es necesario antes de enviar. APP-FAENA debe confirmar que interpretará siempre el campo como kilogramos, sin conversión adicional en su lado.

---

## 5. Campo `centro_cultivo.id` — no existe entidad "centro" en APP-CULTIVO

APP-CULTIVO tiene `cultivation_units` (estanques, hatchery, etc.) pero no una entidad de nivel superior "centro de cultivo" con un ID propio.

**Propuesta:** APP-CULTIVO enviará un ID fijo configurable por variable de entorno (`CULTIVO_CENTRO_ID` y `CULTIVO_CENTRO_NOMBRE`). ¿APP-FAENA puede aceptar un ID de tipo string arbitrario (ej. `"kenoz-crianza-central"`)? ¿O requiere un entero o UUID?

---

## 6. Campo `edad_meses` — no derivable con precisión desde el modelo actual

APP-CULTIVO tiene `lots.hatch_year` (solo año) pero no una fecha exacta de primera siembra. El campo es `nullable` en la spec, por lo que se enviará `null` hasta que se extienda el modelo de lotes.

**Sin impacto bloqueante**, pero notificar si APP-FAENA hace alguna validación sobre este campo que rechace el registro cuando viene `null`.

---

## 7. Callback de entrega: ¿`integration_id` del callback es el del despacho o uno nuevo?

La spec §4.1 dice:
> `"integration_id": "uuid-v4-de-este-evento-de-callback"`

Esto sugiere que el `integration_id` en el body del callback es **un UUID nuevo del evento**, distinto al `integration_id` del despacho original.

**Pregunta para APP-FAENA:** ¿Cómo correlaciona APP-FAENA el callback con el despacho original? ¿Solo por `faena_entrega_id`? ¿O APP-FAENA también envía el `integration_id` original del despacho en algún campo del callback body?

APP-CULTIVO necesita al menos un campo de correlación confiable para actualizar el registro correcto. El `{integration_id}` en la URL del endpoint (`/entregas/{integration_id}/estado`) ¿es el del despacho original o el del evento de callback?

---

## 8. Idempotencia del callback — ¿qué campo usar para detectar duplicados?

La spec §4.1 dice: "Si el `integration_id` de este callback ya fue procesado → retornar `200` sin re-ejecutar."

Para implementar esto, APP-CULTIVO guardará en `faena_integration_log` el `integration_id` de cada callback recibido. Confirmar que APP-FAENA garantiza que cada evento de callback tiene un `integration_id` único (no reutiliza el mismo UUID si hace reintento de envío del callback).

---

## 9. Bloqueo de anulación (§5.3) — ¿respuesta 409 siempre tiene el mismo schema?

La spec muestra un ejemplo de `409` con `error: "ANULACION_BLOQUEADA"`. APP-CULTIVO mostrará ese `message` directamente al usuario.

**Pregunta:** ¿Puede APP-FAENA retornar otros códigos de error en ese `error` field para el 409? Si hay más de uno, necesitamos la lista completa para traducirlos a mensajes en español.

---

## 10. Health check — ¿requiere autenticación o es público?

La spec §9 dice "sin header de autenticación". Confirmar que el endpoint `GET /api/integration/v1/health` de APP-FAENA es accesible sin API key, para que APP-CULTIVO pueda usarlo como verificación previa al envío y también como monitor de disponibilidad.

---

## 11. Coordinación de claves antes del primer test

Para el primer test end-to-end necesitamos intercambiar:

| Dato | Quién genera | Quién recibe |
|------|-------------|--------------|
| `FAENA_INTEGRATION_API_KEY` | APP-FAENA | APP-CULTIVO |
| `CULTIVO_CALLBACK_API_KEY` | APP-CULTIVO | APP-FAENA |
| URL base APP-FAENA (dev) | APP-FAENA | APP-CULTIVO |
| URL callback APP-CULTIVO (dev) | APP-CULTIVO | APP-FAENA |

Proponer canal seguro para el intercambio (no por email ni chat sin cifrar).

---

## 12. Secuencia de desarrollo sugerida (APP-CULTIVO)

Para transparencia, este es el orden en que APP-CULTIVO implementará:

| Etapa | Contenido | Dependencia en APP-FAENA |
|-------|-----------|--------------------------|
| 0a | Variables de entorno (`pydantic-settings`) | Ninguna |
| 0b | Modelo y UI de Despachos de Peces | Ninguna |
| 0c | Modelo y UI de Declaraciones de Cultivo | Ninguna |
| 1 | Campos de integración + tabla `faena_integration_log` | Ninguna |
| 2 | Funciones de emisión §5.1 y §5.2 + endpoint callback §4.1 | Ambiente dev de APP-FAENA levantado |
| 3 | Health check §9 + UI de estado + botón reintentar | Health endpoint de APP-FAENA disponible |
| 4 | Reintentos automáticos con backoff (APScheduler) | Estabilidad del ambiente |
