from sqlalchemy import Column, BigInteger, String, Boolean, TIMESTAMP
from app.db.session import Base

class User(Base):
    __tablename__ = "users"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    legacy_id = Column(BigInteger, unique=True)
    email = Column(String(190), nullable=False, unique=True)
    name = Column(String(120))
    lastname = Column(String(120))
    rut = Column(String(30))
    active = Column(Boolean, default=True)
    created_at = Column(TIMESTAMP)
    updated_at = Column(TIMESTAMP)
