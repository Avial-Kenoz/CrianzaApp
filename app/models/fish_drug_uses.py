from sqlalchemy import Column, BigInteger, String, TIMESTAMP, ForeignKey
from app.db.session import Base


class FishDrugUse(Base):
    __tablename__ = "fish_drug_uses"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    fish_id = Column(BigInteger, ForeignKey("fish.id"), nullable=False, index=True)
    drug_name = Column(String(120), nullable=False)
    dose = Column(String(120), nullable=False)
    application_date = Column(TIMESTAMP, nullable=False)
    withdrawal_period = Column(String(120), nullable=False)
    created_at = Column(TIMESTAMP)
    updated_at = Column(TIMESTAMP)
