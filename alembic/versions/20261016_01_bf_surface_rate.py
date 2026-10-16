"""Umbral editable: remocion maxima de TAN por m2 de medio (techo por superficie).

El techo de amonio supone eficiencia constante; con mas carga la biopelicula
se satura y manda la tasa por m2. 0,30 g/m2/d a 13,6 C y pH 7,5 es el valor
medio de literatura MBBR en agua fria; en el sitio aun no se ha medido.

Revision ID: 20261016_01
Revises: 20261015_01
"""
from alembic import op


revision = "20261016_01"
down_revision = "20261015_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "INSERT INTO water_quality_thresholds "
        "(parameter, alert_value, alarm_value, comparator, unit, description, active) "
        "SELECT 'bf_surface_rate', 0.30, NULL, 'gt', 'g/m²·d', "
        "'Remocion maxima de TAN por m2 de medio a 13,6 C y pH 7,5 (techo por superficie)', true "
        "WHERE NOT EXISTS (SELECT 1 FROM water_quality_thresholds WHERE parameter = 'bf_surface_rate')"
    )


def downgrade() -> None:
    op.execute("DELETE FROM water_quality_thresholds WHERE parameter = 'bf_surface_rate'")
