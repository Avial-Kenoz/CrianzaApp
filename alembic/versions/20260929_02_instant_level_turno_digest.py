"""El consolidado se corta por turno y el criterio pasa a ser el nivel.

Reemplaza a `digest_at` (hora fija diaria, sembrada horas antes en
20260929_01) por un corte que ya existe en el dominio: el cambio de turno.
`o2_day_window` define el turno de dia (08:30-16:00) y todo lo demas es `off`,
asi que el consolidado sale dos veces al dia, cuando cambia la gente. Una hora
fija era un numero inventado; el turno es el ritmo real de la planta.

Y el criterio de "esto interrumpe" deja de depender del tipo para depender del
NIVEL, que es lo que de verdad lo determina:

  - `min_level`     piso para avisar. Bajo esto no sale nada, nunca.
  - `instant_level` desde aqui suena AL INSTANTE. Lo que queda entre los dos se
                    junta y sale en el mensaje del turno.

Con min_level=alerta e instant_level=alarma: las alarmas interrumpen, las
alertas esperan al cambio de turno.

Para que ese criterio funcione sin excepciones por tipo, `ronda_vencida` deja
de nacer en `alarma` (ver el motor): que falte un dato no es un pez en riesgo,
es una falta de dato. Nace `alerta` -- al consolidado -- y escala a `alarma`
cuando lleva `notify_after_min` sin resolverse, que es cuando deja de ser un
atraso y pasa a ser un hueco.

Revision ID: 20260929_02
Revises: 20260929_01
"""
from alembic import op
import sqlalchemy as sa


revision = "20260929_02"
down_revision = "20260929_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("water_quality_alert_rules",
                  sa.Column("instant_level", sa.String(length=20),
                            nullable=False, server_default="alarma"))
    op.drop_column("water_quality_alert_rules", "digest_at")

    # El piso baja a `alerta` en los dos tipos que tienen nivel variable: ahora
    # se puede, porque lo que entra por ahi no suena -- se acumula. Antes del
    # consolidado, admitir `alerta` habria significado mas mensajes; ahora
    # significa mas informacion en el mismo mensaje.
    op.execute(
        "UPDATE water_quality_alert_rules SET min_level = 'alerta' "
        "WHERE kind IN ('lectura_alarma', 'ronda_vencida')"
    )
    op.execute(
        "UPDATE water_quality_alert_rules SET instant_level = 'alarma'"
    )


def downgrade() -> None:
    op.add_column("water_quality_alert_rules",
                  sa.Column("digest_at", sa.Numeric(4, 2)))
    op.execute("UPDATE water_quality_alert_rules SET digest_at = 8.0 "
               "WHERE kind = 'ronda_vencida'")
    op.execute("UPDATE water_quality_alert_rules SET min_level = 'alarma'")
    op.drop_column("water_quality_alert_rules", "instant_level")
