"""quién cerró el tambor y por qué (cierre sin carga asociada)

Revision ID: 20260812_01
Revises: 20260810_02
Create Date: 2026-08-12 00:00:00.000000

Hasta ahora un tambor solo se podía sellar CON una carga (el operador marcaba
"cerré el tambor con esta carga"), así que el autor del cierre se deducía del
operador de esa carga.

Aparece un caso real que no estaba cubierto: el tambor va casi lleno y la
molienda del día no cabe, así que el operador la echa en un tambor nuevo y da por
terminado el anterior **sin cargarle nada**. Ese cierre no tiene carga de la que
deducir nada, de ahí estas dos columnas.
"""
from alembic import op
import sqlalchemy as sa


revision = "20260812_01"
down_revision = "20260810_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("silage_drums", sa.Column("sealed_by", sa.String(length=120)))
    op.add_column("silage_drums", sa.Column("seal_note", sa.String(length=255)))


def downgrade() -> None:
    op.drop_column("silage_drums", "seal_note")
    op.drop_column("silage_drums", "sealed_by")
