"""canonicalizar PIT tags de fish (quitar espacios y ceros a la izquierda)

Revision ID: 20260721_01
Revises: 20260720_03
Create Date: 2026-07-21 00:00:00.000000

El lector de PIT antepone ceros (0007CF009C ≡ 7CF009C, mismo valor hex) y hay
tags con espacios de borde por data-entry. El código ya canonicaliza al escribir
(_normalize_pit_tag / Fish._normalize_pit_value) y compara bilateralmente, pero
esta migración deja los ~183 internal_id legados en forma canónica para
consistencia. Idempotente y con guard anti-merge: aborta si la canonicalización
fusionaría dos tags DISTINTOS en el mismo valor (verificado: 0 casos). No toca
los duplicados exactos preexistentes (mismo tag en 2 peces) — eso es otro asunto.
"""
from alembic import op
import sqlalchemy as sa


revision = "20260721_01"
down_revision = "20260720_03"
branch_labels = None
depends_on = None

# canónico = quitar espacios de borde, mayúsculas, quitar ceros a la izquierda.
_CANON = "upper(ltrim(btrim({col}), '0'))"


def _guard_no_merges(bind, col: str) -> None:
    """Aborta si canonicalizar {col} fusiona ≥2 valores distintos en uno."""
    canon = _CANON.format(col=col)
    rows = bind.execute(sa.text(
        f"SELECT {canon} AS c, count(distinct {col}) n "
        f"FROM fish WHERE {col} IS NOT NULL "
        f"GROUP BY 1 HAVING count(distinct {col}) > 1 LIMIT 5"
    )).fetchall()
    if rows:
        raise RuntimeError(
            f"Abortada: canonicalizar fish.{col} fusionaría tags distintos: "
            f"{[(r[0], r[1]) for r in rows]}. Resolver esos casos antes de migrar."
        )


def _canonicalize(bind, col: str) -> None:
    canon = _CANON.format(col=col)
    # solo filas que cambian y cuyo canónico no queda vacío (defensivo).
    bind.execute(sa.text(
        f"UPDATE fish SET {col} = {canon}, updated_at = now() "
        f"WHERE {col} IS NOT NULL AND {canon} <> {col} AND {canon} <> ''"
    ))


def upgrade() -> None:
    bind = op.get_bind()
    for col in ("internal_id", "secondary_internal_id"):
        _guard_no_merges(bind, col)
    for col in ("internal_id", "secondary_internal_id"):
        _canonicalize(bind, col)


def downgrade() -> None:
    # Irreversible: no se puede reconstruir los ceros/espacios originales.
    # La canonicalización es una limpieza de datos sin vuelta atrás.
    pass
