"""Instrucciones de dosificación para la PWA de biofiltros (/dosificacion).

Tabla `dosing_instructions` (lo que el supervisor manda a la tablet, vigente
hasta que se reemplace) y dos columnas en `water_treatment_doses` para las
cargas que llegan desde la tablet: `client_uuid` (idempotencia del lote) e
`instruction_id` (instruido vs aplicado).

Revision ID: 20261019_01
Revises: 20261016_07
"""
from alembic import op
import sqlalchemy as sa


revision = "20261019_01"
down_revision = "20261016_07"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "dosing_instructions",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("cultivation_unit_id", sa.BigInteger,
                  sa.ForeignKey("cultivation_units.id"), nullable=False),
        sa.Column("product", sa.String(30), nullable=False),
        sa.Column("kg_day", sa.Numeric(10, 2), nullable=False),
        sa.Column("suggested_kg_day", sa.Numeric(10, 2)),
        sa.Column("note", sa.String(255)),
        sa.Column("issued_by_id", sa.BigInteger, sa.ForeignKey("users.id")),
        sa.Column("issued_at", sa.TIMESTAMP, nullable=False,
                  server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("ended_at", sa.TIMESTAMP),
        sa.Column("received_at", sa.TIMESTAMP),
    )
    op.create_index("ix_dosing_instructions_cultivation_unit_id",
                    "dosing_instructions", ["cultivation_unit_id"])
    op.create_index("uniq_dosing_instruction_active", "dosing_instructions",
                    ["cultivation_unit_id", "product"], unique=True,
                    postgresql_where=sa.text("ended_at IS NULL"))

    op.add_column("water_treatment_doses",
                  sa.Column("client_uuid", sa.String(64), unique=True))
    op.add_column("water_treatment_doses",
                  sa.Column("instruction_id", sa.BigInteger,
                            sa.ForeignKey("dosing_instructions.id")))


def downgrade() -> None:
    op.drop_column("water_treatment_doses", "instruction_id")
    op.drop_column("water_treatment_doses", "client_uuid")
    op.drop_index("uniq_dosing_instruction_active", table_name="dosing_instructions")
    op.drop_index("ix_dosing_instructions_cultivation_unit_id",
                  table_name="dosing_instructions")
    op.drop_table("dosing_instructions")
