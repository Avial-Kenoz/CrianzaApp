"""add qr_code to ponds (F3: identificador estable para QR de terreno)

Código estable por estanque, independiente del id de la BD, que se imprime en
un QR y que la PWA de terreno usa para identificar el estanque al escanear.
Nullable + único: se rellena perezosamente (ensure_pond_qr_codes) para
estanques padre activos.

Revision ID: 20260717_03
Revises: 20260717_02
Create Date: 2026-07-17 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = "20260717_03"
down_revision = "20260717_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ponds", sa.Column("qr_code", sa.String(length=16), nullable=True))
    op.create_unique_constraint("uq_ponds_qr_code", "ponds", ["qr_code"])


def downgrade() -> None:
    op.drop_constraint("uq_ponds_qr_code", "ponds", type_="unique")
    op.drop_column("ponds", "qr_code")
