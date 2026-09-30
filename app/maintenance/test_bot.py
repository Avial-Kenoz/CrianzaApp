"""Pruebas del bot y de las notificaciones, sin red (API falsa).

Usan la base real (dev y producción comparten fastapp_etapa1): crean solo datos
con prefijo «ZZ » y los borran al terminar, sin tocar los equipos reales.

    set DATABASE_URL=postgresql://fastapp_user:fastapp_pass@localhost/fastapp_etapa1
    .venv\\Scripts\\python.exe -m unittest app.maintenance.test_bot -v
"""
import unittest
from datetime import datetime, timedelta

from sqlalchemy import text

from app.db.session import SessionLocal
from app.maintenance import notify, service
from app.maintenance.models import MntAviso, MntNotificacion, MntPersona, MntTelegramContacto
from app.maintenance.telegram_api import TelegramError
from app.services import personas as directorio
from app.maintenance.telegram_bot import Bot

UID = "990000001"          # usuario de Telegram ficticio
UID2 = "990000002"


class FakeApi:
    def __init__(self, fallar_para=()):
        self.enviados, self.bajados, self.fallar_para, self._mid = [], [], set(fallar_para), 100

    def enviar(self, chat_id, texto, botones=None):
        if str(chat_id) in self.fallar_para:
            raise TelegramError("HTTP 403 Forbidden: bot can't initiate conversation with a user")
        self._mid += 1
        self.enviados.append({"chat": str(chat_id), "texto": texto, "botones": botones or [], "id": self._mid})
        return self._mid

    def quitar_botones(self, *a, **k):
        pass

    def responder_boton(self, *a, **k):
        pass

    def descargar(self, file_id):
        self.bajados.append(file_id)
        return b"BYTES:" + file_id.encode()

    # -- ayudas para las pruebas --
    def ultimo(self):
        return self.enviados[-1]

    def boton(self, prefijo):
        """callback_data del último mensaje que empieza con `prefijo`."""
        for fila in self.ultimo()["botones"]:
            for t, d in fila:
                if d.startswith(prefijo):
                    return d
        return None


class Reloj:
    def __init__(self):
        self.t = datetime.now() - timedelta(minutes=1)

    def __call__(self):
        return self.t


def msg(texto=None, uid=UID, **extra):
    m = {"chat": {"id": int(uid), "type": "private"},
         "from": {"id": int(uid), "first_name": "ZZ Prueba", "last_name": "Bot", "username": "zzprueba"}}
    if texto is not None:
        m["text"] = texto
    m.update(extra)
    return {"message": m}


def boton(data, api, uid=UID):
    return {"callback_query": {"id": "cb", "data": data, "from": {"id": int(uid), "first_name": "ZZ Prueba"},
                               "message": {"message_id": api.ultimo()["id"], "chat": {"id": int(uid)},
                                           "text": api.ultimo()["texto"],
                                           "reply_markup": {"inline_keyboard": [
                                               [{"text": t, "callback_data": d} for t, d in fila]
                                               for fila in api.ultimo()["botones"]]}}}}


def limpiar():
    db = SessionLocal()
    eq = "(select id from mnt_equipos where nombre like 'ZZ %')"
    for sql in [
        f"delete from mnt_notificaciones where aviso_id in (select id from mnt_avisos where equipo_id in {eq})",
        f"delete from mnt_notificaciones where ot_id in (select id from mnt_ots where equipo_id in {eq})",
        f"delete from mnt_ot_eventos where ot_id in (select id from mnt_ots where equipo_id in {eq})",
        f"delete from mnt_avisos where equipo_id in {eq}",
        f"delete from mnt_ots where equipo_id in {eq}",
        f"delete from mnt_criticidad_evaluaciones where equipo_id in {eq}",
        f"delete from mnt_equipo_destinos where equipo_id in {eq}",
        "delete from mnt_equipos where nombre like 'ZZ %'",
        "delete from mnt_grupos_redundancia where nombre like 'ZZ %'",
        # Personas de prueba: por nombre ZZ o por los Telegram ficticios.
        "delete from mnt_notificaciones where persona_id in (select m.id from mnt_personas m join personas p "
        f"on p.id = m.persona_id where p.nombre like 'ZZ %' or p.telegram_id in ('{UID}', '{UID2}'))",
        "update mnt_avisos set reportado_por_id = null where reportado_por_id in (select m.id from mnt_personas m "
        f"join personas p on p.id = m.persona_id where p.nombre like 'ZZ %' or p.telegram_id in ('{UID}', '{UID2}'))",
        "delete from mnt_personas where persona_id in (select id from personas where nombre like 'ZZ %' "
        f"or telegram_id in ('{UID}', '{UID2}'))",
        f"delete from personas where nombre like 'ZZ %' or telegram_id in ('{UID}', '{UID2}')",
        f"delete from mnt_telegram_contactos where telegram_user_id in ('{UID}', '{UID2}')",
    ]:
        db.execute(text(sql))
    db.commit()
    db.close()


