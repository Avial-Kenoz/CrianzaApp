"""Motor de molienda y ensilaje (lógica pura, sin acceso a BD).

Registro PC 03.2. La unidad de captura es la **carga** (cada vez que se muele
mortalidad y se descarga al tambor), no el día: un día puede tener varias cargas
y los kg se suman. La fila diaria del formato en papel es un derivado que se
calcula acá (`build_daily_summary`), no una tabla.

Dos criterios que este módulo sostiene con rigor, porque es fácil implementarlos
al revés:

1. **El criterio de aceptación es el pH, no la dosis.** El operador digita los
   litros de ácido que efectivamente usó; el rango 0,10–0,25 lts/kg es solo un
   chequeo de verosimilitud para cazar tipeos y NUNCA invalida una carga. Si se
   alcanzó pH 3,8 con 0,08 lts/kg, la carga está conforme y el aviso es ruido.
   Presentarlo como error llevaría al operador a "ajustar" el número digitado
   para que calce, corrompiendo el dato que alimenta el inventario de ácido.

2. **Ausencia de cargas ≠ día sin mortalidad.** Un día sin mortalidad se registra
   explícitamente (`had_mortality=False`); un día sin ninguna fila es un registro
   FALTANTE. El formato en papel distingue la X de la celda en blanco, y esa
   diferencia es justamente lo que revisa un auditor.

Las funciones son puras: reciben los umbrales como argumento (dict con la forma
de DEFAULT_THRESHOLDS). El router carga los umbrales de la BD y los inyecta; los
defaults de este módulo replican el seed de la migración para poder testear y
correr sin BD.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Optional

# --- Umbrales por defecto (replican el seed de silage_thresholds) ------------
# Los valores de dosis y de cierre salen del procedimiento escrito del PC 03.2:
# "1 litro de ácido por 4 kg de mortalidad molida" (= 0,25 lts/kg) y "cuando el
# tambor se encuentre en 4/5 partes, cerrarlo y sellarlo" (= 0,80).
DEFAULT_THRESHOLDS: dict[str, float] = {
    "ph_limit": 4.0,
    "drum_capacity_kg": 200.0,
    "acid_low_stock_lts": 50.0,
    "acid_lts_per_kg_nominal": 0.25,
    # El rango bracketea el nominal en vez de terminar en él: si el techo fuera
    # 0,25, toda carga hecha exactamente según el procedimiento quedaría al borde
    # del aviso.
    "acid_lts_per_kg_min": 0.20,
    "acid_lts_per_kg_max": 0.30,
    "drum_close_fill_ratio": 0.80,
}

# Rangos físicamente plausibles (sanidad de ingreso, no reglas de negocio).
PLAUSIBLE_PH = (0.0, 14.0)
PLAUSIBLE_KG = (0.0, 5000.0)      # una carga de moledora no llega a una tonelada
PLAUSIBLE_ACID_LTS = (0.0, 1000.0)

ZERO = Decimal("0")


def _dec(value: Any) -> Optional[Decimal]:
    """Convierte a Decimal tolerando None, coma decimal y basura. None si no se
    puede: los campos son opcionales y el motor no debe reventar por un typo."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value).strip().replace(",", "."))
    except (InvalidOperation, ValueError, AttributeError):
        return None


def _threshold(thresholds: Optional[dict], key: str) -> Decimal:
    """Umbral como Decimal, cayendo al default si falta o viene inválido."""
    source = thresholds or {}
    value = _dec(source.get(key)) if key in source else None
    if value is None:
        value = _dec(DEFAULT_THRESHOLDS[key])
    return value


