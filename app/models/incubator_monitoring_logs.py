from sqlalchemy import Column, BigInteger, Numeric, Text, TIMESTAMP, ForeignKey
from app.db.session import Base


class IncubatorMonitoringLog(Base):
    """Registro de seguimiento periódico por incubadora (Proceso 3)."""

    __tablename__ = "incubator_monitoring_logs"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    incubator_unit_id = Column(BigInteger, ForeignKey("incubator_units.id"), nullable=False, index=True)
    log_datetime = Column(TIMESTAMP, nullable=False)
    size_mm = Column(Numeric(5, 2))
    viability_pct = Column(Numeric(5, 2))
    observation = Column(Text)
    created_at = Column(TIMESTAMP)
