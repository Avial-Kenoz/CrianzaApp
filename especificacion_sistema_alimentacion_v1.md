# Especificacion Sistema de Alimentacion - CrianzaApp (v1)

Fecha: 2026-05-06
Estado: Borrador para revision funcional
Objetivo: Definir alcance funcional y tecnico antes de implementar cambios mayores.

## 1. Objetivo del modulo

Implementar un modulo de alimentacion con 3 capacidades principales:

1. Carga de alimento a inventario.
2. Programa de alimentacion semanal por estanque con reserva y posterior confirmacion de retiro/uso.
3. Cuadres manuales de inventario por tipo de alimento.

Adicionalmente, dejar preparada la integracion futura con Contabilidad de PlantaApp.

## 2. Alcance v1

Incluye:

1. Ingreso de lotes de alimento con validaciones.
2. Inventario disponible/planificado/ejecutado por tipo de alimento.
3. Planificacion por estanque y tipo de alimento.
4. Confirmaciones parciales y multiples sobre una planificacion.
5. Cierre automatico de planificaciones segun reglas.
6. Consulta de historico de planes (solo ejecutado).
7. Cuadres manuales con comentario obligatorio.
8. Registro de eventos para integracion futura con Contabilidad.

No incluye en v1:

1. Envio real a API de PlantaApp Contabilidad (solo outbox/payload preparado).
2. Costeo contable avanzado (promedios, valorizacion IFRS, etc.).
3. Integraciones con sensores o pesaje automatico.

## 3. Reglas de negocio clave

### 3.1 Carga de alimento

Campos obligatorios:

1. guia_despacho (cabecera)
2. fecha_ingreso (cabecera)
3. detalle_productos (1..N lineas)

Reglas:

1. peso_saco solo admite 20 o 25.
2. guia_despacho debe ser unica globalmente a nivel cabecera de recepcion.
3. cantidad_kg debe ser positiva.
4. cantidad_kg debe ser multiplo de peso_saco para derivar cantidad_sacos exacta.
5. Una guia puede contener multiples productos/tipos en lineas de detalle.
6. Cada linea valida consistencia propia (cantidad_kg, peso_saco, cantidad_sacos).
7. Si la guia existe en borrador, se permite agregar/editar lineas.
8. Si la guia esta confirmada, no se edita; se corrige con ajuste o documento compensatorio.
9. Cada ingreso confirmado deja sacos en estado disponible.

### 3.2 Programa de alimentacion (planificacion)

1. Se crea una planificacion semanal (header).
2. En matriz por estanque x tipo_alimento se registran sacos planificados.
3. Se permite mezclar tipos de alimento en un mismo estanque.
4. Al guardar, lo ingresado queda en estado planificado (reserva).
5. La planificacion no considera sub-estanques; opera solo con estanques padre.

Validacion de stock:

1. Por tipo_alimento: planificado_actual + solicitado_nuevo <= stock_real.
2. Si no cumple, rechazar guardado de esa operacion.
3. El formulario no debe perder lo que el usuario ingreso (persistencia en UI tras error).

### 3.3 Confirmacion de ejecucion

1. Vista similar a planificacion, pero con boton Confirmar.
2. Precarga desde la ultima programacion abierta, descontando confirmaciones historicas.
3. Confirmacion por estanque y tipo de alimento.
4. Permite confirmaciones parciales en multiples tandas.
5. Si el usuario edita el numero, se confirma ese valor contra saldo pendiente.
6. Debe permitir confirmar sacos no planificados (consumo ad-hoc) por estanque y tipo.
7. La confirmacion no planificada descuenta directamente desde stock disponible.
8. Si no hay stock disponible suficiente, la confirmacion se rechaza.
9. La confirmacion no considera sub-estanques; opera solo con estanques padre.

Estados de sacos por tipo:

1. disponible
2. planificado
3. ejecutado

Transiciones:

1. disponible -> planificado (guardar plan)
2. planificado -> ejecutado (confirmar retiro)
3. planificado -> disponible (al crear nuevo programa)
4. disponible -> ejecutado (confirmacion no planificada)

### 3.4 Cierre automatico de programa

Un programa se cierra automaticamente cuando se cumple cualquiera:

1. Se agota completamente su stock planificado (saldo planificado = 0), o
2. Se crea una nueva planificacion.

Al crear nueva planificacion:

1. Todo saldo planificado no ejecutado del programa previo vuelve a disponible.
2. El programa previo queda cerrado.

### 3.5 Historico

1. Debe permitirse recuperar programas anteriores.
2. En historico se muestra solo la parte ejecutada.

### 3.6 Cuadres de inventario

1. Ingreso manual por tipo de alimento: cantidad_real_sacos.
2. Sistema calcula diferencia vs stock_calculado.
3. Comentario obligatorio.
4. Ajuste impacta balance principal de inventario.
5. Queda traza auditable (quien, cuando, antes/despues, comentario).

### 3.7 Asignacion de alimento por lote en estanque

