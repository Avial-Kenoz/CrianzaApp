"""mantenimiento: bot de Telegram (PR3)

Revision ID: 20260930_01
Revises: 20260929_07
Create Date: 2026-09-30 00:00:00.000000

- mnt_telegram_contactos: todo el que le escribe al bot, vinculado o no. Es la
  «bandeja de sin vincular» (spec §6.3): la primera vez que alguien escribe,
  aparece aquí y el encargado lo asocia a una persona. También permite
  bloquear un remitente.
- mnt_avisos.telegram_chat_id: el chat desde el que se avisó, para confirmarle
  al que avisó (aceptado, reparado) aunque todavía no esté vinculado.
- mnt_notificaciones: bitácora de cada mensaje que el módulo intenta mandar,
  con su resultado. Sin ella, «¿le llegó la alarma al semanero?» no tiene
  respuesta.
- Parámetro oculto `_bot_offset`: último update de Telegram procesado, para
  no reprocesar mensajes tras un reinicio. Las claves con «_» no se muestran
  en la pantalla de parámetros.
"""
from alembic import op
import sqlalchemy as sa


revision = "20260930_01"
down_revision = "20260929_07"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mnt_telegram_contactos",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("telegram_user_id", sa.String(40), nullable=False),
        sa.Column("nombre", sa.String(120)),
        sa.Column("username", sa.String(80)),
        sa.Column("primer_contacto", sa.TIMESTAMP(), nullable=False),
        sa.Column("ultimo_contacto", sa.TIMESTAMP(), nullable=False),
        sa.Column("bloqueado", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("telegram_user_id", name="uq_mnt_telegram_contacto"),
    )
    op.add_column("mnt_avisos", sa.Column("telegram_chat_id", sa.String(40)))
    op.create_table(
        "mnt_notificaciones",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("motivo", sa.String(30), nullable=False),
        sa.Column("aviso_id", sa.BigInteger(), sa.ForeignKey("mnt_avisos.id", ondelete="SET NULL")),
        sa.Column("ot_id", sa.BigInteger(), sa.ForeignKey("mnt_ots.id", ondelete="SET NULL")),
        sa.Column("persona_id", sa.BigInteger(), sa.ForeignKey("mnt_personas.id", ondelete="SET NULL")),
        sa.Column("destino", sa.String(120)),
        sa.Column("chat_id", sa.String(40)),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("error", sa.String(300)),
        sa.Column("enviado_at", sa.TIMESTAMP(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_mnt_notificaciones_aviso", "mnt_notificaciones", ["aviso_id"])
    op.create_index("ix_mnt_notificaciones_ot", "mnt_notificaciones", ["ot_id"])
    op.execute("INSERT INTO mnt_parametros (clave, valor, unidad, descripcion) "
               "VALUES ('_bot_offset', '0', '', 'Último update de Telegram procesado (interno)')")


def downgrade() -> None:
    op.execute("DELETE FROM mnt_parametros WHERE clave = '_bot_offset'")
    op.drop_table("mnt_notificaciones")
    op.drop_column("mnt_avisos", "telegram_chat_id")
    op.drop_table("mnt_telegram_contactos")
