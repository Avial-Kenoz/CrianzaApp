from sqlalchemy import Column, BigInteger, String, Text, TIMESTAMP, ForeignKey, JSON
from app.db.session import Base


class SexingOfflineOperation(Base):
    """Operación de sexado capturada offline y su resultado al sincronizar.

    Idempotencia por `client_uuid` (generado en el tablet). Al sincronizar:
    - las operaciones limpias se aplican vía apply_fish_save (status=applied),
    - las contingencias (retag, tag ajeno, pez fuera del estanque, etc.) se
      enrutan a la bandeja de reconciliación (status=pending_review) para que
      un supervisor las consolide.
    """

    __tablename__ = "sexing_offline_operations"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    client_uuid = Column(String(64), nullable=False, unique=True)
    session_id = Column(BigInteger, ForeignKey("sexado_offline_sessions.id"), nullable=True)
    pond_id = Column(BigInteger, ForeignKey("ponds.id"), nullable=True)   # estanque fuente
    kind = Column(String(30), nullable=False, default="fish_save")        # fish_save|retag|register|foreign_tag
    pit = Column(String(120))
    fish_id = Column(BigInteger, ForeignKey("fish.id"), nullable=True)
    payload = Column(JSON)              # {sex, weight, diameter, development_state, move_to}
    captured_at = Column(TIMESTAMP)     # hora de captura en terreno
    status = Column(String(20), nullable=False, default="pending_review")  # applied|duplicate|pending_review|error
    result_message = Column(String(255))
    resolution = Column(JSON)           # se completa al reconciliar (PR-S2b)
    applied_at = Column(TIMESTAMP)
    created_at = Column(TIMESTAMP)
