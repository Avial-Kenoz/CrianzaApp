# Requerimientos de APP-CULTIVO para Integración con APP-FAENA
**Versión:** 1.0 — 04/05/2026  
**Referencia:** `especificacion_integracion_cultivo_faena_v1.md`  
**Destinatario:** Equipo de desarrollo de APP-CULTIVO  
**Propósito:** Descripción precisa y autosuficiente de todo lo que APP-CULTIVO debe implementar para que la integración funcione end-to-end. No se requiere conocer el código interno de APP-FAENA.

---

## 1. Resumen Ejecutivo

APP-CULTIVO actúa como **emisor principal** en los dos flujos de integración:

| Flujo | APP-CULTIVO hace | APP-FAENA hace |
|-------|-----------------|----------------|
| Entrega de peces | Envía notificación de despacho | Registra recepción, confirma o rechaza |
| Declaración de cultivo | Envía garantías sanitarias | Valida y activa garantías para la faena |

APP-CULTIVO también debe exponer **dos endpoints de callback** para recibir el resultado de cada flujo desde APP-FAENA.

---

## 2. Variables de Entorno Requeridas

APP-CULTIVO debe gestionar las siguientes variables de entorno. Nunca se hardcodean en código fuente.

| Variable | Tipo | Descripción | Ejemplo |
|----------|------|-------------|---------|
| `FAENA_INTEGRATION_API_KEY` | String ≥32 chars | Clave que APP-CULTIVO presenta a APP-FAENA en cada llamada | `xK9m...` (32+ chars aleatorios) |
| `FAENA_BASE_URL` | URL | URL base de APP-FAENA en el ambiente correspondiente | `https://faena.empresa.cl` |
| `CULTIVO_CALLBACK_API_KEY` | String ≥32 chars | Clave que APP-FAENA debe presentar al llamar los callbacks de APP-CULTIVO | `zP3n...` (32+ chars aleatorios) |

> Los tres valores deben coordinarse con el equipo de APP-FAENA **antes** del primer despliegue en cada ambiente (dev, staging, prod).

---

## 3. Modelo de Datos — Cambios Requeridos

### 3.1 Campos nuevos en el modelo de Entrega/Despacho de Peces

Agregar a la tabla o modelo que registra despachos de peces hacia planta:

| Campo | Tipo | Nullable | Descripción |
|-------|------|----------|-------------|
| `integration_id` | UUID / String(36) | No (generado al crear) | Identificador único del mensaje. Se genera con `uuid.uuid4()` al crear el despacho. Inmutable después de la primera emisión. |
| `faena_integration_status` | String | Sí | Estado del ciclo de integración. Ver estados válidos abajo. |
| `faena_entrega_id` | Integer | Sí | ID asignado por APP-FAENA al recibir la entrega. Se recibe en el callback. |
| `faena_confirmado_en` | DateTime | Sí | Timestamp de confirmación recibido desde APP-FAENA. |
| `faena_rechazo_motivo` | String | Sí | Código de motivo si APP-FAENA rechaza. Ver códigos §4.2. |
| `faena_rechazo_detalle` | Text | Sí | Texto libre del rechazo. |
| `integration_last_error` | Text | Sí | Último error de red o HTTP al intentar enviar. Para reintento manual. |
| `integration_sent_at` | DateTime | Sí | Timestamp del último envío exitoso a APP-FAENA. |

**Estados válidos para `faena_integration_status`:**

| Estado | Significado |
|--------|-------------|
| `NO_ENVIADO` | Despacho creado, aún no enviado a APP-FAENA |
| `ENVIADO` | POST enviado a APP-FAENA, esperando confirmación |
| `RECIBIDA` | APP-FAENA confirmó recepción física |
| `RECHAZADA` | APP-FAENA rechazó la entrega |
| `ERROR_ENVIO` | Fallo de red o HTTP ≥ 400 al intentar enviar |

---

### 3.2 Campos nuevos en el modelo de Declaración de Cultivo

Agregar a la tabla o modelo que registra declaraciones/garantías sanitarias:

