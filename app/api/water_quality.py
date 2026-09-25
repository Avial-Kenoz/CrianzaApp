"""Router del módulo de calidad de agua (páginas + ingreso).

Convención de CrianzaApp: páginas HTML bajo /views/ui/* renderizadas con
jinja_env + HTMLResponse, incluyendo _sidebar.html. Sin capa de auth.

Carga los umbrales de la BD (water_quality_thresholds) y los inyecta al motor
puro `app.services.water_quality`; persiste los flags/niveles calculados.
"""

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pathlib import Path
from sqlalchemy import func, and_, text as sa_text
from sqlalchemy.orm import Session
from datetime import datetime, date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional
from urllib.parse import quote_plus
import secrets
import segno

from app.db.session import SessionLocal
from app.models.ponds import Pond
from app.models.cultivation_units import CultivationUnit
from app.models.users import User
from app.models.pond_oxygen_readings import PondOxygenReading
from app.models.biofilter_readings import BiofilterReading
from app.models.water_quality_thresholds import WaterQualityThreshold
from app.models.water_quality_test_specs import WaterQualityTestSpec
from app.models.water_treatment_doses import WaterTreatmentDose
from app.models.solar_daily import SolarDaily
from app.services import water_quality as wq

router = APIRouter(prefix="/views/ui/calidad-agua", tags=["calidad-agua"])
template_dir = Path(__file__).parent.parent / "templates"
# autoescape: sin esto un '<' de un texto de diagnostico ("< 70%") o una
# observacion escrita por el operador entran crudos al HTML.
jinja_env = Environment(loader=FileSystemLoader(str(template_dir)),
                        autoescape=select_autoescape(["html"]))

# Una lectura de O2 se considera vencida si supera este intervalo (cada 2-4 h)
O2_STALE_HOURS = 4

# Acciones correctivas ofrecidas ante una lectura de O2 en alarma (llamado a la
# acción). El operador elige una al reconocer la lectura; queda en
# pond_oxygen_readings.corrective_action. Compartidas por el form web y la PWA.
CORRECTIVE_ACTIONS = [
    "Encendí aireación / oxígeno",
    "Revisé / ajusté flujo de agua",
    "Avisé al supervisor",
    "Segunda lectura / reingreso",
    "A verificar (aún sin acción)",
]


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _parse_decimal(value: Optional[str]) -> Optional[float]:
    if value is None:
        return None
    value = str(value).strip().replace(",", ".")
    if value == "":
        return None
    try:
        return float(Decimal(value))
    except (InvalidOperation, ValueError):
        return None


def load_thresholds(db: Session) -> dict:
    """Lee los umbrales activos de la BD con la forma que espera el motor.

    Rellena con DEFAULT_THRESHOLDS cualquier parámetro faltante/inactivo.
    """
    out: dict[str, dict] = {}
    for r in db.query(WaterQualityThreshold).filter(WaterQualityThreshold.active.is_(True)).all():
        out[r.parameter] = {
            "alert": float(r.alert_value) if r.alert_value is not None else None,
            "alarm": float(r.alarm_value) if r.alarm_value is not None else None,
            "comparator": r.comparator,
        }
    for key, spec in wq.DEFAULT_THRESHOLDS.items():
        out.setdefault(key, spec)
    return out


def load_test_specs(db: Session) -> dict:
    """Lee las especificaciones de los tests de N con la forma que espera el
    motor. Rellena con DEFAULT_TEST_SPECS lo que falte."""
    out: dict[str, dict] = {}
    for r in db.query(WaterQualityTestSpec).all():
        out[r.test] = {
            "resolution": float(r.resolution) if r.resolution is not None else None,
            "acc_fixed": float(r.acc_fixed) if r.acc_fixed is not None else 0.0,
            "acc_pct": float(r.acc_pct) if r.acc_pct is not None else 0.0,
        }
    for key, spec in wq.DEFAULT_TEST_SPECS.items():
        out.setdefault(key, spec)
    return out


def recent_unit_temp(db: Session, unit_id: int, before_dt: datetime) -> Optional[float]:
    """Temperatura MEDIDA más reciente de la unidad dentro de la ventana de
    frescura, anterior o igual a `before_dt`.

    Sirve para heredar la temperatura cuando el operador la omite en un estanque
    que no es el primero de la ronda: la laguna es homogénea, así que basta la
    del primer estanque medido. Solo considera temperaturas medidas
    (water_temp_inherited=False) para no encadenar herencias.
    """
    if unit_id is None:
        return None
    window_start = before_dt - timedelta(hours=O2_STALE_HOURS)
    row = (
        db.query(PondOxygenReading.water_temp_c)
        .join(Pond, Pond.id == PondOxygenReading.pond_id)
        .filter(
            Pond.cultivation_unit_id == unit_id,
            PondOxygenReading.water_temp_inherited.is_(False),
            PondOxygenReading.water_temp_c.isnot(None),
            PondOxygenReading.reading_datetime >= window_start,
            PondOxygenReading.reading_datetime <= before_dt,
        )
        .order_by(PondOxygenReading.reading_datetime.desc())
        .first()
    )
    return float(row[0]) if row and row[0] is not None else None


def _active_users(db: Session):
    users = db.query(User).filter(User.active.is_(True)).order_by(User.name, User.lastname).all()
    return [{"id": u.id, "label": " ".join(x for x in [u.name, u.lastname] if x) or u.email} for u in users]


# ---------------------------------------------------------------------------
# Helpers de última lectura
# ---------------------------------------------------------------------------
def _latest_o2_by_pond(db: Session) -> dict:
    """Última lectura CON OXIGENO por estanque (pond_id -> PondOxygenReading).

    La tabla tambien admite filas de solo pH: desde el 24-09 el pH por
    estanque se mide en rondas propias, a las 08:30 y a las 12:00, sin OD.
    Sin el filtro, una de esas filas se hace pasar por la ultima lectura de O2
    y el panel muestra el estanque en blanco cuando en realidad tiene un dato
    de oxigeno reciente.
    """
    sub = (
        db.query(
            PondOxygenReading.pond_id.label("pid"),
            func.max(PondOxygenReading.reading_datetime).label("mx"),
        )
        .filter(PondOxygenReading.do_mg_l.isnot(None))
        .group_by(PondOxygenReading.pond_id)
        .subquery()
    )
    out = {}
    for r in db.query(PondOxygenReading).filter(
        PondOxygenReading.do_mg_l.isnot(None),
    ).join(
        sub, and_(PondOxygenReading.pond_id == sub.c.pid,
                  PondOxygenReading.reading_datetime == sub.c.mx),
    ).order_by(PondOxygenReading.id.desc()).all():
        out.setdefault(r.pond_id, r)
    return out


def _latest_bf_by_unit(db: Session) -> dict:
    """Último muestreo de biofiltro por unidad (unit_id -> BiofilterReading)."""
    sub = (
        db.query(
            BiofilterReading.cultivation_unit_id.label("uid"),
            func.max(BiofilterReading.reading_date).label("mx"),
        )
        .group_by(BiofilterReading.cultivation_unit_id)
        .subquery()
    )
    out = {}
    for r in db.query(BiofilterReading).join(
        sub, and_(BiofilterReading.cultivation_unit_id == sub.c.uid,
                  BiofilterReading.reading_date == sub.c.mx),
    ).order_by(BiofilterReading.id.desc()).all():
        out.setdefault(r.cultivation_unit_id, r)
    return out


# --- Salud del biofiltro ----------------------------------------------------
# Dos indicadores que no se ven en la medición diaria: la velocidad de
# nitrificación (k) y si la segunda etapa sigue el ritmo de la primera (NOB/AOB).
#
# k se calcula SIN caudal, combinando el balance de masa con la cinética (ver
# app/services/water_quality.py). El N producido sale del alimento registrado.
#
# Van sobre una ventana móvil, nunca sobre una lectura suelta: el test de amonio
# tiene ±0,04 mg/L fijo sobre concentraciones de 0,3-0,7, así que una lectura
# individual trae ~30% de error en el delta y el semáforo parpadearía al azar.
BF_HEALTH_WINDOW = 8            # muestreos de la ventana actual (~3 semanas)
BF_HEALTH_MIN_HISTORY = 24      # ~3 meses: bajo esto la "línea base" se solaparía
                                # con la ventana actual y el % no significaría nada
BF_HEALTH_OK = 0.85             # fracción de la referencia
BF_HEALTH_WARN = 0.70


FEED_MIN_WINDOW_DAYS = 28       # el alimento se confirma por lotes, no a diario:
                                # ventanas cortas dan kg/día muy ruidosos


def _unit_feed_n_kg_day(db: Session, unit_id: int, since: date, until: date) -> Optional[float]:
    """N amoniacal producido por el alimento de una unidad, en kg N/día.

    Usa proteína y kilos por saco de feed_types (datos de etiqueta). Devuelve
    None si la unidad no tiene alimento registrado o falta la config.

    La ventana se ensancha hacia atrás hasta FEED_MIN_WINDOW_DAYS: los eventos de
    alimento se confirman en tandas, así que un rango corto puede caer entre dos
    confirmaciones y dar una ración irreal.
    """
    days = (until - since).days + 1
    if days <= 0:
        return None
    if days < FEED_MIN_WINDOW_DAYS:
        since = until - timedelta(days=FEED_MIN_WINDOW_DAYS - 1)
        days = FEED_MIN_WINDOW_DAYS
    row = db.execute(sa_text("""
        SELECT SUM(e.confirmed_bags * ft.bag_kg * ft.protein_pct / 100.0) AS prot_kg
          FROM feed_execution_events e
          JOIN ponds p ON p.id = e.pond_id
          JOIN feed_types ft ON ft.id = e.feed_type_id
         WHERE p.cultivation_unit_id = :uid
           AND e.confirmed_at::date BETWEEN :since AND :until
           AND ft.bag_kg IS NOT NULL AND ft.protein_pct IS NOT NULL
    """), {"uid": unit_id, "since": since, "until": until}).first()
    if not row or row.prot_kg is None:
        return None
    # prot_kg ya trae alimento x fracción proteica; solo falta el factor TAN
    return float(row.prot_kg) * wq.TAN_PER_FEED_N / days


