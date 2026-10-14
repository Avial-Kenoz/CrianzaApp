"""Alza de pH de la tarde por unidad.

El techo de amonio se calculaba con el pH del muestreo del biofiltro, que es
de la manana. A esa hora el NH3 casi no existe y el techo salia en 1.169
kg/dia; la sal se dimensionaba para alcanzarlo. Mientras no haya pH por hora,
el pico de la tarde se estima como pH medido + esta alza. Null = 0,40.

Revision ID: 20261014_01
Revises: 20260930_03
"""
from alembic import op
import sqlalchemy as sa


revision = "20261014_01"
down_revision = "20260930_03"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("cultivation_units",
                  sa.Column("ph_afternoon_rise", sa.Numeric(4, 2)))


def downgrade():
    op.drop_column("cultivation_units", "ph_afternoon_rise")
