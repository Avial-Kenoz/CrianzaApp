from sqlalchemy import (Column, BigInteger, Integer, String, Boolean, Numeric,
                        TIMESTAMP, ForeignKey, JSON)
from app.db.session import Base


class WaterQualityAlert(Base):
    """Una condición anómala con historia propia, no un atributo de la lectura.

    Hasta ahora el nivel vivía en `pond_oxygen_readings.alarm_level`: sirve para
    pintar el panel, pero no para avisar. Una alarma que dura seis horas son
    docenas de evaluaciones del motor y tiene que seguir siendo **un** aviso,
    con un principio, un reconocimiento y un final.

    `dedupe_key` es lo que hace cumplir esa identidad, y lo garantiza la BD con
    un índice único parcial (`uniq_water_quality_alert_open`) sobre las filas
    sin cerrar — el mismo recurso que `uniq_silage_drum_open` en ensilaje.
    Formato por tipo:

      - `lectura_alarma:pond=<id>`
      - `ronda_vencida:unit=<id>`   ← por UNIDAD, nunca por estanque: una noche
        sin medir abriría doce alertas idénticas y el aviso se vuelve ruido.
      - `sin_reconocer:reading=<id>`

    Ciclo: `abierta` → `reconocida` (alguien se hizo cargo) → `cerrada`. Una
    alerta se cierra sola cuando la condición desaparece —la lectura siguiente
    vuelve a rango, o al fin se tomó la ronda—: eso es `close_reason=resuelta` y
    es el caso normal. `caducada` es la que se cierra por antigüedad sin que
    nadie la tocara ni se resolviera.

    `title`/`detail` se congelan al abrir. El texto tiene que decir lo que
    pasaba **en ese momento**: si se recalculara al mostrarlo, el historial
    mentiría apenas se editen los umbrales.
    """

    __tablename__ = "water_quality_alerts"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    kind = Column(String(30), nullable=False, index=True)   # lectura_alarma | ronda_vencida | sin_reconocer
    dedupe_key = Column(String(120), nullable=False)
    level = Column(String(20), nullable=False)              # alerta | alarma

    # Dónde. Una de las dos, según el tipo: el O2 es por estanque; la ronda
    # vencida y el biofiltro, por unidad.
    pond_id = Column(BigInteger, ForeignKey("ponds.id"), index=True)
    cultivation_unit_id = Column(BigInteger, ForeignKey("cultivation_units.id"), index=True)

    state = Column(String(20), nullable=False, default="abierta", index=True)
    opened_at = Column(TIMESTAMP, nullable=False)
    # Última evaluación que TODAVÍA vio la condición. Es lo que permite cerrar
    # por silencio: si el motor dejó de verla, se resolvió.
    last_seen_at = Column(TIMESTAMP, nullable=False)
    closed_at = Column(TIMESTAMP)
    close_reason = Column(String(20))                       # resuelta | reconocida | caducada | manual

    acked_at = Column(TIMESTAMP)
    acked_by = Column(BigInteger, ForeignKey("users.id"))
    ack_note = Column(String(160))

    # Lectura que la disparó (cuando la hay): permite saltar del aviso al dato.
    source_reading_id = Column(BigInteger, ForeignKey("pond_oxygen_readings.id"))

    title = Column(String(120), nullable=False)
    detail = Column(String(400))
    # Valores que dispararon la alerta (OD, saturación, horas de atraso...).
    # Es para diagnóstico posterior, no para recalcular nada.
    payload = Column(JSON)

    # Contabilidad de envíos: vive acá y no en la tabla de notificaciones para
    # que el cooldown se resuelva sin un agregado por cada alerta candidata.
    notify_count = Column(Integer, nullable=False, default=0)
    last_notified_at = Column(TIMESTAMP)

    created_at = Column(TIMESTAMP)
    updated_at = Column(TIMESTAMP)