| Campo | Tipo | Nullable | Descripción |
|-------|------|----------|-------------|
| `integration_id` | UUID / String(36) | No (generado al crear) | Igual que §3.1. Inmutable. |
| `faena_integration_status` | String | Sí | Estado del ciclo de integración. Ver estados válidos abajo. |
| `faena_declaracion_id` | Integer | Sí | ID asignado por APP-FAENA. Se recibe en respuesta directa (no en callback). |
| `faena_estado_validacion` | String | Sí | `VALIDADA` o `OBSERVADA`, recibido en la respuesta de APP-FAENA. |
| `faena_valida_hasta` | Date | Sí | Fecha de vigencia calculada por APP-FAENA. |
| `faena_observaciones` | Text | Sí | Observaciones de validación recibidas desde APP-FAENA (JSON serializado). |
| `integration_last_error` | Text | Sí | Último error de red o HTTP. Para reintento manual. |
| `integration_sent_at` | DateTime | Sí | Timestamp del último envío exitoso. |
| `anulada_en` | DateTime | Sí | Si se anuló, timestamp del evento de anulación. |
| `anulacion_motivo` | String | Sí | Código de motivo de anulación. Ver §4.3. |

**Estados válidos para `faena_integration_status`:**

| Estado | Significado |
|--------|-------------|
| `NO_ENVIADO` | Declaración creada, no enviada aún |
| `ENVIADO` | POST enviado, respuesta recibida (síncrona) |
| `VALIDADA` | APP-FAENA validó sin observaciones |
| `OBSERVADA` | APP-FAENA validó con advertencias (no bloquea operación) |
| `ANULADA` | Declaración anulada desde APP-CULTIVO |
| `ERROR_ENVIO` | Fallo de red o HTTP ≥ 400 |

---

## 4. Endpoints que APP-CULTIVO Debe Implementar (Callbacks)

APP-FAENA llama a estos endpoints cuando cambia el estado de una entrega. APP-CULTIVO debe exponer ambos.

### 4.1 Callback: Resultado de Entrega de Peces

```
POST /api/integration/v1/entregas/{integration_id}/estado
```

**Header requerido:**
```
X-Integration-Key: <CULTIVO_CALLBACK_API_KEY>
```

**Body recibido desde APP-FAENA:**

```json
{
  "integration_id": "uuid-v4-de-este-evento-de-callback",
  "faena_entrega_id": 42,
  "estado": "RECIBIDA",
  "confirmado_en": "2026-05-04T14:22:00Z",
  "faena_log_id": 87,
  "pit_tags_recibidos": ["985141002345678", "985141002345679"],
  "pit_tags_rechazados": [],
  "motivo_rechazo": null,
  "detalle_rechazo": null
}
```

**O en caso de rechazo:**

```json
{
  "integration_id": "uuid-v4-de-este-evento-de-callback",
  "faena_entrega_id": 42,
  "estado": "RECHAZADA",
  "confirmado_en": "2026-05-04T14:22:00Z",
  "faena_log_id": null,
  "pit_tags_recibidos": [],
  "pit_tags_rechazados": ["985141002345678"],
  "motivo_rechazo": "TEMPERATURA_FUERA_RANGO",
  "detalle_rechazo": "Temperatura de llegada: 12°C. Límite: 6°C."
}
```

**Lógica que APP-CULTIVO debe ejecutar al recibir este callback:**
1. Verificar `X-Integration-Key` con `hmac.compare_digest`. Retornar `401` si falla.
2. Buscar el despacho por `integration_id` original (el que está en el campo `faena_entrega_id` correlacionado, o bien por búsqueda en tabla local).
3. Actualizar `faena_integration_status`, `faena_confirmado_en`, `faena_entrega_id` y campos de rechazo si aplica.
4. Retornar `200 { "ok": true }`.
5. Si `integration_id` de este callback ya fue procesado → retornar `200` sin re-ejecutar (idempotencia del callback).

**Respuesta esperada (siempre):**
```json
HTTP 200
{ "ok": true }
```

---

### 4.2 Códigos de Motivo de Rechazo que APP-CULTIVO debe poder almacenar

