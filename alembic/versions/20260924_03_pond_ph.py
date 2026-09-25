"""pH por estanque en la ronda de oxigeno.

El 24-09-2026 se midio pH en los 30 estanques a las 08:30 y a las 12:00 para
el ensayo de ventilacion, y no habia donde guardarlo: `biofilter_readings`
tiene pH de entrada y salida del reactor, no por estanque.

El lugar natural es la ronda de O2 -- mismo estanque, misma hora, mismo
operador -- y no una tabla aparte. Un pH suelto sin la temperatura y el
oxigeno de ese momento sirve para poco: el CO2 se despeja con pH, alcalinidad
y temperatura, y la temperatura ya viene en esta fila.

Lo que destraba:

  - el diagnostico "pH de la manana hundido", que existe desde ayer y estaba
    mudo porque el dato no entraba. Con la planilla del 24-09 las tres
    lagunas del norte lo cruzan: 6,40 a 6,60 a las 08:30, o sea 28-36 mg/L de
    CO2 con los sopladores encendidos toda la noche.
  - el ensayo de ventilacion del lunes, que necesita pH por laguna para leer
    la respuesta contra el aire que perdio cada una.

Revision ID: 20260924_03
Revises: 20260924_02
"""
from alembic import op
import sqlalchemy as sa


revision = "20260924_03"
down_revision = "20260924_02"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("pond_oxygen_readings", sa.Column("ph", sa.Numeric(4, 2)))


def downgrade():
    op.drop_column("pond_oxygen_readings", "ph")
