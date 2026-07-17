"""add client_uuid to pond_oxygen_readings (F2: sync idempotente)

Columna generada por el cliente PWA de captura en terreno para que la carga
en lote sea idempotente (reintentar la sincronización no duplica lecturas).
Nullable: las lecturas creadas desde el formulario web no la usan.

Revision ID: 20260717_02
Revises: 20260717_01
Create Date: 2026-07-17 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "20260717_02"
down_revision = "20260717_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("pond_oxygen_readings",
                  sa.Column("client_uuid", sa.String(length=64), nullable=True))
    op.create_unique_constraint(
        "uq_pond_oxygen_readings_client_uuid", "pond_oxygen_readings", ["client_uuid"])


def downgrade() -> None:
    op.drop_constraint(
        "uq_pond_oxygen_readings_client_uuid", "pond_oxygen_readings", type_="unique")
    op.drop_column("pond_oxygen_readings", "client_uuid")
