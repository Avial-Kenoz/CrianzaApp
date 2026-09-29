"""Dos compuertas entre detectar y avisar: atraso minimo y resumen diario.

Los primeros cinco dias del motor en produccion dieron ~15,6 alertas al dia,
casi todas de ronda vencida y repartidas parejo entre las diez unidades. La
causa no es que la operacion falle: la ronda de noche termina cerca de las
06:15 y la de dia empieza cerca de las 09:30, y como una lectura de las 06:15
cae en turno `off` hereda un plazo de 3 h, asi que TODAS las unidades vencen
juntas alrededor de las 09:10. Todos los dias.

Detectar eso esta bien -- el panel lo muestra y el historial lo guarda. Lo que
no se sostiene es mandarlo por Telegram: quince mensajes diarios que casi
siempre significan "la ronda viene un poco atrasada" terminan silenciados, y el
dia que uno signifique otra cosa tampoco se va a leer.

Se separa entonces DETECTAR de AVISAR, con dos columnas:

- `notify_after_min`: la alerta no avisa hasta llevar ese rato abierta. Como
  una alerta se cierra sola apenas llega el dato que faltaba, "abierta 180 min"
  significa exactamente "nadie midio en las 3 h siguientes al vencimiento", o
  sea ~6 h sin medir. Sobre el historico, 180 min baja las rondas de 15,6 a 4,6
  avisos al dia y deja pasar el 29% de los casos: los que de verdad son un hueco
  y no un atraso.
- `digest_at`: hora decimal del resumen diario. Con esto puesto, los avisos de
  RUTINA de ese tipo no salen al instante; se juntan y salen una vez al dia. Si
  ademas hay `notify_after_min`, la alerta que lo supera rompe el silencio y
  sale igual en el momento.

`lectura_alarma` no lleva ninguna de las dos: son ~2,4 al dia con mediana de 5 h
abierta. Poco volumen y mucha senal -- eso se avisa al instante.

Revision ID: 20260929_01
Revises: 20260925_03
"""
from alembic import op
import sqlalchemy as sa


revision = "20260929_01"
down_revision = "20260925_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("water_quality_alert_rules",
                  sa.Column("notify_after_min", sa.Integer()))
    op.add_column("water_quality_alert_rules",
                  sa.Column("digest_at", sa.Numeric(4, 2)))

    # Ronda vencida: al resumen de las 08:00, salvo que lleve 3 h sin
    # resolverse. Los valores son editables desde la vista de reglas; estos son
    # los que el historico de los primeros cinco dias justifica.
    op.execute(
        "UPDATE water_quality_alert_rules "
        "SET notify_after_min = 180, digest_at = 8.0 "
        "WHERE kind = 'ronda_vencida'"
    )
    op.execute(
        "UPDATE water_quality_alert_rules "
        "SET description = description || ' Se avisa en el resumen diario; "
        "solo rompe el silencio la que lleva mas de notify_after_min sin "
        "resolverse.' WHERE kind = 'ronda_vencida'"
    )


def downgrade() -> None:
    op.drop_column("water_quality_alert_rules", "digest_at")
    op.drop_column("water_quality_alert_rules", "notify_after_min")
