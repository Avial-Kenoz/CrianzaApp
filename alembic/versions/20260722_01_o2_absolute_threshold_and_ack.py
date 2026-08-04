"""umbral absoluto de O2 (mg/L) + acknowledgment/acción correctiva en lecturas

Revision ID: 20260722_01
Revises: 20260721_01
Create Date: 2026-07-22 00:00:00.000000

Llamado a la acción ante O2 alarmante:
- Nuevo umbral `o2_do_mg_l` (comparador 'lt', alerta 6 / alarma 5 mg/L): piso
  absoluto independiente de la temperatura, además de la saturación (%).
- Columnas en pond_oxygen_readings: `acknowledged` (el operador reconoció la
  lectura reingresándola) y `corrective_action` (qué acción tomó).
Idempotente: no duplica el umbral si ya existe.
"""
from alembic import op
import sqlalchemy as sa


revision = "20260722_01"
down_revision = "20260721_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "pond_oxygen_readings",
        sa.Column("acknowledged", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "pond_oxygen_readings",
        sa.Column("corrective_action", sa.String(length=120), nullable=True),
    )

    bind = op.get_bind()
    bind.execute(sa.text(
        "INSERT INTO water_quality_thresholds "
        "(parameter, alert_value, alarm_value, comparator, unit, description, active, updated_at) "
        "SELECT 'o2_do_mg_l', 6.0, 5.0, 'lt', 'mg/L', "
        "'O2 disuelto absoluto: piso independiente de la temperatura', true, now() "
        "WHERE NOT EXISTS (SELECT 1 FROM water_quality_thresholds WHERE parameter = 'o2_do_mg_l')"
    ))


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("DELETE FROM water_quality_thresholds WHERE parameter = 'o2_do_mg_l'"))
    op.drop_column("pond_oxygen_readings", "corrective_action")
    op.drop_column("pond_oxygen_readings", "acknowledged")
