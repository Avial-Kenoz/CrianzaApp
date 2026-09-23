# -*- coding: utf-8 -*-
"""Capacidad de conversion de nitrogeno de un biofiltro, en funcion de T y pH.

Modelo calibrado con los datos de las 3 lagunas del norte (jul-ago 2026, n=45) y
validado de forma independiente contra la estequiometria del alimento: la conversion
medida por balance de masa (3,46 kg N/dia) coincide dentro del 2% con la predicha por
80 kg/dia de alimento al 48% de proteina (3,53 kg N/dia).

NO es una interpolacion sobre la tabla: es la funcion analitica que la genero.
Reproduce las celdas conocidas y se puede evaluar en cualquier punto del rango.

La capacidad tiene dos factores que se mueven en sentidos opuestos con T y pH:

    capacidad = Q * eficiencia(T,pH) * techo_NH4(T,pH)
                    [sube con T y pH]  [BAJA con T y pH]
                     cinetica bacteriana  fraccion NH3 toxica

Con la temperatura los dos casi se cancelan (+-3 C -> +-8%); con el pH no hay nada
que compense la exponencial del NH3 (+-0,5 pH -> factor 3 en cada sentido).

Uso:  python scripts/capacidad_biofiltro.py          # imprime tabla y validacion
      from scripts.capacidad_biofiltro import capacidad
      capacidad(14.0, 7.6)
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

# --- calibracion ------------------------------------------------------------
T_REF, PH_REF = 13.6, 7.50       # condiciones de referencia (medianas medidas)
Y_REF = math.log(0.521 / 0.271)  # k*tau de referencia: ln(C_ent/C_sal), ef. 48,0%
Q_REF = 160.0                    # L/s por biofiltro del norte
THETA = 1.07                     # Arrhenius de nitrificacion, por C (literatura)
NO2_A, NO2_B = 0.1324, 0.58      # ajuste NO2-N = A * NH4-N^B  (R2=0,69, n=45)

# Capacidad intrinseca kV = k * V (L/s): el TECHO del clearance, propiedad del
# biofiltro (medio filtrante x biomasa x velocidad de reaccion), independiente
# del caudal. Se despeja de la eficiencia medida:  kV = -ln(1-ef) * Q.
KV_NORTE = Y_REF * Q_REF         # 104,6 L/s  (promedio de las 3 lagunas)
KV_SUR_ORIENTE = 40.3            # L/s        (ef. 16,7% a 220 L/s)
Q_SUR_ORIENTE = 220.0

NH3_ALERTA, NH3_ALARMA = 0.0125, 0.0250   # mg/L de NH3-N
NO2_ALERTA, NO2_ALARMA = 0.10, 0.50       # mg/L de NO2-N

RANGO_T = (10.5, 16.5)           # rango calibrado; fuera de aqui es extrapolacion
RANGO_PH = (7.0, 8.0)

PC_PROT, K_TAN = 0.48, 0.092     # para convertir kg N/dia <-> kg alimento/dia
SPD = 86400.0


def fraccion_nh3(ph: float, temp_c: float) -> float:
    """Fraccion de amonio total que esta como NH3 no ionizado (Emerson 1975).

    Misma formula que app.services.water_quality.unionized_ammonia_n, replicada
    aqui para que el modulo sea autocontenido.
    """
    pka = 0.09018 + 2729.92 / (temp_c + 273.15)
    return 1.0 / (1.0 + 10.0 ** (pka - ph))


B_PH_EMPIRICO = 0.934   # coef. de pH sobre ln(k), regresion sobre los 45 datos del norte
MODELO_PH = "literatura"  # "literatura" (conservador) | "empirico" (datos propios)


def _f_ph(ph: float) -> float:
    """Correccion de velocidad de nitrificacion por pH.

    Los dos modelos disponibles DISCREPAN justo en 7,2-8,0, que es donde operan
    las lagunas, asi que conviene mirar los dos:

    - "literatura" (Metcalf & Eddy): plana entre 7,2 y 8,0, cae linealmente bajo
      7,2. Conservadora: dice que subir el pH de 7,2 a 7,5 no acelera nada.
    - "empirico": ajuste sobre los 45 datos del norte, ln(k) ~ 0,934*pH
      (t=3,55, significativo). Dice que ese mismo cambio acelera x1,32.

    Subir el pH de Sur Oriente de 7,20 a 7,50 es justamente el experimento que
    distingue un modelo del otro.
    """
    if MODELO_PH == "empirico":
        return math.exp(B_PH_EMPIRICO * (ph - PH_REF))
    return 1.0 if ph >= 7.2 else max(0.10, 1.0 - 0.833 * (7.2 - ph))


def velocidad_rel(temp_c: float, ph: float) -> float:
    """Velocidad de nitrificacion relativa a las condiciones de referencia."""
    return THETA ** (temp_c - T_REF) * _f_ph(ph) / _f_ph(PH_REF)


def kv(temp_c: float, ph: float, kv_ref: float = KV_NORTE) -> float:
    """Capacidad intrinseca del biofiltro (L/s) a T y pH dados.

    Es el techo del clearance: clearance -> kV cuando el caudal -> infinito.
    T y pH la mueven a traves de la velocidad de reaccion; el caudal NO.
    """
    return kv_ref * velocidad_rel(temp_c, ph)


def eficiencia(temp_c: float, ph: float, q_l_s: float = Q_REF,
               kv_ref: float = KV_NORTE) -> float:
    """Remocion fraccional de NH4-N en una pasada por el biofiltro."""
    return 1.0 - math.exp(-kv(temp_c, ph, kv_ref) / q_l_s)


def clearance(temp_c: float, ph: float, q_l_s: float = Q_REF,
              kv_ref: float = KV_NORTE) -> float:
    """Caudal equivalente de agua dejada en cero (L/s) = Q * eficiencia.

    Satura en kV: por mucho caudal que pongas, no puede superarlo.
    """
    return q_l_s * eficiencia(temp_c, ph, q_l_s, kv_ref)


def elasticidad_kv(temp_c: float, ph: float, q_l_s: float = Q_REF,
                   kv_ref: float = KV_NORTE) -> float:
    """d ln(clearance) / d ln(kV): cuanto se traslada una mejora biologica.

    = 1 cerca del techo (todo se traslada), -> 0 si el filtro ya deja el agua
    casi en cero (mejorar la biologia no sirve, falta caudal).
    """
    x = kv(temp_c, ph, kv_ref) / q_l_s
    return x * math.exp(-x) / (1.0 - math.exp(-x))


@dataclass
class Capacidad:
    temp_c: float
    ph: float
    eficiencia: float
    clearance_l_s: float
    techo_nh4: float          # mg/L de NH4-N donde se toca el umbral de NH3
    cap_amonio: float         # kg N/dia
    cap_nitrito: float        # kg N/dia
    limitante: str
    en_rango: bool
    kv: float = 0.0           # capacidad intrinseca (L/s), techo del clearance
    pct_del_techo: float = 0.0  # clearance / kV
    elasticidad: float = 0.0  # cuanto de una mejora biologica llega al clearance

    @property
    def cap(self) -> float:
        """Capacidad efectiva: manda la restriccion mas apretada."""
        return min(self.cap_amonio, self.cap_nitrito)

    @property
    def alimento_kg_dia(self) -> float:
        return self.cap / (PC_PROT * K_TAN)


def capacidad(temp_c: float, ph: float, q_l_s: float = Q_REF,
              kv_ref: float = KV_NORTE,
              nh3_lim: float = NH3_ALERTA, no2_lim: float = NO2_ALERTA) -> Capacidad:
    """Capacidad de conversion de N del biofiltro a temperatura y pH dados.

    Devuelve el techo por amonio y por nitrito por separado, y cual manda. Por
    defecto usa los umbrales de ALERTA; pasa NH3_ALARMA / NO2_ALARMA para el
    techo duro.

    IMPORTANTE: `q_l_s` sin `kv_ref` describe un biofiltro DEL NORTE operado a
    otro caudal, no otro biofiltro. Para Sur Oriente hay que pasar los dos:
        capacidad(13.6, 7.2, q_l_s=Q_SUR_ORIENTE, kv_ref=KV_SUR_ORIENTE)
    """
    ef = eficiencia(temp_c, ph, q_l_s, kv_ref)
    clear = q_l_s * ef
    kv_tp = kv(temp_c, ph, kv_ref)

    # techo por amonio: la fraccion no ionizada fija cuanto NH4 total aguantas
    techo_nh4 = nh3_lim / fraccion_nh3(ph, temp_c)
    cap_nh4 = clear * techo_nh4 / 1e6 * SPD

    # techo por nitrito: el umbral no depende de T ni pH (toxicidad via cloruro),
    # pero el nitrito de equilibrio escala como 1/velocidad de las NOB
    v = velocidad_rel(temp_c, ph)
    nh4_eq = ((no2_lim * v) / NO2_A) ** (1.0 / NO2_B)
    cap_no2 = clear * nh4_eq / 1e6 * SPD

    return Capacidad(
        temp_c=temp_c, ph=ph, eficiencia=ef, clearance_l_s=clear,
        techo_nh4=techo_nh4, cap_amonio=cap_nh4, cap_nitrito=cap_no2,
        limitante="amonio" if cap_nh4 <= cap_no2 else "nitrito",
        en_rango=(RANGO_T[0] <= temp_c <= RANGO_T[1]
                  and RANGO_PH[0] <= ph <= RANGO_PH[1]),
        kv=kv_tp, pct_del_techo=clear / kv_tp,
        elasticidad=elasticidad_kv(temp_c, ph, q_l_s, kv_ref),
    )


def no2_equilibrio(temp_c: float, ph: float, carga_kg_n_dia: Optional[float] = None,
                   q_l_s: float = Q_REF, kv_ref: float = KV_NORTE) -> float:
    """NO2-N de equilibrio esperado (mg/L) a la carga dada."""
    ef = eficiencia(temp_c, ph, q_l_s, kv_ref)
    nh4 = 0.521 if carga_kg_n_dia is None else (
        carga_kg_n_dia * 1e6 / SPD / (q_l_s * ef))
    return NO2_A * nh4 ** NO2_B / velocidad_rel(temp_c, ph)


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    TABLA = {  # celdas ya calculadas, para validar que la funcion las reproduce
        (10.5, 7.0): 32.0, (13.5, 7.0): 29.7,
        (10.5, 7.5): 11.7, (13.5, 7.5): 10.8, (16.5, 7.5): 9.9,
        (13.5, 8.0): 3.5, (16.5, 8.0): 3.2,
    }
    print("=" * 80)
    print("VALIDACION: la funcion reproduce la tabla?  (kg N/dia, techo por amonio)")
    print("=" * 80)
    print(f"{'T':>6}{'pH':>6}{'tabla':>9}{'funcion':>10}{'dif':>8}")
    peor = 0.0
    for (t, p), esperado in sorted(TABLA.items()):
        obt = capacidad(t, p).cap_amonio
        dif = abs(obt - esperado) / esperado
        peor = max(peor, dif)
        print(f"{t:>6.1f}{p:>6.2f}{esperado:>9.1f}{obt:>10.2f}{dif:>7.1%}")
    print(f"\n  Peor discrepancia: {peor:.2%}  (solo redondeo de la tabla a 1 decimal)")

    phs = [7.0, 7.2, 7.4, 7.5, 7.6, 7.8, 8.0]
    temps = (10.5, 12.0, 13.5, 15.0, 16.5)

    print("\n" + "=" * 80)
    print("GRILLA - capacidad por AMONIO (kg N/dia)")
    print("=" * 80)
    print(f"{'T \\ pH':>8}" + "".join(f"{p:>9.1f}" for p in phs))
    for t in temps:
        print(f"{t:>8.1f}" + "".join(
            f"{capacidad(t, p).cap_amonio:>9.1f}" for p in phs))

    print("\n" + "=" * 80)
    print("GRILLA - capacidad por NITRITO (kg N/dia)   <-- suele ser la que manda")
    print("=" * 80)
    print(f"{'T \\ pH':>8}" + "".join(f"{p:>9.1f}" for p in phs))
    for t in temps:
        print(f"{t:>8.1f}" + "".join(
            f"{capacidad(t, p).cap_nitrito:>9.1f}" for p in phs))

    print("\n" + "=" * 80)
    print("GRILLA - LIMITANTE y capacidad efectiva (kg alimento/dia al 48% prot.)")
    print("=" * 80)
    print(f"{'T \\ pH':>8}" + "".join(f"{p:>12.1f}" for p in phs))
    for t in temps:
        fila = ""
        for p in phs:
            c = capacidad(t, p)
            fila += f"{c.alimento_kg_dia:>8.0f} {c.limitante[:3]:<4}"
        print(f"{t:>8.1f}" + fila)
    print("\n  (hoy: 13,6 C / pH 7,50 / 80 kg alimento/dia)")

    print("\n" + "=" * 80)
    print("EL CAUDAL DENTRO DE LA FORMULA: cap = Q*(1-exp(-kV/Q)) * techo")
    print("=" * 80)
    print(f"{'unidad':<16}{'Q':>7}{'kV':>8}{'kV/Q':>7}{'clear':>8}"
          f"{'% techo':>9}{'elast':>8}{'kg N/dia':>10}")
    for nom, q, k in (("Norte", Q_REF, KV_NORTE),
                      ("Sur Oriente", Q_SUR_ORIENTE, KV_SUR_ORIENTE)):
        c = capacidad(T_REF, PH_REF if nom == "Norte" else 7.20, q, k)
        print(f"{nom:<16}{q:>7.0f}{c.kv:>8.1f}{c.kv/q:>7.2f}{c.clearance_l_s:>8.1f}"
              f"{c.pct_del_techo:>8.0%}{c.elasticidad:>8.2f}{c.cap_amonio:>10.1f}")

    print("\n  Ganancia de capacidad por DUPLICAR el caudal (misma biologia):")
    for nom, q, k, p in (("Norte", Q_REF, KV_NORTE, PH_REF),
                         ("Sur Oriente", Q_SUR_ORIENTE, KV_SUR_ORIENTE, 7.20)):
        a = capacidad(T_REF, p, q, k).cap_amonio
        b = capacidad(T_REF, p, q * 2, k).cap_amonio
        top = capacidad(T_REF, p, 1e9, k).cap_amonio
        print(f"    {nom:<14} {a:>5.1f} -> {b:>5.1f} kg N/dia  ({b/a-1:>+5.0%})   "
              f"techo con caudal infinito: {top:.1f}  ({top/a-1:+.0%})")