# ---------------------------------------------------------------------------
# Vista de una carga (desacopla el motor del ORM)
# ---------------------------------------------------------------------------
@dataclass
class EventView:
    """Los campos de una carga que el motor necesita.

    Se construye igual desde una fila ORM, un dict del payload de la PWA o un
    caso de prueba escrito a mano, así que el motor se puede ejercitar sin BD.
    """

    log_date: Optional[date] = None
    had_mortality: bool = True
    mortality_kg: Optional[Decimal] = None
    acid_used: bool = False
    acid_lts: Optional[Decimal] = None
    additional_acid: bool = False
    ph_value: Optional[Decimal] = None
    drum_id: Optional[int] = None
    drum_number: Optional[int] = None
    drum_closed: bool = False
    voided_at: Any = None
    received_at: Any = None
    folio: Optional[int] = None

    @property
    def is_voided(self) -> bool:
        return self.voided_at is not None

    @property
    def is_received(self) -> bool:
        return self.received_at is not None

    @classmethod
    def of(cls, source: Any) -> "EventView":
        """Adapta una fila ORM, un dict o un objeto cualquiera con estos
        atributos."""
        if isinstance(source, EventView):
            return source
        get = source.get if isinstance(source, dict) else (lambda k, d=None: getattr(source, k, d))
        return cls(
            log_date=get("log_date"),
            had_mortality=bool(get("had_mortality", True)),
            mortality_kg=_dec(get("mortality_kg")),
            acid_used=bool(get("acid_used", False)),
            acid_lts=_dec(get("acid_lts")),
            additional_acid=bool(get("additional_acid", False)),
            ph_value=_dec(get("ph_value")),
            drum_id=get("drum_id"),
            drum_number=get("drum_number"),
            drum_closed=bool(get("drum_closed", False)),
            voided_at=get("voided_at"),
            received_at=get("received_at"),
            folio=get("folio"),
        )


# ---------------------------------------------------------------------------
# Evaluación de una carga
# ---------------------------------------------------------------------------
@dataclass
class EventEvaluation:
    """Resultado de evaluar una carga.

    `errors` impide registrarla (incoherencias duras). `warnings` NO: son avisos
    que se muestran sin bloquear —incluido el pH sobre el límite, que marca la
    carga `no_conforme` pero se acepta para poder reportar el problema real.
    """

    conformity: str                      # conforme | no_conforme | sin_mortalidad
    ph_below_limit: Optional[bool] = None
    dose_lts_per_kg: Optional[Decimal] = None
    dose_in_range: Optional[bool] = None
    drum_status: Optional[str] = None    # ok | near_capacity | over_capacity
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return not self.errors


