from sqlalchemy import (Column, BigInteger, String, Numeric, TIMESTAMP,
                        ForeignKey, Index, text)
from app.db.session import Base


class DosingInstruction(Base):
    """Lo que el supervisor manda a la tablet: «a esta unidad, X kg/día de Y».

    La tablet muestra `kg_day` y nada más. `suggested_kg_day` es la sugerencia
    del modelo en el momento de instruir y queda solo para auditoría (cuánto
    se apartó la decisión del cálculo); nunca viaja a la tablet.

    Vale hasta que se reemplace: una instrucción nueva para la misma unidad y
    producto cierra la anterior (`ended_at`). Así la historia queda completa y
    nunca hay dos vigentes a la vez (índice único parcial).

    `kg_day = 0` es una instrucción válida: «no aplicar». Para que el producto
    desaparezca de la tablet se cierra la vigente sin reemplazo.

    `received_at` se marca la primera vez que la tablet descarga la
    instrucción: el escritorio lo muestra para saber si ya llegó.
    """

    __tablename__ = "dosing_instructions"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    cultivation_unit_id = Column(BigInteger, ForeignKey("cultivation_units.id"),
                                 nullable=False, index=True)
    product = Column(String(30), nullable=False)      # mismos valores que water_treatment_doses
    kg_day = Column(Numeric(10, 2), nullable=False)
    suggested_kg_day = Column(Numeric(10, 2))
    note = Column(String(255))

    issued_by_id = Column(BigInteger, ForeignKey("users.id"))
    issued_at = Column(TIMESTAMP, nullable=False,
                       server_default=text("CURRENT_TIMESTAMP"))
    ended_at = Column(TIMESTAMP)
    received_at = Column(TIMESTAMP)

    __table_args__ = (
        Index("uniq_dosing_instruction_active", "cultivation_unit_id", "product",
              unique=True, postgresql_where=text("ended_at IS NULL")),
    )
