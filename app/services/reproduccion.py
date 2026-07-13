"""Lógica del módulo Reproducción (Proceso 1: selección).

F2: contexto de la vista principal (Tabla A / Tabla B) + expiración lazy de
selecciones vencidas con aplicación del sufijo `_R` (§3.2 del spec).
"""
from datetime import date, datetime, time, timedelta

from sqlalchemy.orm import Session

from app.models.reproductor_selections import ReproductorSelection
from app.models.reproductor_monitoring_logs import ReproductorMonitoringLog
from app.models.reproductor_spawning_events import ReproductorSpawningEvent
from app.models.reproductor_spawning_males import ReproductorSpawningMale
from app.models.reproductor_spawning_incubators import ReproductorSpawningIncubator
from app.models.reproductor_recovery_reports import ReproductorRecoveryReport
from app.models.incubator_batches import IncubatorBatch
from app.models.incubator_units import IncubatorUnit
from app.models.incubator_monitoring_logs import IncubatorMonitoringLog
from app.models.incubator_split_events import IncubatorSplitEvent
from app.models.fish import Fish
from app.models.lots import Lot
from app.models.species import Species
from app.models.fish_drug_uses import FishDrugUse
from app.models.ponds import Pond
from app.models.pond_types import PondType
from app.models.cultivation_units import CultivationUnit
from app.models.ponds_movements import PondMovement

SELECTION_DAYS = 30

# Salas Hatchery válidas como destino de eclosión (P10). Se filtra por unidad,
# no por string exacto; se normaliza para tolerar tildes/espacios/dos puntos.
HATCHERY_UNIT_HINTS = ("hatchery",)


class ReproduccionError(ValueError):
    """Error de negocio del módulo (mensaje apto para mostrar al usuario)."""


def active_selection_for(db: Session, fish_id: int) -> ReproductorSelection | None:
    return (
        db.query(ReproductorSelection)
        .filter(ReproductorSelection.fish_id == fish_id, ReproductorSelection.status == "active")
        .first()
    )


def create_selection(db: Session, fish_id: int, sex: str | None = None) -> ReproductorSelection:
    """Crea una selección activa de 30 días (§3.1). Fuerza sexo si falta (P2)."""
    fish = db.get(Fish, fish_id)
    if not fish:
        raise ReproduccionError("Pez no encontrado.")
    if active_selection_for(db, fish_id):
        raise ReproduccionError("El pez ya tiene una selección activa.")

    sex_val = (sex or fish.sex or "").strip().upper()[:1]
    if sex_val not in ("F", "M"):
        raise ReproduccionError("Debe indicar el sexo del pez (F/M) para seleccionarlo.")
    if not fish.sex or (fish.sex or "").upper()[:1] not in ("F", "M"):
        fish.sex = sex_val  # persiste el sexo forzado en el mismo flujo

    today = date.today()
    now = datetime.now()
    sel = ReproductorSelection(
        fish_id=fish_id,
        start_date=today,
        end_date=today + timedelta(days=SELECTION_DAYS),
        status="active",
        sex_at_selection=sex_val,
        created_at=now,
        updated_at=now,
    )
    db.add(sel)
    db.commit()
    return sel


def fail_selection(db: Session, selection_id: int, note: str | None = None) -> None:
    """Marca una selección como fallida (§3.1). Nunca aplica `_R` (§3.2)."""
    sel = db.get(ReproductorSelection, selection_id)
    if not sel or sel.status != "active":
        raise ReproduccionError("La selección no está activa.")
    sel.status = "failed"
    sel.closed_at = datetime.now()
    if note:
        sel.notes = note
    db.commit()


def add_drug_log(db: Session, selection_id: int, *, log_date: str, log_time: str | None,
                 drug_name: str, dose: str, water_temp: str | None) -> None:
    """Registra un fármaco de reproducción en fish_drug_uses (P9)."""
    sel = db.get(ReproductorSelection, selection_id)
    if not sel or sel.status != "active":
        raise ReproduccionError("La selección no está activa.")
    if not (drug_name or "").strip() or not (dose or "").strip() or not (log_date or "").strip():
        raise ReproduccionError("Fármaco, dosis y fecha son obligatorios.")
    try:
        app_dt = datetime.strptime(log_date.strip(), "%Y-%m-%d")
        if log_time and log_time.strip():
            hh, mm = log_time.strip().split(":")[:2]
            app_dt = app_dt.replace(hour=int(hh), minute=int(mm))
    except ValueError:
        raise ReproduccionError("Fecha u hora inválida.")

    now = datetime.now()
    db.add(FishDrugUse(
        fish_id=sel.fish_id,
        drug_name=drug_name.strip(),
        dose=dose.strip(),
        application_date=app_dt,
        withdrawal_period=None,  # reproducción puede no tener carencia (P9)
        reproductor_selection_id=sel.id,
        water_temp=_to_decimal(water_temp),
        created_at=now,
        updated_at=now,
    ))
    db.commit()


