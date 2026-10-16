"""Ración diaria por carril (feed_rations).

Capa aparte de la bodega: se programa por día y carril lo que se echa al agua
(hasta 4 días adelante) y al volver se confirma alimentado / no alimentado.

Revision ID: 20261016_03
Revises: 20261016_02
"""
from alembic import op
import sqlalchemy as sa


revision = "20261016_03"
down_revision = "20261016_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "feed_rations",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("ration_date", sa.Date, nullable=False),
        sa.Column("pond_id", sa.BigInteger, sa.ForeignKey("ponds.id"), nullable=False),
        sa.Column("cultivation_unit_id", sa.BigInteger, sa.ForeignKey("cultivation_units.id")),
        sa.Column("feed_type_id", sa.BigInteger, sa.ForeignKey("feed_types.id")),
        sa.Column("kg_planned", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("split_mode", sa.String(20)),
        sa.Column("status", sa.String(20), nullable=False, server_default="programado"),
        sa.Column("kg_fed", sa.Numeric(10, 2)),
        sa.Column("planned_by", sa.BigInteger, sa.ForeignKey("users.id")),
        sa.Column("planned_at", sa.TIMESTAMP),
        sa.Column("confirmed_by", sa.BigInteger, sa.ForeignKey("users.id")),
        sa.Column("confirmed_at", sa.TIMESTAMP),
        sa.Column("note", sa.Text),
        sa.UniqueConstraint("ration_date", "pond_id", name="uq_feed_rations_date_pond"),
    )
    op.create_index("ix_feed_rations_ration_date", "feed_rations", ["ration_date"])
    op.create_index("ix_feed_rations_pond_id", "feed_rations", ["pond_id"])


def downgrade() -> None:
    op.drop_table("feed_rations")
