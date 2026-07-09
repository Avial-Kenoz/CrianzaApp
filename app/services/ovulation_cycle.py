"""
Estimación del ciclo ovárico de esturión hembra a partir de los muestreos
gonadales históricos (fish_samplings.development_state).

Modelo: cadena de Markov en tiempo continuo con datos de panel (censura por
intervalo). Topología cíclica progresiva que cierra por el estado de reposo:

    0(basal/reposo) → 1 → 2 → 3 → 4 → rev(reabsorción) → 0 → 1 …

La duración media de cada etapa es 1/(tasa de salida); el "ciclo 1→1" es el
tiempo medio de recurrencia al estado 1. Se añade una modulación estacional
proporcional Q(t)=ρ(t)·Q₀ (ρ sinusoide anual → "tiempo operacional"), que
captura que el desarrollo corre más rápido en verano.

Todo el ajuste es vectorizado vía autovalores de Q₀ (sin expm en el loop).
El resultado se cachea en app/data/ y las figuras se escriben en app/static/.
"""
from __future__ import annotations

import functools
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from scipy.linalg import expm
from scipy.optimize import minimize
from sqlalchemy import func
from sqlalchemy.orm import Session

import matplotlib
matplotlib.use("Agg")
from matplotlib.figure import Figure  # OO API (thread-safe, sin pyplot global)

from app.models.fish import Fish
from app.models.fish_samplings import FishSampling

# --------------------------------------------------------------------------- #
# Configuración del modelo
# --------------------------------------------------------------------------- #
STATE_NAMES = ["basal / reposo", "1", "2", "3", "4", "rev (reabsorción)"]
NS = 6
CYCLE_STATES = [1, 2, 3, 4, 5]           # el "ciclo 1→1" (rev incluido, basal aparte)
EDGES = [(0, 1), (1, 2), (2, 3), (3, 4), (4, 5), (5, 0)]  # modelo C

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_CACHE_PATH = _DATA_DIR / "ovulation_cycle_cache.json"
_FIG_STAGES = "ciclo_ovarico_etapas.png"
_FIG_SEASON = "ciclo_ovarico_estacional.png"

_MONTHS_ES = ["Ene", "Feb", "Mar", "Abr", "May", "Jun",
              "Jul", "Ago", "Sep", "Oct", "Nov", "Dic"]


def _canon(raw: str | None):
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if s in ("1", "2", "3", "4"):
        return int(s)
    if s in ("reverse", "r", "revertida", "rev"):
        return 5
    if s in ("0", "immature", "imm", "l"):
        return 0
    return None


# --------------------------------------------------------------------------- #
# Datos
# --------------------------------------------------------------------------- #
def _load_pairs(db: Session):
    """Devuelve arrays (I, J, T0_ordinal, DT_dias) de transiciones consecutivas
    por pez, para todas las hembras (vivas y muertas). Colapsa muestreos del
    mismo día quedándose con el último estado registrado."""
    t_expr = func.coalesce(FishSampling.registry_time, FishSampling.harvest_date)
    rows = (
        db.query(FishSampling.fish_id, FishSampling.development_state, t_expr)
        .join(Fish, Fish.id == FishSampling.fish_id)
        .filter(
            func.upper(Fish.sex) == "F",
            FishSampling.development_state.isnot(None),
            func.btrim(FishSampling.development_state) != "",
            t_expr.isnot(None),
        )
        .order_by(FishSampling.fish_id, t_expr)
        .all()
    )
    series: dict[int, dict[int, int]] = {}
    for fish_id, dev, t in rows:
        st = _canon(dev)
        if st is None:
            continue
        d = t.date() if isinstance(t, datetime) else t
        series.setdefault(fish_id, {})[d.toordinal()] = st

    I, J, T0, DT, FISH = [], [], [], [], []
    for fish_id, obs in series.items():
        seq = sorted(obs.items())
        for (o0, s0), (o1, s1) in zip(seq, seq[1:]):
            dt = o1 - o0
            if dt > 0:
                I.append(s0); J.append(s1); T0.append(o0); DT.append(dt)
                FISH.append(fish_id)
    return (np.array(I), np.array(J), np.array(T0, dtype=np.int64),
            np.array(DT, dtype=float), np.array(FISH), len(series))


# --------------------------------------------------------------------------- #
# Núcleo del modelo
# --------------------------------------------------------------------------- #
def _build_Q(rates):
    Q = np.zeros((NS, NS))
    for (i, j), r in zip(EDGES, rates):
        Q[i, j] = r
    for i in range(NS):
        Q[i, i] = -Q[i].sum()
    return Q


