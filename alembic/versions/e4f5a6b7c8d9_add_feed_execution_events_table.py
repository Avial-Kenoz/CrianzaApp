"""add_feed_execution_events_table

Revision ID: e4f5a6b7c8d9
Revises: d3e4f5a6b7c8
Create Date: 2026-05-11 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "e4f5a6b7c8d9"
down_revision = "d3e4f5a6b7c8"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "feed_execution_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("feed_program_id", sa.BigInteger(), nullable=False),
        sa.Column("feed_program_line_id", sa.BigInteger(), nullable=True),
        sa.Column("pond_id", sa.BigInteger(), nullable=False),
        sa.Column("feed_type_id", sa.BigInteger(), nullable=False),
        sa.Column("confirmed_bags", sa.Integer(), nullable=False),
        sa.Column("confirmed_kg", sa.Numeric(14, 3), nullable=False, server_default="0"),
        sa.Column("is_unplanned", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("confirmed_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("confirmed_by", sa.String(120), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["feed_program_id"], ["feed_programs.id"]),
        sa.ForeignKeyConstraint(["feed_program_line_id"], ["feed_program_lines.id"]),
        sa.ForeignKeyConstraint(["pond_id"], ["ponds.id"]),
        sa.ForeignKeyConstraint(["feed_type_id"], ["feed_types.id"]),
    )
    op.create_index("ix_feed_exec_events_program_id", "feed_execution_events", ["feed_program_id"])
    op.create_index("ix_feed_exec_events_pond_ft", "feed_execution_events", ["feed_program_id", "pond_id", "feed_type_id"])


def downgrade():
    op.drop_index("ix_feed_exec_events_pond_ft", table_name="feed_execution_events")
    op.drop_index("ix_feed_exec_events_program_id", table_name="feed_execution_events")
    op.drop_table("feed_execution_events")
