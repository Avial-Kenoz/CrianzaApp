"""add_feed_program_lines_table

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
Create Date: 2026-05-06 14:30:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = "c2d3e4f5a6b7"
down_revision = "b1c2d3e4f5a6"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "feed_program_lines",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("feed_program_id", sa.BigInteger(), nullable=False),
        sa.Column("pond_id", sa.BigInteger(), nullable=False),
        sa.Column("feed_type_id", sa.BigInteger(), nullable=False),
        sa.Column("planned_bags", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("planned_kg", sa.Numeric(14, 3), nullable=False, server_default="0"),
        sa.Column("executed_bags", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("executed_kg", sa.Numeric(14, 3), nullable=False, server_default="0"),
        sa.Column("remaining_bags", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("remaining_kg", sa.Numeric(14, 3), nullable=False, server_default="0"),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("updated_at", sa.TIMESTAMP(), nullable=True),
        sa.ForeignKeyConstraint(["feed_program_id"], ["feed_programs.id"]),
        sa.ForeignKeyConstraint(["pond_id"], ["ponds.id"]),
        sa.ForeignKeyConstraint(["feed_type_id"], ["feed_types.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("feed_program_id", "pond_id", "feed_type_id", name="uq_feed_program_line_pond_feed"),
    )
    op.create_index("ix_feed_program_lines_program_id", "feed_program_lines", ["feed_program_id"])
    op.create_index("ix_feed_program_lines_pond_id", "feed_program_lines", ["pond_id"])
    op.create_index("ix_feed_program_lines_feed_type_id", "feed_program_lines", ["feed_type_id"])


def downgrade():
    op.drop_index("ix_feed_program_lines_feed_type_id", table_name="feed_program_lines")
    op.drop_index("ix_feed_program_lines_pond_id", table_name="feed_program_lines")
    op.drop_index("ix_feed_program_lines_program_id", table_name="feed_program_lines")
    op.drop_table("feed_program_lines")
