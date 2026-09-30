"""Directorio de personas de CrianzaApp (quién es quién, una sola vez).

Hasta ahora cada módulo guardaba su propia copia: calidad de agua tenía sus
destinatarios (nombre + chat de Telegram) y mantenimiento sus personas (nombre
+ Telegram). El encargado quedaba registrado dos veces, con el nombre escrito
distinto, y corregir un Telegram en un lado no lo corregía en el otro.

Ahora la **identidad** vive aquí y cada módulo guarda solo sus **preferencias**
apuntando a una persona:
  - `water_quality_alert_recipients.persona_id`: nivel mínimo, rotación de
    semaneros, filtros por tipo/unidad/horario.
  - `mnt_personas.persona_id`: rol en mantenimiento, sitios de alarma, horario
    del resumen.

No es la tabla `users`: esa tiene 27 registros heredados del sistema antiguo
con correo obligatorio y sirve a los selectores de operador de las apps de
captura; mezclarlas confundiría «quién opera» con «a quién se avisa».
`user_id` enlaza con ella cuando son la misma persona.

`telegram_id` es texto porque un chat de grupo tiene id negativo y largo.
"""
from sqlalchemy import Column, BigInteger, String, Text, Boolean, TIMESTAMP, ForeignKey

from app.db.session import Base


class Persona(Base):
    __tablename__ = "personas"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    nombre = Column(String(120), nullable=False)
    telegram_id = Column(String(40), unique=True)
    telefono = Column(String(40))
    email = Column(String(190))
    user_id = Column(BigInteger, ForeignKey("users.id"))
    notas = Column(Text)
    activo = Column(Boolean, nullable=False, default=True)
    created_at = Column(TIMESTAMP)
    updated_at = Column(TIMESTAMP)
