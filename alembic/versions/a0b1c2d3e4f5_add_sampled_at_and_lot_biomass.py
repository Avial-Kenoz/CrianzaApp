"""add_sampled_at_to_pond_lot_stats_and_biomass_current_to_lots

Revision ID: a0b1c2d3e4f5
Revises: f5a6b7c8d9e0
Create Date: 2026-05-26

"""
from alembic import op
import sqlalchemy as sa

revision = 'a0b1c2d3e4f5'
down_revision = 'f5a6b7c8d9e0'
branch_labels = None
depends_on = None


def upgrade():
    # pond_lot_stats.sampled_at: fecha real del muestreo (≠ updated_at que es cuando se escribió en BD)
    op.add_column("pond_lot_stats", sa.Column("sampled_at", sa.Date(), nullable=True))
    # Inicialización best-effort: usar la fecha de updated_at para registros históricos
    op.execute("UPDATE pond_lot_stats SET sampled_at = CAST(updated_at AS DATE) WHERE updated_at IS NOT NULL")

    # lots.biomass_current: caché de biomasa total del lote en kg
    op.add_column("lots", sa.Column("biomass_current", sa.Numeric(14, 3), nullable=True))


def downgrade():
    op.drop_column("pond_lot_stats", "sampled_at")
    op.drop_column("lots", "biomass_current")
