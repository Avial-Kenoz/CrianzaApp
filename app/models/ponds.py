from sqlalchemy import Column, BigInteger, String, Integer, Boolean, Numeric, TIMESTAMP, ForeignKey, JSON
from app.db.session import Base

class Pond(Base):
    __tablename__ = "ponds"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    legacy_id = Column(BigInteger, unique=True)
    name = Column(String(120), nullable=False)
    internal_id = Column(String(120))
    code = Column(String(120))
    cultivation_unit_id = Column(BigInteger, ForeignKey("cultivation_units.id"))
    pond_type_id = Column(BigInteger, ForeignKey("pond_types.id"))
    lot_id = Column(BigInteger, ForeignKey("lots.id"))
    volume = Column(Integer, default=0)
    depuration = Column(Boolean, default=False)
    state = Column(String(50), default='success')
    biomass = Column(Numeric(14,3))
    biomass_measured = Column(Numeric(14,3))
    biomass_current = Column(Numeric(14,3))
    avg_weight = Column(Numeric(14,3))
    tagged_count = Column(Integer, default=0, nullable=False)
    unregistered_count = Column(Integer, default=0, nullable=False)
    n_fish_cached = Column(Integer, default=0, nullable=False)
    active_lots_count = Column(Integer, default=0, nullable=False)
    active_lot_ids = Column(JSON, default=list)
    unregistered_lot_ids = Column(JSON, default=list)
    unregistered_lot_conflict = Column(Boolean, default=False, nullable=False)
    parent_pond_id = Column(BigInteger, ForeignKey("ponds.id"), nullable=True)
    created_at = Column(TIMESTAMP)
    updated_at = Column(TIMESTAMP)
