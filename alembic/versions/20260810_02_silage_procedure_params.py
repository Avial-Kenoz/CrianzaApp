"""ajusta los parámetros de ensilaje al procedimiento escrito (PC 03.2)

Revision ID: 20260810_02
Revises: 20260810_01
Create Date: 2026-08-10 00:00:00.000000

El texto "Información Importante" del formato en papel fija tres cosas que los
seeds de 20260810_01 tenían mal o de más:

1. **Dosis: 1 litro de ácido fórmico por 4 kg de mortalidad molida = 0,25 lts/kg.**
   El rango sembrado era 0,10–0,25, o sea el valor NOMINAL caía justo en el techo:
   cualquier carga hecha exactamente según el procedimiento quedaba al borde y una
   décima de más disparaba el aviso. Se recentra el rango en 0,20–0,30 y se
   agrega `acid_lts_per_kg_nominal` = 0,25 para poder mostrar la referencia.
2. **El tambor se cierra a 4/5 (80%)**, no al 90% que asumía el motor. Pasa a ser
   umbral configurable `drum_close_fill_ratio` en vez de constante en el código.
3. El pH exigido (`< 4.0`) queda confirmado: no cambia.

Idempotente: usa UPDATE sobre los parámetros existentes e INSERT condicional para
los nuevos, así que corre igual en instalaciones que ya tenían los seeds viejos.
"""
from alembic import op
import sqlalchemy as sa


revision = "20260810_02"
down_revision = "20260810_01"
branch_labels = None
depends_on = None


UPDATES = [
    ("acid_lts_per_kg_min", 0.20,
     "Piso del rango de dosis esperado (nominal 0,25 = 1 lt por 4 kg). Solo chequeo de verosimilitud"),
    ("acid_lts_per_kg_max", 0.30,
     "Techo del rango de dosis esperado (nominal 0,25 = 1 lt por 4 kg). Solo chequeo de verosimilitud"),
]

INSERTS = [
    ("acid_lts_per_kg_nominal", 0.25, "lts/kg",
     "Dosis del procedimiento: 1 litro de ácido fórmico por 4 kg de mortalidad molida"),
    ("drum_close_fill_ratio", 0.80, "fracción",
     "El procedimiento manda cerrar y sellar el tambor a 4/5 de su capacidad"),
]


def upgrade() -> None:
    bind = op.get_bind()
    for parameter, value, description in UPDATES:
        bind.execute(
            sa.text(
                "UPDATE silage_thresholds SET value = :v, description = :d, updated_at = now() "
                "WHERE parameter = :p"
            ),
            {"p": parameter, "v": value, "d": description},
        )
    for parameter, value, unit, description in INSERTS:
        bind.execute(
            sa.text(
                "INSERT INTO silage_thresholds "
                "(parameter, value, unit, description, active, updated_at) "
                "SELECT :p, :v, :u, :d, true, now() "
                "WHERE NOT EXISTS (SELECT 1 FROM silage_thresholds WHERE parameter = :p)"
            ),
            {"p": parameter, "v": value, "u": unit, "d": description},
        )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text(
        "DELETE FROM silage_thresholds "
        "WHERE parameter IN ('acid_lts_per_kg_nominal', 'drum_close_fill_ratio')"
    ))
    bind.execute(sa.text(
        "UPDATE silage_thresholds SET value = 0.10 WHERE parameter = 'acid_lts_per_kg_min'"
    ))
    bind.execute(sa.text(
        "UPDATE silage_thresholds SET value = 0.25 WHERE parameter = 'acid_lts_per_kg_max'"
    ))
