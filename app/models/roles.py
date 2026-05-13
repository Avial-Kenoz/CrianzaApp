from sqlalchemy import Column, BigInteger, String, TIMESTAMP
from app.db.session import Base

class Role(Base):
    __tablename__ = "roles"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    legacy_id = Column(BigInteger, unique=True)
    name = Column(String(120), nullable=False)
    resource_type = Column(String(120))
    resource_id = Column(BigInteger)
    created_at = Column(TIMESTAMP)
    updated_at = Column(TIMESTAMP)