def evaluate_event(event: Any, thresholds: Optional[dict] = None,
                   drum_total_kg: Any = None) -> EventEvaluation:
    """Evalúa una carga: conformidad por pH, verosimilitud de la dosis,
    coherencia de los campos y estado del tambor.

    `drum_total_kg` son los kg que el tambor YA acumulaba antes de esta carga
    (opcional): permite avisar que se está por llenar en el momento de descargar,
    que es cuando el operador puede hacer algo al respecto.
    """
    ev = EventView.of(event)
    ph_limit = _threshold(thresholds, "ph_limit")
    result = EventEvaluation(conformity="conforme")

    # --- Día sin mortalidad: marca explícita, no una carga -------------------
    if not ev.had_mortality:
        result.conformity = "sin_mortalidad"
        if ev.mortality_kg is not None and ev.mortality_kg > ZERO:
            result.errors.append("Un registro sin mortalidad no puede llevar kilos")
        if ev.drum_id is not None:
            result.errors.append("Un registro sin mortalidad no puede ir a un tambor")
        if ev.acid_lts is not None and ev.acid_lts > ZERO:
            result.errors.append("Un registro sin mortalidad no puede consumir ácido")
        return result

    # --- Carga con mortalidad -----------------------------------------------
    if ev.mortality_kg is None or ev.mortality_kg <= ZERO:
        result.errors.append("Falta el peso de la mortalidad (kg)")
    elif not (PLAUSIBLE_KG[0] <= float(ev.mortality_kg) <= PLAUSIBLE_KG[1]):
        result.errors.append(
            f"Peso fuera de rango plausible ({PLAUSIBLE_KG[0]:g}–{PLAUSIBLE_KG[1]:g} kg)"
        )

    if ev.drum_id is None:
        result.errors.append("Falta indicar el tambor que recibió la carga")

    # --- pH: el criterio de aceptación --------------------------------------
    if ev.ph_value is None:
        result.warnings.append("Carga sin medición de pH")
    elif not (PLAUSIBLE_PH[0] <= float(ev.ph_value) <= PLAUSIBLE_PH[1]):
        result.errors.append("pH fuera del rango físico (0–14)")
    else:
        result.ph_below_limit = ev.ph_value < ph_limit
        if not result.ph_below_limit:
            # No es un error: se acepta para dejar constancia del problema, pero
            # la carga queda no conforme y visible hasta que alguien la resuelva.
            result.conformity = "no_conforme"
            result.warnings.append(
                f"pH {ev.ph_value} no alcanzó el objetivo (bajo {ph_limit})"
            )

    # --- Dosis de ácido: SOLO chequeo de verosimilitud ----------------------
    if ev.acid_lts is not None and not (PLAUSIBLE_ACID_LTS[0] <= float(ev.acid_lts) <= PLAUSIBLE_ACID_LTS[1]):
        result.errors.append(
            f"Litros de ácido fuera de rango plausible "
            f"({PLAUSIBLE_ACID_LTS[0]:g}–{PLAUSIBLE_ACID_LTS[1]:g} lts)"
        )
    elif ev.acid_lts is not None and ev.mortality_kg and ev.mortality_kg > ZERO:
        dose_min = _threshold(thresholds, "acid_lts_per_kg_min")
        dose_max = _threshold(thresholds, "acid_lts_per_kg_max")
        result.dose_lts_per_kg = (ev.acid_lts / ev.mortality_kg).quantize(Decimal("0.001"))
        result.dose_in_range = dose_min <= result.dose_lts_per_kg <= dose_max
        if not result.dose_in_range:
            # Aviso, jamás error: el criterio es el pH (ver docstring del módulo).
            result.warnings.append(
                f"Dosis {result.dose_lts_per_kg} lts/kg fuera del rango habitual "
                f"({dose_min}–{dose_max}); verifica los litros digitados"
            )

    if ev.acid_used and (ev.acid_lts is None or ev.acid_lts <= ZERO):
        result.warnings.append("Se marcó uso de ácido pero no se digitaron litros")
    if ev.additional_acid and not ev.acid_used:
        result.warnings.append("Se marcó ácido adicional sin marcar uso de ácido")

    # --- Estado del tambor ---------------------------------------------------
    previous_kg = _dec(drum_total_kg)
    if previous_kg is not None and ev.mortality_kg is not None:
        result.drum_status, drum_warning = evaluate_drum_fill(
            previous_kg + ev.mortality_kg, thresholds
        )
        if drum_warning:
            result.warnings.append(drum_warning)

    return result


def evaluate_drum_fill(total_kg: Any, thresholds: Optional[dict] = None) -> tuple[str, Optional[str]]:
    """Estado de llenado del tambor: ok | should_close | over_capacity.

    La capacidad es única para todos los tambores (umbral `drum_capacity_kg`),
    no un atributo por tambor.

    `should_close` NO es un "conviene": el procedimiento manda cerrar y sellar el
    tambor **a 4/5 de su capacidad** (`drum_close_fill_ratio`), así que a partir
    de ese punto el aviso es una instrucción, no una sugerencia.
    """
    capacity = _threshold(thresholds, "drum_capacity_kg")
    ratio = _threshold(thresholds, "drum_close_fill_ratio")
    kg = _dec(total_kg) or ZERO
    if capacity <= ZERO:
        return "ok", None
    if kg >= capacity:
        return "over_capacity", (
            f"El tambor superó su capacidad ({kg} de {capacity} kg): ciérralo y sella"
        )
    if kg >= capacity * ratio:
        pct = int(ratio * 100)
        return "should_close", (
            f"El tambor va en {kg} de {capacity} kg (sobre {pct}%): "
            f"el procedimiento manda cerrarlo y sellarlo"
        )
    return "ok", None


