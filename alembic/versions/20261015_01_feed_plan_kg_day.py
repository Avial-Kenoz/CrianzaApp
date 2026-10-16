"""Ración planificada por unidad (kg de alimento por día).

La mantención de alcalinidad se calcula con el alimento: dosis = lo que
consume la nitrificación, y la laguna tiende a la alcalinidad de la fuente.
El programa de alimentación (feed_programs) es una reserva de sacos, no una
ración diaria, así que no sirve para proyectar. Null = se usa lo ejecutado
en los últimos 7 días.

Revision ID: 20261015_01
Revises: 20261014_01
"""
from alembic import op
import sqlalchemy as sa


revision = "20261015_01"
down_revision = "20261014_01"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("cultivation_units",
                  sa.Column("feed_plan_kg_day", sa.Numeric(8, 1)))


def downgrade():
    op.drop_column("cultivation_units", "feed_plan_kg_day")
