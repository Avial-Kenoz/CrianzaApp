from sqlalchemy import Column, BigInteger, String, Integer, Date, TIMESTAMP, ForeignKey, Boolean, Numeric
from app.db.session import Base


class CultivationDeclarationItem(Base):
    __tablename__ = "cultivation_declaration_items"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    declaration_id = Column(
        BigInteger,
        ForeignKey("cultivation_declarations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    fish_id = Column(BigInteger, ForeignKey("fish.id"), nullable=True, index=True)
    pit_tag = Column(String(120), nullable=False)
    lot_label = Column(String(120))
    sex = Column(String(20))
    weight_g = Column(Numeric(12, 3))
    depuration_days = Column(Integer)
    has_drug_use = Column(Boolean, nullable=False, default=False)
    withdrawal_end_date = Column(Date)
    created_at = Column(TIMESTAMP)
    updated_at = Column(TIMESTAMP)