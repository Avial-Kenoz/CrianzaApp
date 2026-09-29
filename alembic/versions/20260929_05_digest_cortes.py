"""El corte del consolidado se separa del turno de las rondas: 10:00 y 19:00.

Hasta ahora los mensajes de rutina salian en los cortes de `o2_day_window`
(08:30 y 16:00). Tenia una logica -- el ritmo real de la planta -- pero acopla
dos cosas que no son la misma: esa ventana ES el turno con que se miden las
rondas de O2 (4 h de dia, 2 h el resto), asi que mover la hora de los mensajes
habria movido tambien los plazos de deteccion. Cambiar cuando se lee un
resumen no puede cambiar cuando se considera vencida una ronda.

Por eso el corte pasa a ser configuracion propia, `digest_cortes`, con el
mismo par (alert, alarm) que usa `o2_day_window`: 10,0 y 19,0. La fila no
aparece en la pantalla de Umbrales -- esa solo lista los parametros de
THRESHOLD_ORDER -- porque no es un umbral de medicion sino de aviso; se edita
en Alertas -> Reglas, junto al resto de la configuracion de notificaciones.

De paso el domingo deja de ser un turno unico para el consolidado. Esa
excepcion venia de `o2_round` (el domingo las rondas usan el intervalo corto
las 24 h) y no tiene sentido para los mensajes: el resumen sale a las 10 y a
las 19 todos los dias, sin excepciones que haya que recordar.

Revision ID: 20260929_05
Revises: 20260929_04
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.sql import table, column


revision = "20260929_05"
down_revision = "20260929_04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    t = table("water_quality_thresholds",
              column("parameter", sa.String), column("alert_value", sa.Numeric),
              column("alarm_value", sa.Numeric), column("comparator", sa.String),
              column("unit", sa.String), column("description", sa.String),
              column("active", sa.Boolean))
    op.bulk_insert(t, [{
        "parameter": "digest_cortes", "alert_value": 10.0, "alarm_value": 19.0,
        "comparator": "gt", "unit": "h",
        "description": ("Horas a las que sale el mensaje consolidado de rutina "
                        "(10,0 = 10:00). No afecta los plazos de ronda."),
        "active": True,
    }])


def downgrade() -> None:
    op.execute("DELETE FROM water_quality_thresholds "
               "WHERE parameter = 'digest_cortes'")
