"""Casos de uso del módulo de mantenimiento.

La web del encargado (router.py), la API de Planta (PR3P) y el bot (PR3) llaman
a estas funciones: una regla de negocio no se escribe dos veces, venga el cambio
de donde venga (spec §8.1). Reciben una Session y no hacen commit salvo que se
indique; el que llama decide la transacción.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.maintenance import rules
from app.maintenance.models import (
    SITIOS, MntSistema, MntTipoEquipo, MntContratista, MntPersona, MntEquipo,
    MntEquipoDestino, MntCriticidadEvaluacion, MntParametro, MntAviso, MntOt, MntOtEvento,
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
    if not dar_de_baja:
        # Al reactivarlo, su estado vuelve a salir de sus fallas vigentes.
        recalcular_estado_equipo(db, equipo.id)


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
# Avisos y OT (PR2)
# ---------------------------------------------------------------------------
# Tolerancia para horas "en el futuro": el reloj del teléfono o del PC puede
# ir un par de minutos adelantado respecto del servidor.
_MARGEN_FUTURO = timedelta(minutes=5)
ESTADOS_INICIALES = ("pendiente", "espera_contratista", "espera_repuesto", "en_ejecucion")


def resolver_actor(db: Session, texto: Optional[str]) -> tuple[Optional[int], Optional[str]]:
    """Nombre escrito en «Registra» → (persona_id, nombre). Si coincide con una
    persona (sin mayúsculas) queda vinculado; si no, se guarda el texto tal
    cual. En la web de Crianza no hay login: esto anota, no acredita."""
    t = (texto or "").strip()
    if not t:
        return None, None
    p = db.query(MntPersona).filter(func.lower(MntPersona.nombre) == t.lower()).first()
    return (p.id, p.nombre) if p else (None, t)


def _validar_hora(cuando: Optional[datetime], que: str, no_antes_de: Optional[datetime] = None,
                  referencia: str = "al último registro de la OT") -> datetime:
    """`que` y `referencia` incluyen su preposición ("del cambio", "al último…")."""
    if cuando is None:
        raise ErrorValidacion(f"Falta la hora {que}.")
    if cuando > datetime.now() + _MARGEN_FUTURO:
        raise ErrorValidacion(f"La hora {que} está en el futuro.")
    if no_antes_de and cuando < no_antes_de:
        raise ErrorValidacion(
            f"La hora {que} ({cuando:%d-%m %H:%M}) es anterior {referencia} "
            f"({no_antes_de:%d-%m %H:%M}).")
    return cuando


def plazo_de(db: Session, prioridad: str) -> Optional[Decimal]:
    """Plazo vigente para una prioridad. Se congela en la OT al asignarlo."""
    valor = parametros(db).get(f"plazo_{prioridad.lower()}_horas")
    try:
        return Decimal(str(valor).replace(",", ".")) if valor else None
    except InvalidOperation:
        return None


def crear_aviso(db: Session, *, equipo_id: int, condicion: str, origen: str,
                detectado_at: Optional[datetime] = None, respaldo_entro: Optional[str] = None,
                descripcion: Optional[str] = None, reportado_por_id: Optional[int] = None,
                reportante_texto: Optional[str] = None, foto: Optional[bytes] = None) -> MntAviso:
    """Registra un aviso de falla. La misma función la usará el bot (PR3).

    La entrada nunca rechaza por falta de datos accesorios (spec §6.2): solo
    exige el equipo y la condición, que es lo que calcula la prioridad.
    """
    equipo = db.get(MntEquipo, equipo_id) if equipo_id else None
    if equipo is None:
        raise ErrorValidacion("Elige el equipo que falla.")
    if equipo.estado == "baja":
        raise ErrorValidacion(f"{equipo.codigo} está dado de baja.")
    if condicion not in rules.CONDICIONES:
        raise ErrorValidacion("Indica cómo está el equipo (detenido, con problemas o algo raro).")
    now = datetime.now()
    detectado_at = _validar_hora(detectado_at or now, "de detección")
    # «¿Entró el respaldo?» solo tiene sentido si está detenido y tiene respaldo.
    if condicion != "detenido" or not equipo.respaldo_equipo_id:
        respaldo_entro = None
    elif respaldo_entro not in rules.RESPALDO_ENTRO:
        respaldo_entro = "no_se"
    aviso = MntAviso(
        equipo_id=equipo.id, origen=origen, detectado_at=detectado_at, condicion=condicion,
        respaldo_entro=respaldo_entro, descripcion=(descripcion or "").strip() or None,
        reportado_por_id=reportado_por_id,
        reportante_texto=(reportante_texto or "").strip() or None,
        foto=foto or None,
        prioridad_sugerida=rules.prioridad_sugerida(equipo.criticidad, condicion, respaldo_entro),
        estado="nuevo", created_at=now,
    )
    db.add(aviso)
    db.flush()
    recalcular_estado_equipo(db, equipo.id)
    return aviso


def ot_abierta_de(db: Session, equipo_id: int) -> Optional[MntOt]:
    return (db.query(MntOt)
            .filter(MntOt.equipo_id == equipo_id, MntOt.estado.in_(rules.ESTADOS_ABIERTOS))
            .order_by(MntOt.id).first())


def _evento(db: Session, ot: MntOt, desde: Optional[str], hasta: str, cuando: datetime,
            actor: tuple, nota: Optional[str] = None) -> None:
    db.add(MntOtEvento(ot_id=ot.id, estado_desde=desde, estado_hasta=hasta,
                       ocurrido_at=cuando, registrado_at=datetime.now(),
                       actor_id=actor[0], actor_texto=actor[1],
                       nota=(nota or "").strip()[:300] or None))


def eventos_de(db: Session, ot_id: int) -> list:
    return (db.query(MntOtEvento).filter(MntOtEvento.ot_id == ot_id)
            .order_by(MntOtEvento.ocurrido_at, MntOtEvento.id).all())


def _ultima_hora(db: Session, ot: MntOt) -> datetime:
    ultimo = (db.query(func.max(MntOtEvento.ocurrido_at)).filter(MntOtEvento.ot_id == ot.id).scalar())
    return ultimo or ot.inicio_at


def aceptar_avisos(db: Session, aviso_ids: list[int], *, estado_inicial: str = "pendiente",
                   cuando: Optional[datetime] = None, actor_texto: Optional[str] = None) -> dict:
    """Acuse de recibo en lote (spec §5.3): «recibí estos avisos y los estoy
    atendiendo». Un clic crea las OT y cierra el tramo de reacción de todas
    con la misma hora.

    Nunca deja dos OT abiertas para un mismo equipo: si el equipo ya tiene una,
    el aviso se une a ella; y si en el lote vienen varios avisos del mismo
    equipo, van a una sola OT.
    """
    if estado_inicial not in ESTADOS_INICIALES:
        raise ErrorValidacion("Estado inicial inválido.")
    avisos = (db.query(MntAviso).filter(MntAviso.id.in_(aviso_ids or []), MntAviso.estado == "nuevo")
              .order_by(MntAviso.detectado_at, MntAviso.id).all())
    if not avisos:
        raise ErrorValidacion("Marca al menos un aviso nuevo.")
    cuando = cuando or datetime.now()
    mas_tardio = max(a.detectado_at for a in avisos)
    _validar_hora(cuando, "del acuse", mas_tardio, "a la detección del aviso más reciente")
    actor = resolver_actor(db, actor_texto)

    por_equipo: dict = {}
    for a in avisos:
        por_equipo.setdefault(a.equipo_id, []).append(a)

    creadas, unidos = [], []
    for equipo_id, grupo in por_equipo.items():
        ot = ot_abierta_de(db, equipo_id)
        if ot is not None:
            for a in grupo:
                unir_aviso(db, a, ot, actor_texto=actor_texto)
                unidos.append((a, ot))
            continue
        prioridad = rules.prioridad_mas_alta([a.prioridad_sugerida for a in grupo])
        detenidos = [a.detectado_at for a in grupo if a.condicion == "detenido"]
        ot = MntOt(
            tipo="correctiva", equipo_id=equipo_id, prioridad=prioridad,
            plazo_horas=plazo_de(db, prioridad), estado=estado_inicial,
            inicio_at=min(a.detectado_at for a in grupo),
            equipo_detenido_desde=min(detenidos) if detenidos else None,
            created_at=datetime.now(), updated_at=datetime.now(),
        )
        db.add(ot)
        db.flush()
        _evento(db, ot, "aviso", estado_inicial, cuando, actor,
                "Acuse de recibo" + (f" ({len(grupo)} avisos)" if len(grupo) > 1 else ""))
        for a in grupo:
            a.estado, a.ot_id = "aceptado", ot.id
        creadas.append(ot)
    for equipo_id in por_equipo:
        recalcular_estado_equipo(db, equipo_id)
    return {"creadas": creadas, "unidos": unidos}


def unir_aviso(db: Session, aviso: MntAviso, ot: MntOt, actor_texto: Optional[str] = None) -> None:
    """Suma un aviso duplicado a una OT abierta del mismo equipo.

    Si el aviso es más urgente (el equipo empeoró: de «con problemas» a
    «detenido»), la OT sube de prioridad y queda anotado el motivo.
    """
    if aviso.estado != "nuevo":
        raise ErrorValidacion(f"El aviso {rules.folio_aviso(aviso.id)} ya fue procesado.")
    if ot.estado not in rules.ESTADOS_ABIERTOS or ot.equipo_id != aviso.equipo_id:
        raise ErrorValidacion("Solo se puede unir a una OT abierta del mismo equipo.")
    aviso.estado, aviso.ot_id = "unido", ot.id
    if aviso.condicion == "detenido" and not ot.equipo_detenido_desde:
        ot.equipo_detenido_desde = aviso.detectado_at
    if rules.prioridad_mas_alta([aviso.prioridad_sugerida, ot.prioridad]) != ot.prioridad:
        cambiar_prioridad(db, ot, aviso.prioridad_sugerida,
                          f"Aviso {rules.folio_aviso(aviso.id)} más urgente", actor_texto)
    ot.updated_at = datetime.now()
    recalcular_estado_equipo(db, ot.equipo_id)


def descartar_aviso(db: Session, aviso: MntAviso, motivo: str) -> None:
    if aviso.estado != "nuevo":
        raise ErrorValidacion(f"El aviso {rules.folio_aviso(aviso.id)} ya fue procesado.")
    motivo = (motivo or "").strip()
    if not motivo:
        raise ErrorValidacion("Para descartar un aviso hay que indicar el motivo.")
    aviso.estado, aviso.motivo_descarte = "descartado", motivo[:200]
    recalcular_estado_equipo(db, aviso.equipo_id)


def cambiar_estado(db: Session, ot: MntOt, nuevo: str, *, cuando: Optional[datetime] = None,
                   actor_texto: Optional[str] = None, nota: Optional[str] = None) -> None:
    """Transición de estado con su hora real (`ocurrido_at`, editable).

    La hora no puede ser anterior al último registro de la OT: si lo fuera, los
    tramos quedarían desordenados. Anular exige motivo.
    """
    if not rules.transicion_valida(ot.estado, nuevo):
        raise ErrorValidacion(f"{rules.folio_ot(ot.id)}: no se puede pasar de "
                              f"«{rules.ESTADOS_OT.get(ot.estado, ot.estado)}» a "
                              f"«{rules.ESTADOS_OT.get(nuevo, nuevo)}».")
    cuando = _validar_hora(cuando or datetime.now(), "del cambio", _ultima_hora(db, ot))
    if nuevo == "anulada" and not (nota or "").strip():
        raise ErrorValidacion("Para anular una OT hay que indicar el motivo.")
    _evento(db, ot, ot.estado, nuevo, cuando, resolver_actor(db, actor_texto), nota)
    ot.estado = nuevo
    if nuevo == "anulada":
        ot.cierre_at = cuando
    ot.updated_at = datetime.now()
    recalcular_estado_equipo(db, ot.equipo_id)


def cambiar_prioridad(db: Session, ot: MntOt, prioridad: str, motivo: str,
                      actor_texto: Optional[str] = None) -> bool:
    """Cambia la prioridad con motivo obligatorio y vuelve a congelar el plazo.
    Queda en la bitácora como un evento sin cambio de estado."""
    if prioridad not in rules.PRIORIDADES:
        raise ErrorValidacion("Prioridad inválida.")
    if prioridad == ot.prioridad:
        return False
    motivo = (motivo or "").strip()
    if not motivo:
        raise ErrorValidacion("Para cambiar la prioridad hay que indicar el motivo.")
    antes = ot.prioridad
    ot.prioridad, ot.prioridad_motivo = prioridad, motivo[:200]
    ot.plazo_horas = plazo_de(db, prioridad)
    _evento(db, ot, ot.estado, ot.estado, max(datetime.now(), _ultima_hora(db, ot)),
            resolver_actor(db, actor_texto), f"Prioridad {antes} → {prioridad}: {motivo}")
    ot.updated_at = datetime.now()
    return True


def asignar_ejecutor(db: Session, ot: MntOt, ejecutor_tipo: Optional[str], tecnico_id=None,
                     contratista_id=None, actor_texto: Optional[str] = None) -> None:
    if ejecutor_tipo not in (None, "", "interno", "contratista"):
        raise ErrorValidacion("Ejecutor inválido.")
    tecnico_id = _id_valido(db, MntPersona, tecnico_id, "técnico") if ejecutor_tipo == "interno" else None
    contratista_id = (_id_valido(db, MntContratista, contratista_id, "contratista")
                      if ejecutor_tipo == "contratista" else None)
    if ejecutor_tipo == "interno" and not tecnico_id:
        raise ErrorValidacion("Elige el técnico interno.")
    if ejecutor_tipo == "contratista" and not contratista_id:
        raise ErrorValidacion("Elige el contratista.")
    ot.ejecutor_tipo = ejecutor_tipo or None
    ot.tecnico_id, ot.contratista_id = tecnico_id, contratista_id
    if ejecutor_tipo:
        quien = (db.get(MntPersona, tecnico_id).nombre if tecnico_id
                 else db.get(MntContratista, contratista_id).empresa)
        _evento(db, ot, ot.estado, ot.estado, max(datetime.now(), _ultima_hora(db, ot)),
                resolver_actor(db, actor_texto), f"Ejecutor: {quien}")
    ot.updated_at = datetime.now()


def cerrar_ot(db: Session, ot: MntOt, *, causa: str, accion: str, en_servicio_at: Optional[datetime],
              provisorio: Optional[bool], cuando: Optional[datetime] = None,
              repuestos: Optional[str] = None, trabajo: Optional[str] = None,
              actor_texto: Optional[str] = None) -> None:
    """Cierre de la OT (spec §5.4). Causa y acción en texto libre hasta que
    exista el diccionario; «¿provisorio?» es el único dato estructurado."""
    if ot.estado not in rules.ESTADOS_ABIERTOS:
        raise ErrorValidacion(f"{rules.folio_ot(ot.id)} ya está {rules.ESTADOS_OT[ot.estado].lower()}.")
    causa, accion = (causa or "").strip(), (accion or "").strip()
    if not causa or not accion:
        raise ErrorValidacion("Para cerrar hay que anotar qué falló (causa) y qué se hizo (acción).")
    if provisorio is None:
        raise ErrorValidacion("Indica si el arreglo es provisorio.")
    cuando = _validar_hora(cuando or datetime.now(), "del cierre", _ultima_hora(db, ot))
    en_servicio_at = _validar_hora(en_servicio_at, "de vuelta a servicio", ot.inicio_at,
                                   "a la detección de la falla")
    if en_servicio_at > cuando:
        raise ErrorValidacion("El equipo no puede volver a servicio después del cierre de la OT.")
    _evento(db, ot, ot.estado, "cerrada", cuando, resolver_actor(db, actor_texto),
            "Arreglo provisorio" if provisorio else None)
    ot.estado, ot.cierre_at, ot.equipo_en_servicio_at = "cerrada", cuando, en_servicio_at
    ot.causa_texto, ot.accion_texto, ot.provisorio = causa, accion, bool(provisorio)
    ot.repuestos_texto = (repuestos or "").strip() or None
    ot.trabajo_realizado = (trabajo or "").strip() or None
    ot.updated_at = datetime.now()
    recalcular_estado_equipo(db, ot.equipo_id)


def crear_ot_definitiva(db: Session, origen: MntOt, actor_texto: Optional[str] = None) -> MntOt:
    """OT del arreglo definitivo tras uno provisorio. El equipo funciona, así
    que nace como P3 (se planifica) y su reloj parte con el cierre del
    provisorio: el tiempo que tarde en hacerse el definitivo también se mide."""
    if origen.estado != "cerrada" or not origen.provisorio:
        raise ErrorValidacion("Solo una OT cerrada con arreglo provisorio genera la del definitivo.")
    if db.query(MntOt).filter(MntOt.ot_origen_id == origen.id).first():
        raise ErrorValidacion(f"{rules.folio_ot(origen.id)} ya tiene su OT del arreglo definitivo.")
    ot = MntOt(tipo="correctiva", equipo_id=origen.equipo_id, prioridad="P3",
               prioridad_motivo=f"Arreglo definitivo de {rules.folio_ot(origen.id)}",
               plazo_horas=plazo_de(db, "P3"), estado="pendiente", inicio_at=origen.cierre_at,
               ot_origen_id=origen.id, created_at=datetime.now(), updated_at=datetime.now())
    db.add(ot)
    db.flush()
    _evento(db, ot, "aviso", "pendiente", origen.cierre_at, resolver_actor(db, actor_texto),
            f"Definitivo de {rules.folio_ot(origen.id)}: {origen.accion_texto or ''}")
    return ot


def recalcular_estado_equipo(db: Session, equipo_id: int) -> None:
    """El estado del equipo sale de sus fallas vigentes (spec §3.1): detenido o
    degradado mientras tenga un aviso sin atender o una OT abierta con esa
    condición; operativo si no. Una OT en «reparada» ya no lo detiene. Un
    equipo dado de baja no se toca."""
    equipo = db.get(MntEquipo, equipo_id)
    if equipo is None or equipo.estado == "baja":
        return
    db.flush()
    condiciones = {c for (c,) in (
        db.query(MntAviso.condicion)
        .outerjoin(MntOt, MntOt.id == MntAviso.ot_id)
        .filter(MntAviso.equipo_id == equipo_id)
        .filter((MntAviso.estado == "nuevo")
                | (MntOt.estado.in_([e for e in rules.ESTADOS_ABIERTOS if e != "reparada"])))
        .all())}
    nuevo = ("detenido" if "detenido" in condiciones
             else "degradado" if "degradado" in condiciones else "operativo")
    if equipo.estado != nuevo:
        equipo.estado = nuevo
        equipo.updated_at = datetime.now()


def guardar_parte(db: Session, filas: list[dict], actor_texto: Optional[str] = None) -> tuple[int, list[str]]:
    """Parte diario (spec §5.3): una sola pantalla y un solo guardar.

    Cada fila trae {ot_id, estado, cuando, nota}. Una fila sin estado nuevo ni
    nota se ignora («sin cambios» no exige clic). Solo nota = anotación en la
    bitácora sin cambiar de estado. Cada fila se aplica por separado: si una
    falla (hora inválida, transición imposible), las demás se guardan igual y
    el error se informa con su folio.
    """
    aplicadas, errores = 0, []
    for f in filas:
        nuevo, nota = (f.get("estado") or "").strip(), (f.get("nota") or "").strip()
        if not nuevo and not nota:
            continue
        ot = db.get(MntOt, f["ot_id"])
        if ot is None:
            continue
        sp = db.begin_nested()
        try:
            if nuevo:
                cambiar_estado(db, ot, nuevo, cuando=f.get("cuando"), actor_texto=actor_texto, nota=nota)
            else:
                cuando = _validar_hora(f.get("cuando") or datetime.now(), "de la nota", _ultima_hora(db, ot))
                _evento(db, ot, ot.estado, ot.estado, cuando, resolver_actor(db, actor_texto), nota)
                ot.updated_at = datetime.now()
            sp.commit()
            aplicadas += 1
        except ErrorValidacion as e:
            sp.rollback()
            errores.append(f"{rules.folio_ot(ot.id)}: {e}")
    return aplicadas, errores


def tramos_de(ot: MntOt, eventos: list, ahora: Optional[datetime] = None) -> dict:
    """Desglose del tiempo de una OT (horas por tramo + total)."""
    fin = ot.cierre_at or ahora or datetime.now()
    return rules.tramos(ot.inicio_at, [(e.estado_hasta, e.ocurrido_at) for e in eventos], fin)


def ot_fuera_de_plazo(ot: MntOt, ahora: Optional[datetime] = None) -> bool:
    if ot.estado == "anulada":
        return False
    fin = ot.equipo_en_servicio_at or ot.cierre_at or ahora or datetime.now()
    return rules.fuera_de_plazo(ot.inicio_at, ot.plazo_horas, fin)


def avisos_nuevos(db: Session, sitio: Optional[str] = None) -> list:
    q = db.query(MntAviso).join(MntEquipo, MntEquipo.id == MntAviso.equipo_id).filter(MntAviso.estado == "nuevo")
    if sitio:
        q = q.filter(MntEquipo.sitio == sitio)
    return q.order_by(MntAviso.prioridad_sugerida, MntAviso.detectado_at).all()


def ots(db: Session, *, sitio: Optional[str] = None, abiertas: bool = True,
        desde: Optional[datetime] = None) -> list:
    q = db.query(MntOt).join(MntEquipo, MntEquipo.id == MntOt.equipo_id)
    if sitio:
        q = q.filter(MntEquipo.sitio == sitio)
    if abiertas:
        q = q.filter(MntOt.estado.in_(rules.ESTADOS_ABIERTOS)).order_by(MntOt.prioridad, MntOt.inicio_at)
    else:
        q = q.filter(MntOt.estado.in_(rules.ESTADOS_FINALES))
        if desde:
            q = q.filter(MntOt.cierre_at >= desde)
        q = q.order_by(MntOt.cierre_at.desc())
    return q.all()


def eventos_por_ot(db: Session, ot_ids: list[int]) -> dict:
    """Eventos de varias OT en una sola consulta (para el tablero)."""
    out: dict = {i: [] for i in ot_ids}
    if ot_ids:
        for e in (db.query(MntOtEvento).filter(MntOtEvento.ot_id.in_(ot_ids))
                  .order_by(MntOtEvento.ocurrido_at, MntOtEvento.id).all()):
            out[e.ot_id].append(e)
    return out


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
