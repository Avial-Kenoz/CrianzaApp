"""directorio de personas único para la app

Revision ID: 20260930_03
Revises: 20260930_02
Create Date: 2026-09-30 00:00:00.000000

Crea `personas` (identidad: nombre, Telegram, teléfono) y hace que calidad de
agua (`water_quality_alert_recipients`) y mantenimiento (`mnt_personas`)
apunten a ella con `persona_id`, conservando cada uno sus preferencias.

Unificación de lo existente: una persona por **Telegram** (el mismo usuario de
Telegram es la misma persona, aunque el nombre esté escrito distinto en cada
módulo: «Andres» / «Andrés»). Sin Telegram, por nombre. Se prefiere el nombre
con más letras «de verdad» (tildes incluidas) — en la práctica, el más completo.

**Expandir y después contraer**, como en 20260930_02: solo agrega. Las
columnas de identidad de cada módulo (`name`/`telegram_chat_id` en calidad de
agua, `nombre`/`telegram_user_id`/`bot_iniciado` en mantenimiento) se
conservan porque el proceso de producción que corre todavía las usa; pasan a
aceptar NULL porque el código nuevo ya no las escribe. Se borran en una
migración posterior al despliegue. Mientras tanto, al arrancar, la app enlaza
con una persona cualquier fila que haya quedado sin `persona_id`.
"""
import unicodedata

from alembic import op
import sqlalchemy as sa


revision = "20260930_03"
down_revision = "20260930_02"
branch_labels = None
depends_on = None


def _clave(nombre: str) -> str:
    s = unicodedata.normalize("NFKD", (nombre or "").strip().lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def upgrade() -> None:
    op.create_table(
        "personas",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("nombre", sa.String(120), nullable=False),
        sa.Column("telegram_id", sa.String(40)),
        sa.Column("telefono", sa.String(40)),
        sa.Column("email", sa.String(190)),
        sa.Column("user_id", sa.BigInteger(), sa.ForeignKey("users.id")),
        sa.Column("notas", sa.Text()),
        sa.Column("activo", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.TIMESTAMP()),
        sa.Column("updated_at", sa.TIMESTAMP()),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("telegram_id", name="uq_personas_telegram"),
    )
    op.add_column("water_quality_alert_recipients",
                  sa.Column("persona_id", sa.BigInteger(), sa.ForeignKey("personas.id")))
    op.create_unique_constraint("uq_wq_recipient_persona", "water_quality_alert_recipients", ["persona_id"])
    op.alter_column("water_quality_alert_recipients", "name", nullable=True)
    op.alter_column("water_quality_alert_recipients", "telegram_chat_id", nullable=True)

    op.add_column("mnt_personas", sa.Column("persona_id", sa.BigInteger(), sa.ForeignKey("personas.id")))
    op.create_unique_constraint("uq_mnt_persona_persona", "mnt_personas", ["persona_id"])
    op.alter_column("mnt_personas", "nombre", nullable=True)

    # --- unificar lo existente ----------------------------------------------------
    con = op.get_bind()
    filas = []   # (tabla, id, nombre, telegram, activo)
    for r in con.execute(sa.text("SELECT id, name, telegram_chat_id, active FROM water_quality_alert_recipients")):
        filas.append(("wq", r[0], r[1], r[2], r[3]))
    for r in con.execute(sa.text("SELECT id, nombre, telegram_user_id, activo FROM mnt_personas")):
        filas.append(("mnt", r[0], r[1], r[2], r[3]))

    grupos: dict = {}
    for f in filas:
        clave = ("tg", f[3]) if f[3] else ("nombre", _clave(f[2]))
        grupos.setdefault(clave, []).append(f)
    for clave, fs in grupos.items():
        # El nombre más completo: más caracteres no ASCII (tildes) y más largo.
        nombre = max((f[2] or "" for f in fs), key=lambda n: (sum(ord(c) > 127 for c in n), len(n)))
        pid = con.execute(sa.text(
            "INSERT INTO personas (nombre, telegram_id, activo, created_at, updated_at) "
            "VALUES (:n, :t, :a, now(), now()) RETURNING id"),
            {"n": nombre or "(sin nombre)", "t": clave[1] if clave[0] == "tg" else None,
             "a": any(bool(f[4]) for f in fs)}).scalar()
        for tabla, rid, *_ in fs:
            t = "water_quality_alert_recipients" if tabla == "wq" else "mnt_personas"
            con.execute(sa.text(f"UPDATE {t} SET persona_id = :p WHERE id = :i"), {"p": pid, "i": rid})


def downgrade() -> None:
    op.drop_constraint("uq_mnt_persona_persona", "mnt_personas", type_="unique")
    op.drop_column("mnt_personas", "persona_id")
    op.drop_constraint("uq_wq_recipient_persona", "water_quality_alert_recipients", type_="unique")
    op.drop_column("water_quality_alert_recipients", "persona_id")
    op.drop_table("personas")
    # Las columnas de identidad quedan nullable: volver a NOT NULL fallaría si el
    # código nuevo creó filas sin ellas.
