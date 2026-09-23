"""config para indicadores de salud del biofiltro

Revision ID: 20260907_01
Revises: 20260831_01
Create Date: 2026-09-07 12:00:00.000000

Agrega lo mínimo para calcular la salud del biofiltro SIN medir caudal.

    N producido = Q * (C_ent - C_sal)          (balance de masa)
    k           = ln(C_ent/C_sal) * Q / V      (cinética de primer orden)
    -> dividiendo, Q se cancela:
    k = N_producido / (V * C_logmedia)

El N producido sale del alimento ya registrado en feed_execution_events, así que
solo hacen falta datos de ficha (volumen de medio filtrante) y de etiqueta
(proteína y kilos por saco). Deliberadamente NO se guarda un caudal: el único
disponible viene de un aforo puntual, caro y variable, y fijarlo daría precisión
falsa al indicador.

Semillas:
  - media_volume_m3: 163 en las tres del norte, 300 en Sur Oriente y Sur Poniente.
  - is_recirculating: false en Central (es de flujo abierto; su biofiltro no
    procesa carga y no debe evaluarse como si recirculara).
  - feed_types: 48% de proteína y sacos de 20 kg como provisorio uniforme. El 20
    kg está confirmado por los datos (144 sacos / 36 días = 80,0 kg/día exactos en
    Nor-Central, que coincide con lo declarado). La proteína hay que corregirla por
    tipo desde la etiqueta.
"""
from alembic import op
import sqlalchemy as sa


revision = "20260907_01"
down_revision = "20260831_01"
branch_labels = None
depends_on = None


VOLUMENES = {
    "Nor-Central": 163.0,
    "Nor-Oriente": 163.0,
    "Nor-Poniente": 163.0,
    "Sur Oriente": 300.0,
    "Sur Poniente": 300.0,
}


def _has_column(bind, table: str, column: str) -> bool:
    return bool(bind.execute(sa.text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = :t AND column_name = :c"
    ), {"t": table, "c": column}).first())


def upgrade() -> None:
    bind = op.get_bind()

    if not _has_column(bind, "cultivation_units", "media_volume_m3"):
        op.add_column("cultivation_units",
                      sa.Column("media_volume_m3", sa.Numeric(10, 2), nullable=True))
    if not _has_column(bind, "cultivation_units", "is_recirculating"):
        op.add_column("cultivation_units",
                      sa.Column("is_recirculating", sa.Boolean(), nullable=False,
                                server_default=sa.true()))
    if not _has_column(bind, "feed_types", "protein_pct"):
        op.add_column("feed_types",
                      sa.Column("protein_pct", sa.Numeric(5, 2), nullable=True))
    if not _has_column(bind, "feed_types", "bag_kg"):
        op.add_column("feed_types",
                      sa.Column("bag_kg", sa.Numeric(8, 3), nullable=True))

    for nombre, vol in VOLUMENES.items():
        bind.execute(sa.text(
            "UPDATE cultivation_units SET media_volume_m3 = :v "
            "WHERE name = :n AND media_volume_m3 IS NULL"
        ), {"v": vol, "n": nombre})

    # Central es de flujo abierto: recibe poco alimento, su biofiltro no remueve
    # nada medible (k*tau = -0,012, IC cruza cero) y aun asi tiene el amonio mas
    # bajo de la planta. El nitrogeno sale con el agua, no por el filtro.
    bind.execute(sa.text(
        "UPDATE cultivation_units SET is_recirculating = false WHERE name = 'Central'"
    ))

    bind.execute(sa.text(
        "UPDATE feed_types SET protein_pct = 48.0 WHERE protein_pct IS NULL"))
    bind.execute(sa.text(
        "UPDATE feed_types SET bag_kg = 20.0 WHERE bag_kg IS NULL"))


def downgrade() -> None:
    bind = op.get_bind()
    for table, column in (("feed_types", "bag_kg"),
                          ("feed_types", "protein_pct"),
                          ("cultivation_units", "is_recirculating"),
                          ("cultivation_units", "media_volume_m3")):
        if _has_column(bind, table, column):
            op.drop_column(table, column)
