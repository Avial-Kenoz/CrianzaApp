"""add_feed_monthly_legacy_tables

Revision ID: f5a6b7c8d9e0
Revises: e4f5a6b7c8d9
Create Date: 2026-05-20

"""
from alembic import op
import sqlalchemy as sa

revision = 'f5a6b7c8d9e0'
down_revision = 'e4f5a6b7c8d9'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'feed_monthly_consumption_legacy',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('year', sa.Integer(), nullable=False),
        sa.Column('month', sa.Integer(), nullable=False),
        sa.Column('pond_code', sa.String(10), nullable=False),
        sa.Column('feed_type_raw', sa.String(120), nullable=False),
        sa.Column('feed_type_key', sa.String(60), nullable=False),
        sa.Column('consumed_kg', sa.Float(), nullable=False),
        sa.Column('source_file', sa.String(200), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('year', 'month', 'pond_code', 'feed_type_key',
                            name='uq_feed_monthly_legacy'),
    )

    op.create_table(
        'feed_monthly_stock_legacy',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('year', sa.Integer(), nullable=False),
        sa.Column('month', sa.Integer(), nullable=False),
        sa.Column('feed_type_key', sa.String(60), nullable=False),
        sa.Column('feed_type_raw', sa.String(120), nullable=False),
        sa.Column('initial_kg', sa.Float(), nullable=True),
        sa.Column('receipts_kg', sa.Float(), nullable=True),
        sa.Column('consumed_kg', sa.Float(), nullable=True),
        sa.Column('final_kg', sa.Float(), nullable=True),
        sa.Column('source_file', sa.String(200), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('year', 'month', 'feed_type_key',
                            name='uq_feed_monthly_stock_legacy'),
    )


def downgrade():
    op.drop_table('feed_monthly_stock_legacy')
    op.drop_table('feed_monthly_consumption_legacy')
