"""
Atribuye el consumo mensual de alimento por estanque a los lotes presentes,
usando un modelo de intervalos ponderados por dias.

Logica central:
  Para cada (pond, mes), se obtiene la secuencia de checkpoints del estanque
  que solapan con el mes (extendida ±6 meses para cubrir gaps).
  El mes se divide en sub-intervalos definidos por esos checkpoints.
  Cada sub-intervalo recibe feed proporcional a sus dias, y se atribuye
  a los lotes segun las shares de biomasa del checkpoint que lo define.
  Esto maneja los tres casos posibles (mes contenido, checkpoint contenido,
  y cruce de boundary) de forma uniforme sin interpolacion.

Regla para estanques padre (sin checkpoints propios):
  Se usan los checkpoints de los estanques hijo, agrupando por lote.

Fuentes de alimento:
  - feed_monthly_consumption_legacy  -> source = 'legacy'
  - feed_execution_events (agrupado) -> source = 'app'

Idempotente: borra y reconstruye cada (year, month, source).

Uso:
    python -m scripts.build_feed_lot_monthly_allocations
"""
import sys
import os
import calendar
from collections import defaultdict
from datetime import datetime, date, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(_ROOT, ".env"), override=True)

from sqlalchemy import text
from app.db.session import SessionLocal


def _child_ponds(db) -> dict[int, list[int]]:
    """Devuelve {parent_pond_id: [child_pond_id, ...]}."""
    rows = db.execute(text(
        "SELECT id, parent_pond_id FROM ponds WHERE parent_pond_id IS NOT NULL"
    )).fetchall()
    result: dict[int, list[int]] = defaultdict(list)
    for child_id, parent_id in rows:
        result[int(parent_id)].append(int(child_id))
    return result


def _fetch_checkpoint_shares(
    db,
    pond_ids: list[int],
    from_date: date,
    to_date: date,
) -> dict[int, list[tuple[date, dict[int, float]]]]:
    """
    Para cada pond, retorna lista ordenada de (checkpoint_date, {lot_id: share})
    donde share = biomasa_lote / biomasa_total_estanque en esa fecha.

    Usa todos los tipos de checkpoint (month_start, sampling, fish_sampling).
    Si una fecha tiene multiples checkpoints del mismo tipo para el mismo (pond, lot),
    se promedia la biomasa (raro pero defensivo).
    """
    if not pond_ids:
        return {}

    rows = db.execute(text("""
        SELECT pond_id, checkpoint_date, lot_id, AVG(biomass_kg) AS biomass_kg
        FROM biomass_checkpoints
        WHERE pond_id = ANY(:pids)
          AND checkpoint_date BETWEEN :from_d AND :to_d
          AND fish_count > 0
          AND biomass_kg > 0
          AND avg_weight_g IS NOT NULL
        GROUP BY pond_id, checkpoint_date, lot_id
        ORDER BY pond_id, checkpoint_date, lot_id
    """), {"pids": pond_ids, "from_d": from_date, "to_d": to_date}).fetchall()

    # Agrupar por (pond, date) -> {lot: biomass}
    pond_date_bm: dict[tuple[int, date], dict[int, float]] = defaultdict(dict)
    for pond_id, ck_date, lot_id, biomass_kg in rows:
        pond_date_bm[(int(pond_id), ck_date)][int(lot_id)] = float(biomass_kg)

    # Calcular shares y organizar por pond
    result: dict[int, list[tuple[date, dict[int, float]]]] = defaultdict(list)
    for (pond_id, ck_date), lot_bm in pond_date_bm.items():
        total = sum(lot_bm.values())
        if total > 0:
            shares = {lot_id: bm / total for lot_id, bm in lot_bm.items()}
            result[pond_id].append((ck_date, shares))

    for pond_id in result:
        result[pond_id].sort(key=lambda x: x[0])

    return result


def _day_weighted_lots(
    feed_kg: float,
    month_start: date,
    month_end: date,
    checkpoints: list[tuple[date, dict[int, float]]],
) -> dict[int, float]:
    """
    Atribuye feed_kg a lotes usando intervalos ponderados por dias.

    Los checkpoints definen intervalos: cada checkpoint es valido desde
    su fecha hasta el dia anterior al siguiente checkpoint.

    Si el mes empieza antes del primer checkpoint, se usa el primer
    checkpoint para esa fraccion inicial (extrapolacion hacia atras).
    Si el mes termina despues del ultimo checkpoint, el ultimo se extiende
    (extrapolacion hacia adelante, ya capturada por el ultimo intervalo).
    """
    if not checkpoints:
        return {}

    total_days = (month_end - month_start).days + 1
    lot_totals: dict[int, float] = defaultdict(float)

    # Construir intervalos [periodo_start, periodo_end, shares]
    intervals: list[tuple[date, date, dict[int, float]]] = []

    # Fraccion antes del primer checkpoint -> usar shares del primero
    if month_start < checkpoints[0][0]:
        pre_end = min(month_end, checkpoints[0][0] - timedelta(days=1))
        intervals.append((month_start, pre_end, checkpoints[0][1]))

    for i, (ck_date, shares) in enumerate(checkpoints):
        next_date = checkpoints[i + 1][0] if i + 1 < len(checkpoints) else month_end + timedelta(days=1)
        period_start = max(month_start, ck_date)
        period_end   = min(month_end,   next_date - timedelta(days=1))
        if period_start <= period_end:
            intervals.append((period_start, period_end, shares))

    for period_start, period_end, shares in intervals:
        overlap_start = max(month_start, period_start)
        overlap_end   = min(month_end,   period_end)
        if overlap_start > overlap_end:
            continue
        overlap_days = (overlap_end - overlap_start).days + 1
        overlap_feed = feed_kg * overlap_days / total_days
        for lot_id, share in shares.items():
            lot_totals[lot_id] += overlap_feed * share

    return dict(lot_totals)


