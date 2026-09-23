from sqlalchemy import Column, BigInteger, String, Integer, Numeric, Date, TIMESTAMP, ForeignKey
from app.db.session import Base


class SilageDrumDispatch(Base):
    """Guía de despacho con que los tambores sellados salen de planta.

    Proceso **independiente y secundario** del registro diario: manejo tipo
    inventario, se emite la guía y los tambores incluidos se dan de baja
    (`silage_drums.status = 'dispatched'` + `dispatched_at` + `dispatch_id`).

    La tabla se crea junto con el resto del módulo porque `silage_drums` la
    referencia, pero la UI que la alimenta llega en una fase posterior.
    """

    __tablename__ = "silage_drum_dispatches"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    dispatch_date = Column(Date, nullable=False)
    guide_number = Column(String(60))       # N° de guía de despacho
    carrier = Column(String(120))           # transportista
    destination = Column(String(120))

    total_drums = Column(Integer, nullable=False, default=0)
    total_kg = Column(Numeric(10, 2), nullable=False, default=0)

    user_id = Column(BigInteger, ForeignKey("users.id"))
    notes = Column(String(255))
    created_at = Column(TIMESTAMP)
    updated_at = Column(TIMESTAMP)
