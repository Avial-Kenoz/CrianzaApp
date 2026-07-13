from sqlalchemy import Column, BigInteger, String, Text, TIMESTAMP, ForeignKey
from app.db.session import Base


class ReproductorRecoveryReport(Base):
    """Resultado de recuperación posterior al desove por cada reproductor involucrado."""

    __tablename__ = "reproductor_recovery_reports"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    spawning_event_id = Column(BigInteger, ForeignKey("reproductor_spawning_events.id"), nullable=False, index=True)
    fish_id = Column(BigInteger, ForeignKey("fish.id"), nullable=False)
    role = Column(String(20), nullable=False)  # female/male
    recovery_result = Column(String(20), nullable=False)  # successful/sacrifice
    notes = Column(Text)
    created_at = Column(TIMESTAMP)