1. En cada confirmacion de alimento por estanque, se debe asignar consumo a lotes presentes en ese estanque.
2. Prioridad 1 (siempre): prorrateo por biomasa estimada por lote a la fecha de confirmacion.
3. Prioridad 2 (solo contingencia): si no existe biomasa estimada por lote, prorrateo por numero de peces por lote.
4. Solo participan lotes activos en el estanque padre a la fecha de confirmacion.
5. La suma asignada a lotes debe cuadrar exactamente con el total confirmado del estanque.
6. Debe registrarse metodo de asignacion usado (biomass o fish_count) para trazabilidad.

Formula base (regla general):

1. proporcion_lote = biomasa_lote_est / biomasa_total_estanque
2. alimento_lote = alimento_confirmado_estanque * proporcion_lote

Contingencia (solo si no existe biomasa estimada):

1. proporcion_lote = n_peces_lote / n_peces_total_estanque
2. alimento_lote = alimento_confirmado_estanque * proporcion_lote

## 4. Modelo de datos propuesto (v1)

## 4.1 feed_types

Catalogo de tipos de alimento.

Campos sugeridos:

1. id
2. name (unico)
3. active
4. created_at
5. updated_at

## 4.2 feed_receipts

Cabecera de recepcion por guia de despacho.

Campos sugeridos:

1. id
2. guia_despacho (unico)
3. supplier_name (opcional v1)
4. fecha_ingreso
5. status (draft, confirmed, canceled)
6. created_by
7. created_at
8. confirmed_at

## 4.3 feed_receipt_lines

Detalle de productos por guia (1..N lineas por cabecera).

Campos sugeridos:

1. id
2. feed_receipt_id
3. feed_type_id
4. quantity_kg
5. bag_weight_kg (20 o 25)
6. quantity_bags (derivado y almacenado)
7. supplier_lot (opcional)
8. expiration_date (opcional)
9. created_at

Unicidad recomendada:

1. (feed_receipt_id, feed_type_id, bag_weight_kg, supplier_lot)

## 4.4 feed_programs

Cabecera de planificacion semanal.

Campos sugeridos:

1. id
2. program_code (unico)
3. week_start_date
4. week_end_date
5. status (open, closed)
6. closed_reason (depleted, superseded)
7. created_by
8. created_at
9. closed_at

## 4.5 feed_program_lines

Detalle planificado por estanque y alimento.

Campos sugeridos:

1. id
2. feed_program_id
3. pond_id
4. feed_type_id
5. planned_bags
6. planned_kg
7. executed_bags (acumulado)
8. executed_kg (acumulado)
9. remaining_bags
10. remaining_kg
11. created_at
12. updated_at

Unicidad recomendada:

1. (feed_program_id, pond_id, feed_type_id)

## 4.6 feed_execution_events

Eventos de confirmacion (permite multiples confirmaciones parciales).

Campos sugeridos:

1. id
2. feed_program_line_id
3. confirmed_bags
4. confirmed_kg
5. confirmed_at
6. confirmed_by
7. note (opcional)

## 4.7 feed_inventory_adjustments

Cuadres manuales.

Campos sugeridos:

1. id
2. feed_type_id
3. stock_calculated_bags
4. stock_real_bags
5. difference_bags
6. comment (obligatorio)
7. created_by
8. created_at

## 4.8 feed_stock_ledger

Libro mayor de movimientos de stock (auditoria y conciliacion).

Campos sugeridos:

1. id
2. feed_type_id
3. movement_type (entry, reserve, execute, release, adjustment)
4. bags_delta
5. kg_delta
6. source_table
7. source_id
8. created_at
9. created_by

## 4.9 accounting_outbox_events

Outbox para integracion futura con PlantaApp Contabilidad.

Campos sugeridos:

1. id
2. event_type
3. payload_json
4. status (pending, sent, error)
5. retry_count
6. created_at
7. sent_at
8. last_error

Eventos iniciales a emitir:

1. feed_entry_created
2. feed_adjustment_created
3. feed_execution_confirmed

## 4.10 feed_execution_lot_allocations

Asignacion de consumo confirmado por lote dentro de cada estanque.

Campos sugeridos:

1. id
2. feed_execution_event_id
3. lot_id
4. allocated_bags
5. allocated_kg
6. allocation_method (biomass, fish_count)
7. biomass_estimated_kg (opcional)
8. fish_count_estimated (opcional)
9. created_at

## 5. Vistas/UI

### 5.1 Carga de alimento

Formulario simple con validaciones inmediatas y mensajes claros.

Campos:

1. Guia despacho (cabecera)
2. Fecha ingreso (cabecera)
3. Tabla de lineas de productos:
4. Tipo alimento (selector)
5. Cantidad kg
6. Peso saco (20/25)
7. Cantidad sacos (derivada o editable con recalculo)
8. Agregar linea
9. Guardar guia

### 5.2 Programa de alimentacion - Planificar

Vista tipo tabla, inspirada en Estanques general.

Encabezado fijo:

1. Estanque
2. Biomasa
3. Alimento 1
4. Alimento 2
5. ...
6. Alimento N
7. Total kg alimento
8. Accion

Filas:

