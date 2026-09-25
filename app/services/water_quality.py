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
# Cota FÍSICA de la temperatura = detección de glitch de sensor (p. ej. 950 °C).
# Fuera de esto la lectura no es un dato real: se marca "sospechosa" y no se
# confía en ella (no se computa saturación ni se dispara la alarma de temp).
# NO es la banda biológica: una temp alta REAL (26–40 °C) es una EMERGENCIA que
# sí debe alarmar (umbral 'water_temp'), no un glitch. Por eso el tope es amplio.
PLAUSIBLE_TEMP_C = (0.0, 45.0)
PLAUSIBLE_SAT_PCT = (0.0, 150.0)   # saturación (ingresada o teórica)
# Máximo almacenable en pond_oxygen_readings.saturation_computed_pct = Numeric(6,2).
# Más allá (temp absurda → saturación 1e20+) NO se guarda, para no desbordar la BD.
SAT_COMPUTED_STORAGE_MAX = 9999.99


def temp_is_glitch(temp: Optional[float]) -> bool:
    """True si la temperatura está fuera del rango físico plausible (glitch de
    sensor): no es un dato confiable."""
    return temp is not None and (temp < PLAUSIBLE_TEMP_C[0] or temp > PLAUSIBLE_TEMP_C[1])


def oxygen_range_issues(do_mg_l: Optional[float], water_temp_c: Optional[float],
                        saturation_pct: Optional[float],
                        saturation_computed_pct: Optional[float] = None) -> list[str]:
    """Lista de valores fuera de rango físico plausible (vacía si todo ok).

    Compartida por el ingreso (form web + PWA), el backfill y el espejo del
    cliente, para que la regla de sanidad sea única. Detecta glitches/typos; la
    alarma biológica (O2 bajo / temp alta) va por los umbrales, no por acá.
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
    # O2 disuelto absoluto (mg/L): piso intuitivo e independiente de la
    # temperatura. Dispara aunque falte temp/saturación. Editable en BD.
    "o2_do_mg_l":         {"alert": 6.0,    "alarm": 5.0,    "comparator": "lt"},
    # Temperatura del agua (°C): ALARMA BIOLÓGICA por temp alta. El esturión es
    # pez de agua fría; sobre ~24 °C hay estrés térmico y el agua retiene menos
    # O2. Dispara con valores MAYORES al umbral (comparator 'gt'). Editable.
    "water_temp":         {"alert": 23.0,   "alarm": 25.0,   "comparator": "gt"},
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
    alarm_level: str = "ok"               # ok | alerta | alarma (peor de los tres)
    sat_level: str = "ok"                 # nivel por saturación (%)
    do_level: str = "ok"                  # nivel por O2 absoluto (mg/L)
    temp_level: str = "ok"                # nivel por temperatura alta (°C)


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

    # ¿La temperatura es un glitch de sensor (fuera del rango físico)? No es un
    # dato confiable: no se computa saturación ni se dispara la alarma de temp.
    temp_glitch = temp_is_glitch(water_temp_c)

    computed = None
    if do_mg_l is not None and water_temp_c is not None and not temp_glitch:
        val = expected_saturation_pct(do_mg_l, water_temp_c, altitude_m)
        # Defensa extra: si aun así la saturación no cabe en la columna, no se guarda.
        if math.isfinite(val) and abs(val) <= SAT_COMPUTED_STORAGE_MAX:
            computed = val
            res.saturation_computed_pct = round(val, 2)

    # Consistencia (validación): valores fuera de rango físico (incl. glitch de
    # temp) o saturación ingresada vs teórica fuera de tolerancia -> sospechoso.
    suspect = bool(oxygen_range_issues(do_mg_l, water_temp_c, saturation_pct, computed))
    if computed is not None and saturation_pct is not None:
        diff = abs(computed - float(saturation_pct))
        suspect = suspect or eval_threshold(diff, th["o2_consistency_tol"]) != "ok"
    res.consistency_flag = "sospechoso" if suspect else "ok"

    # Alarma biológica: peor nivel entre saturación (%), O2 absoluto (mg/L) y
    # temperatura alta (°C). La saturación prioriza la INGRESADA; solo cae a la
    # teórica si la temperatura es confiable (no glitch), para que una temp sin
    # sentido no DEGRADE una alarma vía una saturación teórica falsa. El O2
    # absoluto es un piso independiente de la temperatura: siempre aplica.
    sat_for_alarm = saturation_pct
    if sat_for_alarm is None and not temp_glitch:
        sat_for_alarm = computed
    if sat_for_alarm is not None:
        res.sat_level = eval_threshold(float(sat_for_alarm), th["o2_saturation"])
    if do_mg_l is not None:
        do_spec = th.get("o2_do_mg_l") or DEFAULT_THRESHOLDS["o2_do_mg_l"]
        res.do_level = eval_threshold(float(do_mg_l), do_spec)
    # Temperatura alta: solo si es confiable (no glitch).
    if water_temp_c is not None and not temp_glitch:
        temp_spec = th.get("water_temp") or DEFAULT_THRESHOLDS["water_temp"]
        res.temp_level = eval_threshold(float(water_temp_c), temp_spec)
    res.alarm_level = worst_level(res.sat_level, res.do_level, res.temp_level)

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


# ---------------------------------------------------------------------------
# Motivos ("¿por qué está en amarillo/rojo?") para el panel de estado
# ---------------------------------------------------------------------------
# Para cada parámetro: etiqueta corta + palabra de dirección, para armar la
# frase de lectura rápida ("O₂ bajo", "Amonio alto", "Temperatura alta").
_REASON_LABELS: dict[str, tuple[str, str]] = {
    "o2_do_mg_l":    ("O₂", "bajo"),
    "o2_saturation": ("Saturación O₂", "baja"),
    "water_temp":    ("Temperatura", "alta"),
    "nh3_n":         ("Amonio", "alto"),
    "nitrite_n":     ("Nitrito", "alto"),
}
# Severidad del motivo: la sospecha de medición (validación) queda por DEBAJO de
# cualquier alarma biológica, para que el color biológico siga mandando.
_REASON_SEVERITY = {"ok": 0, "sospechoso": 1, "alerta": 2, "alarma": 3}
# Desempate cuando dos motivos comparten nivel (mayor = se muestra antes).
_REASON_PRIORITY = {
    "o2_do_mg_l": 5, "nh3_n": 5, "o2_saturation": 4, "nitrite_n": 4,
    "water_temp": 3, "n_balance": 2, "ph_delta": 2, "temp_delta": 2,
}


@dataclass
class Reason:
    """Un motivo por el que una lectura no está en verde.

    kind: 'bio' (alarma biológica, tiñe la tarjeta) | 'val' (sospecha de
    medición; no implica riesgo biológico). level: alarma|alerta|sospechoso.
    text: frase corta del parámetro (sin el estanque/unidad, que antepone quien
    renderiza). param: clave estable para desempate y estilos.
    """
    kind: str
    level: str
    param: str
    text: str


def reason_rank(r: "Reason") -> tuple:
    """Clave de orden (descendente) para elegir el motivo más crítico."""
    return (_REASON_SEVERITY.get(r.level, 0), _REASON_PRIORITY.get(r.param, 0))


def _sort_reasons(reasons: list["Reason"]) -> list["Reason"]:
    reasons.sort(key=reason_rank, reverse=True)
    return reasons


def _to_float(v) -> Optional[float]:
    """Coacción segura a float (los valores ORM llegan como Decimal)."""
    return None if v is None else float(v)


def oxygen_reasons(do_mg_l, water_temp_c, saturation_pct,
                   thresholds: Optional[dict] = None,
                   altitude_m: float = SITE_ALTITUDE_M) -> list["Reason"]:
    """Motivos biológicos de una lectura de O2, del más severo al menos.

    Reusa `evaluate_oxygen` para que los niveles coincidan exactamente con el
    `alarm_level` persistido (misma decisión, mismos umbrales)."""
    res = evaluate_oxygen(_to_float(do_mg_l), _to_float(water_temp_c),
                          _to_float(saturation_pct), thresholds=thresholds,
                          altitude_m=altitude_m)
    out: list[Reason] = []
    for param, level in (("o2_do_mg_l", res.do_level),
                         ("o2_saturation", res.sat_level),
                         ("water_temp", res.temp_level)):
        if level != "ok":
            label, direction = _REASON_LABELS[param]
            out.append(Reason("bio", level, param, f"{label} {direction}"))
    return _sort_reasons(out)


def biofilter_reasons(reading: dict, thresholds: Optional[dict] = None) -> list["Reason"]:
    """Motivos de un muestreo de biofiltro, del más severo al menos.

    Combina alarmas biológicas (amonio no ionizado y nitrito, por punto de
    entrada/salida) con los flags de validación ya persistidos (balance de N,
    ΔpH, Δtemp) —estos últimos son sospecha de medición, no riesgo biológico.
    `reading` es un mapping con las claves in_/out_ y los *_flag persistidos.
    """
    th = thresholds or DEFAULT_THRESHOLDS
    out: list[Reason] = []
    for point, pt_label in (("in", "entrada"), ("out", "salida")):
        nh3 = unionized_ammonia_n(_to_float(reading.get(f"{point}_nh4_n")),
                                  _to_float(reading.get(f"{point}_ph")),
                                  _to_float(reading.get(f"{point}_temp_c")))
        lvl = eval_threshold(nh3, th["nh3_n"])
        if lvl != "ok":
            out.append(Reason("bio", lvl, "nh3_n", f"Amonio {pt_label} alto"))
        lvl = eval_threshold(_to_float(reading.get(f"{point}_no2_n")), th["nitrite_n"])
        if lvl != "ok":
            out.append(Reason("bio", lvl, "nitrite_n", f"Nitrito {pt_label} alto"))
    for flag, param, text in (
        ("n_balance_flag", "n_balance", "Balance N desbalanceado"),
        ("no2_flag", "no2_outlier", "Nitrito fuera de la curva"),
        ("ph_delta_flag", "ph_delta", "ΔpH anormal"),
        ("temp_delta_flag", "temp_delta", "Δtemp anormal"),
    ):
        if reading.get(flag) == "sospechoso":
            out.append(Reason("val", "sospechoso", param, text))
    return _sort_reasons(out)


def oxygen_field_levels(do_mg_l, water_temp_c, saturation_pct,
                        thresholds: Optional[dict] = None,
                        altitude_m: float = SITE_ALTITUDE_M) -> dict:
    """Nivel (ok|alerta|alarma) por campo de una lectura de O2, para destacar en
    el detalle la celda fuera de rango. Consistente con `evaluate_oxygen`."""
    th = thresholds or DEFAULT_THRESHOLDS
    res = evaluate_oxygen(_to_float(do_mg_l), _to_float(water_temp_c),
                          _to_float(saturation_pct), thresholds=th, altitude_m=altitude_m)
    sat_spec = th.get("o2_saturation") or DEFAULT_THRESHOLDS["o2_saturation"]
    return {
        "do_mg_l": res.do_level,
        "water_temp_c": res.temp_level,
        "saturation_pct": eval_threshold(_to_float(saturation_pct), sat_spec),
        "saturation_computed_pct": eval_threshold(res.saturation_computed_pct, sat_spec),
    }


def biofilter_field_levels(reading: dict, thresholds: Optional[dict] = None) -> dict:
    """Nivel por celda para destacar en el detalle del biofiltro: nitrito y
    amonio no ionizado, en entrada y salida (alarmas biológicas por valor).

    Las validaciones por diferencia (ΔpH / Δtemp / balance N) no colorean celdas
    individuales —el valor suelto no está "fuera de rango"—: se comunican con los
    pills de validación de la fila inferior."""
    th = thresholds or DEFAULT_THRESHOLDS
    nh3_spec = th.get("nh3_n") or DEFAULT_THRESHOLDS["nh3_n"]
    no2_spec = th.get("nitrite_n") or DEFAULT_THRESHOLDS["nitrite_n"]
    out = {}
    for point, nh3_key in (("in", "nh3_n_in"), ("out", "nh3_n_out")):
        out[f"{point}_no2_n"] = eval_threshold(_to_float(reading.get(f"{point}_no2_n")), no2_spec)
        nh3 = unionized_ammonia_n(_to_float(reading.get(f"{point}_nh4_n")),
                                  _to_float(reading.get(f"{point}_ph")),
                                  _to_float(reading.get(f"{point}_temp_c")))
        out[nh3_key] = eval_threshold(nh3, nh3_spec)
    return out


# =============================================================================
# SALUD DEL BIOFILTRO
# =============================================================================
# ADVERTENCIA: todo lo de aqui abajo son coeficientes CALIBRADOS, no constantes
# fisicas. Lo de mas arriba (Emerson, Benson-Krause) es quimica y no se toca; esto
# viene de literatura y de una regresion sobre 45 lecturas del norte, y va a
# cambiar cuando haya mas datos. Se mantienen separados a proposito.
#
# La idea: el biofiltro sigue cinetica de primer orden, C_sal = C_ent * e^(-k*tau).
# Medir k directo exigiria el caudal, que aqui NO se usa: el unico disponible
# viene de un aforo puntual, caro y variable, y fijarlo daria precision falsa.
# En su lugar se combina el balance de masa con la cinetica y el caudal se cancela:
#
#     N producido = Q * (C_ent - C_sal)
#     k           = ln(C_ent/C_sal) * Q / V
#     ------------------------------------------------
#     k = N_producido / (V * C_logmedia)
#
# El N producido sale del alimento registrado. Validado contra el aforo: k por
# ambos caminos coincide dentro de 4-25% y da el mismo veredicto (Sur Oriente al
# 20-29% del norte).

NITRIFICATION_THETA = 1.07      # Arrhenius por °C (literatura)
NITRIFICATION_T_REF = 13.6      # °C de referencia (mediana medida en el norte)
NITRIFICATION_PH_REF = 7.50     # pH de referencia
TAN_PER_FEED_N = 0.092
O2_PER_N = 4.57                 # g O2 por g N nitrificado (estequiometria rigida:
                                # NH4+ + 2 O2 -> NO3- + 2 H+ + H2O)          # kg TAN-N por kg de alimento x fraccion proteica
                                # (Timmons & Ebeling); validado al 2% contra el
                                # balance de masa del norte.
MIN_SAMPLES_FOR_HEALTH = 5      # el test de amonio (+-0,04 mg/L fijo) hace que una
                                # lectura suelta tenga ~30% de error en el delta:
                                # el indicador SOLO tiene sentido promediado.


def nitrification_ph_factor(ph: Optional[float]) -> float:
    """Correccion de velocidad por pH (Metcalf & Eddy): plana >= 7,2, cae bajo eso.

    El ajuste sobre datos propios sugiere que sigue subiendo entre 7,2 y 8,0
    (x1,60 por +0,5 de pH), pero es observacional. Se usa el valor conservador de
    literatura; el experimento de bicarbonato en Sur Oriente distingue cual vale.
    """
    if ph is None:
        return 1.0
    return 1.0 if ph >= 7.2 else max(0.10, 1.0 - 0.833 * (7.2 - float(ph)))


def nitrification_rate_factor(temp_c: Optional[float], ph: Optional[float]) -> float:
    """Factor de velocidad respecto a las condiciones de referencia. Vale 1 en 13,6 °C / pH 7,50."""
    t = NITRIFICATION_T_REF if temp_c is None else float(temp_c)
    return (NITRIFICATION_THETA ** (t - NITRIFICATION_T_REF)
            * nitrification_ph_factor(ph) / nitrification_ph_factor(NITRIFICATION_PH_REF))


def log_mean_concentration(c_in: Optional[float], c_out: Optional[float]) -> Optional[float]:
    """Concentracion media logaritmica a lo largo del filtro (la que ve la reaccion)."""
    if not c_in or not c_out or c_in <= 0 or c_out <= 0:
        return None
    ci, co = float(c_in), float(c_out)
    if abs(ci - co) < 1e-9:
        return ci
    if co >= ci:                      # sin remocion: no hay media logaritmica util
        return None
    return (ci - co) / math.log(ci / co)


def feed_nitrogen_kg_day(feed_kg_day: Optional[float], protein_pct: Optional[float]) -> Optional[float]:
    """N amoniacal que produce una racion diaria (kg N/dia)."""
    if feed_kg_day is None or protein_pct is None:
        return None
    return float(feed_kg_day) * (float(protein_pct) / 100.0) * TAN_PER_FEED_N


def purge_nitrogen_kg_day(freshwater_l_s: Optional[float],
                          pond_nh4_n: Optional[float]) -> float:
    """N que se lleva la purga, en kg N/dia.

    La laguna recibe agua fresca y descarga el mismo caudal a su propia
    concentracion. Ese nitrogeno NO pasa por el biofiltro y no debe atribuirsele.
    Pesa entre 16% y 26% del total segun lo sucia que este el agua.
    """
    if not freshwater_l_s or not pond_nh4_n:
        return 0.0
    return float(freshwater_l_s) * float(pond_nh4_n) * 86400.0 / 1e6


def biofilter_k(n_produced_kg_day: Optional[float], media_volume_m3: Optional[float],
                c_in: Optional[float], c_out: Optional[float]) -> Optional[float]:
    """Constante de velocidad volumetrica del biofiltro, en 1/h. Sin caudal.

    Comparable entre unidades de distinto tamano y distinto caudal, que es lo que
    la eficiencia por si sola no permite.
    """
    c_log = log_mean_concentration(c_in, c_out)
    if not n_produced_kg_day or not media_volume_m3 or not c_log:
        return None
    volume_l = float(media_volume_m3) * 1000.0
    # kg N/dia -> mg/dia ; dividido por (L * mg/L) da 1/dia ; /24 -> 1/h
    return float(n_produced_kg_day) * 1e6 / (volume_l * c_log) / 24.0


def normalized_biofilter_k(k_per_hour: Optional[float], temp_c: Optional[float],
                           ph: Optional[float]) -> Optional[float]:
    """k llevado a 13,6 °C y pH 7,50, para poder comparar entre estaciones."""
    if k_per_hour is None:
        return None
    factor = nitrification_rate_factor(temp_c, ph)
    return k_per_hour / factor if factor else None


def nob_aob_balance(nh4_in: Optional[float], nh4_out: Optional[float],
                    no2_in: Optional[float], no2_out: Optional[float]) -> Optional[float]:
    """Razon NOB/AOB: si la segunda etapa (nitrito->nitrato) sigue el ritmo de la primera.

    Todo el amonio oxidado pasa obligatoriamente por nitrito, asi que lo que
    procesan las NOB es el amonio removido menos lo que se acumulo de nitrito.

        >= 1,00  al dia (incluso consumiendo el nitrito que traia el agua)
        <  0,95  las NOB se rezagan: acumulacion, filtro joven o estresado

    Ojo: el delta de nitrito por si solo esta al borde del ruido del test; esta
    razon es util porque el termino dominante es el amonio, 25x su propio ruido.
    """
    if nh4_in is None or nh4_out is None or no2_in is None or no2_out is None:
        return None
    aob = float(nh4_in) - float(nh4_out)
    if aob <= 0:
        return None
    nob = aob - (float(no2_out) - float(no2_in))
    return nob / aob


# =============================================================================
# FOTOSÍNTESIS  (floración de algas)
# =============================================================================
# El oxígeno delata la floración antes que cualquier otra medición: durante el
# día las algas producen O2 y de noche lo consumen. Lo que importa no es la
# floración en sí, sino sus dos consecuencias:
#
#   1. De tarde el pH sube (las algas consumen CO2) y el amonio NO IONIZADO se
#      multiplica. De pH 7,0 a 8,5 el factor es ~29x: el mismo TAN que a las
#      7 AM es inofensivo, a las 4 PM cruza el umbral.
#   2. De madrugada las algas respiran y hunden el O2 justo cuando ya está en
#      su mínimo diario.
#
# Umbrales calibrados sobre 50 pares unidad-semana (jul-sep 2026): la amplitud
# diaria tiene mediana 6,6 y percentil 90 en 14,5 puntos de saturación.
# ---------------------------------------------------------------------------
PHOTO_DAY_HOURS = (11, 17)       # ventana diurna (inclusive)
PHOTO_NIGHT_HOURS = (21, 5)      # cruza medianoche
PHOTO_DAWN_HOURS = (4, 7)        # mínimo diario: las algas ya respiraron toda la noche
PHOTO_WINDOW_DAYS = 7
PHOTO_MIN_READINGS = 20          # por ventana; bajo esto el p90 es demasiado ruidoso
PHOTO_AMPLITUDE_TRIGGER = 15.0   # puntos de saturación (p90 histórico = 14,5)
PHOTO_P90_TRIGGER = 112.0        # % — sobre el techo de aireación de un estanque
                                 # mezclado (~107% a 1,4 m de profundidad)


def percentile(values: list, p: float) -> Optional[float]:
    """Percentil por interpolación lineal. `p` en [0, 1]."""
    if not values:
        return None
    v = sorted(values)
    k = (len(v) - 1) * p
    i = int(k)
    if i + 1 >= len(v):
        return float(v[i])
    return float(v[i] + (k - i) * (v[i + 1] - v[i]))


def ph_for_nh3_limit(nh4_n: Optional[float], temp_c: Optional[float],
                     limit_nh3_n: Optional[float]) -> Optional[float]:
    """pH al que el NH3-N no ionizado alcanza `limit_nh3_n`, dado el TAN.

    Es la inversa de `unionized_ammonia_n`: si f = limit/nh4_n, entonces
    pH = pKa - log10(1/f - 1). Devuelve None si el límite ya se excede con
    f >= 1 (imposible) o si falta algún dato.
    """
    if not nh4_n or nh4_n <= 0 or temp_c is None or not limit_nh3_n:
        return None
    fraction = float(limit_nh3_n) / float(nh4_n)
    if fraction >= 1.0:
        return None          # ni con todo el TAN no ionizado se llega al límite
    pka = 0.09018 + 2729.92 / (float(temp_c) + 273.15)
    return pka - math.log10(1.0 / fraction - 1.0)


def photosynthesis_signal(day_sats: list, night_sats: list,
                          dawn_sats: list, dawn_dos: list,
                          thresholds: Optional[dict] = None) -> Optional[dict]:
    """Señal de floración de algas a partir de las saturaciones de O2.

    `day_sats` / `night_sats` / `dawn_sats` son listas de saturación (%) en sus
    ventanas horarias; `dawn_dos` son los mg/L del amanecer. Devuelve None si
    no hay lecturas suficientes para evaluar.

    Enciende si la amplitud diaria supera PHOTO_AMPLITUDE_TRIGGER **o** si el
    percentil 90 diurno supera PHOTO_P90_TRIGGER. Los dos criterios se ganan el
    puesto: cuando la noche también sube, la amplitud se comprime y sólo el p90
    ve la floración.
    """
    if len(day_sats) < PHOTO_MIN_READINGS or len(night_sats) < PHOTO_MIN_READINGS:
        return None

    day_mean = sum(day_sats) / len(day_sats)
    night_mean = sum(night_sats) / len(night_sats)
    amplitude = day_mean - night_mean
    day_p90 = percentile(day_sats, 0.90)

    by_amplitude = amplitude >= PHOTO_AMPLITUDE_TRIGGER
    by_p90 = day_p90 is not None and day_p90 >= PHOTO_P90_TRIGGER
    if by_amplitude and by_p90:
        trigger = "ambos"
    elif by_amplitude:
        trigger = "amplitud"
    elif by_p90:
        trigger = "saturacion"
    else:
        trigger = None

    out = {
        "active": trigger is not None,
        "trigger": trigger,
        "amplitude": round(amplitude, 1),
        "day_p90": round(day_p90, 1) if day_p90 is not None else None,
        "night_mean": round(night_mean, 1),
        "n_day": len(day_sats),
        "n_night": len(night_sats),
        "dawn": None,
    }

    # --- riesgo del amanecer -------------------------------------------------
    # La floración respira de noche. Miramos el percentil 10 del amanecer (no el
    # mínimo, que es una sola lectura) contra los umbrales de O2 del sitio, y
    # cuánto tendría que crecer la caída nocturna para tocar la alerta.
    th = thresholds or {}
    sat_spec = th.get("o2_saturation") or {}
    do_spec = th.get("o2_do_mg_l") or {}
    if len(dawn_sats) >= PHOTO_MIN_READINGS:
        sat_p10 = percentile(dawn_sats, 0.10)
        do_p10 = percentile(dawn_dos, 0.10) if len(dawn_dos) >= PHOTO_MIN_READINGS else None
        sat_alert = sat_spec.get("alert")
        drop = (day_p90 - sat_p10) if (day_p90 is not None and sat_p10 is not None) else None
        margin = (sat_p10 - sat_alert) if (sat_p10 is not None and sat_alert is not None) else None
        factor = None
        if drop and drop > 0 and margin is not None:
            factor = 1.0 + margin / drop
        out["dawn"] = {
            "sat_p10": round(sat_p10, 1) if sat_p10 is not None else None,
            "do_p10": round(do_p10, 2) if do_p10 is not None else None,
            "sat_level": eval_threshold(sat_p10, sat_spec),
            "do_level": eval_threshold(do_p10, do_spec),
            "sat_min": round(min(dawn_sats), 1),
            "drop": round(drop, 1) if drop is not None else None,
            "margin": round(margin, 1) if margin is not None else None,
            "factor_to_alert": round(factor, 1) if factor is not None else None,
            "n": len(dawn_sats),
        }
    return out


# =============================================================================
# PROTOCOLO v2: DUPLICADOS Y DETECTOR DE NITRITO
# =============================================================================
# El scatter dia a dia de ktau se explica 100% por el ruido del kit de amonio
# (sd esperada 0,36 contra 0,19 observada; la parte sistematica se cancela
# porque entrada y salida se miden en la misma sesion). Por eso duplicar la
# lectura rinde mas que acortar el intervalo de muestreo.
# ---------------------------------------------------------------------------
NO2_FIT_MIN_PAIRS = 40      # bajo esto el detector no se activa
NO2_FIT_WINDOW = 60         # pares usados para reajustar (~2 meses de rotacion)
NO2_FLAG_SIGMAS = 2.0       # marca fuera de +-2 sd del residuo


def mean_of_replicates(*values) -> Optional[float]:
    """Promedia las replicas presentes; None si no hay ninguna.

    Permite duplicado opcional en linea: si el operador carga la segunda
    lectura se promedia, si no se usa la simple. No distingue vacio de cero
    por si solo: eso lo hace `_parse_decimal` en el router.
    """
    vals = [float(v) for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def fit_nitrite_relation(pairs: list) -> Optional[dict]:
    """Ajusta NO2 = a * NH4^b por minimos cuadrados en log-log.

    `pairs` es una lista de (nh4_n, no2_n) con ambos > 0, mas reciente primero.
    Usa las ultimas NO2_FIT_WINDOW. Devuelve None si no alcanza el minimo.

    Los coeficientes NO se persisten: son funcion de lecturas que ya estan
    guardadas, asi que cualquier analisis retrospectivo puede recalcularlos
    sobre la ventana que quiera. Guardarlos congelaria una decision que no
    hace falta tomar hoy.
    """
    pts = [(x, y) for x, y in pairs if x and y and x > 0 and y > 0][:NO2_FIT_WINDOW]
    if len(pts) < NO2_FIT_MIN_PAIRS:
        return None
    xs = [math.log(x) for x, _ in pts]
    ys = [math.log(y) for _, y in pts]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return None
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    a = math.exp(my - b * mx)
    residuos = [y - a * x ** b for x, y in pts]
    med = sum(residuos) / n
    sd = math.sqrt(sum((r - med) ** 2 for r in residuos) / n)
    return {"a": a, "b": b, "sd": sd, "n": n}


def nitrite_outlier(nh4_n: Optional[float], no2_n: Optional[float],
                    fit: Optional[dict]) -> Optional[dict]:
    """Evalua una lectura de nitrito contra la relacion ajustada.

    Detecta lo que el balance de N no alcanza a ver: su incertidumbre esta
    dominada 100% por el nitrato (U ~1,24 mg/L), mientras que el residuo de
    esta relacion tiene sd ~0,029. Sirve para errores de digitacion chicos,
    inversiones entrada/salida y muestras cruzadas.

    Advierte, no bloquea.
    """
    if not fit or not nh4_n or not no2_n or nh4_n <= 0:
        return None
    esperado = fit["a"] * float(nh4_n) ** fit["b"]
    residuo = float(no2_n) - esperado
    sigmas = abs(residuo) / fit["sd"] if fit["sd"] > 0 else 0.0
    return {
        "esperado": round(esperado, 3),
        "residuo": round(residuo, 3),
        "sigmas": round(sigmas, 1),
        "flag": "sospechoso" if sigmas > NO2_FLAG_SIGMAS else "ok",
        "n": fit["n"],
    }


def heterotrophic_load(do_in: Optional[float], do_out: Optional[float],
                       nh4_in: Optional[float], nh4_out: Optional[float],
                       q_l_s: Optional[float] = None,
                       area_m2: Optional[float] = None) -> Optional[dict]:
    """Separa el consumo de O2 del biofiltro en nitrificante y heterotrofo.

    La nitrificacion tiene estequiometria rigida (O2_PER_N g O2 por g N), asi
    que el exceso de la caida de OD sobre esa cuenta es respiracion
    heterotrofa: los solidos que llegan al medio y le ganan la superficie a
    las nitrificantes.
    """
    if None in (do_in, do_out, nh4_in, nh4_out):
        return None
    d_total = float(do_in) - float(do_out)
    d_nitrif = (float(nh4_in) - float(nh4_out)) * O2_PER_N
    d_hetero = d_total - d_nitrif
    d_n = float(nh4_in) - float(nh4_out)
    out = {"do_drop": round(d_total, 2), "nitrif": round(d_nitrif, 2),
           "hetero": round(d_hetero, 2),
           # El cociente es lo que se lee: 4,57 es nitrificacion pura.
           "ratio": (round(d_total / d_n, 2) if d_n > 0 else None)}
    if q_l_s and area_m2 and area_m2 > 0:
        out["hetero_g_m2_d"] = round(d_hetero * float(q_l_s) * 0.0864 * 1000 / area_m2, 3)
    return out


# =============================================================================
# COMPUERTA DE ACCION: un dato marcado no debe gatillar una accion
# =============================================================================
# Un nitrito alto mal digitado lleva a dosificar sal, bajar la racion o abrir
# agua — acciones caras y lentas de revertir, tomadas sobre un dato que puede
# ser un dedo mal puesto. Cuando la alarma biologica descansa sobre una lectura
# que la validacion marco como sospechosa, la accion correcta no es la del
# parametro: es REMUESTREAR.
#
# Cada flag pone en duda distintas alarmas:
#   no2_flag        -> solo el nitrito (el amonio sigue siendo confiable)
#   n_balance_flag  -> TODA la muestra (si la masa no cierra, alguna
#                      concentracion esta mal y no sabemos cual)
# ---------------------------------------------------------------------------
_FLAG_COVERS = {
    "no2_flag": {"nitrite_n"},
    "n_balance_flag": {"nitrite_n", "nh3_n"},
}


def sampling_confidence(reading: dict, reasons: list) -> dict:
    """¿Alguna alarma biologica descansa sobre una lectura marcada?

    Devuelve `gate=True` cuando hay al menos una alarma biologica cuyo
    parametro esta cubierto por un flag de validacion activo. En ese caso la
    accion sugerida es remuestrear antes de cualquier otra cosa.
    """
    activos = [f for f in _FLAG_COVERS if reading.get(f) == "sospechoso"]
    cubiertos = set()
    for f in activos:
        cubiertos |= _FLAG_COVERS[f]

    bio = [r for r in reasons if r.kind == "bio"]
    en_duda = [r for r in bio if r.param in cubiertos]
    firmes = [r for r in bio if r.param not in cubiertos]

    out = {
        "flags": activos,
        "gate": bool(en_duda),
        "en_duda": [r.text for r in en_duda],
        "firmes": [r.text for r in firmes],
        "action": None,
    }
    if en_duda:
        que = " y ".join(r.text.lower() for r in en_duda)
        out["action"] = (f"Remuestrear antes de actuar: la alarma de {que} "
                         f"se apoya en una lectura que la validación marcó.")
        if firmes:
            out["action"] += (" El resto de la tarjeta (" +
                              ", ".join(r.text.lower() for r in firmes) +
                              ") no depende de esa lectura.")
    elif activos:
        out["action"] = ("Lectura marcada por validación, pero ninguna alarma "
                         "depende de ella: corregir cuando se pueda, sin urgencia.")
    return out


# =============================================================================
# VACIO vs CERO EN CONCENTRACIONES
# =============================================================================
# Un colorimetro no puede leer cero: tiene un limite de deteccion (la columna
# `resolution` de water_quality_test_specs). Un 0 digitado significa una de dos
# cosas, y son distintas:
#
#   "no lo medi"           -> el campo debe quedar VACIO (None)
#   "salio bajo el limite" -> es un dato real, pero la division de
#                             ln(C_ent/C_sal) no lo admite
#
# El dano de guardarlo como 0.000 no es que rompa el calculo —
# `log_mean_concentration` ya devuelve None— sino que lo excluye SIN AVISAR.
# La lectura desaparece de la salud del biofiltro y nadie se entera.
# ---------------------------------------------------------------------------
CONCENTRATION_FIELDS = {
    "in_nh4_n": "nh4_n", "out_nh4_n": "nh4_n",
    "in_no2_n": "no2_n", "out_no2_n": "no2_n",
    "in_no3_n": "no3_n", "out_no3_n": "no3_n",
}
_FIELD_LABELS = {
    "in_nh4_n": "amonio entrada", "out_nh4_n": "amonio salida",
    "in_no2_n": "nitrito entrada", "out_no2_n": "nitrito salida",
    "in_no3_n": "nitrato entrada", "out_no3_n": "nitrato salida",
}


def concentration_issues(reading: dict,
                         test_specs: Optional[dict] = None) -> list:
    """Campos de concentracion cargados en cero o bajo el limite de deteccion.

    Devuelve una lista de dicts con el campo, su etiqueta, el limite del test y
    el efecto concreto sobre los calculos. No corrige nada: el operador decide
    si queria dejarlo vacio o si la lectura salio realmente bajo el limite.
    """
    specs = test_specs or DEFAULT_TEST_SPECS
    out = []
    for field, test in CONCENTRATION_FIELDS.items():
        v = _to_float(reading.get(field))
        if v is None:
            continue                      # vacio: intencion clara, no hay nada que avisar
        lod = float((specs.get(test) or {}).get("resolution") or 0.0)
        if v > lod:
            continue
        rompe_eta = field in ("in_nh4_n", "out_nh4_n")
        out.append({
            "field": field,
            "label": _FIELD_LABELS[field],
            "value": v,
            "lod": lod,
            "breaks_eta": rompe_eta,
            "text": (f"{_FIELD_LABELS[field]} = {v:g}" +
                     (f" (el test resuelve hasta {lod:g})" if lod else "")),
        })
    return out


# =============================================================================
# LAS CUATRO DIMENSIONES DE LA TARJETA
# =============================================================================
# La tarjeta vigila siempre lo mismo, en el mismo orden, este o no pasando algo.
# Con casillas fijas el ojo aprende donde mirar y solo lee el valor; con chips
# condicionales tiene que leer primero QUE hay.
#
#   Oxigeno       disponibilidad ahora      -> el MINIMO, no el promedio: el
#                                              promedio esconde el estanque malo
#   Biofiltro     nitrificacion             -> mediana de 8 muestreos: la eta de
#                                              una muestra sola es casi todo ruido
#   Fotosintesis  actividad algal           -> detectada / no detectada
#   Validez       confianza en lo anterior  -> transversal, SIN edad propia:
#                                              hereda la del dato sobre el que opina
#
# Un dato viejo es tan critico como uno malo, y "viejo" se deriva de la
# variabilidad de cada senal, no de un numero parejo:
#
#   O2     4 h   el dato ya no describe la laguna (cae al piso en 1,7-4 h)
#   BF    10 d   el valor NO envejece (autocorrelacion ~0); lo que envejece
#                es la vigilancia: cuanto puede degradarse sin que nos enteremos
#   Foto   5 d   la ventana movil de 7 dias queda coja
# ---------------------------------------------------------------------------
DIM_STALE = {                     # (alerta, vencido) en horas
    "o2": (2.0, 4.0),
    "bf": (7 * 24.0, 10 * 24.0),
    "photo": (3 * 24.0, 5 * 24.0),
}

# Catalogo de acciones, de mayor a menor prioridad. Las de DATO van antes que
# las de OPERACION: no tiene sentido sugerir intervenir un biofiltro apoyandose
# en una lectura que la validacion marco.
ACTIONS = [
    ("remuestrear_bf", "Remuestrear biofiltro antes de actuar", "dato"),
    ("tomar_o2",       "Tomar lecturas de O₂", "dato"),
    ("muestrear_bf",   "Muestrear biofiltro", "dato"),
    ("ph_tarde",       "Medir pH y NAT entre 15:00 y 17:00", "operacion"),
    ("od_amanecer",    "Revisar OD del amanecer", "operacion"),
    ("revisar_medio",  "Revisar el medio del biofiltro", "operacion"),
]


FADE_FROM = 0.80          # fraccion de la vida util donde empieza a atenuarse
FADE_FLOOR = 0.18         # opacidad minima justo antes de vencer


def _age_opacity(hours: Optional[float], kind: str) -> float:
    """El dato se desvanece a medida que envejece.

    Convierte el tiempo en algo que se percibe en vez de leerse: a los 3,2 h una
    lectura de O2 ya se ve palida, y al vencer desaparece. La ausencia es la
    alerta — no hace falta un cartel que diga "vencido".
    """
    if hours is None:
        return 1.0
    vida = DIM_STALE[kind][1]
    frac = hours / vida
    if frac <= FADE_FROM:
        return 1.0
    if frac >= 1.0:
        return 0.0
    t = (frac - FADE_FROM) / (1.0 - FADE_FROM)
    return round(1.0 - t * (1.0 - FADE_FLOOR), 2)


def _age_state(hours: Optional[float], kind: str) -> tuple:
    """(estado, texto) de frescura. Sin dato -> 'sin_dato'."""
    if hours is None:
        return "sin_dato", "sin datos"
    alerta, vencido = DIM_STALE[kind]
    estado = "vencido" if hours >= vencido else ("viejo" if hours >= alerta else "ok")
    if hours < 1:
        txt = "recién"
    elif hours < 48:
        txt = f"hace {hours:.0f} h"
    else:
        txt = f"hace {hours / 24:.0f} d"
    return estado, txt


def _worse(a: str, b: str) -> str:
    """El mas severo de dos estados. Los desconocidos no degradan."""
    orden = ["ok", "alerta", "alarma"]
    if a not in orden:
        return b if b in orden else a
    if b not in orden:
        return a
    return a if orden.index(a) >= orden.index(b) else b


def build_dimensions(o2_min: Optional[float], o2_pond: Optional[str],
                     o2_hours: Optional[float], o2_state: str,
                     eta: Optional[float], eta_n: int, eta_hours: Optional[float],
                     eta_state: str, bf_alarm: Optional[str], photo: Optional[dict],
                     photo_hours: Optional[float], marks: list,
                     has_bf: bool = True, photo_evaluable: bool = True) -> dict:
    """Arma las cuatro filas de la tarjeta.

    Cada fila lleva valor, detalle, estado biologico y estado de frescura. Una
    dimension vencida se pinta en rojo AUNQUE su ultimo valor fuera bueno: un
    eta de 0,51 de hace doce dias no es un biofiltro sano, es un biofiltro sin
    vigilancia.
    """
    def fila(label, value, detail, state, hours, kind):
        age_state, age_text = (_age_state(hours, kind) if kind else (None, None))
        opacity = _age_opacity(hours, kind) if kind else 1.0
        # Con la opacidad diciendo la edad, el texto solo estorba mientras el
        # dato esta fresco. Aparece cuando empieza a atenuarse.
        if opacity >= 1.0 and age_state == "ok":
            age_text = None
        if age_state == "vencido":
            # Vencido: el valor NO se muestra. Mostrarlo palido invitaria a
            # leerlo igual, y ya no describe nada.
            state, value, detail = "alarma", "", ""
        elif age_state == "sin_dato":
            state = "sin_dato"
        return {"label": label, "value": value, "detail": detail, "state": state,
                "age_state": age_state, "age_text": age_text, "opacity": opacity}

    dims = {
        "o2": fila("Oxígeno",
                   (f"{o2_min:.0f}%" if o2_min is not None else "—"),
                   (o2_pond or ""), o2_state, o2_hours, "o2"),
        "bf": (fila("Biofiltro",
                    # eta como porcentaje: se lee mas directo que 0,51. El
                    # numero de muestreos no le dice nada a quien mira.
                    (f"{eta * 100:.0f}%" if eta is not None else "—"), "",
                    _worse(eta_state, bf_alarm or "ok"), eta_hours, "bf")
               if has_bf else
               {"label": "Biofiltro", "value": "no aplica", "detail": "",
                "state": "no_aplica", "age_state": None, "age_text": None,
                "opacity": 1.0}),
        "photo": (fila("Fotosíntesis", ("Activa" if photo else "Inactiva"),
                       "", ("alerta" if photo else "ok"), photo_hours, "photo")
                  if photo_evaluable else
                  {"label": "Fotosíntesis", "value": "N/D", "detail": "",
                   "state": "sin_dato", "age_state": None, "age_text": None,
                   "opacity": 1.0}),
    }
    dims["valid"] = {
        "label": "Validez",
        # Solo el conteo: cuales son se ve en el detalle.
        "value": ("OK" if not marks else f"{len(marks)} marca" +
                  ("s" if len(marks) > 1 else "")),
        "detail": "",
        "state": ("ok" if not marks else "alerta"),
        "age_state": None, "age_text": None, "opacity": 1.0,  # transversal: sin edad propia
    }
    return dims


def suggest_action(dims: dict, gate: bool, photo: Optional[dict],
                   health_state: Optional[str]) -> Optional[dict]:
    """La accion de mayor prioridad que este activa, o None."""
    activas = set()
    if gate:
        activas.add("remuestrear_bf")
    if dims["o2"]["age_state"] in ("vencido", "sin_dato"):
        activas.add("tomar_o2")
    if dims["bf"]["state"] != "no_aplica" and             dims["bf"]["age_state"] in ("vencido", "sin_dato"):
        activas.add("muestrear_bf")
    if photo:
        activas.add("ph_tarde")
        dw = photo.get("dawn") or {}
        if dw.get("margin") is not None and dw["margin"] < 10:
            activas.add("od_amanecer")
    if health_state == "bajo":
        activas.add("revisar_medio")
    for key, text, kind in ACTIONS:
        if key in activas:
            return {"key": key, "text": text, "kind": kind,
                    "otras": len(activas) - 1}
    return None
# =============================================================================
# DIAGNOSTICO CONTEXTUAL
# =============================================================================
# No es una lista fija de indicadores estructurales: es una respuesta a QUE
# dimension se encendio. Contesta "por que esta tarjeta esta de ese color" y
# deja al operador dirigir una respuesta ad-hoc.
#
# Tres naturalezas distintas, y conviene no confundirlas:
#   dato        la lectura probablemente esta mal tomada  -> remuestrear
#   coyuntura   algo pasa AHORA (floracion, un estanque)  -> actuar hoy
#   estructura  el biofiltro rinde poco de forma estable  -> intervenir
# ---------------------------------------------------------------------------
#
# Formato fijo y corto:  titulo · cifra · causa probable
#                        medir  -> SOLO parametros de instrumento
#                        senal  -> el umbral numerico que decide
# ---------------------------------------------------------------------------
BUBBLE_CEILING_PCT = 113.6   # techo de la burbuja al pie del difusor (1,4 m)
PH_MORNING_LOW = 6.8         # pH de primera hora bajo el cual pesa el CO2
ALK_WARN = 50.0              # mg/L CaCO3: la nitrificacion empieza a ceder
NO3_TREND_TRIGGER = 1.0      # mg/L por semana: nitrato acumulandose


def biofilter_capacity(eta, q_l_s, reactor_m3=None):
    """CDE, techo de depuracion y vida media a partir de eta y el caudal.

    - CDE (caudal depurado equivalente) = Q*eta: el caudal que saldria en cero
      y haria el mismo trabajo. Se suma con otras vias de remocion; las
      eficiencias no se suman.
    - techo = ktau*Q: el CDE maximo del medio aunque le pases caudal infinito.
    - vida media = ln2/k: cuanto tarda el agua en contacto en perder la mitad
      de su amonio. Comparada con el tiempo de contacto da eta directamente.

    El tiempo de contacto sale del volumen del REACTOR: el agua no circula por
    dentro del plastico. Usar el volumen de medio como sustituto subestima tau
    —163 m3 de medio a 160 L/s dan 17 min, pero si el reactor tiene 220 m3 el
    contacto real son 23— y con eso se deforman la vida media y el uso del
    techo, que es justo el numero que decide si mas caudal sirve de algo.
    """
    if not eta or not q_l_s or eta <= 0 or eta >= 1 or q_l_s <= 0:
        return None
    ktau = -math.log(1.0 - eta)
    out = {"cde": q_l_s * eta, "techo": ktau * q_l_s, "uso_del_techo": eta / ktau}
    if reactor_m3 and reactor_m3 > 0:
        tau_h = reactor_m3 * 1000.0 / q_l_s / 3600.0
        out["tau_min"] = tau_h * 60.0
        out["vida_media_min"] = math.log(2) / (ktau / tau_h) * 60.0
    return out


def _n(x, dec=1):
    """Numero con coma decimal: es lo que lee el operador."""
    return ("{:.%df}" % dec).format(x).replace(".", ",")


def _dg(kind, level, title, text, medir, senal):
    """Un bloque de diagnostico en el formato corto.

    `medir` SOLO admite parametros de instrumento: OD, pH, temperatura, NH4-N,
    NO2-N, NO3-N, alcalinidad por titulacion. eta, k*tau, CDE y salud son
    derivados — se calculan, no se miden — y por eso viven en `senal`.
    """
    return {"kind": kind, "level": level, "title": title, "text": text,
            "medir": medir, "senal": senal}


# --- DATO: la lectura probablemente esta mal tomada -------------------------

def _dg_nitrito_curva(dims, no2_check):
    if dims["valid"]["state"] == "ok" or not no2_check:
        return []
    esperado = no2_check["esperado"]
    medido = esperado + no2_check["residuo"]
    txt = "NO₂ {:.3f} vs {:.3f} esperado del amonio ({}σ).".format(
        medido, esperado, no2_check["sigmas"])
    for alt in (medido / 10.0, medido * 10.0):
        if esperado * 0.6 <= alt <= esperado * 1.6:
            txt += " Con la coma corrida: {:.3f}.".format(alt)
            break
    return [_dg("dato", "alerta", "Nitrito fuera de curva", txt,
                "NO₂-N de esa muestra, por duplicado, misma hora.",
                "Si repite alto es nitrito real: mirar T y OD del reactor.")]


def _dg_balance_n(bf):
    if bf is None or getattr(bf, "n_balance_flag", None) != "sospechoso":
        return []
    return [_dg("dato", "alerta", "El nitrógeno no cierra",
                "Suma de N entrada ≠ salida. El NO₃ domina la incertidumbre.",
                "NH₄-N, NO₂-N y NO₃-N in/out; empezar por el NO₃-N.",
                "Si no cierra de nuevo es método: blanco único, cubeta o reactivo.")]


def _dg_cero_vacio(issues):
    if not issues:
        return []
    campos = ", ".join(i["label"] for i in issues[:3])
    rompe = any(i.get("breaks_eta") for i in issues)
    txt = "{} campo{} en 0 bajo el límite del test: {}.".format(
        len(issues), "s" if len(issues) > 1 else "", campos)
    if rompe:
        txt += " Con amonio en 0 el η no se puede calcular."
    return [_dg("dato", "alerta", "Cero que parece vacío", txt,
                "Re-digitar desde la planilla de terreno.",
                "0,000 exacto en un parámetro que nunca da 0 es casilla vacía.")]


def _dg_terna_o2(pond_rows):
    malos = [p for p in pond_rows if p.get("consistency_flag") == "sospechoso"]
    if not malos:
        return []
    nombres = ", ".join(p["pond_name"].split(" - ")[0].strip() for p in malos[:4])
    return [_dg("dato", "alerta", "Terna de O₂ inconsistente",
                "{} lectura{} donde OD, T y saturación no cierran entre sí: {}.".format(
                    len(malos), "s" if len(malos) > 1 else "", nombres),
                "OD, temperatura y saturación del estanque, otra vez.",
                "Si la terna vuelve a fallar, la sonda está descalibrada.")]


def _dg_sin_vigilancia(dims):
    vencidas = [(k, d) for k, d in dims.items()
                if d.get("age_state") in ("vencido", "sin_dato")]
    if not vencidas:
        return []
    que = {"o2": ("OD y temperatura por estanque", "Oxígeno"),
           "bf": ("NH₄-N y NO₂-N, entrada y salida", "Biofiltro"),
           "photo": ("OD por estanque a lo largo del día", "Fotosíntesis")}
    labels = ", ".join(que.get(k, ("", k))[1] for k, _ in vencidas)
    medir = "; ".join(dict.fromkeys(que.get(k, ("", ""))[0] for k, _ in vencidas
                                    if k in que))
    return [_dg("dato", "alarma", "Sin vigilancia",
                "{} sin dato vigente.".format(labels),
                medir or "El parámetro vencido.",
                "Sin dato fresco el color de la tarjeta no significa nada.")]


# --- COYUNTURA: algo pasa AHORA ---------------------------------------------

def _dg_floracion(photo, solar=None):
    if not photo:
        return []
    dw = photo.get("dawn") or {}
    txt = "Amplitud {} pts · p90 diurno {}%.".format(
        photo["amplitude"], photo["day_p90"])
    if photo.get("ph_alert"):
        txt += " Sobre pH {:.2f} el NH₃ cruza la alerta con {} mg/L de TAN.".format(
            photo["ph_alert"], photo["nh4_n"])
    if dw.get("margin") is not None:
        txt += " Amanecer en {}% ({} pts de margen).".format(
            dw["sat_p10"], dw["margin"])
    sen = ("pH de la tarde > {:.2f}.".format(photo["ph_alert"])
           if photo.get("ph_alert") else "pH de la tarde subiendo día a día.")
    if dw.get("sat_p10") is not None:
        sen += " Amanecer bajo 78% = respiración nocturna pesando."
    lvl = "alarma" if dw.get("sat_level") not in (None, "ok") else "alerta"
    if solar:
        txt += " " + solar["texto"]
        sen = solar["accion"] + " " + sen
    return [_dg("coyuntura", lvl, "Floración de algas activa", txt,
                "pH a las 07:00 y 16:30; NH₄-N junto al pH de la tarde.", sen)]


def _dg_solar(solar, photo):
    """El pronostico solo, cuando NO hay floracion declarada.

    Un dia despejado empuja el pH de la tarde aunque la floracion no cruce el
    gatillo de 15 puntos. Con floracion activa la nota va dentro de ese
    bloque; sin ella vale por si sola, pero solo si dice algo accionable: en
    un dia intermedio `solar_outlook` devuelve None y aqui no aparece nada.
    """
    if photo or not solar or solar.get("nivel") != "alerta":
        return []
    return [_dg("coyuntura", "alerta", "Día despejado mañana", solar["texto"],
                "pH a las 07:00 y a las 16:30.", solar["accion"])]


def _dg_oxigeno(dims, pond_rows):
    if dims["o2"]["state"] not in ("alerta", "alarma") or not pond_rows:
        return []
    malos = [p for p in pond_rows if p.get("alarm_level") in ("alerta", "alarma")]
    tot = len([p for p in pond_rows if p.get("has_data")])
    n = len(malos)
    if not n or not tot:
        return []
    nombres = ", ".join(p["pond_name"].split(" - ")[0].strip() for p in malos[:4])
    if n == 1:
        txt = "Solo {} fuera de rango de {} · difusor o reparto, no la unidad.".format(
            nombres, tot)
    elif n >= tot - 1 and tot > 1:
        txt = "{} de {} fuera de rango · es de la unidad: manifold o carga.".format(n, tot)
    else:
        txt = "{} de {} fuera de rango ({}) · si son contiguos, caída de presión.".format(
            n, tot, nombres)
    return [_dg("coyuntura", dims["o2"]["state"], "Oxígeno bajo", txt,
                "OD de {} y de sus vecinos de rama, cada 2 h.".format(nombres),
                "< 70% de saturación o < 6,0 mg/L: intervenir, no seguir midiendo.")]


def _dg_sobresaturacion(pond_rows):
    """Sobre 113,6% no puede venir de los difusores: a 1,4 m ese es el techo."""
    cand = [p for p in pond_rows
            if p.get("sat_pct") and BUBBLE_CEILING_PCT < p["sat_pct"] < 200]
    if not cand:
        return []
    peor = max(cand, key=lambda p: p["sat_pct"])
    return [_dg("coyuntura", "alerta", "Sobresaturación",
                "{:.0f}% en {} · sobre el techo de burbuja a 1,4 m ({}%).".format(
                    peor["sat_pct"], peor["pond_name"].split(" - ")[0].strip(),
                    _n(BUBBLE_CEILING_PCT)),
                "OD y temperatura al mediodía y a las 17:00.",
                "> {}% sostenido: riesgo de embolia gaseosa.".format(_n(BUBBLE_CEILING_PCT)))]


def _dg_ph_manana(ph_manana):
    """pH de primera hora: el CO2 de la noche es lo que lo hunde."""
    if ph_manana is None:
        return []
    ph, hora = ph_manana
    if ph >= PH_MORNING_LOW:
        return []
    return [_dg("coyuntura", "alerta", "pH de la mañana hundido",
                "pH {:.2f} a las {:02d}:00 · CO₂ acumulado de la noche.".format(ph, hora),
                "pH de entrada del biofiltro a las 07:00 y a las 16:30.",
                "< {} a primera hora: la ventilación de la mañana es la palanca.".format(
                    _n(PH_MORNING_LOW)))]


def _dg_alcalinidad(alk):
    if alk is None:
        return []
    if alk >= ALK_WARN:
        return []
    lvl = "alarma" if alk < ALK_FLOOR else "alerta"
    return [_dg("coyuntura", lvl, "Alcalinidad baja",
                "{:.0f} mg/L CaCO₃ · bajo {:.0f} la nitrificación se vuelve inestable.".format(
                    alk, ALK_FLOOR),
                "Alcalinidad por titulación, entrada de Central y de la unidad.",
                "< {:.0f} alerta, < {:.0f} alarma.".format(ALK_WARN, ALK_FLOOR))]


def _dg_techo_alcalinidad(feed_cap):
    if not feed_cap or feed_cap.get("manda") != "alcalinidad":
        return []
    holgura = feed_cap.get("holgura")
    txt = "El techo lo pone la alcalinidad: {:.0f} kg/día.".format(feed_cap["feed_max"])
    if holgura:
        txt += " El siguiente techo está {:.1f}× más arriba.".format(holgura)
    return [_dg("coyuntura", "alerta", "Ración limitada por alcalinidad", txt,
                "Alcalinidad de entrada por titulación + ración diaria real.",
                "7,14 mg de CaCO₃ por cada mg de N nitrificado.")]


def _dg_nitrato(no3_trend):
    if not no3_trend or no3_trend.get("slope_week") is None:
        return []
    s = no3_trend["slope_week"]
    if s < NO3_TREND_TRIGGER:
        return []
    return [_dg("coyuntura", "alerta", "Nitrato acumulándose",
                "NO₃-N sube {} mg/L por semana ({} muestreos) · purga corta.".format(
                    _n(s), no3_trend.get("n", 0)),
                "NO₃-N de la laguna, semanal, mismo punto y hora.",
                "Si sigue subiendo, falta agua de reposición, no biofiltro.")]


# --- ESTRUCTURA: el biofiltro rinde poco de forma estable -------------------

def _dg_nitrificacion(health, capacity):
    if not health or health.get("state") not in ("bajo", "atencion"):
        return []
    pct = health.get("pct")
    txt = ("{:.0f}% de {} · probable ensuciamiento del medio.".format(
        pct, health.get("ref_kind", "la referencia")) if pct
        else "Bajo la referencia · probable ensuciamiento del medio.")
    if capacity and capacity["uso_del_techo"] > 0.85:
        txt += " Medio al {:.0%} del techo: más caudal no lo arregla.".format(
            capacity["uso_del_techo"])
    return [_dg("estructura", "alarma" if health["state"] == "bajo" else "alerta",
                "Nitrificación baja", txt,
                "OD y NH₄-N, entrada y salida del reactor.",
                "ΔOD/ΔN > {} mg O₂/mg N → heterótrofos.".format(_n(O2_PER_N, 2)))]


def _dg_nob(health):
    if not health or health.get("balance_state") != "rezagado":
        return []
    return [_dg("estructura", "alerta", "NOB por detrás de las AOB",
                "Sale más NO₂ del que entra por N removido · biopelícula joven, T o OD.",
                "NO₂-N entrada y salida + temperatura del reactor.",
                "Brecha creciente muestreo a muestreo; sobre 20 °C las AOB ganan.")]


def _dg_heterotrofa(hetero):
    if not hetero or not hetero.get("ratio"):
        return []
    r = hetero["ratio"]
    if r <= O2_PER_N:
        return []
    return [_dg("estructura", "alerta", "Carga heterótrofa alta",
                "ΔOD/ΔN = {} · el exceso sobre {} es respiración de sólidos.".format(
                    _n(r, 2), _n(O2_PER_N, 2)),
                "OD y NH₄-N, entrada y salida, en cada muestreo.",
                "Sostenido sobre {}: retrolavar y revisar sólidos de entrada.".format(
                    _n(O2_PER_N, 2)))]


SURFACE_OUT_OF_LINE = 1.5       # veces la mediana de las otras unidades


def media_gap(carga, carga_ref, media_m3, reactor_m3, eta, q_l_s):
    """Cuanto medio falta para alinearse con las hermanas, y que compra.

    El objetivo NO sale de la literatura sino de la mediana del sitio: es lo
    que demostradamente funciona en estas lagunas, con esta agua y este medio.

    La proyeccion de eta supone k proporcional a la superficie. Vale mientras
    la carga quede lejos de saturar la biopelicula —y como agregar medio la
    BAJA, el supuesto se cumple mejor despues que antes.
    """
    if not (carga and carga_ref and media_m3 and reactor_m3 and eta and q_l_s):
        return None
    objetivo = float(media_m3) * carga / carga_ref
    tope = float(reactor_m3) * MEDIA_FILL_MAX_PCT / 100.0
    objetivo = min(objetivo, tope)
    if objetivo <= float(media_m3):
        return None
    ktau = -math.log(1.0 - float(eta)) * objetivo / float(media_m3)
    eta_proy = 1.0 - math.exp(-ktau)
    return {
        "media_objetivo": objetivo,
        "agregar": objetivo - float(media_m3),
        "llenado_objetivo": objetivo / float(reactor_m3) * 100.0,
        "eta_proyectado": eta_proy,
        "cde_actual": float(q_l_s) * float(eta),
        "cde_proyectado": float(q_l_s) * eta_proy,
        "ganancia_pct": (eta_proy / float(eta) - 1.0) * 100.0,
        "carga_objetivo": carga * float(media_m3) / objetivo,
    }


def _dg_poco_medio(medio):
    """Poco medio: no hay umbral absoluto, hay comparacion con las hermanas.

    Sur Oriente esta en 0,165 g/m2/d, BAJO los 0,2 de referencia MBBR: un
    umbral de literatura no lo agarraria. Lo que lo delata es que carga 2,5
    veces mas por m2 que las otras lagunas del mismo sitio, con el mismo medio
    y la misma agua. Esa comparacion es medicion, no modelo.

    El llenado decide si hay accion posible: pasado el ~65% el lecho se
    empaqueta y mas plastico deja de ser la respuesta.
    """
    if not medio or medio.get("razon") is None or medio.get("llenado_pct") is None:
        return []
    if medio["razon"] < SURFACE_OUT_OF_LINE:
        return []
    if medio["llenado_pct"] >= MEDIA_FILL_MAX_PCT:
        return [_dg("estructura", "alerta", "Medio saturado y sin espacio",
                    "Carga {} g/m²·d, {}× el resto · llenado {:.0f}%: no cabe más medio.".format(
                        _n(medio["carga"], 3), _n(medio["razon"], 1), medio["llenado_pct"]),
                    "NH₄-N entrada y salida del reactor.",
                    "Con el lecho lleno la palanca ya no es plástico: es reactor o caudal.")]
    return [_dg("estructura", "alerta", "Poco medio en el biofiltro",
                "Carga {} g/m²·d, {}× el resto · llenado {:.0f}%: cabe más medio.".format(
                    _n(medio["carga"], 3), _n(medio["razon"], 1), medio["llenado_pct"]),
                "NH₄-N entrada y salida, antes y después de agregar medio.",
                "Comparar la CARGA SUPERFICIAL, no η: η también se mueve con el "
                "caudal y la ración. Medio nuevo tarda semanas en colonizar.")]


def _dg_techo_hidraulico(capacity, health):
    if not capacity or capacity.get("uso_del_techo") is None:
        return []
    if capacity["uso_del_techo"] < 0.90:
        return []
    if health and health.get("state") in ("bajo", "atencion"):
        return []                       # ya lo dice el bloque de nitrificacion
    return [_dg("estructura", "alerta", "Techo hidráulico agotado",
                "Medio al {:.0%} de su techo · η no sube con más caudal.".format(
                    capacity["uso_del_techo"]),
                "NH₄-N entrada y salida.",
                "Si η cae al subir el caudal, falta medio, no flujo.")]


def diagnose(dims, bf, photo, health, no2_check, pond_rows, capacity=None,
             ctx=None):
    """Catalogo completo, del mas severo al menos.

    Cada bloque se activa SOLO si tiene con que: sin el dato que lo sustenta
    devuelve [] y no aparece. Por eso el catalogo puede ser amplio sin llenar
    la pantalla de casos vacios — el silencio sigue siendo informacion.

    `ctx` trae lo que no cabe en los argumentos fijos: alcalinidad, consumo de
    O2 del reactor, tendencia de nitrato, pH de la manana, techos de racion y
    campos en cero. Todo opcional.
    """
    c = ctx or {}
    out = (
        # dato
        _dg_nitrito_curva(dims, no2_check)
        + _dg_balance_n(bf)
        + _dg_cero_vacio(c.get("issues"))
        + _dg_terna_o2(pond_rows or [])
        + _dg_sin_vigilancia(dims)
        # coyuntura
        + _dg_floracion(photo, c.get("solar"))
        + _dg_solar(c.get("solar"), photo)
        + _dg_oxigeno(dims, pond_rows or [])
        + _dg_sobresaturacion(pond_rows or [])
        + _dg_ph_manana(c.get("ph_manana"))
        + _dg_alcalinidad(c.get("alk"))
        + _dg_techo_alcalinidad(c.get("feed_cap"))
        + _dg_nitrato(c.get("no3_trend"))
        # estructura
        + _dg_nitrificacion(health, capacity)
        + _dg_nob(health)
        + _dg_heterotrofa(c.get("hetero"))
        + _dg_poco_medio(c.get("medio"))
        + _dg_techo_hidraulico(capacity, health)
    )
    sev = {"alarma": 0, "alerta": 1}
    nat = {"dato": 0, "coyuntura": 1, "estructura": 2}
    out.sort(key=lambda d: (sev.get(d["level"], 2), nat[d["kind"]]))
    return out


# =============================================================================
# ALIMENTACION MAXIMA ADMISIBLE Y QUIEN LA LIMITA
# =============================================================================
# La capacidad no es una sola cifra: es el MINIMO de varios techos que se
# mueven con cosas distintas. El de amonio depende del pH y la temperatura; el
# de nitrito, del cloruro y la temperatura. Por eso cual manda cambia de
# estacion, y por eso la sal reordena el ranking sin tocar el biofiltro.
#
#   capacidad = clearance_total  x  min(techo_NH4_por_NH3, techo_NH4_por_NO2)
#
# clearance_total = Q*eta + q*(1-eta). El segundo termino lleva (1-eta) porque
# la purga sale DESPUES del biofiltro: se lleva agua ya depurada una vez.
# ---------------------------------------------------------------------------
FEED_N_FACTOR = 0.092           # kg TAN por kg de proteina (Timmons & Ebeling)
ALK_PER_N = 7.14                # g CaCO3 consumidos por g de N nitrificado
ALK_FLOOR = 40.0                # mg/L: bajo esto la nitrificacion se vuelve inestable


def feed_capacity(eta, q_rec, q_fresh, ph, temp_c, thresholds=None,
                  no2_fit=None, protein_pct=48.0, alk_source=None):
    """Techos de alimentacion y cual manda.

    Devuelve None si falta lo indispensable. Cada techo trae el supuesto del
    que cuelga, para que la UI pueda advertir cuando descansa en un estimado.
    """
    if not eta or not q_rec or eta <= 0 or eta >= 1:
        return None
    th = thresholds or DEFAULT_THRESHOLDS
    k_tan = (protein_pct or 48.0) / 100.0 * FEED_N_FACTOR
    if k_tan <= 0:
        return None

    clearance = q_rec * eta + (q_fresh or 0.0) * (1.0 - eta)
    techos = []

    # --- por amonio no ionizado: se mueve con pH y temperatura -------------
    lim_nh3 = th.get("nh3_n", {}).get("alert")
    if lim_nh3 and ph is not None and temp_c is not None:
        pka = 0.09018 + 2729.92 / (float(temp_c) + 273.15)
        frac = 1.0 / (1.0 + 10.0 ** (pka - float(ph)))
        if frac > 0:
            c_max = lim_nh3 / frac
            techos.append({"key": "amonio", "label": "Amonio (NH₃)",
                           "c_max": c_max,
                           "feed": c_max * clearance * 0.0864 / k_tan,
                           "nota": "se mueve con pH y temperatura"})

    # --- por nitrito: via la relacion empirica ajustada a los datos --------
    lim_no2 = th.get("nitrite_n", {}).get("alert")
    if lim_no2 and no2_fit and no2_fit.get("b"):
        try:
            c_max = (lim_no2 / no2_fit["a"]) ** (1.0 / no2_fit["b"])
            techos.append({"key": "nitrito", "label": "Nitrito (NO₂)",
                           "c_max": c_max,
                           "feed": c_max * clearance * 0.0864 / k_tan,
                           "nota": "sube con cloruro; la sal lo desplaza"})
        except (ValueError, ZeroDivisionError, OverflowError):
            pass

    # --- por alcalinidad: la trae el agua fresca, la consume la nitrificacion
    # `alk_source` es la alcalinidad del agua de entrada. En esta planta toda
    # el agua pasa por la unidad de flujo abierto antes de recircular, asi que
    # la medicion de esa unidad ES la del origen.
    if alk_source and q_fresh and alk_source > ALK_FLOOR:
        neto = (alk_source - ALK_FLOOR) * q_fresh * 0.0864     # kg CaCO3/dia
        techos.append({"key": "alcalinidad", "label": "Alcalinidad",
                       "c_max": None,
                       "feed": neto / ALK_PER_N / k_tan,
                       "nota": "la trae el agua fresca; el bicarbonato la desplaza"})

    if not techos:
        return None
    techos.sort(key=lambda t: t["feed"])
    manda = techos[0]
    return {
        "clearance": clearance,
        "cde": q_rec * eta,
        "aporte_purga": (q_fresh or 0.0) * (1.0 - eta),
        "techos": techos,
        "manda": manda["key"],
        "feed_max": manda["feed"],
        "holgura": (techos[1]["feed"] / manda["feed"]) if len(techos) > 1 else None,
    }


# =============================================================================
# TERAPIA: BICARBONATO Y SAL
# =============================================================================
# Dos dosis distintas, y confundirlas es el error clasico:
#
#   CHOQUE      subir la alcalinidad de golpe          depende del VOLUMEN DE AGUA
#   MANTENCION  compensar lo que consume el biofiltro  depende de la CARGA DE N
#
# En una laguna norte (3.294 m3, 12 L/s de reposicion) el choque de 37 a 80
# mg/L son ~240 kg de bicarbonato y la mantencion ~35 kg/dia: no se parecen.
#
# El tope por turno NO es un numero inventado. Subir alcalinidad sube el pH, y
# el pH sube el NH3 no ionizado: el mismo amonio que a las 7 AM es inofensivo
# a las 4 PM no lo es. El tope sale de ahi.
# ---------------------------------------------------------------------------
NAHCO3_AS_CACO3 = 0.595     # g CaCO3-equiv por g de NaHCO3 (50,04/84,01)
NACL_CL_FRACTION = 0.6066   # g Cl- por g de NaCl (35,45/58,44)
CL_NO2_RATIO = 20.0         # Cl-:NO2-N en masa; 6:1 es el minimo, 20:1 es
                            # el que se usa con especies sensibles


def media_surface_m2(media_m3, ssa_m2_m3):
    """Superficie activa: volumen de medio a granel x superficie especifica.

    Los fabricantes cotizan la SSA por m3 de biomedio COMO SE VIERTE (K1 ~ 500
    m2/m3), asi que el volumen del reactor no entra aqui. Pedir reactor +
    llenado + medio serian tres numeros para dos grados de libertad, y nada
    impediria cargarlos contradictorios.

    Es lo que convierte eta —que depende del tamano de cada laguna— en algo
    comparable entre lagunas y contra la literatura.
    """
    if not media_m3 or not ssa_m2_m3:
        return None
    return float(media_m3) * float(ssa_m2_m3)


MEDIA_FILL_MAX_PCT = 67.0   # sobre esto los biomedios dejan de circular


def media_fill(media_m3, reactor_m3):
    """Llenado = medio / reactor. Se CALCULA, no se pregunta.

    Importa por la mezcla, no por la superficie: pasado el ~67% el lecho se
    empaqueta y los carriers dejan de moverse.
    """
    if not media_m3 or not reactor_m3 or float(reactor_m3) <= 0:
        return None
    pct = float(media_m3) / float(reactor_m3) * 100.0
    return {"pct": pct, "empaquetado": pct > MEDIA_FILL_MAX_PCT,
            "max_pct": MEDIA_FILL_MAX_PCT}


def surface_loading(tan_removed_kg_d, surface_m2):
    """Carga superficial en g TAN/m2/d.

    Referencia de MBBR a 15-20 C: 0,2-0,5 g/m2/d. Bajo 0,1 el medio esta
    sobrado o la biopelicula no esta colonizada; sobre 0,6 se pierde eta.
    """
    if not tan_removed_kg_d or not surface_m2 or surface_m2 <= 0:
        return None
    return tan_removed_kg_d * 1000.0 / surface_m2


def ph_after_alkalinity(ph0, alk0, alk1):
    """pH inmediato despues de subir la alcalinidad, a CO2 constante.

    De CO2 = 0,8794 * Alk / 10^(pH - pK1) se despeja que, con el CO2 sin
    cambiar todavia (que es el caso justo despues de echar el bicarbonato),
    el pH se mueve con el logaritmo de la razon de alcalinidades:

        dpH = log10(Alk1 / Alk0)

    Doblar la alcalinidad son +0,30 de pH. Es el efecto INMEDIATO: despues el
    CO2 se re-equilibra con la ventilacion y el pH sigue subiendo, asi que
    esto es un piso, no un techo.
    """
    if ph0 is None or not alk0 or not alk1 or alk0 <= 0 or alk1 <= 0:
        return None
    return float(ph0) + math.log10(float(alk1) / float(alk0))


def bicarbonate_kg(volume_m3, alk_from, alk_to):
    """kg de NaHCO3 para llevar la alcalinidad de `alk_from` a `alk_to`."""
    if not volume_m3 or alk_from is None or alk_to is None:
        return None
    delta = float(alk_to) - float(alk_from)
    if delta <= 0:
        return 0.0
    kg_caco3 = delta * float(volume_m3) / 1000.0        # mg/L * m3 = g -> kg
    return kg_caco3 / NAHCO3_AS_CACO3


def shock_plan(volume_m3, alk_now, alk_target, ph_now, temp_c, nh4_n,
               nh3_limit, max_kg_turno=None, max_turnos=10):
    """Reparte el choque en turnos, topado por el NH3 y por la mano de obra.

    Son DOS topes y mandan en momentos distintos. Con el pH bajo de la manana
    el NH3 deja mucho margen y el que limita es el operativo: 240 kg de golpe
    no se reparten parejo en una laguna. Con floracion y pH alto manda el NH3.

    Devuelve un turno por aplicacion, cada uno con la alcalinidad y el pH que
    deja, y el NH3 resultante. El tope de cada turno es el que hace que el NH3
    toque el limite; si con `max_turnos` no se llega al objetivo, se devuelve
    lo que se alcanza y `completo=False` — mejor quedarse corto y decirlo que
    proponer una dosis que cruza el umbral.
    """
    if not volume_m3 or alk_now is None or not alk_target:
        return None
    if float(alk_now) >= float(alk_target):
        return {"turnos": [], "completo": True, "kg_total": 0.0,
                "alk_final": float(alk_now)}

    alk = float(alk_now)
    ph = float(ph_now) if ph_now is not None else None
    turnos = []
    for _ in range(max_turnos):
        # Alcalinidad maxima de este turno: la que deja el NH3 en el limite.
        alk_max = float(alk_target)
        if ph is not None and nh4_n and temp_c is not None and nh3_limit:
            # pH que toca el limite con el TAN actual, y la alcalinidad que
            # produce ese pH a CO2 constante.
            ph_max = ph_for_nh3_limit(nh4_n, temp_c, nh3_limit)
            if ph_max is not None:
                if ph >= ph_max:
                    break                      # ya no hay margen: no se dosifica
                alk_max = min(alk_max, alk * 10.0 ** (ph_max - ph))
        # Tope operativo: lo que se alcanza a repartir bien en un turno.
        if max_kg_turno:
            alk_op = alk + float(max_kg_turno) * NAHCO3_AS_CACO3 * 1000.0 / float(volume_m3)
            alk_max = min(alk_max, alk_op)
        paso = alk_max - alk
        if paso <= 0.5:                        # menos de eso no se mide
            break
        kg = bicarbonate_kg(volume_m3, alk, alk_max)
        ph_nuevo = ph_after_alkalinity(ph, alk, alk_max) if ph is not None else None
        turnos.append({
            "kg": kg,
            "alk_desde": alk, "alk_hasta": alk_max,
            "ph_desde": ph, "ph_hasta": ph_nuevo,
            "nh3": unionized_ammonia_n(nh4_n, ph_nuevo, temp_c),
            "topado_por": ("operativo"
                           if (max_kg_turno and abs(kg - float(max_kg_turno)) < 0.5)
                           else ("nh3" if alk_max < float(alk_target) - 0.5 else None)),
        })
        alk, ph = alk_max, ph_nuevo
        if alk >= float(alk_target) - 0.5:
            break
    return {
        "turnos": turnos,
        "completo": alk >= float(alk_target) - 0.5,
        "kg_total": sum(t["kg"] for t in turnos),
        "alk_final": alk,
        "kg_teorico": bicarbonate_kg(volume_m3, alk_now, alk_target),
    }


def maintenance_kg_day(feed_kg_day, protein_pct, q_fresh_l_s, alk_source,
                       alk_target):
    """kg/dia de NaHCO3 para sostener la alcalinidad objetivo.

    Balance: consume la nitrificacion (ALK_PER_N por cada g de N), aporta el
    agua fresca, y la purga se lleva agua a la concentracion objetivo. El
    bicarbonato cubre la diferencia. Si da negativo el agua fresca alcanza
    sola — y entonces una alcalinidad baja medida no se explica por el
    biofiltro, que es justo lo que pasa hoy.
    """
    # Sin la alcalinidad de ENTRADA el balance no se puede escribir. Tratarla
    # como 0 da un numero plausible y equivocado —el aporte desaparece y la
    # dosis se triplica—, que es peor que no dar ninguno.
    if not q_fresh_l_s or alk_target is None or alk_source is None:
        return None
    n_kg = feed_nitrogen_kg_day(feed_kg_day, protein_pct) or 0.0
    consumo = n_kg * ALK_PER_N                                   # kg CaCO3/dia
    m3_dia = float(q_fresh_l_s) * 86.4                           # L/s -> m3/dia
    aporte = float(alk_source) * m3_dia / 1000.0                 # kg CaCO3/dia
    # La purga se lleva agua a la concentracion OBJETIVO: sostener 80 con un
    # ingreso de 60 significa pagar tambien lo que sale por el desague, y eso
    # suele pesar mas que la nitrificacion.
    purga = float(alk_target) * m3_dia / 1000.0                  # kg CaCO3/dia
    neto = consumo + purga - aporte
    return {
        "consumo_nitrificacion": consumo,
        "aporte_agua_fresca": aporte,
        "salida_por_purga": purga,
        "kg_caco3_dia": neto,
        "kg_nahco3_dia": (neto / NAHCO3_AS_CACO3) if neto > 0 else 0.0,
        "agua_fresca_alcanza": neto <= 0,
    }


def steady_state_alkalinity(alk_source, feed_kg_day, protein_pct, q_fresh_l_s,
                            dose_kg_day=0.0):
    """Alcalinidad de equilibrio de la laguna, sin medirla.

    En estado estacionario entra tanto como sale:

        q*alk_in + dosis  =  q*alk_laguna + consumo_nitrificacion

    de donde  alk_laguna = alk_in + (dosis - consumo) / q.

    El termino que se olvida facil es que la PURGA se lleva agua a la
    concentracion de la laguna, no a la de entrada. Sin eso uno compara el
    aporte (62 kg/dia) contra el consumo (21 kg/dia), concluye que sobra, y no
    entiende por que mide 37. Con el termino puesto, 37 es exactamente lo que
    corresponde.
    """
    if alk_source is None or not q_fresh_l_s:
        return None
    m3_dia = float(q_fresh_l_s) * 86.4
    n_kg = feed_nitrogen_kg_day(feed_kg_day, protein_pct) or 0.0
    consumo = n_kg * ALK_PER_N                              # kg CaCO3/dia
    dosis = float(dose_kg_day or 0.0) * NAHCO3_AS_CACO3     # kg CaCO3/dia
    return {
        "alk_equilibrio": float(alk_source) + (dosis - consumo) * 1000.0 / m3_dia,
        "consumo_kg_dia": consumo,
        "caida_mg_l": consumo * 1000.0 / m3_dia,
        "m3_dia": m3_dia,
    }


def salt_target(feed_cap, no2_fit, nitrite_alert):
    """Cloruro objetivo: el que alinea el techo de nitrito con el que le sigue.

    El criterio no es una concentracion de tabla, es de sistema: subir cloruro
    solo hasta que el nitrito deje de ser el que manda. Mas alla de eso la sal
    no compra nada porque otro techo pasa a limitar.

    Y si hoy manda la alcalinidad, la sal no compra nada TODAVIA: primero hay
    que levantar ese techo. Eso se devuelve como `util=False`.
    """
    if not feed_cap or not no2_fit or not no2_fit.get("b") or not nitrite_alert:
        return None
    techos = {t["key"]: t["feed"] for t in feed_cap["techos"]}
    if "nitrito" not in techos:
        return None
    otros = [v for k, v in techos.items() if k != "nitrito"]
    if not otros:
        return None
    objetivo = min(otros)                    # hasta alcanzar al siguiente techo
    util = feed_cap["manda"] == "nitrito"
    factor = objetivo / techos["nitrito"]
    if factor <= 1.0:
        return {"util": False, "manda": feed_cap["manda"], "factor": factor,
                "razon": "el nitrito no es el techo que manda"}
    # feed ∝ c_max ∝ (lim/a)^(1/b)  =>  lim sube con factor^b
    lim_nuevo = float(nitrite_alert) * (factor ** no2_fit["b"])
    return {
        "util": util,
        "manda": feed_cap["manda"],
        "factor": factor,
        "nitrito_lim_actual": float(nitrite_alert),
        "nitrito_lim_objetivo": lim_nuevo,
        "cloruro_objetivo": CL_NO2_RATIO * lim_nuevo,
        "ratio": CL_NO2_RATIO,
    }


def salt_plan(cl_target, cl_base, volume_m3, q_fresh_l_s):
    """kg de NaCl: la carga inicial y la diaria para sostenerla.

    Acá la sal es un costo corriente, no una carga: con la purga el cloruro se
    lava en pocos dias. La diaria suele pesar mas que la inicial.
    """
    if cl_target is None or not volume_m3:
        return None
    base = float(cl_base or 0.0)
    delta = max(0.0, float(cl_target) - base)
    inicial = delta * float(volume_m3) / 1000.0 / NACL_CL_FRACTION
    diaria = None
    if q_fresh_l_s:
        m3_dia = float(q_fresh_l_s) * 86.4                       # L/s -> m3/dia
        diaria = float(cl_target) * m3_dia / 1000.0 / NACL_CL_FRACTION
    tau_dias = (float(volume_m3) / (float(q_fresh_l_s) * 86.4)
                if q_fresh_l_s else None)
    return {"kg_inicial": inicial, "kg_dia": diaria, "tau_dias": tau_dias,
            "cl_target": float(cl_target), "cl_base": base}


def modeled_chloride(doses, volume_m3, q_fresh_l_s, cl_base, now):
    """Cloruro por balance de masa, cuando no hay reactivo para medirlo.

    Cada carga de sal sube el cloruro de golpe y despues se lava con la purga
    (exponencial, constante tau = V/q). Es un MODELO: sirve para dosificar,
    no para afirmar una concentracion. El test lo valida o lo corrige.
    """
    if not volume_m3 or not q_fresh_l_s:
        return None
    tau_dias = float(volume_m3) / (float(q_fresh_l_s) * 86.4)
    cl = float(cl_base or 0.0)
    for d in doses:
        if (d.product or "").lower() != "sal" or not d.kg:
            continue
        dias = (now - d.applied_at).total_seconds() / 86400.0
        if dias < 0:
            continue
        aporte = float(d.kg) * NACL_CL_FRACTION * 1000.0 / float(volume_m3)
        cl += aporte * math.exp(-dias / tau_dias)
    return {"cl_mg_l": cl, "tau_dias": tau_dias, "modelado": True}



# =============================================================================
# RADIACION SOLAR: DEL PRONOSTICO AL PLAN DE ACCION
# =============================================================================
# Verificado antes de usarlo, sobre 320 dias-unidad ya medidos:
#
#   r(radiacion, amplitud dia-noche) = +0,38 a +0,57 en las lagunas
#                                      +0,01 en Jaula  <- el control
#
# Jaula es profunda y grande: ahi no deberia haber senal de laguna, y no la
# hay. Ese cero es lo que descarta que la correlacion sea un artefacto de
# estacion o de sonda.
#
# Ajuste sobre los mismos datos:  amplitud (pts) = 2,57 + 0,398 x MJ/m2
#
# Dos advertencias que el texto tiene que respetar:
#
#   1. r ~ 0,5 explica un 25-30% de la varianza. Es un MODIFICADOR y un
#      filtro, no un predictor. La amplitud va de 4,6 a 10,5 pts entre
#      extremos y el gatillo de floracion esta en 15: la radiacion sola nunca
#      lo cruza, y el texto no debe sugerir que si.
#
#   2. Un dia soleado NO deja un amanecer peor al dia siguiente. Se probo
#      (r = +0,11, con el signo al reves) y es falso. Por eso el consejo habla
#      del pH de la tarde; el margen del amanecer sigue saliendo de la
#      medicion, no del pronostico.
# ---------------------------------------------------------------------------
AMP_PER_MJ = 0.398          # puntos de amplitud por MJ/m2 (n=342)
AMP_INTERCEPT = 2.573
SOLAR_CLEAR_MJ = 15.0       # sobre esto el dia pesa: tercil alto
SOLAR_DULL_MJ = 8.8         # bajo esto el dia no empuja: tercil bajo


def expected_amplitude(ghi_mj_m2):
    """Amplitud dia-noche esperable para esa radiacion, en puntos de saturacion."""
    if ghi_mj_m2 is None:
        return None
    return AMP_INTERCEPT + AMP_PER_MJ * float(ghi_mj_m2)


def solar_forecast_strip(rows, n_con_floracion=0, hoy=None):
    """Pronostico corto del SITIO, con los dias marcados.

    La radiacion es del sitio, no de la laguna: repetirla en cada tarjeta es
    ruido. Va una vez, arriba, y las tarjetas conservan solo lo que depende de
    la unidad (el margen del amanecer, que decide si la palanca de ventilacion
    se puede usar en ESA laguna).

    "peligro" pide las dos cosas: dia despejado Y floracion ya detectada en
    alguna laguna. Un dia de sol sin floracion se marca como "sol" y no como
    peligro -- puede iniciarla, pero todavia no hay nada que contener.
    """
    # Los nombres van en el codigo y no por locale: el servidor corre en una
    # sesion cuyo locale no controlamos, y %a devolvia "fri" y "sat".
    dias_es = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
    out = []
    for r in rows:
        if r.ghi_mj_m2 is None:
            continue
        mj = float(r.ghi_mj_m2)
        despejado = mj >= SOLAR_CLEAR_MJ
        out.append({
            "dia": r.day,
            "es_hoy": (hoy is not None and r.day == hoy),
            "label": ("HOY" if (hoy is not None and r.day == hoy)
                      else "%s %d" % (dias_es[r.day.weekday()], r.day.day)),
            "mj": mj,
            "nubes": (float(r.cloud_cover_pct) if r.cloud_cover_pct is not None else None),
            "amplitud": expected_amplitude(mj),
            "estado": ("peligro" if (despejado and n_con_floracion)
                       else ("sol" if despejado
                             else ("apagado" if mj <= SOLAR_DULL_MJ else "medio"))),
            "es_pronostico": bool(getattr(r, "is_forecast", True)),
        })
    return out


def solar_outlook(manana, dawn_margin=None):
    """Convierte el pronostico de manana en una nota de accion.

    `manana` es la fila de solar_daily (o None). `dawn_margin` son los puntos
    de margen que hoy tiene el amanecer: la palanca de bajar ventilacion se
    paga en oxigeno, asi que con el amanecer justo no se recomienda.

    Devuelve {nivel, texto, accion} o None. Tres casos y nada mas: nublado
    tranquiliza, soleado sugiere, y el intermedio no dice nada — una nota que
    aparece todos los dias deja de leerse.
    """
    if manana is None or manana.ghi_mj_m2 is None:
        return None
    mj = float(manana.ghi_mj_m2)
    amp = expected_amplitude(mj)
    nubes = float(manana.cloud_cover_pct) if manana.cloud_cover_pct is not None else None
    es_pron = bool(getattr(manana, "is_forecast", True))
    cola = " (pronostico)" if es_pron else ""

    if mj <= SOLAR_DULL_MJ:
        txt = "Manana {} MJ/m2{}{}: dia apagado, amplitud esperable ~{} pts.".format(
            _n(mj), (", %.0f%% de nubes" % nubes) if nubes is not None else "", cola,
            _n(amp))
        return {"nivel": "ok", "texto": txt,
                "accion": "Sin cambios en la ventilacion. El pH de la tarde no deberia empujar."}

    if mj >= SOLAR_CLEAR_MJ:
        txt = "Manana {} MJ/m2{}{}: dia despejado, amplitud esperable ~{} pts.".format(
            _n(mj), (", %.0f%% de nubes" % nubes) if nubes is not None else "", cola,
            _n(amp))
        if dawn_margin is not None and dawn_margin < 10:
            return {"nivel": "alerta", "texto": txt,
                    "accion": ("NO bajar ventilacion: el amanecer queda con {} pts de "
                               "margen y el aire tambien oxigena. Medir el pH a las "
                               "16:30.".format(_n(dawn_margin)))}
        return {"nivel": "alerta", "texto": txt,
                "accion": ("Bajar ventilacion entre las 09:00 y las 12:00 deja el CO2 "
                           "puesto, y ese CO2 es lo que amortigua el pH de la tarde. "
                           "Medir el pH a las 16:30.")}
    return None

# --- regimen de muestreo ----------------------------------------------------
# La frecuencia de la alcalinidad no es una constante: depende de si esta
# pasando algo. Y el regimen tiene que VOLVER SOLO a basal — si depende de que
# alguien lo apague, queda prendido para siempre.
REGIMES = {
    "basal":       {"alk_hours": 5 * 24.0, "label": "Basal",
                    "detalle": "con la rotación del biofiltro"},
    "choque":      {"alk_hours": 12.0, "label": "Choque",
                    "detalle": "antes de cada dosis y 2 h después"},
    "floracion":   {"alk_hours": 30.0, "label": "Floración",
                    "detalle": "diaria, junto al pH de la tarde"},
    "post_choque": {"alk_hours": 2 * 24.0 + 6, "label": "Post-choque",
                    "detalle": "cada 2 días, 2 semanas"},
}
SHOCK_QUIET_DAYS = 2        # sin cargas de choque -> pasa a post-choque
POST_SHOCK_DAYS = 14        # y de ahi vuelve a basal


def regime_for(last_shock_dose_at, photo_active, now):
    """Regimen que corresponde HOY, sin depender de que nadie lo apague."""
    if last_shock_dose_at is not None:
        dias = (now - last_shock_dose_at).total_seconds() / 86400.0
        if dias <= SHOCK_QUIET_DAYS:
            return "choque"
        if dias <= SHOCK_QUIET_DAYS + POST_SHOCK_DAYS:
            return "post_choque"
    if photo_active:
        return "floracion"
    return "basal"
