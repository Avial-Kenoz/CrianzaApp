"""operaciones de sexado offline (idempotencia + bandeja) [PR-S2]

Revision ID: 20260720_03
Revises: 20260720_02
Create Date: 2026-07-20 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "20260720_03"
down_revision = "20260720_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sexing_offline_operations",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("client_uuid", sa.String(length=64), nullable=False, unique=True),
        sa.Column("session_id", sa.BigInteger(), sa.ForeignKey("sexado_offline_sessions.id"), nullable=True),
        sa.Column("pond_id", sa.BigInteger(), sa.ForeignKey("ponds.id"), nullable=True),
        sa.Column("kind", sa.String(length=30), nullable=False, server_default="fish_save"),
        sa.Column("pit", sa.String(length=120), nullable=True),
        sa.Column("fish_id", sa.BigInteger(), sa.ForeignKey("fish.id"), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("captured_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending_review"),
        sa.Column("result_message", sa.String(length=255), nullable=True),
        sa.Column("resolution", sa.JSON(), nullable=True),
        sa.Column("applied_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=True),
    )
    op.create_index("ix_sexing_offline_operations_session_id", "sexing_offline_operations", ["session_id"])
    op.create_index("ix_sexing_offline_operations_status", "sexing_offline_operations", ["status"])


def downgrade() -> None:
    op.drop_index("ix_sexing_offline_operations_status", "sexing_offline_operations")
    op.drop_index("ix_sexing_offline_operations_session_id", "sexing_offline_operations")
    op.drop_table("sexing_offline_operations")
