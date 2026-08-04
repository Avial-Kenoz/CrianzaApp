"""temperatura alta como alarma biológica (reemplaza la banda sospechosa)

Revision ID: 20260724_01
Revises: 20260722_02
Create Date: 2026-07-24 00:00:00.000000

El esturión es pez de agua fría: sobre ~24 °C hay estrés térmico y el agua
retiene menos O2. La temperatura alta pasa de ser "dato sospechoso" (banda
'water_temp_site' 8–24) a **alarma biológica** ('water_temp', comparator 'gt',
alerta 23 / alarma 25). La detección de glitch de sensor queda en el código
(rango físico 0–45 °C). Idempotente.
"""
from alembic import op
import sqlalchemy as sa


revision = "20260724_01"
down_revision = "20260722_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    # Fuera la banda sospechosa 8–24.
    bind.execute(sa.text("DELETE FROM water_quality_thresholds WHERE parameter = 'water_temp_site'"))
    # Alarma biológica por temperatura alta.
    bind.execute(sa.text(
        "INSERT INTO water_quality_thresholds "
        "(parameter, alert_value, alarm_value, comparator, unit, description, active, updated_at) "
        "SELECT 'water_temp', 23.0, 25.0, 'gt', '°C', "
        "'Temperatura del agua: alarma biológica por temperatura alta (estrés térmico del esturión)', true, now() "
        "WHERE NOT EXISTS (SELECT 1 FROM water_quality_thresholds WHERE parameter = 'water_temp')"
    ))


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("DELETE FROM water_quality_thresholds WHERE parameter = 'water_temp'"))
    bind.execute(sa.text(
        "INSERT INTO water_quality_thresholds "
        "(parameter, alert_value, alarm_value, comparator, unit, description, active, updated_at) "
        "SELECT 'water_temp_site', 8.0, 24.0, 'band', '°C', "
        "'Rango operativo de temperatura del sitio', true, now() "
        "WHERE NOT EXISTS (SELECT 1 FROM water_quality_thresholds WHERE parameter = 'water_temp_site')"
    ))
