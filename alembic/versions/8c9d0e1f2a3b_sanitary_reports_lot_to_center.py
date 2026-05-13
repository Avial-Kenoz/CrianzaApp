"""sanitary_reports_lot_to_center

Revision ID: 8c9d0e1f2a3b
Revises: 7b8c9d0e1f2a
Create Date: 2026-05-04 13:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = '8c9d0e1f2a3b'
down_revision = '7b8c9d0e1f2a'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_index('ix_sanitary_reports_lot_id', table_name='sanitary_reports')
    op.drop_constraint('sanitary_reports_lot_id_fkey', 'sanitary_reports', type_='foreignkey')
    op.drop_column('sanitary_reports', 'lot_id')
    op.add_column('sanitary_reports', sa.Column('cultivation_unit_id', sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        'sanitary_reports_cultivation_unit_id_fkey',
        'sanitary_reports', 'cultivation_units',
        ['cultivation_unit_id'], ['id']
    )


def downgrade():
    op.drop_constraint('sanitary_reports_cultivation_unit_id_fkey', 'sanitary_reports', type_='foreignkey')
    op.drop_column('sanitary_reports', 'cultivation_unit_id')
    op.add_column('sanitary_reports', sa.Column('lot_id', sa.BigInteger(), nullable=False))
    op.create_foreign_key('sanitary_reports_lot_id_fkey', 'sanitary_reports', 'lots', ['lot_id'], ['id'])
    op.create_index('ix_sanitary_reports_lot_id', 'sanitary_reports', ['lot_id'])