1. Fila 1: disponibilidad de alimento por tipo (sacos), actualizada en vivo.
2. Filas de estanques: inputs de sacos por tipo, total kg calculado, boton Guardar.
3. Ultima fila: total de sacos programados por columna/tipo.
4. No se muestran ni editan sub-estanques en esta vista.

Comportamiento de guardado:

1. Validar stock antes de persistir.
2. Si falla, mostrar error por columna/tipo y mantener valores ingresados.
3. Si ok, reservar en planificacion.

### 5.3 Programa de alimentacion - Confirmar

Misma matriz base, pero con:

1. Datos precargados desde ultima programacion abierta menos lo ya ejecutado.
2. Boton Confirmar por fila (estanque).
3. Permitir editar cantidades antes de confirmar.
4. Permitir confirmar en multiples instancias.
5. Permitir ingresar y confirmar cantidades no planificadas por tipo de alimento.
6. Diferenciar visualmente lo confirmado desde plan vs lo no planificado.
7. No se muestran ni editan sub-estanques en esta vista.

### 5.4 Historico de programas

1. Lista de programas cerrados y abiertos.
2. Detalle por programa mostrando solo ejecutado para historico.
3. Filtros por semana, fecha, estado.

### 5.5 Cuadres de inventario

Formulario por tipo de alimento:

1. Stock calculado (solo lectura)
2. Cantidad real (sacos)
3. Diferencia (calculada)
4. Comentario obligatorio
5. Confirmar ajuste

## 6. API interna propuesta (v1)

1. GET /views/ui/feed/entries/new
2. POST /views/ui/feed/entries (crea/actualiza cabecera en borrador)
3. POST /views/ui/feed/entries/{id}/lines
4. POST /views/ui/feed/entries/{id}/confirm
5. GET /views/ui/feed/programs
6. POST /views/ui/feed/programs (crear nuevo programa)
7. POST /views/ui/feed/programs/{id}/plan
8. POST /views/ui/feed/programs/{id}/confirm
9. GET /views/ui/feed/programs/{id}/history
10. GET /views/ui/feed/adjustments
11. POST /views/ui/feed/adjustments

## 7. Validaciones y concurrencia

1. Validar unicidad guia_despacho en DB.
2. Permitir multiples lineas por guia con transaccion unica al confirmar.
3. En planificacion/confirmacion usar transacciones y bloqueo optimista o FOR UPDATE por feed_type.
4. Evitar sobre-reserva por operaciones concurrentes.
5. Todas las operaciones de stock deben escribir en ledger.
6. En confirmacion no planificada validar disponible_actual >= confirmado_no_planificado.
7. En asignacion por lote, usar biomasa como regla general aun cuando la biomasa disponible no sea reciente.
8. Usar numero de peces solo si no existe biomasa estimada por lote en el estanque.
9. En cada confirmacion, validar cuadratura: suma_asignada_lotes = total_confirmado_estanque.

## 8. Criterios de aceptacion (MVP)

1. No se puede ingresar alimento con peso_saco distinto de 20/25.
2. No se puede repetir guia_despacho a nivel cabecera.
3. Una misma guia permite multiples lineas de productos.
4. Planificacion rechaza sobre-reserva y conserva inputs en UI.
5. Confirmacion parcial descuenta correctamente saldo pendiente.
6. Confirmacion permite registrar sacos no planificados si existe stock disponible.
7. Confirmacion no planificada rechaza la operacion cuando no hay stock suficiente.
8. Nueva planificacion libera planificado no ejecutado previo.
9. Cuadre manual exige comentario y ajusta stock.
10. Historico permite ver planes anteriores y ejecutado.
11. Outbox genera eventos para integracion futura.
12. Programacion y confirmacion excluyen sub-estanques y operan solo con estanques padre.
13. Confirmacion asigna alimento a lotes por biomasa como criterio principal, incluso con biomasa no reciente.
14. El sistema usa numero de peces solo cuando no existe biomasa estimada por lote.
15. Cada confirmacion deja trazabilidad del metodo de asignacion por lote y cumple cuadratura con el total del estanque.

## 9. Plan de implementacion sugerido

Fase 1:

1. Modelo de datos + migraciones.
2. Carga de alimento + inventario base.
3. Ledger + outbox basico.

Fase 2:

1. Planificacion semanal (UI matriz + validaciones).
2. Reserva de stock y cierres automaticos por nueva planificacion.

Fase 3:

1. Confirmaciones parciales y multiples.
2. Cierre por agotamiento de planificado.

Fase 4:

1. Cuadres manuales.
2. Historico y reportes.
3. Hardening de concurrencia y pruebas.

## 10. Preguntas para cerrar antes de codificar

1. La planificacion es semanal fija (lun-dom) o rango libre?
2. La confirmacion se hara diaria o sin restriccion de fecha?
3. Se requiere bloquear confirmaciones retroactivas?
4. La unidad oficial de entrada es kg o sacos? (en v1 se propone sacos+kg derivado)
5. Se necesita costo por alimento en v1 para reportes?
6. El modulo debe operar con permisos por rol desde el inicio?
