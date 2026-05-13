from sqlalchemy import Column, BigInteger, String, TIMESTAMP, ForeignKey, Text, func
from app.db.session import Base



class TagDetachmentEvent(Base):
    __tablename__ = "tag_detachment_events"

    id = Column(BigInteger, primary_key=True, autoincrement=True)

    # Estanque donde se encontró el pez sin tag
    pond_id = Column(BigInteger, ForeignKey("ponds.id"), nullable=False)

    # Pez al que se le atribuye la pérdida del tag (casi siempre NULL — no identificable)
    fish_id = Column(BigInteger, ForeignKey("fish.id"), nullable=True)

    # Si fue re-taggeado, referencia al nuevo pez creado
    retag_fish_id = Column(BigInteger, ForeignKey("fish.id"), nullable=True)

    event_date = Column(TIMESTAMP, nullable=False)
    registered_by_user_id = Column(BigInteger, ForeignKey("users.id"), nullable=True)
    notes = Column(Text, nullable=True)

    # 'unidentified'  : pez sin tag, sin resolver
    # 'retagged'       : se le asignó un nuevo tag (retag_fish_id apunta al nuevo pez)
    # 'written_off'    : cerrado en reconciliación de cierre de estanque
    status = Column(String(30), nullable=False, default="unidentified")

    # Razón de cierre en reconciliación:
    # 'left_unregistered' : salió con el pool sin tag al vaciar el estanque
    # 'mortality'         : murió sin haberse re-taggeado
    # 'other'             : ver notes
    resolution = Column(String(40), nullable=True)
    resolved_at = Column(TIMESTAMP, nullable=True)

    created_at = Column(TIMESTAMP, server_default=func.now())
