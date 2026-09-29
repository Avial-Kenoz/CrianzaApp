"""Recalcula el nivel de las rondas vencidas ya registradas.

Las ~78 filas de `ronda_vencida` que dejaron los primeros cinco dias nacieron
todas en `alarma`, porque el motor de entonces no distinguia atraso de hueco.
Con el criterio nuevo casi todas son `alerta`, y mientras digan `alarma` la
tabla de volumen de la vista proyecta al reves justamente lo que se acaba de
corregir: mostraria que casi todo suena.

El recalculo sale de cuanto estuvo la alerta sin resolverse, que es la misma
cantidad que el motor evalua en vivo: la alerta abre cuando el atraso cruza el
plazo, asi que de ahi en adelante "minutos abierta" y "minutos de atraso sobre
el plazo" son el mismo numero. Y como se cierra sola en cuanto llega el dato
que faltaba, tambien es "cuanto tardaron en medir".

    alarma  <=>  minutos_abierta >= notify_after_min

(El atraso guardado en el payload NO sirve: es el del instante en que la fila
se creo, que por construccion es ~0. Calculado asi, todo seria `alerta`.)

Se reescribe un nivel ya persistido, que normalmente no se toca. Se justifica
porque estas filas son material de calibracion anterior al lanzamiento: nunca
se envio un mensaje por ninguna de ellas, no hay nadie que haya leido un texto
que ahora vaya a decir otra cosa, y su unica funcion es dimensionar el volumen
del canal antes de encenderlo. Las filas sin payload utilizable se dejan como
estan.

Revision ID: 20260929_03
Revises: 20260929_02
"""
from alembic import op


revision = "20260929_03"
down_revision = "20260929_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        UPDATE water_quality_alerts a
           SET level = 'alerta'
         WHERE a.kind = 'ronda_vencida'
           AND a.level = 'alarma'
           AND EXTRACT(EPOCH FROM (COALESCE(a.closed_at, now()) - a.opened_at)) / 60
               < COALESCE((SELECT r.notify_after_min
                             FROM water_quality_alert_rules r
                            WHERE r.kind = 'ronda_vencida'), 180)
    """)


def downgrade() -> None:
    # El nivel anterior era 'alarma' para todas las de este tipo.
    op.execute("UPDATE water_quality_alerts SET level = 'alarma' "
               "WHERE kind = 'ronda_vencida'")
