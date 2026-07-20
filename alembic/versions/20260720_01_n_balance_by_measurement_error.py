"""balance de N por incertidumbre de medición (specs de test + factor k)

Reemplaza el chequeo de balance de N por porcentaje fijo (n_balance_tol=12%)
por un modelo basado en el error de los reactivos: cada lectura tiene
accuracy = acc_fixed + acc_pct·valor; la incertidumbre combinada del balance
es la suma en cuadratura de los 6 errores, y se marca desbalance si
|N_ent - N_sal| > k·U.

- Nueva tabla water_quality_test_specs (nh4_n, no2_n, no3_n), sembrada.
- Umbral n_balance_tol (12%) -> n_balance_k (factor k = 1.0).

Revision ID: 20260720_01
Revises: 20260717_03
Create Date: 2026-07-20 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "20260720_01"
down_revision = "20260717_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    specs = op.create_table(
        "water_quality_test_specs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("test", sa.String(length=20), nullable=False, unique=True),
        sa.Column("label", sa.String(length=60), nullable=True),
        sa.Column("resolution", sa.Numeric(10, 5), nullable=True),
        sa.Column("acc_fixed", sa.Numeric(10, 5), nullable=True),
        sa.Column("acc_pct", sa.Numeric(6, 4), nullable=True),
        sa.Column("updated_at", sa.TIMESTAMP(), nullable=True),
    )
    # Valores expresados en mg/L como N (nitrito: 20 ug/L = 0.020; 1 ug/L = 0.001)
    op.bulk_insert(specs, [
        {"test": "nh4_n", "label": "Amonio (NH4-N)",  "resolution": 0.01,  "acc_fixed": 0.04,  "acc_pct": 0.04},
        {"test": "no2_n", "label": "Nitrito (NO2-N)",  "resolution": 0.001, "acc_fixed": 0.020, "acc_pct": 0.04},
        {"test": "no3_n", "label": "Nitrato (NO3-N)",  "resolution": 0.1,   "acc_fixed": 0.5,   "acc_pct": 0.10},
    ])

    # Sustituir n_balance_tol (12%) por n_balance_k (factor k = 1.0)
    op.execute("DELETE FROM water_quality_thresholds WHERE parameter = 'n_balance_tol'")
    op.execute(
        "INSERT INTO water_quality_thresholds "
        "(parameter, alert_value, alarm_value, comparator, unit, description, active) "
        "VALUES ('n_balance_k', NULL, 1.0, 'gt', '×U', "
        "'Balance de N: factor de cobertura k (desbalance si |dif| > k·U)', true)"
    )


def downgrade() -> None:
    op.execute("DELETE FROM water_quality_thresholds WHERE parameter = 'n_balance_k'")
    op.execute(
        "INSERT INTO water_quality_thresholds "
        "(parameter, alert_value, alarm_value, comparator, unit, description, active) "
        "VALUES ('n_balance_tol', NULL, 12.0, 'gt', '%', "
        "'Tolerancia balance de N entrada vs salida', true)"
    )
    op.drop_table("water_quality_test_specs")