def _window_k(db: Session, unit, rows) -> Optional[float]:
    """k normalizado (1/h) sobre un conjunto de muestreos."""
    usable = [r for r in rows if r.in_nh4_n and r.out_nh4_n
              and float(r.in_nh4_n) > 0 and float(r.out_nh4_n) > 0]
    if len(usable) < 2 or not unit.media_volume_m3:
        return None
    ci = sum(float(r.in_nh4_n) for r in usable) / len(usable)
    co = sum(float(r.out_nh4_n) for r in usable) / len(usable)
    n_kg = _unit_feed_n_kg_day(db, unit.id,
                               min(r.reading_date for r in usable),
                               max(r.reading_date for r in usable))
    if n_kg is not None:
        # La laguna no es un circuito cerrado: entra agua fresca y sale la misma
        # purga, llevándose amonio a la concentración de la laguna. Ese N no pasa
        # por el biofiltro; atribuírselo lo haría ver mejor de lo que es.
        n_kg = max(0.0, n_kg - wq.purge_nitrogen_kg_day(unit.freshwater_l_s, ci))
    k = wq.biofilter_k(n_kg, float(unit.media_volume_m3), ci, co)
    if k is None:
        return None
    temps = [float(r.in_temp_c) for r in usable if r.in_temp_c is not None]
    phs = [float(r.in_ph) for r in usable if r.in_ph is not None]
    return wq.normalized_biofilter_k(
        k,
        sum(temps) / len(temps) if temps else None,
        sum(phs) / len(phs) if phs else None,
    )


