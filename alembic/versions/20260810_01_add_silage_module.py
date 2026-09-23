"""módulo de molienda y ensilaje (registro PC 03.2)

Revision ID: 20260810_01
Revises: 20260724_01
Create Date: 2026-08-10 00:00:00.000000

Crea el módulo completo:
- silage_drum_dispatches: guías con que salen los tambores (proceso secundario).
- silage_drums: tambores y su ciclo de vida. Índice único PARCIAL que garantiza
  un solo tambor `open` a la vez.
- silage_grinding_events: las cargas de molienda (unidad de captura, aditiva).
  El folio sale de la secuencia `silage_folio_seq`, asignada por el servidor.
- silage_weekly_inspections: la mitad derecha del formato en papel.
- silage_acid_lots / silage_acid_movements: inventario de ácido.
- silage_thresholds: parámetros configurables, sembrados con defaults.

Idempotente en los seeds: no duplica umbrales si ya existen.
"""
from alembic import op
import sqlalchemy as sa


revision = "20260810_01"
down_revision = "20260724_01"
branch_labels = None
depends_on = None


# (parámetro, valor, unidad, descripción)
THRESHOLD_SEEDS = [
    ("ph_limit", 4.0, "pH",
     "Criterio de aceptación del ensilaje: la carga se registra con pH bajo este valor"),
    ("drum_capacity_kg", 200.0, "kg",
     "Volumen fijo del tambor (igual para todos). PROVISORIO: ajustar al valor real"),
    ("acid_low_stock_lts", 50.0, "lts",
     "Piso de stock de ácido para alertar. PROVISORIO: ajustar al valor real"),
    ("acid_lts_per_kg_min", 0.10, "lts/kg",
     "Piso del rango de dosis esperado. Solo chequeo de verosimilitud, no invalida la carga"),
    ("acid_lts_per_kg_max", 0.25, "lts/kg",
     "Techo del rango de dosis esperado. Solo chequeo de verosimilitud, no invalida la carga"),
]


