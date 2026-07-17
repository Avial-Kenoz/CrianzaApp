"""add water quality module tables (PR1)

Crea las 3 tablas del módulo de Calidad de Agua:
  - pond_oxygen_readings: lecturas de O2 por estanque (cada 2-4 h)
  - biofilter_readings: muestreo diario del biofiltro por unidad de cultivo
    (entrada + salida en la misma fila, N expresado como N)
  - water_quality_thresholds: umbrales/tolerancias configurables (sembrados)

No carga datos legacy (acuicola.sql): el ETL histórico es una etapa posterior.

Revision ID: 20260717_01
Revises: 20260710_01
Create Date: 2026-07-17 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "20260717_01"
down_revision = "20260710_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pond_oxygen_readings",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("pond_id", sa.BigInteger(), sa.ForeignKey("ponds.id"), nullable=False),
        sa.Column("operator_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("reading_datetime", sa.TIMESTAMP(), nullable=False),
        sa.Column("do_mg_l", sa.Numeric(6, 2), nullable=True),
        sa.Column("water_temp_c", sa.Numeric(5, 2), nullable=True),
        sa.Column("saturation_pct", sa.Numeric(6, 2), nullable=True),
        sa.Column("saturation_computed_pct", sa.Numeric(6, 2), nullable=True),
        sa.Column("consistency_flag", sa.String(length=20), nullable=True),
        sa.Column("alarm_level", sa.String(length=20), nullable=True),
        sa.Column("observation", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=True),
    )
    op.create_index("ix_pond_oxygen_readings_pond_id", "pond_oxygen_readings", ["pond_id"])
    op.create_index("ix_pond_oxygen_readings_reading_datetime", "pond_oxygen_readings", ["reading_datetime"])

    op.create_table(
        "biofilter_readings",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("cultivation_unit_id", sa.BigInteger(), sa.ForeignKey("cultivation_units.id"), nullable=False),
        sa.Column("operator_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("reading_date", sa.Date(), nullable=False),
        sa.Column("in_ph", sa.Numeric(4, 2), nullable=True),
        sa.Column("in_temp_c", sa.Numeric(5, 2), nullable=True),
        sa.Column("in_nh4_n", sa.Numeric(8, 3), nullable=True),
        sa.Column("in_no2_n", sa.Numeric(8, 3), nullable=True),
        sa.Column("in_no3_n", sa.Numeric(8, 3), nullable=True),
        sa.Column("out_ph", sa.Numeric(4, 2), nullable=True),
        sa.Column("out_temp_c", sa.Numeric(5, 2), nullable=True),
        sa.Column("out_nh4_n", sa.Numeric(8, 3), nullable=True),
        sa.Column("out_no2_n", sa.Numeric(8, 3), nullable=True),
        sa.Column("out_no3_n", sa.Numeric(8, 3), nullable=True),
        sa.Column("tn_in", sa.Numeric(9, 3), nullable=True),
        sa.Column("tn_out", sa.Numeric(9, 3), nullable=True),
        sa.Column("nh3_n_in", sa.Numeric(8, 4), nullable=True),
        sa.Column("nh3_n_out", sa.Numeric(8, 4), nullable=True),
        sa.Column("n_balance_flag", sa.String(length=20), nullable=True),
        sa.Column("ph_delta_flag", sa.String(length=20), nullable=True),
        sa.Column("temp_delta_flag", sa.String(length=20), nullable=True),
        sa.Column("alarm_level", sa.String(length=20), nullable=True),
        sa.Column("observation", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=True),
    )
    op.create_index("ix_biofilter_readings_cultivation_unit_id", "biofilter_readings", ["cultivation_unit_id"])
    op.create_index("ix_biofilter_readings_reading_date", "biofilter_readings", ["reading_date"])

    thresholds = op.create_table(
        "water_quality_thresholds",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("parameter", sa.String(length=50), nullable=False, unique=True),
        sa.Column("alert_value", sa.Numeric(10, 4), nullable=True),
        sa.Column("alarm_value", sa.Numeric(10, 4), nullable=True),
        sa.Column("comparator", sa.String(length=10), nullable=False),
        sa.Column("unit", sa.String(length=20), nullable=True),
        sa.Column("description", sa.String(length=200), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("updated_at", sa.TIMESTAMP(), nullable=True),
    )

    # Defaults (editables desde config/BD). comparator: 'lt' dispara si el valor
    # es menor que el umbral; 'gt' si es mayor.
    op.bulk_insert(
        thresholds,
        [
            {"parameter": "o2_saturation", "alert_value": 70, "alarm_value": 60,
             "comparator": "lt", "unit": "%", "active": True,
             "description": "Saturación de oxígeno en estanque"},
            {"parameter": "nh3_n", "alert_value": 0.0125, "alarm_value": 0.025,
             "comparator": "gt", "unit": "mg/L", "active": True,
             "description": "Amonio no ionizado (NH3-N) en biofiltro"},
            {"parameter": "nitrite_n", "alert_value": 0.10, "alarm_value": 0.50,
             "comparator": "gt", "unit": "mg/L", "active": True,
             "description": "Nitrito (NO2-N) en biofiltro"},
            {"parameter": "n_balance_tol", "alert_value": None, "alarm_value": 12,
             "comparator": "gt", "unit": "%", "active": True,
             "description": "Tolerancia balance de N entrada vs salida"},
            {"parameter": "ph_delta_tol", "alert_value": 0.3, "alarm_value": 0.5,
             "comparator": "gt", "unit": "pH", "active": True,
             "description": "Tolerancia ΔpH entrada→salida biofiltro"},
            {"parameter": "temp_delta_tol", "alert_value": 1.0, "alarm_value": None,
             "comparator": "gt", "unit": "°C", "active": True,
             "description": "Tolerancia Δtemperatura entrada→salida biofiltro"},
            {"parameter": "o2_consistency_tol", "alert_value": None, "alarm_value": 10,
             "comparator": "gt", "unit": "%", "active": True,
             "description": "Tolerancia consistencia terna O2/temp/saturación"},
        ],
    )


def downgrade() -> None:
    op.drop_table("water_quality_thresholds")
    op.drop_index("ix_biofilter_readings_reading_date", "biofilter_readings")
    op.drop_index("ix_biofilter_readings_cultivation_unit_id", "biofilter_readings")
    op.drop_table("biofilter_readings")
    op.drop_index("ix_pond_oxygen_readings_reading_datetime", "pond_oxygen_readings")
    op.drop_index("ix_pond_oxygen_readings_pond_id", "pond_oxygen_readings")
    op.drop_table("pond_oxygen_readings")
