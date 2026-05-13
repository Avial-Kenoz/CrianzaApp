from sqlalchemy import Column, BigInteger, String, Integer, Date, DateTime, TIMESTAMP, ForeignKey, Boolean, Numeric, JSON
from app.db.session import Base


class CultivationDeclaration(Base):
    __tablename__ = "cultivation_declarations"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    folio = Column(String(120), nullable=False, unique=True, index=True)
    source_pond_id = Column(BigInteger, ForeignKey("ponds.id"), nullable=False, index=True)

    cultivation_unit_name = Column(String(160), nullable=False)
    manager_name = Column(String(160), nullable=False)
    manager_rut = Column(String(40), nullable=False)
    manager_phone = Column(String(40), nullable=False)

    declaration_date = Column(Date, nullable=False)
    validity_days = Column(Integer, nullable=False, default=3)
    valid_until = Column(Date, nullable=False)

    species_name = Column(String(120))
    total_fish = Column(Integer, nullable=False)
    total_weight_kg = Column(Numeric(12, 3))

    sanitary_report_number = Column(String(120))
    sanitary_report_date = Column(Date)

    sanitary_check_ok = Column(Boolean, nullable=False, default=False)
    drug_check_ok = Column(Boolean, nullable=False, default=False)
    depuration_check_ok = Column(Boolean, nullable=False, default=False)

    status = Column(String(30), nullable=False, default="sent")
    confirmed_at = Column(DateTime)
    confirmed_by = Column(String(160))

    payload = Column(JSON, nullable=False)
    created_at = Column(TIMESTAMP)
    updated_at = Column(TIMESTAMP)