def _get_pond_lots_for_month(
    db,
    year: int,
    month: int,
    children: dict[int, list[int]],
) -> dict[int, dict[int, float]]:
    """
    Para cada pond con alimento ese mes, retorna {lot_id: feed_fraction}.
    La fraccion es 1.0 si el lote recibe todo el alimento; en ponds con
    multiples lotes, la suma de fracciones = 1.0.

    Retorna {pond_id: {lot_id: share_total_mes}} — sin multiplicar por feed_kg todavia.
    """
    last_day   = calendar.monthrange(year, month)[1]
    month_start = date(year, month, 1)
    month_end   = date(year, month, last_day)

    # Ventana de busqueda: 6 meses antes y despues del mes
    window_from = month_start - timedelta(days=180)
    window_to   = month_end   + timedelta(days=180)

    # Ponds con alimento ese mes
    feed_ponds_rows = db.execute(text("""
        SELECT DISTINCT pond_id FROM feed_monthly_consumption_legacy
        WHERE year = :y AND month = :m AND pond_id IS NOT NULL
        UNION
        SELECT DISTINCT pond_id FROM feed_execution_events
        WHERE EXTRACT(year FROM confirmed_at) = :y
          AND EXTRACT(month FROM confirmed_at) = :m
          AND pond_id IS NOT NULL
    """), {"y": year, "m": month}).fetchall()

    feed_pond_ids = [int(r[0]) for r in feed_ponds_rows]

    # Ponds a consultar en checkpoints: propios + hijos posibles
    all_query_ponds: set[int] = set()
    for pid in feed_pond_ids:
        all_query_ponds.add(pid)
        all_query_ponds.update(children.get(pid, []))

    ck_shares = _fetch_checkpoint_shares(db, list(all_query_ponds), window_from, window_to)

    result: dict[int, dict[int, float]] = {}

    for feed_pond_id in feed_pond_ids:
        # Checkpoints propios primero; si no hay, usar hijos agregados
        own_cks = ck_shares.get(feed_pond_id, [])

        if own_cks:
            checkpoints = own_cks
        else:
            # Agregar hijos: unir todas las fechas y sumar biomasa por lote
            child_ids = children.get(feed_pond_id, [])
            if not child_ids:
                continue

            # Recolectar todos los (fecha, {lot: biomasa}) de los hijos
            all_dates: set[date] = set()
            for cid in child_ids:
                for ck_date, _ in ck_shares.get(cid, []):
                    all_dates.add(ck_date)

            if not all_dates:
                continue

            # Para cada fecha, sumar biomasa de todos los hijos usando
            # el checkpoint mas reciente de cada hijo <= esa fecha
            agg_checkpoints: list[tuple[date, dict[int, float]]] = []
            for ck_date in sorted(all_dates):
                combined_bm: dict[int, float] = defaultdict(float)
                for cid in child_ids:
                    child_cks = ck_shares.get(cid, [])
                    # Mas reciente <= ck_date
                    valid = [(d, s) for d, s in child_cks if d <= ck_date]
                    if valid:
                        _, shares = valid[-1]
                        # Reconvertir share -> biomasa necesitaria el total;
                        # usamos shares directamente si solo hay un hijo por lote
                        # En este contexto las shares ya son proporcionales,
                        # sumamos como si fueran biomasa relativa
                        for lot_id, sh in shares.items():
                            combined_bm[lot_id] += sh

                total = sum(combined_bm.values())
                if total > 0:
                    agg_checkpoints.append((
                        ck_date,
                        {lid: bm / total for lid, bm in combined_bm.items()},
                    ))

            checkpoints = agg_checkpoints

        if not checkpoints:
            continue

        # Calcular la contribucion ponderada por dias de cada checkpoint
        # Retornamos shares medias ponderadas (no el feed aun)
        lot_shares = _day_weighted_lots(1.0, month_start, month_end, checkpoints)

        if lot_shares:
            result[feed_pond_id] = lot_shares

    return result


