"""caudal de agua fresca por laguna (segundo sumidero de nitrógeno)

Revision ID: 20260907_02
Revises: 20260907_01
Create Date: 2026-09-07 14:00:00.000000

Las lagunas no son circuitos cerrados: entran ~12 L/s de agua fresca, y por lo
tanto salen 12 L/s de purga que se llevan amonio a la concentración de la laguna.

El balance correcto en estado estacionario es:

    N producido = Q*(C_ent - C_sal)  +  q*C_laguna
                  [lo que convierte     [lo que se lleva
                   el biofiltro]         la purga]

Sin este término, el indicador de salud le atribuye al biofiltro nitrógeno que
en realidad se fue por el desagüe, y sobreestima k entre 16% y 26% según lo
sucia que esté el agua de cada laguna (la purga se lleva más donde hay más
concentración, así que castiga menos a las unidades sanas).

A diferencia del caudal de recirculación, este sí es un dato conocido y estable:
es el aporte que se bombea, no un aforo.
"""
from alembic import op
import sqlalchemy as sa


revision = "20260907_02"
down_revision = "20260907_01"
branch_labels = None
depends_on = None

LAGUNAS = ("Nor-Central", "Nor-Oriente", "Nor-Poniente", "Sur Oriente", "Sur Poniente")


def _has_column(bind, table: str, column: str) -> bool:
    return bool(bind.execute(sa.text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = :t AND column_name = :c"
    ), {"t": table, "c": column}).first())


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_column(bind, "cultivation_units", "freshwater_l_s"):
        op.add_column("cultivation_units",
                      sa.Column("freshwater_l_s", sa.Numeric(8, 2), nullable=True))
    bind.execute(sa.text(
        "UPDATE cultivation_units SET freshwater_l_s = 12.0 "
        "WHERE name = ANY(:n) AND freshwater_l_s IS NULL"
    ), {"n": list(LAGUNAS)})


def downgrade() -> None:
    bind = op.get_bind()
    if _has_column(bind, "cultivation_units", "freshwater_l_s"):
        op.drop_column("cultivation_units", "freshwater_l_s")
