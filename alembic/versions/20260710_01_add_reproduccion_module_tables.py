"""add reproduccion module tables (F1)

Crea las 10 tablas del módulo Reproducción y extiende fish_drug_uses
(reutilización sanitaria, decisión P9). No crea reproductor_drug_logs
ni columnas lot_name/lot_code (P8: se reutiliza `lots`).

Revision ID: 20260710_01
Revises: 20260616_01
Create Date: 2026-07-10 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "20260710_01"
down_revision = "20260616_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- Proceso 1: selección ---
    op.create_table(
        "reproductor_selections",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("fish_id", sa.BigInteger(), sa.ForeignKey("fish.id"), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("sex_at_selection", sa.String(length=20), nullable=True),
        sa.Column("closed_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("updated_at", sa.TIMESTAMP(), nullable=True),
    )
    op.create_index("ix_reproductor_selections_fish_id", "reproductor_selections", ["fish_id"])

    op.create_table(
        "reproductor_monitoring_logs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("selection_id", sa.BigInteger(), sa.ForeignKey("reproductor_selections.id"), nullable=False),
        sa.Column("fish_id", sa.BigInteger(), sa.ForeignKey("fish.id"), nullable=False),
        sa.Column("log_date", sa.Date(), nullable=False),
        sa.Column("sex", sa.String(length=20), nullable=True),
        sa.Column("sperm_density", sa.String(length=10), nullable=True),
        sa.Column("sperm_motility", sa.String(length=10), nullable=True),
        sa.Column("sperm_time", sa.String(length=10), nullable=True),
        sa.Column("ip_value", sa.Integer(), nullable=True),
        sa.Column("observation", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=True),
    )
    op.create_index("ix_reproductor_monitoring_logs_selection_id", "reproductor_monitoring_logs", ["selection_id"])

    # --- Proceso 2: desove ---
    op.create_table(
        "reproductor_spawning_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("selection_id", sa.BigInteger(), sa.ForeignKey("reproductor_selections.id"), nullable=False),
        sa.Column("fish_id", sa.BigInteger(), sa.ForeignKey("fish.id"), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("event_date", sa.Date(), nullable=True),
        sa.Column("event_time", sa.Time(), nullable=True),
        sa.Column("water_temp", sa.Numeric(5, 2), nullable=True),
        sa.Column("anesthesia_drug_name", sa.String(length=200), nullable=True),
        sa.Column("anesthesia_dilution", sa.String(length=100), nullable=True),
        sa.Column("total_ova_weight_g", sa.Numeric(10, 2), nullable=True),
        sa.Column("ova_sample_grams", sa.Numeric(6, 2), nullable=True),
        sa.Column("ova_count_sample", sa.Integer(), nullable=True),
        sa.Column("ova_diameter_mm", sa.Numeric(4, 2), nullable=True),
        sa.Column("viability_pct", sa.Numeric(5, 2), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("updated_at", sa.TIMESTAMP(), nullable=True),
    )
    op.create_index("ix_reproductor_spawning_events_selection_id", "reproductor_spawning_events", ["selection_id"])

    op.create_table(
        "reproductor_spawning_males",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("spawning_event_id", sa.BigInteger(), sa.ForeignKey("reproductor_spawning_events.id"), nullable=False),
        sa.Column("male_selection_id", sa.BigInteger(), sa.ForeignKey("reproductor_selections.id"), nullable=False),
        sa.Column("fish_id", sa.BigInteger(), sa.ForeignKey("fish.id"), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=True),
    )
    op.create_index("ix_reproductor_spawning_males_spawning_event_id", "reproductor_spawning_males", ["spawning_event_id"])

    op.create_table(
        "reproductor_spawning_incubators",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("spawning_event_id", sa.BigInteger(), sa.ForeignKey("reproductor_spawning_events.id"), nullable=False),
        sa.Column("incubator_id", sa.Integer(), nullable=False),
        sa.Column("ova_weight_g", sa.Numeric(10, 2), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=True),
    )
    op.create_index("ix_reproductor_spawning_incubators_spawning_event_id", "reproductor_spawning_incubators", ["spawning_event_id"])

    op.create_table(
        "reproductor_recovery_reports",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("spawning_event_id", sa.BigInteger(), sa.ForeignKey("reproductor_spawning_events.id"), nullable=False),
        sa.Column("fish_id", sa.BigInteger(), sa.ForeignKey("fish.id"), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("recovery_result", sa.String(length=20), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=True),
    )
    op.create_index("ix_reproductor_recovery_reports_spawning_event_id", "reproductor_recovery_reports", ["spawning_event_id"])

    # --- Proceso 3: incubadoras ---
    op.create_table(
        "incubator_batches",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("spawning_event_id", sa.BigInteger(), sa.ForeignKey("reproductor_spawning_events.id"), nullable=False),
        sa.Column("lot_id", sa.BigInteger(), sa.ForeignKey("lots.id"), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("updated_at", sa.TIMESTAMP(), nullable=True),
    )
    op.create_index("ix_incubator_batches_spawning_event_id", "incubator_batches", ["spawning_event_id"])

    op.create_table(
        "incubator_units",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("incubator_batch_id", sa.BigInteger(), sa.ForeignKey("incubator_batches.id"), nullable=False),
        sa.Column("display_id", sa.String(length=30), nullable=False),
        sa.Column("base_incubator_id", sa.Integer(), nullable=False),
        sa.Column("current_weight_g", sa.Numeric(10, 2), nullable=True),
        sa.Column("parent_unit_id", sa.BigInteger(), sa.ForeignKey("incubator_units.id"), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("target_pond_id", sa.BigInteger(), sa.ForeignKey("ponds.id"), nullable=True),
        sa.Column("hatched_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=True),
        sa.Column("updated_at", sa.TIMESTAMP(), nullable=True),
    )
    op.create_index("ix_incubator_units_incubator_batch_id", "incubator_units", ["incubator_batch_id"])

    op.create_table(
        "incubator_monitoring_logs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("incubator_unit_id", sa.BigInteger(), sa.ForeignKey("incubator_units.id"), nullable=False),
        sa.Column("log_datetime", sa.TIMESTAMP(), nullable=False),
        sa.Column("size_mm", sa.Numeric(5, 2), nullable=True),
        sa.Column("viability_pct", sa.Numeric(5, 2), nullable=True),
        sa.Column("observation", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=True),
    )
    op.create_index("ix_incubator_monitoring_logs_incubator_unit_id", "incubator_monitoring_logs", ["incubator_unit_id"])

    op.create_table(
        "incubator_split_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("source_unit_id", sa.BigInteger(), sa.ForeignKey("incubator_units.id"), nullable=False),
        sa.Column("split_datetime", sa.TIMESTAMP(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(), nullable=True),
    )
    op.create_index("ix_incubator_split_events_source_unit_id", "incubator_split_events", ["source_unit_id"])

    # --- P9: reutilizar fish_drug_uses para fármacos de reproducción ---
    op.alter_column("fish_drug_uses", "withdrawal_period",
                    existing_type=sa.String(length=120), nullable=True)
    op.add_column("fish_drug_uses",
                  sa.Column("reproductor_selection_id", sa.BigInteger(),
                            sa.ForeignKey("reproductor_selections.id"), nullable=True))
    op.add_column("fish_drug_uses",
                  sa.Column("water_temp", sa.Numeric(5, 2), nullable=True))
    op.create_index("ix_fish_drug_uses_reproductor_selection_id",
                    "fish_drug_uses", ["reproductor_selection_id"])


def downgrade() -> None:
    op.drop_index("ix_fish_drug_uses_reproductor_selection_id", "fish_drug_uses")
    op.drop_column("fish_drug_uses", "water_temp")
    op.drop_column("fish_drug_uses", "reproductor_selection_id")
    op.alter_column("fish_drug_uses", "withdrawal_period",
                    existing_type=sa.String(length=120), nullable=False)

    op.drop_index("ix_incubator_split_events_source_unit_id", "incubator_split_events")
    op.drop_table("incubator_split_events")
    op.drop_index("ix_incubator_monitoring_logs_incubator_unit_id", "incubator_monitoring_logs")
    op.drop_table("incubator_monitoring_logs")
    op.drop_index("ix_incubator_units_incubator_batch_id", "incubator_units")
    op.drop_table("incubator_units")
    op.drop_index("ix_incubator_batches_spawning_event_id", "incubator_batches")
    op.drop_table("incubator_batches")
    op.drop_index("ix_reproductor_recovery_reports_spawning_event_id", "reproductor_recovery_reports")
    op.drop_table("reproductor_recovery_reports")
    op.drop_index("ix_reproductor_spawning_incubators_spawning_event_id", "reproductor_spawning_incubators")
    op.drop_table("reproductor_spawning_incubators")
    op.drop_index("ix_reproductor_spawning_males_spawning_event_id", "reproductor_spawning_males")
    op.drop_table("reproductor_spawning_males")
    op.drop_index("ix_reproductor_spawning_events_selection_id", "reproductor_spawning_events")
    op.drop_table("reproductor_spawning_events")
    op.drop_index("ix_reproductor_monitoring_logs_selection_id", "reproductor_monitoring_logs")
    op.drop_table("reproductor_monitoring_logs")
    op.drop_index("ix_reproductor_selections_fish_id", "reproductor_selections")
    op.drop_table("reproductor_selections")
