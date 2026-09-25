"""El intervalo de ronda de O2 pasa a ser editable.

Estaba como constantes en water_quality.py. La ronda no es la misma todo el
tiempo -- cada 4 h en el turno de dia de lunes a sabado, cada 2 h el resto y
el domingo completo -- y ese horario puede cambiar: durante la terapia de
bicarbonato el turno de dia baja a 2 h, y ahi habria que editar el archivo y
reiniciar la app.

Se reutiliza la forma de la tabla: `alert_value` es el intervalo de ronda y
`alarm_value` la antiguedad a la que la lectura se da por vencida (un
intervalo y medio: se salto una ronda). La vista de umbrales les pone
encabezados propios -- "Intervalo" y "Vence" -- porque en estas filas el par
no significa alerta/alarma.

`o2_day_window` guarda el turno de dia en horas decimales: 8,5 = 08:30.

Revision ID: 20260925_01
Revises: 20260924_03
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.sql import table, column


revision = "20260925_01"
down_revision = "20260924_03"
branch_labels = None
depends_on = None

FILAS = [
    ("o2_round_day", 4.0, 6.0, "h",
     "Ronda de O2: turno de dia lun-sab. Vence al saltarse una ronda."),
    ("o2_round_off", 2.0, 3.0, "h",
     "Ronda de O2: 16:00-08:30 todos los dias y domingo completo."),
    ("o2_day_window", 8.5, 16.0, "h",
     "Turno de dia: hora de inicio y de termino (8,5 = 08:30)."),
]


def upgrade():
    t = table("water_quality_thresholds",
              column("parameter", sa.String), column("alert_value", sa.Numeric),
              column("alarm_value", sa.Numeric), column("comparator", sa.String),
              column("unit", sa.String), column("description", sa.String),
              column("active", sa.Boolean))
    op.bulk_insert(t, [
        {"parameter": p, "alert_value": a, "alarm_value": b, "comparator": "gt",
         "unit": u, "description": d, "active": True}
        for p, a, b, u, d in FILAS
    ])


def downgrade():
    op.execute("DELETE FROM water_quality_thresholds WHERE parameter IN "
               "('o2_round_day','o2_round_off','o2_day_window')")
