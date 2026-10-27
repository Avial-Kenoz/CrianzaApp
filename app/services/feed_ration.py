"""Dieta teórica según las fichas Aller (kg de alimento por 100 kg de peces por día).

Fuente: fichas técnicas de Aller Aqua para esturión, sección "Niveles de
alimentación recomendados" (tablas de peso del pez × temperatura del agua):

  Progress EX           1990-es-9.pdf  (25-03-2026)
  Reproductor (Rep) EX  1310-es-9.pdf  (30-04-2025)  solo 8 mm
  Thalassa EX GR        1030-es-9.pdf  (25-03-2026)
  Infa EX GR            510-es-9.pdf   (23-03-2026)
  https://stalleraquaprod.blob.core.windows.net/datasheets-pdf/<n>-es-9.pdf

Reglas para lo que la ficha no cubre (acordadas 16-10-2026):
  - Reproductor EX 6.0 no tiene ficha: se usa la de 8 mm.
  - Pez más pesado que la última banda (las tablas terminan en 7 kg y hay
    carriles de 10-21 kg): se usa la última banda y se marca "extrapolado".
  - Pez más liviano que la primera banda: la primera banda, también marcada.
  - Temperatura entre columnas: interpolación lineal. Fuera de 10-26 °C: la
    columna del extremo, marcada.

La tabla que manda es la del alimento que recibe el carril, no la del peso:
para el mismo pez Reproductor recomienda la mitad que Progress.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.biofilter_readings import BiofilterReading
from app.models.feed import FeedExecutionEvent, FeedType
from app.models.pond_oxygen_readings import PondOxygenReading
from app.models.ponds import Pond

TEMPS_C = (10, 12, 14, 16, 18, 20, 22, 24, 26)

# (peso_min_g, peso_max_g, calibre, % del peso corporal por día en cada TEMPS_C)
# Las bandas se leen [min, max): un pez de 100 g cae en 100-200.
ALLER_TABLES: dict[str, dict] = {
    "progress": {
        "label": "Aller Progress EX",
        "ficha": "1990-es-9.pdf (25-03-2026)",
        "bands": [
            (50, 100, "3 mm",     (0.19, 0.65, 1.12, 1.50, 1.87, 1.68, 1.50, 0.93, 0.19)),
            (100, 200, "3 mm",    (0.16, 0.54, 0.93, 1.24, 1.55, 1.40, 1.24, 0.78, 0.15)),
            (200, 800, "4.5 mm",  (0.13, 0.45, 0.77, 1.03, 1.29, 1.16, 1.03, 0.64, 0.13)),
            (800, 1500, "4.5 mm", (0.11, 0.37, 0.64, 0.85, 1.07, 0.96, 0.85, 0.53, 0.11)),
            (1500, 3000, "6 mm",  (0.09, 0.31, 0.53, 0.71, 0.89, 0.80, 0.71, 0.44, 0.09)),
            (3000, 4000, "6 mm",  (0.07, 0.26, 0.44, 0.59, 0.74, 0.66, 0.59, 0.37, 0.07)),
            (4000, 7000, "8 mm",  (0.06, 0.22, 0.37, 0.50, 0.62, 0.56, 0.50, 0.31, 0.06)),
        ],
    },
    "reproductor": {
        "label": "Aller Sturgeon Rep EX",
        "ficha": "1310-es-9.pdf (30-04-2025)",
        "bands": [
            (4000, 7000, "8 mm",  (0.03, 0.12, 0.20, 0.27, 0.33, 0.30, 0.27, 0.17, 0.03)),
        ],
    },
    "thalassa": {
        "label": "Aller Thalassa EX GR",
        "ficha": "1030-es-9.pdf (25-03-2026)",
        "bands": [
            (1, 2, "0.5-1.0 mm",  (0.70, 2.47, 4.23, 5.64, 7.04, 6.34, 5.64, 3.52, 0.70)),
            (2, 5, "0.9-1.6 mm",  (0.59, 2.05, 3.52, 4.70, 5.87, 5.28, 4.70, 2.94, 0.59)),
            (5, 10, "1.3-2.0 mm", (0.47, 1.66, 2.85, 3.80, 4.75, 4.27, 3.80, 2.37, 0.47)),
        ],
    },
    "infa": {
        "label": "Aller Infa EX GR",
        "ficha": "510-es-9.pdf (23-03-2026)",
        # La fila de 0.1 mm viene en 0 en la ficha (alimento de inicio, sin
        # ración por biomasa) y se omite.
        "bands": [
            (0.03, 0.20, "0.2 mm", (1.29, 4.50, 7.72, 10.29, 12.86, 11.57, 10.29, 6.43, 1.29)),
            (0.20, 0.50, "0.2 mm", (1.07, 3.75, 6.43, 8.57, 10.72, 9.64, 8.57, 5.36, 1.07)),
            (0.50, 1.00, "0.4 mm", (0.89, 3.13, 5.36, 7.14, 8.93, 8.04, 7.14, 4.46, 0.89)),
        ],
    },
}


def table_for_feed(feed_name: Optional[str]) -> Optional[str]:
    """Tabla Aller de un tipo de alimento del catálogo (por su nombre)."""
    n = (feed_name or "").strip().lower()
    for prefix, key in (("progress", "progress"), ("reproductor", "reproductor"),
                        ("rep ", "reproductor"), ("thalassa", "thalassa"),
                        ("infa", "infa")):
        if n.startswith(prefix):
            return key
    return None


def _pct_at_temp(row: tuple, temp_c: float) -> float:
    if temp_c <= TEMPS_C[0]:
        return row[0]
    if temp_c >= TEMPS_C[-1]:
        return row[-1]
    for i in range(len(TEMPS_C) - 1):
        t0, t1 = TEMPS_C[i], TEMPS_C[i + 1]
        if t0 <= temp_c <= t1:
            return row[i] + (row[i + 1] - row[i]) * (temp_c - t0) / (t1 - t0)
    return row[-1]  # inalcanzable


def aller_rate(table: Optional[str], weight_g: Optional[float],
               temp_c: Optional[float]) -> Optional[dict]:
    """% del peso corporal por día que recomienda la ficha.

    Devuelve None si falta la tabla, el peso o la temperatura. `avisos` lista
    lo que se tomó fuera de la ficha, para que la vista lo diga.
    """
    spec = ALLER_TABLES.get(table or "")
    if spec is None or not weight_g or weight_g <= 0 or temp_c is None:
        return None
    bands = spec["bands"]
    avisos = []
    band = next((b for b in bands if b[0] <= weight_g < b[1]), None)
    if band is None:
        if weight_g >= bands[-1][1]:
            band = bands[-1]
            avisos.append("extrapolado: pez sobre {:g} g".format(band[1]))
        elif weight_g < bands[0][0]:
            band = bands[0]
            avisos.append("extrapolado: pez bajo {:g} g".format(band[0]))
        else:  # hueco entre bandas: la más cercana
            band = min(bands, key=lambda b: min(abs(weight_g - b[0]), abs(weight_g - b[1])))
            avisos.append("banda más cercana")
    if temp_c < TEMPS_C[0] or temp_c > TEMPS_C[-1]:
        avisos.append("temperatura fuera de {}-{} °C".format(TEMPS_C[0], TEMPS_C[-1]))
    return {
        "pct": _pct_at_temp(band[3], float(temp_c)),
        "tabla": table,
        "label": spec["label"],
        "banda": "{:g}-{:g} g".format(band[0], band[1]),
        "calibre": band[2],
        "temp_c": float(temp_c),
        "avisos": avisos,
    }


def theoretical_kg_day(biomass_kg: Optional[float], weight_g: Optional[float],
                       temp_c: Optional[float], table: Optional[str]) -> Optional[dict]:
    """Ración teórica en kg/día: biomasa × % de la ficha. None si falta algo."""
    if not biomass_kg or biomass_kg <= 0:
        return None
    rate = aller_rate(table, weight_g, temp_c)
    if rate is None:
        return None
    return dict(rate, kg_day=float(biomass_kg) * rate["pct"] / 100.0,
                biomass_kg=float(biomass_kg), weight_g=float(weight_g))


# =============================================================================
# DATOS DE TERRENO
# =============================================================================
TEMP_WINDOW_H = 24          # ventana de las rondas de O2: la ración es diaria
TEMP_FALLBACK_H = 72        # si la ronda se cortó, hasta 3 días atrás
TEMP_BF_MAX_DAYS = 7        # y si no, el último muestreo de biofiltro


def unit_temperatures(db: Session, now: Optional[datetime] = None) -> dict:
    """{unit_id: {"temp_c", "fuente", "n"}}: temperatura del agua para la ración.

    Promedio de las rondas de O2 de las últimas 24 h en los estanques de la
    unidad (el pez come todo el día; el muestreo de biofiltro es de una hora
    de la mañana y sale 1-3 °C más frío). Si no hay rondas, hasta 72 h; si
    tampoco, la entrada del último muestreo de biofiltro de la semana.
    """
    now = now or datetime.now()
    out: dict = {}
    for horas in (TEMP_WINDOW_H, TEMP_FALLBACK_H):
        rows = (db.query(Pond.cultivation_unit_id,
                         func.avg(PondOxygenReading.water_temp_c),
                         func.count(PondOxygenReading.id))
                .join(Pond, Pond.id == PondOxygenReading.pond_id)
                .filter(PondOxygenReading.reading_datetime >= now - timedelta(hours=horas),
                        PondOxygenReading.reading_datetime <= now,
                        PondOxygenReading.water_temp_c.isnot(None),
                        Pond.cultivation_unit_id.isnot(None))
                .group_by(Pond.cultivation_unit_id)
                .all())
        for uid, temp, n in rows:
            if uid not in out and temp is not None:
                out[uid] = {"temp_c": float(temp), "n": int(n),
                            "fuente": "rondas O₂ {} h".format(horas)}
    bf_rows = (db.query(BiofilterReading.cultivation_unit_id,
                        BiofilterReading.reading_date, BiofilterReading.in_temp_c)
               .filter(BiofilterReading.in_temp_c.isnot(None),
                       BiofilterReading.reading_date >= (now - timedelta(days=TEMP_BF_MAX_DAYS)).date())
               .order_by(BiofilterReading.reading_date.desc(), BiofilterReading.id.desc())
               .all())
    for uid, d, temp in bf_rows:
        if uid not in out:
            out[uid] = {"temp_c": float(temp), "n": 1,
                        "fuente": "biofiltro {}".format(d.strftime("%d-%m"))}
    return out


def carril_inputs(db: Session) -> dict:
    """{pond_id: {...}} por carril (estanque padre activo con peces).

    La biomasa y los peces del carril suman los del padre y sus hijos (las
    divisiones como C3N/C3S); el peso medio sale de ese total. El alimento es
    el último tipo que salió de bodega para el carril: es el que se le da.
    """
    ponds = (db.query(Pond)
             .filter(Pond.state != "inactive")
             .all())
    padres = {p.id: p for p in ponds if p.parent_pond_id is None}
    acc: dict = {}
    for p in ponds:
        pid = p.parent_pond_id if p.parent_pond_id in padres else p.id
        if pid not in padres:
            continue
        a = acc.setdefault(pid, {"biomass_kg": 0.0, "n_fish": 0})
        a["biomass_kg"] += float(p.biomass_projected_kg or 0)
        a["n_fish"] += int(p.n_fish_cached or 0)

    # Alimento del carril: el último programado en la ración (la dieta); si
    # nunca se ha programado, el último que salió de bodega para él.
    ultimo = (db.query(FeedExecutionEvent.pond_id,
                       func.max(FeedExecutionEvent.id).label("ev_id"))
              .group_by(FeedExecutionEvent.pond_id).subquery())
    feed_by_pond = {
        pond_id: (ft_id, name)
        for pond_id, ft_id, name in (
            db.query(FeedExecutionEvent.pond_id, FeedType.id, FeedType.name)
            .join(ultimo, ultimo.c.ev_id == FeedExecutionEvent.id)
            .join(FeedType, FeedType.id == FeedExecutionEvent.feed_type_id)
            .all())
    }
    from app.models.feed import FeedRation
    for pond_id, ft_id, name in (db.query(FeedRation.pond_id, FeedType.id, FeedType.name)
                                 .join(FeedType, FeedType.id == FeedRation.feed_type_id)
                                 .order_by(FeedRation.ration_date, FeedRation.id).all()):
        feed_by_pond[pond_id] = (ft_id, name)

    out = {}
    for pid, a in acc.items():
        if a["n_fish"] <= 0 and a["biomass_kg"] <= 0:
            continue
        p = padres[pid]
        ft_id, ft_name = feed_by_pond.get(pid, (None, None))
        out[pid] = {
            "pond_id": pid,
            "name": p.name,
            "unit_id": p.cultivation_unit_id,
            "biomass_kg": a["biomass_kg"],
            "n_fish": a["n_fish"],
            "weight_g": (a["biomass_kg"] * 1000.0 / a["n_fish"]) if a["n_fish"] else None,
            "feed_type_id": ft_id,
            "feed_name": ft_name,
            "tabla": table_for_feed(ft_name),
        }
    return out


def theoretical_by_carril(db: Session, now: Optional[datetime] = None) -> dict:
    """{pond_id: carril_inputs + temperatura de su unidad + ración teórica}."""
    temps = unit_temperatures(db, now)
    out = {}
    for pid, c in carril_inputs(db).items():
        t = temps.get(c["unit_id"]) or {}
        c = dict(c, temp_c=t.get("temp_c"), temp_fuente=t.get("fuente"))
        c["teorica"] = theoretical_kg_day(c["biomass_kg"], c["weight_g"],
                                          c["temp_c"], c["tabla"])
        out[pid] = c
    return out


# =============================================================================
# RACIÓN DIARIA: programado, terreno y pendientes
# =============================================================================
HORIZON_DAYS = 4            # programar hasta 4 días adelante (fin de semana / feriado)
PROGRAM_DAYS = 7            # el programa de bodega se piensa por semana
# Desde este día todo día pasado se confirma, aunque no se haya programado:
# un día sin registro en una unidad de la ración queda pendiente hasta que
# alguien diga si se alimentó o no (antes no se distinguía "no se alimentó"
# de "nadie lo registró").
REQUIRED_SINCE = date(2026, 10, 25)


def programmed_daily_kg(db: Session) -> dict:
    """{pond_id: kg/día} del programa de bodega vigente: sacos × kg/saco ÷ 7."""
    from app.models.feed import FeedProgram, FeedProgramLine
    rows = (db.query(FeedProgramLine.pond_id,
                     func.sum(FeedProgramLine.planned_bags * FeedType.bag_kg))
            .join(FeedProgram, FeedProgram.id == FeedProgramLine.feed_program_id)
            .join(FeedType, FeedType.id == FeedProgramLine.feed_type_id)
            .filter(FeedProgram.status == "open", FeedProgramLine.planned_bags > 0)
            .group_by(FeedProgramLine.pond_id)
            .all())
    return {pid: float(kg or 0) / PROGRAM_DAYS for pid, kg in rows}


def terreno_since(db: Session) -> Optional[date]:
    """Desde cuándo se cuenta el alimento en terreno.

    Antes de la ración diaria no hay registro de lo que se echó al agua, así
    que no se puede saber cuánto quedó de las salidas de bodega anteriores.
    El conteo parte con la primera salida de bodega desde el primer día de
    ración: hasta entonces no hay terreno que mostrar (None), en vez de un
    negativo que solo diría que el sistema empezó a mitad de una tanda.
    """
    from app.models.feed import FeedRation
    inicio = db.query(func.min(FeedRation.ration_date)).scalar()
    if inicio is None:
        return None
    return (db.query(func.min(func.date(FeedExecutionEvent.confirmed_at)))
            .filter(func.date(FeedExecutionEvent.confirmed_at) >= inicio)
            .scalar())


def terreno_kg(db: Session, since: Optional[date]) -> dict:
    """{pond_id: kg} en terreno = sacado de bodega − alimentado, desde `since`."""
    from app.models.feed import FeedRation
    if since is None:
        return {}
    out: dict = {}
    for pid, kg in (db.query(FeedExecutionEvent.pond_id, func.sum(FeedExecutionEvent.confirmed_kg))
                    .filter(func.date(FeedExecutionEvent.confirmed_at) >= since)
                    .group_by(FeedExecutionEvent.pond_id).all()):
        out[pid] = out.get(pid, 0.0) + float(kg or 0)
    for pid, kg in (db.query(FeedRation.pond_id, func.sum(FeedRation.kg_fed))
                    .filter(FeedRation.ration_date >= since,
                            FeedRation.status == "alimentado")
                    .group_by(FeedRation.pond_id).all()):
        out[pid] = out.get(pid, 0.0) - float(kg or 0)
    return out


def ration_units(db: Session) -> set:
    """Unidades que están en la ración diaria: alguna vez programadas y con
    carriles con peces hoy. Jaula y Tranque nunca se programan y quedan fuera
    solas; una unidad vacía no tiene carriles que confirmar."""
    from app.models.feed import FeedRation
    alguna_vez = {uid for (uid,) in db.query(FeedRation.cultivation_unit_id)
                  .filter(FeedRation.cultivation_unit_id.isnot(None)).distinct().all()}
    return alguna_vez & {c["unit_id"] for c in carril_inputs(db).values()}


def last_planner_by_unit(db: Session) -> dict:
    """{unit_id: user_id} de quien programó la unidad por última vez.

    No hay responsables fijos (vacaciones, permisos): la unidad "es" de quien
    la viene programando, y cambia sola cuando otro toma el relevo.
    """
    from app.models.feed import FeedRation
    ultimo = (db.query(FeedRation.cultivation_unit_id,
                       func.max(FeedRation.planned_at).label("at"))
              .filter(FeedRation.planned_by.isnot(None))
              .group_by(FeedRation.cultivation_unit_id).subquery())
    return {uid: by for uid, by in
            db.query(FeedRation.cultivation_unit_id, FeedRation.planned_by)
            .join(ultimo, (ultimo.c.cultivation_unit_id == FeedRation.cultivation_unit_id)
                  & (ultimo.c.at == FeedRation.planned_at))
            .distinct().all()}


def missing_dates_by_unit(db: Session, today: date) -> dict:
    """{unit_id: [días pasados sin ninguna ración]} desde REQUIRED_SINCE,
    para las unidades de la ración (ver ration_units)."""
    from app.models.feed import FeedRation
    if today <= REQUIRED_SINCE:
        return {}
    unidades = ration_units(db)
    hay = set(db.query(FeedRation.cultivation_unit_id, FeedRation.ration_date)
              .filter(FeedRation.ration_date >= REQUIRED_SINCE,
                      FeedRation.ration_date < today,
                      FeedRation.cultivation_unit_id.in_(unidades))
              .distinct().all())
    out: dict = {}
    for uid in unidades:
        d = REQUIRED_SINCE
        while d < today:
            if (uid, d) not in hay:
                out.setdefault(uid, []).append(d)
            d += timedelta(days=1)
    return out


def pending_dates_by_unit(db: Session, today: date) -> dict:
    """{unit_id: [días pasados por confirmar]} (None = carriles sin unidad).

    Pendiente = programado sin confirmar, o (desde REQUIRED_SINCE) sin ningún
    registro. Cada unidad se confirma por separado: lo pendiente de una
    unidad no frena programar las demás.
    """
    from app.models.feed import FeedRation
    out: dict = {}
    for uid, d in (db.query(FeedRation.cultivation_unit_id, FeedRation.ration_date)
                   .filter(FeedRation.ration_date < today,
                           FeedRation.status == "programado")
                   .distinct().all()):
        out.setdefault(uid, set()).add(d)
    for uid, dias in missing_dates_by_unit(db, today).items():
        out.setdefault(uid, set()).update(dias)
    return {uid: sorted(ds) for uid, ds in out.items()}


def pending_dates(db: Session, today: date) -> list:
    """Días ya pasados con algo por confirmar en alguna unidad (más antiguo primero)."""
    return sorted({d for ds in pending_dates_by_unit(db, today).values() for d in ds})


def split_kg(total_kg: float, carriles: list, mode: str) -> dict:
    """Reparte el total de la unidad entre los carriles seleccionados.

    mode "biomasa": proporcional a la biomasa; "iguales": lo mismo a cada uno.
    Si ninguno tiene biomasa, se reparte por igual. Redondeo a 0,1 kg.
    """
    if not carriles or total_kg <= 0:
        return {c["pond_id"]: 0.0 for c in carriles}
    bio = sum(max(0.0, c.get("biomass_kg") or 0.0) for c in carriles)
    if mode == "biomasa" and bio > 0:
        return {c["pond_id"]: round(total_kg * max(0.0, c.get("biomass_kg") or 0.0) / bio, 1)
                for c in carriles}
    return {c["pond_id"]: round(total_kg / len(carriles), 1) for c in carriles}
