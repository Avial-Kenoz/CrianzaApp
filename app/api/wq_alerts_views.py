"""Vistas de monitoreo de las alertas de calidad de agua.

Convención de CrianzaApp: páginas HTML bajo /views/ui/* renderizadas con
jinja_env + HTMLResponse, incluyendo _sidebar.html. Sin capa de auth.

Tres pestañas y cada una responde una pregunta distinta:

  - **Alertas** — qué está pasando ahora y, sobre todo, **cuánto ruido haría el
    canal si estuviera encendido**. Esa es la razón de que esta vista exista
    antes que el envío: las primeras cuatro corridas del motor abrieron ~20
    alertas por día, casi todas de ronda vencida. Mandar eso por Telegram
    habría quemado el canal en una semana. Acá se ve el volumen por día y por
    tipo antes de encender nada.
  - **Reglas** — la perilla contra ese ruido, editable sin tocar código.
  - **Destinatarios** — a quién le llega, con sus filtros.

El reconocimiento desde acá es el del **supervisor**: deja constancia de que
alguien se hizo cargo y frena el escalamiento. No toca `acknowledged` de la
lectura, que es el gesto del **operador** en terreno (re-ingreso + acción
correctiva) y significa otra cosa.
"""

from fastapi import APIRouter, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import quote_plus

from app.db.session import SessionLocal
from app.models.cultivation_units import CultivationUnit
from app.models.ponds import Pond
from app.models.users import User
from app.models.water_quality_alerts import (
    WaterQualityAlert,
    WaterQualityAlertNotification,
    WaterQualityAlertRecipient,
    WaterQualityAlertRule,
)
from app.services import water_quality as wq
from app.services import wq_alerts as al
from app.services.wq_queries import load_thresholds

router = APIRouter(prefix="/views/ui/calidad-agua/alertas", tags=["calidad-agua-alertas"])
template_dir = Path(__file__).parent.parent / "templates"
# autoescape: el detalle de una alerta puede traer texto del operador y el
# título lleva "<" en los umbrales ("< 70%").
jinja_env = Environment(loader=FileSystemLoader(str(template_dir)),
                        autoescape=select_autoescape(["html"]))

KIND_LABELS = {
    al.KIND_LECTURA: "Lectura fuera de rango",
    al.KIND_RONDA: "Ronda sin tomar",
    al.KIND_SIN_ACK: "Alarma sin reconocer",
}
CLOSE_LABELS = {
    "resuelta": "se resolvió sola",
    "reconocida": "la reconocieron",
    "caducada": "caducó",
    "manual": "cierre manual",
}
DIAS_VOLUMEN = 7


