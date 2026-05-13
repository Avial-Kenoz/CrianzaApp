"""add resolution fields to tag_detachment_events

Revision ID: 4d5e6f7a8b9c
Revises: 3c4d5e6f7a8b
Create Date: 2026-04-30 19:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "4d5e6f7a8b9c"
down_revision: Union[str, Sequence[str], None] = "3c4d5e6f7a8b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("tag_detachment_events",
        sa.Column("resolution", sa.String(40), nullable=True))
    op.add_column("tag_detachment_events",
        sa.Column("resolved_at", sa.TIMESTAMP(), nullable=True))


def downgrade() -> None:
    op.drop_column("tag_detachment_events", "resolved_at")
    op.drop_column("tag_detachment_events", "resolution")
