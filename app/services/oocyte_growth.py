"""
Modelo de crecimiento intra-ciclo del diámetro de la ova → predice "días hasta
el umbral de cosecha (2,8 mm)" por hembra.

Idea:
  - El diámetro crece de forma **~lineal** durante la maduración (no hay
    saturación biológica en 2,8: ese "plateau" observado es **censura** — se
    cosecha al cruzar el umbral, así que la ova sale del dato, no deja de
    crecer). La ova de la especie llega a ~2,7-3,2 mm (hasta 3,8 aislado).
  - El crecimiento se modela en **tiempo operacional**, acoplado a la
    estacionalidad del modelo Markov de ovulación (ρ): en verano crece más
    rápido. Δdiámetro = g · τ,  con τ = ∫ ρ(u) du  y  g = mm por día operacional.

La tasa g se estima combinando:
  - seed histórico: app/data/crecimiento_ovas_seed.csv (dic-2025..mar-2026),
  - datos vivos: pares consecutivos de fish_samplings.diameter (mismo ciclo),
y se re-estima en cada refresco (el modelo se alimenta de las actualizaciones).
"""
from __future__ import annotations

import csv
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.fish import Fish
from app.models.fish_samplings import FishSampling
from app.services import ovulation_cycle as _ovc

THRESHOLD_MM = 2.8
_DIAM_MIN, _DIAM_MAX = 1.5, 5.0
_GROWTH_MAX_START = 2.8      # solo fase de crecimiento (evita censurados ≥umbral)
_MIN_GAP = 10               # span mínimo (días) para estimar crecimiento
_CYCLE_GAP = 200            # gap máx (días) para considerar el mismo ciclo

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_SEED_CSV = _DATA_DIR / "crecimiento_ovas_seed.csv"
_CACHE = _DATA_DIR / "oocyte_growth_cache.json"


# --------------------------------------------------------------------------- #
# Estacionalidad (ρ del modelo Markov)
# --------------------------------------------------------------------------- #
def _rho_params():
    """(a, phi) del modelo de ovulación; (0,0) => sin estacionalidad (ρ≡1)."""
    p = _ovc.get_params()
    if p and "a" in p and "phi" in p:
        return float(p["a"]), float(p["phi"])
    return 0.0, 0.0


def _rho_daily(a, phi):
    """ρ normalizada (media anual = 1) por día del año 1..365."""
    doy = np.arange(1, 366)
    rho = np.exp(a * np.cos(2 * np.pi * (doy - phi) / 365.25))
    return rho / rho.mean()


def _op_time(d0: date, d1: date, a, phi) -> float:
    """tiempo operacional (Σρ) entre dos fechas."""
    if d1 <= d0:
        return 0.0
    rho = _rho_daily(a, phi)
    t = 0.0
    d = d0
    while d < d1:
        t += rho[d.timetuple().tm_yday - 1]
        d += timedelta(days=1)
    return t


# --------------------------------------------------------------------------- #
# Fuentes de datos → pares (Δdiam, τ) en fase de crecimiento
# --------------------------------------------------------------------------- #
def _parse_diam(s):
    s = (s or "").strip().replace(",", ".")
    try:
        v = float(s)
        return v if _DIAM_MIN <= v <= _DIAM_MAX else None
    except ValueError:
        return None


def _seed_series():
    """Trayectorias (lista de (date, diam)) desde la planilla histórica."""
    if not _SEED_CSV.exists():
        return []
    rows = list(csv.reader(_SEED_CSV.open(encoding="latin-1"), delimiter=";"))
    if not rows:
        return []
    date_cols = [i for i in range(2, len(rows[0]), 2)]
    dates = []
    for i in date_cols:
        try:
            dates.append(datetime.strptime(rows[0][i].strip(), "%d-%m-%y").date())
        except ValueError:
            dates.append(None)
    out = []
    for r in rows[1:]:
        if len(r) <= max(date_cols) or not r[0].strip():
            continue
        traj = []
        for k, dc in enumerate(date_cols):
            d = _parse_diam(r[dc]) if dc < len(r) else None
            if d is not None and dates[k] is not None:
                traj.append((dates[k], d))
        if len(traj) >= 2:
            out.append(traj)
    return out


def _db_series(db: Session):
    """Trayectorias de diámetro por hembra desde fish_samplings (mismo ciclo)."""
    t_expr = func.coalesce(FishSampling.registry_time, FishSampling.harvest_date)
    rows = (
        db.query(FishSampling.fish_id, FishSampling.diameter, t_expr)
        .join(Fish, Fish.id == FishSampling.fish_id)
        .filter(
            func.upper(Fish.sex) == "F",
            FishSampling.diameter.isnot(None),
            FishSampling.diameter >= _DIAM_MIN,
            FishSampling.diameter <= _DIAM_MAX,
            t_expr.isnot(None),
        )
        .order_by(FishSampling.fish_id, t_expr)
        .all()
    )
    by = {}
    for fid, dm, t in rows:
        d = t.date() if isinstance(t, datetime) else t
        by.setdefault(fid, []).append((d, float(dm)))
    return list(by.values())


