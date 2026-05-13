from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Optional
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.request
from urllib.parse import quote_plus

from fastapi import APIRouter, Body, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, aliased

from app.db.session import SessionLocal
from app.models.cultivation_declarations import CultivationDeclaration
from app.models.cultivation_declaration_items import CultivationDeclarationItem
from app.models.cultivation_units import CultivationUnit
from app.models.fish import Fish
from app.models.fish_drug_uses import FishDrugUse
from app.models.fish_samplings import FishSampling
from app.models.lots import Lot
from app.models.ponds import Pond
from app.models.ponds_movements import PondMovement
from app.models.sanitary_reports import SanitaryReport
from app.models.species import Species

router = APIRouter(prefix="/views", tags=["cultivation-declarations"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))

_PLANTA_IMPORT_URL_ENV = (os.getenv("PLANTA_IMPORT_URL") or "").strip()
PLANTA_IMPORT_URL = _PLANTA_IMPORT_URL_ENV or "http://127.0.0.1:8000/api/integration/v1/warranty-imports/receive-json"
INTEGRATION_SHARED_SECRET = (
    os.getenv("INTEGRATION_SHARED_SECRET")
    or "kenoz-integration-dev-secret-change-me"
).strip()
INTEGRATION_SOURCE_KEY = (os.getenv("INTEGRATION_SOURCE_KEY") or "crianzaapp").strip() or "crianzaapp"
INTEGRATION_TIMEOUT_SECONDS = int(os.getenv("INTEGRATION_TIMEOUT_SECONDS") or "30")


def _resolve_planta_import_url(request: Request) -> str:
    """
    Resuelve URL de integración hacia PlantaAPP.
    - Si existe PLANTA_IMPORT_URL en ambiente, la respeta.
    - Si no, usa el host con el que el usuario llegó a CrianzaApp y puerto 8000.
    """
    if _PLANTA_IMPORT_URL_ENV:
        return _PLANTA_IMPORT_URL_ENV

    host = (request.url.hostname or "").strip() or "127.0.0.1"
    return f"http://{host}:8000/api/integration/v1/warranty-imports/receive-json"