R_A = {"impacto": "a", "respaldo": "a", "reposicion": "c", "seguridad_ambiente": "no"}


class BotTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        limpiar()
        db = SessionLocal()
        cls.respaldo = service.crear_equipo(db, {"sitio": "crianza", "nombre": "ZZ Soplador R"}, R_A,
                                            origen="crianza", autor="test")
        cls.eq = service.crear_equipo(db, {"sitio": "crianza", "nombre": "ZZ Soplador T"}, R_A,
                                      origen="crianza", autor="test")
        # Los dos sopladores se respaldan entre sí: un grupo que necesita 1 operando.
        service.guardar_grupo(db, {"nombre": "ZZ Grupo T", "sitio": "crianza", "necesarios": "1",
                                   "conmutacion": "manual", "impacto": "a", "seguridad_ambiente": "no"},
                              miembros_ids=[cls.respaldo.id, cls.eq.id])
        cls.eq_planta = service.crear_equipo(db, {"sitio": "planta", "nombre": "ZZ Selladora T"}, R_A,
                                             origen="crianza", autor="test")
        db.commit()
        cls.eq_id, cls.eq_codigo = cls.eq.id, cls.eq.codigo
        cls.resp_codigo = cls.respaldo.codigo
        cls.planta_id = cls.eq_planta.id
        db.close()

    @classmethod
    def tearDownClass(cls):
        limpiar()

    def setUp(self):
        self.api, self.reloj, self.alarmas = FakeApi(), Reloj(), []
        self.bot = Bot(self.api, reloj=self.reloj, alarmar=lambda db, aid: self.alarmas.append(aid) or 2)

    def _aviso(self, aid):
        db = SessionLocal()
        try:
            a = db.get(MntAviso, aid)
            db.expunge(a)
            return a
        finally:
            db.close()

    def _ultimo_aviso_id(self):
        db = SessionLocal()
        try:
            return db.query(MntAviso.id).filter(MntAviso.equipo_id.in_([self.eq_id, self.planta_id])) \
                     .order_by(MntAviso.id.desc()).first()[0]
        finally:
            db.close()

    # ------------------------------------------------------------------------
    def test_qr_flujo_completo(self):
        self.bot.procesar(msg(f"/start {self.eq_codigo.lower()}"))           # el código no distingue mayúsculas
        self.assertIn("ZZ Soplador T", self.api.ultimo()["texto"])
        self.bot.procesar(boton("c:detenido", self.api))
        self.assertIn("Entró otra unidad del grupo", self.api.ultimo()["texto"])
        self.assertIn(self.resp_codigo, self.api.ultimo()["texto"])
        self.bot.procesar(boton("r:no", self.api))
        self.assertIn("Terminar", str(self.api.ultimo()["botones"]))
        self.bot.procesar(msg("ZZ no parte, huele a quemado"))
        fotos = [{"file_id": "chica", "width": 320, "height": 240},
                 {"file_id": "media", "width": 1280, "height": 960},
                 {"file_id": "gigante", "width": 4000, "height": 3000}]
        self.bot.procesar(msg(photo=fotos, caption="ZZ tablero"))
        self.assertEqual(self.api.bajados, ["media"])                  # la mayor ≤ 1600 px
        self.bot.procesar(msg(voice={"file_id": "voz1", "file_size": 5000}))
        self.bot.procesar(boton("fin", self.api))

        a = self._aviso(self._ultimo_aviso_id())
        self.assertEqual((a.origen, a.condicion, a.respaldo_entro, a.prioridad_sugerida), ("telegram", "detenido", "no", "P1"))
        self.assertEqual(a.descripcion, "ZZ no parte, huele a quemado\nZZ tablero")
        self.assertEqual((a.foto, a.audio), (b"BYTES:media", b"BYTES:voz1"))
        self.assertEqual((a.telegram_chat_id, a.reportante_texto), (UID, "ZZ Prueba Bot"))
        self.assertEqual(self.alarmas, [a.id])                          # P1 → alarma
        self.assertIn("Se avisó de inmediato a 2 personas", self.api.ultimo()["texto"])

        # un botón viejo ya no hace nada útil
        self.bot.procesar(boton("c:detenido", self.api))
        self.assertIn("ya no está abierto", self.api.ultimo()["texto"])

    def test_falla_sin_qr_elige_sitio_y_equipo(self):
        self.bot.procesar(msg("/falla"))
        s = self.api.boton("s:planta")
        self.assertIsNotNone(s, "debería preguntar el sitio (hay equipos en ambos)")
        self.bot.procesar(boton(s, self.api))
        for _ in range(2):                                             # sistema (si hay varios) y equipo
            e = self.api.boton(f"e:{self.planta_id}")
            if e:
                break
            self.bot.procesar(boton(self.api.boton("y:0"), self.api))
        self.bot.procesar(boton(f"e:{self.planta_id}", self.api))
        self.bot.procesar(boton("c:anomalia", self.api))               # sin respaldo: directo al detalle
        self.bot.procesar(boton("fin", self.api))
        a = self._aviso(self._ultimo_aviso_id())
        self.assertEqual((a.equipo_id, a.condicion, a.prioridad_sugerida), (self.planta_id, "anomalia", "P3"))
        self.assertEqual(self.alarmas, [])                              # P3 → sin alarma
        self.assertIn("lo verá en el tablero", self.api.ultimo()["texto"])

    def test_boton_de_otro_paso_se_ignora(self):
        self.bot.procesar(msg(f"/start {self.eq_codigo}"))
        n = len(self.api.enviados)
        self.bot.procesar(boton("r:si", self.api))                      # aún no pregunta el respaldo
        self.assertEqual(len(self.api.enviados), n)

    def test_expira_con_condicion_se_guarda(self):
        self.bot.procesar(msg(f"/start {self.eq_codigo}"))
        self.bot.procesar(boton("c:degradado", self.api))
        self.reloj.t += timedelta(minutes=11)
        self.bot.expirar()
        self.assertIn("Guardé tu aviso", self.api.ultimo()["texto"])
        self.assertEqual(self.bot.convs, {})

    def test_expira_sin_condicion_se_descarta(self):
        antes = self._ultimo_aviso_id_o_none()
        self.bot.procesar(msg(f"/start {self.eq_codigo}"))
        self.reloj.t += timedelta(minutes=11)
        self.bot.expirar()
        self.assertIn("se canceló por inactividad", self.api.ultimo()["texto"])
        self.assertEqual(self._ultimo_aviso_id_o_none(), antes)

    def _ultimo_aviso_id_o_none(self):
        try:
            return self._ultimo_aviso_id()
        except TypeError:
            return None

    def test_codigo_inexistente_y_cancelar(self):
        self.bot.procesar(msg("/start EQ-99999"))
        self.assertIn("No encuentro", self.api.ultimo()["texto"])
        self.bot.procesar(msg(f"/start {self.eq_codigo}"))
        self.bot.procesar(msg("/cancelar"))
        self.assertIn("cancelado", self.api.ultimo()["texto"])
        self.assertEqual(self.bot.convs, {})

    def test_grupos_se_ignoran_y_bloqueados_tambien(self):
        g = msg("/falla")
        g["message"]["chat"]["type"] = "group"
        self.bot.procesar(g)
        self.assertEqual(self.api.enviados, [])
        db = SessionLocal()
        c, _ = service.registrar_contacto(db, UID2, "ZZ Spam", None)
        c.bloqueado = True
        db.commit()
        db.close()
        self.bot.procesar(msg("/falla", uid=UID2))
        self.assertEqual(self.api.enviados, [])

    def test_start_sin_parametro_y_vinculacion(self):
        self.bot.procesar(msg("/start"))
        self.assertIn("bot de mantenimiento", self.api.ultimo()["texto"])
        db = SessionLocal()
        try:
            c = next(c for c in service.contactos_sin_vincular(db) if c.telegram_user_id == UID)
            # un aviso previo, sin persona
            service.crear_aviso(db, equipo_id=self.eq_id, condicion="anomalia", origen="telegram",
                                reportante_texto="ZZ Prueba Bot", telegram_chat_id=UID)
            db.commit()
            p = service.vincular_contacto(db, c.id, nombre_nuevo="ZZ Operario", rol="reportante")
            db.commit()
            self.assertTrue(p.bot_iniciado)
            # todos sus avisos previos (de esta y otras pruebas) quedan atribuidos
            self.assertGreaterEqual(db.query(MntAviso).filter(MntAviso.telegram_chat_id == UID,
                                                              MntAviso.reportado_por_id == p.id).count(), 1)
            self.assertEqual(db.query(MntAviso).filter(MntAviso.telegram_chat_id == UID,
                                                       MntAviso.reportado_por_id.is_(None)).count(), 0)
            self.assertNotIn(UID, [x.telegram_user_id for x in service.contactos_sin_vincular(db)])
        finally:
            db.close()
        # ya vinculado: el bot lo saluda por su nombre de persona
        self.bot.procesar(msg("/start"))
        self.assertIn("Hola ZZ Operario", self.api.ultimo()["texto"])


