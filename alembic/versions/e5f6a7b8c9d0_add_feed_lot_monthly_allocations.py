"""add feed_lot_monthly_allocations table

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-06-02 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "e5f6a7b8c9d0"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "feed_lot_monthly_allocations",
        sa.Column("id",               sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("year",             sa.Integer(),    nullable=False),
        sa.Column("month",            sa.Integer(),    nullable=False),
        sa.Column("pond_id",          sa.BigInteger(), sa.ForeignKey("ponds.id"), nullable=False),
        sa.Column("lot_id",           sa.BigInteger(), sa.ForeignKey("lots.id"),  nullable=False),
        sa.Column("source",           sa.String(20),   nullable=False),
        sa.Column("consumed_kg_pond", sa.Numeric(14, 3), nullable=False),
        sa.Column("lot_biomass_kg",   sa.Numeric(14, 3)),
        sa.Column("pond_biomass_kg",  sa.Numeric(14, 3)),
        sa.Column("lot_share",        sa.Numeric(10, 6)),
        sa.Column("allocated_kg",     sa.Numeric(14, 3), nullable=False),
        sa.Column("checkpoint_date",  sa.String(10)),
        sa.Column("computed_at",      sa.TIMESTAMP(), nullable=False),
        sa.UniqueConstraint(
            "year", "month", "pond_id", "lot_id", "source",
            name="uq_feed_lot_monthly_alloc",
        ),
    )
    op.create_index("ix_feed_lot_monthly_alloc_lot_ym",
                    "feed_lot_monthly_allocations", ["lot_id", "year", "month"])
    op.create_index("ix_feed_lot_monthly_alloc_pond_ym",
                    "feed_lot_monthly_allocations", ["pond_id", "year", "month"])


def downgrade():
    op.drop_table("feed_lot_monthly_allocations")
