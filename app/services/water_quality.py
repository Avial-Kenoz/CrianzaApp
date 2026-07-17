"""Motor de calidad de agua (química pura, sin acceso a BD).

Dos familias de cálculo:

1. Oxígeno en estanques:
   - Solubilidad de O2 en saturación Cs(T) para agua dulce (Benson-Krause /
     APHA), corregida por la presión barométrica de la altitud del sitio.
   - Saturación teórica = O2_medido / Cs(T) * 100, usada como checksum de la
     terna (O2, temperatura, saturación) que ingresa el operador.
   - Nivel de alarma por saturación (<70% alerta, <60% alarma por default).

2. Biofiltro (entrada/salida):
   - Balance de nitrógeno: N total = NH4-N + NO2-N + NO3-N (todo como N), la
     suma debe conservarse entrada→salida dentro de tolerancia (validación).
   - ΔpH y Δtemperatura acotados por residencia corta (validación).
   - NH3 no ionizado = f(pH, T) * NH4-N; alarma de amonio efectivo.
   - Nitrito (NO2-N): alerta/alarma por umbral.

Las funciones son puras: reciben los umbrales como argumento (dict con la forma
de DEFAULT_THRESHOLDS). El router carga los umbrales de la BD y los inyecta; los
defaults de este módulo replican el seed de la migración para poder testear y
correr sin BD.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

# --- Constantes de sitio (editables; agua dulce, un solo valor de altitud) ---
SITE_ALTITUDE_M: float = 170.0   # Parral, VII región (Google Earth)
SITE_SALINITY: float = 0.0       # agua dulce

# --- Umbrales por defecto (replican el seed de water_quality_thresholds) ---
# comparator: 'lt' dispara si el valor es menor que el umbral; 'gt' si es mayor.
DEFAULT_THRESHOLDS: dict[str, dict] = {
    "o2_saturation":      {"alert": 70.0,   "alarm": 60.0,   "comparator": "lt"},
    "nh3_n":              {"alert": 0.0125, "alarm": 0.025,  "comparator": "gt"},
    "nitrite_n":          {"alert": 0.10,   "alarm": 0.50,   "comparator": "gt"},
    "n_balance_tol":      {"alert": None,   "alarm": 12.0,   "comparator": "gt"},
    "ph_delta_tol":       {"alert": 0.30,   "alarm": 0.50,   "comparator": "gt"},
    "temp_delta_tol":     {"alert": 1.0,    "alarm": None,   "comparator": "gt"},
    "o2_consistency_tol": {"alert": None,   "alarm": 10.0,   "comparator": "gt"},
}

# Orden de severidad para combinar niveles
_SEVERITY = {"ok": 0, "alerta": 1, "alarma": 2}


def worst_level(*levels: Optional[str]) -> str:
    """Devuelve el nivel más severo entre varios (ignora None)."""
    out = "ok"
    for lv in levels:
        if lv and _SEVERITY.get(lv, 0) > _SEVERITY[out]:
            out = lv
    return out


def eval_threshold(value: Optional[float], spec: dict) -> str:
    """Evalúa un valor contra un umbral {alert, alarm, comparator}."""
    if value is None:
        return "ok"
    alert, alarm, comp = spec.get("alert"), spec.get("alarm"), spec.get("comparator", "gt")
    if comp == "lt":  # valores bajos son malos (p. ej. saturación de O2)
        if alarm is not None and value < alarm:
            return "alarma"
        if alert is not None and value < alert:
            return "alerta"
    else:  # 'gt': valores altos son malos (amonio, nitrito, deltas)
        if alarm is not None and value > alarm:
            return "alarma"
        if alert is not None and value > alert:
            return "alerta"
    return "ok"


# ---------------------------------------------------------------------------
# Oxígeno
# ---------------------------------------------------------------------------
def do_saturation_mg_l(temp_c: float, altitude_m: float = SITE_ALTITUDE_M) -> float:
    """Solubilidad de O2 en saturación (mg/L), agua dulce, corregida por altitud.

    Cs(T) por la ecuación de Benson-Krause (APHA 4500-O), y corrección de
    presión barométrica (atmósfera estándar ISA) con presión de vapor de agua.
    """
    t_k = temp_c + 273.15
    ln_c0 = (
        -139.34411
        + 1.575701e5 / t_k
        - 6.642308e7 / t_k ** 2
        + 1.243800e10 / t_k ** 3
        - 8.621949e11 / t_k ** 4
    )
    c0 = math.exp(ln_c0)  # mg/L a 1 atm, salinidad 0

    # Presión barométrica a la altitud (atm), atmósfera estándar
    pb = (1.0 - 2.25577e-5 * altitude_m) ** 5.25588
    # Presión de vapor de agua (atm)
    pwv = math.exp(11.8571 - 3840.70 / t_k - 216961.0 / t_k ** 2)
    theta = 0.000975 - 1.426e-5 * temp_c + 6.436e-8 * temp_c ** 2
    fp = ((pb - pwv) * (1.0 - theta * pb)) / ((1.0 - pwv) * (1.0 - theta))
    return c0 * fp


def expected_saturation_pct(do_mg_l: float, temp_c: float,
                            altitude_m: float = SITE_ALTITUDE_M) -> float:
    """Saturación teórica (%) a partir de O2 medido y temperatura."""
    cs = do_saturation_mg_l(temp_c, altitude_m)
    return do_mg_l / cs * 100.0 if cs > 0 else 0.0


@dataclass
class OxygenResult:
    saturation_computed_pct: Optional[float] = None
    consistency_flag: str = "ok"          # ok | sospechoso
    alarm_level: str = "ok"               # ok | alerta | alarma


def evaluate_oxygen(do_mg_l: Optional[float], water_temp_c: Optional[float],
                    saturation_pct: Optional[float],
                    thresholds: Optional[dict] = None,
                    altitude_m: float = SITE_ALTITUDE_M) -> OxygenResult:
    """Evalúa una lectura de O2 en estanque: consistencia de la terna + alarma.

    Basta con dos de (O2, temp, saturación); el tercero valida. Si el operador
    ingresó O2 y temp, se recomputa la saturación y se compara con la ingresada.
    """
    th = thresholds or DEFAULT_THRESHOLDS
    res = OxygenResult()

    computed = None
    if do_mg_l is not None and water_temp_c is not None:
        computed = expected_saturation_pct(do_mg_l, water_temp_c, altitude_m)
        res.saturation_computed_pct = round(computed, 2)

    # Consistencia: comparar saturación ingresada vs teórica
    if computed is not None and saturation_pct is not None:
        diff = abs(computed - float(saturation_pct))
        res.consistency_flag = "sospechoso" if eval_threshold(
            diff, th["o2_consistency_tol"]) != "ok" else "ok"

    # Alarma por saturación (prioriza la ingresada; si falta, usa la teórica)
    sat_for_alarm = saturation_pct if saturation_pct is not None else computed
    if sat_for_alarm is not None:
        res.alarm_level = eval_threshold(float(sat_for_alarm), th["o2_saturation"])

    return res


# ---------------------------------------------------------------------------
# Biofiltro
# ---------------------------------------------------------------------------
def total_nitrogen(nh4_n: Optional[float], no2_n: Optional[float],
                   no3_n: Optional[float]) -> Optional[float]:
    """N total (mg/L como N) = suma de especies. Requiere las tres presentes."""
    parts = [nh4_n, no2_n, no3_n]
    if any(p is None for p in parts):
        return None
    return float(sum(parts))


def unionized_ammonia_n(nh4_n: Optional[float], ph: Optional[float],
                        temp_c: Optional[float]) -> Optional[float]:
    """NH3-N no ionizado (mg/L) a partir de amonio total (como N), pH y temp.

    Fracción no ionizada f = 1 / (1 + 10^(pKa - pH)), con
    pKa = 0.09018 + 2729.92 / T_K  (Emerson et al., 1975).
    """
    if nh4_n is None or ph is None or temp_c is None:
        return None
    t_k = temp_c + 273.15
    pka = 0.09018 + 2729.92 / t_k
    fraction = 1.0 / (1.0 + 10.0 ** (pka - ph))
    return float(nh4_n) * fraction


@dataclass
class BiofilterResult:
    tn_in: Optional[float] = None
    tn_out: Optional[float] = None
    nh3_n_in: Optional[float] = None
    nh3_n_out: Optional[float] = None
    n_balance_flag: str = "ok"     # ok | sospechoso (validación)
    ph_delta_flag: str = "ok"      # ok | sospechoso (validación)
    temp_delta_flag: str = "ok"    # ok | sospechoso (validación)
    alarm_level: str = "ok"        # ok | alerta | alarma (biológico)
    detail: dict = field(default_factory=dict)


def evaluate_biofilter(reading: dict, thresholds: Optional[dict] = None) -> BiofilterResult:
    """Evalúa un muestreo de biofiltro (entrada/salida).

    `reading` con claves: in_ph, in_temp_c, in_nh4_n, in_no2_n, in_no3_n,
    out_ph, out_temp_c, out_nh4_n, out_no2_n, out_no3_n. Valores None se
    toleran (validaciones que dependen de ellos quedan en "ok").
    """
    th = thresholds or DEFAULT_THRESHOLDS
    r = BiofilterResult()

    # --- Balance de nitrógeno (validación) ---
    r.tn_in = total_nitrogen(reading.get("in_nh4_n"), reading.get("in_no2_n"), reading.get("in_no3_n"))
    r.tn_out = total_nitrogen(reading.get("out_nh4_n"), reading.get("out_no2_n"), reading.get("out_no3_n"))
    if r.tn_in is not None and r.tn_out is not None:
        denom = max(r.tn_in, r.tn_out)
        rel_diff_pct = abs(r.tn_in - r.tn_out) / denom * 100.0 if denom > 0 else 0.0
        r.detail["n_balance_diff_pct"] = round(rel_diff_pct, 2)
        r.n_balance_flag = "sospechoso" if eval_threshold(rel_diff_pct, th["n_balance_tol"]) != "ok" else "ok"

    # --- ΔpH y Δtemp (validación) ---
    if reading.get("in_ph") is not None and reading.get("out_ph") is not None:
        dph = abs(float(reading["in_ph"]) - float(reading["out_ph"]))
        r.detail["ph_delta"] = round(dph, 2)
        r.ph_delta_flag = "sospechoso" if eval_threshold(dph, th["ph_delta_tol"]) != "ok" else "ok"
    if reading.get("in_temp_c") is not None and reading.get("out_temp_c") is not None:
        dt = abs(float(reading["in_temp_c"]) - float(reading["out_temp_c"]))
        r.detail["temp_delta"] = round(dt, 2)
        r.temp_delta_flag = "sospechoso" if eval_threshold(dt, th["temp_delta_tol"]) != "ok" else "ok"

    # --- NH3 no ionizado (alarma biológica), evaluado en ambos puntos ---
    r.nh3_n_in = unionized_ammonia_n(reading.get("in_nh4_n"), reading.get("in_ph"), reading.get("in_temp_c"))
    r.nh3_n_out = unionized_ammonia_n(reading.get("out_nh4_n"), reading.get("out_ph"), reading.get("out_temp_c"))
    nh3_level = worst_level(
        eval_threshold(r.nh3_n_in, th["nh3_n"]),
        eval_threshold(r.nh3_n_out, th["nh3_n"]),
    )

    # --- Nitrito (alarma biológica), peor de entrada/salida ---
    nitrite_level = worst_level(
        eval_threshold(reading.get("in_no2_n"), th["nitrite_n"]),
        eval_threshold(reading.get("out_no2_n"), th["nitrite_n"]),
    )

    r.alarm_level = worst_level(nh3_level, nitrite_level)
    return r
