"""mantenimiento: un equipo atiende varios destinos

Revision ID: 20260929_07
Revises: 20260929_06
Create Date: 2026-09-29 00:00:00.000000

El vínculo equipo → estanque era uno solo (`ref_ubicacion_tipo/_id`), y no
alcanza: los sopladores de Crianza están centralizados y atienden todos los
estanques. Pasa a una tabla de destinos, con tres niveles para no tener que
marcar decenas de estanques:

- `sitio:crianza`  el sitio completo
- `unit:<id>`      una unidad de cultivo completa (todos sus estanques)
- `pond:<id>`      un estanque suelto

Sigue siendo referencia blanda (sin FK a ponds/cultivation_units, spec §3).
Los vínculos que existieran se copian a la tabla nueva antes de borrar las
columnas.
"""
from alembic import op
import sqlalchemy as sa


revision = "20260929_07"
down_revision = "20260929_06"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mnt_equipo_destinos",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("equipo_id", sa.BigInteger(), sa.ForeignKey("mnt_equipos.id", ondelete="CASCADE"), nullable=False),
        sa.Column("ref_tipo", sa.String(10), nullable=False),
        sa.Column("ref_id", sa.String(40), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("equipo_id", "ref_tipo", "ref_id", name="uq_mnt_equipo_destino"),
        sa.CheckConstraint("ref_tipo IN ('sitio', 'unit', 'pond')", name="ck_mnt_equipo_destino_tipo"),
    )
    op.create_index("ix_mnt_equipo_destinos_equipo", "mnt_equipo_destinos", ["equipo_id"])
    op.create_index("ix_mnt_equipo_destinos_ref", "mnt_equipo_destinos", ["ref_tipo", "ref_id"])
    op.execute(
        "INSERT INTO mnt_equipo_destinos (equipo_id, ref_tipo, ref_id) "
        "SELECT id, ref_ubicacion_tipo, ref_ubicacion_id FROM mnt_equipos "
        "WHERE ref_ubicacion_tipo IN ('unit', 'pond') AND ref_ubicacion_id IS NOT NULL"
    )
    op.drop_column("mnt_equipos", "ref_ubicacion_tipo")
    op.drop_column("mnt_equipos", "ref_ubicacion_id")


def downgrade() -> None:
    op.add_column("mnt_equipos", sa.Column("ref_ubicacion_tipo", sa.String(30)))
    op.add_column("mnt_equipos", sa.Column("ref_ubicacion_id", sa.String(40)))
    # Vuelve a un solo vínculo: se conserva el primero de cada equipo (el
    # sitio completo no tenía representación en el esquema viejo).
    op.execute(
        "UPDATE mnt_equipos e SET ref_ubicacion_tipo = d.ref_tipo, ref_ubicacion_id = d.ref_id "
        "FROM (SELECT DISTINCT ON (equipo_id) equipo_id, ref_tipo, ref_id FROM mnt_equipo_destinos "
        "      WHERE ref_tipo IN ('unit', 'pond') ORDER BY equipo_id, id) d "
        "WHERE d.equipo_id = e.id"
    )
    op.drop_table("mnt_equipo_destinos")
