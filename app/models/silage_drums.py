from sqlalchemy import Column, BigInteger, String, Integer, Numeric, Date, TIMESTAMP, ForeignKey
from app.db.session import Base


class SilageDrum(Base):
    """Tambor de ensilaje y su ciclo de vida.

    Recorrido: `available` → `open` (recibe cargas) → `sealed` (cerrado y
    sellado) → `dispatched` (retirado de planta con guía, ver
    SilageDrumDispatch).

    **Solo puede haber un tambor `open` a la vez.** Se garantiza en la BD con un
    índice único parcial (`uniq_silage_drum_open`), no solo en la lógica de
    aplicación. Consecuencia: cerrar un tambor y abrir el siguiente debe ocurrir
    en la misma transacción, porque el índice no admite el estado intermedio con
    dos abiertos.

    `total_kg` y `acid_lts` son acumulados derivados de las cargas
    (SilageGrindingEvent); se recalculan al registrar o anular una carga. La
    capacidad NO vive acá: es fija para todos los tambores y se configura
    centralizada en el umbral `drum_capacity_kg` (SilageThreshold).
    """

    __tablename__ = "silage_drums"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    drum_number = Column(Integer, nullable=False)  # N° rotulado en terreno
    status = Column(String(20), nullable=False, default="available")

    opened_at = Column(Date)
    sealed_at = Column(Date)
    # Autor y motivo del cierre. Se llenan sobre todo cuando el tambor se cierra
    # SIN una carga asociada (no cabía la molienda del día y se pasó al
    # siguiente): en ese caso no hay carga de la que deducir quién lo cerró.
    sealed_by = Column(String(120))
    seal_note = Column(String(255))
    dispatched_at = Column(Date)
    dispatch_id = Column(BigInteger, ForeignKey("silage_drum_dispatches.id"))

    total_kg = Column(Numeric(10, 2), nullable=False, default=0)
    acid_lts = Column(Numeric(10, 2), nullable=False, default=0)

    observation = Column(String(255))
    created_at = Column(TIMESTAMP)
    updated_at = Column(TIMESTAMP)
