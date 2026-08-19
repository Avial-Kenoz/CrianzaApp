"""motivo estructurado del conflicto de sexado offline

Revision ID: 20260819_01
Revises: 20260812_01
Create Date: 2026-08-19 00:00:00.000000

Hasta ahora el único rastro de POR QUÉ una operación quedó en la bandeja era
`result_message`: texto en español, escrito para que lo lea una persona. El
tablet ahora necesita clasificar el conflicto para avisarle al operador qué
pierde si lo descarta, y clasificar con regex sobre esos mensajes se rompe al
primer cambio de redacción.

`reason_code` guarda el motivo como código estable en el momento de sincronizar.
Las filas viejas se rellenan una sola vez derivándolas del mensaje (abajo), y
las que no matcheen quedan en NULL -> el clasificador las trata como críticas,
que es el lado seguro.
"""
from alembic import op
import sqlalchemy as sa


revision = "20260819_01"
down_revision = "20260812_01"
branch_labels = None
depends_on = None


# (patrón ILIKE, código) — el orden importa: gana el primero que matchea.
_BACKFILL = [
    ("%contingencia%", "contingency"),
    ("%activo en un pez vivo%", "pit_active_on_live_fish"),
    ("%no se puede reutilizar%", "pit_active_on_live_fish"),
    ("%requiere confirmación de reutilización%", "pit_needs_reuse_confirm"),
    ("%tag perdido vigente%", "lost_tag_pending"),
    ("%pit no encontrado%", "pit_not_found"),
    ("%no hay saldo%", "no_untagged_balance"),
    ("%destino no está entre los preconfigurados%", "destination_not_allowed"),
    ("%destino no válido%", "destination_not_allowed"),
    ("%destino debe ser distinto%", "destination_not_allowed"),
    ("%ya no está en este estanque%", "fish_moved_away"),
    ("%no está activo%", "fish_not_active"),
    ("%peso debe ser mayor%", "invalid_measurement"),
    ("%diámetro debe ser mayor%", "invalid_measurement"),
    ("%estado de desarrollo%", "invalid_measurement"),
    ("%debe indicar sexo%", "invalid_measurement"),
]


def upgrade() -> None:
    op.add_column("sexing_offline_operations", sa.Column("reason_code", sa.String(length=40)))
    conn = op.get_bind()
    for pattern, code in _BACKFILL:
        conn.execute(
            sa.text("""
                UPDATE sexing_offline_operations
                   SET reason_code = :code
                 WHERE reason_code IS NULL
                   AND result_message ILIKE :pattern
            """),
            {"code": code, "pattern": pattern},
        )
    # las contingencias se reconocen por kind aunque el mensaje haya cambiado
    conn.execute(sa.text("""
        UPDATE sexing_offline_operations
           SET reason_code = 'contingency'
         WHERE reason_code IS NULL AND kind IN ('retag', 'foreign_tag')
    """))


def downgrade() -> None:
    op.drop_column("sexing_offline_operations", "reason_code")
