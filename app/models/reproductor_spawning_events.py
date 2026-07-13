from sqlalchemy import Column, BigInteger, String, Integer, Numeric, Text, Date, Time, TIMESTAMP, ForeignKey
from app.db.session import Base


class ReproductorSpawningEvent(Base):
    """Proceso de desove (Proceso 2), asociado a la hembra seleccionada."""

    __tablename__ = "reproductor_spawning_events"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    selection_id = Column(BigInteger, ForeignKey("reproductor_selections.id"), nullable=False, index=True)
    fish_id = Column(BigInteger, ForeignKey("fish.id"), nullable=False)  # hembra base
    status = Column(String(30), nullable=False, default="draft")  # draft/partial/completed/cancelled

    event_date = Column(Date)
    event_time = Column(Time)
    water_temp = Column(Numeric(5, 2))

    anesthesia_drug_name = Column(String(200))
    anesthesia_dilution = Column(String(100))

    total_ova_weight_g = Column(Numeric(10, 2))
    ova_sample_grams = Column(Numeric(6, 2))  # gramos de la muestra usada para contar (la n)
    ova_count_sample = Column(Integer)  # recuento de ovas en la muestra de n gramos
    ova_diameter_mm = Column(Numeric(4, 2))
    viability_pct = Column(Numeric(5, 2))

    notes = Column(Text)
    created_at = Column(TIMESTAMP)
    updated_at = Column(TIMESTAMP)
