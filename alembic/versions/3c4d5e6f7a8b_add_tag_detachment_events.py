"""add tag_detachment_events table

Revision ID: 3c4d5e6f7a8b
Revises: 1b1b2f4a98c1
Create Date: 2026-04-30 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "3c4d5e6f7a8b"
down_revision: Union[str, Sequence[str], None] = "1b1b2f4a98c1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tag_detachment_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("pond_id", sa.BigInteger(), sa.ForeignKey("ponds.id"), nullable=False),
        sa.Column("fish_id", sa.BigInteger(), sa.ForeignKey("fish.id"), nullable=True),
        sa.Column("retag_fish_id", sa.BigInteger(), sa.ForeignKey("fish.id"), nullable=True),
        sa.Column("event_date", sa.TIMESTAMP(), nullable=False),
        sa.Column("registered_by_user_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("status", sa.String(30), nullable=False, server_default="unidentified"),
        sa.Column("created_at", sa.TIMESTAMP(), server_default=sa.text("now()")),
    )
    op.create_index("ix_tag_detachment_pond_id", "tag_detachment_events", ["pond_id"])
    op.create_index("ix_tag_detachment_status", "tag_detachment_events", ["status"])


def downgrade() -> None:
    op.drop_index("ix_tag_detachment_status", "tag_detachment_events")
    op.drop_index("ix_tag_detachment_pond_id", "tag_detachment_events")
    op.drop_table("tag_detachment_events")
