"""Turno de semaneros: quien esta de turno cada semana.

El centro se cubre con tres encargados que se turnan: uno esta disponible y
atento 24/7, y los otros dos esa semana no. Sin esto, la lista de
destinatarios es plana y los tres reciben siempre lo mismo -- que es
exactamente como se pierde un aviso: si le llega a todos, cada uno puede
suponer que responde otro.

Dos piezas:

- `water_quality_oncall_weeks`: una fila por semana (lunes) con el semanero
  asignado. Se precarga el mes desde la vista. `recipient_id` admite NULL para
  poder dejar una semana explicitamente sin asignar.
- `water_quality_alert_recipients.in_rotation`: si esa persona entra o no en el
  turno. Quien queda fuera -- jefatura, responsabilidades diferenciadas --
  recibe SOLO las alarmas, nunca los consolidados de turno.

Las reglas de reparto, que viven en `wq_notify`:

  semanero de la semana        -> todo, y 24/7 (se ignora su ventana horaria:
                                  estar de turno es justamente eso)
  en rotacion, no es semanero  -> nada
  semana sin asignar           -> todos los de la rotacion (omitir la
                                  configuracion no puede dejar al centro mudo)
  fuera de rotacion            -> solo alarmas

Las vacaciones no necesitan campo propio: esas semanas simplemente no se le
asignan. `in_rotation` es para el que nunca es semanero, no para el que esta
de vacaciones dos semanas.

Revision ID: 20260929_04
Revises: 20260929_03
"""
from alembic import op
import sqlalchemy as sa


revision = "20260929_04"
down_revision = "20260929_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("water_quality_alert_recipients",
                  sa.Column("in_rotation", sa.Boolean(), nullable=False,
                            server_default=sa.true()))

    op.create_table(
        "water_quality_oncall_weeks",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        # Lunes de la semana. Unico: un solo semanero a la vez, garantizado por
        # la BD y no por la UI.
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("recipient_id", sa.BigInteger(),
                  sa.ForeignKey("water_quality_alert_recipients.id")),
        sa.Column("note", sa.String(length=200)),
        sa.Column("created_at", sa.TIMESTAMP()),
        sa.Column("updated_at", sa.TIMESTAMP()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("week_start", name="uq_wq_oncall_week"),
    )
    op.create_index("ix_wq_oncall_week_start", "water_quality_oncall_weeks",
                    ["week_start"])

    # El destinatario inicial es jefatura, no semanero: recibe las alarmas
    # todas las semanas y no entra en el turno.
    op.execute("UPDATE water_quality_alert_recipients SET in_rotation = false")


def downgrade() -> None:
    op.drop_table("water_quality_oncall_weeks")
    op.drop_column("water_quality_alert_recipients", "in_rotation")
