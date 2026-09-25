"""Configuracion del biofiltro y registro de cargas de insumos.

Tres cosas que hoy no se pueden anotar en ninguna parte:

1. COMO ES el biofiltro. `media_volume_m3` solo dice cuanto medio hay. Para
   convertir eta en algo comparable entre lagunas y con la literatura hace
   falta la SUPERFICIE: volumen del reactor x llenado x superficie especifica
   del medio. Con eso la carga superficial (g TAN/m2/d) se puede mirar de
   frente contra los 0,2-0,5 tipicos de un MBBR a estas temperaturas.

2. CUANTA AGUA tiene la unidad. Sin el volumen no hay dosis de choque: subir
   la alcalinidad 43 mg/L en 3.294 m3 son 240 kg de bicarbonato, y ese numero
   no sale de la carga de nitrogeno. Se siembra desde la suma de ponds.volume,
   que es lo mejor que tenemos, y queda editable porque no sabemos si esa suma
   incluye el canal o solo los carriles.

3. QUE SE ECHO. `water_treatment_doses` es el registro de cargas. No lleva
   stock a proposito: mientras esto sea una prueba, el saldo de bodega es
   complejidad que no paga.

El regimen de muestreo vive aqui tambien. La frecuencia de la alcalinidad no
es una constante: en basal va con la rotacion del biofiltro (4-5 dias), en
choque va antes de cada dosis y 2 h despues, y durante una floracion va
diaria. Se guarda el estado y desde cuando, para que la UI pueda vencer la
alcalinidad segun el regimen y para que el regimen VUELVA SOLO a basal: si
depende de que alguien lo apague, queda prendido para siempre.

Revision ID: 20260924_01
Revises: 20260923_03
"""
from alembic import op
import sqlalchemy as sa


revision = "20260924_01"
down_revision = "20260923_03"
branch_labels = None
depends_on = None


def upgrade():
    # --- configuracion del biofiltro y del agua de la unidad ---------------
    op.add_column("cultivation_units",
                  sa.Column("reactor_volume_m3", sa.Numeric(10, 2)))
    op.add_column("cultivation_units",
                  sa.Column("media_fill_pct", sa.Numeric(5, 2)))
    op.add_column("cultivation_units",
                  sa.Column("media_ssa_m2_m3", sa.Numeric(8, 1)))
    op.add_column("cultivation_units",
                  sa.Column("media_type", sa.String(60)))
    op.add_column("cultivation_units",
                  sa.Column("water_volume_m3", sa.Numeric(12, 2)))
    op.add_column("cultivation_units",
                  sa.Column("water_volume_is_estimated", sa.Boolean(),
                            nullable=False, server_default=sa.true()))

    # --- objetivos de la terapia -------------------------------------------
    op.add_column("cultivation_units",
                  sa.Column("alk_target_mg_l", sa.Numeric(6, 1)))
    op.add_column("cultivation_units",
                  sa.Column("chloride_base_mg_l", sa.Numeric(8, 2)))
    op.add_column("cultivation_units",
                  sa.Column("chloride_is_measured", sa.Boolean(),
                            nullable=False, server_default=sa.false()))

    # --- regimen de muestreo ------------------------------------------------
    op.add_column("cultivation_units",
                  sa.Column("sampling_regime", sa.String(20),
                            nullable=False, server_default="basal"))
    op.add_column("cultivation_units",
                  sa.Column("sampling_regime_since", sa.Date()))

    # El volumen de agua arranca desde la suma de los estanques de la unidad.
    op.execute("""
        UPDATE cultivation_units u
           SET water_volume_m3 = s.vol
          FROM (SELECT cultivation_unit_id AS uid, SUM(volume)::numeric AS vol
                  FROM ponds
                 WHERE cultivation_unit_id IS NOT NULL
                 GROUP BY cultivation_unit_id) s
         WHERE s.uid = u.id AND s.vol > 0
    """)

    # --- cargas de insumos --------------------------------------------------
    op.create_table(
        "water_treatment_doses",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("cultivation_unit_id", sa.BigInteger(),
                  sa.ForeignKey("cultivation_units.id"), nullable=False,
                  index=True),
        sa.Column("operator_id", sa.BigInteger(), sa.ForeignKey("users.id")),
        sa.Column("applied_at", sa.TIMESTAMP(), nullable=False),
        # bicarbonato | sal
        sa.Column("product", sa.String(30), nullable=False),
        sa.Column("kg", sa.Numeric(10, 2), nullable=False),
        # choque | mantencion
        sa.Column("mode", sa.String(20), nullable=False, server_default="mantencion"),
        sa.Column("application_point", sa.String(120)),
        # Alcalinidad antes y 2 h despues: el par dosis-respuesta es lo que
        # calibra el consumo real. Solo se pide durante el choque.
        sa.Column("alk_before", sa.Numeric(8, 2)),
        sa.Column("alk_after_2h", sa.Numeric(8, 2)),
        sa.Column("ph_before", sa.Numeric(4, 2)),
        sa.Column("ph_after_2h", sa.Numeric(4, 2)),
        sa.Column("observation", sa.String(255)),
        sa.Column("created_at", sa.TIMESTAMP(),
                  server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index("ix_wtd_unit_applied", "water_treatment_doses",
                    ["cultivation_unit_id", "applied_at"])


def downgrade():
    op.drop_index("ix_wtd_unit_applied", table_name="water_treatment_doses")
    op.drop_table("water_treatment_doses")
    for c in ("sampling_regime_since", "sampling_regime", "chloride_is_measured",
              "chloride_base_mg_l", "alk_target_mg_l", "water_volume_is_estimated",
              "water_volume_m3", "media_type", "media_ssa_m2_m3",
              "media_fill_pct", "reactor_volume_m3"):
        op.drop_column("cultivation_units", c)
