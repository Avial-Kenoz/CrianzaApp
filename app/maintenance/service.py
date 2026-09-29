"""Casos de uso del módulo de mantenimiento.

La web del encargado (router.py), la API de Planta (PR3P) y el bot (PR3) llaman
a estas funciones: una regla de negocio no se escribe dos veces, venga el cambio
de donde venga (spec §8.1). Reciben una Session y no hacen commit salvo que se
indique; el que llama decide la transacción.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Optional

from sqlalchemy.orm import Session

from app.maintenance import rules
from app.maintenance.models import (
    SITIOS, MntSistema, MntTipoEquipo, MntContratista, MntPersona, MntEquipo,
    MntEquipoDestino, MntCriticidadEvaluacion, MntParametro,
)


class ErrorValidacion(ValueError):
    """Dato de entrada inválido; el mensaje se muestra tal cual al usuario."""


ROLES_PERSONA = {
    "encargado": "Encargado de mantenimiento",
    "tecnico": "Técnico interno",
    "supervisor_planta": "Supervisor de Planta",
    "reportante": "Reportante",
}
ESTADOS_EQUIPO = {
    "operativo": "Operativo",
    "degradado": "Degradado",
    "detenido": "Detenido",
    "baja": "Dado de baja",
}
# Campos de la ficha que se copian tal cual desde el formulario.
_CAMPOS_TEXTO = ("nombre", "ubicacion_texto", "marca", "modelo", "serie", "voltaje", "notas")


# ---------------------------------------------------------------------------
# Parámetros
# ---------------------------------------------------------------------------
def parametros(db: Session) -> dict:
    return {p.clave: p.valor for p in db.query(MntParametro).all()}


def actualizar_parametros(db: Session, valores: dict) -> int:
    """Guarda solo las claves que existen; ignora el resto. Devuelve cuántas cambió."""
    cambios = 0
    now = datetime.now()
    for p in db.query(MntParametro).all():
        if p.clave not in valores:
            continue
        nuevo = (valores[p.clave] or "").strip()
        if p.clave.endswith("_horas") and p.clave != "resumen_horas":
            _decimal_positivo(nuevo, p.descripcion or p.clave)
        elif p.clave == "resumen_horas" and nuevo:
            for h in nuevo.split(","):
                try:
                    rules.parse_hora(h)
                except ValueError:
                    raise ErrorValidacion(f"Hora de resumen inválida: «{h.strip()}». Usa hh:mm, p. ej. 8:30, 14:00.")
        elif p.clave == "inicio_registro" and nuevo:
            try:
                datetime.strptime(nuevo, "%Y-%m-%d")
            except ValueError:
                raise ErrorValidacion("La fecha de inicio del registro debe ser una fecha válida.")
        if nuevo != (p.valor or ""):
            p.valor = nuevo
            p.updated_at = now
            cambios += 1
    return cambios


def _decimal_positivo(valor: str, nombre: str) -> Decimal:
    try:
        d = Decimal(str(valor).replace(",", "."))
    except (InvalidOperation, ValueError):
        raise ErrorValidacion(f"«{nombre}» debe ser un número.")
    if d <= 0:
        raise ErrorValidacion(f"«{nombre}» debe ser mayor que cero.")
    return d


# ---------------------------------------------------------------------------
# Catálogos simples: sistemas, tipos, contratistas
# ---------------------------------------------------------------------------
def sistemas(db: Session, sitio: Optional[str] = None, solo_activos: bool = True) -> list:
    q = db.query(MntSistema)
    if solo_activos:
        q = q.filter(MntSistema.activo.is_(True))
    if sitio:
        q = q.filter((MntSistema.sitio == sitio) | (MntSistema.sitio.is_(None)))
    return q.order_by(MntSistema.orden, MntSistema.nombre).all()


def tipos(db: Session, solo_activos: bool = True) -> list:
    q = db.query(MntTipoEquipo)
    if solo_activos:
        q = q.filter(MntTipoEquipo.activo.is_(True))
    return q.order_by(MntTipoEquipo.nombre).all()


def contratistas(db: Session, solo_activos: bool = True) -> list:
    q = db.query(MntContratista)
    if solo_activos:
        q = q.filter(MntContratista.activo.is_(True))
    return q.order_by(MntContratista.empresa).all()


def crear_sistema(db: Session, nombre: str, sitio: Optional[str], orden: Optional[int]) -> MntSistema:
    nombre = (nombre or "").strip()
    if not nombre:
        raise ErrorValidacion("El sistema necesita un nombre.")
    if db.query(MntSistema).filter(MntSistema.nombre.ilike(nombre)).first():
        raise ErrorValidacion(f"Ya existe el sistema «{nombre}».")
    s = MntSistema(nombre=nombre, sitio=_sitio_o_none(sitio), orden=orden or 0, activo=True)
    db.add(s)
    return s


def crear_tipo(db: Session, nombre: str) -> MntTipoEquipo:
    nombre = (nombre or "").strip()
    if not nombre:
        raise ErrorValidacion("El tipo de equipo necesita un nombre.")
    if db.query(MntTipoEquipo).filter(MntTipoEquipo.nombre.ilike(nombre)).first():
        raise ErrorValidacion(f"Ya existe el tipo «{nombre}».")
    t = MntTipoEquipo(nombre=nombre, activo=True)
    db.add(t)
    return t


def actualizar_sistema(db: Session, sistema: MntSistema, nombre: str, sitio: Optional[str],
                       orden: Optional[int]) -> MntSistema:
    nombre = (nombre or "").strip()
    if not nombre:
        raise ErrorValidacion("El sistema necesita un nombre.")
    otro = db.query(MntSistema).filter(MntSistema.nombre.ilike(nombre), MntSistema.id != sistema.id).first()
    if otro:
        raise ErrorValidacion(f"Ya existe el sistema «{otro.nombre}». Si son el mismo, fusiónalos.")
    sistema.nombre = nombre
    sistema.sitio = _sitio_o_none(sitio)
    if orden is not None:
        sistema.orden = orden
    return sistema


def fusionar(db: Session, que: str, origen_id: int, destino_id: int) -> tuple[str, str, int]:
    """Mueve los equipos de un sistema (o tipo) duplicado a otro y desactiva el
    sobrante. No lo borra: si el diccionario o una OT futura lo referencian,
    la referencia sigue siendo válida.

    Devuelve (nombre origen, nombre destino, equipos movidos).
    """
    modelo, columna = {
        "sistemas": (MntSistema, MntEquipo.sistema_id),
        "tipos": (MntTipoEquipo, MntEquipo.tipo_id),
    }[que]
    if origen_id == destino_id:
        raise ErrorValidacion("Elige un destino distinto para fusionar.")
    origen, destino = db.get(modelo, origen_id), db.get(modelo, destino_id)
    if origen is None or destino is None:
        raise ErrorValidacion("El elemento a fusionar no existe.")
    movidos = (db.query(MntEquipo).filter(columna == origen_id)
               .update({columna: destino_id}, synchronize_session=False))
    origen.activo = False
    destino.activo = True
    return origen.nombre, destino.nombre, movidos


def guardar_contratista(db: Session, datos: dict, contratista: Optional[MntContratista] = None) -> MntContratista:
    empresa = (datos.get("empresa") or "").strip()
    if not empresa:
        raise ErrorValidacion("El contratista necesita el nombre de la empresa.")
    now = datetime.now()
    if contratista is None:
        contratista = MntContratista(created_at=now, activo=True)
        db.add(contratista)
    contratista.empresa = empresa
    for campo in ("contacto", "telefono", "especialidad", "notas"):
        setattr(contratista, campo, (datos.get(campo) or "").strip() or None)
    contratista.updated_at = now
    return contratista


# ---------------------------------------------------------------------------
# Personas
# ---------------------------------------------------------------------------
def personas(db: Session, solo_activas: bool = True) -> list:
    q = db.query(MntPersona)
    if solo_activas:
        q = q.filter(MntPersona.activo.is_(True))
    return q.order_by(MntPersona.nombre).all()


def guardar_persona(db: Session, datos: dict, persona: Optional[MntPersona] = None) -> MntPersona:
    nombre = (datos.get("nombre") or "").strip()
    if not nombre:
        raise ErrorValidacion("La persona necesita un nombre.")
    rol = datos.get("rol") or "reportante"
    if rol not in ROLES_PERSONA:
        raise ErrorValidacion("Rol inválido.")

    telegram = (datos.get("telegram_user_id") or "").strip() or None
    if telegram:
        otra = db.query(MntPersona).filter(MntPersona.telegram_user_id == telegram).first()
        if otra and (persona is None or otra.id != persona.id):
            raise ErrorValidacion(f"El ID de Telegram {telegram} ya está asociado a {otra.nombre}.")

    alarmas = [s for s in (datos.get("alarmas_sitios") or []) if s in SITIOS]
    dias = sorted({int(d) for d in (datos.get("horario_dias") or []) if str(d).isdigit() and 0 <= int(d) <= 6})
    try:
        desde = rules.parse_hora(datos.get("horario_desde"))
        hasta = rules.parse_hora(datos.get("horario_hasta"))
    except ValueError:
        raise ErrorValidacion("Horario inválido. Usa hh:mm, p. ej. 8:30 y 17:30.")
    recibe_resumen = bool(datos.get("recibe_resumen"))
    if recibe_resumen and (desde is None or hasta is None or not dias):
        # Sin horario, el resumen no tendría cuándo salir: mejor decirlo al
        # guardar que descubrirlo cuando nadie recibe nada.
        raise ErrorValidacion("Para recibir el resumen hay que indicar días y horario.")

    now = datetime.now()
    if persona is None:
        persona = MntPersona(created_at=now, activo=True, bot_iniciado=False)
        db.add(persona)
    persona.nombre = nombre
    persona.rol = rol
    persona.sitio = _sitio_o_none(datos.get("sitio"))
    if telegram != persona.telegram_user_id:
        # Un ID nuevo no está verificado hasta que esa persona le escriba al bot.
        persona.bot_iniciado = False
    persona.telegram_user_id = telegram
    persona.alarmas_sitios = ",".join(s for s in SITIOS if s in alarmas) or None
    persona.recibe_resumen = recibe_resumen
    persona.horario_dias = ",".join(str(d) for d in dias) or None
    persona.horario_desde = desde
    persona.horario_hasta = hasta
    persona.planta_username = (datos.get("planta_username") or "").strip() or None
    persona.updated_at = now
    return persona


def advertencias_personas(db: Session) -> list[str]:
    """Problemas de configuración que harían perder una alarma (spec §6.4)."""
    out = []
    activas = personas(db)
    for sitio in SITIOS:
        if not any(sitio in (p.alarmas_sitios or "").split(",") for p in activas):
            out.append(f"Nadie recibe las alarmas P1 de {sitio.capitalize()}. "
                       f"Marca al encargado de mantenimiento en «Recibe alarmas de».")
    for p in activas:
        if p.alarmas_sitios and not p.telegram_user_id:
            out.append(f"{p.nombre} recibe alarmas pero no tiene ID de Telegram: no le llegará nada.")
        elif p.alarmas_sitios and not p.bot_iniciado:
            out.append(f"{p.nombre} recibe alarmas pero todavía no le ha escrito al bot de "
                       f"mantenimiento (/start). Hasta que lo haga, el bot no puede escribirle.")
    if not any(p.recibe_resumen for p in activas):
        out.append("Nadie recibe el resumen de fallas menores.")
    return out


# ---------------------------------------------------------------------------
# Equipos
# ---------------------------------------------------------------------------
def equipos(db: Session, sitio: Optional[str] = None, sistema_id: Optional[int] = None,
            incluir_baja: bool = False) -> list:
    q = db.query(MntEquipo)
    if sitio:
        q = q.filter(MntEquipo.sitio == sitio)
    if sistema_id:
        q = q.filter(MntEquipo.sistema_id == sistema_id)
    if not incluir_baja:
        q = q.filter(MntEquipo.estado != "baja")
    return q.order_by(MntEquipo.codigo).all()


def ultima_evaluacion(db: Session, equipo_id: int) -> Optional[MntCriticidadEvaluacion]:
    return (db.query(MntCriticidadEvaluacion)
            .filter(MntCriticidadEvaluacion.equipo_id == equipo_id)
            .order_by(MntCriticidadEvaluacion.evaluado_at.desc(), MntCriticidadEvaluacion.id.desc())
            .first())


def historial_criticidad(db: Session, equipo_id: int) -> list:
    return (db.query(MntCriticidadEvaluacion)
            .filter(MntCriticidadEvaluacion.equipo_id == equipo_id)
            .order_by(MntCriticidadEvaluacion.evaluado_at.desc(), MntCriticidadEvaluacion.id.desc())
            .all())


def crear_equipo(db: Session, datos: dict, respuestas: dict, *, origen: str, autor: Optional[str]) -> MntEquipo:
    """Alta de un equipo con su encuesta de criticidad (obligatoria, spec §4).

    Valida la encuesta ANTES de tocar la BD: sin encuesta completa no hay alta.
    """
    sitio = datos.get("sitio")
    if sitio not in SITIOS:
        raise ErrorValidacion("Elige el sitio del equipo (Crianza o Planta).")
    try:
        criticidad, tolerancia = rules.calcular_criticidad(respuestas)
    except rules.RespuestasInvalidas:
        raise ErrorValidacion("Responde las cuatro preguntas de criticidad: sin ellas el equipo no se puede dar de alta.")

    now = datetime.now()
    codigos = [c for (c,) in db.query(MntEquipo.codigo).all()]
    equipo = MntEquipo(
        codigo=rules.siguiente_codigo(codigos),
        sitio=sitio,
        estado="operativo",
        alta_origen=origen,
        alta_por=(autor or "").strip() or None,
        created_at=now,
    )
    _aplicar_ficha(db, equipo, datos)
    db.add(equipo)
    db.flush()
    _guardar_destinos(db, equipo, datos.get("destinos") or [])
    _registrar_evaluacion(db, equipo, respuestas, criticidad, tolerancia, autor, now)
    return equipo


def actualizar_equipo(db: Session, equipo: MntEquipo, datos: dict) -> MntEquipo:
    """Edita la ficha. El sitio no cambia: un equipo no se muda de Crianza a
    Planta, y cambiarlo rompería la Q1 con que se evaluó su criticidad."""
    _aplicar_ficha(db, equipo, datos)
    _guardar_destinos(db, equipo, datos.get("destinos"))
    equipo.updated_at = datetime.now()
    return equipo


def reevaluar_criticidad(db: Session, equipo: MntEquipo, respuestas: dict, autor: Optional[str]) -> bool:
    """Registra una nueva evaluación solo si las respuestas cambiaron.

    Devuelve True si hubo evaluación nueva. Nunca borra la anterior.
    """
    try:
        limpio = rules.validar_respuestas(respuestas)
        criticidad, tolerancia = rules.calcular_criticidad(limpio)
    except rules.RespuestasInvalidas:
        raise ErrorValidacion("Responde las cuatro preguntas de criticidad.")
    previa = ultima_evaluacion(db, equipo.id)
    if previa and previa.respuestas == limpio and previa.regla_version == rules.REGLA_CRITICIDAD_VERSION:
        return False
    _registrar_evaluacion(db, equipo, limpio, criticidad, tolerancia, autor, datetime.now())
    return True


def datos_redundancia(db: Session, original: MntEquipo) -> tuple[dict, dict]:
    """Datos para precargar el alta de una redundancia (copia de un equipo).

    Copia lo que comparten dos equipos gemelos y deja fuera lo que es propio de
    cada unidad física (serie, foto, fecha de instalación). La copia queda con
    el original como respaldo; el vínculo inverso lo decide el alta (§ respaldo
    mutuo en `vincular_respaldo_mutuo`). Devuelve (datos de ficha, respuestas
    de criticidad del original).
    """
    nombres = [n for (n,) in db.query(MntEquipo.nombre).all()]
    datos = {
        "sitio": original.sitio,
        "nombre": rules.nombre_redundancia(original.nombre, nombres),
        "sistema_id": original.sistema_id,
        "tipo_id": original.tipo_id,
        "ubicacion_texto": original.ubicacion_texto,
        "destinos": destinos_de(db, original.id),
        "marca": original.marca,
        "modelo": original.modelo,
        "potencia_kw": original.potencia_kw,
        "voltaje": original.voltaje,
        "contratista_habitual_id": original.contratista_habitual_id,
        "notas": original.notas,
        "respaldo_equipo_id": original.id,
    }
    ev = ultima_evaluacion(db, original.id)
    return datos, dict(ev.respuestas) if ev else {}


def vincular_respaldo_mutuo(db: Session, original: MntEquipo, nuevo: MntEquipo) -> bool:
    """Deja al original respaldado por su redundancia, si no tenía respaldo.

    Si ya tenía uno (p. ej. al crear la tercera unidad de un trío), no se pisa:
    cambiar el respaldo de un equipo existente sin que nadie lo decida sería
    un cambio silencioso. Devuelve True si vinculó.
    """
    if original.sitio != nuevo.sitio or original.respaldo_equipo_id:
        return False
    original.respaldo_equipo_id = nuevo.id
    original.updated_at = datetime.now()
    return True


def respaldo_incoherente(equipo: MntEquipo, evaluacion: Optional[MntCriticidadEvaluacion]) -> bool:
    """El equipo tiene un respaldo registrado pero su encuesta dice «sin
    respaldo»: la criticidad está calculada sobre un supuesto que ya no vale."""
    return bool(equipo.respaldo_equipo_id and evaluacion
                and (evaluacion.respuestas or {}).get("respaldo") == "a")


def catalogo_tecnico(db: Session) -> list[dict]:
    """Marca/modelo ya ingresados, con sus datos técnicos, para autocompletar.

    Uno por combinación marca+modelo (el más reciente), incluidos los dados de
    baja: un equipo retirado sigue sirviendo de referencia para su reemplazo.
    """
    vistos: dict = {}
    for e in db.query(MntEquipo).filter(MntEquipo.marca.isnot(None)).order_by(MntEquipo.id).all():
        clave = ((e.marca or "").strip().lower(), (e.modelo or "").strip().lower())
        vistos[clave] = {
            "marca": e.marca, "modelo": e.modelo or "", "codigo": e.codigo,
            "tipo_id": e.tipo_id,
            "potencia_kw": str(e.potencia_kw) if e.potencia_kw is not None else "",
            "voltaje": e.voltaje or "",
        }
    return sorted(vistos.values(), key=lambda x: (x["marca"].lower(), x["modelo"].lower()))


def cambiar_baja(db: Session, equipo: MntEquipo, dar_de_baja: bool) -> None:
    equipo.estado = "baja" if dar_de_baja else "operativo"
    equipo.updated_at = datetime.now()


def guardar_foto(equipo: MntEquipo, contenido: bytes, mime: str) -> None:
    # El navegador ya la reduce a ~1280 px antes de subirla (ver plantilla), así
    # que acá solo se pone un techo por si llega una sin reducir.
    if len(contenido) > 3 * 1024 * 1024:
        raise ErrorValidacion("La foto pesa más de 3 MB. Prueba de nuevo desde el navegador (la reduce solo).")
    if mime not in ("image/jpeg", "image/png", "image/webp"):
        raise ErrorValidacion("La foto debe ser JPG, PNG o WebP.")
    equipo.foto = contenido
    equipo.foto_mime = mime
    equipo.updated_at = datetime.now()


def _registrar_evaluacion(db, equipo, respuestas, criticidad, tolerancia, autor, cuando) -> None:
    db.add(MntCriticidadEvaluacion(
        equipo_id=equipo.id,
        respuestas=rules.validar_respuestas(respuestas),
        resultado=criticidad,
        tolerancia_horas=tolerancia,
        regla_version=rules.REGLA_CRITICIDAD_VERSION,
        evaluado_por=(autor or "").strip() or None,
        evaluado_at=cuando,
    ))
    equipo.criticidad = criticidad


def _aplicar_ficha(db: Session, equipo: MntEquipo, datos: dict) -> None:
    nombre = (datos.get("nombre") or "").strip()
    if not nombre:
        raise ErrorValidacion("El equipo necesita un nombre.")
    for campo in _CAMPOS_TEXTO:
        setattr(equipo, campo, (datos.get(campo) or "").strip() or None)

    equipo.tipo_id = _id_valido(db, MntTipoEquipo, datos.get("tipo_id"), "tipo de equipo")
    equipo.sistema_id = _id_valido(db, MntSistema, datos.get("sistema_id"), "sistema")
    equipo.contratista_habitual_id = _id_valido(db, MntContratista, datos.get("contratista_habitual_id"), "contratista")

    respaldo_id = _id_o_none(datos.get("respaldo_equipo_id"))
    if respaldo_id is not None:
        respaldo = db.get(MntEquipo, respaldo_id)
        if respaldo is None:
            raise ErrorValidacion("El equipo de respaldo no existe.")
        if equipo.id is not None and respaldo.id == equipo.id:
            raise ErrorValidacion("Un equipo no puede ser su propio respaldo.")
        if respaldo.sitio != equipo.sitio:
            raise ErrorValidacion("El respaldo debe ser un equipo del mismo sitio.")
    equipo.respaldo_equipo_id = respaldo_id

    potencia = (datos.get("potencia_kw") or "").strip()
    if potencia:
        try:
            equipo.potencia_kw = Decimal(potencia.replace(",", "."))
        except InvalidOperation:
            raise ErrorValidacion("La potencia debe ser un número (kW).")
    else:
        equipo.potencia_kw = None

    fecha = (datos.get("fecha_instalacion") or "").strip()
    try:
        equipo.fecha_instalacion = datetime.strptime(fecha, "%Y-%m-%d").date() if fecha else None
    except ValueError:
        raise ErrorValidacion("La fecha de instalación no es válida.")



def _guardar_destinos(db: Session, equipo: MntEquipo, refs: Optional[list]) -> None:
    """Reemplaza los destinos del equipo. Solo Crianza tiene destinos (sirven
    para el cruce con calidad de agua de F4); en Planta se descartan.

    `refs` None significa "el formulario no trae destinos": no se tocan.
    """
    if refs is None:
        return
    db.query(MntEquipoDestino).filter(MntEquipoDestino.equipo_id == equipo.id).delete()
    if equipo.sitio != "crianza":
        return
    for tipo, ident in rules.normalizar_destinos(refs, _unidad_de_estanque(db)):
        db.add(MntEquipoDestino(equipo_id=equipo.id, ref_tipo=tipo, ref_id=ident))


def destinos_de(db: Session, equipo_id: int) -> list[str]:
    return [f"{d.ref_tipo}:{d.ref_id}" for d in
            db.query(MntEquipoDestino).filter(MntEquipoDestino.equipo_id == equipo_id)
            .order_by(MntEquipoDestino.id).all()]


def _unidad_de_estanque(db: Session) -> dict[str, str]:
    from app.models.ponds import Pond
    return {str(pid): str(uid) for pid, uid in db.query(Pond.id, Pond.cultivation_unit_id).all() if uid}


def _id_o_none(valor) -> Optional[int]:
    try:
        return int(str(valor).strip()) if valor not in (None, "") else None
    except ValueError:
        return None


def _id_valido(db: Session, modelo, valor, nombre: str) -> Optional[int]:
    i = _id_o_none(valor)
    if i is not None and db.get(modelo, i) is None:
        raise ErrorValidacion(f"El {nombre} elegido no existe.")
    return i


def _sitio_o_none(valor) -> Optional[str]:
    return valor if valor in SITIOS else None


# ---------------------------------------------------------------------------
# Destinos disponibles (solo Crianza)
# ---------------------------------------------------------------------------
def destinos_disponibles(db: Session) -> list[dict]:
    """Unidades de cultivo con sus estanques activos, para marcar a qué atiende
    un equipo. Estanques sin unidad van en un grupo aparte (ref None).

    Referencia blanda `unit:ID` / `pond:ID`: si un estanque desaparece, el
    equipo sigue en pie (spec §3).
    """
    from app.models.ponds import Pond
    from app.models.cultivation_units import CultivationUnit

    ponds = (db.query(Pond)
             .filter(Pond.state != "inactive", Pond.parent_pond_id.is_(None))
             .order_by(Pond.name).all())
    grupos = []
    for u in db.query(CultivationUnit).order_by(CultivationUnit.name).all():
        suyos = [{"ref": f"pond:{p.id}", "nombre": p.name} for p in ponds if p.cultivation_unit_id == u.id]
        grupos.append({"ref": f"unit:{u.id}", "nombre": u.name, "estanques": suyos})
    sueltos = [{"ref": f"pond:{p.id}", "nombre": p.name} for p in ponds if not p.cultivation_unit_id]
    if sueltos:
        grupos.append({"ref": None, "nombre": "Sin unidad", "estanques": sueltos})
    return grupos


def etiquetas_destinos(db: Session, refs: list[str]) -> list[str]:
    """Texto legible de los destinos guardados, en el orden guardado."""
    if not refs:
        return []
    nombres = {"sitio:crianza": "Todo Crianza"}
    for g in destinos_disponibles(db):
        if g["ref"]:
            nombres[g["ref"]] = f"Unidad {g['nombre']} (completa)"
        for p in g["estanques"]:
            nombres[p["ref"]] = f"Estanque {p['nombre']}"
    # Un estanque dado de baja ya no está en la lista: se muestra su ref cruda
    # en vez de esconder el vínculo.
    return [nombres.get(r, r) for r in refs]
