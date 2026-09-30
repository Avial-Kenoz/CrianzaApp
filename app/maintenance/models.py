"""Tablas del módulo de mantenimiento (prefijo mnt_).

Todas viven en fastapp_etapa1 aunque también guarden equipos de Planta: el
núcleo es uno solo (spec §1). No hay claves foráneas hacia tablas de otros
módulos: el vínculo a un estanque o unidad es una referencia blanda
(`ref_ubicacion_tipo` + `ref_ubicacion_id`), y a los usuarios de PlantaApp se
los identifica por su `username`. Así el módulo no ata su ciclo de vida al de
`ponds` o `users`.

Las tablas de avisos, OT y diccionario se crean ya en PR1, aunque sus pantallas
llegan en PR2 y en la fase del diccionario: así las FK entre ellas existen
desde el principio y no hay que reescribir la historia al llenarlas.
"""
from sqlalchemy import (
    Column, BigInteger, Integer, String, Text, Boolean, Numeric, Date,
    TIMESTAMP, ForeignKey, LargeBinary, JSON,
)

from app.db.session import Base


SITIOS = ("crianza", "planta")
SITIO_LABELS = {"crianza": "Crianza", "planta": "Planta"}


class MntSistema(Base):
    """Agrupador funcional: aireación, bombeo, eléctrico, frío…

    `sitio` NULL significa que aplica a ambos (p. ej. eléctrico).
    """

    __tablename__ = "mnt_sistemas"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    nombre = Column(String(80), nullable=False, unique=True)
    sitio = Column(String(10))
    orden = Column(Integer, nullable=False, default=0)
    activo = Column(Boolean, nullable=False, default=True)


class MntTipoEquipo(Base):
    """Soplador, bomba, generador… Sin lista de síntomas: el diccionario llega
    después y vive en `mnt_diccionario` (spec §5.6)."""

    __tablename__ = "mnt_tipos_equipo"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    nombre = Column(String(80), nullable=False, unique=True)
    activo = Column(Boolean, nullable=False, default=True)


class MntContratista(Base):
    """Empresa externa. No es usuario: reporta al encargado, que registra."""

    __tablename__ = "mnt_contratistas"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    empresa = Column(String(120), nullable=False)
    contacto = Column(String(120))
    telefono = Column(String(40))
    especialidad = Column(String(120))
    notas = Column(Text)
    activo = Column(Boolean, nullable=False, default=True)
    created_at = Column(TIMESTAMP)
    updated_at = Column(TIMESTAMP)


class MntPersona(Base):
    """Quién reporta, gestiona, ejecuta o recibe alarmas.

    Propia del módulo y no `users`: la mayoría de quienes reportan por Telegram
    no tienen cuenta en ninguna app, y los de Planta viven en otra base.

    `alarmas_sitios` dice de qué sitios recibe SIEMPRE las alarmas P1 (lista
    separada por coma). El semanero de Crianza no se configura aquí: sale de la
    rotación de calidad de agua en el momento de la alarma (spec §6.4).

    El horario (`horario_*`) usa el formato de `water_quality_alert_recipients`:
    días 0 = lunes separados por coma, horas decimales (8,5 = 08:30). Solo filtra
    el resumen; las alarmas no tienen horario.
    """

    __tablename__ = "mnt_personas"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    nombre = Column(String(120), nullable=False)
    rol = Column(String(20), nullable=False, default="reportante")
    sitio = Column(String(10))
    telegram_user_id = Column(String(40), unique=True)
    bot_iniciado = Column(Boolean, nullable=False, default=False)
    alarmas_sitios = Column(String(20))
    recibe_resumen = Column(Boolean, nullable=False, default=False)
    horario_dias = Column(String(20))
    horario_desde = Column(Numeric(4, 2))
    horario_hasta = Column(Numeric(4, 2))
    planta_username = Column(String(80))
    activo = Column(Boolean, nullable=False, default=True)
    created_at = Column(TIMESTAMP)
    updated_at = Column(TIMESTAMP)


