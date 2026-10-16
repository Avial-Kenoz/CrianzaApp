"""Umbral editable de CO2 libre (co2_mg_l): alerta 30, alarma 45 mg/L.

La fila del biofiltro de la tarjeta pasa a pintarse solo por umbrales
químicos (amonio no ionizado, nitrito y CO2). El CO2 se estima de pH +
alcalinidad de la entrada. 20 mg/L es el criterio de salmónidos, pero la
fuente de este sitio ya trae 25-48: con 30/45 solo marca lo excepcional.

Revision ID: 20261016_07
Revises: 20261016_06
"""
from alembic import op


revision = "20261016_07"
down_revision = "20261016_06"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "INSERT INTO water_quality_thresholds "
        "(parameter, alert_value, alarm_value, comparator, unit, description, active) "
        "SELECT 'co2_mg_l', 30, 45, 'gt', 'mg/L', "
        "'CO2 libre estimado de pH y alcalinidad de la entrada del biofiltro', true "
        "WHERE NOT EXISTS (SELECT 1 FROM water_quality_thresholds WHERE parameter = 'co2_mg_l')"
    )


def downgrade() -> None:
    op.execute("DELETE FROM water_quality_thresholds WHERE parameter = 'co2_mg_l'")
