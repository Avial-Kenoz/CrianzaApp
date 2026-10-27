"""Pedidos de reposición: rellenar los tarros de los carriles desde la bodega.

Se emite con los sacos por carril (sugeridos para durar N días) y, cuando sale
de bodega, se marca "salió": recién ahí se registran las salidas
(FeedExecutionEvent con feed_pedido_line_id) que bajan la bodega principal y
cargan los tarros. Si no sale, se anula. Ver services/feed_pedidos.py.
"""
from sqlalchemy import (BigInteger, Boolean, Column, Date, ForeignKey, Integer, Numeric,
                        String, Text, TIMESTAMP)

from app.db.session import Base


class FeedPedido(Base):
    __tablename__ = "feed_pedidos"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    code = Column(String(40), nullable=False, unique=True)
    status = Column(String(20), nullable=False, default="emitido")   # emitido | salio | anulado
    dias_n = Column(Integer, nullable=False)          # debe durar N días
    dias_min = Column(Integer, nullable=False)        # no rellenar si alcanza para n días
    dias_semana = Column(Integer, nullable=False)     # días de alimentación por semana
    note = Column(Text, nullable=True)
    created_by = Column(BigInteger, ForeignKey("users.id"), nullable=True)
    created_at = Column(TIMESTAMP, nullable=True)
    salio_date = Column(Date, nullable=True)
    salio_by = Column(BigInteger, ForeignKey("users.id"), nullable=True)
    salio_at = Column(TIMESTAMP, nullable=True)
    anulado_by = Column(BigInteger, ForeignKey("users.id"), nullable=True)
    anulado_at = Column(TIMESTAMP, nullable=True)


class FeedPedidoLine(Base):
    """Sacos de un alimento para el tarro de un carril, con el cálculo que
    los sugirió (saldo y ritmo al emitir) para poder revisarlo después."""
    __tablename__ = "feed_pedido_lines"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    pedido_id = Column(BigInteger, ForeignKey("feed_pedidos.id"), nullable=False, index=True)
    pond_id = Column(BigInteger, ForeignKey("ponds.id"), nullable=False, index=True)
    feed_type_id = Column(BigInteger, ForeignKey("feed_types.id"), nullable=False)
    bags = Column(Integer, nullable=False)
    bag_kg = Column(Numeric(8, 3), nullable=False)
    saldo_kg = Column(Numeric(10, 2), nullable=True)
    ritmo_kg = Column(Numeric(10, 2), nullable=True)
    sugerido_bags = Column(Integer, nullable=True)
    limitado_tope = Column(Boolean, nullable=False, default=False)
