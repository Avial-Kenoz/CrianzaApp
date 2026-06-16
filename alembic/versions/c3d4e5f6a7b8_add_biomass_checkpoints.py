"""add_biomass_checkpoints

Revision ID: c3d4e5f6a7b8
Revises: a0b1c2d3e4f5
Create Date: 2026-05-27

"""
from alembic import op
import sqlalchemy as sa

revision = 'c3d4e5f6a7b8'
down_revision = 'a0b1c2d3e4f5'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "biomass_checkpoints",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("checkpoint_date", sa.Date(), nullable=False),
        sa.Column("checkpoint_type", sa.String(20), nullable=False),
        sa.Column("lot_id", sa.BigInteger(), nullable=False),
        sa.Column("pond_id", sa.BigInteger(), nullable=False),
        sa.Column("fish_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("biomass_kg", sa.Numeric(14, 3), nullable=False, server_default="0"),
        sa.Column("avg_weight_g", sa.Numeric(10, 3), nullable=True),
        sa.Column("source_session_id", sa.BigInteger(), nullable=True),
        sa.Column("computed_at", sa.TIMESTAMP(), nullable=False),
        sa.ForeignKeyConstraint(["lot_id"], ["lots.id"]),
        sa.ForeignKeyConstraint(["pond_id"], ["ponds.id"]),
        sa.ForeignKeyConstraint(["source_session_id"], ["sampling_sessions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "checkpoint_date", "checkpoint_type", "lot_id", "pond_id",
            name="uq_biomass_checkpoint",
        ),
    )
    op.create_index("ix_bmc_lot_date",  "biomass_checkpoints", ["lot_id",  "checkpoint_date"])
    op.create_index("ix_bmc_pond_date", "biomass_checkpoints", ["pond_id", "checkpoint_date"])


def downgrade():
    op.drop_index("ix_bmc_pond_date", table_name="biomass_checkpoints")
    op.drop_index("ix_bmc_lot_date",  table_name="biomass_checkpoints")
    op.drop_table("biomass_checkpoints")
