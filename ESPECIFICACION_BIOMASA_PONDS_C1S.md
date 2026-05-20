# Especificacion tecnica: calculo de biomasa en vista ponds (foco C1S)

## 1) Objetivo
Describir, paso a paso, como se calcula y muestra la biomasa de un estanque en la vista de estanques, usando como ejemplo el estanque C1S - Central 1 Sur.

## 2) Punto de entrada de la vista
La vista HTML de estanques se arma en el endpoint:
- GET /views/ui/ponds
- Archivo: app/api/views.py

Esta ruta:
1. Consulta estanques desde tabla ponds.
2. Construye un resumen por estanque con _build_cached_pond_rows(...).
3. Renderiza la plantilla app/templates/ponds.html.

## 3) De donde sale biomasa mostrada en la tabla
En _build_cached_pond_rows(...), el campo mostrado como biomasa del estanque se toma de:
- pond.biomass_current

Regla:
- biomasa fila estanque = float(pond.biomass_current) si existe
- si no existe, se muestra vacio (None -> "-" en template)

En el template ponds.html se imprime con formato:
- 1 decimal en kg por fila (p.biomass, c.biomass, cu.biomass)

## 4) Como se calcula biomass_current (recálculo completo)
La funcion central es:
- _recalc_pond_biomass(pond_id, db)
- Archivo: app/api/views.py

### 4.1) Componentes que suma
A) Peces marcados (con PIT)
1. Obtiene peces activos en estanque (estado alive/depuration) con ultimo movimiento al estanque.
2. Para cada pez, toma ultimo peso individual de FishSampling.
3. Si no hay peso individual, usa fallback por _get_fish_weight_estimate(...).
4. Si el ultimo peso individual es viejo, aplica piso por promedio reciente de lote (floor) para no subestimar.
5. Suma en gramos.

B) Peces sin marcar (fish_id NULL)
1. Obtiene saldos actuales por lote con _get_unregistered_balances_by_lot(...).
2. Para cada lote, toma avg_weight desde pond_lot_stats.
3. Multiplica avg_weight (g) x cantidad (peces).
4. Suma en gramos.

### 4.2) Conversiones finales
- total_biomass_g = tagged_biomass_g + unregistered_biomass_g
- biomass_kg = round(total_biomass_g / 1000, 3)
- avg_weight_g = round(total_biomass_g / total_population, 3)

### 4.3) Campos que actualiza en ponds
_recalc_pond_biomass(...) escribe:
- pond.biomass = biomass_kg
- pond.biomass_measured = biomass_kg
- pond.biomass_current = biomass_kg
- pond.avg_weight = avg_weight_g
- ademas refresca caches de conteo/lotes: tagged_count, unregistered_count, n_fish_cached, etc.

Importante:
- este recálculo "resetea" biomass_current al valor calculado en ese momento.

## 5) Ajuste en movimientos (delta) antes de recálculo
Tambien existe ajuste incremental por movimiento:
- _adjust_biomass_on_movement(mov, db)
- Archivo: app/api/views.py

Comportamiento:
1. Movimiento de pez marcado:
   - resta peso estimado en origen
   - suma peso estimado en destino
2. Movimiento sin PIT:
   - usa avg_weight del lote
   - resta/suma N x avg_weight segun origen/destino
3. Nunca permite biomasa negativa (max(0, ...)).

Despues del movimiento, en varios flujos se llama de nuevo a _recalc_pond_biomass(...) para origen y destino, dejando consistencia final.

## 6) Cuando se dispara el recálculo
Principales gatillos observados:
1. Cierre de sesion de muestreo (_close_sampling_session -> _recalc_pond_biomass).
2. Registro de movimientos (tagged, untagged, masivos) en UI de movimientos.
3. Flujos de integracion y otros endpoints que actualizan cache/biomasa.

## 7) Densidad y agregados que ves en ponds
Para cada fila estanque:
- density = biomass_current / volume (kg/m3), redondeado 2 decimales si volumen > 0.

Para grupo padre-hijos y unidad de cultivo:
- biomasa agregada = suma de biomasa de filas hijas/miembros con valor no nulo
- densidad agregada = biomasa agregada / volumen agregado

## 8) Caso C1S - Central 1 Sur
No existe una regla hardcodeada por nombre "C1S" en el codigo:
- C1S se procesa igual que cualquier estanque, por pond.id.

Por lo tanto, si C1S llama la atencion, normalmente es por datos (no por logica especial), por ejemplo:
1. Pesos individuales antiguos o faltantes en peces marcados.
2. Avg_weight de lotes sin PIT en pond_lot_stats.
3. Saldos de movimientos sin PIT (entradas - salidas) por lote.
4. Recálculo reciente que haya reseteado biomass_current.
5. Volumen del estanque (impacta densidad mostrada, no biomasa).

## 9) Checklist para auditar C1S paso a paso
1. Identificar pond.id de C1S en tabla ponds.
2. Revisar ponds.biomass_current, ponds.avg_weight, ponds.n_fish_cached.
3. Listar peces con PIT activos cuyo ultimo movimiento termina en C1S.
4. Para esos peces, revisar ultimo FishSampling.weight y fecha.
5. Revisar saldos sin PIT por lote en C1S (entradas - salidas).
6. Revisar pond_lot_stats.avg_weight por esos lotes en C1S.
7. Recalcular manualmente:
   - biomasa_tagged_g + biomasa_untagged_g
   - /1000 -> kg
8. Comparar con ponds.biomass_current.
9. Verificar valor final en /views/ui/ponds (columna Biomasa).

## 10) Resumen corto
- Lo que se muestra en ponds viene de ponds.biomass_current.
- biomass_current se calcula desde peces marcados + no marcados con pesos de muestreo/estadistica.
- Movimientos ajustan biomasa en delta y luego suele haber recálculo completo.
- C1S no tiene logica especial: si se ve raro, revisar datos de muestreo, stats por lote y movimientos.
