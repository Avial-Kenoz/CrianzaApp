"""optimize pond detail cache and indexes

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
Create Date: 2026-05-07 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa


revision = "d3e4f5a6b7c8"
down_revision = "c2d3e4f5a6b7"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("ponds", sa.Column("unregistered_balances_by_lot", sa.JSON(), nullable=True, server_default="{}"))
    op.add_column("ponds", sa.Column("runtime_cache_updated_at", sa.TIMESTAMP(), nullable=True))

    op.alter_column("ponds", "unregistered_balances_by_lot", server_default=None)

    op.create_index(
        "ix_ponds_movements_destiny_fishid_time_id",
        "ponds_movements",
        ["destiny_pond_id", "fish_id", "movement_time", "id"],
    )
    op.create_index(
        "ix_ponds_movements_fish_time_id",
        "ponds_movements",
        ["fish_id", "movement_time", "id"],
    )
    op.create_index(
        "ix_ponds_movements_destiny_lot_unmarked",
        "ponds_movements",
        ["destiny_pond_id", "lot_id"],
        postgresql_where=sa.text("fish_id IS NULL"),
    )
    op.create_index(
        "ix_ponds_movements_source_lot_unmarked",
        "ponds_movements",
        ["source_pond_id", "lot_id"],
        postgresql_where=sa.text("fish_id IS NULL"),
    )

    op.create_index(
        "ix_fish_samplings_fish_registry_created",
        "fish_samplings",
        ["fish_id", "registry_time", "created_at"],
    )


def downgrade():
    op.drop_index("ix_fish_samplings_fish_registry_created", table_name="fish_samplings")

    op.drop_index("ix_ponds_movements_source_lot_unmarked", table_name="ponds_movements")
    op.drop_index("ix_ponds_movements_destiny_lot_unmarked", table_name="ponds_movements")
    op.drop_index("ix_ponds_movements_fish_time_id", table_name="ponds_movements")
    op.drop_index("ix_ponds_movements_destiny_fishid_time_id", table_name="ponds_movements")

    op.drop_column("ponds", "runtime_cache_updated_at")
    op.drop_column("ponds", "unregistered_balances_by_lot")
