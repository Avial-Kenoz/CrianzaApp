from sqlalchemy import Column, BigInteger, String, Integer, Numeric, TIMESTAMP, ForeignKey
from app.db.session import Base


class IncubatorUnit(Base):
    """Cada incubadora activa dentro de un Proceso 3, incluidas las particiones.

    display_id es el identificador compuesto visible (ej: `2`, `6ex2`, `6ex2ex6`).
    status: active / split (particionada, sale del monitoreo) / hatched.
    """

    __tablename__ = "incubator_units"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    incubator_batch_id = Column(BigInteger, ForeignKey("incubator_batches.id"), nullable=False, index=True)
    display_id = Column(String(30), nullable=False)
    base_incubator_id = Column(Integer, nullable=False)  # 1-20, incubadora original
    current_weight_g = Column(Numeric(10, 2))
    parent_unit_id = Column(BigInteger, ForeignKey("incubator_units.id"), nullable=True)
    status = Column(String(20), nullable=False, default="active")  # active/split/hatched
    target_pond_id = Column(BigInteger, ForeignKey("ponds.id"), nullable=True)  # estanque Hatchery de eclosión
    hatched_at = Column(TIMESTAMP)
    created_at = Column(TIMESTAMP)
    updated_at = Column(TIMESTAMP)
