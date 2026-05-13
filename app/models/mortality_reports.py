from sqlalchemy import Column, BigInteger, String, Boolean, TIMESTAMP, Text, ForeignKey
from app.db.session import Base

class MortalityReport(Base):
    __tablename__ = "mortality_reports"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    legacy_id = Column(BigInteger, unique=True)
    comment = Column(Text)
    registry_time = Column(TIMESTAMP)
    breeding_user_id = Column(BigInteger, ForeignKey("users.id"))
    supervisor_user_id = Column(BigInteger, ForeignKey("users.id"))
    general_user_id = Column(BigInteger, ForeignKey("users.id"))
    supervisor_validation = Column(Boolean, default=False)
    general_validation = Column(Boolean, default=False)
    supervisor_validation_time = Column(TIMESTAMP)
    general_validation_time = Column(TIMESTAMP)
    created_at = Column(TIMESTAMP)
    updated_at = Column(TIMESTAMP)