class MntEquipo(Base):
    """El activo desde el punto de vista del mantenimiento (no contable).

    `codigo` (EQ-001…) es un correlativo único para ambos sitios: va impreso en
    el QR, así que no puede repetirse entre Crianza y Planta.

    `criticidad` es una copia del resultado vigente de la encuesta; la fuente es
    `mnt_criticidad_evaluaciones`, que guarda las respuestas y el historial.
    """

    __tablename__ = "mnt_equipos"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    codigo = Column(String(20), nullable=False, unique=True)
    sitio = Column(String(10), nullable=False, index=True)
    nombre = Column(String(120), nullable=False)
    tipo_id = Column(BigInteger, ForeignKey("mnt_tipos_equipo.id"))
    sistema_id = Column(BigInteger, ForeignKey("mnt_sistemas.id"))
    ubicacion_texto = Column(String(120))
    marca = Column(String(80))
    modelo = Column(String(80))
    serie = Column(String(80))
    potencia_kw = Column(Numeric(8, 2))
    voltaje = Column(String(20))
    fecha_instalacion = Column(Date)
    respaldo_equipo_id = Column(BigInteger, ForeignKey("mnt_equipos.id"))
    contratista_habitual_id = Column(BigInteger, ForeignKey("mnt_contratistas.id"))
    foto = Column(LargeBinary)
    foto_mime = Column(String(40))
    notas = Column(Text)
    estado = Column(String(20), nullable=False, default="operativo")
    criticidad = Column(String(1))
    alta_origen = Column(String(10), nullable=False, default="crianza")
    alta_por = Column(String(120))
    created_at = Column(TIMESTAMP)
    updated_at = Column(TIMESTAMP)


class MntEquipoDestino(Base):
    """A qué atiende un equipo de Crianza: puede ser más de un destino (los
    sopladores centralizados atienden todos los estanques).

    Tres niveles, de más amplio a más fino, para no marcar decenas de estanques:
    `sitio:crianza` (todo el sitio), `unit:<id>` (una unidad completa) y
    `pond:<id>` (un estanque). Referencia blanda: sin FK a ponds ni a
    cultivation_units. Se usa en F4 para cruzar fallas con calidad de agua.
    """

    __tablename__ = "mnt_equipo_destinos"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    equipo_id = Column(BigInteger, ForeignKey("mnt_equipos.id", ondelete="CASCADE"), nullable=False, index=True)
    ref_tipo = Column(String(10), nullable=False)
    ref_id = Column(String(40), nullable=False)


class MntCriticidadEvaluacion(Base):
    """Una fila por evaluación, sin borrar las anteriores.

    Guarda las respuestas, no solo el resultado: si mañana cambia la regla, se
    recalcula sin volver a preguntar. `regla_version` dice con qué tabla de
    `rules.py` se obtuvo el resultado.
    """

    __tablename__ = "mnt_criticidad_evaluaciones"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    equipo_id = Column(BigInteger, ForeignKey("mnt_equipos.id"), nullable=False, index=True)
    respuestas = Column(JSON, nullable=False)
    resultado = Column(String(1), nullable=False)
    tolerancia_horas = Column(Numeric(6, 2))
    regla_version = Column(String(10), nullable=False)
    evaluado_por = Column(String(120))
    evaluado_at = Column(TIMESTAMP, nullable=False)


class MntParametro(Base):
    """Parámetros editables del módulo (plazos por prioridad, resumen…).

    `valor` es texto porque hay parámetros que son listas (horas del resumen) o
    fechas; `service.parametros()` los interpreta.
    """

    __tablename__ = "mnt_parametros"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    clave = Column(String(50), nullable=False, unique=True)
    valor = Column(String(120))
    unidad = Column(String(20))
    descripcion = Column(String(250))
    updated_at = Column(TIMESTAMP)


class MntDiccionario(Base):
    """Etiquetas de síntoma/causa/acción. Nace vacía (spec §5.6)."""

    __tablename__ = "mnt_diccionario"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    dominio = Column(String(10), nullable=False)
    tipo_equipo_id = Column(BigInteger, ForeignKey("mnt_tipos_equipo.id"))
    etiqueta = Column(String(60), nullable=False)
    sinonimos = Column(Text)
    orden = Column(Integer, nullable=False, default=0)
    activo = Column(Boolean, nullable=False, default=True)


