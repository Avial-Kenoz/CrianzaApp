from sqlalchemy import Column, BigInteger, String, Integer, TIMESTAMP, Text
from app.db.session import Base

class Transport(Base):
    __tablename__ = "transports"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    legacy_id = Column(BigInteger, unique=True)
    sheet_number = Column(Integer)
    delivery_date = Column(TIMESTAMP)
    chofer = Column(String(120))
    phone_number = Column(BigInteger)
    license_plate = Column(String(40))
    observations = Column(Text)
    created_at = Column(TIMESTAMP)
    updated_at = Column(TIMESTAMP)
