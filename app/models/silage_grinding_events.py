from sqlalchemy import Column, BigInteger, String, Integer, Numeric, Boolean, Date, TIMESTAMP, ForeignKey
from app.db.session import Base


class SilageGrindingEvent(Base):
    """Una **carga** de molienda: cada vez que se muele mortalidad y se descarga
    al tambor. Es la unidad de captura del registro PC 03.2.

    La captura es **aditiva**: un día puede tener varias cargas y los kg se
    suman. No hay unicidad por fecha —`log_date` es solo un índice de
    agrupación—, así que reenviar un `client_uuid` ya presente es un duplicado
    que se ignora (misma semántica que pond_oxygen_readings). Corregir una carga
    mal registrada NO es reenviarla: es anularla (`voided_at`) y registrar la
    correcta, de modo que el original quede visible en la auditoría.

    **pH**: el operador dosifica, mide y corrige hasta alcanzar pH < 4, y recién
    entonces registra. Por eso hay un solo `ph_value` (el logrado) y
    `additional_acid` es un dato retrospectivo ("necesité más ácido para
    llegar"), no una acción pendiente. Un `ph_value` sobre el límite es la vía de
    escape para reportar un problema real, no el flujo normal.

    **Folio**: correlativo asignado por el SERVIDOR al sincronizar (secuencia
    `silage_folio_seq`). La PWA no puede generarlo offline sin arriesgar
    colisiones entre tablets, así que viaja NULL y se completa en el INSERT.
    """

    __tablename__ = "silage_grinding_events"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    # NULL solo en el instante previo al INSERT del servidor; ver docstring.
    folio = Column(BigInteger, unique=True)

    event_datetime = Column(TIMESTAMP, nullable=False, index=True)
    # Fecha de la carga. Indexada pero NO única: la captura es aditiva.
    log_date = Column(Date, nullable=False, index=True)

    # False = marca explícita de "hoy no hubo mortalidad" (kg NULL). Distingue el
    # día revisado sin mortalidad del día que nadie registró.
    had_mortality = Column(Boolean, nullable=False, default=True)
    mortality_kg = Column(Numeric(10, 2))

    acid_used = Column(Boolean, nullable=False, default=False)
    # Litros TOTALES usados, incluida la corrección. Es el número que consume el
    # inventario de ácido, y lo digita el operador (el sistema no lo calcula).
    acid_lts = Column(Numeric(8, 2))
    additional_acid = Column(Boolean, nullable=False, default=False)

    ph_value = Column(Numeric(4, 2))            # pH alcanzado al cerrar la carga
    ph_below_limit = Column(Boolean)            # derivado de ph_value < ph_limit

    drum_id = Column(BigInteger, ForeignKey("silage_drums.id"), index=True)
    drum_number = Column(Integer)               # copia textual (resiliencia histórica)
    drum_closed = Column(Boolean, nullable=False, default=False)

    operator_id = Column(BigInteger, ForeignKey("users.id"))

    # Recepción por un segundo usuario. Es TEXTO ESCRITO por quien declara haber
    # recibido: CrianzaApp no tiene autenticación, así que NO acredita identidad
    # y no debe llamarse "firma" en la UI ni en el procedimiento. Cuando se
    # quiera convertir en firma real se agrega received_by_user_id + PIN, sin
    # reescribir el histórico.
    received_by_name = Column(String(120))
    received_at = Column(TIMESTAMP)
    reception_note = Column(String(255))

    observation = Column(String(255))

    # Anulación (borrado lógico): la carga sigue visible, tachada, con su motivo.
    voided_at = Column(TIMESTAMP)
    voided_by = Column(String(120))
    void_reason = Column(String(255))

    # UUID generado por la PWA (idempotencia de sincronización). NULL si la carga
    # se creó desde el formulario web.
    client_uuid = Column(String(64), unique=True)

    created_at = Column(TIMESTAMP)
    updated_at = Column(TIMESTAMP)
