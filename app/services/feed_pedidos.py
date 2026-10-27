"""Pedidos de reposición de los tarros.

Sugerencia por carril (alimento de su última ración):
    ritmo    = kg del último día con ración × días de alimentación por semana / 7
    alcanza  = saldo del tarro / ritmo
    si alcanza ≥ n días → no se rellena
    sacos    = ⌈(ritmo × N − saldo) / kg por saco⌉, sin pasar el tope del tarro

Emitir no mueve stock (queda "comprometido"). Al marcar "salió" se registran
las salidas (FeedExecutionEvent + movimiento execute en el libro de stock), que
bajan la bodega principal y cargan los tarros. Un carril no puede estar en dos
pedidos emitidos a la vez: así no se registra dos veces la misma salida.
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, datetime, time
from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Session

from app.models.cultivation_units import CultivationUnit
from app.models.feed import (FeedExecutionEvent, FeedMovementType, FeedRation, FeedStockLedger,
                             FeedType)
from app.models.feed_pedidos import FeedPedido, FeedPedidoLine
from app.services import feed_ration as fr
from app.services import feed_terreno as ft

DEFAULT_N = 10
DEFAULT_MIN = 3
DEFAULT_DIAS_SEMANA = 6
MAX_N = 15


class PedidoError(ValueError):
    pass


def ultima_racion(db: Session) -> dict:
    """{carril: {kg, feed_type_id, fecha}} del último día con ración > 0
    (programada a futuro incluida: es la última decisión)."""
    carril = ft._carril_of(db)
    out: dict = {}
    for r in db.query(FeedRation).order_by(FeedRation.ration_date).all():
        kg = float(r.kg_fed or 0) if r.status == "alimentado" else (
            float(r.kg_planned or 0) if r.status == "programado" else 0.0)
        if kg <= 0:
            continue
        c = carril.get(r.pond_id, r.pond_id)
        out[c] = {"kg": kg, "feed_type_id": r.feed_type_id, "fecha": r.ration_date}
    return out


def comprometido(db: Session) -> dict:
    """{feed_type_id: sacos} en pedidos emitidos (aún no salen)."""
    out: dict = defaultdict(int)
    for ftid, bags in (db.query(FeedPedidoLine.feed_type_id, FeedPedidoLine.bags)
                       .join(FeedPedido, FeedPedido.id == FeedPedidoLine.pedido_id)
                       .filter(FeedPedido.status == "emitido").all()):
        out[ftid] += int(bags or 0)
    return out


def carriles_emitidos(db: Session, excepto: Optional[int] = None) -> dict:
    """{pond_id: código} de carriles que ya están en un pedido emitido."""
    q = (db.query(FeedPedidoLine.pond_id, FeedPedido.code)
         .join(FeedPedido, FeedPedido.id == FeedPedidoLine.pedido_id)
         .filter(FeedPedido.status == "emitido", FeedPedidoLine.bags > 0))
    if excepto:
        q = q.filter(FeedPedido.id != excepto)
    return {pid: code for pid, code in q.all()}


def stock(db: Session) -> list:
    comp = comprometido(db)
    out = []
    for f in db.query(FeedType).filter(FeedType.active == 1).order_by(FeedType.name).all():
        b = ft.bodega_bags(db, f.id)
        out.append({"feed_type_id": f.id, "name": f.name, "bag_kg": float(f.bag_kg or 0),
                    "bodega": b, "comprometido": comp.get(f.id, 0), "disponible": b - comp.get(f.id, 0)})
    return out


def sugerencia(db: Session, today: date, n_dias: int, min_dias: int, dias_semana: int) -> list:
    """Unidades con sus carriles y los sacos sugeridos (del alimento de la
    última ración; el saldo es el del tarro completo)."""
    inputs = fr.carril_inputs(db)
    ultima = ultima_racion(db)
    evs = ft.events(db)
    tope = ft.topes(db)
    feeds = {f.id: f for f in db.query(FeedType).all()}
    units = {u.id: u.name for u in db.query(CultivationUnit).all()}
    en_pedido = carriles_emitidos(db)
    en_racion = fr.ration_units(db)

    por_unidad: dict = defaultdict(list)
    for pid, c in inputs.items():
        u = ultima.get(pid)
        ftid = (u or {}).get("feed_type_id") or c.get("feed_type_id")
        f = feeds.get(ftid)
        bag = float(f.bag_kg) if f and f.bag_kg else None
        # Lo que queda en el tarro se come primero, sea del alimento que sea:
        # cuenta entero para la cobertura, y lo que se pide es de la dieta.
        sim = ft.simular(evs.get(pid, []), today)["saldos"]
        saldo = sum(sim.values())
        viejos = [(feeds[x].name if x in feeds else "sin alimento", k)
                  for x, k in sim.items() if x != ftid and k > 1]
        ritmo = u["kg"] * dias_semana / 7.0 if u else 0.0
        alcanza = saldo / ritmo if ritmo > 0 else None
        t = tope.get(pid, ft.DEFAULT_TOPE_KG)
        sug, limitado, motivo = 0, False, None
        if not u:
            motivo = "sin ración"
        elif not bag:
            motivo = "alimento sin peso de saco"
        elif alcanza is not None and alcanza >= min_dias:
            motivo = "alcanza {:.0f} días".format(alcanza)
        else:
            sug = max(1, math.ceil((ritmo * n_dias - saldo) / bag))
            cabe = math.floor(max(0.0, t - saldo) / bag)
            if sug > cabe:
                sug, limitado = cabe, True
                motivo = "limitado por el tope ({:.0f} kg)".format(t)
        por_unidad[c["unit_id"] or 0].append({
            "pond_id": pid, "name": c["name"], "feed_type_id": ftid,
            "feed_name": f.name if f else None, "bag_kg": bag,
            "saldo_kg": round(saldo, 1), "ritmo_kg": round(ritmo, 1),
            "ultima": u, "alcanza_dias": alcanza, "tope_kg": t,
            "transicion": ("tarro con {}".format(", ".join("{:.0f} kg de {}".format(k, nm) for nm, k in viejos))
                           if viejos else None),
            "sugerido": sug, "limitado": limitado, "motivo": motivo,
            "en_pedido": en_pedido.get(pid),
        })
    out = []
    for uid, cs in por_unidad.items():
        cs.sort(key=lambda c: c["name"])
        out.append({"unit_id": uid, "name": units.get(uid, "Sin unidad"), "carriles": cs,
                    "en_racion": uid in en_racion})
    out.sort(key=lambda u: (not u["en_racion"], u["unit_id"] == 0, u["unit_id"]))
    return out


def _code(db: Session, d: date) -> str:
    base = "PED-{}".format(d.strftime("%Y%m%d"))
    n = db.query(FeedPedido.id).filter(FeedPedido.code.like(base + "%")).count()
    return base if n == 0 else "{}-{}".format(base, n + 1)


def emitir(db: Session, *, lineas: list, n_dias: int, min_dias: int, dias_semana: int,
           user_id: Optional[int], note: Optional[str] = None) -> FeedPedido:
    """lineas: [{pond_id, feed_type_id, bags, saldo_kg, ritmo_kg, sugerido, limitado}]. No hace commit."""
    if not (1 <= n_dias <= MAX_N) or not (0 <= min_dias <= n_dias) or not (1 <= dias_semana <= 7):
        raise PedidoError("Parámetros inválidos (N entre 1 y {}, n ≤ N, días por semana 1-7)".format(MAX_N))
    feeds = {f.id: f for f in db.query(FeedType).all()}
    ocupados = carriles_emitidos(db)
    plan, vistos = [], set()
    for ln in lineas or []:
        try:
            pid, ftid, bags = int(ln["pond_id"]), int(ln["feed_type_id"]), int(ln.get("bags") or 0)
        except (KeyError, TypeError, ValueError):
            raise PedidoError("Línea inválida")
        if bags < 0:
            raise PedidoError("Los sacos no pueden ser negativos")
        if bags == 0:
            continue
        if pid in ocupados:
            raise PedidoError("El carril {} ya está en el pedido {} (aún no sale)".format(pid, ocupados[pid]))
        if (pid, ftid) in vistos:
            raise PedidoError("Carril y alimento repetidos en el pedido")
        f = feeds.get(ftid)
        if f is None or not f.bag_kg:
            raise PedidoError("Alimento sin peso de saco")
        vistos.add((pid, ftid))
        plan.append((pid, ftid, bags, Decimal(str(f.bag_kg)), ln))
    if not plan:
        raise PedidoError("El pedido no tiene sacos")
    now = datetime.utcnow()
    p = FeedPedido(code=_code(db, date.today()), status="emitido", dias_n=n_dias, dias_min=min_dias,
                   dias_semana=dias_semana, note=(note or "").strip() or None,
                   created_by=user_id, created_at=now)
    db.add(p)
    db.flush()
    for pid, ftid, bags, bag_kg, ln in plan:
        def dec(k):
            v = ln.get(k)
            return Decimal(str(v)) if v not in (None, "") else None
        sug = ln.get("sugerido")
        db.add(FeedPedidoLine(pedido_id=p.id, pond_id=pid, feed_type_id=ftid, bags=bags, bag_kg=bag_kg,
                              saldo_kg=dec("saldo_kg"), ritmo_kg=dec("ritmo_kg"),
                              sugerido_bags=int(sug) if sug not in (None, "") else None,
                              limitado_tope=bool(ln.get("limitado"))))
    db.flush()
    return p


def marcar_salio(db: Session, pedido_id: int, fecha: date, user_id: Optional[int], who: str,
                 today: Optional[date] = None) -> FeedPedido:
    """Registra las salidas del pedido tal como se emitió. No hace commit."""
    today = today or date.today()
    p = db.get(FeedPedido, pedido_id)
    if p is None or p.status != "emitido":
        raise PedidoError("El pedido no está emitido (ya salió o se anuló)")
    if fecha > today or fecha < p.created_at.date():
        raise PedidoError("La fecha de salida debe estar entre la emisión y hoy")
    now = datetime.utcnow()
    at = now if fecha == today else datetime.combine(fecha, time(12, 0))
    for ln in db.query(FeedPedidoLine).filter(FeedPedidoLine.pedido_id == p.id).all():
        if ln.bags <= 0:
            continue
        kg = ln.bags * Decimal(ln.bag_kg)
        ev = FeedExecutionEvent(feed_program_id=None, feed_pedido_line_id=ln.id, pond_id=ln.pond_id,
                                feed_type_id=ln.feed_type_id, confirmed_bags=ln.bags, confirmed_kg=kg,
                                is_unplanned=0, confirmed_at=at, confirmed_by=who,
                                note="Pedido {}".format(p.code))
        db.add(ev)
        db.flush()
        db.add(FeedStockLedger(feed_type_id=ln.feed_type_id, movement_type=FeedMovementType.execute,
                               bags_delta=-ln.bags, kg_delta=-kg, source_table="feed_execution_events",
                               source_id=ev.id, created_at=now, created_by=who))
    p.status, p.salio_date, p.salio_by, p.salio_at = "salio", fecha, user_id, now
    db.flush()
    return p


def anular(db: Session, pedido_id: int, user_id: Optional[int]) -> FeedPedido:
    p = db.get(FeedPedido, pedido_id)
    if p is None or p.status != "emitido":
        raise PedidoError("Solo se anula un pedido emitido que aún no sale")
    p.status, p.anulado_by, p.anulado_at = "anulado", user_id, datetime.utcnow()
    db.flush()
    return p


def detalle(db: Session, pedido_id: int) -> Optional[dict]:
    p = db.get(FeedPedido, pedido_id)
    if p is None:
        return None
    from app.models.ponds import Pond
    lines = db.query(FeedPedidoLine).filter(FeedPedidoLine.pedido_id == p.id).all()
    ponds = {x.id: x for x in db.query(Pond).filter(Pond.id.in_([l.pond_id for l in lines] or [0])).all()}
    feeds = {f.id: f.name for f in db.query(FeedType).all()}
    units = {u.id: u.name for u in db.query(CultivationUnit).all()}
    por_unidad: dict = defaultdict(list)
    tot: dict = defaultdict(lambda: {"bags": 0, "kg": 0.0})
    for l in lines:
        pd = ponds.get(l.pond_id)
        uid = pd.cultivation_unit_id if pd else 0
        por_unidad[uid or 0].append({"name": pd.name if pd else l.pond_id, "feed": feeds.get(l.feed_type_id),
                                     "bags": l.bags, "bag_kg": float(l.bag_kg), "kg": l.bags * float(l.bag_kg),
                                     "saldo_kg": float(l.saldo_kg) if l.saldo_kg is not None else None,
                                     "ritmo_kg": float(l.ritmo_kg) if l.ritmo_kg is not None else None,
                                     "sugerido": l.sugerido_bags, "limitado": l.limitado_tope})
        t = tot[feeds.get(l.feed_type_id)]
        t["bags"] += l.bags
        t["kg"] += l.bags * float(l.bag_kg)
    unidades = [{"name": units.get(uid, "Sin unidad"), "carriles": sorted(cs, key=lambda c: str(c["name"])),
                 "bags": sum(c["bags"] for c in cs)}
                for uid, cs in sorted(por_unidad.items(), key=lambda kv: (kv[0] == 0, kv[0]))]
    return {"pedido": p, "unidades": unidades,
            "totales": sorted(({"feed": k, **v} for k, v in tot.items()), key=lambda x: x["feed"] or ""),
            "bags": sum(v["bags"] for v in tot.values()), "kg": sum(v["kg"] for v in tot.values())}


def lista(db: Session, limit: int = 60) -> list:
    ps = db.query(FeedPedido).order_by(FeedPedido.id.desc()).limit(limit).all()
    if not ps:
        return []
    from sqlalchemy import func
    agg = {pid: (b, c) for pid, b, c in
           db.query(FeedPedidoLine.pedido_id, func.sum(FeedPedidoLine.bags), func.count(FeedPedidoLine.id))
           .filter(FeedPedidoLine.pedido_id.in_([p.id for p in ps])).group_by(FeedPedidoLine.pedido_id).all()}
    return [{"p": p, "bags": int(agg.get(p.id, (0, 0))[0] or 0), "carriles": agg.get(p.id, (0, 0))[1]}
            for p in ps]
