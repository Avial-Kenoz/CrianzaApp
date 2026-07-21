"""add water_temp_inherited a pond_oxygen_readings (herencia de temp por ronda)

Una laguna (unidad de cultivo) tiene temperatura homogénea entre sus estanques,
así que el operador mide la temperatura una sola vez por ronda (en el primer
estanque) y los demás la heredan. `water_temp_c` guarda la temperatura efectiva
usada para el cálculo (medida o heredada); este flag marca cuáles fueron
heredadas para preservar la distinción medida/heredada en el panel y auditorías.

Revision ID: 20260721_02
Revises: 20260721_01
Create Date: 2026-07-21 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "20260721_02"
down_revision = "20260721_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "pond_oxygen_readings",
        sa.Column("water_temp_inherited", sa.Boolean(),
                  nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("pond_oxygen_readings", "water_temp_inherited")
