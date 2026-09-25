"""Marca si el reactor del biofiltro esta aireado.

El 25-09-2026 llegaron los primeros OD de entrada y salida del reactor y
faltaba entre el 96% y el 105% del oxigeno que la nitrificacion deberia
consumir, en las cuatro unidades. Nor-Central incluso GANABA oxigeno a traves
del reactor.

La explicacion es que el biofiltro esta aireado: la misma linea de aire repone
el O2 tan rapido como la biopelicula lo consume. En un MBBR aireado el ΔOD no
mide respiracion -- no mide nada.

El pH lo corrobora desde otro parametro: entrada y salida salen identicos a dos
decimales en las seis lecturas, y se miden por separado. Nitrificar deberia
bajar el pH, pero el aire tambien arrastra CO2, que lo sube. Se cancelan.

Esto invalida el diagnostico de carga heterotrofa y la senal "ΔOD/ΔN > 4,57"
para estos reactores. El flag permite apagarlos donde no aplican sin perderlos
para un reactor que algun dia no este aireado.

Revision ID: 20260925_03
Revises: 20260925_02
"""
from alembic import op
import sqlalchemy as sa

revision = "20260925_03"
down_revision = "20260925_02"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("cultivation_units",
                  sa.Column("reactor_is_aerated", sa.Boolean(),
                            nullable=False, server_default=sa.true()))


def downgrade():
    op.drop_column("cultivation_units", "reactor_is_aerated")
