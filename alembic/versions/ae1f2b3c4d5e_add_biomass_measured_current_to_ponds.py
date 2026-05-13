"""add biomass_measured and biomass_current to ponds

Revision ID: ae1f2b3c4d5e
Revises: 9d0e1f2a3b4c
Create Date: 2026-05-05 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "ae1f2b3c4d5e"
down_revision: Union[str, Sequence[str], None] = "9d0e1f2a3b4c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("ponds", sa.Column("biomass_measured", sa.Numeric(14, 3), nullable=True))
    op.add_column("ponds", sa.Column("biomass_current", sa.Numeric(14, 3), nullable=True))


def downgrade() -> None:
    op.drop_column("ponds", "biomass_current")
    op.drop_column("ponds", "biomass_measured")
