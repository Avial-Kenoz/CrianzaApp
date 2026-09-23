"""cache persistido de la biomasa proyectada por estanque

Revision ID: 20260923_01
Revises: 20260907_02
Create Date: 2026-09-23 12:00:00.000000

Desde la unificación del criterio de peso (ago-2026) la biomasa que muestra esta
app sale de `_project_biomass_from_checkpoint` (proyección por (estanque, lote)
desde `biomass_checkpoints`), no de `ponds.biomass_current` —que quedó como caché
del criterio anterior, mantenido solo de forma incremental—.

AdminDashboard seguía sumando `biomass_current` y por eso mostraba 192.854 kg
donde esta app muestra 176.339 kg (+9,4%, y hasta ±4,4 t por estanque). Estas
columnas materializan la proyección para que el dashboard lea el mismo número sin
duplicar el algoritmo: las refresca `rebuild_all_pond_runtime_cache` (job Lun-Vie
8-18 cada 2h) y el cierre de muestreo.

`biomass_projected_lots_missing` cuenta los lotes con peces en el estanque que no
tienen checkpoint: la proyección los devuelve en 0 kg (no los omite), así que sin ese
contador un estanque sin muestreo se reporta igual que uno vacío.
"""
from alembic import op
import sqlalchemy as sa


revision = "20260923_01"
down_revision = "20260907_02"
branch_labels = None
depends_on = None


def _has_column(bind, table: str, column: str) -> bool:
    return bool(bind.execute(sa.text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = :t AND column_name = :c"
    ), {"t": table, "c": column}).first())


def upgrade() -> None:
    bind = op.get_bind()

    if not _has_column(bind, "ponds", "biomass_projected_kg"):
        op.add_column("ponds", sa.Column("biomass_projected_kg", sa.Numeric(14, 3), nullable=True))
    if not _has_column(bind, "ponds", "biomass_projected_at"):
        op.add_column("ponds", sa.Column("biomass_projected_at", sa.TIMESTAMP(), nullable=True))
    if not _has_column(bind, "ponds", "biomass_projected_lots_missing"):
        op.add_column(
            "ponds",
            sa.Column(
                "biomass_projected_lots_missing",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
        )

    # Sin backfill acá: la proyección vive en app/api/views.py y necesita la app
    # importada. Tras `alembic upgrade head` corre
    # `scripts/rebuild_pond_runtime_cache.py`, que la deja poblada.


def downgrade() -> None:
    bind = op.get_bind()
    for column in (
        "biomass_projected_lots_missing",
        "biomass_projected_at",
        "biomass_projected_kg",
    ):
        if _has_column(bind, "ponds", column):
            op.drop_column("ponds", column)