def _growth_spans(series, a, phi):
    """(Δdiam, τ) por hembra usando **primera→última medición del mismo ciclo**.
    Es robusto a la resolución de 0,1 mm y al patrón "plano-y-salto" (valores
    arrastrados entre días de medición): agrega en vez de mirar par a par.
    No filtra por signo (evita sesgo al alza)."""
    dd, tau = [], []
    for traj in series:
        traj = sorted(traj)
        run = [traj[0]]
        for prev, cur in zip(traj, traj[1:]):
            if (cur[0] - prev[0]).days <= _CYCLE_GAP:
                run.append(cur)
            else:
                _emit_span(run, dd, tau, a, phi)
                run = [cur]
        _emit_span(run, dd, tau, a, phi)
    return np.array(dd), np.array(tau)


def _emit_span(run, dd, tau, a, phi):
    if len(run) < 2:
        return
    (d0, x0), (d1, x1) = run[0], run[-1]
    if x0 < _GROWTH_MAX_START and (d1 - d0).days >= _MIN_GAP:
        t = _op_time(d0, d1, a, phi)
        if t > 0:
            dd.append(x1 - x0)
            tau.append(t)


# --------------------------------------------------------------------------- #
# Estimación de la tasa g (mm por día operacional)
# --------------------------------------------------------------------------- #
def estimate(db: Session) -> dict:
    a, phi = _rho_params()
    seed = _seed_series()
    live = _db_series(db)
    dd_s, tau_s = _growth_spans(seed, a, phi)
    dd_l, tau_l = _growth_spans(live, a, phi)
    dd = np.concatenate([dd_s, dd_l]) if len(dd_l) else dd_s
    tau = np.concatenate([tau_s, tau_l]) if len(dd_l) else tau_s

    # Estimador = crecimiento total / tiempo operacional total (primera→última
    # por hembra). Agregar es lo correcto dada la resolución de 0,1 mm.
    def _rate(d, t):
        return float(d.sum() / t.sum()) if t.sum() > 0 else 0.0
    g_seed = _rate(dd_s, tau_s)
    g_live = _rate(dd_l, tau_l) if len(dd_l) else None
    # Combinado seed + vivos: ahora los vivos aportan crecimiento real (spans),
    # así el modelo se alimenta de las actualizaciones.
    g_op = _rate(dd, tau)

    # diámetros de cosecha (para caracterizar el rango real, no el censurado)
    harvest = _harvest_diams()

    result = {
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "generated_at_human": datetime.now(timezone.utc).astimezone().strftime("%d/%m/%Y %H:%M"),
        "g_op": g_op,                  # mm por día operacional (seed + vivos)
        "g_seed": g_seed, "g_live": g_live,
        "n_seed": int(len(dd_s)),
        "n_live": int(len(dd_l)),
        "a": a, "phi": phi,
        "threshold_mm": THRESHOLD_MM,
        "harvest_diam": harvest,       # rango real de cosecha (no censurado)
    }
    return result


def _harvest_diams():
    """Distribución del diámetro al cosechar (de la planilla) — define el rango
    real de la especie, no el 'plateau' censurado."""
    if not _SEED_CSV.exists():
        return None
    rows = list(csv.reader(_SEED_CSV.open(encoding="latin-1"), delimiter=";"))
    date_cols = [i for i in range(2, len(rows[0]), 2)]
    vals = []
    for r in rows[1:]:
        last = None
        for dc in date_cols:
            d = _parse_diam(r[dc]) if dc < len(r) else None
            if d is not None:
                last = d
            est = (r[dc + 1] if dc + 1 < len(r) else "").strip().lower()
            if "cosech" in est and last is not None:
                vals.append(last)
    if not vals:
        return None
    v = np.array(vals)
    return {"n": int(len(v)), "mean": float(v.mean()), "median": float(np.median(v)),
            "p10": float(np.percentile(v, 10)), "p90": float(np.percentile(v, 90))}


def estimate_and_cache(db: Session) -> dict:
    r = estimate(db)
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _CACHE.write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
    return r


def load_params() -> dict | None:
    if _CACHE.exists():
        try:
            return json.loads(_CACHE.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


# --------------------------------------------------------------------------- #
# Predicción: días hasta el umbral de cosecha
# --------------------------------------------------------------------------- #
def days_to_threshold(current_diam: float, from_date: date, params: dict,
                      threshold: float = THRESHOLD_MM, max_days: int = 500):
    """Días calendario para que el diámetro alcance `threshold`, integrando ρ.
    Devuelve 0 si ya está en/sobre el umbral, None si no converge."""
    if current_diam is None:
        return None
    if current_diam >= threshold:
        return 0
    g = params.get("g_op", 0.0)
    if g <= 0:
        return None
    need = threshold - current_diam
    a, phi = params.get("a", 0.0), params.get("phi", 0.0)
    rho = _rho_daily(a, phi)
    acc = 0.0
    d = from_date
    for k in range(1, max_days + 1):
        acc += g * rho[d.timetuple().tm_yday - 1]
        if acc >= need:
            return k
        d += timedelta(days=1)
    return None
