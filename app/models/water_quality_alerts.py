from sqlalchemy import (Column, BigInteger, Integer, String, Boolean, Date,
                        Numeric, TIMESTAMP, ForeignKey, JSON)
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

    `detail` se **refresca en cada pasada** mientras la alerta siga abierta: es
    la descripción de algo que está pasando ahora, y sus números tienen que ser
    los de ahora. Congelado, el aviso de una ronda vencida hace catorce horas
    decía "hace 3,0 h" — verdad sólo en el instante en que se abrió. Lo
    inmutable es `message_text` en la bitácora de envíos: ahí queda palabra por
    palabra lo que se le mandó a alguien. El `title` sí se mantiene, y sólo
    cambia si la alerta escala de nivel.
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

    Cuatro controles, de grueso a fino:

      - `enabled` — apaga el tipo completo.
      - `min_level` — piso para avisar: bajo esto no sale nada, nunca. Se
        combina con el `min_level` del destinatario tomando el MÁS exigente.
      - `instant_level` — desde aquí **suena al instante**. Lo que queda entre
        `min_level` e `instant_level` no interrumpe a nadie: se junta y sale en
        el mensaje del cambio de turno. Ese corte no es una hora inventada —
        sale de `o2_day_window` (día 08:30-16:00, resto `off`), el mismo turno
        con que el módulo mide todo lo demás.
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

    **Detectar no es avisar**, y esa separación la completa `notify_after_min`:
    la alerta no avisa hasta llevar ese rato abierta. Como se cierra sola apenas
    llega el dato que faltaba, "abierta 180 min" significa exactamente "nadie
    midió en las 3 h siguientes al vencimiento". Distingue el hueco real del
    atraso de rutina sin aflojar el plazo, que para el panel sigue siendo el
    correcto.

    En `ronda_vencida` ese mismo umbral es además lo que **sube el nivel**: la
    alerta nace `alerta` (al consolidado del turno) y se convierte en `alarma`
    al superarlo, o sea suena. Por eso el criterio de "esto interrumpe" puede
    ser el nivel y nada más, sin excepciones por tipo.
    """

    __tablename__ = "water_quality_alert_rules"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    kind = Column(String(30), nullable=False, unique=True)
    enabled = Column(Boolean, nullable=False, default=True)
    min_level = Column(String(20), nullable=False, default="alarma")
    # Desde este nivel suena al instante; bajo él va al consolidado del turno
    # (migración 20260929_02, que reemplazó la hora fija `digest_at`).
    instant_level = Column(String(20), nullable=False, default="alarma")

    cooldown_min = Column(Integer, nullable=False, default=60)
    renotify_min = Column(Integer)
    quiet_from = Column(Numeric(4, 2))
    quiet_to = Column(Numeric(4, 2))
    escalate_after_min = Column(Integer)
    # Compuerta entre detectar y avisar (migración 20260929_01).
    notify_after_min = Column(Integer)

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
    restricción horaria. Al semanero de turno la ventana **no** se le aplica:
    estar de turno es precisamente estar disponible a cualquier hora.

    `in_rotation` dice si entra en el turno de semaneros (ver
    `WaterQualityOncallWeek`). Quien queda fuera —jefatura, responsabilidades
    diferenciadas— recibe sólo las alarmas, nunca los consolidados de turno.
    """

    __tablename__ = "water_quality_alert_recipients"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    name = Column(String(120), nullable=False)
    telegram_chat_id = Column(String(40), nullable=False, unique=True)
    user_id = Column(BigInteger, ForeignKey("users.id"))
    active = Column(Boolean, nullable=False, default=True)

    min_level = Column(String(20), nullable=False, default="alarma")
    # Entra en el turno de semaneros (migración 20260929_04).
    in_rotation = Column(Boolean, nullable=False, default=True)
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


class WaterQualityOncallWeek(Base):
    """Quién es el semanero esa semana.

    El centro se cubre con tres encargados que se turnan: uno está disponible y
    atento 24/7 y los otros dos esa semana no. Sin esto la lista de
    destinatarios es plana y los tres reciben lo mismo siempre — que es
    exactamente como se pierde un aviso, porque si le llega a todos cada uno
    puede suponer que responde otro.

    Una fila por semana, identificada por su **lunes**, con un índice único que
    lo garantiza en la BD y no sólo en la UI. `recipient_id` admite NULL para
    poder dejar una semana explícitamente sin asignar.

    **Omitir la configuración no deja al centro mudo**: una semana sin asignar
    —o asignada a alguien que después se desactivó— cae de vuelta en "todos los
    de la rotación reciben". El modo de fallar de un turno es que nadie esté
    mirando; esa es la única falla que no puede pasar en silencio.
    """

    __tablename__ = "water_quality_oncall_weeks"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    week_start = Column(Date, nullable=False, unique=True, index=True)
    recipient_id = Column(BigInteger,
                          ForeignKey("water_quality_alert_recipients.id"))
    note = Column(String(200))
    created_at = Column(TIMESTAMP)
    updated_at = Column(TIMESTAMP)
