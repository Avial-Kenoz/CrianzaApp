"""add fish_drug_uses table

Revision ID: 5e6f7a8b9cad
Revises: 4d5e6f7a8b9c
Create Date: 2026-05-04 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "5e6f7a8b9cad"
down_revision: Union[str, Sequence[str], None] = "4d5e6f7a8b9c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "fish_drug_uses",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("fish_id", sa.BigInteger(), sa.ForeignKey("fish.id"), nullable=False),
        sa.Column("drug_name", sa.String(length=120), nullable=False),
        sa.Column("dose", sa.String(length=120), nullable=False),
        sa.Column("application_date", sa.TIMESTAMP(), nullable=False),
        sa.Column("withdrawal_period", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("updated_at", sa.TIMESTAMP(), nullable=True),
    )
    op.create_index("ix_fish_drug_uses_fish_id", "fish_drug_uses", ["fish_id"])


def downgrade() -> None:
    op.drop_index("ix_fish_drug_uses_fish_id", "fish_drug_uses")
    op.drop_table("fish_drug_uses")
