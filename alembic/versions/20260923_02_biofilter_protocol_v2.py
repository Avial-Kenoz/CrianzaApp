"""Protocolo de muestreo de biofiltro v2: alcalinidad, OD y duplicados.

Tres agregados, todos opcionales para no romper el ingreso existente:

- `in_alkalinity`: solo entrada. La caida a traves del reactor (~1,5 mg/L)
  esta bajo la resolucion de la titulacion (~5), asi que medirla en ambos
  puntos seria medir ruido. En un punto si sirve, como serie temporal para
  el balance del sistema.

- `in_do_mg_l` / `out_do_mg_l`: la diferencia de mayor relacion señal/ruido
  del protocolo (~1 mg/L de caida contra 0,1 de resolucion). El exceso sobre
  la caida estequiometrica de la nitrificacion (4,57 g O2 por g N) es
  respiracion heterotrofa, que es el diagnostico que falta para Sur Oriente.
  Se mide con UNA sonda movida entre los dos puntos: con dos sondas fijas el
  offset entre electrodos destruye una diferencia de este tamaño.

- duplicados de NH4 y NO3: el scatter dia a dia de ktau se explica 100% por
  el ruido del kit, asi que duplicar la lectura rinde mas que acortar el
  intervalo de muestreo. Si vienen, el motor promedia; si no, usa el simple.

`no2_flag` guarda el resultado del detector de outlier de nitrito.

Revision ID: 20260923_02
Revises: 20260923_01
"""
from alembic import op
import sqlalchemy as sa

revision = "20260923_02"
down_revision = "20260923_01"
branch_labels = None
depends_on = None

COLS = [
    ("in_alkalinity", sa.Numeric(8, 2)),
    ("in_do_mg_l", sa.Numeric(6, 2)),
    ("out_do_mg_l", sa.Numeric(6, 2)),
    ("in_nh4_n_2", sa.Numeric(8, 3)),
    ("out_nh4_n_2", sa.Numeric(8, 3)),
    ("in_no3_n_2", sa.Numeric(8, 3)),
    ("out_no3_n_2", sa.Numeric(8, 3)),
    ("no2_flag", sa.String(20)),
]


def upgrade():
    for name, type_ in COLS:
        op.add_column("biofilter_readings", sa.Column(name, type_, nullable=True))


def downgrade():
    for name, _ in reversed(COLS):
        op.drop_column("biofilter_readings", name)
