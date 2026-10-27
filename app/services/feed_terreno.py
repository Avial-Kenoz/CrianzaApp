"""Inventario en terreno: el tarro de cada carril como bodega intermedia.

Saldo de un carril y un alimento =
    salidas de bodega (cargan el tarro)
  − ración del día (alimentado: kg_fed · no alimentado: 0 · programado: kg_planned)
  ± ajustes de los conteos (FeedTerrenoMove, cada uno con su motivo)

Se cuenta desde terreno_since (la primera salida de bodega desde que existe la
ración diaria): antes no hay registro de lo que se echó al agua.

El alimento se elige al programar la ración (la dieta) y es la instrucción de
carga del tarro, pero lo que se come sale del tarro gastando primero la carga
más antigua, sea del alimento que sea: al cambiar la dieta con saldo del
anterior, primero se termina lo viejo (transición) y una carga vieja llega a
cero aunque se rellene encima (sin la paradoja del promedio).

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
# Conteo inicial: el tarro queda en lo contado sin explicar cada diferencia;
# el cuadre se lee por alimento contra el descuadre de la bodega principal.
CONTEO_INICIAL = "conteo_inicial"
MOTIVO_LABEL = dict({k: v["label"] for k, v in MOTIVOS.items()},
                    **{TRASPASO_RECIBIDO: "Recibido de otro carril",
                       CONTEO_INICIAL: "Diferencia del conteo inicial",
                       "reparto": "Repartido a otros carriles",
                       "reparto_recibido": "Recibido por reparto"})


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
    """{carril: [evento]} ordenado por fecha.

    evento = {fecha, tipo: salida|racion|ajuste, feed_type_id, kg (con signo),
              estimado, motivo, id}. La ración de un día programado y sin
    confirmar cuenta como estimada (kg_planned). Su feed_type_id es el
    alimento programado, pero lo que se come sale del tarro (ver simular).
    """
    since = since or start_date(db)
    out: dict = defaultdict(list)
    if since is None:
        return out
    carril = _carril_of(db)
    for ev in (db.query(FeedExecutionEvent)
               .filter(func.date(FeedExecutionEvent.confirmed_at) >= since).all()):
        out[carril.get(ev.pond_id, ev.pond_id)].append({
            "fecha": ev.confirmed_at.date(), "tipo": "salida", "feed_type_id": ev.feed_type_id,
            "kg": float(ev.confirmed_kg or 0), "estimado": False, "motivo": None, "id": ev.id})
    for r in db.query(FeedRation).filter(FeedRation.ration_date >= since).all():
        if r.status == "alimentado":
            kg, est = float(r.kg_fed or 0), False
        elif r.status == "programado":
            kg, est = float(r.kg_planned or 0), True
        else:
            continue
        if kg:
            out[carril.get(r.pond_id, r.pond_id)].append({
                "fecha": r.ration_date, "tipo": "racion", "feed_type_id": r.feed_type_id,
                "kg": -kg, "estimado": est, "motivo": None, "id": r.id})
    for m in db.query(FeedTerrenoMove).filter(FeedTerrenoMove.move_date >= since).all():
        out[carril.get(m.pond_id, m.pond_id)].append({
            "fecha": m.move_date, "tipo": "ajuste", "feed_type_id": m.feed_type_id,
            "kg": float(m.kg), "estimado": False, "motivo": m.motivo, "id": m.id})
    orden = {"salida": 0, "ajuste": 1, "racion": 2}
    for evs in out.values():
        evs.sort(key=lambda e: (e["fecha"], orden[e["tipo"]], e["id"]))
    return out


def simular(evs: list, upto: Optional[date] = None, momento: str = "despues") -> dict:
    """Recorre los movimientos de un tarro gastando primero la carga más antigua.

    - Salida o ajuste positivo: nueva carga de su alimento.
    - Ración: sale del tarro completo, lo más antiguo primero, sea cual sea el
      alimento programado (así se hace la transición al cambiar la dieta).
      Si el tarro no alcanza, la falta queda en el alimento programado.
    - Ajuste negativo (conteo, vaciar, traspaso): sale de ese alimento.

    upto/momento: hasta qué día ('antes' de alimentar ese día o 'despues').
    Devuelve {saldos: {alimento: kg}, cargas: {alimento: [{fecha, kg}]},
    filas: [evento + consumo por alimento + saldo del tarro]}.
    """
    cargas: list = []                  # [fecha, alimento, kg] en orden de llegada
    deuda: dict = defaultdict(float)   # lo que faltó, por alimento
    filas = []
    for e in evs:
        if upto is not None:
            if e["fecha"] > upto:
                break
            if e["fecha"] == upto and e["tipo"] == "racion" and momento == "antes":
                continue
        f, kg = e["feed_type_id"], e["kg"]
        uso: dict = defaultdict(float)
        if kg > 0:
            paga = min(kg, deuda[f])
            deuda[f] -= paga
            if kg - paga > TOL_KG:
                cargas.append([e["fecha"], f, kg - paga])
        else:
            falta = -kg
            for c in cargas:
                if falta <= 0:
                    break
                if e["tipo"] != "racion" and c[1] != f:
                    continue
                usa = min(c[2], falta)
                c[2] -= usa
                falta -= usa
                uso[c[1]] += usa
            cargas = [c for c in cargas if c[2] > TOL_KG]
            if falta > TOL_KG:
                deuda[f] += falta
                uso[f] += falta
        total = sum(c[2] for c in cargas) - sum(deuda.values())
        filas.append(dict(e, uso=dict(uso), saldo=round(total, 2)))
    saldos: dict = defaultdict(float)
    por_alimento: dict = defaultdict(list)
    for fch, f, k in cargas:
        saldos[f] += k
        por_alimento[f].append({"fecha": fch, "kg": round(k, 2)})
    for f, d in deuda.items():
        if d > TOL_KG:
            saldos[f] -= d
    return {"saldos": {f: round(v, 2) for f, v in saldos.items()},
            "cargas": dict(por_alimento), "filas": filas}


def saldo(evs: list, upto: date, momento: str = "despues", feed_type_id=None) -> float:
    """Saldo del tarro (o de un alimento) al día `upto`."""
    s = simular(evs, upto, momento)["saldos"]
    return round(s.get(feed_type_id, 0.0) if feed_type_id is not None else sum(s.values()), 2)


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
    inputs = fr.carril_inputs(db)          # su feed_type_id es la dieta programada
    ritmo = ritmo_by_carril(db, today)
    tope = topes(db)
    feeds = {f.id: f for f in db.query(FeedType).all()}
    units = {u.id: u.name for u in db.query(CultivationUnit).all()}
    pend = fr.pending_dates_by_unit(db, today)
    ult_conteo = {}
    for c in (db.query(FeedTerrenoCount).filter(FeedTerrenoCount.annulled_at.is_(None))
              .order_by(FeedTerrenoCount.count_date, FeedTerrenoCount.id).all()):
        ult_conteo[(c.pond_id, c.feed_type_id)] = c

    carriles = set(inputs) | set(evs)
    ponds = {p.id: p for p in db.query(Pond).filter(Pond.id.in_(carriles)).all()} if carriles else {}

    def fname(f):
        return feeds[f].name if f in feeds else "Sin alimento"

    def bag(f):
        return float(feeds[f].bag_kg) if f in feeds and feeds[f].bag_kg else None

    def conteo(pid, f):
        uc = ult_conteo.get((pid, f))
        return {"fecha": uc.count_date, "kg": float(uc.counted_kg), "id": uc.id} if uc else None

    por_unidad: dict = defaultdict(list)
    alertas = []
    for pid in carriles:
        p = ponds.get(pid)
        if p is None:
            continue
        inp = inputs.get(pid) or {}
        dieta = inp.get("feed_type_id")
        ev = evs.get(pid, [])
        sim = simular(ev, today)
        estimado = any(e["estimado"] for e in ev)
        alimentos = []
        for f, s in sim["saldos"].items():
            cg = sim["cargas"].get(f) or []
            if abs(s) <= TOL_KG and f != dieta:
                continue
            alimentos.append({
                "feed_type_id": f, "feed_name": fname(f), "bag_kg": bag(f),
                "saldo_kg": s, "estimado": estimado, "dieta": f == dieta,
                "carga_antigua": cg[0]["fecha"] if cg else None,
                "dias_carga": (today - cg[0]["fecha"]).days if cg else None,
                "ultimo_conteo": conteo(pid, f),
            })
        if dieta and not any(a["feed_type_id"] == dieta for a in alimentos):
            alimentos.append({"feed_type_id": dieta, "feed_name": fname(dieta), "bag_kg": bag(dieta),
                              "saldo_kg": 0.0, "estimado": False, "dieta": True, "carga_antigua": None,
                              "dias_carga": None, "ultimo_conteo": conteo(pid, dieta)})
        if not alimentos:
            continue        # nunca ha recibido alimento (Jaula, Tranque): nada que contar
        # Primero lo que se está comiendo (la carga más antigua); al final la dieta nueva.
        alimentos.sort(key=lambda a: (a["carga_antigua"] is None, a["carga_antigua"] or today, a["feed_name"]))
        total = round(sum(a["saldo_kg"] for a in alimentos), 2)
        rit = ritmo.get(pid)
        cobertura = (total / rit) if rit and rit > 0 and total > 0 else None
        t = tope.get(pid, DEFAULT_TOPE_KG)
        viejos = [a for a in alimentos if a["feed_type_id"] != dieta and a["saldo_kg"] > 1]
        en_dieta = next((a["saldo_kg"] for a in alimentos if a["feed_type_id"] == dieta), 0.0)
        transicion = None
        if viejos and dieta:
            transicion = "quedan {} antes de pasar a {}".format(
                ", ".join("{:.0f} kg de {}".format(a["saldo_kg"], a["feed_name"]) for a in viejos), fname(dieta))

        avisos = []
        if total < -1:
            avisos.append(("neg", "saldo negativo ({:.0f} kg): falta registrar una salida o hay ración de más".format(total)))
        if total > t:
            avisos.append(("tope", "tiene inventario inusual: {:.0f} kg (tope del tarro {:.0f} kg)".format(total, t)))
        elif cobertura and cobertura > COBERTURA_MAX_DIAS:
            avisos.append(("cobertura", "tiene para {:.0f} días (más de {})".format(cobertura, COBERTURA_MAX_DIAS)))
        if viejos and dieta and en_dieta <= TOL_KG and pid in inputs:
            avisos.append(("pedido", "pasa a {}: no hay en el tarro, requiere pedido de bodega".format(fname(dieta))))
        if any(a["feed_type_id"] is None and abs(a["saldo_kg"]) > TOL_KG for a in alimentos):
            avisos.append(("sin_alimento", "tiene ración registrada sin tipo de alimento"))
        vieja = max((a["dias_carga"] or 0) for a in alimentos)
        if vieja > CARGA_VIEJA_DIAS:
            avisos.append(("vieja", "tiene alimento abierto hace {} días".format(vieja)))

        uid = p.cultivation_unit_id or 0
        c = {"pond_id": pid, "name": p.name, "con_peces": pid in inputs,
             "alimentos": alimentos, "total_kg": total, "ritmo_kg": rit, "dieta": dieta,
             "dieta_name": fname(dieta) if dieta else None, "transicion": transicion,
             "cobertura_dias": cobertura, "tope_kg": t, "tope_propio": pid in tope,
             "avisos": avisos}
        por_unidad[uid].append(c)
        for k, txt in avisos:
            alertas.append({"tipo": k, "pond_id": pid, "texto": "{} {}".format(p.name, txt)})

    unidades = []
    for uid, cs in por_unidad.items():
        cs.sort(key=lambda c: c["name"])
        unidades.append({"unit_id": uid, "name": units.get(uid, "Sin unidad"),
                         "grupo": grupo_reparto(uid, units.get(uid)),
                         "carriles": cs, "total_kg": round(sum(c["total_kg"] for c in cs), 1),
                         "pendientes": pend.get(uid or None, [])})
    unidades.sort(key=lambda u: (u["unit_id"] == 0, u["unit_id"]))
    return {"since": since, "unidades": unidades, "alertas": alertas,
            "conteo_inicial": db.query(FeedTerrenoCount.count_date)
                                .filter(FeedTerrenoCount.is_initial.is_(True),
                                        FeedTerrenoCount.annulled_at.is_(None)).first()}


def tarros(db: Session, today: date) -> dict:
    """{carril: {total, saldos: {alimento: kg}, viejos: [(alimento, kg)]}} al cierre de hoy,
    para la ración y los pedidos."""
    out = {}
    for pid, ev in events(db).items():
        s = simular(ev, today)["saldos"]
        out[pid] = {"total": round(sum(s.values()), 2), "saldos": s}
    return out


def carril_historial(db: Session, pond_id: int) -> dict:
    """Movimientos del tarro de un carril, con lo que se consumió de cada
    alimento en cada ración y el saldo del tarro."""
    feeds = {f.id: f.name for f in db.query(FeedType).all()}
    sim = simular(events(db).get(pond_id, []))
    filas = []
    for e in sim["filas"]:
        uso = ", ".join("{:.1f} kg {}".format(k, feeds.get(f, "sin alimento"))
                        for f, k in e["uso"].items() if k > TOL_KG)
        filas.append(dict(e, feed_name=feeds.get(e["feed_type_id"], "Sin alimento"),
                          motivo_label=MOTIVO_LABEL.get(e["motivo"]), uso_txt=uso))
    saldos = [{"feed_name": feeds.get(f, "Sin alimento"), "kg": k}
              for f, k in sim["saldos"].items() if abs(k) > TOL_KG]
    return {"filas": filas, "saldos": saldos, "total": round(sum(sim["saldos"].values()), 2)}


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
    """Saldo de un alimento en el tarro según el sistema."""
    return simular(events(db).get(pond_id, []), fecha, momento)["saldos"].get(feed_type_id, 0.0)


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

    if is_initial and not plan and abs(dif) > TOL_KG:
        plan.append((CONTEO_INICIAL, {"sign": 1}, dif, None, None, None, None))
        suma = dif
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


# ---------------------------------------------------------------------------
# Reparto: mover alimento entre tarros de la misma unidad (no es un conteo)
# ---------------------------------------------------------------------------
def grupo_reparto(unit_id: Optional[int], unit_name: Optional[str]):
    """Entre qué tarros se puede repartir: los de la misma unidad, y las salas
    de Hatchery (Incubación, Sala 2, Alevinaje) entre sí."""
    if unit_name and unit_name.lower().startswith("hatchery"):
        return "hatchery"
    return unit_id or 0


def repartir(db: Session, *, pond_id: int, feed_type_id: Optional[int], fecha: date, destinos: list,
             user_id: Optional[int], note: Optional[str] = None, today: Optional[date] = None) -> FeedTerrenoMove:
    """Pasa kg de un alimento del tarro de un carril a otros de su grupo
    (misma unidad, o cualquier sala de Hatchery; ver grupo_reparto).

    destinos: [{pond_id, kg}]. El total no cambia ni toca la bodega: el origen
    baja (motivo 'reparto') y cada destino sube ('reparto_recibido', enlazado
    al origen por related_move_id). No hace commit.
    """
    today = today or date.today()
    since = start_date(db)
    if since is None or fecha < since or fecha > today:
        raise ConteoError("La fecha del reparto debe estar entre el {} y hoy".format(since.strftime("%d-%m") if since else "—"))
    carril = _carril_of(db)
    origen = db.get(Pond, pond_id)
    if origen is None or carril.get(pond_id, pond_id) != pond_id:
        raise ConteoError("El reparto sale del tarro de un carril (estanque padre)")
    unames = {u.id: u.name for u in db.query(CultivationUnit).all()}
    grupo = grupo_reparto(origen.cultivation_unit_id, unames.get(origen.cultivation_unit_id))
    plan, total = [], 0.0
    for d in destinos or []:
        try:
            pid, kg = int(d.get("pond_id")), round(float(d.get("kg") or 0), 2)
        except (TypeError, ValueError):
            raise ConteoError("Destino inválido")
        if kg <= 0:
            continue
        p = db.get(Pond, pid)
        if p is None or pid == pond_id or carril.get(pid, pid) != pid:
            raise ConteoError("Destino inválido")
        if grupo_reparto(p.cultivation_unit_id, unames.get(p.cultivation_unit_id)) != grupo:
            raise ConteoError("{} no es de la misma unidad".format(p.name))
        plan.append((pid, kg))
        total += kg
    if not plan:
        raise ConteoError("Indica cuántos kg van a cada carril")
    hay = simular(events(db).get(pond_id, []), fecha)["saldos"].get(feed_type_id, 0.0)
    if total > hay + TOL_KG:
        raise ConteoError("Se reparten {:.1f} kg y el tarro tiene {:.1f} kg".format(total, hay))
    now = datetime.utcnow()
    nota = (note or "").strip() or None
    src = FeedTerrenoMove(move_date=fecha, pond_id=pond_id, feed_type_id=feed_type_id,
                          kg=Decimal(str(-round(total, 2))), motivo="reparto", note=nota,
                          created_by=user_id, created_at=now)
    db.add(src)
    db.flush()
    for pid, kg in plan:
        db.add(FeedTerrenoMove(move_date=fecha, pond_id=pid, feed_type_id=feed_type_id,
                               kg=Decimal(str(kg)), motivo="reparto_recibido", related_move_id=src.id,
                               note=nota, created_by=user_id, created_at=now))
    db.flush()
    return src


def anular_reparto(db: Session, move_id: int) -> None:
    """Deshace un reparto completo (origen y destinos). No hace commit."""
    src = db.get(FeedTerrenoMove, move_id)
    if src is None or src.motivo not in ("reparto", "reparto_recibido"):
        raise ConteoError("Ese movimiento no es un reparto")
    if src.motivo == "reparto_recibido":
        src = db.get(FeedTerrenoMove, src.related_move_id)
    db.query(FeedTerrenoMove).filter(FeedTerrenoMove.related_move_id == src.id,
                                     FeedTerrenoMove.motivo == "reparto_recibido").delete(synchronize_session=False)
    db.delete(src)
    db.flush()


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