def nominal_acid_lts(mortality_kg: Any, thresholds: Optional[dict] = None) -> Optional[Decimal]:
    """Litros que indica el procedimiento para esos kg (1 lt por 4 kg).

    Es una **referencia** para mostrar en pantalla, no un valor que el sistema
    escriba por el operador: quien digita los litros es él, y ese número es el
    que consume el inventario.
    """
    kg = _dec(mortality_kg)
    if kg is None or kg <= ZERO:
        return None
    nominal = _threshold(thresholds, "acid_lts_per_kg_nominal")
    return (kg * nominal).quantize(Decimal("0.01"))


# ---------------------------------------------------------------------------
# Agregación: cargas → resumen del día
# ---------------------------------------------------------------------------
@dataclass
class DailySummary:
    """Resumen de un día. Es información de gestión: el registro oficial es la
    tabla de cargas, no esta agregación."""

    log_date: Optional[date]
    events_count: int = 0            # cargas no anuladas con mortalidad
    voided_count: int = 0
    total_kg: Decimal = ZERO
    total_acid_lts: Decimal = ZERO
    ph_min: Optional[Decimal] = None
    ph_max: Optional[Decimal] = None
    drums: list = field(default_factory=list)      # N° de tambores usados, en orden
    any_acid_used: bool = False
    any_additional_acid: bool = False
    any_drum_closed: bool = False
    has_mortality: bool = False
    no_mortality_declared: bool = False            # marca explícita "hoy no hubo"
    non_conforming_count: int = 0                  # cargas con pH sobre el límite
    pending_reception: int = 0

    @property
    def is_missing_record(self) -> bool:
        """Día sin ninguna fila: nadie registró. Distinto de un día declarado sin
        mortalidad."""
        return not self.has_mortality and not self.no_mortality_declared and self.voided_count == 0


def build_daily_summary(events: Iterable[Any], thresholds: Optional[dict] = None,
                        log_date: Optional[date] = None) -> DailySummary:
    """Agrega las cargas de UN día.

    Ignora las anuladas en todas las sumas (pero las cuenta aparte, porque el
    detalle del día las muestra tachadas con su motivo).
    """
    ph_limit = _threshold(thresholds, "ph_limit")
    views = [EventView.of(e) for e in events]
    summary = DailySummary(log_date=log_date or (views[0].log_date if views else None))

    for ev in views:
        if ev.is_voided:
            summary.voided_count += 1
            continue

        if not ev.had_mortality:
            summary.no_mortality_declared = True
            continue

        summary.has_mortality = True
        summary.events_count += 1

        if ev.mortality_kg is not None:
            summary.total_kg += ev.mortality_kg
        if ev.acid_lts is not None:
            summary.total_acid_lts += ev.acid_lts
        if ev.acid_used:
            summary.any_acid_used = True
        if ev.additional_acid:
            summary.any_additional_acid = True
        if ev.drum_closed:
            summary.any_drum_closed = True
        if not ev.is_received:
            summary.pending_reception += 1

        if ev.ph_value is not None:
            summary.ph_min = ev.ph_value if summary.ph_min is None else min(summary.ph_min, ev.ph_value)
            summary.ph_max = ev.ph_value if summary.ph_max is None else max(summary.ph_max, ev.ph_value)
            if ev.ph_value >= ph_limit:
                summary.non_conforming_count += 1

        # Un día puede tocar más de un tambor (el primero se llenó a media
        # jornada). Se listan todos, en orden de aparición y sin repetir.
        number = ev.drum_number if ev.drum_number is not None else ev.drum_id
        if number is not None and number not in summary.drums:
            summary.drums.append(number)

    return summary


def build_daily_summaries(events: Iterable[Any], thresholds: Optional[dict] = None) -> dict:
    """Agrupa cargas de varios días y devuelve {log_date: DailySummary}."""
    by_date: dict = {}
    for event in events:
        ev = EventView.of(event)
        by_date.setdefault(ev.log_date, []).append(ev)
    return {
        day: build_daily_summary(day_events, thresholds, log_date=day)
        for day, day_events in sorted(by_date.items(), key=lambda kv: (kv[0] is None, kv[0]))
    }


