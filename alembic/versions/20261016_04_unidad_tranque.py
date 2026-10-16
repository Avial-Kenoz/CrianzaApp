"""El estanque Tranque es una unidad de cultivo en sí misma.

Estaba sin unidad (cultivation_unit_id NULL) y en la ración diaria salía como
"Sin unidad". Se crea la unidad Tranque, sin recirculación (no tiene biofiltro:
el panel de calidad de agua la trata como flujo abierto, igual que Central), y
se le asigna el estanque.

Revision ID: 20261016_04
Revises: 20261016_03
"""
from alembic import op


revision = "20261016_04"
down_revision = "20261016_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        INSERT INTO cultivation_units
            (name, is_recirculating, flow_is_nominal, reactor_is_aerated,
             water_volume_is_estimated, chloride_is_measured, sampling_regime,
             created_at, updated_at)
        SELECT 'Tranque', false, true, true, true, false, 'basal', now(), now()
         WHERE NOT EXISTS (SELECT 1 FROM cultivation_units WHERE name = 'Tranque')
    """)
    op.execute("""
        UPDATE ponds
           SET cultivation_unit_id = (SELECT id FROM cultivation_units WHERE name = 'Tranque')
         WHERE name = 'Tranque' AND cultivation_unit_id IS NULL
    """)


def downgrade() -> None:
    op.execute("""
        UPDATE ponds SET cultivation_unit_id = NULL
         WHERE cultivation_unit_id = (SELECT id FROM cultivation_units WHERE name = 'Tranque')
    """)
    op.execute("DELETE FROM cultivation_units WHERE name = 'Tranque'")
