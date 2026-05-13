from sqlalchemy import Column, BigInteger, String, Date, TIMESTAMP, ForeignKey, Integer
from app.db.session import Base


class SanitaryReport(Base):
    __tablename__ = "sanitary_reports"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    cultivation_unit_id = Column(BigInteger, ForeignKey("cultivation_units.id"), nullable=True)
    report_number = Column(String(120), nullable=False)  # ID / folio del informe
    laboratory = Column(String(200), nullable=False)     # nombre del laboratorio
    report_date = Column(Date, nullable=False)            # fecha del informe
    created_at = Column(TIMESTAMP)
    updated_at = Column(TIMESTAMP)