class WaterQualityAlertRule(Base):
    """Qué se avisa y con qué frecuencia. Una fila por tipo de evento.

    Espejo de `water_quality_thresholds`: editable desde la UI sin tocar código
    ni reiniciar. Es la perilla contra el ruido, y el riesgo real del módulo no
    es que avise de menos sino que avise de más: una notificación que molesta se
    silencia el primer día y después no sirve para nada.

    Tres controles, de grueso a fino:

      - `enabled` — apaga el tipo completo.
      - `min_level` — `alarma` deja pasar sólo lo grave; `alerta` deja pasar
        todo. Se combina con el `min_level` del destinatario tomando el MÁS
        exigente de los dos.
      - `cooldown_min` / `renotify_min` — el primero es el piso entre dos avisos
        de la MISMA alerta; el segundo, cada cuánto recordar que sigue abierta
        (nulo = no recordar).

    `quiet_from`/`quiet_to` son horas decimales, como `o2_day_window` en los
    umbrales (8,5 = 08:30). Dentro de esa ventana **sólo pasa `alarma`**: no es
    un silencio total, porque el turno de noche es justo cuando más caro sale no
    enterarse. Lo que deja fuera son las alertas menores, que pueden esperar a
    las 8.

    `escalate_after_min` sólo tiene sentido en `sin_reconocer`: los minutos que
    se le dan al operador para hacerse cargo antes de subir al supervisor.
    """

    __tablename__ = "water_quality_alert_rules"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    kind = Column(String(30), nullable=False, unique=True)
    enabled = Column(Boolean, nullable=False, default=True)
    min_level = Column(String(20), nullable=False, default="alarma")

    cooldown_min = Column(Integer, nullable=False, default=60)
    renotify_min = Column(Integer)
    quiet_from = Column(Numeric(4, 2))
    quiet_to = Column(Numeric(4, 2))
    escalate_after_min = Column(Integer)

    label = Column(String(120), nullable=False)
    description = Column(String(300))
    updated_at = Column(TIMESTAMP)


class WaterQualityAlertRecipient(Base):
    """A quién le llega.

    El `chat_id` vive acá y no en el `.env` a propósito: sumar al encargado de
    turno tiene que ser editar una fila, no un archivo del servidor más un
    reinicio.

    `telegram_chat_id` es String y no entero porque los grupos de Telegram usan
    identificadores negativos y largos; guardarlo como texto evita sorpresas el
    día que se avise a un grupo de turno en vez de a una persona.

    Los filtros son todos opcionales y nulo significa "sin restricción":
    `kinds` y `unit_ids` son listas separadas por coma, `hours_from`/`hours_to`
    la ventana horaria en que esta persona recibe (decimal, 8,5 = 08:30) y
    `weekdays` los días (0 = lunes, como `datetime.weekday()`).

    Ojo con la ventana horaria: si nadie cubre una franja, en esa franja no se
    avisa nada. El turno de noche es el que importa —es la razón de ser del
    módulo—, así que conviene que siempre haya al menos un destinatario sin
    restricción horaria.
    """

    __tablename__ = "water_quality_alert_recipients"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    name = Column(String(120), nullable=False)
    telegram_chat_id = Column(String(40), nullable=False, unique=True)
    user_id = Column(BigInteger, ForeignKey("users.id"))
    active = Column(Boolean, nullable=False, default=True)

    min_level = Column(String(20), nullable=False, default="alarma")
    kinds = Column(String(120))
    unit_ids = Column(String(120))
    hours_from = Column(Numeric(4, 2))
    hours_to = Column(Numeric(4, 2))
    weekdays = Column(String(20))

    note = Column(String(200))
    created_at = Column(TIMESTAMP)
    updated_at = Column(TIMESTAMP)


class WaterQualityAlertNotification(Base):
    """Cada envío, con su resultado. Es la bitácora y además el antirrebote.

    Guarda dos cosas que no se pueden reconstruir después: el texto exacto que
    se mandó (los umbrales cambian; el mensaje que alguien recibió, no) y la
    dirección a la que salió. Por eso `address` y `message_text` se congelan en
    vez de leerse del destinatario: si mañana se corrige un `chat_id`, el
    historial tiene que seguir diciendo a dónde fue el aviso de anoche.

    `recipient_id` es nullable para que borrar a una persona no se lleve puesta
    la historia de lo que se le notificó.

    `telegram_message_id` guarda el id que devuelve la API para poder EDITAR
    después ese mismo mensaje —tacharlo cuando la alerta se reconoce o se
    resuelve— en vez de mandar uno nuevo. Agregarlo ahora es una columna;
    agregarlo después es una migración con historial ya escrito y sin forma de
    rellenarlo.
    """

    __tablename__ = "water_quality_alert_notifications"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    alert_id = Column(BigInteger, ForeignKey("water_quality_alerts.id"),
                      nullable=False, index=True)
    recipient_id = Column(BigInteger, ForeignKey("water_quality_alert_recipients.id"))
    channel = Column(String(20), nullable=False, default="telegram")
    address = Column(String(60), nullable=False)
    reason = Column(String(20), nullable=False)   # apertura | recordatorio | escalamiento | cierre | prueba

    sent_at = Column(TIMESTAMP, nullable=False, index=True)
    ok = Column(Boolean, nullable=False, default=False)
    error = Column(String(300))
    message_text = Column(String(1000))
    telegram_message_id = Column(BigInteger)

    created_at = Column(TIMESTAMP)
