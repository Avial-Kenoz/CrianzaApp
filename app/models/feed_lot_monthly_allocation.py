from sqlalchemy import Column, BigInteger, Integer, Numeric, String, TIMESTAMP, ForeignKey, UniqueConstraint
from app.db.session import Base


class FeedLotMonthlyAllocation(Base):
    __tablename__ = "feed_lot_monthly_allocations"

    id               = Column(BigInteger, primary_key=True, autoincrement=True)
    year             = Column(Integer, nullable=False)
    month            = Column(Integer, nullable=False)
    pond_id          = Column(BigInteger, ForeignKey("ponds.id"), nullable=False)
    lot_id           = Column(BigInteger, ForeignKey("lots.id"), nullable=False)
    source           = Column(String(20), nullable=False)   # 'legacy' | 'app'
    consumed_kg_pond = Column(Numeric(14, 3), nullable=False)  # total feed al estanque ese mes
    lot_biomass_kg   = Column(Numeric(14, 3))                  # biomasa del lote en el estanque
    pond_biomass_kg  = Column(Numeric(14, 3))                  # biomasa total del estanque
    lot_share        = Column(Numeric(10, 6))                  # proporción asignada
    allocated_kg     = Column(Numeric(14, 3), nullable=False)  # feed atribuido al lote
    checkpoint_date  = Column(String(10))                      # fecha del checkpoint usado ('YYYY-MM-DD')
    computed_at      = Column(TIMESTAMP, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "year", "month", "pond_id", "lot_id", "source",
            name="uq_feed_lot_monthly_alloc",
        ),
    )