def upgrade() -> None:
    # --- guías de despacho (se crea primero: silage_drums la referencia) ------
    op.create_table(
        "silage_drum_dispatches",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("dispatch_date", sa.Date(), nullable=False),
        sa.Column("guide_number", sa.String(length=60)),
        sa.Column("carrier", sa.String(length=120)),
        sa.Column("destination", sa.String(length=120)),
        sa.Column("total_drums", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_kg", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id")),
        sa.Column("notes", sa.String(length=255)),
        sa.Column("created_at", sa.TIMESTAMP()),
        sa.Column("updated_at", sa.TIMESTAMP()),
        sa.PrimaryKeyConstraint("id"),
    )

    # --- tambores ------------------------------------------------------------
    op.create_table(
        "silage_drums",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("drum_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="available"),
        sa.Column("opened_at", sa.Date()),
        sa.Column("sealed_at", sa.Date()),
        sa.Column("dispatched_at", sa.Date()),
        sa.Column("dispatch_id", sa.BigInteger(), sa.ForeignKey("silage_drum_dispatches.id")),
        sa.Column("total_kg", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("acid_lts", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("observation", sa.String(length=255)),
        sa.Column("created_at", sa.TIMESTAMP()),
        sa.Column("updated_at", sa.TIMESTAMP()),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_silage_drums_status", "silage_drums", ["status"])

    # Un solo tambor abierto a la vez, garantizado por la BD y no solo por la
    # lógica de aplicación. El índice sobre una expresión constante hace que a lo
    # más una fila pueda satisfacer el predicado.
    op.execute(
        "CREATE UNIQUE INDEX uniq_silage_drum_open "
        "ON silage_drums ((true)) WHERE status = 'open'"
    )

    # --- cargas de molienda --------------------------------------------------
    # Secuencia del folio: la asigna el servidor al sincronizar, nunca la PWA
    # (dos tablets offline producirían correlativos colisionantes).
    op.execute("CREATE SEQUENCE silage_folio_seq START 1")

    op.create_table(
        "silage_grinding_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("folio", sa.BigInteger()),
        sa.Column("event_datetime", sa.TIMESTAMP(), nullable=False),
        sa.Column("log_date", sa.Date(), nullable=False),
        sa.Column("had_mortality", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("mortality_kg", sa.Numeric(10, 2)),
        sa.Column("acid_used", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("acid_lts", sa.Numeric(8, 2)),
        sa.Column("additional_acid", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("ph_value", sa.Numeric(4, 2)),
        sa.Column("ph_below_limit", sa.Boolean()),
        sa.Column("drum_id", sa.BigInteger(), sa.ForeignKey("silage_drums.id")),
        sa.Column("drum_number", sa.Integer()),
        sa.Column("drum_closed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("operator_id", sa.BigInteger(), sa.ForeignKey("users.id")),
        sa.Column("received_by_name", sa.String(length=120)),
        sa.Column("received_at", sa.TIMESTAMP()),
        sa.Column("reception_note", sa.String(length=255)),
        sa.Column("observation", sa.String(length=255)),
        sa.Column("voided_at", sa.TIMESTAMP()),
        sa.Column("voided_by", sa.String(length=120)),
        sa.Column("void_reason", sa.String(length=255)),
        sa.Column("client_uuid", sa.String(length=64)),
        sa.Column("created_at", sa.TIMESTAMP()),
        sa.Column("updated_at", sa.TIMESTAMP()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("folio", name="uq_silage_grinding_events_folio"),
        sa.UniqueConstraint("client_uuid", name="uq_silage_grinding_events_client_uuid"),
    )
    # log_date NO es único: la captura es aditiva (varias cargas por día).
    op.create_index("ix_silage_grinding_events_log_date", "silage_grinding_events", ["log_date"])
    op.create_index("ix_silage_grinding_events_event_dt", "silage_grinding_events", ["event_datetime"])
    op.create_index("ix_silage_grinding_events_drum_id", "silage_grinding_events", ["drum_id"])

    # --- inspección semanal --------------------------------------------------
    op.create_table(
        "silage_weekly_inspections",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("cleanliness_ok", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("drums_sealed_ok", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("drums_sealed_count", sa.Integer()),
        sa.Column("acid_used_lts", sa.Numeric(10, 2)),
        sa.Column("acid_available_lts", sa.Numeric(10, 2)),
        sa.Column("drums_used", sa.Integer()),
        sa.Column("drums_available", sa.Integer()),
        sa.Column("acid_lot", sa.String(length=60)),
        sa.Column("responsible_name", sa.String(length=120)),
        sa.Column("inspector_name", sa.String(length=120)),
        sa.Column("inspected_at", sa.TIMESTAMP()),
        sa.Column("observation", sa.String(length=255)),
        sa.Column("client_uuid", sa.String(length=64)),
        sa.Column("created_at", sa.TIMESTAMP()),
        sa.Column("updated_at", sa.TIMESTAMP()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("week_start", name="uq_silage_weekly_inspections_week"),
        sa.UniqueConstraint("client_uuid", name="uq_silage_weekly_inspections_client_uuid"),
    )

    # --- inventario de ácido -------------------------------------------------
    op.create_table(
        "silage_acid_lots",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("lot_code", sa.String(length=60), nullable=False),
        sa.Column("supplier", sa.String(length=120)),
        sa.Column("received_date", sa.Date()),
        sa.Column("received_lts", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("concentration", sa.String(length=40)),
        sa.Column("notes", sa.String(length=255)),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.TIMESTAMP()),
        sa.Column("updated_at", sa.TIMESTAMP()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("lot_code", name="uq_silage_acid_lots_code"),
    )

    op.create_table(
        "silage_acid_movements",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("lot_id", sa.BigInteger(), sa.ForeignKey("silage_acid_lots.id")),
        sa.Column("movement_date", sa.Date(), nullable=False),
        sa.Column("movement_type", sa.String(length=20), nullable=False),
        sa.Column("lts", sa.Numeric(10, 2), nullable=False),
        sa.Column("grinding_event_id", sa.BigInteger(), sa.ForeignKey("silage_grinding_events.id")),
        sa.Column("drum_id", sa.BigInteger(), sa.ForeignKey("silage_drums.id")),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id")),
        sa.Column("notes", sa.String(length=255)),
        sa.Column("created_at", sa.TIMESTAMP()),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_silage_acid_movements_lot_id", "silage_acid_movements", ["lot_id"])
    op.create_index("ix_silage_acid_movements_event_id", "silage_acid_movements", ["grinding_event_id"])

    # --- umbrales configurables ----------------------------------------------
    op.create_table(
        "silage_thresholds",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("parameter", sa.String(length=50), nullable=False),
        sa.Column("value", sa.Numeric(12, 4)),
        sa.Column("unit", sa.String(length=20)),
        sa.Column("description", sa.String(length=200)),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("updated_at", sa.TIMESTAMP()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("parameter", name="uq_silage_thresholds_parameter"),
    )

    bind = op.get_bind()
    for parameter, value, unit, description in THRESHOLD_SEEDS:
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
    op.drop_table("silage_thresholds")
    op.drop_table("silage_acid_movements")
    op.drop_table("silage_acid_lots")
    op.drop_table("silage_weekly_inspections")
    op.drop_table("silage_grinding_events")
    op.execute("DROP SEQUENCE IF EXISTS silage_folio_seq")
    op.execute("DROP INDEX IF EXISTS uniq_silage_drum_open")
    op.drop_table("silage_drums")
    op.drop_table("silage_drum_dispatches")