def add_monitoring_log(db: Session, selection_id: int, *, log_date: str, sex: str,
                       sperm_density: str | None = None, sperm_motility: str | None = None,
                       sperm_time: str | None = None, ip_value: str | None = None,
                       observation: str | None = None) -> None:
    """Registra un monitoreo reproductivo (campos según sexo, §5.2)."""
    sel = db.get(ReproductorSelection, selection_id)
    if not sel or sel.status != "active":
        raise ReproduccionError("La selección no está activa.")
    if not (log_date or "").strip():
        raise ReproduccionError("La fecha del monitoreo es obligatoria.")
    try:
        d = datetime.strptime(log_date.strip(), "%Y-%m-%d").date()
    except ValueError:
        raise ReproduccionError("Fecha inválida.")

    sex_val = (sex or sel.sex_at_selection or "").strip().upper()[:1]
    ip_int = None
    if ip_value and str(ip_value).strip():
        try:
            ip_int = max(0, min(100, int(float(ip_value))))
        except ValueError:
            raise ReproduccionError("IP debe ser un número 0–100.")

    db.add(ReproductorMonitoringLog(
        selection_id=sel.id,
        fish_id=sel.fish_id,
        log_date=d,
        sex=sex_val,
        sperm_density=(sperm_density or None),
        sperm_motility=(sperm_motility or None),
        sperm_time=(sperm_time or None),
        ip_value=ip_int,
        observation=((observation or "").strip()[:100] or None),
        created_at=datetime.now(),
    ))
    db.commit()


def _to_decimal(value):
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(str(value).replace(",", "."))
    except ValueError:
        return None


# ═══════════════════════════════════════════════════════════════════════════
# Proceso 2 — Desove (§8)
# ═══════════════════════════════════════════════════════════════════════════
def get_or_create_spawning(db: Session, selection_id: int) -> ReproductorSpawningEvent:
    """Devuelve el desove de la selección (lo crea en `draft` si no existe)."""
    sel = db.get(ReproductorSelection, selection_id)
    if not sel:
        raise ReproduccionError("Selección no encontrada.")
    if (sel.sex_at_selection or "").upper()[:1] != "F":
        raise ReproduccionError("El desove solo aplica a hembras seleccionadas.")
    ev = (
        db.query(ReproductorSpawningEvent)
        .filter(ReproductorSpawningEvent.selection_id == sel.id)
        .first()
    )
    if not ev:
        now = datetime.now()
        ev = ReproductorSpawningEvent(
            selection_id=sel.id, fish_id=sel.fish_id, status="draft",
            created_at=now, updated_at=now,
        )
        db.add(ev)
        db.commit()
    return ev


def available_males(db: Session):
    """Machos con selección activa (candidatos para el desove, §8.5)."""
    return (
        db.query(ReproductorSelection, Fish)
        .join(Fish, Fish.id == ReproductorSelection.fish_id)
        .filter(
            ReproductorSelection.status == "active",
            ReproductorSelection.sex_at_selection == "M",
        )
        .order_by(Fish.internal_id)
        .all()
    )


