"""Inventario en terreno: el tarro de cada carril (bodega intermedia).

Las salidas de bodega cargan el tarro y la ración confirmada lo descarga; el
saldo sale de ahí (ver services/feed_terreno.py). Estas tablas guardan lo que
el papel no sabe: los conteos del tarro y cómo se explica cada diferencia.
"""
from sqlalchemy import (Boolean, BigInteger, Column, Date, ForeignKey, Numeric,
                        String, Text, TIMESTAMP)

from app.db.session import Base


class FeedTerrenoCount(Base):
    """Conteo del tarro de un carril para un alimento.

    paper_kg es el saldo que decía el sistema al contar; la diferencia con
    counted_kg se reparte en movimientos (FeedTerrenoMove) con su motivo.
    momento: 'antes' (antes de alimentar ese día) | 'despues' (al cierre).
    """
    __tablename__ = "feed_terreno_counts"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    count_date = Column(Date, nullable=False, index=True)
    momento = Column(String(10), nullable=False, default="despues")
    pond_id = Column(BigInteger, ForeignKey("ponds.id"), nullable=False, index=True)
    feed_type_id = Column(BigInteger, ForeignKey("feed_types.id"), nullable=True)
    paper_kg = Column(Numeric(10, 2), nullable=False)
    counted_kg = Column(Numeric(10, 2), nullable=False)
    is_initial = Column(Boolean, nullable=False, default=False)
    note = Column(Text, nullable=True)
    created_by = Column(BigInteger, ForeignKey("users.id"), nullable=True)
    created_at = Column(TIMESTAMP, nullable=True)
    annulled_at = Column(TIMESTAMP, nullable=True)
    annulled_by = Column(BigInteger, ForeignKey("users.id"), nullable=True)


class FeedTerrenoMove(Base):
    """Ajuste del saldo de un tarro (kg con signo: + entra, − sale).

    motivo: ver MOTIVOS en services/feed_terreno.py. Los que tocan la bodega
    principal dejan bodega_bags (con signo, efecto en bodega) y su
    movimiento en feed_stock_ledger (ledger_id). Un traspaso son dos
    movimientos enlazados por related_move_id.
    """
    __tablename__ = "feed_terreno_moves"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    move_date = Column(Date, nullable=False, index=True)
    pond_id = Column(BigInteger, ForeignKey("ponds.id"), nullable=False, index=True)
    feed_type_id = Column(BigInteger, ForeignKey("feed_types.id"), nullable=True)
    kg = Column(Numeric(10, 2), nullable=False)
    motivo = Column(String(30), nullable=False)
    count_id = Column(BigInteger, ForeignKey("feed_terreno_counts.id"), nullable=True, index=True)
    related_move_id = Column(BigInteger, ForeignKey("feed_terreno_moves.id"), nullable=True)
    consumo_date = Column(Date, nullable=True)       # "dado sin registrar": qué día se dio
    bodega_bags = Column(BigInteger, nullable=True)
    ledger_id = Column(BigInteger, ForeignKey("feed_stock_ledger.id"), nullable=True)
    note = Column(Text, nullable=True)
    created_by = Column(BigInteger, ForeignKey("users.id"), nullable=True)
    created_at = Column(TIMESTAMP, nullable=True)


class FeedTarro(Base):
    """Capacidad del tarro de un carril (tope para la alerta de saldo inusual)."""
    __tablename__ = "feed_tarros"

    pond_id = Column(BigInteger, ForeignKey("ponds.id"), primary_key=True)
    capacity_kg = Column(Numeric(8, 1), nullable=False)
    updated_by = Column(BigInteger, ForeignKey("users.id"), nullable=True)
    updated_at = Column(TIMESTAMP, nullable=True)