def _biofilter_health_by_unit(db: Session) -> dict:
    """{unit_id: {...}} con salud (k) y balance de nitrito por unidad.

    La referencia contra la que se juzga k es, por ahora, la MEDIANA de las otras
    unidades recirculantes: k es comparable entre unidades de distinto tamaño y
    caudal, que es justamente lo que la eficiencia no permite. La comparación
    contra la propia historia (que detectaría degradación lenta) se activa sola
    cuando la unidad acumule BF_HEALTH_MIN_HISTORY muestreos.
    """
    out = {}
    units = db.query(CultivationUnit).all()
    for u in units:
        if not u.is_recirculating:
            # Flujo abierto (Central): el nitrógeno sale con el agua, no por el
            # filtro. Evaluarlo como recirculante daría un falso "enfermo".
            out[u.id] = {"state": "no_aplica", "applies": False,
                         "reason": "Flujo abierto"}
            continue
        rows = (db.query(BiofilterReading)
                .filter(BiofilterReading.cultivation_unit_id == u.id)
                .order_by(BiofilterReading.reading_date.desc(),
                          BiofilterReading.id.desc())
                .all())
        if not rows:
            # Unidad sin biofiltro (hatchery, jaula): no hay nada que evaluar y
            # el panel ya lo muestra como "biofiltro —".
            continue
        window, history = rows[:BF_HEALTH_WINDOW], rows[BF_HEALTH_WINDOW:]

        k_now = _window_k(db, u, window)
        baseline = _window_k(db, u, history) if len(rows) >= BF_HEALTH_MIN_HISTORY else None

        # Balance NOB/AOB sobre la misma ventana (promedio de las medias)
        usable = [r for r in window if r.in_nh4_n and r.out_nh4_n
                  and r.in_no2_n is not None and r.out_no2_n is not None]
        balance = None
        if usable:
            n = len(usable)
            balance = wq.nob_aob_balance(
                sum(float(r.in_nh4_n) for r in usable) / n,
                sum(float(r.out_nh4_n) for r in usable) / n,
                sum(float(r.in_no2_n) for r in usable) / n,
                sum(float(r.out_no2_n) for r in usable) / n,
            )

        out[u.id] = {
            "k": k_now, "baseline": baseline, "applies": True,
            "no_config": not u.media_volume_m3,
            "balance": balance,
            "balance_state": (None if balance is None else
                              "al_dia" if balance >= 1.0 else
                              "justo" if balance >= 0.95 else "rezagado"),
            "n_samples": len(window),
        }

    # Referencia entre unidades: mediana de las demás con k calculable.
    for uid, d in out.items():
        if not d.get("applies"):               # flujo abierto, ya resuelto
            continue
        otras = sorted(o["k"] for i, o in out.items()
                       if i != uid and o.get("applies") and o.get("k"))
        ref = (otras[len(otras) // 2] if len(otras) % 2 else
               (otras[len(otras) // 2 - 1] + otras[len(otras) // 2]) / 2) if otras else None
        # La propia historia manda cuando existe: detecta degradación de esa
        # unidad aunque todo el plantel esté igual de mal.
        base, base_kind = (d["baseline"], "su línea base") if d["baseline"] else (ref, "las otras unidades")

        if d["k"] is None:
            d["state"] = "sin_dato"
            d["reason"] = ("Falta volumen de medio filtrante" if d["no_config"]
                           else "Sin alimento registrado o muestreos insuficientes")
        elif base is None:
            d["state"], d["reason"] = "sin_base", "Sin referencia para comparar"
        elif d["k"] >= BF_HEALTH_OK * base:
            d["state"], d["reason"] = "ok", None
        elif d["k"] >= BF_HEALTH_WARN * base:
            d["state"] = "atencion"
            d["reason"] = f"Nitrificación bajo {base_kind}"
        else:
            d["state"] = "bajo"
            d["reason"] = f"Nitrificación muy por debajo de {base_kind}"
        d["ref"] = base
        d["ref_kind"] = base_kind
        d["pct"] = (d["k"] / base * 100.0) if (d["k"] and base) else None
    return out


def _photosynthesis_by_unit(db: Session, thresholds: dict, now: datetime) -> dict:
    """Señal de floración de algas por unidad, sobre los últimos 7 días de O2.

    Una sola consulta para todas las unidades. Los estanques de una unidad
    comparten agua, así que la señal es de unidad: no tiene sentido separar.

    Al resultado del motor le agrega el pH al que el amonio no ionizado cruza
    los umbrales del sitio, usando el último TAN medido en el biofiltro. Ese
    par de números es lo que convierte la alerta en algo accionable.
    """
    since = now - timedelta(days=wq.PHOTO_WINDOW_DAYS)
    rows = (
        db.query(
            Pond.cultivation_unit_id.label("unit_id"),
            PondOxygenReading.reading_datetime,
            PondOxygenReading.do_mg_l,
            PondOxygenReading.water_temp_c,
        )
        .join(Pond, Pond.id == PondOxygenReading.pond_id)
        .filter(
            PondOxygenReading.reading_datetime >= since,
            PondOxygenReading.do_mg_l.isnot(None),
            PondOxygenReading.water_temp_c.isnot(None),
        )
        .all()
    )

    d0, d1 = wq.PHOTO_DAY_HOURS
    n0, n1 = wq.PHOTO_NIGHT_HOURS
    a0, a1 = wq.PHOTO_DAWN_HOURS
    buckets: dict = {}
    for r in rows:
        do = float(r.do_mg_l)
        tc = float(r.water_temp_c)
        if not (0 < do < 20) or not (2 < tc < 30):
            continue
        sat = do / wq.do_saturation_mg_l(tc) * 100.0
        h = r.reading_datetime.hour
        b = buckets.setdefault(r.unit_id, {"day": [], "night": [], "dawn_s": [], "dawn_o": []})
        if d0 <= h <= d1:
            b["day"].append(sat)
        if h >= n0 or h <= n1:          # la ventana nocturna cruza medianoche
            b["night"].append(sat)
        if a0 <= h <= a1:
            b["dawn_s"].append(sat)
            b["dawn_o"].append(do)

    # Último TAN y temperatura del biofiltro, para traducir el pH a toxicidad
    tan_by_unit: dict = {}
    for br in _latest_bf_by_unit(db).values():
        if br is not None and br.in_nh4_n is not None:
            tan_by_unit[br.cultivation_unit_id] = (
                float(br.in_nh4_n),
                float(br.in_temp_c) if br.in_temp_c is not None else None,
            )

    nh3_spec = thresholds.get("nh3_n") or {}
    out: dict = {}
    for unit_id, b in buckets.items():
        sig = wq.photosynthesis_signal(b["day"], b["night"], b["dawn_s"],
                                       b["dawn_o"], thresholds)
        if not sig or not sig["active"]:
            continue
        tan, tan_temp = tan_by_unit.get(unit_id, (None, None))
        sig["nh4_n"] = tan
        sig["ph_alert"] = wq.ph_for_nh3_limit(tan, tan_temp, nh3_spec.get("alert"))
        sig["ph_alarm"] = wq.ph_for_nh3_limit(tan, tan_temp, nh3_spec.get("alarm"))
        out[unit_id] = sig
    return out


def _recent_etas(db: Session, unit_id: int, n: int = 8) -> Optional[dict]:
    """Mediana de eta de los ultimos `n` muestreos de la unidad.

    La eta de una muestra sola es casi todo ruido del kit (sd 0,19 sobre 0,68),
    asi que la tarjeta mostraria un numero distinto cada vez sin que nada haya
    cambiado. La mediana de 8 es estable y es la misma ventana del indicador
    de salud.
    """
    rows = (db.query(BiofilterReading)
            .filter(BiofilterReading.cultivation_unit_id == unit_id,
                    BiofilterReading.in_nh4_n.isnot(None),
                    BiofilterReading.out_nh4_n.isnot(None))
            .order_by(BiofilterReading.reading_date.desc(),
                      BiofilterReading.id.desc())
            .limit(n).all())
    etas = [1.0 - float(r.out_nh4_n) / float(r.in_nh4_n)
            for r in rows
            if r.in_nh4_n and float(r.in_nh4_n) > 0 and r.out_nh4_n is not None
            and float(r.out_nh4_n) <= float(r.in_nh4_n)]
    if not etas:
        return None
    etas.sort()
    m = len(etas) // 2
    mediana = etas[m] if len(etas) % 2 else (etas[m - 1] + etas[m]) / 2
    return {"eta": mediana, "n": len(etas)}


def _reactor_m3(u) -> Optional[float]:
    """Volumen para el tiempo de contacto.

    El agua circula por el REACTOR, no por el plastico. Mientras el reactor no
    este configurado se usa el volumen de medio, que es lo que se venia
    haciendo: subestima tau, pero mantiene la cifra viva en vez de vaciarla de
    golpe en las cinco unidades. Cuando el reactor se carga, tau sube.
    """
    if getattr(u, "reactor_volume_m3", None):
        return float(u.reactor_volume_m3)
    return float(u.media_volume_m3) if u.media_volume_m3 else None


def _surface_load_by_unit(db: Session, now: datetime) -> dict:
    """Carga superficial (g TAN/m2/d) de cada unidad, y su razon con las otras.

    La referencia NO es la literatura sino la MEDIANA DEL SITIO: es lo que
    demostradamente funciona en estas lagunas, con esta agua y este medio. Un
    umbral de catalogo no habria agarrado a Sur Oriente, que carga 2,5 veces
    mas por m2 que sus hermanas estando bajo los 0,2 de referencia MBBR.
    """
    units = (db.query(CultivationUnit)
             .filter(CultivationUnit.is_recirculating.is_(True)).all())
    bfs = _latest_bf_by_unit(db)
    hoy = date.today()
    out = {}
    for u in units:
        sup = wq.media_surface_m2(u.media_volume_m3, u.media_ssa_m2_m3)
        if not sup:
            continue
        n_kg = _unit_feed_n_kg_day(db, u.id, hoy - timedelta(days=27), hoy)
        bf = bfs.get(u.id)
        if n_kg is None or bf is None or not bf.in_nh4_n:
            continue
        purga = wq.purge_nitrogen_kg_day(
            float(u.freshwater_l_s) if u.freshwater_l_s else None,
            float(bf.in_nh4_n)) or 0.0
        carga = wq.surface_loading(max(0.0, n_kg - purga), sup)
        if carga is None:
            continue
        ll = wq.media_fill(u.media_volume_m3, u.reactor_volume_m3)
        out[u.id] = {"carga": carga, "superficie": sup,
                     "llenado_pct": (ll["pct"] if ll else None)}
    # Razon contra la mediana de LAS OTRAS: con pocas unidades, incluirse a si
    # misma arrastra la referencia hacia el propio valor y apaga el outlier.
    for uid, d in out.items():
        otras = sorted(o["carga"] for i, o in out.items() if i != uid)
        if not otras:
            continue
        m = len(otras) // 2
        ref = otras[m] if len(otras) % 2 else (otras[m - 1] + otras[m]) / 2
        d["ref"] = ref
        d["razon"] = (d["carga"] / ref) if ref else None
    return out


def _no3_trend_by_unit(db: Session, now: datetime, dias: int = 35) -> dict:
    """Pendiente del nitrato de salida, en mg/L por semana, por unidad.

    El nitrato es el producto final: si sube sostenidamente no es el biofiltro
    fallando, es la purga quedandose corta. Se ajusta una recta por minimos
    cuadrados sobre los muestreos de la ventana; con menos de 4 puntos no se
    devuelve nada, una recta sobre 3 muestras de kit es adivinanza.
    """
    desde = (now - timedelta(days=dias)).date()
    rows = (db.query(BiofilterReading.cultivation_unit_id,
                     BiofilterReading.reading_date,
                     BiofilterReading.out_no3_n)
            .filter(BiofilterReading.reading_date >= desde,
                    BiofilterReading.out_no3_n.isnot(None))
            .all())
    por_unidad: dict = {}
    for uid, fecha, no3 in rows:
        por_unidad.setdefault(uid, []).append(
            ((fecha - desde).days, float(no3)))
    out: dict = {}
    for uid, pts in por_unidad.items():
        if len(pts) < 4:
            continue
        n_p = len(pts)
        sx = sum(x for x, _ in pts)
        sy = sum(y for _, y in pts)
        sxx = sum(x * x for x, _ in pts)
        sxy = sum(x * y for x, y in pts)
        den = n_p * sxx - sx * sx
        if den == 0:
            continue
        pendiente = (n_p * sxy - sx * sy) / den        # mg/L por dia
        out[uid] = {"slope_week": pendiente * 7.0, "n": n_p}
    return out


def _source_alkalinity(db: Session) -> Optional[float]:
    """Alcalinidad del agua de origen.

    En esta planta toda el agua entra por la unidad de flujo abierto (~60 L/s)
    y desde ahi se reparte a las lagunas, asi que la alcalinidad medida en esa
    unidad ES la del origen. Evita depender de un analisis de pozo antiguo.
    """
    r = (db.query(BiofilterReading.in_alkalinity)
         .join(CultivationUnit,
               CultivationUnit.id == BiofilterReading.cultivation_unit_id)
         .filter(CultivationUnit.is_recirculating.is_(False),
                 BiofilterReading.in_alkalinity.isnot(None))
         .order_by(BiofilterReading.reading_date.desc(),
                   BiofilterReading.id.desc())
         .first())
    return float(r[0]) if r and r[0] is not None else None


def _photosynthesis_evaluable(db: Session, now: datetime) -> set:
    """Unidades con lecturas de O2 suficientes en ambas ventanas horarias."""
    since = now - timedelta(days=wq.PHOTO_WINDOW_DAYS)
    d0, d1 = wq.PHOTO_DAY_HOURS
    n0, n1 = wq.PHOTO_NIGHT_HOURS
    rows = (db.query(Pond.cultivation_unit_id.label("uid"),
                     PondOxygenReading.reading_datetime)
            .join(Pond, Pond.id == PondOxygenReading.pond_id)
            .filter(PondOxygenReading.reading_datetime >= since,
                    PondOxygenReading.do_mg_l.isnot(None),
                    PondOxygenReading.water_temp_c.isnot(None)).all())
    cuenta: dict = {}
    for r in rows:
        h = r.reading_datetime.hour
        c = cuenta.setdefault(r.uid, [0, 0])
        if d0 <= h <= d1:
            c[0] += 1
        if h >= n0 or h <= n1:
            c[1] += 1
    return {u for u, (d, n) in cuenta.items()
            if d >= wq.PHOTO_MIN_READINGS and n >= wq.PHOTO_MIN_READINGS}


def _o2_hours_ago(reading, now: datetime):
    if reading is None or reading.reading_datetime is None:
        return None, False
    hours = (now - reading.reading_datetime).total_seconds() / 3600.0
    return round(hours, 1), hours > O2_STALE_HOURS


# ---------------------------------------------------------------------------
# Panel de estado (rollup por unidad de cultivo, semáforo + detalle inline)
# ---------------------------------------------------------------------------
@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def panel(request: Request, msg: Optional[str] = None, open: Optional[int] = None):
    db = SessionLocal()
    try:
        now = datetime.now()
        units = db.query(CultivationUnit).order_by(CultivationUnit.name).all()
        ponds = (
            db.query(Pond)
            .filter(Pond.state != "inactive", Pond.parent_pond_id.is_(None))
            .order_by(Pond.name)
            .all()
        )
        ponds_by_unit: dict = {}
        for p in ponds:
            ponds_by_unit.setdefault(p.cultivation_unit_id, []).append(p)

        o2_latest = _latest_o2_by_pond(db)
        bf_latest = _latest_bf_by_unit(db)
        bf_health = _biofilter_health_by_unit(db)
        # Umbrales actuales: se reinyectan al motor para derivar el motivo
        # (qué parámetro dispara la tarjeta) sin persistir nada.
        thresholds = load_thresholds(db)
        photo = _photosynthesis_by_unit(db, thresholds, now)
        # Unidades con lecturas suficientes para evaluar fotosintesis: sin esto
        # "no detectada" y "no pude evaluar" se ven igual, y no son lo mismo.
        photo_eval = _photosynthesis_evaluable(db, now)
        alk_origen = _source_alkalinity(db)
        no3_trend = _no3_trend_by_unit(db, now)
        carga_sup = _surface_load_by_unit(db, now)
        # Pronostico de manana: una consulta para todo el panel.
        solar_manana = db.get(SolarDaily, now.date() + timedelta(days=1))
        # Franja del sitio: hoy y los dos dias siguientes.
        solar_dias = (db.query(SolarDaily)
                      .filter(SolarDaily.day >= now.date(),
                              SolarDaily.day <= now.date() + timedelta(days=2))
                      .order_by(SolarDaily.day).all())
        no2_fit = wq.fit_nitrite_relation(_nitrite_pairs(db))

        units_data = []
        totals = {"ok": 0, "alerta": 0, "alarma": 0, "sin_dato": 0}
        for u in units:
            u_ponds = ponds_by_unit.get(u.id, [])
            levels = []
            # Motivos de la unidad como tuplas (Reason, etiqueta de ubicación):
            # el estanque para O₂, "Biofiltro" para el muestreo de la unidad.
            unit_reasons = []
            n_alarm = n_alert = n_nodata = n_stale = n_alarm_unack = 0
            pond_rows = []
            # Dimension O2: el MINIMO entre estanques, no el promedio — el
            # promedio esconde justo el estanque que necesita atencion.
            o2_min, o2_pond, o2_hours, o2_state = None, None, None, "ok"
            o2_marks = 0

            for p in u_ponds:
                r = o2_latest.get(p.id)
                hours, stale = _o2_hours_ago(r, now)
                # saturation_pct viene NULL en todo el historico: se calcula
                # desde OD y temperatura (Benson-Krause corregido por altitud).
                sat = (wq.expected_saturation_pct(float(r.do_mg_l), float(r.water_temp_c))
                       if (r is not None and r.do_mg_l is not None
                           and r.water_temp_c is not None) else None)
                if sat is not None and 0 < sat < 300:
                    if o2_min is None or sat < o2_min:
                        # Solo el codigo: "C6", no "C6 - Carriles 6".
                        o2_min = sat
                        o2_pond = p.name.split(" - ")[0].strip()
                    if hours is not None and (o2_hours is None or hours < o2_hours):
                        o2_hours = hours
                    # El nivel lo pone el motor por lectura (mira saturacion,
                    # OD absoluto y temperatura); la saturacion minima es solo
                    # el VALOR que se muestra.
                    o2_state = wq.worst_level(o2_state, r.alarm_level or "ok")
                    if r.consistency_flag == "sospechoso":
                        o2_marks += 1
                if r is None:
                    n_nodata += 1
                else:
                    if stale:
                        n_stale += 1
                    if r.alarm_level:
                        levels.append(r.alarm_level)
                        if r.alarm_level == "alarma":
                            n_alarm += 1
                            # Alarma cuya lectura no fue reconocida por el operador:
                            # necesita atención del supervisor.
                            if not r.acknowledged:
                                n_alarm_unack += 1
                        elif r.alarm_level == "alerta":
                            n_alert += 1
                    for rs in wq.oxygen_reasons(r.do_mg_l, r.water_temp_c,
                                                r.saturation_pct, thresholds):
                        unit_reasons.append((rs, p.name))
                pond_rows.append({
                    "pond_id": p.id, "pond_name": p.name, "reading": r,
                    "alarm_level": (r.alarm_level if r else None),
                    "consistency_flag": (r.consistency_flag if r else None),
                    "hours_ago": hours, "stale": stale, "has_data": r is not None,
                    # Nivel por campo, para destacar la celda fuera de rango.
                    "levels": (wq.oxygen_field_levels(r.do_mg_l, r.water_temp_c,
                                                      r.saturation_pct, thresholds) if r else {}),
                    # saturation_pct viene NULL en todo el historico
                    "sat_pct": sat,
                })

            bf = bf_latest.get(u.id)
            has_bf = bf is not None
            bf_levels = {}
            bf_conf = None
            bf_issues = []
            if bf is None:
                n_nodata += 1
            else:
                if bf.alarm_level:
                    levels.append(bf.alarm_level)
                    if bf.alarm_level == "alarma":
                        n_alarm += 1
                    elif bf.alarm_level == "alerta":
                        n_alert += 1
                bf_vals = {
                    "in_ph": bf.in_ph, "in_temp_c": bf.in_temp_c,
                    "in_nh4_n": bf.in_nh4_n, "in_no2_n": bf.in_no2_n, "in_no3_n": bf.in_no3_n,
                    "out_ph": bf.out_ph, "out_temp_c": bf.out_temp_c,
                    "out_nh4_n": bf.out_nh4_n, "out_no2_n": bf.out_no2_n, "out_no3_n": bf.out_no3_n,
                    "n_balance_flag": bf.n_balance_flag,
                    "no2_flag": bf.no2_flag,
                    "ph_delta_flag": bf.ph_delta_flag,
                    "temp_delta_flag": bf.temp_delta_flag,
                }
                bf_reasons = wq.biofilter_reasons(bf_vals, thresholds)
                for rs in bf_reasons:
                    unit_reasons.append((rs, "Biofiltro"))
                bf_levels = wq.biofilter_field_levels(bf_vals, thresholds)
                # Compuerta: si una alarma biologica se apoya en una lectura
                # marcada, la accion es remuestrear, no dosificar.
                bf_conf = wq.sampling_confidence(bf_vals, bf_reasons)
                # Cero que en realidad es casilla vacia: rompe eta sin avisar.
                bf_issues = wq.concentration_issues(bf_vals)

            # --- las cuatro dimensiones de la tarjeta -------------------
            h = bf_health.get(u.id)
            eta = eta_n = eta_hours = None
            eta_state = "sin_dato"
            marks = []
            if bf is not None:
                eta_hours = ((now.date() - bf.reading_date).days * 24.0
                             if bf.reading_date else None)
                etas = _recent_etas(db, u.id)
                if etas:
                    eta, eta_n = etas["eta"], etas["n"]
                    eta_state = {"bajo": "alarma", "atencion": "alerta"}.get(
                        (h or {}).get("state"), "ok")
                for flag, txt in (("n_balance_flag", "balance N"),
                                  ("no2_flag", "nitrito fuera de curva"),
                                  ("ph_delta_flag", "ΔpH"),
                                  ("temp_delta_flag", "Δtemp")):
                    if getattr(bf, flag, None) == "sospechoso":
                        marks.append(txt)
            if o2_marks:
                marks.append(f"{o2_marks} lectura{'s' if o2_marks > 1 else ''} de O₂")
            photo_hours = o2_hours          # la fotosintesis se calcula del O2
            # Un biofiltro que nunca se ha muestreado y no tiene medio
            # configurado simplemente no existe: la fila dice "no aplica" en
            # vez de pedir un muestreo imposible.
            # Sin recirculacion el biofiltro no tiene rol: Central convierte
            # un 3% y eso es de diseño, no una falla. Mostrarlo junto al 51%
            # del norte invitaria a leerlo como un problema.
            tiene_bf = ((has_bf or u.media_volume_m3 is not None)
                        and bool(u.is_recirculating))
            dims = wq.build_dimensions(o2_min, o2_pond, o2_hours, o2_state,
                                       eta, eta_n or 0, eta_hours, eta_state,
                                       (bf.alarm_level if bf is not None else None),
                                       photo.get(u.id), photo_hours, marks,
                                       has_bf=tiene_bf,
                                       photo_evaluable=(u.id in photo_eval))
            accion = wq.suggest_action(dims, bool(bf_conf and bf_conf.get("gate")),
                                       photo.get(u.id), (h or {}).get("state"))

            # Capacidad: con el Q NOMINAL. La UI debe marcarlo como estimado.
            capacidad = wq.biofilter_capacity(
                eta, float(u.recirculation_l_s) if u.recirculation_l_s else None,
                _reactor_m3(u))
            # Capacidad de alimentacion y cual de los techos manda.
            feed_cap = wq.feed_capacity(
                eta,
                float(u.recirculation_l_s) if u.recirculation_l_s else None,
                float(u.freshwater_l_s) if u.freshwater_l_s else None,
                float(bf.in_ph) if (bf is not None and bf.in_ph) else None,
                float(bf.in_temp_c) if (bf is not None and bf.in_temp_c) else None,
                thresholds, no2_fit, 48.0, alk_source=alk_origen)

            # Diagnostico: responde a la dimension encendida, no es lista fija.
            no2_check = None
            if bf is not None and bf.out_nh4_n and bf.out_no2_n:
                fit_u = no2_fit
                no2_check = wq.nitrite_outlier(float(bf.out_nh4_n),
                                               float(bf.out_no2_n), fit_u)
                if no2_check and no2_check["flag"] != "sospechoso":
                    alt = wq.nitrite_outlier(float(bf.in_nh4_n) if bf.in_nh4_n else None,
                                             float(bf.in_no2_n) if bf.in_no2_n else None,
                                             fit_u)
                    if alt and alt["flag"] == "sospechoso":
                        no2_check = alt
            # Contexto: cada bloque del catalogo se enciende solo si aqui
            # llega el dato que lo sustenta. Lo que falta, calla.
            ph_manana = None
            if bf is not None and bf.in_ph is not None and bf.created_at:
                # created_at es la digitacion (~30 min despues de medir): sirve
                # para saber si la muestra es de primera hora, no mas que eso.
                hora = bf.created_at.hour
                if 6 <= hora <= 10:
                    ph_manana = (float(bf.in_ph), hora)
            diag_ctx = {
                "issues": bf_issues,
                "ph_manana": ph_manana,
                # Solo la alcalinidad medida EN esta unidad. La del origen
                # (Central) alimenta el techo de racion, pero atribuirsela a
                # una laguna que no se midio taparia justo el caso interesante.
                "alk": (float(bf.in_alkalinity)
                        if (bf is not None and bf.in_alkalinity is not None)
                        else None),
                "hetero": (wq.heterotrophic_load(
                    float(bf.in_do_mg_l) if bf.in_do_mg_l is not None else None,
                    float(bf.out_do_mg_l) if bf.out_do_mg_l is not None else None,
                    float(bf.in_nh4_n) if bf.in_nh4_n is not None else None,
                    float(bf.out_nh4_n) if bf.out_nh4_n is not None else None)
                    if bf is not None else None),
                "no3_trend": no3_trend.get(u.id),
                "medio": carga_sup.get(u.id),
                # La palanca de bajar ventilacion se paga en oxigeno: el
                # margen del amanecer decide si se puede recomendar.
                "solar": wq.solar_outlook(
                    solar_manana,
                    ((photo.get(u.id) or {}).get("dawn") or {}).get("margin")),
                "feed_cap": feed_cap,
            }
            diag = wq.diagnose(dims, bf, photo.get(u.id), h, no2_check,
                               pond_rows, capacidad, diag_ctx)

            # La barra lateral refleja lo mas critico de las cuatro dimensiones:
            # es lo que la tarjeta muestra, asi que es lo que debe colorearla.
            estados = [d["state"] for d in dims.values()
                       if d["state"] not in ("no_aplica",)]
            if any(e == "alarma" for e in estados):
                rollup = "alarma"
            elif any(e == "alerta" for e in estados):
                rollup = "alerta"
            elif estados and all(e == "sin_dato" for e in estados):
                rollup = "sin_dato"
            else:
                rollup = "ok"
            totals[rollup] = totals.get(rollup, 0) + 1
            # Motivo dominante de la unidad (el más crítico) + cuántos otros hay.
            reason = None
            if unit_reasons:
                unit_reasons.sort(key=lambda t: wq.reason_rank(t[0]), reverse=True)
                top, loc = unit_reasons[0]
                reason = {
                    "text": f"{loc} · {top.text}",
                    "kind": top.kind,
                    "level": top.level,
                    "extra": len(unit_reasons) - 1,
                }
            units_data.append({
                "unit_id": u.id,
                "unit_name": u.name,
                "rollup": rollup,
                "n_ponds": len(u_ponds),
                "has_bf": has_bf,
                "n_alarm": n_alarm,
                "n_alarm_unack": n_alarm_unack,
                "n_alert": n_alert,
                "n_nodata": n_nodata,
                "n_stale": n_stale,
                "pond_rows": pond_rows,
                "bf": bf,
                "bf_levels": bf_levels,
                "bf_conf": bf_conf,
                "dims": dims,
                "accion": accion,
                "capacidad": capacidad,
                "feed_cap": feed_cap,
                "alk_origen": alk_origen,
                "flow_nominal": bool(u.flow_is_nominal),
                "recirc_l_s": float(u.recirculation_l_s) if u.recirculation_l_s else None,
                "health": bf_health.get(u.id),
                "diag": diag,
                "health": bf_health.get(u.id),
                "photo": photo.get(u.id),
                "reason": reason,
            })

        context = {
            "request": request,
            "msg": msg,
            "units": units_data,
            "totals": totals,
            # Pronostico del sitio: una vez, arriba. Marca los dias como
            # "peligro" solo si ademas hay floracion detectada en alguna
            # laguna -- un dia de sol sin floracion no tiene nada que contener.
            "solar_dias": wq.solar_forecast_strip(
                solar_dias,
                sum(1 for u in units_data if u.get("photo")),
                now.date()),
            "solar_lagunas": [u["unit_name"] for u in units_data if u.get("photo")],
            "open_unit": open,
            "o2_stale_hours": O2_STALE_HOURS,
        }
        html = jinja_env.get_template("calidad_agua_panel.html").render(context)
        return HTMLResponse(content=html)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Oxígeno: formulario + alta
# ---------------------------------------------------------------------------
@router.get("/oxigeno/nueva", response_class=HTMLResponse)
def oxigeno_form(request: Request, pond_id: Optional[int] = None):
    db = SessionLocal()
    try:
        ponds = (
            db.query(Pond)
            .filter(Pond.state != "inactive", Pond.parent_pond_id.is_(None))
            .order_by(Pond.name)
            .all()
        )
        th = load_thresholds(db)
        context = {
            "request": request,
            "ponds": [{"id": p.id, "name": p.name} for p in ponds],
            "users": _active_users(db),
            "selected_pond": pond_id,
            "now_local": datetime.now().strftime("%Y-%m-%dT%H:%M"),
            "altitude_m": wq.SITE_ALTITUDE_M,
            # Umbrales para evaluar en vivo (color + confirmación) del lado cliente.
            "o2_thresholds": {"do_mg_l": th["o2_do_mg_l"], "saturation": th["o2_saturation"],
                              "water_temp": th.get("water_temp")},
            "corrective_actions": CORRECTIVE_ACTIONS,
        }
        html = jinja_env.get_template("calidad_agua_oxigeno_form.html").render(context)
        return HTMLResponse(content=html)
    finally:
        db.close()


@router.post("/oxigeno")
def oxigeno_create(
    pond_id: int = Form(...),
    reading_datetime: str = Form(...),
    do_mg_l: Optional[str] = Form(None),
    water_temp_c: Optional[str] = Form(None),
    saturation_pct: Optional[str] = Form(None),
    ph: Optional[str] = Form(None),
    operator_id: Optional[str] = Form(None),
    observation: Optional[str] = Form(None),
    acknowledged: Optional[str] = Form(None),
    corrective_action: Optional[str] = Form(None),
):
    db = SessionLocal()
    try:
        try:
            rdt = datetime.fromisoformat(reading_datetime)
        except (ValueError, TypeError):
            rdt = datetime.now()

        do = _parse_decimal(do_mg_l)
        temp = _parse_decimal(water_temp_c)
        sat = _parse_decimal(saturation_pct)

        pond = db.query(Pond).filter(Pond.id == pond_id).first()
        unit_id = pond.cultivation_unit_id if pond else None

        # Herencia de temperatura: si el operador la omite, se toma la de la
        # ronda de esa unidad (la laguna es homogénea).
        temp_inherited = False
        if temp is None:
            temp = recent_unit_temp(db, unit_id, rdt)
            temp_inherited = temp is not None

        thresholds = load_thresholds(db)
        res = wq.evaluate_oxygen(do, temp, sat, thresholds=thresholds)

        reading = PondOxygenReading(
            pond_id=pond_id,
            operator_id=int(operator_id) if operator_id else None,
            reading_datetime=rdt,
            do_mg_l=do,
            water_temp_c=temp,
            water_temp_inherited=temp_inherited,
            saturation_pct=sat,
            # Opcional: la ronda normal no mide pH. Cuando viene, alimenta el
            # diagnostico de CO2 de la manana.
            ph=_parse_decimal(ph),
            saturation_computed_pct=res.saturation_computed_pct,
            consistency_flag=res.consistency_flag,
            alarm_level=res.alarm_level,
            # El acknowledgment solo aplica a lecturas en alarma; en ok/alerta se
            # ignora lo que mande el cliente para no marcar como "reconocidas"
            # lecturas que no lo requieren.
            acknowledged=(res.alarm_level == "alarma" and bool(acknowledged)),
            corrective_action=((corrective_action or None) if res.alarm_level == "alarma" else None),
            observation=(observation or None),
            created_at=datetime.now(),
        )
        db.add(reading)
        db.commit()

        msg = f"Lectura de O2 registrada (estado: {res.alarm_level}"
        if res.alarm_level == "alarma" and reading.corrective_action:
            msg += f", acción: {reading.corrective_action}"
        if res.consistency_flag == "sospechoso":
            msg += ", terna sospechosa"
        msg += ")."
        url = f"/views/ui/calidad-agua?msg={quote_plus(msg)}"
        if unit_id:
            url += f"&open={unit_id}"
        return RedirectResponse(url=url, status_code=303)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Biofiltro: formulario + alta
# ---------------------------------------------------------------------------
# --- Fixtures del espejo JS -------------------------------------------------
# El formulario evalúa en vivo repitiendo en JS las fórmulas del motor (NH3 no
# ionizado, N total, incertidumbre del balance). Para que esa copia no se
# desalinee en silencio, /biofiltro/nueva?selftest=1 manda estos casos con el
# resultado calculado por Python; el JS los recalcula y reporta diferencias en
# la consola del navegador. Es una verificación, no parte del ingreso.
_BF_SELFTEST_READINGS = [
    # Muestreo sano
    {"in_ph": 7.2, "in_temp_c": 17.0, "in_nh4_n": 0.40, "in_no2_n": 0.05, "in_no3_n": 12.0,
     "out_ph": 7.1, "out_temp_c": 17.2, "out_nh4_n": 0.12, "out_no2_n": 0.06, "out_no3_n": 12.3},
    # pH alto -> NH3 no ionizado en alarma con el mismo amonio total
    {"in_ph": 8.6, "in_temp_c": 20.0, "in_nh4_n": 0.40, "in_no2_n": 0.05, "in_no3_n": 12.0,
     "out_ph": 8.5, "out_temp_c": 20.1, "out_nh4_n": 0.35, "out_no2_n": 0.06, "out_no3_n": 12.2},
    # Coma corrida en nitrato de salida -> balance de N desbalanceado
    {"in_ph": 7.2, "in_temp_c": 17.0, "in_nh4_n": 0.40, "in_no2_n": 0.05, "in_no3_n": 1.2,
     "out_ph": 7.1, "out_temp_c": 17.2, "out_nh4_n": 0.12, "out_no2_n": 0.06, "out_no3_n": 12.0},
    # Nitrito alto (alarma biológica real)
    {"in_ph": 7.0, "in_temp_c": 16.0, "in_nh4_n": 0.20, "in_no2_n": 0.80, "in_no3_n": 8.0,
     "out_ph": 7.0, "out_temp_c": 16.1, "out_nh4_n": 0.10, "out_no2_n": 0.75, "out_no3_n": 8.2},
    # Incompleto: sin pH de entrada no se puede evaluar NH3 de entrada
    {"in_ph": None, "in_temp_c": 17.0, "in_nh4_n": 0.40, "in_no2_n": 0.05, "in_no3_n": 12.0,
     "out_ph": 7.1, "out_temp_c": 17.2, "out_nh4_n": 0.12, "out_no2_n": 0.06, "out_no3_n": 12.3},
]


def _bf_selftest_cases(thresholds: dict, test_specs: dict) -> list:
    """Casos + resultado del motor Python, para que el espejo JS se compare."""
    cases = []
    for reading in _BF_SELFTEST_READINGS:
        res = wq.evaluate_biofilter(reading, thresholds=thresholds, test_specs=test_specs)
        cases.append({
            "reading": reading,
            "expected": {
                "tn_in": res.tn_in, "tn_out": res.tn_out,
                "nh3_n_in": res.nh3_n_in, "nh3_n_out": res.nh3_n_out,
                "n_balance_flag": res.n_balance_flag,
                "ph_delta_flag": res.ph_delta_flag,
                "temp_delta_flag": res.temp_delta_flag,
                "alarm_level": res.alarm_level,
                "n_balance_uncertainty": res.detail.get("n_balance_uncertainty"),
            },
        })
    return cases


# Campos numéricos del muestreo, en el orden en que se digitan. Se usan para
# armar el contexto del muestreo anterior (pista "ant.:") del lado cliente.
_BF_NUM_FIELDS = ["in_ph", "in_temp_c", "in_nh4_n", "in_no2_n", "in_no3_n",
                  "out_ph", "out_temp_c", "out_nh4_n", "out_no2_n", "out_no3_n"]


def _bf_prev_by_unit(db: Session) -> dict:
    """Último muestreo por unidad, serializado para el form (pista de digitación).

    El operador elige la unidad dentro del formulario, así que se mandan todas
    y el cliente muestra la que corresponda al cambiar el selector.
    """
    out = {}
    for uid, r in _latest_bf_by_unit(db).items():
        vals = {f: (float(getattr(r, f)) if getattr(r, f) is not None else None)
                for f in _BF_NUM_FIELDS}
        vals["reading_date"] = r.reading_date.strftime("%d-%m-%Y") if r.reading_date else None
        out[str(uid)] = vals
    return out


@router.get("/biofiltro/nueva", response_class=HTMLResponse)
def biofiltro_form(request: Request, unit_id: Optional[int] = None,
                   selftest: Optional[int] = None, edit: Optional[int] = None):
    db = SessionLocal()
    try:
        units = db.query(CultivationUnit).order_by(CultivationUnit.name).all()
        th = load_thresholds(db)
        # Edicion: se precarga el muestreo y el POST lo actualiza en vez de crear.
        editing = (db.query(BiofilterReading).get(edit) if edit else None)
        context = {
            "request": request,
            "units": [{"id": u.id, "name": u.name} for u in units],
            "users": _active_users(db),
            "selected_unit": (editing.cultivation_unit_id if editing else unit_id),
            "today": date.today().strftime("%Y-%m-%d"),
            "editing": editing,
            "no2_fit": wq.fit_nitrite_relation(_nitrite_pairs(db)),
            # Umbrales y specs para evaluar en vivo del lado cliente (espejo del
            # motor). Los NÚMEROS siguen viniendo de la BD; el JS solo repite las
            # fórmulas (NH3 no ionizado y suma en cuadratura de la incertidumbre).
            "bf_thresholds": {
                "nh3_n": th["nh3_n"],
                "nitrite_n": th["nitrite_n"],
                "ph_delta_tol": th["ph_delta_tol"],
                "temp_delta_tol": th["temp_delta_tol"],
                "n_balance_k": th.get("n_balance_k") or wq.DEFAULT_THRESHOLDS["n_balance_k"],
            },
            "bf_test_specs": load_test_specs(db),
            "prev_by_unit": _bf_prev_by_unit(db),
            # ?selftest=1 corre los fixtures del motor contra el espejo JS y
            # reporta en consola. Solo para verificar, no afecta el ingreso.
            "selftest_cases": _bf_selftest_cases(th, load_test_specs(db)) if selftest else None,
        }
        html = jinja_env.get_template("calidad_agua_biofiltro_form.html").render(context)
        return HTMLResponse(content=html)
    finally:
        db.close()


def _nitrite_pairs(db: Session, unit_id: Optional[int] = None) -> list:
    """Pares (NH4, NO2) mas recientes primero, para reajustar la relacion.

    Usa entrada y salida: son dos observaciones independientes de la misma
    relacion. Global por defecto — el coeficiente `a` es estable entre
    unidades (0,123-0,141) mientras que `b` no, y con ~36 pares por unidad
    no hay datos para estimar ambos por separado.
    """
    # Solo unidades que recirculan: la relacion describe biofiltros que
    # nitrifican. Una unidad de flujo abierto tiene nitrito bajo por no
    # producirlo, no por un error de lectura, y marcarla siempre convertiria
    # la alerta en ruido que el operador aprende a ignorar.
    q = (db.query(BiofilterReading)
         .join(CultivationUnit, CultivationUnit.id == BiofilterReading.cultivation_unit_id)
         .filter(CultivationUnit.is_recirculating.is_(True))
         .order_by(BiofilterReading.reading_date.desc(), BiofilterReading.id.desc()))
    if unit_id:
        q = q.filter(BiofilterReading.cultivation_unit_id == unit_id)
    pairs = []
    for r in q.limit(200).all():
        for nh4, no2 in ((r.in_nh4_n, r.in_no2_n), (r.out_nh4_n, r.out_no2_n)):
            if nh4 and no2:
                pairs.append((float(nh4), float(no2)))
    return pairs


@router.post("/biofiltro")
def biofiltro_create(
    reading_id: Optional[str] = Form(None),
    cultivation_unit_id: int = Form(...),
    reading_date: str = Form(...),
    operator_id: Optional[str] = Form(None),
    observation: Optional[str] = Form(None),
    in_ph: Optional[str] = Form(None),
    in_temp_c: Optional[str] = Form(None),
    in_nh4_n: Optional[str] = Form(None),
    in_no2_n: Optional[str] = Form(None),
    in_no3_n: Optional[str] = Form(None),
    out_ph: Optional[str] = Form(None),
    out_temp_c: Optional[str] = Form(None),
    out_nh4_n: Optional[str] = Form(None),
    out_no2_n: Optional[str] = Form(None),
    out_no3_n: Optional[str] = Form(None),
    in_alkalinity: Optional[str] = Form(None),
    in_do_mg_l: Optional[str] = Form(None),
    out_do_mg_l: Optional[str] = Form(None),
    in_nh4_n_2: Optional[str] = Form(None),
    out_nh4_n_2: Optional[str] = Form(None),
    in_no3_n_2: Optional[str] = Form(None),
    out_no3_n_2: Optional[str] = Form(None),
):
    db = SessionLocal()
    try:
        try:
            rdate = date.fromisoformat(reading_date)
        except (ValueError, TypeError):
            rdate = date.today()

        reading_vals = {
            "in_ph": _parse_decimal(in_ph),
            "in_temp_c": _parse_decimal(in_temp_c),
            "in_nh4_n": _parse_decimal(in_nh4_n),
            "in_no2_n": _parse_decimal(in_no2_n),
            "in_no3_n": _parse_decimal(in_no3_n),
            "out_ph": _parse_decimal(out_ph),
            "out_temp_c": _parse_decimal(out_temp_c),
            "out_nh4_n": _parse_decimal(out_nh4_n),
            "out_no2_n": _parse_decimal(out_no2_n),
            "out_no3_n": _parse_decimal(out_no3_n),
        }

        # Campos v2: se guardan tal cual, pero el motor evalua el PROMEDIO de
        # las replicas cuando el duplicado viene cargado.
        extra_vals = {
            "in_alkalinity": _parse_decimal(in_alkalinity),
            "in_do_mg_l": _parse_decimal(in_do_mg_l),
            "out_do_mg_l": _parse_decimal(out_do_mg_l),
            "in_nh4_n_2": _parse_decimal(in_nh4_n_2),
            "out_nh4_n_2": _parse_decimal(out_nh4_n_2),
            "in_no3_n_2": _parse_decimal(in_no3_n_2),
            "out_no3_n_2": _parse_decimal(out_no3_n_2),
        }
        eval_vals = dict(reading_vals)
        for base, dup in (("in_nh4_n", "in_nh4_n_2"), ("out_nh4_n", "out_nh4_n_2"),
                          ("in_no3_n", "in_no3_n_2"), ("out_no3_n", "out_no3_n_2")):
            eval_vals[base] = wq.mean_of_replicates(reading_vals[base], extra_vals[dup])

        thresholds = load_thresholds(db)
        test_specs = load_test_specs(db)
        res = wq.evaluate_biofilter(eval_vals, thresholds=thresholds, test_specs=test_specs)

        # Detector de outlier de nitrito: reajusta al vuelo sobre las ultimas
        # lecturas. No persiste coeficientes — son funcion de datos ya guardados.
        unidad = db.query(CultivationUnit).get(cultivation_unit_id)
        recircula = bool(unidad and unidad.is_recirculating)
        fit = wq.fit_nitrite_relation(_nitrite_pairs(db)) if recircula else None
        no2_checks = [wq.nitrite_outlier(eval_vals["in_nh4_n"], eval_vals["in_no2_n"], fit),
                      wq.nitrite_outlier(eval_vals["out_nh4_n"], eval_vals["out_no2_n"], fit)]
        no2_checks = [c for c in no2_checks if c]
        no2_flag = ("sospechoso" if any(c["flag"] == "sospechoso" for c in no2_checks)
                    else ("ok" if no2_checks else None))

        existing = (db.query(BiofilterReading).get(int(reading_id))
                    if reading_id and reading_id.isdigit() else None)
        if existing is not None:
            # Editar: se conserva created_at (es la hora real del muestreo, a
            # ~30 min de la lectura) y se recalculan todos los derivados.
            existing.cultivation_unit_id = cultivation_unit_id
            existing.operator_id = int(operator_id) if operator_id else None
            existing.reading_date = rdate
            existing.observation = (observation or None)
            existing.tn_in, existing.tn_out = res.tn_in, res.tn_out
            existing.nh3_n_in = (round(res.nh3_n_in, 4) if res.nh3_n_in is not None else None)
            existing.nh3_n_out = (round(res.nh3_n_out, 4) if res.nh3_n_out is not None else None)
            existing.n_balance_flag = res.n_balance_flag
            existing.ph_delta_flag = res.ph_delta_flag
            existing.temp_delta_flag = res.temp_delta_flag
            existing.alarm_level = res.alarm_level
            existing.no2_flag = no2_flag
            for k, v in list(reading_vals.items()) + list(extra_vals.items()):
                setattr(existing, k, v)
            db.commit()
            msg = f"Muestreo #{existing.id} corregido (estado: {res.alarm_level}"
            flags = [f for f, v in (("balance N", res.n_balance_flag),
                                    ("ΔpH", res.ph_delta_flag),
                                    ("Δtemp", res.temp_delta_flag),
                                    ("nitrito", no2_flag)) if v == "sospechoso"]
            if flags:
                msg += "; validación: " + ", ".join(flags)
            msg += ")."
            if ceros:
                msg += (" OJO: " + "; ".join(c["text"] for c in ceros) +
                        (". El cálculo de η ignora ese muestreo."
                         if any(c["breaks_eta"] for c in ceros)
                         else ". Si no lo mediste, deja el campo vacío."))
            return RedirectResponse(
                url=f"/views/ui/calidad-agua?msg={quote_plus(msg)}&open={cultivation_unit_id}",
                status_code=303)

        reading = BiofilterReading(
            cultivation_unit_id=cultivation_unit_id,
            operator_id=int(operator_id) if operator_id else None,
            reading_date=rdate,
            tn_in=res.tn_in,
            tn_out=res.tn_out,
            nh3_n_in=(round(res.nh3_n_in, 4) if res.nh3_n_in is not None else None),
            nh3_n_out=(round(res.nh3_n_out, 4) if res.nh3_n_out is not None else None),
            n_balance_flag=res.n_balance_flag,
            ph_delta_flag=res.ph_delta_flag,
            temp_delta_flag=res.temp_delta_flag,
            alarm_level=res.alarm_level,
            observation=(observation or None),
            created_at=datetime.now(),
            no2_flag=no2_flag,
            **reading_vals,
            **extra_vals,
        )
        db.add(reading)
        db.commit()

        # Cero no es vacio: un colorimetro no lee cero. Avisamos en el mensaje
        # porque el efecto —quedar fuera del calculo de eta— es invisible.
        ceros = wq.concentration_issues(reading_vals, test_specs)

        flags = [f for f, v in (("balance N", res.n_balance_flag), ("ΔpH", res.ph_delta_flag),
                                ("Δtemp", res.temp_delta_flag),
                                ("nitrito", no2_flag)) if v == "sospechoso"]
        if no2_flag == "sospechoso":
            peor = max(no2_checks, key=lambda c: c["sigmas"])
            flags[-1] += (f" (se esperaba ~{peor['esperado']} mg/L, "
                          f"{peor['sigmas']} sigmas)")
        msg = f"Muestreo de biofiltro registrado (estado: {res.alarm_level}"
        if flags:
            msg += "; validación: " + ", ".join(flags)
        msg += ")."
        if ceros:
            msg += (" OJO: " + "; ".join(c["text"] for c in ceros) +
                    (". El cálculo de η ignora ese muestreo."
                     if any(c["breaks_eta"] for c in ceros)
                     else ". Si no lo mediste, deja el campo vacío."))
        url = f"/views/ui/calidad-agua?msg={quote_plus(msg)}&open={cultivation_unit_id}"
        return RedirectResponse(url=url, status_code=303)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Umbrales: vista de configuración (editable)
# ---------------------------------------------------------------------------
# Metadatos de presentación: etiqueta y grupo (biológico vs validación).
THRESHOLD_META = {
    "o2_do_mg_l":         {"label": "O₂ disuelto absoluto", "group": "bio"},
    "o2_saturation":      {"label": "Saturación de O₂", "group": "bio"},
    "nh3_n":              {"label": "Amonio no ionizado (NH₃-N)", "group": "bio"},
    "nitrite_n":          {"label": "Nitrito (NO₂-N)", "group": "bio"},
    "water_temp":         {"label": "Temperatura del agua (alarma por alta)", "group": "bio"},
    "ph_delta_tol":       {"label": "ΔpH entrada→salida", "group": "val"},
    "temp_delta_tol":     {"label": "Δtemperatura entrada→salida", "group": "val"},
    "o2_consistency_tol": {"label": "Consistencia terna O₂/temp/saturación", "group": "val"},
}
# n_balance_k se edita en la página de especificaciones de test, no aquí.
THRESHOLD_ORDER = ["o2_do_mg_l", "o2_saturation", "water_temp", "nh3_n", "nitrite_n",
                   "ph_delta_tol", "temp_delta_tol", "o2_consistency_tol"]
_COMPARATOR_TEXT = {
    "lt": "dispara si es menor que",
    "gt": "dispara si es mayor que",
}


def _num(v):
    return None if v is None else float(v)


@router.get("/umbrales", response_class=HTMLResponse)
def thresholds_form(request: Request, msg: Optional[str] = None):
    db = SessionLocal()
    try:
        by_param = {r.parameter: r for r in db.query(WaterQualityThreshold).all()}
        bio, val = [], []
        for param in THRESHOLD_ORDER:
            r = by_param.get(param)
            if r is None:
                continue
            meta = THRESHOLD_META.get(param, {"label": param, "group": "val"})
            row = {
                "parameter": param,
                "label": meta["label"],
                "unit": r.unit or "",
                "comparator": r.comparator,
                "comparator_text": _COMPARATOR_TEXT.get(r.comparator, r.comparator),
                "alert_value": _num(r.alert_value),
                "alarm_value": _num(r.alarm_value),
                "active": bool(r.active),
                "is_validation": meta["group"] == "val",
            }
            (val if meta["group"] == "val" else bio).append(row)
        context = {"request": request, "msg": msg, "bio_rows": bio, "val_rows": val,
                   "altitude_m": wq.SITE_ALTITUDE_M}
        html = jinja_env.get_template("calidad_agua_umbrales.html").render(context)
        return HTMLResponse(content=html)
    finally:
        db.close()


@router.post("/umbrales")
async def thresholds_save(request: Request):
    form = await request.form()
    db = SessionLocal()
    try:
        rows = {r.parameter: r for r in db.query(WaterQualityThreshold).all()}
        changed = 0
        for param, r in rows.items():
            if param not in THRESHOLD_META:
                continue
            alert = _parse_decimal(form.get(f"alert_{param}"))
            alarm = _parse_decimal(form.get(f"alarm_{param}"))
            active = form.get(f"active_{param}") is not None
            new = (alert, alarm, active)
            old = (_num(r.alert_value), _num(r.alarm_value), bool(r.active))
            if new != old:
                r.alert_value = alert
                r.alarm_value = alarm
                r.active = active
                r.updated_at = datetime.now()
                changed += 1
        db.commit()
        msg = "Umbrales guardados." if changed else "Sin cambios."
        if changed:
            msg = f"{changed} umbral(es) actualizado(s)."
        return RedirectResponse(url=f"/views/ui/calidad-agua/umbrales?msg={quote_plus(msg)}", status_code=303)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# QR por estanque: código estable + hoja imprimible
# ---------------------------------------------------------------------------
def _generate_qr_code() -> str:
    """Token estable y corto para el QR (E + 8 hex)."""
    return "E" + secrets.token_hex(4).upper()


def ensure_pond_qr_codes(db: Session) -> int:
    """Asigna qr_code a los estanques padre activos que aún no tengan uno.

    Idempotente: solo genera para los que falten. Devuelve cuántos creó.
    """
    pending = (
        db.query(Pond)
        .filter(Pond.state != "inactive", Pond.parent_pond_id.is_(None), Pond.qr_code.is_(None))
        .all()
    )
    if not pending:
        return 0
    used = {c for (c,) in db.query(Pond.qr_code).filter(Pond.qr_code.isnot(None)).all()}
    created = 0
    for p in pending:
        code = _generate_qr_code()
        while code in used:
            code = _generate_qr_code()
        p.qr_code = code
        used.add(code)
        created += 1
    db.commit()
    return created


@router.get("/qr", response_class=HTMLResponse)
def qr_labels(request: Request):
    db = SessionLocal()
    try:
        ensure_pond_qr_codes(db)
        unit_names = {u.id: u.name for u in db.query(CultivationUnit).all()}
        ponds = (
            db.query(Pond)
            .filter(Pond.state != "inactive", Pond.parent_pond_id.is_(None))
            .order_by(Pond.name)
            .all()
        )
        # Agrupar por unidad para ordenar la hoja
        groups: dict = {}
        for p in ponds:
            uname = unit_names.get(p.cultivation_unit_id, "Sin unidad")
            groups.setdefault(uname, []).append({
                "pond_name": p.name,
                "unit_name": uname,
                "qr_code": p.qr_code,
                # make_qr fuerza QR estándar (make() elegiría Micro QR para
                # textos cortos, y BarcodeDetector no lee Micro QR).
                "svg": segno.make_qr(p.qr_code, error="m").svg_data_uri(scale=4),
            })
        grouped = [{"unit_name": k, "ponds": v} for k, v in sorted(groups.items())]
        context = {"request": request, "grouped": grouped, "total": len(ponds)}
        html = jinja_env.get_template("calidad_agua_qr.html").render(context)
        return HTMLResponse(content=html)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Especificaciones de tests de N (balance por incertidumbre) — editable
# ---------------------------------------------------------------------------
TEST_ORDER = ["nh4_n", "no2_n", "no3_n"]
TEST_LABELS = {"nh4_n": "Amonio (NH₄-N)", "no2_n": "Nitrito (NO₂-N)", "no3_n": "Nitrato (NO₃-N)"}


def _get_k(db: Session) -> float:
    row = db.query(WaterQualityThreshold).filter_by(parameter="n_balance_k").first()
    if row and row.alarm_value is not None:
        return float(row.alarm_value)
    return 1.0


@router.get("/tests", response_class=HTMLResponse)
def tests_form(request: Request, msg: Optional[str] = None):
    db = SessionLocal()
    try:
        by_test = {s.test: s for s in db.query(WaterQualityTestSpec).all()}
        rows = []
        for t in TEST_ORDER:
            s = by_test.get(t)
            d = wq.DEFAULT_TEST_SPECS[t]
            rows.append({
                "test": t,
                "label": (s.label if s and s.label else TEST_LABELS[t]),
                "resolution": float(s.resolution) if s and s.resolution is not None else d["resolution"],
                "acc_fixed": float(s.acc_fixed) if s and s.acc_fixed is not None else d["acc_fixed"],
                "acc_pct_display": (float(s.acc_pct) if s and s.acc_pct is not None else d["acc_pct"]) * 100.0,
            })
        context = {"request": request, "msg": msg, "rows": rows, "k": _get_k(db)}
        html = jinja_env.get_template("calidad_agua_tests.html").render(context)
        return HTMLResponse(content=html)
    finally:
        db.close()


@router.post("/tests")
async def tests_save(request: Request):
    form = await request.form()
    db = SessionLocal()
    try:
        by_test = {s.test: s for s in db.query(WaterQualityTestSpec).all()}
        for t in TEST_ORDER:
            s = by_test.get(t)
            if s is None:
                s = WaterQualityTestSpec(test=t, label=TEST_LABELS[t])
                db.add(s)
            s.resolution = _parse_decimal(form.get(f"resolution_{t}"))
            s.acc_fixed = _parse_decimal(form.get(f"acc_fixed_{t}"))
            pct = _parse_decimal(form.get(f"acc_pct_{t}"))
            s.acc_pct = (pct / 100.0) if pct is not None else None  # % -> fracción
            s.updated_at = datetime.now()

        # Factor k (en la tabla de umbrales)
        k = _parse_decimal(form.get("k"))
        krow = db.query(WaterQualityThreshold).filter_by(parameter="n_balance_k").first()
        if krow is None:
            krow = WaterQualityThreshold(parameter="n_balance_k", comparator="gt",
                                         unit="×U", active=True)
            db.add(krow)
        krow.alarm_value = k if k is not None else 1.0
        krow.updated_at = datetime.now()

        db.commit()
        return RedirectResponse(
            url=f"/views/ui/calidad-agua/tests?msg={quote_plus('Especificaciones guardadas.')}",
            status_code=303)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Vista por unidad: configuración, desempeño, sugerencia y cargas
# ---------------------------------------------------------------------------
# Se entra desde la tarjeta del panel, no desde la barra. La vista es por
# unidad y casi siempre se llega empujado por un diagnóstico: el diagnóstico
# dice qué pasa, esta vista es donde se actúa.
ALK_TARGET_DEFAULT = 62.0       # mg/L: sostener la alcalinidad de la fuente,
                                # sin pagar por lo que se va por el desagüe
MAX_KG_TURNO_DEFAULT = 60.0     # lo que se reparte parejo en un turno


def _unit_doses(db: Session, unit_id: int, limit: int = 40) -> list:
    return (db.query(WaterTreatmentDose)
            .filter(WaterTreatmentDose.cultivation_unit_id == unit_id)
            .order_by(WaterTreatmentDose.applied_at.desc())
            .limit(limit).all())


def _last_shock_at(doses: list):
    for d in doses:
        if (d.mode or "") == "choque":
            return d.applied_at
    return None


@router.get("/biofiltro/unidad/{unit_id}", response_class=HTMLResponse)
def biofiltro_unidad(request: Request, unit_id: int, msg: Optional[str] = None):
    db = SessionLocal()
    try:
        u = db.query(CultivationUnit).get(unit_id)
        if u is None:
            return RedirectResponse(url="/views/ui/calidad-agua", status_code=303)
        now = datetime.now()
        th = load_thresholds(db)
        no2_fit = wq.fit_nitrite_relation(_nitrite_pairs(db))
        alk_origen = _source_alkalinity(db)
        doses = _unit_doses(db, unit_id)
        photo = _photosynthesis_by_unit(db, th, now).get(unit_id)

        bf = _latest_bf_by_unit(db).get(unit_id)
        etas = _recent_etas(db, unit_id)
        eta = (etas or {}).get("eta")
        health = _biofilter_health_by_unit(db).get(unit_id)

        q_rec = float(u.recirculation_l_s) if u.recirculation_l_s else None
        q_fresh = float(u.freshwater_l_s) if u.freshwater_l_s else None
        vol = float(u.water_volume_m3) if u.water_volume_m3 else None

        # --- desempeño ------------------------------------------------------
        capacidad = wq.biofilter_capacity(eta, q_rec, _reactor_m3(u))
        superficie = wq.media_surface_m2(u.media_volume_m3, u.media_ssa_m2_m3)
        llenado = wq.media_fill(u.media_volume_m3, u.reactor_volume_m3)
        tau_es_estimado = not u.reactor_volume_m3
        n_kg = _unit_feed_n_kg_day(db, unit_id, date.today() - timedelta(days=27),
                                   date.today())
        # TAN removido por el biofiltro = lo que produce el alimento menos lo
        # que se lleva la purga. Es el mismo reparto del indicador de salud.
        tan_removido = None
        if n_kg is not None and bf is not None and bf.in_nh4_n:
            purga = wq.purge_nitrogen_kg_day(q_fresh, float(bf.in_nh4_n)) or 0.0
            tan_removido = max(0.0, n_kg - purga)
        carga_sup = wq.surface_loading(tan_removido, superficie)

        ph = float(bf.in_ph) if (bf is not None and bf.in_ph) else None
        temp = float(bf.in_temp_c) if (bf is not None and bf.in_temp_c) else None
        tan = float(bf.in_nh4_n) if (bf is not None and bf.in_nh4_n) else None

        # --- alcalinidad: medida, o de equilibrio ---------------------------
        alk_medida = (float(bf.in_alkalinity)
                      if (bf is not None and bf.in_alkalinity is not None) else None)
        feed_kg = (n_kg / wq.TAN_PER_FEED_N / 0.48) if n_kg else None
        equilibrio = wq.steady_state_alkalinity(alk_origen, feed_kg, 48.0, q_fresh)
        # Lo medido manda; si no hay medición se usa el equilibrio y se dice.
        alk_actual = alk_medida if alk_medida is not None else (
            equilibrio["alk_equilibrio"] if equilibrio else None)

        objetivo = float(u.alk_target_mg_l) if u.alk_target_mg_l else ALK_TARGET_DEFAULT

        # --- sugerencia -----------------------------------------------------
        choque = wq.shock_plan(vol, alk_actual, objetivo, ph, temp, tan,
                               th["nh3_n"]["alert"],
                               max_kg_turno=MAX_KG_TURNO_DEFAULT)
        mantencion = wq.maintenance_kg_day(feed_kg, 48.0, q_fresh, alk_origen,
                                           objetivo)
        # Sensibilidad del objetivo: cada mg/L sobre la fuente cuesta todos los
        # días, así que conviene verlo antes de fijarlo.
        sensibilidad = []
        for t in (60.0, 65.0, 70.0, 80.0):
            m = wq.maintenance_kg_day(feed_kg, 48.0, q_fresh, alk_origen, t)
            if m:
                sensibilidad.append({"objetivo": t, "kg_dia": m["kg_nahco3_dia"],
                                     "choque": wq.bicarbonate_kg(vol, alk_actual, t)})

        feed_cap = wq.feed_capacity(eta, q_rec, q_fresh, ph, temp, th, no2_fit,
                                    48.0, alk_source=alk_origen)
        # Con la alcalinidad sostenida deja de ser techo: ahí recién la sal
        # compra algo, y por eso se calcula sobre el escenario CON terapia.
        feed_cap_con = wq.feed_capacity(eta, q_rec, q_fresh, ph, temp, th,
                                        no2_fit, 48.0, alk_source=None)
        # El OBJETIVO de cloruro se calcula sobre el escenario con la
        # alcalinidad ya sostenida —ahi el nitrito pasa a mandar—, pero si HOY
        # manda la alcalinidad la sal no compra nada todavia. Son dos
        # preguntas distintas y mezclarlas haria comprar sal antes de tiempo.
        sal_obj = wq.salt_target(feed_cap_con, no2_fit, th["nitrite_n"]["alert"])
        sal_util = bool(feed_cap and feed_cap.get("manda") == "nitrito")
        sal_manda_hoy = (feed_cap or {}).get("manda")
        cl_mod = wq.modeled_chloride(doses, vol, q_fresh,
                                     u.chloride_base_mg_l, now)
        sal = (wq.salt_plan(sal_obj["cloruro_objetivo"],
                            (cl_mod or {}).get("cl_mg_l"), vol, q_fresh)
               if (sal_obj and sal_obj.get("cloruro_objetivo")) else None)

        # --- régimen de muestreo -------------------------------------------
        regimen = wq.regime_for(_last_shock_at(doses), bool(photo), now)

        context = {
            "request": request, "msg": msg, "u": u, "now": now,
            "eta": eta, "eta_n": (etas or {}).get("n"),
            "health": health, "capacidad": capacidad,
            "superficie": superficie, "carga_sup": carga_sup,
            "llenado": llenado, "tau_es_estimado": tau_es_estimado,
            "tan_removido": tan_removido, "n_kg": n_kg, "feed_kg": feed_kg,
            "bf": bf, "ph": ph, "temp": temp, "tan": tan,
            "alk_medida": alk_medida, "alk_origen": alk_origen,
            "equilibrio": equilibrio, "alk_actual": alk_actual,
            "objetivo": objetivo, "choque": choque, "mantencion": mantencion,
            "sensibilidad": sensibilidad,
            "feed_cap": feed_cap, "feed_cap_con": feed_cap_con,
            "sal_obj": sal_obj, "sal": sal, "cl_mod": cl_mod,
            "sal_util": sal_util, "sal_manda_hoy": sal_manda_hoy,
            "regimen": regimen, "regimenes": wq.REGIMES,
            "doses": doses, "photo": photo,
            "max_kg_turno": MAX_KG_TURNO_DEFAULT,
        }
        html = jinja_env.get_template("calidad_agua_biofiltro_unidad.html").render(context)
        return HTMLResponse(content=html)
    finally:
        db.close()


@router.post("/biofiltro/unidad/{unit_id}/config")
def biofiltro_unidad_config(
    unit_id: int,
    reactor_volume_m3: Optional[str] = Form(None),
    media_ssa_m2_m3: Optional[str] = Form(None),
    media_type: Optional[str] = Form(None),
    media_volume_m3: Optional[str] = Form(None),
    water_volume_m3: Optional[str] = Form(None),
    water_volume_is_estimated: Optional[str] = Form(None),
    alk_target_mg_l: Optional[str] = Form(None),
    chloride_base_mg_l: Optional[str] = Form(None),
    chloride_is_measured: Optional[str] = Form(None),
):
    db = SessionLocal()
    try:
        u = db.query(CultivationUnit).get(unit_id)
        if u is None:
            return RedirectResponse(url="/views/ui/calidad-agua", status_code=303)
        for campo, valor in (("reactor_volume_m3", reactor_volume_m3),
                             ("media_ssa_m2_m3", media_ssa_m2_m3),
                             ("media_volume_m3", media_volume_m3),
                             ("water_volume_m3", water_volume_m3),
                             ("alk_target_mg_l", alk_target_mg_l),
                             ("chloride_base_mg_l", chloride_base_mg_l)):
            setattr(u, campo, _num(valor))
        u.media_type = (media_type or None)
        u.water_volume_is_estimated = bool(water_volume_is_estimated)
        u.chloride_is_measured = bool(chloride_is_measured)
        db.commit()
        msg = "Configuración del biofiltro guardada."
        return RedirectResponse(
            url=f"/views/ui/calidad-agua/biofiltro/unidad/{unit_id}?msg={quote_plus(msg)}",
            status_code=303)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Carga de insumos (bicarbonato / sal)
# ---------------------------------------------------------------------------
# Botón global arriba del panel, no enterrado en la tarjeta: es una tarea
# diaria, repetitiva y de otro usuario. Cuatro pasos para anotar 60 kg es
# demasiado.
@router.get("/carga/nueva", response_class=HTMLResponse)
def carga_form(request: Request, unit_id: Optional[int] = None,
               product: Optional[str] = None, edit: Optional[int] = None):
    db = SessionLocal()
    try:
        units = (db.query(CultivationUnit)
                 .filter(CultivationUnit.is_recirculating.is_(True))
                 .order_by(CultivationUnit.name).all())
        editing = (db.query(WaterTreatmentDose).get(edit) if edit else None)
        # El régimen de la unidad decide si se piden las alcalinidades del par
        # dosis-respuesta: en basal no se piden, son calibración, no rutina.
        regimenes = {}
        for u in units:
            doses = _unit_doses(db, u.id, limit=5)
            photo = None
            regimenes[u.id] = wq.regime_for(_last_shock_at(doses), photo,
                                            datetime.now())
        context = {
            "request": request,
            "units": [{"id": u.id, "name": u.name,
                       "alk_target": float(u.alk_target_mg_l) if u.alk_target_mg_l
                       else ALK_TARGET_DEFAULT,
                       "regimen": regimenes.get(u.id, "basal")} for u in units],
            "users": _active_users(db),
            "selected_unit": (editing.cultivation_unit_id if editing else unit_id),
            "product": product or "bicarbonato",
            "editing": editing,
            "now": datetime.now().strftime("%Y-%m-%dT%H:%M"),
            "regimenes": wq.REGIMES,
        }
        html = jinja_env.get_template("calidad_agua_carga_form.html").render(context)
        return HTMLResponse(content=html)
    finally:
        db.close()


@router.post("/carga/nueva")
def carga_create(
    cultivation_unit_id: int = Form(...),
    applied_at: str = Form(...),
    product: str = Form(...),
    kg: str = Form(...),
    mode: str = Form("mantencion"),
    operator_id: Optional[str] = Form(None),
    application_point: Optional[str] = Form(None),
    alk_before: Optional[str] = Form(None),
    alk_after_2h: Optional[str] = Form(None),
    ph_before: Optional[str] = Form(None),
    ph_after_2h: Optional[str] = Form(None),
    observation: Optional[str] = Form(None),
    dose_id: Optional[str] = Form(None),
):
    db = SessionLocal()
    try:
        d = (db.query(WaterTreatmentDose).get(int(dose_id))
             if dose_id else None) or WaterTreatmentDose()
        d.cultivation_unit_id = cultivation_unit_id
        d.applied_at = datetime.fromisoformat(applied_at)
        d.product = product
        d.kg = _num(kg)
        d.mode = mode
        d.operator_id = int(operator_id) if operator_id else None
        d.application_point = application_point or None
        d.alk_before = _num(alk_before)
        d.alk_after_2h = _num(alk_after_2h)
        d.ph_before = _num(ph_before)
        d.ph_after_2h = _num(ph_after_2h)
        d.observation = observation or None
        if d.id is None:
            db.add(d)
        db.commit()

        msg = f"Carga registrada: {d.kg:g} kg de {product}."
        # El par dosis-respuesta es lo unico que calibra el consumo real. Si
        # la carga es de choque y no trae la alcalinidad previa, se avisa:
        # despues no hay forma de reconstruirla.
        if mode == "choque" and d.alk_before is None:
            msg += " Falta la alcalinidad ANTES: sin ella la dosis no calibra nada."
        elif d.alk_before is not None and d.alk_after_2h is None:
            msg += " Recordar la alcalinidad a las 2 h."
        return RedirectResponse(
            url=f"/views/ui/calidad-agua/biofiltro/unidad/{cultivation_unit_id}"
                f"?msg={quote_plus(msg)}",
            status_code=303)
    finally:
        db.close()
