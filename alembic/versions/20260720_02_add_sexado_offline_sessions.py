"""sesiones de sexado offline (checkout + bloqueo de estanques) [PR-S1]

Revision ID: 20260720_02
Revises: 20260720_01
Create Date: 2026-07-20 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "20260720_02"
down_revision = "20260720_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sexado_offline_sessions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("token", sa.String(length=64), nullable=False, unique=True),
        sa.Column("operator_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("source_pond_id", sa.BigInteger(), sa.ForeignKey("ponds.id"), nullable=False),
        sa.Column("pond_ids", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("released_at", sa.TIMESTAMP(), nullable=True),
    )
    op.create_index("ix_sexado_offline_sessions_status", "sexado_offline_sessions", ["status"])


def downgrade() -> None:
    op.drop_index("ix_sexado_offline_sessions_status", "sexado_offline_sessions")
    op.drop_table("sexado_offline_sessions")
