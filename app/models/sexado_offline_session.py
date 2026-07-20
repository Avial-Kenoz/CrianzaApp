from sqlalchemy import Column, BigInteger, String, Text, TIMESTAMP, ForeignKey, JSON
from app.db.session import Base


class SexadoOfflineSession(Base):
    """Sesión de sexado offline (modelo checkout).

    Al iniciar, bloquea 1 estanque fuente + hasta 10 destinos: mientras la
    sesión está `active`, esos estanques quedan en solo-lectura para el flujo
    online. `token` lo guarda el cliente PWA y lo presenta al sincronizar.
    `pond_ids` incluye la fuente + todos los destinos (para el chequeo de lock).
    """

    __tablename__ = "sexado_offline_sessions"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    token = Column(String(64), nullable=False, unique=True)
    operator_id = Column(BigInteger, ForeignKey("users.id"), nullable=True)
    source_pond_id = Column(BigInteger, ForeignKey("ponds.id"), nullable=False)
    pond_ids = Column(JSON, nullable=False)          # [fuente, ...destinos]
    status = Column(String(20), nullable=False, default="active")  # active|released|synced
    notes = Column(Text)
    created_at = Column(TIMESTAMP)
    released_at = Column(TIMESTAMP)