def _prob_obs(Q, tau, I, J):
    """P_ij(tau) por par, vectorizado con autovalores de Q."""
    lam, V = np.linalg.eig(Q)
    Vinv = np.linalg.inv(V)
    Erow = np.exp(np.outer(tau, lam))                       # (n,6) complejo
    val = np.einsum("nk,nk,nk->n", V[I, :], Erow, Vinv[:, J].T).real
    return np.clip(val, 1e-300, None)


def _make_doy(T0, DT):
    base = int(T0.min())
    span = int((T0 + DT).max()) - base
    doy = np.array(
        [date.fromordinal(base + k).timetuple().tm_yday for k in range(span + 2)],
        dtype=float,
    )
    return base, doy


def _tau_seasonal(a, phi, T0, DT, base, doy):
    rho = np.exp(a * np.cos(2 * np.pi * (doy - phi) / 365.25))
    rho /= rho.mean()                                        # media anual = 1
    cum = np.concatenate([[0.0], np.cumsum(rho)])
    return cum[(T0 + DT).astype(int) - base] - cum[T0.astype(int) - base]


def _fit(I, J, T0, DT, seasonal, base=None, doy=None, init=None):
    def nll(theta):
        Q = _build_Q(np.exp(theta[:6]))
        tau = (_tau_seasonal(theta[6], theta[7], T0, DT, base, doy)
               if seasonal else DT)
        return -np.log(_prob_obs(Q, tau, I, J)).sum()

    if init is None:
        init = np.log(np.full(6, 1 / 200.0))
        if seasonal:
            init = np.r_[init, [0.3, 20.0]]
    res = minimize(nll, init, method="Nelder-Mead",
                   options={"maxiter": 9000, "fatol": 1e-6, "xatol": 1e-6})
    return res


def _sojourns(rates):
    Q = _build_Q(rates)
    return {s: (1.0 / -Q[s, s] if Q[s, s] < 0 else np.inf) for s in range(NS)}


def _stationary(rates):
    Q = _build_Q(rates)
    A = np.vstack([Q.T, np.ones(NS)])
    b = np.zeros(NS + 1); b[-1] = 1.0
    pi, *_ = np.linalg.lstsq(A, b, rcond=None)
    pi = np.clip(pi, 0, None)
    return pi / pi.sum()


def _cycle_days(rates):
    """Tiempo medio de recurrencia al estado 1 (= ciclo 1→1)."""
    pi = _stationary(rates)
    Q = _build_Q(rates)
    return 1.0 / (pi[1] * -Q[1, 1])


D_PER_MONTH = 30.44