def _allocate_source(
    db,
    year: int,
    month: int,
    source: str,
    now: datetime,
    pond_lot_shares: dict[int, dict[int, float]],
) -> tuple[int, int, float]:
    """Genera e inserta asignaciones para (year, month, source)."""
    if source == "legacy":
        rows = db.execute(text("""
            SELECT pond_id, SUM(consumed_kg) AS total_kg
            FROM feed_monthly_consumption_legacy
            WHERE year = :y AND month = :m AND pond_id IS NOT NULL
            GROUP BY pond_id
        """), {"y": year, "m": month}).fetchall()
    else:
        rows = db.execute(text("""
            SELECT pond_id, SUM(confirmed_kg) AS total_kg
            FROM feed_execution_events
            WHERE EXTRACT(year FROM confirmed_at) = :y
              AND EXTRACT(month FROM confirmed_at) = :m
              AND pond_id IS NOT NULL
            GROUP BY pond_id
        """), {"y": year, "m": month}).fetchall()

    db.execute(text("""
        DELETE FROM feed_lot_monthly_allocations
        WHERE year = :y AND month = :m AND source = :src
    """), {"y": year, "m": month, "src": source})

    inserted = 0
    without_ck = 0
    kg_total = 0.0

    for pond_id, total_kg in rows:
        pond_id  = int(pond_id)
        lot_shares = pond_lot_shares.get(pond_id)

        if not lot_shares:
            without_ck += 1
            continue

        pond_bm_proxy = sum(lot_shares.values())  # = 1.0 por construccion

        for lot_id, share in lot_shares.items():
            allocated = float(total_kg) * share

            db.execute(text("""
                INSERT INTO feed_lot_monthly_allocations
                  (year, month, pond_id, lot_id, source,
                   consumed_kg_pond, lot_biomass_kg, pond_biomass_kg,
                   lot_share, allocated_kg, checkpoint_date, computed_at)
                VALUES
                  (:y, :m, :pid, :lid, :src,
                   :cpond, :lbm, :pbm,
                   :share, :alloc, :ckd, :now)
                ON CONFLICT ON CONSTRAINT uq_feed_lot_monthly_alloc
                DO UPDATE SET
                  consumed_kg_pond = EXCLUDED.consumed_kg_pond,
                  lot_biomass_kg   = EXCLUDED.lot_biomass_kg,
                  pond_biomass_kg  = EXCLUDED.pond_biomass_kg,
                  lot_share        = EXCLUDED.lot_share,
                  allocated_kg     = EXCLUDED.allocated_kg,
                  checkpoint_date  = EXCLUDED.checkpoint_date,
                  computed_at      = EXCLUDED.computed_at
            """), {
                "y": year, "m": month, "pid": pond_id, "lid": lot_id, "src": source,
                "cpond": float(total_kg),
                "lbm": round(share, 6),        # share relativa (no biomasa absoluta)
                "pbm": round(pond_bm_proxy, 6),
                "share": round(share, 6),
                "alloc": round(allocated, 3),
                "ckd": None,  # ponderado por dias, sin checkpoint unico
                "now": now,
            })
            inserted += 1
            kg_total += allocated

    return inserted, without_ck, kg_total


def run():
    db = SessionLocal()
    try:
        children = _child_ponds(db)
        print(f"Ponds padre: {len(children)} ({sum(len(v) for v in children.values())} hijos)")

        periods_legacy = set(db.execute(text("""
            SELECT DISTINCT year, month FROM feed_monthly_consumption_legacy
            WHERE pond_id IS NOT NULL
        """)).fetchall())

        periods_app = set(db.execute(text("""
            SELECT DISTINCT EXTRACT(year FROM confirmed_at)::int,
                            EXTRACT(month FROM confirmed_at)::int
            FROM feed_execution_events
            WHERE confirmed_at IS NOT NULL AND pond_id IS NOT NULL
        """)).fetchall())

        all_periods = sorted(periods_legacy | periods_app)
        print(f"Periodos: {len(all_periods)} ({all_periods[0]} a {all_periods[-1]})")

        now = datetime.now()
        total_inserted = 0
        total_without  = 0
        total_kg       = 0.0

        for year, month in all_periods:
            pond_lot_shares = _get_pond_lots_for_month(db, year, month, children)

            sources = []
            if (year, month) in periods_legacy:
                sources.append("legacy")
            if (year, month) in periods_app:
                sources.append("app")

            period_ins = 0
            for source in sources:
                ins, no_ck, kg = _allocate_source(db, year, month, source, now, pond_lot_shares)
                period_ins += ins
                total_without += no_ck
                total_kg += kg

            db.commit()
            total_inserted += period_ins
            print(f"  OK {year}-{month:02d}: {len(pond_lot_shares)} estanques, {period_ins} asignaciones")

        print(f"\nTotal asignaciones : {total_inserted}")
        print(f"Sin checkpoint     : {total_without}")
        print(f"Kg atribuidos      : {total_kg:,.1f}")

    except Exception as e:
        db.rollback()
        print(f"\nERROR: {e}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    run()