| Código | Descripción |
|--------|-------------|
| `TEMPERATURA_FUERA_RANGO` | Temperatura fuera del rango aceptado |
| `DOCUMENTO_FALTANTE` | Guía de despacho o garantía no presentada |
| `PIT_TAG_ILEGIBLE` | Uno o más pit_tags no pueden leerse |
| `DISCREPANCIA_PESO` | Peso recibido difiere >5% del declarado |
| `ANIMAL_MUERTO` | Uno o más animales llegaron sin vida |
| `OTRO` | Motivo libre en `detalle_rechazo` |

---

### 4.3 Códigos de Motivo de Anulación de Declaración

| Código | Descripción |
|--------|-------------|
| `ERROR_DATOS` | Datos incorrectos, se emite nueva declaración |
| `CADUCIDAD` | Declaración vencida antes de usarse |
| `CAMBIO_LOTE` | El lote cambió y los pit_tags ya no corresponden |
| `OTRO` | Motivo libre, requiere detalle obligatorio |

---

## 5. Llamadas que APP-CULTIVO Debe Hacer (Emisión)

### 5.1 Enviar Entrega de Peces

**Cuándo:** En el momento en que el operario confirma el despacho hacia planta en APP-CULTIVO (no antes).

**Destino:**
```
POST {FAENA_BASE_URL}/api/integration/v1/entregas
Header: X-Integration-Key: {FAENA_INTEGRATION_API_KEY}
Content-Type: application/json
```

**Body a enviar:**

```json
{
  "integration_id": "<uuid generado al crear el despacho>",
  "emitido_en": "<ISO 8601 UTC>",
  "centro_cultivo": {
    "id": "<id interno del centro>",
    "nombre": "<nombre del centro>"
  },
  "entrega": {
    "fecha_despacho": "<YYYY-MM-DD>",
    "guia_despacho": "<número de guía>",
    "transportista": "<nombre transportista o null>",
    "temperatura_despacho_c": "<float o null>",
    "peso_total_declarado_kg": "<float>",
    "observaciones": "<texto libre o null>"
  },
  "peces": [
    {
      "pit_tag": "<15 dígitos numéricos>",
      "especie": "<nombre científico o común>",
      "sexo": "<H|M|I>",
      "peso_vivo_kg": "<float>",
      "edad_meses": "<integer o null>",
      "lote_cultivo": "<código lote en APP-CULTIVO>",
      "estanque_origen": "<código estanque o null>",
      "ultimo_control_sanitario": "<YYYY-MM-DD o null>",
      "observaciones": "<texto libre o null>"
    }
  ]
}
```

**Reglas de formato críticas:**
- `integration_id` → UUID v4, **mismo valor siempre** para el mismo despacho (no generar uno nuevo en cada reintento).
- `pit_tag` → exactamente 15 dígitos numéricos, sin espacios ni guiones.
- `peso_vivo_kg` → en kilogramos con hasta 3 decimales. No en gramos.
- `emitido_en` → siempre en UTC, formato `YYYY-MM-DDTHH:MM:SSZ`.
- `peces` → lista no vacía. No enviar despacho sin peces.

**Manejo de respuesta:**

| HTTP | Acción en APP-CULTIVO |
|------|----------------------|
| 201 | Guardar `faena_entrega_id` del response, poner estado `ENVIADO` |
| 200 | Idempotente: despacho ya recibido. Sincronizar estado con `data.estado` |
| 422 | Guardar error en `integration_last_error`, notificar operario |
| 4xx/5xx | Guardar error, poner estado `ERROR_ENVIO`, habilitar reintento manual |
| Timeout / red | Guardar error, poner estado `ERROR_ENVIO`, NO cambiar `integration_id` |

---

### 5.2 Enviar Declaración de Cultivo

**Cuándo:** En el momento en que el médico veterinario o responsable firma/confirma la declaración en APP-CULTIVO.

**Destino:**
```
POST {FAENA_BASE_URL}/api/integration/v1/declaraciones
Header: X-Integration-Key: {FAENA_INTEGRATION_API_KEY}
Content-Type: application/json
```

**Body a enviar:**

