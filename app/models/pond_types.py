from sqlalchemy import Column, BigInteger, String, TIMESTAMP
from app.db.session import Base

class PondType(Base):
    __tablename__ = "pond_types"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    legacy_id = Column(BigInteger, unique=True)
    name = Column(String(120), nullable=False)
    created_at = Column(TIMESTAMP)
    updated_at = Column(TIMESTAMP)
