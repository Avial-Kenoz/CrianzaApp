"""Radiacion solar diaria de Parral: archivo y pronostico.

Insumo para el plan de accion de fotosintesis. La correlacion se verifico
antes de construir esto, sobre 320 dias-unidad ya medidos:

    r(radiacion, amplitud dia-noche de saturacion) = +0,38 a +0,57 en las
    lagunas, y +0,01 en Jaula -- el sistema profundo donde NO deberia haber
    senal de laguna. Ese cero es el control: descarta que sea un artefacto de
    estacion o de sonda.

    radiacion baja   1,4- 8,8 MJ/m2  ->  amplitud media  +4,7 pts
    radiacion media  8,8-14,4        ->  amplitud media  +7,9 pts
    radiacion alta  14,5-22,0        ->  amplitud media +11,0 pts

Una hipotesis que SI se probo y resulto falsa: un dia soleado no deja un
amanecer peor al dia siguiente (r = +0,11, y el signo va al reves). Por eso
el consejo derivado del pronostico habla del pH de la tarde y no del
amanecer; el margen del amanecer sigue saliendo de la medicion.

Una fila por dia. El pronostico se pisa con el archivo cuando el dia pasa:
`is_forecast` dice cual de los dos esta guardado, porque no son lo mismo --
el archivo es reanalisis de algo que ya ocurrio, el pronostico es modelo.

Revision ID: 20260924_02
Revises: 20260924_01
"""
from alembic import op
import sqlalchemy as sa


revision = "20260924_02"
down_revision = "20260924_01"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "solar_daily",
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("ghi_mj_m2", sa.Numeric(6, 2)),        # radiacion de onda corta
        sa.Column("sunshine_hours", sa.Numeric(5, 2)),
        sa.Column("cloud_cover_pct", sa.Numeric(5, 1)),
        sa.Column("temp_max_c", sa.Numeric(5, 2)),
        sa.Column("temp_min_c", sa.Numeric(5, 2)),
        sa.Column("is_forecast", sa.Boolean(), nullable=False,
                  server_default=sa.true()),
        sa.Column("source", sa.String(40), nullable=False,
                  server_default="open-meteo"),
        sa.Column("fetched_at", sa.TIMESTAMP(),
                  server_default=sa.text("CURRENT_TIMESTAMP")),
    )


def downgrade():
    op.drop_table("solar_daily")
