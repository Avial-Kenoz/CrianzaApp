"""posicion denormalizada del pez: fish.current_pond_id mantenida por trigger

Revision ID: 20260831_01
Revises: 20260819_01
Create Date: 2026-08-31 00:00:00.000000

Saber en que estanque esta un pez requeria agregar `ponds_movements` ENTERA:
dos seq scans de 173k filas y dos HashAggregate para calcular el ultimo
movimiento de los 49k peces, y recien despues filtrar por estanque. 110 ms de
motor por llamada, sin importar si el estanque tiene 3 peces o 3.000, y hay 7
sitios que lo invocan (detalle de estanque, formulario de movimiento, sexado,
conflictos de jaula...).

La posicion se denormaliza en `fish.current_pond_id`. La mantiene un TRIGGER y
no el codigo Python a proposito: hay 14 puntos de entrada y 30 sitios que crean
PondMovement (formularios, sync de sexado offline, faena, reconciliacion,
scripts de mantenimiento). Olvidar uno solo desincroniza la columna en silencio
y para siempre, y el sintoma aparece semanas despues como "el pez desaparecio
del estanque".

El trigger RECALCULA el maximo por (movement_time, id) en vez de tomar
NEW.destiny_pond_id: varios caminos aceptan fecha retroactiva sin guarda (solo
el movimiento masivo la tiene), asi que el movimiento recien insertado no es
necesariamente el ultimo del pez. Hoy hay 9 peces de 49.003 en esa situacion.
"""
from alembic import op
import sqlalchemy as sa


revision = "20260831_01"
down_revision = "20260819_01"
branch_labels = None
depends_on = None


_RECOMPUTE_FN = """
CREATE OR REPLACE FUNCTION recompute_fish_current_pond(p_fish_id bigint)
RETURNS void AS $$
    UPDATE fish f
       SET current_pond_id = (
               SELECT pm.destiny_pond_id
                 FROM ponds_movements pm
                WHERE pm.fish_id = p_fish_id
                ORDER BY pm.movement_time DESC, pm.id DESC
                LIMIT 1
           )
     WHERE f.id = p_fish_id
       AND f.current_pond_id IS DISTINCT FROM (
               SELECT pm.destiny_pond_id
                 FROM ponds_movements pm
                WHERE pm.fish_id = p_fish_id
                ORDER BY pm.movement_time DESC, pm.id DESC
                LIMIT 1
           );
$$ LANGUAGE sql;
"""

# Se recalcula el pez de OLD y el de NEW: en UPDATE puede cambiar el fish_id, y
# tambien puede cambiar movement_time/destiny sin cambiar el pez.
_TRIGGER_FN = """
CREATE OR REPLACE FUNCTION sync_fish_current_pond()
RETURNS trigger AS $$
BEGIN
    IF TG_OP <> 'INSERT' AND OLD.fish_id IS NOT NULL THEN
        PERFORM recompute_fish_current_pond(OLD.fish_id);
    END IF;
    IF TG_OP <> 'DELETE' AND NEW.fish_id IS NOT NULL
       AND (TG_OP = 'INSERT' OR NEW.fish_id IS DISTINCT FROM OLD.fish_id) THEN
        PERFORM recompute_fish_current_pond(NEW.fish_id);
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;
"""

_BACKFILL = """
UPDATE fish f
   SET current_pond_id = sub.dest
  FROM (
        SELECT DISTINCT ON (fish_id) fish_id, destiny_pond_id AS dest
          FROM ponds_movements
         WHERE fish_id IS NOT NULL
         ORDER BY fish_id, movement_time DESC, id DESC
       ) sub
 WHERE f.id = sub.fish_id;
"""


def upgrade() -> None:
    op.add_column("fish", sa.Column("current_pond_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "fish_current_pond_id_fkey", "fish", "ponds", ["current_pond_id"], ["id"]
    )
    op.execute(_BACKFILL)
    # (current_pond_id, state): todas las lecturas filtran por estanque + estado activo.
    op.create_index("ix_fish_current_pond_state", "fish", ["current_pond_id", "state"])
    op.execute(_RECOMPUTE_FN)
    op.execute(_TRIGGER_FN)
    op.execute(
        """
        CREATE TRIGGER trg_sync_fish_current_pond
        AFTER INSERT OR UPDATE OR DELETE ON ponds_movements
        FOR EACH ROW EXECUTE FUNCTION sync_fish_current_pond();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_sync_fish_current_pond ON ponds_movements;")
    op.execute("DROP FUNCTION IF EXISTS sync_fish_current_pond();")
    op.execute("DROP FUNCTION IF EXISTS recompute_fish_current_pond(bigint);")
    op.drop_index("ix_fish_current_pond_state", table_name="fish")
    op.drop_constraint("fish_current_pond_id_fkey", "fish", type_="foreignkey")
    op.drop_column("fish", "current_pond_id")
