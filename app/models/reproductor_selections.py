from sqlalchemy import Column, BigInteger, String, Text, Date, TIMESTAMP, ForeignKey
from app.db.session import Base


class ReproductorSelection(Base):
    """Período de selección de un pez como reproductor activo (Proceso 1)."""

    __tablename__ = "reproductor_selections"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    fish_id = Column(BigInteger, ForeignKey("fish.id"), nullable=False, index=True)
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)  # start_date + 30 días
    status = Column(String(30), nullable=False, default="active")  # active/completed/failed/spawned
    sex_at_selection = Column(String(20))  # copia del sexo del pez (F/M) al seleccionar
    closed_at = Column(TIMESTAMP)
    notes = Column(Text)
    created_at = Column(TIMESTAMP)
    updated_at = Column(TIMESTAMP)
