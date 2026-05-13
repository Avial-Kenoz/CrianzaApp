from sqlalchemy import Column, BigInteger, Integer, Numeric, TIMESTAMP, ForeignKey, UniqueConstraint
from app.db.session import Base


class PondLotStats(Base):
    __tablename__ = "pond_lot_stats"

    id         = Column(BigInteger, primary_key=True, autoincrement=True)
    pond_id    = Column(BigInteger, ForeignKey("ponds.id"), nullable=False)
    lot_id     = Column(BigInteger, ForeignKey("lots.id"), nullable=False)
    avg_weight = Column(Numeric(10, 3))   # gramos
    avg_length = Column(Numeric(10, 3))   # mm
    condition_k = Column(Numeric(8, 4))  # K = (g / cm³) × 100
    n_sampled  = Column(Integer, default=0)
    last_sampling_session_id = Column(BigInteger, ForeignKey("sampling_sessions.id"))
    updated_at = Column(TIMESTAMP)

    __table_args__ = (UniqueConstraint("pond_id", "lot_id", name="uq_pond_lot_stats"),)