def save_spawning(db: Session, selection_id: int, form: dict) -> ReproductorSpawningEvent:
    """Guardado parcial del formulario de desove (reemplaza machos/incubadoras/recuperación)."""
    ev = get_or_create_spawning(db, selection_id)
    if ev.status == "completed":
        raise ReproduccionError("El desove ya está cerrado.")

    ev.event_date = _parse_date(form.get("event_date"))
    ev.event_time = _parse_time(form.get("event_time"))
    ev.water_temp = _to_decimal(form.get("water_temp"))
    ev.anesthesia_drug_name = (form.get("anesthesia_drug_name") or "").strip() or None
    ev.anesthesia_dilution = (form.get("anesthesia_dilution") or "").strip() or None
    ev.total_ova_weight_g = _to_decimal(form.get("total_ova_weight_g"))
    ev.ova_sample_grams = _to_decimal(form.get("ova_sample_grams"))
    ev.ova_count_sample = _to_int(form.get("ova_count_sample"))
    ev.ova_diameter_mm = _to_decimal(form.get("ova_diameter_mm"))
    ev.viability_pct = _to_decimal(form.get("viability_pct"))
    ev.notes = (form.get("notes") or "").strip() or None
    ev.status = "partial"
    ev.updated_at = datetime.now()

    # Machos (lista de selection_ids marcados)
    male_sel_ids = [int(x) for x in form.get("male_selection_ids", []) if str(x).strip().isdigit()]
    db.query(ReproductorSpawningMale).filter(
        ReproductorSpawningMale.spawning_event_id == ev.id
    ).delete()
    for msel_id in male_sel_ids:
        msel = db.get(ReproductorSelection, msel_id)
        if msel:
            db.add(ReproductorSpawningMale(
                spawning_event_id=ev.id, male_selection_id=msel.id,
                fish_id=msel.fish_id, created_at=datetime.now(),
            ))

    # Incubadoras (listas paralelas incubator_id[] / ova_weight_g[])
    inc_ids = form.get("incubator_id", [])
    inc_ws = form.get("incubator_weight", [])
    db.query(ReproductorSpawningIncubator).filter(
        ReproductorSpawningIncubator.spawning_event_id == ev.id
    ).delete()
    for iid, w in zip(inc_ids, inc_ws):
        iid_i, w_f = _to_int(iid), _to_decimal(w)
        if iid_i and w_f:
            db.add(ReproductorSpawningIncubator(
                spawning_event_id=ev.id, incubator_id=iid_i,
                ova_weight_g=w_f, created_at=datetime.now(),
            ))

    # Recuperación (recovery_<fish_id> = successful/sacrifice)
    db.query(ReproductorRecoveryReport).filter(
        ReproductorRecoveryReport.spawning_event_id == ev.id
    ).delete()
    female_sel = db.get(ReproductorSelection, selection_id)
    involved = [(female_sel.fish_id, "female")] + [
        (db.get(ReproductorSelection, mid).fish_id, "male") for mid in male_sel_ids
    ]
    for fid, role in involved:
        result = (form.get(f"recovery_{fid}") or "").strip()
        if result in ("successful", "sacrifice"):
            db.add(ReproductorRecoveryReport(
                spawning_event_id=ev.id, fish_id=fid, role=role,
                recovery_result=result, created_at=datetime.now(),
            ))

    db.commit()
    return ev


def complete_spawning(db: Session, selection_id: int) -> IncubatorBatch:
    """Cierra el Proceso 2 y abre el Proceso 3 (§8.9). Valida invariantes duras."""
    ev = get_or_create_spawning(db, selection_id)
    existing = db.query(IncubatorBatch).filter(
        IncubatorBatch.spawning_event_id == ev.id
    ).first()
    if ev.status == "completed" and existing:
        return existing

    sel = db.get(ReproductorSelection, ev.selection_id)
    males = db.query(ReproductorSpawningMale).filter(
        ReproductorSpawningMale.spawning_event_id == ev.id
    ).all()
    incs = db.query(ReproductorSpawningIncubator).filter(
        ReproductorSpawningIncubator.spawning_event_id == ev.id
    ).all()

    if ev.total_ova_weight_g is None or not ev.ova_sample_grams or not ev.ova_count_sample:
        raise ReproduccionError("Faltan datos de ovas (peso total, muestra n y recuento).")
    if not males:
        raise ReproduccionError("Debe vincular al menos un macho.")
    if not incs:
        raise ReproduccionError("Debe distribuir las ovas en al menos una incubadora.")
    total_dist = sum(float(i.ova_weight_g) for i in incs)
    if abs(total_dist - float(ev.total_ova_weight_g)) > 0.01:
        raise ReproduccionError(
            f"La suma distribuida ({total_dist:g} g) debe igualar el peso total de ovas "
            f"({float(ev.total_ova_weight_g):g} g)."
        )
    recovery_fids = {
        r.fish_id for r in db.query(ReproductorRecoveryReport).filter(
            ReproductorRecoveryReport.spawning_event_id == ev.id
        )
    }
    involved_fids = {sel.fish_id} | {m.fish_id for m in males}
    if not involved_fids.issubset(recovery_fids):
        raise ReproduccionError("Debe registrar la recuperación de todos los reproductores.")

    now = datetime.now()
    # Anestesia → historial de fármacos de todos los involucrados (§8.4, P9)
    if ev.anesthesia_drug_name:
        app_dt = datetime.combine(ev.event_date or date.today(), time())
        for sel_obj in [sel] + [db.get(ReproductorSelection, m.male_selection_id) for m in males]:
            if sel_obj:
                db.add(FishDrugUse(
                    fish_id=sel_obj.fish_id, drug_name=ev.anesthesia_drug_name,
                    dose=ev.anesthesia_dilution or "", application_date=app_dt,
                    withdrawal_period=None, reproductor_selection_id=sel_obj.id,
                    water_temp=ev.water_temp, created_at=now, updated_at=now,
                ))
        db.flush()

    # Cierra selecciones (spawned) → aplica _R si hubo fármacos (§3.2)
    close_selection(db, sel, "spawned")
    for m in males:
        msel = db.get(ReproductorSelection, m.male_selection_id)
        if msel and msel.status == "active":
            close_selection(db, msel, "spawned")

    # Crea el batch (Proceso 3) + una unidad por incubadora
    batch = IncubatorBatch(
        spawning_event_id=ev.id, status="monitoring", created_at=now, updated_at=now,
    )
    db.add(batch)
    db.flush()
    for inc in incs:
        db.add(IncubatorUnit(
            incubator_batch_id=batch.id, display_id=str(inc.incubator_id),
            base_incubator_id=inc.incubator_id, current_weight_g=inc.ova_weight_g,
            status="active", created_at=now, updated_at=now,
        ))
    ev.status = "completed"
    ev.updated_at = now
    db.commit()
    return batch


