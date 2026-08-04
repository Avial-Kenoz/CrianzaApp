"""banda operativa de temperatura del sitio (umbral 'water_temp_site')

Revision ID: 20260722_02
Revises: 20260722_01
Create Date: 2026-07-22 01:00:00.000000

Marca sospechosa (pide reingreso) una temperatura fuera del rango operativo del
sitio (alert=mín, alarm=máx; comparator 'band'). Editable en la página de
umbrales. Semilla 8–24 °C (lagunas rara vez <10 o >21 °C). Idempotente.
"""
from alembic import op
import sqlalchemy as sa


revision = "20260722_02"
down_revision = "20260722_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text(
        "INSERT INTO water_quality_thresholds "
        "(parameter, alert_value, alarm_value, comparator, unit, description, active, updated_at) "
        "SELECT 'water_temp_site', 8.0, 24.0, 'band', '°C', "
        "'Rango operativo de temperatura del sitio (mín=alerta, máx=alarma): fuera de la banda la lectura es sospechosa', true, now() "
        "WHERE NOT EXISTS (SELECT 1 FROM water_quality_thresholds WHERE parameter = 'water_temp_site')"
    ))


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("DELETE FROM water_quality_thresholds WHERE parameter = 'water_temp_site'"))
