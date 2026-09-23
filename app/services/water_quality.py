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
TAN_PER_FEED_N = 0.092          # kg TAN-N por kg de alimento x fraccion proteica
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
