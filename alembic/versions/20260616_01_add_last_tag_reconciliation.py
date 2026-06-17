"""add_last_tag_reconciliation_at to ponds

Revision ID: 20260616_01
Revises: e5f6a7b8c9d0
Create Date: 2026-06-16 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '20260616_01'
down_revision = 'e5f6a7b8c9d0'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('ponds', sa.Column('last_tag_reconciliation_at', sa.TIMESTAMP(), nullable=True))


def downgrade() -> None:
    op.drop_column('ponds', 'last_tag_reconciliation_at')
