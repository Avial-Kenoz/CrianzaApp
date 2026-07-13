from sqlalchemy import Column, BigInteger, String, TIMESTAMP, ForeignKey
from app.db.session import Base


class IncubatorBatch(Base):
    """Proceso 3: monitoreo de incubadoras asociado a un desove.

    El nombre y el código del lote NO viven aquí: se reutiliza `lots`
    (lots.name / lots.internal_id). Este batch solo referencia el lote por lot_id.
    """

    __tablename__ = "incubator_batches"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    spawning_event_id = Column(BigInteger, ForeignKey("reproductor_spawning_events.id"), nullable=False, index=True)
    lot_id = Column(BigInteger, ForeignKey("lots.id"), nullable=True)  # NULL hasta que el usuario lo defina
    status = Column(String(30), nullable=False, default="monitoring")  # monitoring/awaiting_first_count/completed
    created_at = Column(TIMESTAMP)
    updated_at = Column(TIMESTAMP)