# ---------------------------------------------------------------------------
# Consolidación semanal y contraste con lo declarado
# ---------------------------------------------------------------------------
@dataclass
class PeriodTotals:
    total_kg: Decimal = ZERO
    total_acid_lts: Decimal = ZERO
    events_count: int = 0
    drums_used: int = 0
    drums_sealed: int = 0


def build_period_totals(events: Iterable[Any]) -> PeriodTotals:
    """Totales del período (se usa para la semana de la inspección)."""
    totals = PeriodTotals()
    drums: set = set()
    for event in events:
        ev = EventView.of(event)
        if ev.is_voided or not ev.had_mortality:
            continue
        totals.events_count += 1
        if ev.mortality_kg is not None:
            totals.total_kg += ev.mortality_kg
        if ev.acid_lts is not None:
            totals.total_acid_lts += ev.acid_lts
        if ev.drum_closed:
            totals.drums_sealed += 1
        number = ev.drum_number if ev.drum_number is not None else ev.drum_id
        if number is not None:
            drums.add(number)
    totals.drums_used = len(drums)
    return totals


@dataclass
class Variance:
    item: str
    declared: Optional[Decimal]
    computed: Optional[Decimal]

    @property
    def delta(self) -> Optional[Decimal]:
        if self.declared is None or self.computed is None:
            return None
        return self.declared - self.computed

    @property
    def matches(self) -> Optional[bool]:
        d = self.delta
        return None if d is None else d == ZERO


def compare_weekly_declaration(declared: Any, totals: PeriodTotals,
                               acid_available_lts: Any = None) -> list[Variance]:
    """Contrasta lo que declaró el inspector contra lo que calcula el sistema.

    Es informativo y NO bloqueante: lo declarado se conserva tal cual porque es
    el registro con valor legal. El descuadre se muestra, no se corrige solo.
    """
    get = declared.get if isinstance(declared, dict) else (lambda k, d=None: getattr(declared, k, d))
    variances = [
        Variance("Ácido utilizado (lts)", _dec(get("acid_used_lts")), totals.total_acid_lts),
        Variance("Tambores utilizados", _dec(get("drums_used")), _dec(totals.drums_used)),
    ]
    available = _dec(acid_available_lts)
    if available is not None:
        variances.append(
            Variance("Ácido disponible (lts)", _dec(get("acid_available_lts")), available)
        )
    return variances


# ---------------------------------------------------------------------------
# Inventario de ácido
# ---------------------------------------------------------------------------
def acid_available_lts(movements: Iterable[Any]) -> Decimal:
    """Disponible = Σ ingresos + Σ ajustes − Σ consumos.

    Mismo criterio que el inventario de alimento de CrianzaApp, a propósito: dos
    semánticas de stock distintas en la misma app serían una fuente segura de
    confusión.
    """
    total = ZERO
    for movement in movements:
        get = movement.get if isinstance(movement, dict) else (lambda k, d=None: getattr(movement, k, d))
        lts = _dec(get("lts")) or ZERO
        kind = (get("movement_type") or "").strip().lower()
        if kind == "out":
            total -= lts
        else:  # in / adjustment (un ajuste negativo viaja con lts negativo)
            total += lts
    return total


def acid_stock_is_low(available: Any, thresholds: Optional[dict] = None) -> bool:
    value = _dec(available)
    if value is None:
        return False
    return value < _threshold(thresholds, "acid_low_stock_lts")


# ---------------------------------------------------------------------------
# Folio
# ---------------------------------------------------------------------------
def format_folio(folio: Optional[int]) -> str:
    """Folio para mostrar. Es un correlativo GLOBAL (no se reinicia por año ni
    por mes), así que el formato no lleva año: prometer `EN-2026-…` insinuaría un
    reinicio anual que no ocurre.

    Devuelve "pendiente" mientras la carga no se haya sincronizado: el folio lo
    asigna el servidor, y mostrar un número provisorio en la PWA llevaría al
    operador a anotar en su cuaderno un folio que después no existe.
    """
    if folio is None:
        return "pendiente"
    return f"EN-{int(folio):06d}"
