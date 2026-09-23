from sqlalchemy import Column, BigInteger, String, Numeric, Boolean, Date, TIMESTAMP, ForeignKey
from app.db.session import Base


class SilageAcidLot(Base):
    """Lote de ácido recibido para el ensilaje. `lot_code` es el "Lote ácido"
    que el inspector anota en la revisión semanal."""

    __tablename__ = "silage_acid_lots"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    lot_code = Column(String(60), nullable=False, unique=True)
    supplier = Column(String(120))
    received_date = Column(Date)
    received_lts = Column(Numeric(10, 2), nullable=False, default=0)
    concentration = Column(String(40))
    notes = Column(String(255))
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(TIMESTAMP)
    updated_at = Column(TIMESTAMP)


class SilageAcidMovement(Base):
    """Movimiento de inventario de ácido.

    **Disponible = Σ ingresos + Σ ajustes − Σ consumos.** Es el mismo criterio
    que el inventario de alimento de CrianzaApp, a propósito: no conviene tener
    dos semánticas de stock distintas en la misma app.

    `movement_type`: `in` (recepción de lote) | `out` (consumo de una carga) |
    `adjustment` (corrección manual, y también el contra-movimiento que genera
    anular una carga).
    """

    __tablename__ = "silage_acid_movements"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    lot_id = Column(BigInteger, ForeignKey("silage_acid_lots.id"), index=True)
    movement_date = Column(Date, nullable=False)
    movement_type = Column(String(20), nullable=False)
    lts = Column(Numeric(10, 2), nullable=False)

    # Consumo que originó el egreso (NULL en ingresos y ajustes manuales).
    grinding_event_id = Column(BigInteger, ForeignKey("silage_grinding_events.id"), index=True)
    drum_id = Column(BigInteger, ForeignKey("silage_drums.id"))

    user_id = Column(BigInteger, ForeignKey("users.id"))
    notes = Column(String(255))
    created_at = Column(TIMESTAMP)