def _send_declaration_json_to_planta(payload_obj: dict, target_url: str | None = None) -> dict:
    if not INTEGRATION_SHARED_SECRET:
        raise RuntimeError("INTEGRATION_SHARED_SECRET no está configurado")

    resolved_target_url = (target_url or PLANTA_IMPORT_URL).strip() or PLANTA_IMPORT_URL

    body_bytes = json.dumps(payload_obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ts = str(int(time.time()))
    nonce = secrets.token_hex(16)
    signed_message = f"{ts}.{nonce}.".encode("utf-8") + body_bytes
    signature = hmac.new(
        INTEGRATION_SHARED_SECRET.encode("utf-8"),
        signed_message,
        hashlib.sha256,
    ).hexdigest()

    req = urllib.request.Request(
        resolved_target_url,
        data=body_bytes,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Integration-Key": INTEGRATION_SOURCE_KEY,
            "X-Integration-Timestamp": ts,
            "X-Integration-Nonce": nonce,
            "X-Integration-Signature": signature,
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=INTEGRATION_TIMEOUT_SECONDS) as resp:
            response_body = resp.read().decode("utf-8", errors="replace")
            try:
                response_json = json.loads(response_body) if response_body else {}
            except json.JSONDecodeError:
                response_json = {"raw_response": response_body}
            return {
                "ok": 200 <= int(resp.status) < 300,
                "status_code": int(resp.status),
                "response": response_json,
            }
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace") if hasattr(exc, "read") else ""
        try:
            parsed_error = json.loads(error_body) if error_body else {}
        except json.JSONDecodeError:
            parsed_error = {"raw_error": error_body}
        return {
            "ok": False,
            "status_code": int(exc.code),
            "response": parsed_error,
        }


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _parse_withdrawal_days(withdrawal_period: Optional[str]) -> Optional[int]:
    raw = (withdrawal_period or "").strip()
    if not raw:
        return None
    m = re.search(r"(\d+)", raw)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _latest_movement_by_fish(db: Session, fish_ids: list[int]) -> dict[int, PondMovement]:
    if not fish_ids:
        return {}
    rows = (
        db.query(PondMovement)
        .filter(PondMovement.fish_id.in_(fish_ids))
        .order_by(PondMovement.fish_id.asc(), PondMovement.movement_time.desc(), PondMovement.id.desc())
        .all()
    )
    out: dict[int, PondMovement] = {}
    for mv in rows:
        if mv.fish_id not in out:
            out[mv.fish_id] = mv
    return out


def _jaula_available_rows(db: Session):
    jaula_units = (
        db.query(CultivationUnit)
        .filter(func.lower(CultivationUnit.name).like("%jaula%"))
        .all()
    )
    if not jaula_units:
        return [], [], None, {}

    unit_ids = [u.id for u in jaula_units]
    jaula_ponds = db.query(Pond).filter(Pond.cultivation_unit_id.in_(unit_ids)).all()
    if not jaula_ponds:
        return [], [], None, {}

    jaula_pond_ids = [p.id for p in jaula_ponds]
    jaula_pond_name_map = {p.id: p.name for p in jaula_ponds}
    representative_pond_id = min(jaula_pond_ids)

    active_fish = (
        db.query(Fish)
        .filter(
            Fish.internal_id.isnot(None),
            Fish.internal_id != "",
            Fish.state.notin_(["dead", "faena", "in_process", "processed"]),
        )
        .order_by(Fish.lot_id.asc(), Fish.id.asc())
        .all()
    )
    # Excluir reproductores (PIT tag termina en _R)
    active_fish = [f for f in active_fish if not str(f.internal_id or "").upper().endswith("_R")]
    fish_ids = [f.id for f in active_fish]
    latest_mv = _latest_movement_by_fish(db, fish_ids)

    fish_in_jaula = []
    for fish in active_fish:
        mv = latest_mv.get(fish.id)
        if mv and mv.destiny_pond_id in jaula_pond_ids:
            fish_in_jaula.append(fish)

    fish_ids = [f.id for f in fish_in_jaula]
    if not fish_ids:
        return [], jaula_pond_ids, representative_pond_id, jaula_pond_name_map

    depuration_start_map: dict[int, datetime] = {}
    movements = (
        db.query(PondMovement)
        .filter(PondMovement.fish_id.in_(fish_ids))
        .order_by(PondMovement.fish_id.asc(), PondMovement.movement_time.asc(), PondMovement.id.asc())
        .all()
    )
    if movements:
        pond_ids = {
            m.source_pond_id for m in movements if m.source_pond_id
        } | {
            m.destiny_pond_id for m in movements if m.destiny_pond_id
        }
        pond_depuration_map = {
            p.id: bool(p.depuration)
            for p in db.query(Pond).filter(Pond.id.in_(pond_ids)).all()
        } if pond_ids else {}

        cycle_start_by_fish: dict[int, datetime] = {}
        for movement in movements:
            fish_id = movement.fish_id
            if fish_id is None:
                continue

            src_is_depuration = bool(
                movement.source_pond_id and pond_depuration_map.get(movement.source_pond_id)
            )
            dst_is_depuration = bool(
                movement.destiny_pond_id and pond_depuration_map.get(movement.destiny_pond_id)
            )

            if (not src_is_depuration) and dst_is_depuration:
                cycle_start_by_fish[fish_id] = movement.movement_time
            elif src_is_depuration and (not dst_is_depuration):
                cycle_start_by_fish[fish_id] = None

        depuration_start_map = cycle_start_by_fish

    latest_sampling_subq = (
        db.query(
            FishSampling.fish_id,
            func.max(func.coalesce(FishSampling.registry_time, FishSampling.created_at)).label("max_sample_time"),
        )
        .filter(FishSampling.fish_id.in_(fish_ids))
        .group_by(FishSampling.fish_id)
        .subquery()
    )
    latest_samples = (
        db.query(FishSampling)
        .join(
            latest_sampling_subq,
            (latest_sampling_subq.c.fish_id == FishSampling.fish_id)
            & (latest_sampling_subq.c.max_sample_time == func.coalesce(FishSampling.registry_time, FishSampling.created_at)),
        )
        .all()
    )
    samples_map = {s.fish_id: s for s in latest_samples}

    lot_ids = {f.lot_id for f in fish_in_jaula if f.lot_id}
    lots_map = {lot.id: lot for lot in db.query(Lot).filter(Lot.id.in_(lot_ids)).all()} if lot_ids else {}

    drug_rows = (
        db.query(FishDrugUse)
        .filter(FishDrugUse.fish_id.in_(fish_ids))
        .order_by(FishDrugUse.fish_id.asc(), FishDrugUse.application_date.desc(), FishDrugUse.id.desc())
        .all()
    )
    latest_drug_by_fish: dict[int, FishDrugUse] = {}
    for du in drug_rows:
        if du.fish_id not in latest_drug_by_fish:
            latest_drug_by_fish[du.fish_id] = du

    rows = []
    for fish in fish_in_jaula:
        mv = latest_mv.get(fish.id)
        sample = samples_map.get(fish.id)
        weight_g = float(sample.weight) if sample and sample.weight is not None else None
        sex = (fish.sex or "N/D").upper()
        caviar_diameter = None
        if sex == "F" and sample and sample.diameter is not None:
            caviar_diameter = float(sample.diameter)
        lot = lots_map.get(fish.lot_id)

        depuration_days = None
        if fish.state == "depuration":
            dep_start = None
            dep_cycle_start = depuration_start_map.get(fish.id)
            if dep_cycle_start:
                dep_start = dep_cycle_start
            elif fish.depuration_start_time:
                dep_start = fish.depuration_start_time

            if dep_start:
                depuration_days = (datetime.utcnow().date() - dep_start.date()).days

        rows.append(
            {
                "fish_id": fish.id,
                "pit_tag": fish.internal_id,
                "lot_id": fish.lot_id,
                # Enviar nombre completo del lote (ej. "Fátima") en lugar de códigos abreviados.
                "lot": (lot.name or lot.internal_id) if lot else "N/D",
                "sex": fish.sex or "N/D",
                "weight_g": weight_g,
                "caviar_diameter_mm": caviar_diameter,
                "missing_weight": weight_g is None,
                "source_pond_id": mv.destiny_pond_id,
                "source_pond_name": jaula_pond_name_map.get(mv.destiny_pond_id) if mv else None,
                "depuration_days": depuration_days,
                "drug_use": latest_drug_by_fish.get(fish.id),
            }
        )

    return rows, jaula_pond_ids, representative_pond_id, jaula_pond_name_map


def _faena_rows_for_source_pond(db: Session, source_pond_id: int):
    fish_in_faena = (
        db.query(Fish)
        .filter(Fish.state == "faena")
        .order_by(Fish.lot_id.asc(), Fish.id.asc())
        .all()
    )
    fish_ids = [f.id for f in fish_in_faena]
    if not fish_ids:
        return []

    faena_movements = (
        db.query(PondMovement)
        .filter(
            PondMovement.fish_id.in_(fish_ids),
            PondMovement.movement_reason == "faena",
            PondMovement.destiny_pond_id.is_(None),
        )
        .order_by(PondMovement.fish_id.asc(), PondMovement.movement_time.desc())
        .all()
    )
    faena_mov_map: dict[int, PondMovement] = {}
    for mv in faena_movements:
        if mv.fish_id not in faena_mov_map:
            faena_mov_map[mv.fish_id] = mv

    fish_for_pond = []
    for fish in fish_in_faena:
        mv = faena_mov_map.get(fish.id)
        if mv and mv.source_pond_id == source_pond_id:
            fish_for_pond.append(fish)

    fish_ids = [f.id for f in fish_for_pond]
    if not fish_ids:
        return []

    latest_sampling_subq = (
        db.query(
            FishSampling.fish_id,
            func.max(func.coalesce(FishSampling.registry_time, FishSampling.created_at)).label("max_sample_time"),
        )
        .filter(FishSampling.fish_id.in_(fish_ids))
        .group_by(FishSampling.fish_id)
        .subquery()
    )
    latest_samples = (
        db.query(FishSampling)
        .join(
            latest_sampling_subq,
            (latest_sampling_subq.c.fish_id == FishSampling.fish_id)
            & (latest_sampling_subq.c.max_sample_time == func.coalesce(FishSampling.registry_time, FishSampling.created_at)),
        )
        .all()
    )
    samples_map = {s.fish_id: s for s in latest_samples}

    SourcePond = aliased(Pond)
    DestPond = aliased(Pond)
    dep_transition_rows = (
        db.query(PondMovement)
        .join(DestPond, DestPond.id == PondMovement.destiny_pond_id)
        .outerjoin(SourcePond, SourcePond.id == PondMovement.source_pond_id)
        .filter(
            PondMovement.fish_id.in_(fish_ids),
            DestPond.depuration.is_(True),
            or_(
                PondMovement.source_pond_id.is_(None),
                SourcePond.depuration.is_(False),
            ),
        )
        .order_by(PondMovement.fish_id.asc(), PondMovement.movement_time.desc())
        .all()
    )
    dep_entry_map: dict[int, PondMovement] = {}
    for mv in dep_transition_rows:
        if mv.fish_id not in dep_entry_map:
            dep_entry_map[mv.fish_id] = mv

    lot_ids = {f.lot_id for f in fish_for_pond if f.lot_id}
    lots_map = {lot.id: lot for lot in db.query(Lot).filter(Lot.id.in_(lot_ids)).all()} if lot_ids else {}

    drug_rows = (
        db.query(FishDrugUse)
        .filter(FishDrugUse.fish_id.in_(fish_ids))
        .order_by(FishDrugUse.fish_id.asc(), FishDrugUse.application_date.desc(), FishDrugUse.id.desc())
        .all()
    )
    latest_drug_by_fish: dict[int, FishDrugUse] = {}
    for du in drug_rows:
        if du.fish_id not in latest_drug_by_fish:
            latest_drug_by_fish[du.fish_id] = du

    rows = []
    for fish in fish_for_pond:
        mv = faena_mov_map.get(fish.id)
        sample = samples_map.get(fish.id)
        dep_entry = dep_entry_map.get(fish.id)
        dep_days = None
        if dep_entry and dep_entry.movement_time and mv and mv.movement_time:
            dep_days = (mv.movement_time.date() - dep_entry.movement_time.date()).days
            if dep_days < 0:
                dep_days = 0

        weight_g = float(sample.weight) if sample and sample.weight is not None else None
        lot = lots_map.get(fish.lot_id)
        du = latest_drug_by_fish.get(fish.id)
        rows.append(
            {
                "fish_id": fish.id,
                "pit_tag": fish.internal_id or "N/D",
                "lot_id": fish.lot_id,
                # Enviar nombre completo del lote (ej. "Fátima") en lugar de códigos abreviados.
                "lot": (lot.name or lot.internal_id) if lot else "N/D",
                "sex": fish.sex or "N/D",
                "weight_g": weight_g,
                "weight_kg": round(weight_g / 1000.0, 3) if weight_g is not None else None,
                "depuration_days": dep_days,
                "drug_use": du,
            }
        )

    return rows


def _redirect_faena(
    status: str,
    msg: str = "",
    decl_id: Optional[int] = None,
    decl_folio: Optional[str] = None,
    decl_scope: Optional[str] = None,
):
    parts = [f"decl_status={quote_plus(status)}"]
    if msg:
        parts.append(f"decl_msg={quote_plus(msg)}")
    if decl_id is not None:
        parts.append(f"decl_id={decl_id}")
    if decl_folio:
        parts.append(f"decl_folio={quote_plus(decl_folio)}")
    if decl_scope:
        parts.append(f"decl_scope={quote_plus(decl_scope)}")
    return RedirectResponse(url="/views/ui/faena?" + "&".join(parts), status_code=303)


@router.post("/ui/faena/declarations")
def create_cultivation_declaration(
    request: Request,
    manager_name: str = Form(...),
    manager_rut: str = Form(...),
    manager_phone: str = Form(...),
    declaration_date: str = Form(...),
    window_days: int = Form(3),
    scope: str = Form("unit"),
    db: Session = Depends(get_db),
):
    try:
        declaration_dt = date.fromisoformat((declaration_date or "").strip())
    except ValueError:
        return _redirect_faena("error", "Fecha de declaración inválida", decl_scope=scope)

    if window_days <= 0:
        return _redirect_faena("error", "La vigencia debe ser mayor a 0", decl_scope=scope)

    rows_all, jaula_pond_ids, representative_pond_id, jaula_pond_name_map = _jaula_available_rows(db)
    if not rows_all:
        return _redirect_faena("error", "No hay peces disponibles en la unidad Jaula", decl_scope=scope)
    if representative_pond_id is None:
        return _redirect_faena("error", "No se encontró estanque representativo para la unidad Jaula", decl_scope=scope)

    normalized_scope = (scope or "unit").strip().lower()
    selected_scope = "unit"
    selected_pond_id: Optional[int] = None
    if normalized_scope != "unit":
        if not normalized_scope.startswith("pond:"):
            return _redirect_faena("error", "Alcance de declaración inválido", decl_scope="unit")
        pond_raw = normalized_scope.split(":", 1)[1].strip()
        try:
            selected_pond_id = int(pond_raw)
        except ValueError:
            return _redirect_faena("error", "Estanque de alcance inválido", decl_scope="unit")
        if selected_pond_id not in jaula_pond_ids:
            return _redirect_faena("error", "El estanque seleccionado no pertenece a la unidad Jaula", decl_scope="unit")
        selected_scope = f"pond:{selected_pond_id}"

    if selected_pond_id is None:
        rows = rows_all
        source_pond_id_for_declaration = representative_pond_id
        declaration_scope_payload = {
            "type": "cultivation_unit",
            "name": "Jaula",
            "pond_ids": jaula_pond_ids,
        }
    else:
        rows = [row for row in rows_all if row.get("source_pond_id") == selected_pond_id]
        source_pond_id_for_declaration = selected_pond_id
        declaration_scope_payload = {
            "type": "pond",
            "name": jaula_pond_name_map.get(selected_pond_id) or f"Estanque {selected_pond_id}",
            "pond_ids": [selected_pond_id],
        }

    if not rows:
        return _redirect_faena(
            "error",
            "No hay peces disponibles para el alcance seleccionado",
            decl_scope=selected_scope,
        )

    latest_sanitary = db.query(SanitaryReport).order_by(SanitaryReport.report_date.desc()).first()
    sanitary_expiry = (latest_sanitary.report_date + timedelta(days=365)) if latest_sanitary and latest_sanitary.report_date else None
    sanitary_check_ok = bool(latest_sanitary and sanitary_expiry and declaration_dt <= sanitary_expiry)

    depuration_check_ok = True

    lot_ids = {r["lot_id"] for r in rows if r.get("lot_id")}
    lots_map = {l.id: l for l in db.query(Lot).filter(Lot.id.in_(lot_ids)).all()} if lot_ids else {}
    species_map = {s.id: s.name for s in db.query(Species).all()}
    species_names = set()
    source_pond_counts: dict[int, int] = {}
    species_counts: dict[str, int] = {}

    items_payload = []
    drug_check_ok = True
    for r in rows:
        lot = lots_map.get(r["lot_id"])
        fish_species_id = lot.species_id if lot and lot.species_id else None
        fish_species_name = species_map.get(fish_species_id) if fish_species_id else None
        if lot and lot.species_id and species_map.get(lot.species_id):
            species_names.add(species_map[lot.species_id])

        source_pond_id = r.get("source_pond_id")
        if source_pond_id:
            source_pond_counts[source_pond_id] = source_pond_counts.get(source_pond_id, 0) + 1
        if fish_species_name:
            species_counts[fish_species_name] = species_counts.get(fish_species_name, 0) + 1

        du = r["drug_use"]
        withdrawal_end_date = None
        if du and du.application_date:
            wd = _parse_withdrawal_days(du.withdrawal_period)
            if wd is None:
                drug_check_ok = False
            else:
                withdrawal_end_date = du.application_date.date() + timedelta(days=wd)
                if withdrawal_end_date > declaration_dt:
                    drug_check_ok = False

        items_payload.append(
            {
                "fish_id": r["fish_id"],
                "pit_tag": r["pit_tag"],
                "weight_g": r["weight_g"],
                "caviar_diameter_mm": r.get("caviar_diameter_mm"),
                "missing_weight": r.get("missing_weight", False),
                "source_pond": {
                    "id": source_pond_id,
                    "name": r.get("source_pond_name") or jaula_pond_name_map.get(source_pond_id) or f"Estanque {source_pond_id}",
                }
                if source_pond_id
                else None,
                "species": fish_species_name,
                "species_id": fish_species_id,
                "lot": r["lot"],
                "depuration_days": r["depuration_days"],
                "sex": r["sex"],
                "has_drug_use": bool(du),
                "withdrawal_end_date": withdrawal_end_date.isoformat() if withdrawal_end_date else None,
                "drug_name": du.drug_name if du else None,
                "withdrawal_period": du.withdrawal_period if du else None,
                "application_date": du.application_date.date().isoformat() if du and du.application_date else None,
            }
        )

    fish_without_weight_count = sum(1 for r in rows if r.get("missing_weight"))
    source_pond = db.query(Pond).filter(Pond.id == source_pond_id_for_declaration).first()
    if not source_pond:
        return _redirect_faena("error", "Estanque de alcance no existe", decl_scope=selected_scope)

    source_ponds_payload = [
        {
            "id": pond_id,
            "name": jaula_pond_name_map.get(pond_id) or f"Estanque {pond_id}",
            "fish_count": count,
        }
        for pond_id, count in sorted(
            source_pond_counts.items(),
            key=lambda item: (str(jaula_pond_name_map.get(item[0]) or ""), item[0]),
        )
    ]
    species_breakdown_payload = [
        {"name": name, "fish_count": count}
        for name, count in sorted(species_counts.items(), key=lambda item: item[0])
    ]

    unit = db.query(CultivationUnit).order_by(CultivationUnit.id.asc()).first()
    unit_name = unit.name if unit and unit.name else "Centro de Cultivo La Casona"

    now = datetime.utcnow()
    valid_until = declaration_dt + timedelta(days=window_days)
    total_weight_kg = round(sum((r["weight_g"] or 0.0) / 1000.0 for r in rows), 3)
    folio = f"DC-{declaration_dt.strftime('%Y%m%d')}-JAULA-{int(now.timestamp())}"

    payload = {
        "folio": folio,
        "declaration_date": declaration_dt.isoformat(),
        "validity_days": window_days,
        "valid_until": valid_until.isoformat(),
        "status": "sent",
        "cultivation_unit": {
            "name": unit_name,
            "manager_name": manager_name.strip(),
            "manager_rut": manager_rut.strip(),
            "manager_phone": manager_phone.strip(),
        },
        "scope": {
            "type": declaration_scope_payload["type"],
            "name": declaration_scope_payload["name"],
            "pond_ids": declaration_scope_payload["pond_ids"],
        },
        "source_pond": {"id": source_pond.id, "name": source_pond.name},
        "source_ponds": source_ponds_payload,
        "species": ", ".join(sorted(species_names)) if species_names else None,
        "species_breakdown": species_breakdown_payload,
        "totals": {
            "total_fish": len(rows),
            "total_weight_kg": total_weight_kg,
        },
        "data_quality": {
            "fish_without_weight_count": fish_without_weight_count,
        },
        "checks": {
            "sanitary_report": {
                "ok": sanitary_check_ok,
                "report_number": latest_sanitary.report_number if latest_sanitary else None,
                "report_date": latest_sanitary.report_date.isoformat() if latest_sanitary and latest_sanitary.report_date else None,
                "expiry_date": sanitary_expiry.isoformat() if sanitary_expiry else None,
            },
            "drug_use": {"ok": drug_check_ok},
            "depuration_days": {"ok": depuration_check_ok, "minimum_required_days": 3},
        },
        "fish": items_payload,
    }

    declaration = CultivationDeclaration(
        folio=folio,
        source_pond_id=source_pond.id,
        cultivation_unit_name=unit_name,
        manager_name=manager_name.strip(),
        manager_rut=manager_rut.strip(),
        manager_phone=manager_phone.strip(),
        declaration_date=declaration_dt,
        validity_days=window_days,
        valid_until=valid_until,
        species_name=payload["species"],
        total_fish=len(rows),
        total_weight_kg=Decimal(str(total_weight_kg)),
        sanitary_report_number=latest_sanitary.report_number if latest_sanitary else None,
        sanitary_report_date=latest_sanitary.report_date if latest_sanitary else None,
        sanitary_check_ok=sanitary_check_ok,
        drug_check_ok=drug_check_ok,
        depuration_check_ok=depuration_check_ok,
        status="sent",
        payload=payload,
        created_at=now,
        updated_at=now,
    )
    db.add(declaration)
    db.flush()

    for item in items_payload:
        db.add(
            CultivationDeclarationItem(
                declaration_id=declaration.id,
                fish_id=item["fish_id"],
                pit_tag=item["pit_tag"],
                lot_label=item["lot"],
                sex=item["sex"],
                weight_g=Decimal(str(item["weight_g"])) if item["weight_g"] is not None else None,
                depuration_days=item["depuration_days"],
                has_drug_use=item["has_drug_use"],
                withdrawal_end_date=date.fromisoformat(item["withdrawal_end_date"]) if item["withdrawal_end_date"] else None,
                created_at=now,
                updated_at=now,
            )
        )

    db.commit()

    delivery_note = ""
    delivery_result = None
    try:
        payload_for_delivery = dict(payload)
        payload_for_delivery["declaration_id"] = declaration.id
        resolved_target_url = _resolve_planta_import_url(request)
        delivery_result = _send_declaration_json_to_planta(payload_for_delivery, target_url=resolved_target_url)

        payload_db = dict(declaration.payload or {})
        payload_db["planta_delivery"] = {
            "attempted_at": datetime.utcnow().isoformat(),
            "target_url": resolved_target_url,
            "ok": bool(delivery_result.get("ok")),
            "status_code": delivery_result.get("status_code"),
            "response": delivery_result.get("response"),
        }
        declaration.payload = payload_db
        declaration.updated_at = datetime.utcnow()
        db.commit()

        if delivery_result.get("ok"):
            batch_id = (delivery_result.get("response") or {}).get("batch_id")
            if batch_id:
                delivery_note = f"JSON enviado a PlantaApp y recibido correctamente (batch {batch_id})."
            else:
                delivery_note = "JSON enviado a PlantaApp y recibido correctamente."
        else:
            resp = delivery_result.get("response") or {}
            detail = (
                resp.get("detail")
                or resp.get("message")
                or resp.get("raw_error")
                or "sin detalle"
            )
            delivery_note = (
                f"Folio generado, pero PlantaApp respondió error de integración "
                f"({delivery_result.get('status_code')}): {detail}."
            )
    except Exception as exc:
        payload_db = dict(declaration.payload or {})
        payload_db["planta_delivery"] = {
            "attempted_at": datetime.utcnow().isoformat(),
            "target_url": _resolve_planta_import_url(request),
            "ok": False,
            "status_code": None,
            "response": {"error": str(exc)},
        }
        declaration.payload = payload_db
        declaration.updated_at = datetime.utcnow()
        db.commit()
        delivery_note = f"Folio generado, pero no se pudo enviar a PlantaApp: {str(exc)}"

    return _redirect_faena(
        "ok",
        msg=delivery_note,
        decl_id=declaration.id,
        decl_folio=folio,
        decl_scope=selected_scope,
    )


@router.get("/ui/cultivation-declarations/{declaration_id}/json", response_class=JSONResponse)
def get_cultivation_declaration_json(declaration_id: int, db: Session = Depends(get_db)):
    declaration = db.query(CultivationDeclaration).filter(CultivationDeclaration.id == declaration_id).first()
    if not declaration:
        raise HTTPException(status_code=404, detail="Declaración no encontrada")

    payload = dict(declaration.payload or {})
    payload["declaration_id"] = declaration.id
    payload["status"] = declaration.status
    payload["confirmed_at"] = declaration.confirmed_at.isoformat() if declaration.confirmed_at else None
    payload["confirmed_by"] = declaration.confirmed_by
    return JSONResponse(payload)


@router.get("/ui/cultivation-declarations/{declaration_id}", response_class=HTMLResponse)
def get_cultivation_declaration_detail(
    request: Request,
    declaration_id: int,
    db: Session = Depends(get_db),
):
    declaration = db.query(CultivationDeclaration).filter(CultivationDeclaration.id == declaration_id).first()
    if not declaration:
        raise HTTPException(status_code=404, detail="Declaración no encontrada")

    payload = dict(declaration.payload or {})
    fish_rows = payload.get("fish") or []
    fish_rows = sorted(
        fish_rows,
        key=lambda r: (
            str(r.get("lot") or ""),
            str(r.get("pit_tag") or ""),
        ),
    )
    fish_without_weight_count = sum(1 for r in fish_rows if r.get("missing_weight"))

    context = {
        "request": request,
        "declaration": declaration,
        "payload": payload,
        "fish_rows": fish_rows,
        "total_fish": len(fish_rows),
        "fish_without_weight_count": fish_without_weight_count,
    }
    return templates.TemplateResponse(request, "cultivation_declaration_detail.html", context)


@router.post("/ui/cultivation-declarations/{declaration_id}/confirm")
def confirm_cultivation_declaration(
    declaration_id: int,
    confirmed_by: str = Form("APP-FAENA"),
    db: Session = Depends(get_db),
):
    declaration = db.query(CultivationDeclaration).filter(CultivationDeclaration.id == declaration_id).first()
    if not declaration:
        raise HTTPException(status_code=404, detail="Declaración no encontrada")

    if declaration.status != "confirmed":
        declaration.status = "confirmed"
        declaration.confirmed_at = datetime.utcnow()
        declaration.confirmed_by = (confirmed_by or "APP-FAENA").strip()
        declaration.updated_at = declaration.confirmed_at
        db.commit()

    return _redirect_faena("confirmed", decl_id=declaration.id, decl_folio=declaration.folio)


@router.post("/api/integration/v1/cultivation-declarations/{declaration_id}/received-fish", response_class=JSONResponse)
def receive_faena_pit_tags(
    declaration_id: int,
    payload: dict = Body(...),
    db: Session = Depends(get_db),
):
    declaration = db.query(CultivationDeclaration).filter(CultivationDeclaration.id == declaration_id).first()
    if not declaration:
        raise HTTPException(status_code=404, detail="Declaración no encontrada")

    pit_tags = payload.get("pit_tags")
    if not isinstance(pit_tags, list) or not pit_tags:
        raise HTTPException(status_code=422, detail="pit_tags debe ser una lista no vacía")

    normalized = []
    seen = set()
    for raw in pit_tags:
        tag = str(raw or "").strip().upper()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        normalized.append(tag)

    if not normalized:
        raise HTTPException(status_code=422, detail="No hay PIT tags válidos para procesar")

    declaration_items = (
        db.query(CultivationDeclarationItem)
        .filter(CultivationDeclarationItem.declaration_id == declaration_id)
        .all()
    )
    item_by_tag = {str(i.pit_tag or "").strip().upper(): i for i in declaration_items}

    now = datetime.utcnow()
    moved = []
    skipped = []
    unknown = []

    for tag in normalized:
        item = item_by_tag.get(tag)
        if not item:
            unknown.append(tag)
            continue

        fish = None
        if item.fish_id:
            fish = db.query(Fish).filter(Fish.id == item.fish_id).first()
        if not fish:
            fish = db.query(Fish).filter(func.upper(Fish.internal_id) == tag).first()
        if not fish:
            skipped.append({"pit_tag": tag, "reason": "fish_not_found"})
            continue

        if fish.state == "faena":
            skipped.append({"pit_tag": tag, "reason": "already_faena"})
            continue

        latest_mv = (
            db.query(PondMovement)
            .filter(PondMovement.fish_id == fish.id)
            .order_by(PondMovement.movement_time.desc(), PondMovement.id.desc())
            .first()
        )
        source_pond_id = latest_mv.destiny_pond_id if latest_mv else None

        db.add(
            PondMovement(
                source_pond_id=source_pond_id,
                destiny_pond_id=None,
                fish_id=fish.id,
                lot_id=fish.lot_id,
                fish_quantity=1,
                movement_reason="faena",
                movement_time=now,
                created_at=now,
                updated_at=now,
            )
        )

        fish.state = "faena"
        fish.updated_at = now
        moved.append(tag)

    declaration_payload = dict(declaration.payload or {})
    declaration_payload["faena_received"] = {
        "received_at": now.isoformat(),
        "received_by": (payload.get("received_by") or "APP-FAENA"),
        "received_pit_tags": moved,
        "skipped": skipped,
        "unknown": unknown,
    }

    total_decl_tags = len(item_by_tag)
    moved_or_already = len(moved) + sum(1 for x in skipped if x.get("reason") == "already_faena")
    declaration.status = "received_complete" if total_decl_tags and moved_or_already >= total_decl_tags else "received_partial"
    declaration.payload = declaration_payload
    declaration.updated_at = now

    db.commit()

    return JSONResponse(
        {
            "ok": True,
            "declaration_id": declaration.id,
            "status": declaration.status,
            "moved_to_faena": moved,
            "skipped": skipped,
            "unknown": unknown,
        }
    )
