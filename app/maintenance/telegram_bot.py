"""Bot de mantenimiento: aviso de falla en menos de 30 segundos (spec §6).

Dos piezas:

- `Bot`: convierte cada update de Telegram (mensaje o botón) en acciones.
  Recibe la API y la fábrica de sesiones por parámetro, así se prueba sin red
  (`test_bot.py`). Crea el aviso con `service.crear_aviso`, la misma función
  que usa la web: una regla no se escribe dos veces.
- `Lector`: hilo que lee con long polling (`getUpdates`) y guarda el último
  update procesado en `mnt_parametros._bot_offset`, para no reprocesar
  mensajes tras un reinicio.

⚠️ Telegram admite **un solo lector por token**. Solo arranca con
`MNT_BOT_ENABLED=1` (producción). Si dos procesos leen, se roban los mensajes
(HTTP 409) y los avisos se pierden sin error visible (spec §6.1).

Conversación (spec §6.2):
  QR → /start EQ-012  ─┐
  /falla → sitio → sistema → equipo ─┴→ condición → [¿entró el respaldo?]
  → «¿qué pasa?» (texto, foto o audio, varias veces) → [Terminar] → aviso.
Una conversación sin actividad por 10 min se guarda igual si ya tenía la
condición («un aviso a medias vale más que ninguno»); si no, se descarta.
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Optional

from app.db.session import SessionLocal
from app.maintenance import notify, rules, service
from app.maintenance.models import SITIO_LABELS, SITIOS, MntAviso, MntEquipo, MntParametro, MntSistema
from app.maintenance.service import ErrorValidacion
from app.maintenance.telegram_api import Api, TelegramError, configurado

logger = logging.getLogger("mnt_bot")

EXPIRA = timedelta(minutes=10)
MAX_FOTO = 3 * 1024 * 1024
MAX_AUDIO = 2 * 1024 * 1024
AYUDA = ("Para avisar una falla, escanea el QR pegado en el equipo o escribe /falla.\n"
         "/cancelar anula el aviso que estás escribiendo.")


@dataclass
class Conversacion:
    chat_id: str
    user_id: str
    nombre_tg: str
    persona_id: Optional[int] = None
    persona_nombre: Optional[str] = None
    paso: str = "sitio"                 # sitio · sistema · equipo · condicion · respaldo · detalle
    sitio: Optional[str] = None
    equipo_id: Optional[int] = None
    condicion: Optional[str] = None
    respaldo_entro: Optional[str] = None
    textos: list = field(default_factory=list)
    foto: Optional[bytes] = None
    audio: Optional[bytes] = None
    inicio: datetime = field(default_factory=datetime.now)
    ultima: datetime = field(default_factory=datetime.now)


class Bot:
    def __init__(self, api, sesion: Callable = SessionLocal, reloj: Callable = datetime.now,
                 alarmar: Optional[Callable] = None):
        self.api = api
        self.sesion = sesion
        self.reloj = reloj
        # Cómo mandar la alarma P1 tras guardar. Por defecto, en la misma
        # sesión y con la misma API (el lector ya corre en su propio hilo).
        self.alarmar = alarmar or (lambda db, aviso_id: notify.alarma_aviso(db, aviso_id, self.api))
        self.convs: dict[str, Conversacion] = {}

    # ------------------------------------------------------------------ entrada
    def procesar(self, update: dict) -> None:
        if "callback_query" in update:
            self._boton(update["callback_query"])
        elif "message" in update:
            self._mensaje(update["message"])

    def _contacto(self, db, u: dict):
        nombre = " ".join(x for x in [u.get("first_name"), u.get("last_name")] if x) or u.get("username") or ""
        c, p = service.registrar_contacto(db, str(u["id"]), nombre, u.get("username"))
        db.commit()
        return c, p, nombre

    def _mensaje(self, m: dict) -> None:
        chat = m.get("chat") or {}
        if chat.get("type") != "private":
            return                                  # el bot no conversa en grupos
        chat_id, u = str(chat["id"]), m.get("from") or {}
        db = self.sesion()
        try:
            contacto, persona, nombre = self._contacto(db, u)
            if contacto.bloqueado:
                return
            texto = (m.get("text") or "").strip()
            conv = self.convs.get(chat_id)
            if conv:
                conv.ultima = self.reloj()

            if texto.startswith("/start"):
                partes = texto.split(maxsplit=1)
                if len(partes) > 1:
                    return self._iniciar_con_equipo(db, chat_id, u, nombre, persona, partes[1].strip())
                saludo = f"Hola {persona.nombre if persona else nombre}. " if (persona or nombre) else "Hola. "
                return self.api.enviar(chat_id, saludo + "Este es el bot de mantenimiento.\n" + AYUDA)
            if texto.startswith("/falla"):
                return self._iniciar_seleccion(db, chat_id, u, nombre, persona)
            if texto.startswith("/cancelar"):
                if self.convs.pop(chat_id, None):
                    return self.api.enviar(chat_id, "Aviso cancelado.")
                return self.api.enviar(chat_id, "No había un aviso en curso.")

            if conv and conv.paso == "detalle":
                return self._detalle(conv, m, texto)
            if conv:
                return self.api.enviar(chat_id, "Responde con los botones de arriba, o /cancelar.")
            return self.api.enviar(chat_id, AYUDA)
        finally:
            db.close()

    # ---------------------------------------------------------- inicio del aviso
    def _nueva_conv(self, chat_id, u, nombre, persona) -> Conversacion:
        ahora = self.reloj()
        conv = Conversacion(chat_id=chat_id, user_id=str(u.get("id")), nombre_tg=nombre,
                            persona_id=persona.id if persona else None,
                            persona_nombre=persona.nombre if persona else None,
                            inicio=ahora, ultima=ahora)
        self.convs[chat_id] = conv
        return conv

    def _iniciar_con_equipo(self, db, chat_id, u, nombre, persona, codigo) -> None:
        equipo = (db.query(MntEquipo).filter(MntEquipo.codigo.ilike(codigo)).first())
        if equipo is None or equipo.estado == "baja":
            return self.api.enviar(chat_id, f"No encuentro el equipo «{codigo}» (o está dado de baja).\n" + AYUDA)
        conv = self._nueva_conv(chat_id, u, nombre, persona)
        conv.sitio, conv.equipo_id = equipo.sitio, equipo.id
        self._preguntar_condicion(db, conv, equipo)

    def _iniciar_seleccion(self, db, chat_id, u, nombre, persona) -> None:
        conv = self._nueva_conv(chat_id, u, nombre, persona)
        sitios = [s for s in SITIOS if db.query(MntEquipo).filter(MntEquipo.sitio == s, MntEquipo.estado != "baja").first()]
        if not sitios:
            self.convs.pop(chat_id, None)
            return self.api.enviar(chat_id, "Todavía no hay equipos registrados.")
        if len(sitios) == 1:
            conv.sitio = sitios[0]
            return self._preguntar_sistema(db, conv)
        conv.paso = "sitio"
        self.api.enviar(chat_id, "¿Dónde está el equipo?", [[(SITIO_LABELS[s], f"s:{s}") for s in sitios]])

    def _grupos(self, db, sitio) -> dict:
        grupos: dict = {}
        for e in db.query(MntEquipo).filter(MntEquipo.sitio == sitio, MntEquipo.estado != "baja").order_by(MntEquipo.codigo).all():
            grupos.setdefault(e.sistema_id or 0, []).append(e)
        return grupos

    def _preguntar_sistema(self, db, conv) -> None:
        grupos = self._grupos(db, conv.sitio)
        if len(grupos) == 1:
            return self._preguntar_equipo(db, conv, next(iter(grupos)))
        nombres = {s.id: s.nombre for s in db.query(MntSistema).all()}
        orden = sorted(grupos, key=lambda k: (k == 0, nombres.get(k, "")))
        botones = [(f"{nombres.get(k, 'Otros')} ({len(grupos[k])})", f"y:{k}") for k in orden]
        conv.paso = "sistema"
        self.api.enviar(conv.chat_id, "¿De qué sistema?", _filas(botones, 2))

    def _preguntar_equipo(self, db, conv, sistema_id) -> None:
        equipos = self._grupos(db, conv.sitio).get(sistema_id, [])
        conv.paso = "equipo"
        self.api.enviar(conv.chat_id, "¿Qué equipo?", _filas([(f"{e.codigo} · {e.nombre}"[:60], f"e:{e.id}") for e in equipos], 1))

    def _preguntar_condicion(self, db, conv, equipo) -> None:
        conv.paso = "condicion"
        lineas = [f"🔧 {equipo.nombre} ({equipo.codigo}) · {SITIO_LABELS.get(equipo.sitio)}"]
        ot = service.ot_abierta_de(db, equipo.id)
        pendiente = (db.query(MntAviso).filter(MntAviso.equipo_id == equipo.id, MntAviso.estado == "nuevo")
                     .order_by(MntAviso.detectado_at).first())
        if ot:
            lineas.append(f"ℹ️ Ya tiene la {rules.folio_ot(ot.id)} abierta desde {ot.inicio_at:%d-%m %H:%M} "
                          f"({rules.ESTADOS_OT[ot.estado].lower()}). Tu aviso se sumará a ella.")
        elif pendiente:
            lineas.append(f"ℹ️ Ya hay un aviso sin atender de este equipo ({rules.folio_aviso(pendiente.id)}, "
                          f"{pendiente.detectado_at:%H:%M}). Si es lo mismo, agrega lo que veas.")
        lineas.append("¿Cómo está el equipo?")
        self.api.enviar(conv.chat_id, "\n".join(lineas), [[
            ("🛑 Detenido", "c:detenido"), ("⚠️ Con problemas", "c:degradado"), ("👀 Algo raro", "c:anomalia")]])

    def _preguntar_detalle(self, conv) -> None:
        conv.paso = "detalle"
        self.api.enviar(conv.chat_id, "¿Qué pasa? Escríbelo, manda un audio o una foto (o varias cosas).\n"
                                      "Cuando termines, aprieta Terminar.", [[("✅ Terminar", "fin")]])

    # -------------------------------------------------------------------- botones
    def _boton(self, cq: dict) -> None:
        data = cq.get("data") or ""
        msg = cq.get("message") or {}
        chat_id = str((msg.get("chat") or {}).get("id"))
        self.api.responder_boton(cq.get("id"))
        db = self.sesion()
        try:
            contacto, _, _ = self._contacto(db, cq.get("from") or {})
            if contacto.bloqueado:
                return
            conv = self.convs.get(chat_id)
            if conv is None:
                self.api.quitar_botones(chat_id, msg.get("message_id"))
                return self.api.enviar(chat_id, "Ese aviso ya no está abierto.\n" + AYUDA)
            conv.ultima = self.reloj()
            clave, _, valor = data.partition(":")
            esperado = {"s": "sitio", "y": "sistema", "e": "equipo", "c": "condicion", "r": "respaldo", "fin": "detalle"}
            if esperado.get(clave) != conv.paso:
                return                              # botón de un paso ya respondido
            elegido = _etiqueta_boton(msg, data)
            self.api.quitar_botones(chat_id, msg.get("message_id"),
                                    f"{msg.get('text', '')}\n→ {elegido}" if elegido and clave != "fin" else None)

            if clave == "s" and valor in SITIOS:
                conv.sitio = valor
                return self._preguntar_sistema(db, conv)
            if clave == "y":
                return self._preguntar_equipo(db, conv, int(valor) if valor.isdigit() else 0)
            if clave == "e" and valor.isdigit():
                equipo = db.get(MntEquipo, int(valor))
                if equipo is None or equipo.estado == "baja":
                    return self.api.enviar(chat_id, "Ese equipo ya no está disponible. /falla para elegir otro.")
                conv.equipo_id = equipo.id
                return self._preguntar_condicion(db, conv, equipo)
            if clave == "c" and valor in rules.CONDICIONES:
                conv.condicion = valor
                equipo = db.get(MntEquipo, conv.equipo_id)
                if valor == "detenido" and equipo.respaldo_equipo_id:
                    resp = db.get(MntEquipo, equipo.respaldo_equipo_id)
                    conv.paso = "respaldo"
                    return self.api.enviar(chat_id, f"¿Entró el respaldo ({resp.codigo} · {resp.nombre})?",
                                           [[("Sí", "r:si"), ("No", "r:no"), ("No sé", "r:no_se")]])
                return self._preguntar_detalle(conv)
            if clave == "r" and valor in rules.RESPALDO_ENTRO:
                conv.respaldo_entro = valor
                return self._preguntar_detalle(conv)
            if clave == "fin":
                return self._guardar(db, conv)
        finally:
            db.close()

    # ------------------------------------------------------------------ detalle
    def _detalle(self, conv, m: dict, texto: str) -> None:
        recibido = None
        try:
            if m.get("photo"):
                conv.foto = self._bajar(_mejor_foto(m["photo"]), MAX_FOTO)
                recibido = "📷 Foto recibida"
            elif (m.get("document") or {}).get("mime_type", "").startswith("image/"):
                conv.foto = self._bajar(m["document"], MAX_FOTO)
                recibido = "📷 Foto recibida"
            elif m.get("voice") or m.get("audio"):
                conv.audio = self._bajar(m.get("voice") or m.get("audio"), MAX_AUDIO)
                recibido = "🎤 Audio recibido"
        except (TelegramError, ValueError) as e:
            recibido = f"No pude guardar el archivo ({e}). Si puedes, escríbelo."
        caption = (m.get("caption") or "").strip()
        for t in (texto, caption):
            if t:
                conv.textos.append(t)
                recibido = recibido or "📝 Anotado"
        if recibido:
            self.api.enviar(conv.chat_id, f"{recibido}. Puedes seguir o apretar Terminar.", [[("✅ Terminar", "fin")]])

    def _bajar(self, archivo: dict, maximo: int) -> bytes:
        if (archivo.get("file_size") or 0) > maximo:
            raise ValueError("el archivo es muy grande")
        datos = self.api.descargar(archivo["file_id"])
        if len(datos) > maximo:
            raise ValueError("el archivo es muy grande")
        return datos

    # ------------------------------------------------------------------- guardar
    def _guardar(self, db, conv, por_inactividad: bool = False) -> Optional[int]:
        self.convs.pop(conv.chat_id, None)
        try:
            aviso = service.crear_aviso(
                db, equipo_id=conv.equipo_id, condicion=conv.condicion, origen="telegram",
                detectado_at=conv.inicio, respaldo_entro=conv.respaldo_entro,
                descripcion="\n".join(conv.textos) or None,
                reportado_por_id=conv.persona_id,
                reportante_texto=conv.persona_nombre or conv.nombre_tg or None,
                foto=conv.foto, audio=conv.audio, telegram_chat_id=conv.chat_id)
            db.commit()
        except ErrorValidacion as e:
            db.rollback()
            self.api.enviar(conv.chat_id, f"No se pudo registrar el aviso: {e}\n" + AYUDA)
            return None
        avisados = 0
        if aviso.prioridad_sugerida == "P1":
            try:
                avisados = self.alarmar(db, aviso.id) or 0
            except Exception:
                logger.exception("mnt: falló la alarma del aviso %s", aviso.id)
        encabezado = ("⏱️ Guardé tu aviso con lo que alcanzaste a mandar: " if por_inactividad else "✅ ")
        cola = (f"Se avisó de inmediato a {avisados} persona{'s' if avisados != 1 else ''}."
                if aviso.prioridad_sugerida == "P1" and avisados
                else "El encargado de mantenimiento lo verá en el tablero.")
        self.api.enviar(conv.chat_id, f"{encabezado}Aviso {rules.folio_aviso(aviso.id)} registrado · "
                                      f"prioridad {aviso.prioridad_sugerida}.\n{cola}\nTe avisaré cuando se atienda.")
        return aviso.id

    def expirar(self) -> None:
        """Cierra las conversaciones sin actividad. Se llama en cada vuelta del lector."""
        limite = self.reloj() - EXPIRA
        for conv in [c for c in self.convs.values() if c.ultima < limite]:
            if conv.equipo_id and conv.condicion:
                db = self.sesion()
                try:
                    self._guardar(db, conv, por_inactividad=True)
                finally:
                    db.close()
            else:
                self.convs.pop(conv.chat_id, None)
                try:
                    self.api.enviar(conv.chat_id, "El aviso se canceló por inactividad.\n" + AYUDA)
                except TelegramError:
                    pass


def _filas(botones: list, por_fila: int) -> list:
    return [botones[i:i + por_fila] for i in range(0, len(botones), por_fila)]


def _etiqueta_boton(msg: dict, data: str) -> Optional[str]:
    for fila in ((msg.get("reply_markup") or {}).get("inline_keyboard") or []):
        for b in fila:
            if b.get("callback_data") == data:
                return b.get("text")
    return None


def _mejor_foto(tamanos: list) -> dict:
    """Telegram manda varias resoluciones de la misma foto: se toma la mayor que
    no pase de 1600 px (basta para leer una placa y pesa ~100-300 KB)."""
    aptas = [p for p in tamanos if max(p.get("width", 0), p.get("height", 0)) <= 1600]
    return (aptas or tamanos[:1])[-1]


# =============================================================================
# Lector (long polling)
# =============================================================================
estado = {"activo": False, "desde": None, "ultimo_ok": None, "ultimo_error": None,
          "ultimo_error_at": None, "procesados": 0, "motivo_inactivo": None}


class Lector(threading.Thread):
    def __init__(self, api: Optional[Api] = None):
        super().__init__(daemon=True, name="mnt-bot")
        self.api = api or Api()
        self.bot = Bot(self.api)
        self.alto = threading.Event()

    def _offset(self) -> int:
        db = SessionLocal()
        try:
            p = db.query(MntParametro).filter(MntParametro.clave == "_bot_offset").first()
            return int(p.valor or 0) if p else 0
        finally:
            db.close()

    def _guardar_offset(self, valor: int) -> None:
        db = SessionLocal()
        try:
            p = db.query(MntParametro).filter(MntParametro.clave == "_bot_offset").first()
            if p is not None:
                p.valor = str(valor)
                db.commit()
        finally:
            db.close()

    def run(self) -> None:
        estado.update(activo=True, desde=datetime.now(), motivo_inactivo=None)
        offset = self._offset()
        while not self.alto.is_set():
            try:
                updates = self.api.get_updates(offset + 1 if offset else 0, timeout=25)
                estado["ultimo_ok"] = datetime.now()
                for u in updates:
                    try:
                        self.bot.procesar(u)
                        estado["procesados"] += 1
                    except Exception:
                        logger.exception("mnt: falló el update %s", u.get("update_id"))
                    # Se confirma aunque el update haya fallado: reintentarlo
                    # para siempre bloquearía a todos los demás.
                    offset = u["update_id"]
                    self._guardar_offset(offset)
                self.bot.expirar()
            except TelegramError as e:
                estado.update(ultimo_error=str(e), ultimo_error_at=datetime.now())
                # 409 = otro proceso lee con este token: esperar más y dejarlo a la vista.
                espera = 60 if "409" in str(e) else 10
                logger.warning("mnt bot: %s (reintento en %ss)", e, espera)
                self.alto.wait(espera)
            except Exception as e:
                estado.update(ultimo_error=f"{type(e).__name__}: {e}", ultimo_error_at=datetime.now())
                logger.exception("mnt bot: error inesperado")
                self.alto.wait(10)
        estado["activo"] = False


_lector: Optional[Lector] = None


def iniciar() -> None:
    """Arranca el lector solo en producción (MNT_BOT_ENABLED=1) y con token."""
    global _lector
    if (os.getenv("MNT_BOT_ENABLED") or "").strip() != "1":
        estado["motivo_inactivo"] = "MNT_BOT_ENABLED no es 1 (normal fuera de producción)"
        logger.info("mnt bot: deshabilitado (MNT_BOT_ENABLED != 1)")
        return
    if not configurado():
        estado["motivo_inactivo"] = "falta MNT_TELEGRAM_TOKEN"
        logger.warning("mnt bot: falta MNT_TELEGRAM_TOKEN; no arranca")
        return
    if _lector is None or not _lector.is_alive():
        _lector = Lector()
        _lector.start()
        logger.info("mnt bot: lector iniciado")


def detener() -> None:
    if _lector is not None:
        _lector.alto.set()