```json
{
  "integration_id": "<uuid generado al crear la declaración>",
  "emitido_en": "<ISO 8601 UTC>",
  "centro_cultivo": {
    "id": "<id interno del centro>",
    "nombre": "<nombre del centro>"
  },
  "declaracion": {
    "numero_declaracion": "<folio único de la declaración>",
    "fecha": "<YYYY-MM-DD>",
    "vigencia_dias": "<integer 1-365>",
    "lote_cultivo": "<código lote>",
    "especie": "<nombre especie>",
    "kilos_totales": "<float>",
    "pit_tags_incluidos": ["<pit_tag_1>", "<pit_tag_2>"],
    "garantias": {
      "informe_sustancias_prohibidas": "<true|false>",
      "informe_farmacos": "<true|false>",
      "informacion_depuracion": "<true|false>",
      "informacion_trazabilidad": "<true|false>"
    },
    "firmante": {
      "nombre": "<nombre completo>",
      "cargo": "<cargo>",
      "rut": "<RUT formato XX.XXX.XXX-X o null>",
      "telefono": "<+56XXXXXXXXX o null>"
    },
    "estanques": ["<E-01>", "<E-02>"],
    "observaciones": "<texto libre o null>",
    "adjuntos": []
  }
}
```

**Manejo de respuesta:**

| HTTP | Acción en APP-CULTIVO |
|------|----------------------|
| 201, estado `VALIDADA` | Guardar `faena_declaracion_id`, `faena_valida_hasta`, poner estado `VALIDADA` |
| 201, estado `OBSERVADA` | Igual que arriba + guardar `faena_observaciones`, notificar responsable |
| 200 | Idempotente: sincronizar estado con `data.estado` |
| 422 | Guardar error, notificar responsable |
| 4xx/5xx / timeout | Guardar error, poner estado `ERROR_ENVIO`, habilitar reintento |

---

### 5.3 Anular una Declaración

**Cuándo:** Cuando el responsable decide invalidar una declaración ya enviada.

**Destino:**
```
POST {FAENA_BASE_URL}/api/integration/v1/declaraciones/{faena_declaracion_id}/anular
Header: X-Integration-Key: {FAENA_INTEGRATION_API_KEY}
Content-Type: application/json
```

**Body:**
```json
{
  "integration_id": "<uuid-v4 nuevo, para este evento de anulación>",
  "anulado_en": "<ISO 8601 UTC>",
  "motivo": "<código del §4.3>",
  "detalle": "<texto libre, obligatorio si motivo=OTRO>"
}
```

**Respuesta posible de rechazo (APP-FAENA puede bloquear la anulación):**
```json
HTTP 409
{
  "ok": false,
  "error": "ANULACION_BLOQUEADA",
  "message": "Declaración referenciada por faena activa #87. Contactar responsable antes de anular."
}
```
En este caso, APP-CULTIVO **no debe** marcar la declaración como anulada localmente. Debe mostrar el mensaje al usuario y esperar coordinación manual.

---

## 6. Tabla de Auditoría de Integración (recomendada)

Para trazabilidad y debugging, APP-CULTIVO debe mantener un log de todos los mensajes enviados y recibidos:

```
faena_integration_log
├── id                  PK
├── integration_id      UUID único por evento
├── flow                'entrega_peces' | 'declaracion_cultivo' | 'callback_entrega'
├── direction           'outbound' (APP-CULTIVO envía) | 'inbound' (callback recibido)
├── http_status         Código HTTP de la respuesta
├── request_body        JSON enviado (solo en outbound)
├── response_body       JSON recibido
├── error_message       Descripción del error si aplica
├── created_at          Timestamp del evento
└── local_record_id     ID del despacho o declaración local relacionado
```

---

## 7. Validaciones de Negocio que APP-CULTIVO Debe Garantizar Antes de Enviar

Estas validaciones deben ocurrir en APP-CULTIVO **antes** de llamar a APP-FAENA. Si no se cumplen, no enviar.

**Para entrega de peces:**
- [ ] Todos los `pit_tag` tienen exactamente 15 dígitos numéricos
- [ ] `peso_total_declarado_kg` > 0
- [ ] `fecha_despacho` no es futura más de 1 día calendario
- [ ] La lista de peces no está vacía
- [ ] Guía de despacho no está vacía

**Para declaración de cultivo:**
- [ ] `numero_declaracion` único dentro de APP-CULTIVO (no puede enviarse el mismo folio dos veces)
- [ ] `vigencia_dias` entre 1 y 365
- [ ] Al menos un campo de `garantias` es `true` (no se acepta declaración con todas en `false`)
- [ ] `pit_tags_incluidos` no está vacío
- [ ] `firmante.nombre` y `firmante.cargo` están presentes