def _dur(desde: datetime, hasta: Optional[datetime]) -> str:
    """Duración en la unidad que se lee de un vistazo, no en minutos siempre."""
    if desde is None:
        return "—"
    total = int(((hasta or datetime.now()) - desde).total_seconds() // 60)
    if total < 60:
        return "{} min".format(total)
    if total < 48 * 60:
        return "{} h {:02d} min".format(total // 60, total % 60)
    return "{} d".format(total // (24 * 60))


def _lugar(a, ponds: dict, units: dict) -> str:
    if a.pond_id and a.pond_id in ponds:
        return ponds[a.pond_id]
    if a.cultivation_unit_id and a.cultivation_unit_id in units:
        return units[a.cultivation_unit_id]
    return "—"


def _fila(a, ponds, units, envios: dict) -> dict:
    return {
        "id": a.id,
        "kind": a.kind,
        "kind_label": KIND_LABELS.get(a.kind, a.kind),
        "level": a.level,
        "state": a.state,
        "title": a.title,
        "detail": a.detail or "",
        "lugar": _lugar(a, ponds, units),
        "opened_at": a.opened_at,
        "opened_txt": a.opened_at.strftime("%d-%m %H:%M") if a.opened_at else "—",
        "last_seen_txt": a.last_seen_at.strftime("%d-%m %H:%M") if a.last_seen_at else "—",
        "closed_txt": a.closed_at.strftime("%d-%m %H:%M") if a.closed_at else None,
        "dur": _dur(a.opened_at, a.closed_at),
        "close_label": CLOSE_LABELS.get(a.close_reason, a.close_reason or ""),
        "ack_note": a.ack_note,
        "acked_txt": a.acked_at.strftime("%d-%m %H:%M") if a.acked_at else None,
        "envios": envios.get(a.id, 0),
    }


def _turno_de(cuando: datetime, thresholds: Optional[dict] = None) -> str:
    """En qué turno cayó un momento: "dia" u "off".

    Misma ventana con que el módulo mide todo lo demás (`o2_day_window`, por
    defecto 08:30-16:00) y misma excepción: el domingo es `off` completo. Si el
    corte del consolidado usara otro horario que el de las rondas, el mensaje
    de la mañana llegaría partido respecto del turno que resume.
    """
    win = (thresholds or {}).get("o2_day_window") or wq.O2_DAY_WINDOW
    if cuando.weekday() == 6:
        return "off"
    h = cuando.hour + cuando.minute / 60.0
    return "dia" if (win["alert"] <= h < win["alarm"]) else "off"


def _volumen(db, rules: dict, now: datetime) -> dict:
    """Cuántas alertas por día y por tipo, y cuántos avisos habrían salido.

    El segundo número es el que importa antes de encender el canal: no es lo
    mismo "el motor detectó 20 cosas" que "a alguien le vibró el teléfono 20
    veces". Entre una cosa y la otra hay cuatro compuertas —tipo apagado, nivel
    mínimo, atraso mínimo y resumen diario— y aplicarlas sobre el histórico real
    es la única forma de saber el volumen ANTES de encender nada.

    El consolidado del turno cuenta como **un** mensaje por turno con algo que
    contar, no como uno por alerta: de eso se trata.
    """
    desde = (now - timedelta(days=DIAS_VOLUMEN - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    thresholds = load_thresholds(db)
    filas: dict = {}
    turnos: dict = {}
    for a in (db.query(WaterQualityAlert)
                .filter(WaterQualityAlert.opened_at >= desde).all()):
        dia = a.opened_at.strftime("%d-%m")
        f = filas.setdefault(dia, {"dia": dia, "total": 0, "avisos": 0,
                                   "suenan": 0, "digest": 0,
                                   al.KIND_LECTURA: 0, al.KIND_RONDA: 0,
                                   al.KIND_SIN_ACK: 0})
        f[a.kind] = f.get(a.kind, 0) + 1
        f["total"] += 1

        r = rules.get(a.kind)
        if r is not None and not r.enabled:
            continue
        if r is not None and al.NIVEL.get(a.level, 0) < al.NIVEL.get(r.min_level, 2):
            continue

        instante = al.NIVEL.get(r.instant_level, 2) if r is not None else 2
        if al.NIVEL.get(a.level, 0) >= instante:
            f["suenan"] += 1
            f["avisos"] += 1
        else:
            # Al consolidado del turno en que ocurrió: el mensaje sale cuando
            # cambia la gente, no una vez por alerta.
            turnos.setdefault(dia, set()).add(_turno_de(a.opened_at, thresholds))

    for dia, ts in turnos.items():
        if dia in filas:
            filas[dia]["digest"] = len(ts)
            filas[dia]["avisos"] += len(ts)
    orden = sorted(filas.values(), key=lambda f: f["dia"])
    avisos = sum(f["avisos"] for f in orden)
    por_dia = round(avisos / max(len(orden), 1), 1)
    return {
        "filas": orden,
        "dias": DIAS_VOLUMEN,
        "total": sum(f["total"] for f in orden),
        "avisos": avisos,
        "por_dia": por_dia,
        "por_dia_txt": ("%.1f" % por_dia).replace(".", ","),
    }


def _tabs(activa: str) -> list:
    return [
        {"href": "/views/ui/calidad-agua/alertas", "label": "Alertas",
         "activa": activa == "alertas"},
        {"href": "/views/ui/calidad-agua/alertas/reglas", "label": "Reglas",
         "activa": activa == "reglas"},
        {"href": "/views/ui/calidad-agua/alertas/destinatarios",
         "label": "Destinatarios", "activa": activa == "destinatarios"},
    ]


# ---------------------------------------------------------------------------
# Monitoreo
# ---------------------------------------------------------------------------
@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def alertas(request: Request, msg: Optional[str] = None,
            historial: int = 60):
    db = SessionLocal()
    try:
        now = datetime.now()
        rules = al.load_rules(db)
        ponds = {p.id: p.name.split(" - ")[0].strip()
                 for p in db.query(Pond.id, Pond.name).all()}
        units = {u.id: u.name for u in db.query(CultivationUnit.id,
                                                CultivationUnit.name).all()}

        abiertas_q = (db.query(WaterQualityAlert)
                        .filter(WaterQualityAlert.closed_at.is_(None))
                        .all())
        # Lo más grave primero y, dentro del mismo nivel, lo más antiguo: una
        # alarma de hace tres horas pesa más que una de hace diez minutos.
        abiertas_q.sort(key=lambda a: (-al.NIVEL.get(a.level, 0), a.opened_at))

        cerradas_q = (db.query(WaterQualityAlert)
                        .filter(WaterQualityAlert.closed_at.isnot(None))
                        .order_by(WaterQualityAlert.closed_at.desc())
                        .limit(historial).all())

        ids = [a.id for a in abiertas_q] + [a.id for a in cerradas_q]
        envios: dict = {}
        if ids:
            for n in (db.query(WaterQualityAlertNotification)
                        .filter(WaterQualityAlertNotification.alert_id.in_(ids))
                        .all()):
                envios[n.alert_id] = envios.get(n.alert_id, 0) + 1

        abiertas = [_fila(a, ponds, units, envios) for a in abiertas_q]
        cerradas = [_fila(a, ponds, units, envios) for a in cerradas_q]

        destinatarios_activos = (db.query(WaterQualityAlertRecipient)
                                   .filter(WaterQualityAlertRecipient.active.is_(True))
                                   .count())
        total_envios = db.query(WaterQualityAlertNotification).count()

        html = jinja_env.get_template("calidad_agua_alertas.html").render(
            request=request, msg=msg, now=now,
            tabs=_tabs("alertas"),
            abiertas=abiertas, cerradas=cerradas,
            n_alarma=sum(1 for a in abiertas if a["level"] == "alarma"),
            n_alerta=sum(1 for a in abiertas if a["level"] == "alerta"),
            n_sin_ack=sum(1 for a in abiertas if a["state"] == "abierta"
                          and a["level"] == "alarma"),
            volumen=_volumen(db, rules, now),
            destinatarios_activos=destinatarios_activos,
            total_envios=total_envios,
            kind_labels=KIND_LABELS,
        )
        return HTMLResponse(html)
    finally:
        db.close()


@router.post("/{alert_id}/reconocer")
def reconocer(alert_id: int, nota: Optional[str] = Form(None)):
    """El supervisor se hace cargo: frena el escalamiento sin cerrar la alerta.

    No cierra: la condición sigue viva hasta que el dato diga lo contrario (una
    lectura nueva en rango, o la ronda al fin tomada). Lo único que cambia es
    que ya hay alguien encima, y eso basta para dejar de insistir.
    """
    db = SessionLocal()
    try:
        a = db.get(WaterQualityAlert, alert_id)
        if a is None:
            return RedirectResponse(
                url="/views/ui/calidad-agua/alertas?msg=" +
                    quote_plus("Esa alerta ya no existe."), status_code=303)
        now = datetime.now()
        if a.closed_at is None:
            a.state = "reconocida"
            a.acked_at = now
            a.ack_note = (nota or "").strip()[:160] or None
            a.updated_at = now
            db.commit()
            texto = "Alerta #{} reconocida.".format(alert_id)
        else:
            texto = "Alerta #{} ya estaba cerrada.".format(alert_id)
        return RedirectResponse(
            url="/views/ui/calidad-agua/alertas?msg=" + quote_plus(texto),
            status_code=303)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Reglas
# ---------------------------------------------------------------------------
@router.get("/reglas", response_class=HTMLResponse)
def reglas_form(request: Request, msg: Optional[str] = None):
    db = SessionLocal()
    try:
        rules = (db.query(WaterQualityAlertRule)
                   .order_by(WaterQualityAlertRule.id).all())
        html = jinja_env.get_template("calidad_agua_alertas_reglas.html").render(
            request=request, msg=msg, tabs=_tabs("reglas"), reglas=rules,
            kind_labels=KIND_LABELS)
        return HTMLResponse(html)
    finally:
        db.close()


def _int_o_none(v) -> Optional[int]:
    try:
        s = str(v).strip().replace(",", ".")
        return int(float(s)) if s else None
    except (TypeError, ValueError):
        return None


def _float_o_none(v) -> Optional[float]:
    try:
        s = str(v).strip().replace(",", ".")
        return float(s) if s else None
    except (TypeError, ValueError):
        return None


@router.post("/reglas")
async def reglas_save(request: Request):
    db = SessionLocal()
    try:
        form = await request.form()
        now = datetime.now()
        for r in db.query(WaterQualityAlertRule).all():
            k = r.kind
            r.enabled = bool(form.get("enabled_" + k))
            r.min_level = (form.get("min_level_" + k) or "alarma")
            cd = _int_o_none(form.get("cooldown_" + k))
            # El cooldown es el piso entre dos avisos de la MISMA alerta: sin
            # él, una condición que persiste manda un mensaje por pasada del
            # motor, o sea uno cada cinco minutos.
            r.cooldown_min = cd if cd and cd > 0 else 1
            r.renotify_min = _int_o_none(form.get("renotify_" + k))
            r.quiet_from = _float_o_none(form.get("quiet_from_" + k))
            r.quiet_to = _float_o_none(form.get("quiet_to_" + k))
            r.escalate_after_min = _int_o_none(form.get("escalate_" + k))
            r.notify_after_min = _int_o_none(form.get("notify_after_" + k))
            r.instant_level = (form.get("instant_level_" + k) or "alarma")
            r.updated_at = now
        db.commit()
        return RedirectResponse(
            url="/views/ui/calidad-agua/alertas/reglas?msg=" +
                quote_plus("Reglas guardadas. Rigen desde la próxima pasada del motor."),
            status_code=303)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Destinatarios
# ---------------------------------------------------------------------------
@router.get("/destinatarios", response_class=HTMLResponse)
def destinatarios_form(request: Request, msg: Optional[str] = None):
    db = SessionLocal()
    try:
        dest = (db.query(WaterQualityAlertRecipient)
                  .order_by(WaterQualityAlertRecipient.id).all())
        units = db.query(CultivationUnit).order_by(CultivationUnit.name).all()
        envios = {}
        for d in dest:
            envios[d.id] = (db.query(WaterQualityAlertNotification)
                              .filter(WaterQualityAlertNotification.recipient_id == d.id)
                              .count())
        html = jinja_env.get_template("calidad_agua_alertas_destinatarios.html").render(
            request=request, msg=msg, tabs=_tabs("destinatarios"),
            destinatarios=dest, units=units, envios=envios,
            kind_labels=KIND_LABELS)
        return HTMLResponse(html)
    finally:
        db.close()


@router.post("/destinatarios")
async def destinatarios_save(request: Request):
    """Guarda los existentes y, si viene, da de alta uno nuevo."""
    db = SessionLocal()
    try:
        form = await request.form()
        now = datetime.now()
        for d in db.query(WaterQualityAlertRecipient).all():
            # No hay borrar, sólo desactivar: la bitácora de envíos referencia
            # al destinatario y el historial tiene que seguir diciendo a quién
            # se avisó anoche.
            suf = "_{}".format(d.id)
            d.name = (form.get("name" + suf) or d.name).strip()[:120]
            d.telegram_chat_id = (form.get("chat" + suf) or d.telegram_chat_id).strip()[:40]
            d.active = bool(form.get("active" + suf))
            d.min_level = form.get("min_level" + suf) or "alarma"
            d.kinds = (form.get("kinds" + suf) or "").strip()[:120] or None
            d.unit_ids = (form.get("units" + suf) or "").strip()[:120] or None
            d.hours_from = _float_o_none(form.get("hours_from" + suf))
            d.hours_to = _float_o_none(form.get("hours_to" + suf))
            d.note = (form.get("note" + suf) or "").strip()[:200] or None
            d.updated_at = now

        texto = "Destinatarios guardados."
        nombre = (form.get("nuevo_name") or "").strip()
        chat = (form.get("nuevo_chat") or "").strip()
        if nombre and chat:
            ya = (db.query(WaterQualityAlertRecipient)
                    .filter(WaterQualityAlertRecipient.telegram_chat_id == chat)
                    .first())
            if ya is not None:
                # Ese chat ya tenía dueño. Se reactiva —el caso real es "volvió
                # el encargado que habíamos sacado"— pero NO se renombra: quien
                # creía estar agregando a alguien habría renombrado a otro sin
                # enterarse, y los avisos seguirían yendo al teléfono de antes.
                ya.active = True
                ya.updated_at = now
                texto = ("Ese chat ya estaba registrado a nombre de {}: se "
                         "reactivó sin cambiarle el nombre.").format(ya.name)
            else:
                db.add(WaterQualityAlertRecipient(
                    name=nombre[:120], telegram_chat_id=chat[:40], active=True,
                    min_level=form.get("nuevo_min_level") or "alarma",
                    note=(form.get("nuevo_note") or "").strip()[:200] or None,
                    created_at=now, updated_at=now))
        db.commit()
        return RedirectResponse(
            url="/views/ui/calidad-agua/alertas/destinatarios?msg=" + quote_plus(texto),
            status_code=303)
    finally:
        db.close()
