"""Inventario en terreno: conteos del tarro, ajustes con motivo y capacidad.

Revision ID: 20261027_01
Revises: 20261019_01
"""
from alembic import op
import sqlalchemy as sa


revision = "20261027_01"
down_revision = "20261019_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "feed_terreno_counts",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("count_date", sa.Date, nullable=False, index=True),
        sa.Column("momento", sa.String(10), nullable=False, server_default="despues"),
        sa.Column("pond_id", sa.BigInteger, sa.ForeignKey("ponds.id"), nullable=False, index=True),
        sa.Column("feed_type_id", sa.BigInteger, sa.ForeignKey("feed_types.id")),
        sa.Column("paper_kg", sa.Numeric(10, 2), nullable=False),
        sa.Column("counted_kg", sa.Numeric(10, 2), nullable=False),
        sa.Column("is_initial", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("note", sa.Text),
        sa.Column("created_by", sa.BigInteger, sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.TIMESTAMP),
        sa.Column("annulled_at", sa.TIMESTAMP),
        sa.Column("annulled_by", sa.BigInteger, sa.ForeignKey("users.id")),
    )
    op.create_table(
        "feed_terreno_moves",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("move_date", sa.Date, nullable=False, index=True),
        sa.Column("pond_id", sa.BigInteger, sa.ForeignKey("ponds.id"), nullable=False, index=True),
        sa.Column("feed_type_id", sa.BigInteger, sa.ForeignKey("feed_types.id")),
        sa.Column("kg", sa.Numeric(10, 2), nullable=False),
        sa.Column("motivo", sa.String(30), nullable=False),
        sa.Column("count_id", sa.BigInteger, sa.ForeignKey("feed_terreno_counts.id"), index=True),
        sa.Column("related_move_id", sa.BigInteger, sa.ForeignKey("feed_terreno_moves.id")),
        sa.Column("consumo_date", sa.Date),
        sa.Column("bodega_bags", sa.BigInteger),
        sa.Column("ledger_id", sa.BigInteger, sa.ForeignKey("feed_stock_ledger.id")),
        sa.Column("note", sa.Text),
        sa.Column("created_by", sa.BigInteger, sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.TIMESTAMP),
    )
    op.create_table(
        "feed_tarros",
        sa.Column("pond_id", sa.BigInteger, sa.ForeignKey("ponds.id"), primary_key=True),
        sa.Column("capacity_kg", sa.Numeric(8, 1), nullable=False),
        sa.Column("updated_by", sa.BigInteger, sa.ForeignKey("users.id")),
        sa.Column("updated_at", sa.TIMESTAMP),
    )


def downgrade() -> None:
    op.drop_table("feed_tarros")
    op.drop_table("feed_terreno_moves")
    op.drop_table("feed_terreno_counts")
