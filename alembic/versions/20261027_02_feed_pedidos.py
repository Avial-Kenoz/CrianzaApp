"""Pedidos de reposición de tarros; una salida puede venir de un pedido.

feed_execution_events.feed_program_id pasa a opcional (las salidas nuevas no
tienen programa semanal) y se agrega feed_pedido_line_id.

Revision ID: 20261027_02
Revises: 20261027_01
"""
from alembic import op
import sqlalchemy as sa


revision = "20261027_02"
down_revision = "20261027_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "feed_pedidos",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(40), nullable=False, unique=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="emitido"),
        sa.Column("dias_n", sa.Integer, nullable=False),
        sa.Column("dias_min", sa.Integer, nullable=False),
        sa.Column("dias_semana", sa.Integer, nullable=False),
        sa.Column("note", sa.Text),
        sa.Column("created_by", sa.BigInteger, sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.TIMESTAMP),
        sa.Column("salio_date", sa.Date),
        sa.Column("salio_by", sa.BigInteger, sa.ForeignKey("users.id")),
        sa.Column("salio_at", sa.TIMESTAMP),
        sa.Column("anulado_by", sa.BigInteger, sa.ForeignKey("users.id")),
        sa.Column("anulado_at", sa.TIMESTAMP),
    )
    op.create_table(
        "feed_pedido_lines",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("pedido_id", sa.BigInteger, sa.ForeignKey("feed_pedidos.id"), nullable=False, index=True),
        sa.Column("pond_id", sa.BigInteger, sa.ForeignKey("ponds.id"), nullable=False, index=True),
        sa.Column("feed_type_id", sa.BigInteger, sa.ForeignKey("feed_types.id"), nullable=False),
        sa.Column("bags", sa.Integer, nullable=False),
        sa.Column("bag_kg", sa.Numeric(8, 3), nullable=False),
        sa.Column("saldo_kg", sa.Numeric(10, 2)),
        sa.Column("ritmo_kg", sa.Numeric(10, 2)),
        sa.Column("sugerido_bags", sa.Integer),
        sa.Column("limitado_tope", sa.Boolean, nullable=False, server_default=sa.false()),
    )
    op.alter_column("feed_execution_events", "feed_program_id", existing_type=sa.BigInteger, nullable=True)
    op.add_column("feed_execution_events",
                  sa.Column("feed_pedido_line_id", sa.BigInteger, sa.ForeignKey("feed_pedido_lines.id")))
    op.create_index("ix_feed_execution_events_feed_pedido_line_id", "feed_execution_events",
                    ["feed_pedido_line_id"])


def downgrade() -> None:
    op.drop_index("ix_feed_execution_events_feed_pedido_line_id", table_name="feed_execution_events")
    op.drop_column("feed_execution_events", "feed_pedido_line_id")
    op.alter_column("feed_execution_events", "feed_program_id", existing_type=sa.BigInteger, nullable=False)
    op.drop_table("feed_pedido_lines")
    op.drop_table("feed_pedidos")