---

## 8. Comportamiento ante Fallos de Red

APP-CULTIVO debe implementar una **cola de reintento** para mensajes en estado `ERROR_ENVIO`:

1. Reintentar máximo 3 veces con backoff exponencial: 1 min → 5 min → 30 min.
2. Si los 3 reintentos fallan, dejar en `ERROR_ENVIO` y notificar al operador (alerta en UI o email).
3. **Nunca cambiar el `integration_id`** entre reintentos del mismo mensaje.
4. Proveer un botón manual de "Reintentar envío" en la UI para que el operador resuelva manualmente.

---

## 9. Health Check — Verificación de Conectividad

APP-CULTIVO puede verificar que APP-FAENA está disponible antes de enviar:

```
GET {FAENA_BASE_URL}/api/integration/v1/health
(Sin header de autenticación)
```

Respuesta esperada:
```json
HTTP 200
{ "ok": true, "version": "v1", "timestamp": "2026-05-04T10:00:00Z" }
```

Si retorna algo distinto de `200`, no intentar enviar mensajes y mostrar alerta al operador.

---

## 10. Checklist de Entregables — APP-CULTIVO

Para considerarse lista para integración, APP-CULTIVO debe haber completado:

**Modelo de datos:**
- [ ] Campo `integration_id` en despachos de peces (UUID, generado al crear, inmutable)
- [ ] Campo `integration_id` en declaraciones de cultivo (UUID, generado al crear, inmutable)
- [ ] Campos de estado y resultado de integración en ambas tablas (§3.1 y §3.2)
- [ ] Tabla `faena_integration_log` (§6)

**Lógica de emisión:**
- [ ] Función `enviar_entrega_peces(despacho_id)` → POST a APP-FAENA + manejo de respuesta
- [ ] Función `enviar_declaracion_cultivo(declaracion_id)` → POST a APP-FAENA + manejo de respuesta
- [ ] Función `anular_declaracion(declaracion_id, motivo, detalle)` → POST a APP-FAENA
- [ ] Cola de reintento o mecanismo equivalente para `ERROR_ENVIO`

**Callbacks recibidos:**
- [ ] Endpoint `POST /api/integration/v1/entregas/{integration_id}/estado` implementado
- [ ] Autenticación del callback con `hmac.compare_digest` sobre `CULTIVO_CALLBACK_API_KEY`
- [ ] Idempotencia del callback (si el `integration_id` del evento ya fue procesado → `200` sin re-ejecutar)

**Configuración:**
- [ ] Variables de entorno `FAENA_INTEGRATION_API_KEY`, `FAENA_BASE_URL`, `CULTIVO_CALLBACK_API_KEY` documentadas y gestionadas por ambiente
- [ ] URL del callback de APP-CULTIVO comunicada al equipo de APP-FAENA (se configura como `CULTIVO_WEBHOOK_URL` en APP-FAENA)

**UI mínima:**
- [ ] Indicador de estado de integración visible en el detalle de cada despacho y declaración
- [ ] Botón "Reintentar envío" para registros en estado `ERROR_ENVIO`
- [ ] Mensaje claro si APP-FAENA bloquea una anulación

---

## 11. Coordinación con Equipo APP-FAENA

Antes de la primera prueba end-to-end, ambos equipos deben intercambiar:

| Dato | Dirección | Descripción |
|------|-----------|-------------|
| `FAENA_INTEGRATION_API_KEY` | APP-FAENA → APP-CULTIVO | Clave que APP-CULTIVO usará para llamar a APP-FAENA |
| `CULTIVO_CALLBACK_API_KEY` | APP-CULTIVO → APP-FAENA | Clave que APP-FAENA usará para llamar los callbacks de APP-CULTIVO |
| URL base APP-FAENA | APP-FAENA → APP-CULTIVO | Para configurar `FAENA_BASE_URL` |
| URL callback APP-CULTIVO | APP-CULTIVO → APP-FAENA | Para configurar `CULTIVO_WEBHOOK_URL` en APP-FAENA |
