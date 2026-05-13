"""add_feed_module_phase1_tables

Revision ID: b1c2d3e4f5a6
Revises: ae1f2b3c4d5e
Create Date: 2026-05-06 10:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

revision = 'b1c2d3e4f5a6'
down_revision = 'ae1f2b3c4d5e'
branch_labels = None
depends_on = None


def upgrade():
    # Create feed_types table
    op.create_table(
        'feed_types',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('name', sa.String(120), nullable=False, unique=True),
        sa.Column('active', sa.Integer(), default=1, nullable=False),
        sa.Column('created_at', sa.TIMESTAMP(), nullable=True),
        sa.Column('updated_at', sa.TIMESTAMP(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_feed_types_name', 'feed_types', ['name'])

    # Create feed_receipt_headers table
    op.create_table(
        'feed_receipt_headers',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('guia_despacho', sa.String(120), nullable=False, unique=True),
        sa.Column('supplier_name', sa.String(200), nullable=True),
        sa.Column('fecha_ingreso', sa.DateTime(), nullable=False),
        sa.Column('status', sa.String(50), default='draft', nullable=False),
        sa.Column('created_by', sa.String(120), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(), nullable=True),
        sa.Column('confirmed_at', sa.TIMESTAMP(), nullable=True),
        sa.Column('updated_at', sa.TIMESTAMP(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_feed_receipt_headers_guia_despacho', 'feed_receipt_headers', ['guia_despacho'])

    # Create feed_receipt_lines table
    op.create_table(
        'feed_receipt_lines',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('feed_receipt_id', sa.BigInteger(), nullable=False),
        sa.Column('feed_type_id', sa.BigInteger(), nullable=False),
        sa.Column('quantity_kg', sa.Numeric(14, 3), nullable=False),
        sa.Column('bag_weight_kg', sa.Integer(), nullable=False),
        sa.Column('quantity_bags', sa.Integer(), nullable=False),
        sa.Column('supplier_lot', sa.String(120), nullable=True),
        sa.Column('expiration_date', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(), nullable=True),
        sa.Column('updated_at', sa.TIMESTAMP(), nullable=True),
        sa.ForeignKeyConstraint(['feed_receipt_id'], ['feed_receipt_headers.id']),
        sa.ForeignKeyConstraint(['feed_type_id'], ['feed_types.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_feed_receipt_lines_receipt_id', 'feed_receipt_lines', ['feed_receipt_id'])
    op.create_index('ix_feed_receipt_lines_feed_type_id', 'feed_receipt_lines', ['feed_type_id'])

    # Create feed_inventory_adjustments table
    op.create_table(
        'feed_inventory_adjustments',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('feed_type_id', sa.BigInteger(), nullable=False),
        sa.Column('stock_calculated_bags', sa.Integer(), nullable=False),
        sa.Column('stock_real_bags', sa.Integer(), nullable=False),
        sa.Column('difference_bags', sa.Integer(), nullable=False),
        sa.Column('comment', sa.Text(), nullable=False),
        sa.Column('created_by', sa.String(120), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(), nullable=True),
        sa.Column('updated_at', sa.TIMESTAMP(), nullable=True),
        sa.ForeignKeyConstraint(['feed_type_id'], ['feed_types.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_feed_inventory_adjustments_feed_type_id', 'feed_inventory_adjustments', ['feed_type_id'])

    # Create feed_stock_ledger table
    op.create_table(
        'feed_stock_ledger',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('feed_type_id', sa.BigInteger(), nullable=False),
        sa.Column('movement_type', sa.String(50), nullable=False),
        sa.Column('bags_delta', sa.Integer(), nullable=False),
        sa.Column('kg_delta', sa.Numeric(14, 3), nullable=False),
        sa.Column('source_table', sa.String(120), nullable=True),
        sa.Column('source_id', sa.BigInteger(), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(), nullable=True),
        sa.Column('created_by', sa.String(120), nullable=True),
        sa.ForeignKeyConstraint(['feed_type_id'], ['feed_types.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_feed_stock_ledger_feed_type_id', 'feed_stock_ledger', ['feed_type_id'])
    op.create_index('ix_feed_stock_ledger_created_at', 'feed_stock_ledger', ['created_at'])

    # Create feed_programs table (Phase 2)
    op.create_table(
        'feed_programs',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('program_code', sa.String(120), nullable=False, unique=True),
        sa.Column('week_start_date', sa.DateTime(), nullable=False),
        sa.Column('week_end_date', sa.DateTime(), nullable=False),
        sa.Column('status', sa.String(50), default='open', nullable=False),
        sa.Column('closed_reason', sa.String(50), nullable=True),
        sa.Column('created_by', sa.String(120), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(), nullable=True),
        sa.Column('closed_at', sa.TIMESTAMP(), nullable=True),
        sa.Column('updated_at', sa.TIMESTAMP(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_feed_programs_program_code', 'feed_programs', ['program_code'])

    # Create feed_execution_lot_allocations table (Phase 3+)
    op.create_table(
        'feed_execution_lot_allocations',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('feed_execution_event_id', sa.BigInteger(), nullable=False),
        sa.Column('lot_id', sa.BigInteger(), nullable=False),
        sa.Column('allocated_bags', sa.Integer(), nullable=False),
        sa.Column('allocated_kg', sa.Numeric(14, 3), nullable=False),
        sa.Column('allocation_method', sa.String(50), nullable=False),
        sa.Column('biomass_estimated_kg', sa.Numeric(14, 3), nullable=True),
        sa.Column('fish_count_estimated', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(), nullable=True),
        sa.Column('updated_at', sa.TIMESTAMP(), nullable=True),
        sa.ForeignKeyConstraint(['lot_id'], ['lots.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_feed_execution_lot_allocations_lot_id', 'feed_execution_lot_allocations', ['lot_id'])

    # Create accounting_outbox_events table
    op.create_table(
        'accounting_outbox_events',
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('event_type', sa.String(120), nullable=False),
        sa.Column('payload_json', sa.Text(), nullable=False),
        sa.Column('status', sa.String(50), default='pending', nullable=False),
        sa.Column('retry_count', sa.Integer(), default=0, nullable=False),
        sa.Column('created_at', sa.TIMESTAMP(), nullable=True),
        sa.Column('sent_at', sa.TIMESTAMP(), nullable=True),
        sa.Column('last_error', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_accounting_outbox_events_status', 'accounting_outbox_events', ['status'])
    op.create_index('ix_accounting_outbox_events_created_at', 'accounting_outbox_events', ['created_at'])


def downgrade():
    op.drop_index('ix_accounting_outbox_events_created_at', table_name='accounting_outbox_events')
    op.drop_index('ix_accounting_outbox_events_status', table_name='accounting_outbox_events')
    op.drop_table('accounting_outbox_events')
    
    op.drop_index('ix_feed_execution_lot_allocations_lot_id', table_name='feed_execution_lot_allocations')
    op.drop_table('feed_execution_lot_allocations')
    
    op.drop_index('ix_feed_programs_program_code', table_name='feed_programs')
    op.drop_table('feed_programs')
    
    op.drop_index('ix_feed_stock_ledger_created_at', table_name='feed_stock_ledger')
    op.drop_index('ix_feed_stock_ledger_feed_type_id', table_name='feed_stock_ledger')
    op.drop_table('feed_stock_ledger')
    
    op.drop_index('ix_feed_inventory_adjustments_feed_type_id', table_name='feed_inventory_adjustments')
    op.drop_table('feed_inventory_adjustments')
    
    op.drop_index('ix_feed_receipt_lines_feed_type_id', table_name='feed_receipt_lines')
    op.drop_index('ix_feed_receipt_lines_receipt_id', table_name='feed_receipt_lines')
    op.drop_table('feed_receipt_lines')
    
    op.drop_index('ix_feed_receipt_headers_guia_despacho', table_name='feed_receipt_headers')
    op.drop_table('feed_receipt_headers')
    
    op.drop_index('ix_feed_types_name', table_name='feed_types')
    op.drop_table('feed_types')
