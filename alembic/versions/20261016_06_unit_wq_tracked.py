"""Unidades sin seguimiento de calidad de agua (wq_tracked).

Tranque es flujo libre y casi no tiene biomasa: no vale la pena hacerle
rondas de O2 ni alertas de Telegram. wq_tracked = false la saca del panel,
de los formularios y de la app de terreno, y del motor de alertas. Sigue
existiendo como unidad para la ración diaria y el inventario.

Revision ID: 20261016_06
Revises: 20261016_05
"""
from alembic import op
import sqlalchemy as sa


revision = "20261016_06"
down_revision = "20261016_05"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("cultivation_units",
                  sa.Column("wq_tracked", sa.Boolean, nullable=False, server_default=sa.true()))
    op.execute("UPDATE cultivation_units SET wq_tracked = false WHERE name = 'Tranque'")


def downgrade() -> None:
    op.drop_column("cultivation_units", "wq_tracked")
