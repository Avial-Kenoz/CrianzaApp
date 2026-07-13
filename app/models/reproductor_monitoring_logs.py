from sqlalchemy import Column, BigInteger, String, Integer, Date, TIMESTAMP, ForeignKey
from app.db.session import Base


class ReproductorMonitoringLog(Base):
    """Monitoreo reproductivo periódico. Los campos usados dependen del sexo."""

    __tablename__ = "reproductor_monitoring_logs"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    selection_id = Column(BigInteger, ForeignKey("reproductor_selections.id"), nullable=False, index=True)
    fish_id = Column(BigInteger, ForeignKey("fish.id"), nullable=False)  # desnormalizado
    log_date = Column(Date, nullable=False)
    sex = Column(String(20))  # copia del sexo para facilitar queries

    # Campos machos (low/medium/high)
    sperm_density = Column(String(10))
    sperm_motility = Column(String(10))
    sperm_time = Column(String(10))

    # Campos hembras
    ip_value = Column(Integer)  # Índice de Polarización (0–100)
    observation = Column(String(100))

    created_at = Column(TIMESTAMP)
