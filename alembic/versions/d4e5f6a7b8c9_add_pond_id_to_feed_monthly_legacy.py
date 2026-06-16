"""add_pond_id_to_feed_monthly_legacy

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-06-01

"""
from alembic import op
import sqlalchemy as sa

revision = 'd4e5f6a7b8c9'
down_revision = 'c3d4e5f6a7b8'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'feed_monthly_consumption_legacy',
        sa.Column('pond_id', sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        'fk_feed_monthly_legacy_pond_id',
        'feed_monthly_consumption_legacy', 'ponds',
        ['pond_id'], ['id'],
    )
    op.create_index(
        'ix_feed_monthly_legacy_pond_id',
        'feed_monthly_consumption_legacy',
        ['pond_id'],
    )

    op.execute("""
        UPDATE feed_monthly_consumption_legacy l
        SET pond_id = p.id
        FROM ponds p
        WHERE p.internal_id = l.pond_code
    """)


def downgrade():
    op.drop_index('ix_feed_monthly_legacy_pond_id', table_name='feed_monthly_consumption_legacy')
    op.drop_constraint('fk_feed_monthly_legacy_pond_id', 'feed_monthly_consumption_legacy', type_='foreignkey')
    op.drop_column('feed_monthly_consumption_legacy', 'pond_id')
