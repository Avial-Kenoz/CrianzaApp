from sqlalchemy import Column, BigInteger, Integer, Numeric, TIMESTAMP, ForeignKey
from app.db.session import Base


class ReproductorSpawningIncubator(Base):
    """Distribución inicial del peso de ovas en incubadoras al cerrar el desove."""

    __tablename__ = "reproductor_spawning_incubators"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    spawning_event_id = Column(BigInteger, ForeignKey("reproductor_spawning_events.id"), nullable=False, index=True)
    incubator_id = Column(Integer, nullable=False)  # 1 a 20
    ova_weight_g = Column(Numeric(10, 2), nullable=False)
    created_at = Column(TIMESTAMP)
