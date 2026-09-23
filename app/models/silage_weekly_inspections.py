from sqlalchemy import Column, BigInteger, String, Integer, Numeric, Boolean, Date, TIMESTAMP
from app.db.session import Base


class SilageWeeklyInspection(Base):
    """Inspección semanal de la zona de ensilaje (mitad derecha del PC 03.2).

    A diferencia de las cargas, es un registro **único por semana** (un
    inspector, una revisión), de ahí el unique en `week_start`.

    Los valores declarados (`acid_used_lts`, `acid_available_lts`, `drums_*`) se
    conservan TAL CUAL los escribe el inspector: son el registro con valor legal.
    El sistema calcula su propia versión desde el inventario y muestra el
    descuadre, pero nunca sobrescribe lo declarado.

    `responsible_name` e `inspector_name` son texto escrito, no firmas
    acreditadas (ver SilageGrindingEvent.received_by_name).
    """

    __tablename__ = "silage_weekly_inspections"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    week_start = Column(Date, nullable=False, unique=True)  # lunes de la semana ISO

    cleanliness_ok = Column(Boolean, nullable=False, default=False)
    drums_sealed_ok = Column(Boolean, nullable=False, default=False)
    # En el formato lleno esta celda se anota como conteo, no como check.
    drums_sealed_count = Column(Integer)

    acid_used_lts = Column(Numeric(10, 2))
    acid_available_lts = Column(Numeric(10, 2))
    drums_used = Column(Integer)
    drums_available = Column(Integer)
    acid_lot = Column(String(60))

    responsible_name = Column(String(120))
    inspector_name = Column(String(120))
    inspected_at = Column(TIMESTAMP)

    observation = Column(String(255))
    client_uuid = Column(String(64), unique=True)
    created_at = Column(TIMESTAMP)
    updated_at = Column(TIMESTAMP)
