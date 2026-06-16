from sqlalchemy import Column, BigInteger, Integer, Numeric, Date, TIMESTAMP, String, ForeignKey, UniqueConstraint
from app.db.session import Base


class BiomassCheckpoint(Base):
    __tablename__ = "biomass_checkpoints"

    id                = Column(BigInteger, primary_key=True, autoincrement=True)
    checkpoint_date   = Column(Date, nullable=False)
    checkpoint_type   = Column(String(20), nullable=False)   # 'month_start' | 'sampling'
    lot_id            = Column(BigInteger, ForeignKey("lots.id"), nullable=False)
    pond_id           = Column(BigInteger, ForeignKey("ponds.id"), nullable=False)
    fish_count        = Column(Integer, nullable=False, default=0)
    biomass_kg        = Column(Numeric(14, 3), nullable=False, default=0)
    avg_weight_g      = Column(Numeric(10, 3))
    source_session_id = Column(BigInteger, ForeignKey("sampling_sessions.id"))
    computed_at       = Column(TIMESTAMP, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "checkpoint_date", "checkpoint_type", "lot_id", "pond_id",
            name="uq_biomass_checkpoint",
        ),
    )
