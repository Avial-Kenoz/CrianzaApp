"""add parent_pond_id to ponds

Revision ID: 6a7b8c9d0e1f
Revises: 5e6f7a8b9cad
Create Date: 2026-05-04 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision: str = "6a7b8c9d0e1f"
down_revision: str = "5e6f7a8b9cad"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ponds",
        sa.Column("parent_pond_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        "fk_ponds_parent_pond_id",
        "ponds",
        "ponds",
        ["parent_pond_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_ponds_parent_pond_id", "ponds", type_="foreignkey")
    op.drop_column("ponds", "parent_pond_id")
