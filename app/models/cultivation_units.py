from sqlalchemy import Column, BigInteger, String, TIMESTAMP, Numeric, Boolean, Date
from app.db.session import Base

class CultivationUnit(Base):
    __tablename__ = "cultivation_units"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    legacy_id = Column(BigInteger, unique=True)
    name = Column(String(120), nullable=False)
    created_at = Column(TIMESTAMP)
    updated_at = Column(TIMESTAMP)

    # Config para los indicadores de salud del biofiltro (migracion 20260907_01).
    # NO hay caudal a proposito: k se calcula desde el alimento y Q se cancela.
    media_volume_m3 = Column(Numeric(10, 2))          # volumen de medio filtrante
    is_recirculating = Column(Boolean, nullable=False, default=True)
    # Agua fresca que entra (= purga que sale). Segundo sumidero de N: se lleva
    # amonio a la concentracion de la laguna, y hay que descontarlo antes de
    # atribuirle al biofiltro todo el nitrogeno del alimento.
    freshwater_l_s = Column(Numeric(8, 2))
    # Caudal de recirculacion NOMINAL (aforo 2026, no se mide en continuo).
    # `flow_is_nominal` obliga a que la UI marque los derivados como estimados.
    recirculation_l_s = Column(Numeric(8, 2))
    flow_is_nominal = Column(Boolean, nullable=False, default=True)

    # Configuracion del biofiltro (migracion 20260924_01). `media_volume_m3`
    # dice cuanto medio hay; estos dicen que SUPERFICIE ofrece, que es lo que
    # permite comparar la carga (g TAN/m2/d) entre lagunas y con literatura.
    reactor_volume_m3 = Column(Numeric(10, 2))     # volumen util del reactor
    media_fill_pct = Column(Numeric(5, 2))         # % del reactor con biomedio
    media_ssa_m2_m3 = Column(Numeric(8, 1))        # superficie especifica del medio
    media_type = Column(String(60))
    # Reactor aireado (migracion 20260925_03). Con aire adentro el ΔOD a
    # traves del reactor no mide respiracion: el aire repone el O2 tan
    # rapido como la biopelicula lo consume, y de paso arrastra el CO2,
    # que es por que el pH tampoco cambia de entrada a salida.
    reactor_is_aerated = Column(Boolean, nullable=False, default=True)

    # Agua de la unidad: sin esto no hay dosis de choque. Sembrado desde la
    # suma de ponds.volume; editable porque no sabemos si incluye el canal.
    water_volume_m3 = Column(Numeric(12, 2))
    water_volume_is_estimated = Column(Boolean, nullable=False, default=True)

    # Objetivos de la terapia.
    alk_target_mg_l = Column(Numeric(6, 1))
    chloride_base_mg_l = Column(Numeric(8, 2))
    chloride_is_measured = Column(Boolean, nullable=False, default=False)

    # Regimen de muestreo: basal | choque | floracion | post_choque. Define
    # cada cuanto vence la alcalinidad. Vuelve solo a basal.
    sampling_regime = Column(String(20), nullable=False, default="basal")
    sampling_regime_since = Column(Date)
