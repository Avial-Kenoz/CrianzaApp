from sqlalchemy import (Column, BigInteger, String, Numeric, TIMESTAMP,
                        ForeignKey, text)
from app.db.session import Base


class WaterTreatmentDose(Base):
    """Una carga de insumo a una unidad: bicarbonato o sal.

    No lleva stock a proposito. Mientras esto sea una prueba, el saldo de
    bodega es complejidad que no paga; lo que importa es cuanto se echo,
    cuando, y que hizo la alcalinidad despues.

    `alk_before` / `alk_after_2h` solo se piden durante el choque: son el par
    dosis-respuesta que calibra el consumo real, que es el termino que hoy no
    cuadra (el agua fresca aporta mas alcalinidad de la que la nitrificacion
    deberia consumir, y aun asi medimos 37 mg/L). Terminada la calibracion,
    la alcalinidad vuelve a la rotacion del biofiltro.
    """

    __tablename__ = "water_treatment_doses"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    cultivation_unit_id = Column(BigInteger, ForeignKey("cultivation_units.id"),
                                 nullable=False, index=True)
    operator_id = Column(BigInteger, ForeignKey("users.id"))
    applied_at = Column(TIMESTAMP, nullable=False)

    product = Column(String(30), nullable=False)      # bicarbonato | sal
    kg = Column(Numeric(10, 2), nullable=False)
    mode = Column(String(20), nullable=False, default="mantencion")  # choque | mantencion
    application_point = Column(String(120))

    alk_before = Column(Numeric(8, 2))
    alk_after_2h = Column(Numeric(8, 2))
    ph_before = Column(Numeric(4, 2))
    ph_after_2h = Column(Numeric(4, 2))

    observation = Column(String(255))
    created_at = Column(TIMESTAMP, server_default=text("CURRENT_TIMESTAMP"))
