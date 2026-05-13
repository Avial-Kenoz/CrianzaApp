"""add_sanitary_reports_table

Revision ID: 7b8c9d0e1f2a
Revises: 6a7b8c9d0e1f
Create Date: 2026-05-04 12:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = '7b8c9d0e1f2a'
down_revision = '6a7b8c9d0e1f'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'sanitary_reports',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('lot_id', sa.BigInteger(), nullable=False),
        sa.Column('report_number', sa.String(120), nullable=False),
        sa.Column('laboratory', sa.String(200), nullable=False),
        sa.Column('report_date', sa.Date(), nullable=False),
        sa.Column('created_at', sa.TIMESTAMP(), nullable=True),
        sa.Column('updated_at', sa.TIMESTAMP(), nullable=True),
        sa.ForeignKeyConstraint(['lot_id'], ['lots.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_sanitary_reports_lot_id', 'sanitary_reports', ['lot_id'])


def downgrade():
    op.drop_index('ix_sanitary_reports_lot_id', table_name='sanitary_reports')
    op.drop_table('sanitary_reports')
