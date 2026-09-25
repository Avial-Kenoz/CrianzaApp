"""Caudal de recirculacion NOMINAL por unidad.

Se habia dejado deliberadamente fuera porque no se mide en continuo y el aforo
es caro: el indicador de salud del biofiltro se construyo para NO depender de
el (Q se cancela entre el balance de masa y la cinetica).

Pero sin Q no hay caudal depurado equivalente, ni techo de depuracion, ni vida
media del amonio — que son justamente los conceptos que mejor describen un
biofiltro. Vale mas tenerlos con un valor nominal marcado como estimado que no
tenerlos: `flow_is_nominal` obliga a que la UI lo diga.

Valores del aforo de 2026: 160 L/s en las tres del norte, 220 en las del sur.

Revision ID: 20260923_03
Revises: 20260923_02
"""
from alembic import op
import sqlalchemy as sa

revision = "20260923_03"
down_revision = "20260923_02"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("cultivation_units",
                  sa.Column("recirculation_l_s", sa.Numeric(8, 2), nullable=True))
    op.add_column("cultivation_units",
                  sa.Column("flow_is_nominal", sa.Boolean(), nullable=False,
                            server_default=sa.true()))
    op.execute("""
        update cultivation_units set recirculation_l_s = 160
         where name in ('Nor-Central','Nor-Oriente','Nor-Poniente')
    """)
    op.execute("""
        update cultivation_units set recirculation_l_s = 220
         where name in ('Sur Oriente','Sur Poniente')
    """)


def downgrade():
    op.drop_column("cultivation_units", "flow_is_nominal")
    op.drop_column("cultivation_units", "recirculation_l_s")
