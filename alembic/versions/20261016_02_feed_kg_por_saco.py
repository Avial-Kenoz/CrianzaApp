"""Kilos de alimento: peso del saco desde la guía y relleno de los kg históricos.

feed_types.bag_kg estaba en 20 para todos, pero Progress y Reproductor llegan
en sacos de 25 kg (lo dicen las guías de ingreso). Y las salidas de bodega
(confirmaciones, ajustes, líneas de programa) se grababan con kg = 0: solo
había sacos. Desde aquí:

  - bag_kg = peso del saco de la guía más reciente de cada tipo.
  - kg = sacos × bag_kg en todo lo que quedó en 0 (las entradas por guía ya
    traían sus kg reales y no se tocan).

Solo rellena ceros, así que es idempotente y no pisa kg ya registrados.

Revision ID: 20261016_02
Revises: 20261016_01
"""
from alembic import op


revision = "20261016_02"
down_revision = "20261016_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        UPDATE feed_types ft
           SET bag_kg = g.bag_weight_kg, updated_at = now()
          FROM (SELECT DISTINCT ON (l.feed_type_id) l.feed_type_id, l.bag_weight_kg
                  FROM feed_receipt_lines l
                  JOIN feed_receipt_headers h ON h.id = l.feed_receipt_id
                 WHERE h.status = 'confirmed'
                 ORDER BY l.feed_type_id, h.fecha_ingreso DESC, l.id DESC) g
         WHERE g.feed_type_id = ft.id
           AND ft.bag_kg IS DISTINCT FROM g.bag_weight_kg
    """)
    op.execute("""
        UPDATE feed_execution_events e
           SET confirmed_kg = e.confirmed_bags * ft.bag_kg
          FROM feed_types ft
         WHERE ft.id = e.feed_type_id AND ft.bag_kg IS NOT NULL
           AND COALESCE(e.confirmed_kg, 0) = 0 AND e.confirmed_bags <> 0
    """)
    op.execute("""
        UPDATE feed_stock_ledger s
           SET kg_delta = s.bags_delta * ft.bag_kg
          FROM feed_types ft
         WHERE ft.id = s.feed_type_id AND ft.bag_kg IS NOT NULL
           AND s.movement_type <> 'entry'
           AND COALESCE(s.kg_delta, 0) = 0 AND s.bags_delta <> 0
    """)
    for bags, kg in (("planned_bags", "planned_kg"),
                     ("executed_bags", "executed_kg"),
                     ("remaining_bags", "remaining_kg")):
        op.execute(f"""
            UPDATE feed_program_lines l
               SET {kg} = l.{bags} * ft.bag_kg
              FROM feed_types ft
             WHERE ft.id = l.feed_type_id AND ft.bag_kg IS NOT NULL
               AND COALESCE(l.{kg}, 0) = 0 AND l.{bags} <> 0
        """)


def downgrade() -> None:
    # Volver a kg = 0 solo destruiría información; no hay nada que deshacer.
    pass
