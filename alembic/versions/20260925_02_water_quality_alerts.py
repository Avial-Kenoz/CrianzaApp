"""Alertas de calidad de agua: entidad, reglas, destinatarios y bitacora.

El modulo ya sabe DECIDIR que algo esta mal -- el motor calcula el nivel de
cada lectura y el panel lo pinta -- pero ese diagnostico solo existe mientras
alguien tiene la pantalla abierta. Estas cuatro tablas son lo que hace falta
para que salga a buscar a una persona.

- water_quality_alerts: la condicion como entidad con ciclo de vida. El indice
  unico PARCIAL `uniq_water_quality_alert_open` sobre `dedupe_key` garantiza en
  la BD que una condicion viva es UNA alerta, y no una por evaluacion del
  motor. Sin eso, una alarma que dura seis horas serian docenas de avisos.
- water_quality_alert_rules: lo editable. Sembrada con defaults conservadores
  (solo `alarma`), porque el modo de fallar de un sistema de notificaciones es
  el exceso: si molesta, se silencia, y despues no sirve para nada. Aflojar es
  un clic en la UI; recuperar la confianza de la gente, no.
- water_quality_alert_recipients: a quien. Se siembra un destinatario real para
  poder probar el canal de punta a punta desde el primer dia.
- water_quality_alert_notifications: cada envio con su resultado. Bitacora
  auditable y antirrebote a la vez.

No cambia ningun comportamiento existente: nada escribe todavia en estas
tablas. Eso viene en el PR siguiente, con el motor de deteccion.

Revision ID: 20260925_02
Revises: 20260925_01
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.sql import table, column


revision = "20260925_02"
down_revision = "20260925_01"
branch_labels = None
depends_on = None


# (kind, enabled, min_level, cooldown_min, renotify_min, quiet_from, quiet_to,
#  escalate_after_min, label, description)
#
# El silencio nocturno (22:00-07:00) deja pasar `alarma` igual: de noche es
# cuando mas caro sale no enterarse. Lo que corta son las alertas menores.
REGLAS = [
    ("lectura_alarma", True, "alarma", 60, None, 22.0, 7.0, None,
     "Lectura fuera de rango",
     "Una lectura de O2 entra en nivel alarma. Se cierra sola cuando la "
     "siguiente lectura del mismo estanque vuelve a rango."),
    ("ronda_vencida", True, "alarma", 120, 120, 22.0, 7.0, None,
     "Ronda de O2 sin tomar",
     "Se vencio el plazo de la ronda sin lecturas en la unidad. Se agrupa por "
     "UNIDAD: por estanque serian doce avisos identicos la misma noche."),
    ("sin_reconocer", True, "alarma", 60, None, None, None, 30,
     "Alarma sin reconocer",
     "Una lectura en alarma lleva mas de `escalate_after_min` minutos sin que "
     "nadie la reconozca ni registre accion correctiva."),
]

# Destinatario inicial: el canal se prueba de punta a punta con una persona
# real antes de sumar a los encargados de turno.
DESTINATARIOS = [
    ("Andres Vial Undurraga", "8815279164", "alerta",
     "Destinatario inicial de calibracion: recibe todo, incluidas las alertas "
     "menores, para dimensionar el volumen real antes de sumar a los turnos."),
]


def upgrade() -> None:
    # --- la alerta -----------------------------------------------------------
    op.create_table(
        "water_quality_alerts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("dedupe_key", sa.String(length=120), nullable=False),
        sa.Column("level", sa.String(length=20), nullable=False),
        sa.Column("pond_id", sa.BigInteger(), sa.ForeignKey("ponds.id")),
        sa.Column("cultivation_unit_id", sa.BigInteger(),
                  sa.ForeignKey("cultivation_units.id")),
        sa.Column("state", sa.String(length=20), nullable=False,
                  server_default="abierta"),
        sa.Column("opened_at", sa.TIMESTAMP(), nullable=False),
        sa.Column("last_seen_at", sa.TIMESTAMP(), nullable=False),
        sa.Column("closed_at", sa.TIMESTAMP()),
        sa.Column("close_reason", sa.String(length=20)),
        sa.Column("acked_at", sa.TIMESTAMP()),
        sa.Column("acked_by", sa.BigInteger(), sa.ForeignKey("users.id")),
        sa.Column("ack_note", sa.String(length=160)),
        sa.Column("source_reading_id", sa.BigInteger(),
                  sa.ForeignKey("pond_oxygen_readings.id")),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("detail", sa.String(length=400)),
        sa.Column("payload", sa.JSON()),
        sa.Column("notify_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_notified_at", sa.TIMESTAMP()),
        sa.Column("created_at", sa.TIMESTAMP()),
        sa.Column("updated_at", sa.TIMESTAMP()),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_wq_alerts_kind", "water_quality_alerts", ["kind"])
    op.create_index("ix_wq_alerts_state", "water_quality_alerts", ["state"])
    op.create_index("ix_wq_alerts_pond_id", "water_quality_alerts", ["pond_id"])
    op.create_index("ix_wq_alerts_unit_id", "water_quality_alerts",
                    ["cultivation_unit_id"])
    # La vista de monitoreo entra por aca: abiertas primero, mas nuevas arriba.
    op.create_index("ix_wq_alerts_opened_at", "water_quality_alerts", ["opened_at"])

    # Una condicion viva = una alerta. El predicado deja fuera las cerradas, asi
    # que el mismo estanque puede volver a entrar en alarma manana sin chocar
    # con la alerta de hoy.
    op.execute(
        "CREATE UNIQUE INDEX uniq_water_quality_alert_open "
        "ON water_quality_alerts (dedupe_key) WHERE closed_at IS NULL"
    )

    # --- reglas --------------------------------------------------------------
    op.create_table(
        "water_quality_alert_rules",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("min_level", sa.String(length=20), nullable=False,
                  server_default="alarma"),
        sa.Column("cooldown_min", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("renotify_min", sa.Integer()),
        sa.Column("quiet_from", sa.Numeric(4, 2)),
        sa.Column("quiet_to", sa.Numeric(4, 2)),
        sa.Column("escalate_after_min", sa.Integer()),
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column("description", sa.String(length=300)),
        sa.Column("updated_at", sa.TIMESTAMP()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("kind", name="uq_wq_alert_rules_kind"),
    )

    # --- destinatarios -------------------------------------------------------
    op.create_table(
        "water_quality_alert_recipients",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("telegram_chat_id", sa.String(length=40), nullable=False),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id")),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("min_level", sa.String(length=20), nullable=False,
                  server_default="alarma"),
        sa.Column("kinds", sa.String(length=120)),
        sa.Column("unit_ids", sa.String(length=120)),
        sa.Column("hours_from", sa.Numeric(4, 2)),
        sa.Column("hours_to", sa.Numeric(4, 2)),
        sa.Column("weekdays", sa.String(length=20)),
        sa.Column("note", sa.String(length=200)),
        sa.Column("created_at", sa.TIMESTAMP()),
        sa.Column("updated_at", sa.TIMESTAMP()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("telegram_chat_id", name="uq_wq_alert_recipients_chat"),
    )

    # --- bitacora de envios --------------------------------------------------
    op.create_table(
        "water_quality_alert_notifications",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("alert_id", sa.BigInteger(),
                  sa.ForeignKey("water_quality_alerts.id"), nullable=False),
        sa.Column("recipient_id", sa.BigInteger(),
                  sa.ForeignKey("water_quality_alert_recipients.id")),
        sa.Column("channel", sa.String(length=20), nullable=False,
                  server_default="telegram"),
        sa.Column("address", sa.String(length=60), nullable=False),
        sa.Column("reason", sa.String(length=20), nullable=False),
        sa.Column("sent_at", sa.TIMESTAMP(), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("error", sa.String(length=300)),
        sa.Column("message_text", sa.String(length=1000)),
        sa.Column("telegram_message_id", sa.BigInteger()),
        sa.Column("created_at", sa.TIMESTAMP()),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_wq_alert_notifs_alert_id",
                    "water_quality_alert_notifications", ["alert_id"])
    op.create_index("ix_wq_alert_notifs_sent_at",
                    "water_quality_alert_notifications", ["sent_at"])

    # --- semillas ------------------------------------------------------------
    # Sin guarda de duplicados a proposito: las tablas se crean en esta misma
    # migracion, asi que estan vacias por construccion.
    reglas = table("water_quality_alert_rules",
                   column("kind", sa.String), column("enabled", sa.Boolean),
                   column("min_level", sa.String), column("cooldown_min", sa.Integer),
                   column("renotify_min", sa.Integer), column("quiet_from", sa.Numeric),
                   column("quiet_to", sa.Numeric),
                   column("escalate_after_min", sa.Integer),
                   column("label", sa.String), column("description", sa.String))
    op.bulk_insert(reglas, [
        {"kind": k, "enabled": e, "min_level": ml, "cooldown_min": cd,
         "renotify_min": rn, "quiet_from": qf, "quiet_to": qt,
         "escalate_after_min": esc, "label": lb, "description": de}
        for k, e, ml, cd, rn, qf, qt, esc, lb, de in REGLAS
    ])

    dest = table("water_quality_alert_recipients",
                 column("name", sa.String),
                 column("telegram_chat_id", sa.String),
                 column("active", sa.Boolean), column("min_level", sa.String),
                 column("note", sa.String))
    op.bulk_insert(dest, [
        {"name": n, "telegram_chat_id": c, "active": True, "min_level": ml,
         "note": nt}
        for n, c, ml, nt in DESTINATARIOS
    ])


def downgrade() -> None:
    op.drop_table("water_quality_alert_notifications")
    op.drop_table("water_quality_alert_recipients")
    op.drop_table("water_quality_alert_rules")
    op.execute("DROP INDEX IF EXISTS uniq_water_quality_alert_open")
    op.drop_table("water_quality_alerts")
