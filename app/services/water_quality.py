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

# --- Rangos físicamente plausibles (sanidad de ingreso) ---
# Fuera de estos límites la lectura es casi seguro un error de tipeo (p. ej. un
# O2 de 45 mg/L o una saturación de 426%). No es una alarma biológica: se marca
# la terna como sospechosa para que el operador confirme y el panel lo destaque.
PLAUSIBLE_DO_MG_L = (0.0, 20.0)    # O2 disuelto; agua dulce fría satura ~14 mg/L
PLAUSIBLE_TEMP_C = (0.0, 30.0)     # temperatura del agua del sitio
PLAUSIBLE_SAT_PCT = (0.0, 150.0)   # saturación (ingresada o teórica)


def oxygen_range_issues(do_mg_l: Optional[float], water_temp_c: Optional[float],
                        saturation_pct: Optional[float],
                        saturation_computed_pct: Optional[float] = None) -> list[str]:
    """Lista de valores fuera de rango físico plausible (vacía si todo ok).

    Compartida por el ingreso (form web + PWA), el backfill y el espejo del
    cliente, para que la regla de sanidad sea única.
    """
    issues: list[str] = []

    def _chk(val, lo_hi, label, unit):
        if val is None:
            return
        lo, hi = lo_hi
        if val < lo or val > hi:
            issues.append(f"{label} {val:g} {unit} fuera de rango ({lo:g}–{hi:g})")

    _chk(do_mg_l, PLAUSIBLE_DO_MG_L, "O₂", "mg/L")
    _chk(water_temp_c, PLAUSIBLE_TEMP_C, "temperatura", "°C")
    _chk(saturation_pct, PLAUSIBLE_SAT_PCT, "saturación", "%")
    _chk(saturation_computed_pct, PLAUSIBLE_SAT_PCT, "saturación teórica", "%")
    return issues

# --- Umbrales por defecto (replican el seed de water_quality_thresholds) ---
# comparator: 'lt' dispara si el valor es menor que el umbral; 'gt' si es mayor.
DEFAULT_THRESHOLDS: dict[str, dict] = {
    "o2_saturation":      {"alert": 70.0,   "alarm": 60.0,   "comparator": "lt"},
    "nh3_n":              {"alert": 0.0125, "alarm": 0.025,  "comparator": "gt"},
    "nitrite_n":          {"alert": 0.10,   "alarm": 0.50,   "comparator": "gt"},
    # Balance de N: factor de cobertura k. Se marca desbalance si
    # |N_ent - N_sal| > k · U, con U la incertidumbre combinada (ver abajo).
    "n_balance_k":        {"alert": None,   "alarm": 1.0,    "comparator": "gt"},
    "ph_delta_tol":       {"alert": 0.30,   "alarm": 0.50,   "comparator": "gt"},
    "temp_delta_tol":     {"alert": 1.0,    "alarm": None,   "comparator": "gt"},
    "o2_consistency_tol": {"alert": None,   "alarm": 10.0,   "comparator": "gt"},
}

# --- Especificaciones de los tests de N (reactivos comerciales) ---
# acc_fixed + acc_pct·lectura = error (accuracy) de cada lectura, en mg/L como N.
# La resolución es referencia (no entra en el cálculo). Editable en BD.
DEFAULT_TEST_SPECS: dict[str, dict] = {
    "nh4_n": {"resolution": 0.01,  "acc_fixed": 0.04,  "acc_pct": 0.04},
    "no2_n": {"resolution": 0.001, "acc_fixed": 0.020, "acc_pct": 0.04},
    "no3_n": {"resolution": 0.1,   "acc_fixed": 0.5,   "acc_pct": 0.10},
}
# Mapeo de cada lectura del muestreo a su test
_BALANCE_FIELDS = [
    ("in_nh4_n", "nh4_n"), ("in_no2_n", "no2_n"), ("in_no3_n", "no3_n"),
    ("out_nh4_n", "nh4_n"), ("out_no2_n", "no2_n"), ("out_no3_n", "no3_n"),
]


def reading_sigma(value: float, spec: dict) -> float:
    """Error (accuracy) de una lectura: fijo + %·valor, en mg/L como N."""
    return spec["acc_fixed"] + spec["acc_pct"] * value


def balance_uncertainty(reading: dict, test_specs: Optional[dict] = None) -> Optional[float]:
    """Incertidumbre combinada U del balance de N (suma en cuadratura de los
    6 errores de lectura). None si falta alguna de las 6 especies."""
    specs = test_specs or DEFAULT_TEST_SPECS
    ssq = 0.0
    for field, test in _BALANCE_FIELDS:
        v = reading.get(field)
        if v is None:
            return None
        s = reading_sigma(float(v), specs.get(test) or DEFAULT_TEST_SPECS[test])
        ssq += s * s
    return ssq ** 0.5

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

    # Consistencia: (a) valores fuera de rango físico plausible, o
    # (b) saturación ingresada vs teórica fuera de tolerancia -> sospechoso.
    suspect = bool(oxygen_range_issues(do_mg_l, water_temp_c, saturation_pct, computed))
    if computed is not None and saturation_pct is not None:
        diff = abs(computed - float(saturation_pct))
        suspect = suspect or eval_threshold(diff, th["o2_consistency_tol"]) != "ok"
    res.consistency_flag = "sospechoso" if suspect else "ok"

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


def evaluate_biofilter(reading: dict, thresholds: Optional[dict] = None,
                       test_specs: Optional[dict] = None) -> BiofilterResult:
    """Evalúa un muestreo de biofiltro (entrada/salida).

    `reading` con claves: in_ph, in_temp_c, in_nh4_n, in_no2_n, in_no3_n,
    out_ph, out_temp_c, out_nh4_n, out_no2_n, out_no3_n. Valores None se
    toleran (validaciones que dependen de ellos quedan en "ok").

    Balance de N: se compara la diferencia |N_ent - N_sal| contra k·U, donde U
    es la incertidumbre combinada (cuadratura de los 6 errores de lectura,
    cada uno = acc_fixed + acc_pct·valor) y k el factor de cobertura. Por debajo
    de k·U el desbalance es indistinguible del error de los equipos.
    """
    th = thresholds or DEFAULT_THRESHOLDS
    r = BiofilterResult()

    # --- Balance de nitrógeno (validación, por incertidumbre de medición) ---
    r.tn_in = total_nitrogen(reading.get("in_nh4_n"), reading.get("in_no2_n"), reading.get("in_no3_n"))
    r.tn_out = total_nitrogen(reading.get("out_nh4_n"), reading.get("out_no2_n"), reading.get("out_no3_n"))
    if r.tn_in is not None and r.tn_out is not None:
        diff = abs(r.tn_in - r.tn_out)
        U = balance_uncertainty(reading, test_specs)
        k_spec = th.get("n_balance_k") or {}
        k = k_spec.get("alarm")
        if k is None:
            k = 1.0
        r.detail["n_balance_diff"] = round(diff, 3)
        if U and U > 0:
            ratio = diff / U
            r.detail["n_balance_uncertainty"] = round(U, 3)
            r.detail["n_balance_ratio"] = round(ratio, 2)
            r.n_balance_flag = "sospechoso" if ratio > k else "ok"

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
