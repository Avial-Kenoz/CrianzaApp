from sqlalchemy import Column, BigInteger, String, Boolean, TIMESTAMP, Text, ForeignKey
from app.db.session import Base

class UnmarkedMortalityRegistry(Base):
    __tablename__ = "unmarked_mortality_registries"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    legacy_id = Column(BigInteger, unique=True)
    mortality_report_id = Column(BigInteger, ForeignKey("mortality_reports.id"), nullable=False)
    ponds_movement_id = Column(BigInteger, ForeignKey("ponds_movements.id"))
    comment = Column(Text)
    registry_time = Column(TIMESTAMP)
    supervisor_validation = Column(Boolean, default=False)
    general_validation = Column(Boolean, default=False)
    created_at = Column(TIMESTAMP)
    updated_at = Column(TIMESTAMP)
