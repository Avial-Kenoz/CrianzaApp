from sqlalchemy import Column, BigInteger, TIMESTAMP, ForeignKey
from app.db.session import Base


class ReproductorSpawningMale(Base):
    """Relación entre un desove y uno o más machos seleccionados."""

    __tablename__ = "reproductor_spawning_males"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    spawning_event_id = Column(BigInteger, ForeignKey("reproductor_spawning_events.id"), nullable=False, index=True)
    male_selection_id = Column(BigInteger, ForeignKey("reproductor_selections.id"), nullable=False)
    fish_id = Column(BigInteger, ForeignKey("fish.id"), nullable=False)  # macho involucrado
    created_at = Column(TIMESTAMP)