def build_spawning_context(db: Session, selection_id: int) -> dict:
    ev = get_or_create_spawning(db, selection_id)
    sel = db.get(ReproductorSelection, ev.selection_id)
    female = db.get(Fish, sel.fish_id)
    males = db.query(ReproductorSpawningMale).filter(
        ReproductorSpawningMale.spawning_event_id == ev.id
    ).all()
    chosen_male_sel_ids = {m.male_selection_id for m in males}
    incs = db.query(ReproductorSpawningIncubator).filter(
        ReproductorSpawningIncubator.spawning_event_id == ev.id
    ).order_by(ReproductorSpawningIncubator.incubator_id).all()
    recovery = {
        r.fish_id: r.recovery_result for r in db.query(ReproductorRecoveryReport).filter(
            ReproductorRecoveryReport.spawning_event_id == ev.id
        )
    }
    batch = db.query(IncubatorBatch).filter(
        IncubatorBatch.spawning_event_id == ev.id
    ).first()
    males_avail = [
        {"selection_id": s.id, "fish_id": f.id, "pit": f.internal_id,
         "checked": s.id in chosen_male_sel_ids}
        for s, f in available_males(db)
    ]
    return {
        "ev": ev,
        "selection_id": sel.id,
        "female": {"pit": female.internal_id, "id": female.id, "recovery": recovery.get(female.id, "")},
        "males_avail": males_avail,
        "males_recovery": [
            {"fish_id": db.get(ReproductorSelection, m.male_selection_id).fish_id,
             "pit": db.get(Fish, m.fish_id).internal_id,
             "recovery": recovery.get(m.fish_id, "")}
            for m in males
        ],
        "incubators": [{"incubator_id": i.incubator_id, "weight": float(i.ova_weight_g)} for i in incs],
        "batch_id": batch.id if batch else None,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Proceso 3 — Monitoreo de incubadoras (§9)
# ═══════════════════════════════════════════════════════════════════════════
def _active_units(db: Session, batch_id: int):
    return (
        db.query(IncubatorUnit)
        .filter(IncubatorUnit.incubator_batch_id == batch_id, IncubatorUnit.status == "active")
        .order_by(IncubatorUnit.display_id)
        .all()
    )


def _last_viability(db: Session, unit_id: int):
    row = (
        db.query(IncubatorMonitoringLog)
        .filter(IncubatorMonitoringLog.incubator_unit_id == unit_id)
        .order_by(IncubatorMonitoringLog.log_datetime.desc())
        .first()
    )
    return float(row.viability_pct) if row and row.viability_pct is not None else None


def hatchery_pond_options(db: Session):
    """Estanques Batea (incl. subestanques) en salas Hatchery (P10)."""
    rows = (
        db.query(Pond, CultivationUnit.name)
        .join(PondType, PondType.id == Pond.pond_type_id)
        .outerjoin(CultivationUnit, CultivationUnit.id == Pond.cultivation_unit_id)
        .filter(PondType.name == "Batea")
        .order_by(Pond.name)
        .all()
    )
    opts = []
    for pond, unit_name in rows:
        if unit_name and any(h in unit_name.lower() for h in HATCHERY_UNIT_HINTS):
            opts.append({"id": pond.id, "label": f"{unit_name} · {pond.name}"})
    return opts


def save_lot(db: Session, batch_id: int, name: str, code: str) -> None:
    """Crea/actualiza la fila en `lots` y enlaza batch.lot_id (P8, §9.2)."""
    batch = db.get(IncubatorBatch, batch_id)
    if not batch:
        raise ReproduccionError("Proceso 3 no encontrado.")
    name = (name or "").strip()
    code = (code or "").strip().upper()
    if not name or not code:
        raise ReproduccionError("Nombre y código del lote son obligatorios.")
    if len(code) > 3:
        raise ReproduccionError("El código del lote debe tener máximo 3 caracteres.")

    # species_id de la hembra madre; hatch_year del año actual
    ev = db.get(ReproductorSpawningEvent, batch.spawning_event_id)
    mother = db.get(Fish, ev.fish_id)
    mother_lot = db.get(Lot, mother.lot_id) if mother and mother.lot_id else None
    species_id = mother_lot.species_id if mother_lot else None
    if species_id is None:
        raise ReproduccionError("No se pudo derivar la especie desde la hembra madre.")

    now = datetime.now()
    if batch.lot_id:
        lot = db.get(Lot, batch.lot_id)
        lot.name = name
        lot.internal_id = code
        lot.updated_at = now
    else:
        lot = Lot(
            species_id=species_id, name=name, internal_id=code,
            hatch_year=date.today().year, creation_date=now,
            created_at=now, updated_at=now,
        )
        db.add(lot)
        db.flush()
        batch.lot_id = lot.id
    batch.updated_at = now
    db.commit()


def save_monitoring_round(db: Session, batch_id: int, form: dict) -> int:
    """Guarda una ronda de monitoreo para todas las unidades activas (§9.4)."""
    units = _active_units(db, batch_id)
    when = _parse_datetime(form.get("log_datetime")) or datetime.now()
    saved = 0
    for u in units:
        size = _to_decimal(form.get(f"size_{u.id}"))
        via = _to_decimal(form.get(f"viability_{u.id}"))
        obs = (form.get(f"observation_{u.id}") or "").strip() or None
        if size is None and via is None and obs is None:
            continue
        db.add(IncubatorMonitoringLog(
            incubator_unit_id=u.id, log_datetime=when, size_mm=size,
            viability_pct=via, observation=obs, created_at=datetime.now(),
        ))
        saved += 1
    if saved:
        db.commit()
    return saved


def split_unit(db: Session, unit_id: int, targets: list, notes: str | None = None) -> None:
    """Particiona una unidad en N sub-unidades (§9.5). targets=[(base_id, weight),…]."""
    src = db.get(IncubatorUnit, unit_id)
    if not src or src.status != "active":
        raise ReproduccionError("La incubadora no está activa.")
    parts = [(int(b), _to_decimal(w)) for b, w in targets if str(b).strip().isdigit() and _to_decimal(w)]
    if len(parts) < 2:
        raise ReproduccionError("Una partición requiere al menos 2 sub-unidades.")
    total = sum(w for _, w in parts)
    if abs(total - float(src.current_weight_g or 0)) > 0.01:
        raise ReproduccionError(
            f"La suma de las sub-unidades ({total:g} g) debe igualar el peso actual "
            f"({float(src.current_weight_g or 0):g} g)."
        )
    now = datetime.now()
    db.add(IncubatorSplitEvent(source_unit_id=src.id, split_datetime=now, notes=notes, created_at=now))
    for base_id, w in parts:
        db.add(IncubatorUnit(
            incubator_batch_id=src.incubator_batch_id,
            display_id=f"{base_id}ex{src.display_id}",
            base_incubator_id=base_id, current_weight_g=w,
            parent_unit_id=src.id, status="active", created_at=now, updated_at=now,
        ))
    src.status = "split"
    src.updated_at = now
    db.commit()


def hatch_unit(db: Session, unit_id: int, target_pond_id: int) -> None:
    """Cierra una incubadora por eclosión (§9.6)."""
    unit = db.get(IncubatorUnit, unit_id)
    if not unit or unit.status != "active":
        raise ReproduccionError("La incubadora no está activa.")
    valid_ids = {o["id"] for o in hatchery_pond_options(db)}
    if target_pond_id not in valid_ids:
        raise ReproduccionError("El estanque de destino debe ser una Batea de sala Hatchery.")
    now = datetime.now()
    unit.status = "hatched"
    unit.target_pond_id = target_pond_id
    unit.hatched_at = now
    unit.updated_at = now
    db.flush()  # SessionLocal es autoflush=False: forzar para que el conteo siguiente lo vea
    # Si todas las unidades eclosionaron → batch a awaiting_first_count
    remaining = _active_units(db, unit.incubator_batch_id)
    if not remaining:
        batch = db.get(IncubatorBatch, unit.incubator_batch_id)
        if batch.status == "monitoring":
            batch.status = "awaiting_first_count"
            batch.updated_at = now
    db.commit()


def complete_batch(db: Session, batch_id: int) -> None:
    """Cierra el Proceso 3 (§9.7): todas eclosionadas + lote definido."""
    batch = db.get(IncubatorBatch, batch_id)
    if not batch:
        raise ReproduccionError("Proceso 3 no encontrado.")
    if _active_units(db, batch_id):
        raise ReproduccionError("Aún hay incubadoras activas sin eclosionar.")
    if not batch.lot_id:
        raise ReproduccionError("Debe definir el lote (nombre y código) antes de cerrar.")
    batch.status = "completed"
    batch.updated_at = datetime.now()
    db.commit()


def build_incubator_context(db: Session, batch_id: int) -> dict:
    batch = db.get(IncubatorBatch, batch_id)
    if not batch:
        raise ReproduccionError("Proceso 3 no encontrado.")
    ev = db.get(ReproductorSpawningEvent, batch.spawning_event_id)
    units_all = (
        db.query(IncubatorUnit)
        .filter(IncubatorUnit.incubator_batch_id == batch_id)
        .order_by(IncubatorUnit.display_id)
        .all()
    )
    active = [u for u in units_all if u.status == "active"]

    # KPI 1 — viabilidad ponderada por peso (última por unidad activa)
    num = den = 0.0
    for u in active:
        v = _last_viability(db, u.id)
        w = float(u.current_weight_g or 0)
        if v is not None and w > 0:
            num += v * w
            den += w
    kpi_viability = round(num / den, 1) if den else None

    # KPI 2 — ovas viables estimadas = floor(count/n * peso_total * via/100)
    kpi_viable_ova = None
    if (kpi_viability is not None and ev and ev.ova_count_sample and ev.ova_sample_grams
            and ev.total_ova_weight_g):
        kpi_viable_ova = int(
            (ev.ova_count_sample / float(ev.ova_sample_grams))
            * float(ev.total_ova_weight_g) * (kpi_viability / 100)
        )

    lot = db.get(Lot, batch.lot_id) if batch.lot_id else None
    pond_names = {p.id: n for p, n in [
        (u_pond, u_pond.name) for u_pond in db.query(Pond).all()
    ]}

    # Reproductores originales (hembra + machos con sus lotes)
    def _fish_label(fish_id):
        f = db.get(Fish, fish_id)
        lt = db.get(Lot, f.lot_id) if f and f.lot_id else None
        return {"pit": f.internal_id if f else "—", "lot": lt.name if lt else "—"}

    female_parent = _fish_label(ev.fish_id) if ev else {"pit": "—", "lot": "—"}
    male_parents = [
        _fish_label(m.fish_id)
        for m in db.query(ReproductorSpawningMale)
        .filter(ReproductorSpawningMale.spawning_event_id == batch.spawning_event_id)
        .all()
    ]

    def unit_row(u):
        return {
            "id": u.id, "display_id": u.display_id, "status": u.status,
            "weight": float(u.current_weight_g) if u.current_weight_g is not None else None,
            "last_viability": _last_viability(db, u.id),
            "pond": pond_names.get(u.target_pond_id) if u.target_pond_id else None,
            "hatched_at": u.hatched_at.strftime("%Y-%m-%d %H:%M") if u.hatched_at else None,
        }

    # Histórico de monitoreo (más reciente primero, agrupado por ronda/log_datetime)
    logs = (
        db.query(IncubatorMonitoringLog, IncubatorUnit.display_id)
        .join(IncubatorUnit, IncubatorUnit.id == IncubatorMonitoringLog.incubator_unit_id)
        .filter(IncubatorUnit.incubator_batch_id == batch_id)
        .order_by(IncubatorMonitoringLog.log_datetime.desc(), IncubatorUnit.display_id)
        .all()
    )
    history = [
        {"when": lg.log_datetime.strftime("%Y-%m-%d %H:%M") if lg.log_datetime else "",
         "display_id": disp,
         "size": float(lg.size_mm) if lg.size_mm is not None else "",
         "viability": float(lg.viability_pct) if lg.viability_pct is not None else "",
         "observation": lg.observation or ""}
        for lg, disp in logs
    ]

    return {
        "batch": batch,
        "batch_id": batch.id,
        "status": batch.status,
        "units_all": [unit_row(u) for u in units_all],
        "active_units": [unit_row(u) for u in active],
        "kpi_viability": kpi_viability,
        "kpi_viable_ova": kpi_viable_ova,
        "lot": {"name": lot.name, "code": lot.internal_id} if lot else None,
        "female_parent": female_parent,
        "male_parents": male_parents,
        "pond_options": hatchery_pond_options(db),
        "history": history,
        "can_complete": (not active) and bool(batch.lot_id) and batch.status != "completed",
    }


# --- parsers auxiliares ---
def _parse_date(v):
    v = (v or "").strip()
    try:
        return datetime.strptime(v, "%Y-%m-%d").date() if v else None
    except ValueError:
        return None


def _parse_time(v):
    v = (v or "").strip()
    try:
        hh, mm = v.split(":")[:2]
        return time(int(hh), int(mm)) if v else None
    except (ValueError, IndexError):
        return None


def _parse_datetime(v):
    v = (v or "").strip().replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(v, fmt)
        except ValueError:
            continue
    return None


def _to_int(v):
    try:
        return int(float(str(v).strip())) if str(v).strip() else None
    except ValueError:
        return None


def _selection_used_drugs(db: Session, selection_id: int) -> bool:
    """¿La selección tiene al menos un fármaco registrado en fish_drug_uses (P9)?"""
    return (
        db.query(FishDrugUse.id)
        .filter(FishDrugUse.reproductor_selection_id == selection_id)
        .first()
        is not None
    )


def close_selection(db: Session, sel: ReproductorSelection, new_status: str) -> None:
    """Cierra una selección aplicando la lógica del sufijo `_R` (§3.2).

    - completed / spawned + hubo fármacos → agrega `_R` a fish.internal_id (sin duplicar).
    - failed → nunca agrega `_R`.
    No hace commit (lo hace el caller).
    """
    if new_status in ("completed", "spawned") and _selection_used_drugs(db, sel.id):
        fish = db.get(Fish, sel.fish_id)
        current = (fish.internal_id or "") if fish else ""
        if fish and not current.upper().endswith("_R"):
            fish.internal_id = f"{current}_R"  # el @validates de Fish normaliza a mayúscula
    sel.status = new_status
    sel.closed_at = datetime.now()


def expire_due_selections(db: Session, today: date | None = None) -> int:
    """Opción A (lazy): cierra como `completed` las selecciones `active` vencidas."""
    today = today or date.today()
    due = (
        db.query(ReproductorSelection)
        .filter(
            ReproductorSelection.status == "active",
            ReproductorSelection.end_date < today,
        )
        .all()
    )
    for sel in due:
        close_selection(db, sel, "completed")
    if due:
        db.commit()
    return len(due)


def _current_pond_labels(db: Session, fish_ids: list[int]) -> dict[int, str]:
    """Último estanque de destino por pez (mejor esfuerzo, vía último movimiento)."""
    if not fish_ids:
        return {}
    rows = (
        db.query(PondMovement.fish_id, Pond.code, Pond.name)
        .join(Pond, Pond.id == PondMovement.destiny_pond_id)
        .filter(PondMovement.fish_id.in_(fish_ids))
        .order_by(PondMovement.fish_id, PondMovement.movement_time.desc())
        .distinct(PondMovement.fish_id)
        .all()
    )
    return {fid: (f"{code} - {name}" if code else name) for fid, code, name in rows}


def build_main_context(db: Session) -> dict:
    """Contexto de `/views/ui/reproduccion`: corre expiración y arma Tabla A y B."""
    expired = expire_due_selections(db)
    today = date.today()

    # Tabla A — seleccionados activos
    rows_a = (
        db.query(ReproductorSelection, Fish, Lot, Species)
        .join(Fish, Fish.id == ReproductorSelection.fish_id)
        .join(Lot, Lot.id == Fish.lot_id)
        .join(Species, Species.id == Lot.species_id)
        .filter(ReproductorSelection.status == "active")
        .order_by(ReproductorSelection.start_date)
        .all()
    )
    tabla_a = []
    for sel, fish, lot, sp in rows_a:
        dias = (today - sel.start_date).days if sel.start_date else 0
        sexo = (fish.sex or sel.sex_at_selection or "").upper()
        drugs = (
            db.query(FishDrugUse)
            .filter(FishDrugUse.reproductor_selection_id == sel.id)
            .order_by(FishDrugUse.application_date.desc(), FishDrugUse.id.desc())
            .all()
        )
        monitoring = (
            db.query(ReproductorMonitoringLog)
            .filter(ReproductorMonitoringLog.selection_id == sel.id)
            .order_by(ReproductorMonitoringLog.log_date.desc(), ReproductorMonitoringLog.id.desc())
            .all()
        )
        tabla_a.append(
            {
                "selection_id": sel.id,
                "fish_id": fish.id,
                "pit": fish.internal_id,
                "especie": sp.name,
                "lote": lot.name,
                "sexo": sexo,
                "dias": max(dias, 0),
                "total": SELECTION_DAYS,
                "pct": min(100, round(max(dias, 0) / SELECTION_DAYS * 100)),
                "is_female": sexo.startswith("F"),
                "drugs": [
                    {
                        "date": d.application_date.strftime("%Y-%m-%d") if d.application_date else "",
                        "time": d.application_date.strftime("%H:%M") if d.application_date else "",
                        "drug_name": d.drug_name,
                        "dose": d.dose,
                        "water_temp": (f"{float(d.water_temp):.1f}" if d.water_temp is not None else ""),
                    }
                    for d in drugs
                ],
                "monitoring": [
                    {
                        "date": mlog.log_date.strftime("%Y-%m-%d") if mlog.log_date else "",
                        "sperm_density": mlog.sperm_density or "",
                        "sperm_motility": mlog.sperm_motility or "",
                        "sperm_time": mlog.sperm_time or "",
                        "ip_value": mlog.ip_value if mlog.ip_value is not None else "",
                        "observation": mlog.observation or "",
                    }
                    for mlog in monitoring
                ],
            }
        )

    # Tabla B — candidatos reproductores (_R, vivos, sin selección activa)
    active_fish_ids = {sel.fish_id for sel, *_ in rows_a}
    rows_b = (
        db.query(Fish, Lot, Species)
        .join(Lot, Lot.id == Fish.lot_id)
        .join(Species, Species.id == Lot.species_id)
        .filter(
            Fish.internal_id.like("%\\_R", escape="\\"),
            Fish.state == "alive",
        )
        .order_by(Fish.internal_id)
        .all()
    )
    cand_fish_ids = [f.id for f, *_ in rows_b if f.id not in active_fish_ids]
    pond_labels = _current_pond_labels(db, cand_fish_ids)
    tabla_b = []
    for fish, lot, sp in rows_b:
        if fish.id in active_fish_ids:
            continue
        tabla_b.append(
            {
                "fish_id": fish.id,
                "pit": fish.internal_id,
                "especie": sp.name,
                "lote": lot.name,
                "sexo": (fish.sex or "").upper(),
                "estanque": pond_labels.get(fish.id, "—"),
            }
        )

    # Procesos de incubación (Proceso 3) — punto de entrada a la vista de incubadoras
    batches_raw = db.query(IncubatorBatch).all()
    batches = []
    for b in batches_raw:
        ev = db.get(ReproductorSpawningEvent, b.spawning_event_id)
        female = db.get(Fish, ev.fish_id) if ev else None
        lot = db.get(Lot, b.lot_id) if b.lot_id else None
        units = db.query(IncubatorUnit).filter(IncubatorUnit.incubator_batch_id == b.id).all()
        batches.append({
            "batch_id": b.id,
            "status": b.status,
            "female_pit": female.internal_id if female else "—",
            "event_date": ev.event_date.strftime("%Y-%m-%d") if ev and ev.event_date else "",
            "lot": (f"{lot.name} ({lot.internal_id})" if lot else "—"),
            "units_active": sum(1 for u in units if u.status == "active"),
            "units_hatched": sum(1 for u in units if u.status == "hatched"),
            "units_total": len(units),
        })
    # No cerrados primero; dentro de cada grupo, más reciente primero
    batches.sort(key=lambda x: (x["status"] == "completed", -x["batch_id"]))

    return {
        "tabla_a": tabla_a,
        "tabla_b": tabla_b,
        "batches": batches,
        "expired_count": expired,
        "selection_days": SELECTION_DAYS,
    }
