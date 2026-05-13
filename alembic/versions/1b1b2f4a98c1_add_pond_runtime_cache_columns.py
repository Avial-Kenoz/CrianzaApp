"""add pond runtime cache columns

Revision ID: 1b1b2f4a98c1
Revises: a60b6b25fd16
Create Date: 2026-04-30 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "1b1b2f4a98c1"
down_revision: Union[str, Sequence[str], None] = "a60b6b25fd16"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("ponds", sa.Column("tagged_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("ponds", sa.Column("unregistered_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("ponds", sa.Column("n_fish_cached", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("ponds", sa.Column("active_lots_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("ponds", sa.Column("active_lot_ids", sa.JSON(), nullable=True, server_default="[]"))
    op.add_column("ponds", sa.Column("unregistered_lot_ids", sa.JSON(), nullable=True, server_default="[]"))
    op.add_column("ponds", sa.Column("unregistered_lot_conflict", sa.Boolean(), nullable=False, server_default=sa.text("false")))

    op.alter_column("ponds", "tagged_count", server_default=None)
    op.alter_column("ponds", "unregistered_count", server_default=None)
    op.alter_column("ponds", "n_fish_cached", server_default=None)
    op.alter_column("ponds", "active_lots_count", server_default=None)
    op.alter_column("ponds", "active_lot_ids", server_default=None)
    op.alter_column("ponds", "unregistered_lot_ids", server_default=None)
    op.alter_column("ponds", "unregistered_lot_conflict", server_default=None)


def downgrade() -> None:
    op.drop_column("ponds", "unregistered_lot_conflict")
    op.drop_column("ponds", "unregistered_lot_ids")
    op.drop_column("ponds", "active_lot_ids")
    op.drop_column("ponds", "active_lots_count")
    op.drop_column("ponds", "n_fish_cached")
    op.drop_column("ponds", "unregistered_count")
    op.drop_column("ponds", "tagged_count")
