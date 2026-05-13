from sqlalchemy import Column, BigInteger, ForeignKey
from app.db.session import Base

class UserRole(Base):
    __tablename__ = "users_roles"
    user_id = Column(BigInteger, ForeignKey("users.id"), primary_key=True)
    role_id = Column(BigInteger, ForeignKey("roles.id"), primary_key=True)
