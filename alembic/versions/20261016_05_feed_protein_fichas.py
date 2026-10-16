"""Proteína de cada alimento según las fichas Aller.

feed_types.protein_pct estaba en 48 para todos. Las fichas dicen:
  Infa EX GR 0.2/0.4 ............ 64 %   (510-es-9)
  Thalassa EX GR 0.5-1.0/0.9-1.6  62 %   (1030-es-9)
  Thalassa EX GR 1.3-2.0 ........ 60 %
  Progress EX ................... 48 %   (1990-es-9)
  Sturgeon Rep EX ............... 52 %   (1310-es-9; el 6.0 no tiene ficha, se usa la del 8)

Calidad de agua convierte kilos de alimento en nitrógeno con este valor.

Revision ID: 20261016_05
Revises: 20261016_04
"""
from alembic import op


revision = "20261016_05"
down_revision = "20261016_04"
branch_labels = None
depends_on = None

PROTEIN = [
    ("Infa%", 64),
    ("Thalassa 0.5%", 62),
    ("Thalassa 0.9%", 62),
    ("Thalassa 1.3%", 60),
    ("Progress%", 48),
    ("Reproductor%", 52),
]


def upgrade() -> None:
    for pattern, pct in PROTEIN:
        op.execute(f"UPDATE feed_types SET protein_pct = {pct}, updated_at = now() "
                   f"WHERE name ILIKE '{pattern}'")


def downgrade() -> None:
    op.execute("UPDATE feed_types SET protein_pct = 48, updated_at = now()")