# --------------------------------------------------------------------------- #
# Cómputo completo + figuras + caché
# --------------------------------------------------------------------------- #
def compute(db: Session, n_boot: int = 100) -> dict:
    I, J, T0, DT, FISH, n_females = _load_pairs(db)
    n_pairs = len(I)
    base, doy = _make_doy(T0, DT)

    # ajuste base (no estacional) y estacional
    r0 = _fit(I, J, T0, DT, seasonal=False)
    rates0 = np.exp(r0.x)
    ll0 = -r0.fun
    rs = _fit(I, J, T0, DT, seasonal=True, base=base, doy=doy)
    rates_s = np.exp(rs.x[:6]); a = float(rs.x[6]); phi = float(rs.x[7])
    lls = -rs.fun
    lr = 2 * (lls - ll0)

    soj = _sojourns(rates0)
    pi = _stationary(rates0)
    cycle = _cycle_days(rates0)

    # bootstrap por pez para IC (solo modelo base — es el titular)
    fish_ids = np.unique(FISH)
    idx_by_fish = {f: np.where(FISH == f)[0] for f in fish_ids}
    rng = np.random.default_rng(7)
    bc, bs = [], {s: [] for s in range(NS)}
    for _ in range(n_boot):
        samp = rng.choice(fish_ids, size=len(fish_ids), replace=True)
        idx = np.concatenate([idx_by_fish[f] for f in samp])
        try:
            rb = np.exp(_fit(I[idx], J[idx], T0[idx], DT[idx],
                             seasonal=False, init=r0.x).x)
            bc.append(_cycle_days(rb) / D_PER_MONTH)
            sj = _sojourns(rb)
            for s in range(NS):
                bs[s].append(sj[s] / D_PER_MONTH)
        except Exception:
            pass
    bc = np.array(bc)

    def ci(arr):
        arr = np.array(arr)
        return float(np.percentile(arr, 2.5)), float(np.percentile(arr, 97.5))

    cyc_lo, cyc_hi = ci(bc) if len(bc) else (float("nan"), float("nan"))

    # curva estacional
    grid = np.arange(1, 366)
    rho = np.exp(a * np.cos(2 * np.pi * (grid - phi) / 365.25)); rho /= rho.mean()
    cyc_base_s = _cycle_days(rates_s)
    month_rho, month_cycle = [], []
    for m in range(12):
        mid = 15 + 30 * m
        rm = float(np.exp(a * np.cos(2 * np.pi * (mid - phi) / 365.25)) / rho_mean_of(a, phi))
        month_rho.append(rm)
        month_cycle.append(cyc_base_s / rm / D_PER_MONTH)
    rmax, rmin = float(rho.max()), float(rho.min())
    peak_month = _MONTHS_ES[int((grid[rho.argmax()] - 1) // 30) % 12]
    trough_month = _MONTHS_ES[int((grid[rho.argmin()] - 1) // 30) % 12]

    dense = [_MONTHS_ES[m] for m in range(12) if month_rho[m] >= 1.0]
    sparse = [_MONTHS_ES[m] for m in range(12) if month_rho[m] < 1.0]

    stages = []
    for s in range(NS):
        lo, hi = ci(bs[s]) if bs[s] else (float("nan"), float("nan"))
        stages.append({
            "name": STATE_NAMES[s],
            "months": soj[s] / D_PER_MONTH,
            "lo": lo, "hi": hi,
            "pct_time": 100 * float(pi[s]),
            "cycle_part": s in CYCLE_STATES,
        })

    generated_at = datetime.now(timezone.utc).astimezone()
    stamp = generated_at.strftime("%Y%m%d%H%M%S")

    _draw_stages(stages, cycle / D_PER_MONTH, cyc_lo, cyc_hi, pi)
    _draw_seasonal(grid, rho, month_cycle, a)

    result = {
        "generated_at": generated_at.isoformat(),
        "generated_at_human": generated_at.strftime("%d/%m/%Y %H:%M"),
        "n_females": int(n_females),
        "n_pairs": int(n_pairs),
        "n_boot": int(len(bc)),
        "cycle_months": cycle / D_PER_MONTH,
        "cycle_lo": cyc_lo, "cycle_hi": cyc_hi,
        "stages": stages,
        "seasonal": {
            "amplitude": a,
            "fast_ratio": rmax / rmin,
            "peak_month": peak_month,
            "trough_month": trough_month,
            "cycle_fast_m": cyc_base_s / rmax / D_PER_MONTH,
            "cycle_slow_m": cyc_base_s / rmin / D_PER_MONTH,
            "lr_stat": float(lr),
            "significant": bool(lr > 9.2),    # χ²(2) 99%
            "month_rho": month_rho,
            "month_cycle": month_cycle,
        },
        "sampling": {"dense": dense, "sparse": sparse},
        "params": {"rates": [float(x) for x in rates_s], "a": a, "phi": phi},
        "fig_stages": f"/static/{_FIG_STAGES}?v={stamp}",
        "fig_seasonal": f"/static/{_FIG_SEASON}?v={stamp}",
    }
    return result


def rho_mean_of(a, phi):
    grid = np.arange(1, 366)
    return float(np.exp(a * np.cos(2 * np.pi * (grid - phi) / 365.25)).mean())


def compute_and_cache(db: Session, n_boot: int = 100) -> dict:
    result = compute(db, n_boot=n_boot)
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _CACHE_PATH.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    return result


def _json_default(o):
    """Convierte escalares numpy sueltos a tipos nativos."""
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.floating):
        return float(o)
    if isinstance(o, np.bool_):
        return bool(o)
    raise TypeError(f"No serializable: {type(o)}")


def load_cached() -> dict | None:
    if _CACHE_PATH.exists():
        try:
            return json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return None
    return None


def load_or_compute(db: Session) -> dict:
    return load_cached() or compute_and_cache(db)


# --------------------------------------------------------------------------- #
# Figuras (Agg, sin estado global)
# --------------------------------------------------------------------------- #
def _draw_stages(stages, cycle_m, cyc_lo, cyc_hi, pi):
    _STATIC_DIR.mkdir(parents=True, exist_ok=True)
    fig = Figure(figsize=(11, 4.4)); ax = fig.subplots(1, 2)
    x = np.arange(NS)
    med = [s["months"] for s in stages]
    lo = [max(s["months"] - s["lo"], 0) for s in stages]
    hi = [max(s["hi"] - s["months"], 0) for s in stages]
    ax[0].bar(x, med, yerr=[lo, hi], capsize=4, color="#2b8cbe")
    ax[0].set_xticks(x); ax[0].set_xticklabels([s["name"] for s in stages],
                                               rotation=30, ha="right", fontsize=8)
    ax[0].set_ylabel("meses")
    ax[0].set_title(f"Duración por etapa (IC95%)\nCiclo 1→1 = {cycle_m:.1f} m "
                    f"[{cyc_lo:.1f}–{cyc_hi:.1f}]")
    ax[1].bar(x, 100 * pi, color="#31a354")
    ax[1].set_xticks(x); ax[1].set_xticklabels([s["name"] for s in stages],
                                               rotation=30, ha="right", fontsize=8)
    ax[1].set_ylabel("% del tiempo")
    ax[1].set_title("Distribución estacionaria\n(hembra adulta ciclando)")
    for i, v in enumerate(100 * pi):
        ax[1].text(i, v + 0.5, f"{v:.0f}%", ha="center", fontsize=8)
    fig.tight_layout()
    fig.savefig(_STATIC_DIR / _FIG_STAGES, dpi=110)


def _draw_seasonal(grid, rho, month_cycle, a):
    _STATIC_DIR.mkdir(parents=True, exist_ok=True)
    fig = Figure(figsize=(11, 4.4)); ax = fig.subplots(1, 2)
    ax[0].plot(grid, rho, color="#e6550d")
    ax[0].axhline(1, ls=":", color="grey")
    ax[0].set_xticks([15 + 30 * i for i in range(12)])
    ax[0].set_xticklabels(_MONTHS_ES, fontsize=8)
    ax[0].set_ylabel("velocidad relativa (ρ)")
    ax[0].set_title(f"Modulación estacional (a={a:.2f})\nverano austral = más rápido")
    ax[1].bar(range(12), month_cycle, color="#3182bd")
    ax[1].set_xticks(range(12)); ax[1].set_xticklabels(_MONTHS_ES, fontsize=8)
    ax[1].set_ylabel("ciclo equiv. (meses)")
    ax[1].set_title("Ciclo si el año entero tuviera\nla velocidad de ese mes")
    fig.tight_layout()
    fig.savefig(_STATIC_DIR / _FIG_SEASON, dpi=110)


# --------------------------------------------------------------------------- #
# Predicción por hembra (para el detalle de estanque)
# --------------------------------------------------------------------------- #
# Dado (estado observado, fecha), predice la fecha de entrada a etapa 4 (mediana)
# y una fecha de próximo muestreo (cuando P(ya llegó a E4) ≈ overshoot, "justo
# antes de que abra la ventana"). Reusa los parámetros cacheados (rates, a, phi):
# no re-ajusta el modelo.

def get_params(db: Session | None = None) -> dict | None:
    """Parámetros del ajuste (rates, a, phi) desde la caché; los calcula si falta
    y se pasa una sesión."""
    cached = load_cached()
    if cached and "params" in cached:
        return cached["params"]
    if db is not None:
        return compute_and_cache(db).get("params")
    return None


@functools.lru_cache(maxsize=8)
def _e4_quantiles(rates_key: tuple, overshoot: float):
    """Tiempo operacional (días) para llegar a etapa 4 desde cada estado 0..3,
    en los percentiles `overshoot` (próx. muestreo) y 0.50 (fecha probable).
    Depende solo de las tasas → se cachea."""
    Q = _build_Q(np.array(rates_key))
    Q[4, :] = 0.0                      # estado 4 absorbente → primera llegada
    taus = np.linspace(0.5, 2600, 1700)
    out = {}
    for s0 in (0, 1, 2, 3):
        cdf = np.array([expm(Q * t)[s0, 4] for t in taus])
        out[s0] = {
            "next": float(np.interp(overshoot, cdf, taus)),
            "e4": float(np.interp(0.50, cdf, taus)),
        }
    return out


def _date_from_optime(start_date: date, tau_target: float, a: float, phi: float,
                      max_days: int = 1600) -> date:
    """Fecha en que el tiempo operacional acumulado desde start_date alcanza
    tau_target (integra la modulación estacional día a día)."""
    g = np.arange(1, 366)
    rho_mean = float(np.exp(a * np.cos(2 * np.pi * (g - phi) / 365.25)).mean())
    acc, d = 0.0, start_date
    for _ in range(max_days):
        doy = d.timetuple().tm_yday
        acc += float(np.exp(a * np.cos(2 * np.pi * (doy - phi) / 365.25)) / rho_mean)
        if acc >= tau_target:
            return d
        d = d + timedelta(days=1)
    return d


def predict_for(state_int: int, last_date: date, params: dict,
                overshoot: float = 0.10):
    """(fecha_E4, fecha_prox_muestreo) para una hembra en `state_int` (0..3) el
    `last_date`. Devuelve (None, None) si no aplica."""
    if state_int not in (0, 1, 2, 3):
        return (None, None)
    q = _e4_quantiles(tuple(params["rates"]), overshoot)[state_int]
    a, phi = params["a"], params["phi"]
    return (_date_from_optime(last_date, q["e4"], a, phi),
            _date_from_optime(last_date, q["next"], a, phi))


def fmt_daymon(d: date | None) -> str | None:
    """Fecha compacta sin año, p. ej. '20-sep'."""
    if d is None:
        return None
    return f"{d.day:02d}-{_MONTHS_ES[d.month - 1].lower()}"
