"""Inventario en terreno: el tarro de cada carril como bodega intermedia.

Saldo de un carril y un alimento =
    salidas de bodega (cargan el tarro)
  − ración del día (alimentado: kg_fed · no alimentado: 0 · programado: kg_planned)
  ± ajustes de los conteos (FeedTerrenoMove, cada uno con su motivo)

Se cuenta desde terreno_since (la primera salida de bodega desde que existe la
ración diaria): antes no hay registro de lo que se echó al agua.

Un tarro tiene un alimento a la vez. Si quedan dos con saldo, es alerta: la
mezcla con receta llega en una etapa posterior. La edad del alimento se lee por
cargas, gastando primero la más antigua: así una carga vieja llega a cero
aunque se rellene encima (sin la paradoja del promedio).

Un conteo nunca pisa el papel: la diferencia queda como uno o más ajustes con
motivo, y los que tocan la bodega principal dejan su movimiento en el libro de
stock (feed_stock_ledger) en sacos enteros.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.cultivation_units import CultivationUnit
from app.models.feed import (FeedExecutionEvent, FeedInventoryAdjustment, FeedMovementType,
                             FeedRation, FeedStockLedger, FeedType)
from app.models.feed_terreno import FeedTarro, FeedTerrenoCount, FeedTerrenoMove
from app.models.ponds import Pond
from app.services import feed_ration as fr

DEFAULT_TOPE_KG = 150.0      # tarro de ~150 L; se ajusta por carril
COBERTURA_MAX_DIAS = 15      # más que esto en el tarro es raro
CARGA_VIEJA_DIAS = 30        # alimento abierto hace más de esto
RITMO_DIAS = 7               # ritmo = promedio de los últimos 7 días (incluye ayunos)
TOL_KG = 0.05

# sign: −1 hay menos de lo que dice el papel · +1 hay más.
# bodega: efecto en la bodega principal por cada saco (+1 vuelve, −1 sale).
MOTIVOS = {
    "salida_de_mas": {"label": "Salida registrada de más (error de ingreso)", "sign": -1, "bodega": +1},
    "devuelto_bodega": {"label": "Devuelto a bodega", "sign": -1, "bodega": +1},
    "dado_sin_registrar": {"label": "Alimento dado sin registrar en la ración", "sign": -1, "fecha": True},
    "traspaso": {"label": "Pasado a otro carril", "sign": -1, "destino": True},
    "merma": {"label": "Merma (humedad, hongos, roedores, derrame)", "sign": -1},
    "salida_no_registrada": {"label": "Salida de bodega no registrada", "sign": +1, "bodega": -1},
    "racion_de_mas": {"label": "Ración registrada de más (se dio menos)", "sign": +1},
    "saldo_previo": {"label": "Saldo anterior al sistema", "sign": +1, "solo_inicial": True},
}
TRASPASO_RECIBIDO = "traspaso_recibido"
MOTIVO_LABEL = dict({k: v["label"] for k, v in MOTIVOS.items()},
                    **{TRASPASO_RECIBIDO: "Recibido de otro carril"})


def motivos_para(inicial: bool) -> list:
    return [dict(v, key=k) for k, v in MOTIVOS.items() if inicial or not v.get("solo_inicial")]


# ---------------------------------------------------------------------------
# Movimientos y saldo
# ---------------------------------------------------------------------------
def _carril_of(db: Session) -> dict:
    """{pond_id: carril} — un hijo cuenta en el tarro del padre."""
    return {pid: (parent or pid) for pid, parent in db.query(Pond.id, Pond.parent_pond_id).all()}


def start_date(db: Session) -> Optional[date]:
    return fr.terreno_since(db)


def events(db: Session, since: Optional[date] = None) -> dict:
    """{(carril, feed_type_id): [evento]} ordenado por fecha.

    evento = {fecha, tipo: salida|racion|ajuste, kg (con signo), estimado,
              motivo, id}. La ración de un día programado y sin confirmar
    cuenta como estimada (kg_planned).
    """
    since = since or start_date(db)
    out: dict = defaultdict(list)
    if since is None:
        return out
    carril = _carril_of(db)
    for ev in (db.query(FeedExecutionEvent)
               .filter(func.date(FeedExecutionEvent.confirmed_at) >= since).all()):
        out[(carril.get(ev.pond_id, ev.pond_id), ev.feed_type_id)].append({
            "fecha": ev.confirmed_at.date(), "tipo": "salida", "kg": float(ev.confirmed_kg or 0),
            "estimado": False, "motivo": None, "id": ev.id})
    for r in db.query(FeedRation).filter(FeedRation.ration_date >= since).all():
        if r.status == "alimentado":
            kg, est = float(r.kg_fed or 0), False
        elif r.status == "programado":
            kg, est = float(r.kg_planned or 0), True
        else:
            continue
        if kg:
            out[(carril.get(r.pond_id, r.pond_id), r.feed_type_id)].append({
                "fecha": r.ration_date, "tipo": "racion", "kg": -kg,
                "estimado": est, "motivo": None, "id": r.id})
    for m in db.query(FeedTerrenoMove).filter(FeedTerrenoMove.move_date >= since).all():
        out[(carril.get(m.pond_id, m.pond_id), m.feed_type_id)].append({
            "fecha": m.move_date, "tipo": "ajuste", "kg": float(m.kg),
            "estimado": False, "motivo": m.motivo, "id": m.id})
    orden = {"salida": 0, "ajuste": 1, "racion": 2}
    for evs in out.values():
        evs.sort(key=lambda e: (e["fecha"], orden[e["tipo"]], e["id"]))
    return out


def saldo(evs: list, upto: date, momento: str = "despues") -> float:
    """Saldo al día `upto`: 'antes' de alimentar ese día o 'despues'."""
    s = 0.0
    for e in evs:
        if e["fecha"] > upto:
            break
        if e["fecha"] == upto and e["tipo"] == "racion" and momento == "antes":
            continue
        s += e["kg"]
    return round(s, 2)


def cargas(evs: list) -> list:
    """Cargas que siguen en el tarro, gastando primero la más antigua.

    Carga = salida de bodega o ajuste positivo. Devuelve [{fecha, kg}] con lo
    que queda de cada una (solo las que no se han terminado).
    """
    entradas = [[e["fecha"], e["kg"]] for e in evs if e["kg"] > 0]
    gasto = -sum(e["kg"] for e in evs if e["kg"] < 0)
    for c in entradas:
        usa = min(c[1], gasto)
        c[1] -= usa
        gasto -= usa
    return [{"fecha": f, "kg": round(k, 2)} for f, k in entradas if k > TOL_KG]


def ritmo_by_carril(db: Session, today: date) -> dict:
    """{carril: kg/día} promedio de los últimos RITMO_DIAS (los ayunos cuentan 0)."""
    desde = today - timedelta(days=RITMO_DIAS)
    carril = _carril_of(db)
    tot: dict = defaultdict(float)
    hay: set = set()
    for r in (db.query(FeedRation)
              .filter(FeedRation.ration_date >= desde, FeedRation.ration_date < today).all()):
        c = carril.get(r.pond_id, r.pond_id)
        hay.add(c)
        if r.status == "alimentado":
            tot[c] += float(r.kg_fed or 0)
        elif r.status == "programado":
            tot[c] += float(r.kg_planned or 0)
    return {c: tot[c] / RITMO_DIAS for c in hay}


def topes(db: Session) -> dict:
    return {t.pond_id: float(t.capacity_kg) for t in db.query(FeedTarro).all()}


def bodega_bags(db: Session, feed_type_id: int) -> int:
    """Sacos en la bodega principal (mismo criterio que calculate_feed_stock)."""
    total = 0
    for mt, bags in (db.query(FeedStockLedger.movement_type, func.sum(FeedStockLedger.bags_delta))
                     .filter(FeedStockLedger.feed_type_id == feed_type_id)
                     .group_by(FeedStockLedger.movement_type).all()):
        mt = getattr(mt, "value", mt)
        if mt in ("entry", "adjustment"):
            total += int(bags or 0)
        elif mt == "execute":
            total -= abs(int(bags or 0))
    return total


# ---------------------------------------------------------------------------
# Estado para la vista
# ---------------------------------------------------------------------------
def estado(db: Session, today: date) -> dict:
    since = start_date(db)
    evs = events(db, since)
    inputs = fr.carril_inputs(db)
    ritmo = ritmo_by_carril(db, today)
    tope = topes(db)
    feeds = {f.id: f for f in db.query(FeedType).all()}
    units = {u.id: u.name for u in db.query(CultivationUnit).all()}
    pend = fr.pending_dates_by_unit(db, today)
    ult_conteo = {}
    for c in (db.query(FeedTerrenoCount).filter(FeedTerrenoCount.annulled_at.is_(None))
              .order_by(FeedTerrenoCount.count_date, FeedTerrenoCount.id).all()):
        ult_conteo[(c.pond_id, c.feed_type_id)] = c

    carriles = set(inputs) | {k[0] for k in evs}
    ponds = {p.id: p for p in db.query(Pond).filter(Pond.id.in_(carriles)).all()} if carriles else {}

    por_unidad: dict = defaultdict(list)
    alertas = []
    for pid in carriles:
        p = ponds.get(pid)
        if p is None:
            continue
        inp = inputs.get(pid) or {}
        alimentos = []
        for (cp, ft), ev in evs.items():
            if cp != pid:
                continue
            s = saldo(ev, today)
            cg = cargas(ev)
            uc = ult_conteo.get((pid, ft))
            if abs(s) <= TOL_KG and not cg and ft != inp.get("feed_type_id"):
                continue
            alimentos.append({
                "feed_type_id": ft,
                "feed_name": feeds[ft].name if ft in feeds else "Sin alimento",
                "bag_kg": float(feeds[ft].bag_kg) if ft in feeds and feeds[ft].bag_kg else None,
                "saldo_kg": s,
                "estimado": any(e["estimado"] for e in ev),
                "carga_antigua": cg[0]["fecha"] if cg else None,
                "dias_carga": (today - cg[0]["fecha"]).days if cg else None,
                "ultimo_conteo": ({"fecha": uc.count_date, "kg": float(uc.counted_kg), "id": uc.id}
                                  if uc else None),
            })
        if inp.get("feed_type_id") and not any(a["feed_type_id"] == inp["feed_type_id"] for a in alimentos):
            ft = inp["feed_type_id"]
            alimentos.append({"feed_type_id": ft, "feed_name": inp.get("feed_name"),
                              "bag_kg": float(feeds[ft].bag_kg) if feeds[ft].bag_kg else None,
                              "saldo_kg": 0.0, "estimado": False, "carga_antigua": None,
                              "dias_carga": None, "ultimo_conteo": None})
        alimentos.sort(key=lambda a: (-abs(a["saldo_kg"]), a["feed_name"] or ""))
        if not alimentos:
            continue        # nunca ha recibido alimento (Jaula, Tranque): nada que contar
        total = round(sum(a["saldo_kg"] for a in alimentos), 2)
        rit = ritmo.get(pid)
        cobertura = (total / rit) if rit and rit > 0 and total > 0 else None
        t = tope.get(pid, DEFAULT_TOPE_KG)

        avisos = []
        if total < -1:
            avisos.append(("neg", "saldo negativo ({:.0f} kg): falta registrar una salida o hay ración de más".format(total)))
        if total > t:
            avisos.append(("tope", "tiene inventario inusual: {:.0f} kg (tope del tarro {:.0f} kg)".format(total, t)))
        elif cobertura and cobertura > COBERTURA_MAX_DIAS:
            avisos.append(("cobertura", "tiene para {:.0f} días (más de {})".format(cobertura, COBERTURA_MAX_DIAS)))
        if sum(1 for a in alimentos if a["saldo_kg"] > 1) > 1:
            avisos.append(("mezcla", "tiene saldo de dos alimentos: vaciar o terminar uno antes de rellenar"))
        if any(a["feed_type_id"] is None and abs(a["saldo_kg"]) > TOL_KG for a in alimentos):
            avisos.append(("sin_alimento", "tiene ración registrada sin tipo de alimento"))
        vieja = max((a["dias_carga"] or 0) for a in alimentos) if alimentos else 0
        if vieja > CARGA_VIEJA_DIAS:
            avisos.append(("vieja", "tiene alimento abierto hace {} días".format(vieja)))

        uid = p.cultivation_unit_id or 0
        c = {"pond_id": pid, "name": p.name, "con_peces": pid in inputs,
             "alimentos": alimentos, "total_kg": total, "ritmo_kg": rit,
             "cobertura_dias": cobertura, "tope_kg": t, "tope_propio": pid in tope,
             "avisos": avisos}
        por_unidad[uid].append(c)
        for k, txt in avisos:
            alertas.append({"tipo": k, "pond_id": pid, "texto": "{} {}".format(p.name, txt)})

    unidades = []
    for uid, cs in por_unidad.items():
        cs.sort(key=lambda c: c["name"])
        unidades.append({"unit_id": uid, "name": units.get(uid, "Sin unidad"),
                         "carriles": cs, "total_kg": round(sum(c["total_kg"] for c in cs), 1),
                         "pendientes": pend.get(uid or None, [])})
    unidades.sort(key=lambda u: (u["unit_id"] == 0, u["unit_id"]))
    return {"since": since, "unidades": unidades, "alertas": alertas,
            "conteo_inicial": db.query(FeedTerrenoCount.count_date)
                                .filter(FeedTerrenoCount.is_initial.is_(True),
                                        FeedTerrenoCount.annulled_at.is_(None)).first()}


def carril_historial(db: Session, pond_id: int) -> dict:
    """Movimientos del tarro de un carril con el saldo corrido, por alimento."""
    evs = events(db)
    feeds = {f.id: f.name for f in db.query(FeedType).all()}
    out = []
    for (cp, ft), ev in evs.items():
        if cp != pond_id:
            continue
        s, filas = 0.0, []
        for e in ev:
            s += e["kg"]
            filas.append(dict(e, saldo=round(s, 2), motivo_label=MOTIVO_LABEL.get(e["motivo"])))
        out.append({"feed_type_id": ft, "feed_name": feeds.get(ft, "Sin alimento"),
                    "filas": filas, "saldo": round(s, 2)})
    out.sort(key=lambda a: -abs(a["saldo"]))
    return out


# ---------------------------------------------------------------------------
# Conteos
# ---------------------------------------------------------------------------
class ConteoError(ValueError):
    pass


def _ledger(db: Session, feed_type_id: int, bags: int, bag_kg: Decimal, move_id: int,
            now: datetime, who: str) -> FeedStockLedger:
    led = FeedStockLedger(feed_type_id=feed_type_id, movement_type=FeedMovementType.adjustment,
                          bags_delta=bags, kg_delta=bags * bag_kg,
                          source_table="feed_terreno_moves", source_id=move_id,
                          created_at=now, created_by=who)
    db.add(led)
    db.flush()
    return led


def paper_kg(db: Session, pond_id: int, feed_type_id: Optional[int], fecha: date, momento: str) -> float:
    return saldo(events(db).get((pond_id, feed_type_id), []), fecha, momento)


def registrar_conteo(db: Session, *, pond_id: int, feed_type_id: Optional[int], fecha: date,
                     momento: str, counted_kg: float, lineas: list, user_id: Optional[int],
                     who: str, is_initial: bool = False, note: Optional[str] = None,
                     today: Optional[date] = None) -> FeedTerrenoCount:
    """Guarda un conteo y reparte la diferencia en ajustes con motivo.

    lineas: [{motivo, kg | sacos, consumo_date, destino_pond_id, note}] con
    cantidades positivas; el signo lo pone el motivo. Deben sumar la
    diferencia contado − papel. No hace commit.
    """
    today = today or date.today()
    since = start_date(db)
    if since is None:
        raise ConteoError("Todavía no hay salidas de bodega desde que existe la ración: no hay saldo que contar")
    if momento not in ("antes", "despues"):
        raise ConteoError("Momento inválido")
    if fecha < since or fecha > today:
        raise ConteoError("La fecha del conteo debe estar entre el {} y hoy".format(since.strftime("%d-%m")))
    if counted_kg < 0:
        raise ConteoError("Los kg contados no pueden ser negativos")
    if _carril_of(db).get(pond_id, pond_id) != pond_id:
        raise ConteoError("El conteo se hace en el carril (estanque padre)")
    ultimo = (db.query(FeedTerrenoCount.count_date)
              .filter(FeedTerrenoCount.pond_id == pond_id, FeedTerrenoCount.feed_type_id == feed_type_id,
                      FeedTerrenoCount.annulled_at.is_(None))
              .order_by(FeedTerrenoCount.count_date.desc()).first())
    if ultimo and fecha < ultimo[0]:
        raise ConteoError("Ya hay un conteo posterior ({}) de este alimento en el carril".format(ultimo[0].strftime("%d-%m")))

    ft = db.get(FeedType, feed_type_id) if feed_type_id else None
    bag_kg = Decimal(str(ft.bag_kg)) if ft and ft.bag_kg else None
    papel = paper_kg(db, pond_id, feed_type_id, fecha, momento)
    dif = round(counted_kg - papel, 2)

    plan, suma = [], 0.0
    for ln in lineas or []:
        key = ln.get("motivo")
        m = MOTIVOS.get(key)
        if not m:
            raise ConteoError("Motivo desconocido: {}".format(key))
        if m.get("solo_inicial") and not is_initial:
            raise ConteoError("'{}' solo vale en el conteo inicial".format(m["label"]))
        sacos = None
        if m.get("bodega"):
            if bag_kg is None:
                raise ConteoError("'{}' necesita un alimento con peso de saco".format(m["label"]))
            try:
                sacos = int(ln.get("sacos") or 0)
            except (TypeError, ValueError):
                raise ConteoError("'{}' va en sacos enteros".format(m["label"]))
            if sacos <= 0:
                raise ConteoError("'{}': indica los sacos".format(m["label"]))
            kg = float(sacos * bag_kg)
        else:
            try:
                kg = round(float(ln.get("kg") or 0), 2)
            except (TypeError, ValueError):
                raise ConteoError("'{}': kg inválidos".format(m["label"]))
            if kg <= 0:
                raise ConteoError("'{}': indica los kg".format(m["label"]))
        consumo = None
        if m.get("fecha"):
            try:
                consumo = date.fromisoformat(ln.get("consumo_date") or "")
            except ValueError:
                raise ConteoError("'{}': indica qué día se dio".format(m["label"]))
            if consumo > fecha:
                raise ConteoError("'{}': el día no puede ser posterior al conteo".format(m["label"]))
        destino = None
        if m.get("destino"):
            try:
                destino = int(ln.get("destino_pond_id") or 0)
            except (TypeError, ValueError):
                destino = 0
            if not destino or destino == pond_id or not db.get(Pond, destino):
                raise ConteoError("'{}': elige el carril de destino".format(m["label"]))
            if _carril_of(db).get(destino, destino) != destino:
                raise ConteoError("El destino debe ser un carril (estanque padre)")
        plan.append((key, m, kg, sacos, consumo, destino, (ln.get("note") or "").strip() or None))
        suma += m["sign"] * kg

    if abs(round(suma, 2) - dif) > TOL_KG:
        raise ConteoError("Los motivos suman {:+.1f} kg y la diferencia es {:+.1f} kg".format(suma, dif))

    now = datetime.utcnow()
    cnt = FeedTerrenoCount(count_date=fecha, momento=momento, pond_id=pond_id, feed_type_id=feed_type_id,
                           paper_kg=Decimal(str(papel)), counted_kg=Decimal(str(round(counted_kg, 2))),
                           is_initial=is_initial, note=(note or "").strip() or None,
                           created_by=user_id, created_at=now)
    db.add(cnt)
    db.flush()
    for key, m, kg, sacos, consumo, destino, nota in plan:
        mv = FeedTerrenoMove(move_date=fecha, pond_id=pond_id, feed_type_id=feed_type_id,
                             kg=Decimal(str(m["sign"] * kg)), motivo=key, count_id=cnt.id,
                             consumo_date=consumo, note=nota, created_by=user_id, created_at=now)
        db.add(mv)
        db.flush()
        if sacos:
            mv.bodega_bags = m["bodega"] * sacos
            mv.ledger_id = _ledger(db, feed_type_id, mv.bodega_bags, bag_kg, mv.id, now, who).id
        if destino:
            rec = FeedTerrenoMove(move_date=fecha, pond_id=destino, feed_type_id=feed_type_id,
                                  kg=Decimal(str(kg)), motivo=TRASPASO_RECIBIDO, related_move_id=mv.id,
                                  note=nota, created_by=user_id, created_at=now)
            db.add(rec)
            db.flush()
            mv.related_move_id = rec.id
    db.flush()
    return cnt


def anular_conteo(db: Session, count_id: int, user_id: Optional[int]) -> FeedTerrenoCount:
    """Anula el último conteo de un carril/alimento: borra sus ajustes y lo
    que movieron en la bodega principal; el conteo queda como anulado."""
    cnt = db.get(FeedTerrenoCount, count_id)
    if cnt is None or cnt.annulled_at is not None:
        raise ConteoError("Ese conteo no existe o ya está anulado")
    posterior = (db.query(FeedTerrenoCount.id)
                 .filter(FeedTerrenoCount.pond_id == cnt.pond_id,
                         FeedTerrenoCount.feed_type_id == cnt.feed_type_id,
                         FeedTerrenoCount.annulled_at.is_(None),
                         FeedTerrenoCount.id != cnt.id,
                         FeedTerrenoCount.count_date >= cnt.count_date)
                 .filter(FeedTerrenoCount.id > cnt.id).first())
    if posterior:
        raise ConteoError("Solo se puede anular el último conteo de ese alimento en el carril")
    for mv in db.query(FeedTerrenoMove).filter(FeedTerrenoMove.count_id == cnt.id).all():
        if mv.related_move_id:
            rec = db.get(FeedTerrenoMove, mv.related_move_id)
            mv.related_move_id = None
            if rec is not None:
                rec.related_move_id = None
            db.flush()
            if rec is not None:
                db.delete(rec)
        if mv.ledger_id:
            led = db.get(FeedStockLedger, mv.ledger_id)
            mv.ledger_id = None
            db.flush()
            if led is not None:
                db.delete(led)
        db.delete(mv)
    cnt.annulled_at = datetime.utcnow()
    cnt.annulled_by = user_id
    db.flush()
    return cnt


def ajustar_bodega(db: Session, feed_type_id: int, real_bags: int, comentario: str, who: str) -> Optional[int]:
    """Cuadra la bodega principal contra un conteo físico (como el cuadre de
    inventario de siempre). Devuelve la diferencia aplicada (o None si 0)."""
    papel = bodega_bags(db, feed_type_id)
    dif = int(real_bags) - papel
    if dif == 0:
        return None
    now = datetime.utcnow()
    adj = FeedInventoryAdjustment(feed_type_id=feed_type_id, stock_calculated_bags=papel,
                                  stock_real_bags=int(real_bags), difference_bags=dif,
                                  comment=comentario, created_by=who, created_at=now, updated_at=now)
    db.add(adj)
    db.flush()
    ft = db.get(FeedType, feed_type_id)
    db.add(FeedStockLedger(feed_type_id=feed_type_id, movement_type=FeedMovementType.adjustment,
                           bags_delta=dif, kg_delta=dif * Decimal(str(ft.bag_kg or 0)),
                           source_table="feed_inventory_adjustments", source_id=adj.id,
                           created_at=now, created_by=who))
    db.flush()
    return dif
