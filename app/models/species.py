from sqlalchemy import Column, BigInteger, String, TIMESTAMP
from app.db.session import Base

class Species(Base):
    __tablename__ = "species"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    legacy_id = Column(BigInteger, unique=True)
    name = Column(String(120), nullable=False)
    internal_id = Column(String(120))
    created_at = Column(TIMESTAMP)
    updated_at = Column(TIMESTAMP)