class GrupoServicioTest(unittest.TestCase):
    """El ejemplo del encargado: con redundancia el equipo es menos crítico; si
    su gemelo falla, el que queda solo pasa a crítico y su aviso nace P1."""

    @classmethod
    def setUpClass(cls):
        limpiar()
        db = SessionLocal()
        R3 = {"reposicion": "c"}                       # en grupo solo se responde la reposición
        a = service.crear_equipo(db, {"sitio": "crianza", "nombre": "ZZ Bomba G1"}, R_A, origen="crianza", autor="t")
        b = service.crear_equipo(db, {"sitio": "crianza", "nombre": "ZZ Bomba G2"}, R_A, origen="crianza", autor="t")
        g = service.guardar_grupo(db, {"nombre": "ZZ Grupo G", "sitio": "crianza", "necesarios": "1",
                                       "conmutacion": "automatica", "impacto": "a", "seguridad_ambiente": "no"},
                                  miembros_ids=[a.id, b.id])
        c = service.crear_equipo(db, {"sitio": "crianza", "nombre": "ZZ Bomba G3", "grupo_id": str(g.id)}, R3,
                                 origen="crianza", autor="t")
        db.commit()
        cls.a, cls.b, cls.c, cls.g = a.id, b.id, c.id, g.id
        db.close()

    @classmethod
    def tearDownClass(cls):
        limpiar()

    def test_ciclo_completo(self):
        from app.maintenance.models import MntEquipo, MntGrupoRedundancia
        db = SessionLocal()
        try:
            a, b, c = db.get(MntEquipo, self.a), db.get(MntEquipo, self.b), db.get(MntEquipo, self.c)
            # 3 equipos, 1 necesario, automática → con respaldo: B (impacto <2h + automático)
            self.assertEqual({a.criticidad, b.criticidad, c.criticidad}, {"B"})
            self.assertEqual(c.grupo_id, self.g)
            # necesitar 4 de 3 es imposible
            with self.assertRaises(service.ErrorValidacion):
                service.guardar_grupo(db, {"nombre": "ZZ Grupo G", "necesarios": "4", "conmutacion": "manual",
                                           "impacto": "a", "seguridad_ambiente": "no"},
                                      db.get(MntGrupoRedundancia, self.g))
            db.rollback()

            # falla A (con otros operando) → P2 si el respaldo entró
            av = service.crear_aviso(db, equipo_id=a.id, condicion="detenido", origen="web", respaldo_entro="si")
            db.commit()
            self.assertEqual(av.prioridad_sugerida, "P2")
            self.assertEqual(service.criticidad_de(db, b, nominal=False), "B")   # quedan B y C: aún hay respaldo
            # falla B → queda solo C: sin respaldo, C pasa a A ahora
            av = service.crear_aviso(db, equipo_id=b.id, condicion="detenido", origen="web", respaldo_entro="si")
            db.commit()
            self.assertEqual(av.prioridad_sugerida, "P2")                      # C seguía operando
            self.assertEqual(service.criticidad_de(db, c, nominal=False), "A")
            self.assertEqual(c.criticidad, "B")                                # la nominal no cambia
            riesgo = [r for r in service.servicios_en_riesgo(db) if r["grupo"].id == self.g]
            self.assertEqual(riesgo[0]["estado"], "sin_respaldo")
            self.assertIsNone(service.texto_respaldo(db, c))                   # el bot no pregunta respaldo
            # falla C, el que quedó solo → P1 aunque su nominal sea B
            av = service.crear_aviso(db, equipo_id=c.id, condicion="detenido", origen="web", respaldo_entro="si")
            db.commit()
            self.assertIsNone(av.respaldo_entro)                              # no había respaldo que entrara
            self.assertEqual(av.prioridad_sugerida, "P1")
            riesgo = [r for r in service.servicios_en_riesgo(db) if r["grupo"].id == self.g]
            self.assertEqual(riesgo[0]["estado"], "insuficiente")             # 0 operando de 1 necesario

            # C sale del grupo: hereda la consecuencia y queda «sin respaldo» (A)
            msg = service.actualizar_equipo(db, c, {"nombre": "ZZ Bomba G3", "grupo_id": ""})
            db.commit()
            self.assertIn("salió de", msg)
            self.assertEqual(c.criticidad, "A")
            self.assertEqual(service.ultima_evaluacion(db, c.id).respuestas["respaldo"], "a")
        finally:
            db.close()


class NotifyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        limpiar()
        db = SessionLocal()
        cls.eq = service.crear_equipo(db, {"sitio": "planta", "nombre": "ZZ Camara N"}, R_A,
                                      origen="crianza", autor="test")
        # Identidad en el directorio; el rol de mantenimiento apunta a ella.
        enc = directorio.guardar(db, {"nombre": "ZZ Encargado N", "telegram_id": UID})
        sup = directorio.guardar(db, {"nombre": "ZZ Supervisor N", "telegram_id": UID2})
        db.add(MntPersona(persona_id=enc.id, rol="encargado", alarmas_sitios="crianza,planta", activo=True))
        db.add(MntPersona(persona_id=sup.id, rol="supervisor_planta", alarmas_sitios="planta", activo=True))
        db.commit()
        cls.eq_id = cls.eq.id
        db.close()

    @classmethod
    def tearDownClass(cls):
        limpiar()

    def test_alarma_p1_a_destinatarios_del_sitio_y_bitacora(self):
        db = SessionLocal()
        try:
            a = service.crear_aviso(db, equipo_id=self.eq_id, condicion="detenido", origen="web",
                                    descripcion="ZZ no enfría", reportante_texto="ZZ Juan")
            db.commit()
            self.assertEqual(a.prioridad_sugerida, "P1")
            api = FakeApi(fallar_para={UID2})                  # el supervisor no hizo /start
            n = notify.alarma_aviso(db, a.id, api)
            self.assertEqual(n, 1)
            self.assertEqual([m["chat"] for m in api.enviados], [UID])
            self.assertIn("ALARMA P1 · Planta", api.enviados[0]["texto"])
            self.assertIn("ZZ no enfría", api.enviados[0]["texto"])
            regs = db.query(MntNotificacion).filter(MntNotificacion.aviso_id == a.id).all()
            self.assertEqual(sorted((r.destino, r.ok) for r in regs),
                             [("ZZ Encargado N", True), ("ZZ Supervisor N", False)])
            self.assertIn("403", next(r.error for r in regs if not r.ok))
        finally:
            db.close()

    def test_p2_no_alarma(self):
        db = SessionLocal()
        try:
            a = service.crear_aviso(db, equipo_id=self.eq_id, condicion="degradado", origen="web")
            db.commit()
            api = FakeApi()
            self.assertEqual(notify.alarma_aviso(db, a.id, api), 0)
            self.assertEqual(api.enviados, [])
        finally:
            db.close()

    def test_confirmaciones_al_que_aviso(self):
        db = SessionLocal()
        try:
            a = service.crear_aviso(db, equipo_id=self.eq_id, condicion="anomalia", origen="telegram",
                                    telegram_chat_id=UID, reportante_texto="ZZ Juan")
            db.commit()
            r = service.aceptar_avisos(db, [a.id])
            db.commit()
            ot = r["creadas"][0]
            api = FakeApi()
            notify.confirmar_acuse(db, [a.id], api)
            self.assertIn("fue recibido", api.ultimo()["texto"])
            service.cerrar_ot(db, ot, causa="ZZ", accion="ZZ", provisorio=False,
                              en_servicio_at=datetime.now())
            db.commit()
            notify.confirmar_cierre(db, ot.id, api)
            self.assertIn("quedó en servicio", api.ultimo()["texto"])
            self.assertEqual({m["chat"] for m in api.enviados}, {UID})
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