class MntOt(Base):
    """Orden de trabajo. Los tiempos por tramo salen de `mnt_ot_eventos`."""

    __tablename__ = "mnt_ots"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    tipo = Column(String(12), nullable=False, default="correctiva")
    equipo_id = Column(BigInteger, ForeignKey("mnt_equipos.id"), nullable=False, index=True)
    prioridad = Column(String(2), nullable=False)
    prioridad_motivo = Column(String(200))
    plazo_horas = Column(Numeric(8, 2))
    estado = Column(String(20), nullable=False, index=True)
    ejecutor_tipo = Column(String(12))
    tecnico_id = Column(BigInteger, ForeignKey("mnt_personas.id"))
    contratista_id = Column(BigInteger, ForeignKey("mnt_contratistas.id"))
    inicio_at = Column(TIMESTAMP, nullable=False)
    cierre_at = Column(TIMESTAMP)
    equipo_detenido_desde = Column(TIMESTAMP)
    equipo_en_servicio_at = Column(TIMESTAMP)
    causa_texto = Column(Text)
    accion_texto = Column(Text)
    causa_codigo = Column(BigInteger, ForeignKey("mnt_diccionario.id"))
    accion_codigo = Column(BigInteger, ForeignKey("mnt_diccionario.id"))
    provisorio = Column(Boolean, nullable=False, default=False)
    ot_origen_id = Column(BigInteger, ForeignKey("mnt_ots.id"))
    repuestos_texto = Column(Text)
    trabajo_realizado = Column(Text)
    created_at = Column(TIMESTAMP)
    updated_at = Column(TIMESTAMP)


class MntAviso(Base):
    """Lo que llega desde terreno. Nunca se rechaza en la entrada."""

    __tablename__ = "mnt_avisos"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    equipo_id = Column(BigInteger, ForeignKey("mnt_equipos.id"), nullable=False, index=True)
    origen = Column(String(12), nullable=False)
    reportado_por_id = Column(BigInteger, ForeignKey("mnt_personas.id"))
    reportante_texto = Column(String(120))
    detectado_at = Column(TIMESTAMP, nullable=False)
    condicion = Column(String(20), nullable=False)
    respaldo_entro = Column(String(10))
    descripcion = Column(Text)
    sintoma_codigo = Column(BigInteger, ForeignKey("mnt_diccionario.id"))
    foto = Column(LargeBinary)
    audio = Column(LargeBinary)
    prioridad_sugerida = Column(String(2))
    estado = Column(String(15), nullable=False, default="nuevo", index=True)
    ot_id = Column(BigInteger, ForeignKey("mnt_ots.id"))
    motivo_descarte = Column(String(200))
    # Chat desde el que se avisó por Telegram: para confirmarle al que avisó
    # aunque todavía no esté vinculado a una persona.
    telegram_chat_id = Column(String(40))
    created_at = Column(TIMESTAMP, nullable=False)


class MntTelegramContacto(Base):
    """Todo el que le escribe al bot, vinculado o no (bandeja, spec §6.3)."""

    __tablename__ = "mnt_telegram_contactos"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    telegram_user_id = Column(String(40), nullable=False, unique=True)
    nombre = Column(String(120))
    username = Column(String(80))
    primer_contacto = Column(TIMESTAMP, nullable=False)
    ultimo_contacto = Column(TIMESTAMP, nullable=False)
    bloqueado = Column(Boolean, nullable=False, default=False)


class MntNotificacion(Base):
    """Cada mensaje que el módulo intentó mandar y cómo le fue.

    `motivo`: alarma_p1, confirmacion_acuse, confirmacion_cierre…
    `destino`: nombre legible (persona o «semanero»), para leer la bitácora
    sin cruzar ids.
    """

    __tablename__ = "mnt_notificaciones"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    motivo = Column(String(30), nullable=False)
    aviso_id = Column(BigInteger, ForeignKey("mnt_avisos.id", ondelete="SET NULL"), index=True)
    ot_id = Column(BigInteger, ForeignKey("mnt_ots.id", ondelete="SET NULL"), index=True)
    persona_id = Column(BigInteger, ForeignKey("mnt_personas.id", ondelete="SET NULL"))
    destino = Column(String(120))
    chat_id = Column(String(40))
    ok = Column(Boolean, nullable=False)
    error = Column(String(300))
    enviado_at = Column(TIMESTAMP, nullable=False)


class MntOtEvento(Base):
    """Bitácora de estados: la fuente de todos los tiempos.

    `ocurrido_at` es cuándo pasó (editable); `registrado_at` es cuándo se anotó.
    La diferencia es la huella de un registro tardío (spec §3.3).
    """

    __tablename__ = "mnt_ot_eventos"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    ot_id = Column(BigInteger, ForeignKey("mnt_ots.id"), nullable=False, index=True)
    estado_desde = Column(String(20))
    estado_hasta = Column(String(20), nullable=False)
    ocurrido_at = Column(TIMESTAMP, nullable=False)
    registrado_at = Column(TIMESTAMP, nullable=False)
    actor_id = Column(BigInteger, ForeignKey("mnt_personas.id"))
    actor_texto = Column(String(120))
    nota = Column(String(300))
