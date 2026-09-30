"""Reglas puras del módulo de mantenimiento. Sin BD ni FastAPI.

Viven aparte para poder probarlas sin levantar nada y para que la web, la API
de Planta y el bot usen exactamente la misma regla (spec §8.1).
"""
from __future__ import annotations

import re
from typing import Optional

# ---------------------------------------------------------------------------
# Criticidad (spec §4)
# ---------------------------------------------------------------------------
# Si cambia la tabla o los ajustes, subir la versión: cada evaluación guarda
# con qué versión se calculó, y las respuestas quedan para recalcular.
# v2: con grupos de redundancia, la consecuencia (Q1, Q4) es del grupo y el
# respaldo (Q2) se calcula del margen del grupo; Q2 manual queda solo para
# respaldos que no son equipos (p. ej. oxígeno de emergencia).
REGLA_CRITICIDAD_VERSION = "v2"

# Q1 es la única pregunta cuyo TEXTO cambia según el sitio; la escala (a/b/c)
# y la regla son las mismas.
PREGUNTA_IMPACTO = {
    "crianza": "Si este equipo se detiene, ¿en cuánto tiempo hay daño o mortalidad en peces?",
    "planta": ("Si este equipo se detiene, ¿en cuánto tiempo se compromete producto, "
               "cadena de frío o inocuidad?"),
}
OPCIONES_IMPACTO = {
    "crianza": [("a", "Menos de 2 h"), ("b", "Menos de 24 h"),
                ("c", "No afecta a peces directamente")],
    "planta": [("a", "Menos de 2 h"), ("b", "Menos de 24 h"),
               ("c", "No compromete producto")],
}
# Solo para equipos SIN grupo: un respaldo que no es un equipo registrado (el
# de otra tecnología, que no es simétrico). Si el respaldo es otro equipo, se
# arma un grupo de redundancia y esta pregunta la responde el grupo.
PREGUNTA_RESPALDO = ("¿Tiene un respaldo que NO está registrado como equipo? "
                     "(p. ej. oxígeno de emergencia)")
OPCIONES_RESPALDO = [("a", "Ninguno"), ("b", "Sí, manual (alguien lo tiene que activar)"),
                     ("c", "Sí, automático")]
# Automática = no hace falta que nadie haga nada: incluye a los equipos que ya
# operan todos en paralelo (sopladores centralizados), donde si uno cae los
# demás siguen sin intervención.
CONMUTACIONES = {"automatica": "Automática (o todos operando en paralelo)",
                 "manual": "Manual (alguien tiene que cambiarlo)"}
PREGUNTA_REPOSICION = "¿Cuánto demora reponerlo (repuesto o técnico)?"
OPCIONES_REPOSICION = [("a", "Más de 1 semana"), ("b", "De 1 a 7 días"),
                       ("c", "Menos de 1 día")]
PREGUNTA_SEGURIDAD = "¿Su falla pone en riesgo a personas o al medio ambiente?"
OPCIONES_SEGURIDAD = [("si", "Sí"), ("no", "No")]

# Tabla Q1 (impacto) × Q2 (respaldo). Escrita como tabla y no como puntaje
# ponderado para que el resultado se pueda explicar mirándola.
_MATRIZ = {
    ("a", "a"): "A", ("a", "b"): "A", ("a", "c"): "B",
    ("b", "a"): "A", ("b", "b"): "B", ("b", "c"): "C",
    ("c", "a"): "B", ("c", "b"): "C", ("c", "c"): "C",
}
_SUBIR = {"C": "B", "B": "A", "A": "A"}
_TOLERANCIA = {"a": 2.0, "b": 24.0, "c": None}

CRITICIDAD_LABELS = {
    "A": "A · crítico",
    "B": "B · importante",
    "C": "C · menor",
}


class RespuestasInvalidas(ValueError):
    """La encuesta llegó incompleta o con valores fuera de la escala."""


_ESCALAS = {
    "impacto": {"a", "b", "c"},
    "respaldo": {"a", "b", "c"},
    "reposicion": {"a", "b", "c"},
    "seguridad_ambiente": {"si", "no"},
}
# Un equipo de un grupo de redundancia responde solo la reposición: la
# consecuencia es del grupo y el respaldo sale de su margen.
PREGUNTAS_EN_GRUPO = ("reposicion",)


def validar_respuestas(respuestas: dict, claves: tuple = tuple(_ESCALAS)) -> dict:
    """Normaliza y valida las respuestas pedidas (por defecto, las cuatro).
    Levanta si falta alguna.

    El formulario no se puede guardar sin la encuesta completa (spec §4), así
    que acá no hay defaults: una respuesta ausente es un error, no una "c".
    """
    limpio = {}
    for clave in claves:
        validas = _ESCALAS[clave]
        valor = (respuestas.get(clave) or "").strip().lower()
        if valor not in validas:
            raise RespuestasInvalidas(f"Falta responder la pregunta «{clave}» de criticidad.")
        limpio[clave] = valor
    return limpio


def calcular_criticidad(respuestas: dict) -> tuple[str, Optional[float]]:
    """Devuelve (criticidad A/B/C, tolerancia en horas o None).

    - Base: tabla impacto × respaldo.
    - Reposición de más de una semana sube un nivel (C→B, B→A).
    - Riesgo a personas o ambiente fuerza A.
    - La tolerancia sale solo del impacto: 2 h, 24 h o sin definir.
    """
    r = validar_respuestas(respuestas)
    resultado = _MATRIZ[(r["impacto"], r["respaldo"])]
    if r["reposicion"] == "a":
        resultado = _SUBIR[resultado]
    if r["seguridad_ambiente"] == "si":
        resultado = "A"
    return resultado, _TOLERANCIA[r["impacto"]]


# ---------------------------------------------------------------------------
# Grupos de redundancia: margen y respaldo derivado
# ---------------------------------------------------------------------------
def es_noche(hora: float, desde: Optional[float], hasta: Optional[float]) -> bool:
    """Ventana nocturna que puede cruzar la medianoche (20:00 a 08:00)."""
    if desde is None or hasta is None:
        return False
    return (desde <= hora < hasta) if desde <= hasta else (hora >= desde or hora < hasta)


def necesarios_ahora(necesarios: int, necesarios_noche: Optional[int], noche: bool) -> int:
    """De noche rige `necesarios_noche` si está definido (los sopladores
    necesitan más de noche: el oxígeno baja sin fotosíntesis)."""
    return (necesarios_noche or necesarios) if noche else necesarios


def respaldo_por_margen(margen: int, conmutacion: str) -> str:
    """Q2 derivada del grupo: con al menos un equipo de sobra hay respaldo
    (manual o automático según el grupo); sin sobra, «ninguno»."""
    if margen >= 1:
        return "c" if conmutacion == "automatica" else "b"
    return "a"


def estado_margen(margen: int) -> str:
    """`holgado` (≥1 de sobra) · `sin_respaldo` (justo) · `insuficiente` (faltan)."""
    return "holgado" if margen >= 1 else ("sin_respaldo" if margen == 0 else "insuficiente")


# ---------------------------------------------------------------------------
# Códigos de equipo
# ---------------------------------------------------------------------------
def siguiente_codigo(codigos_existentes: list[str]) -> str:
    """EQ-001, EQ-002… Único para ambos sitios (va impreso en el QR).

    Ignora códigos que no sigan el patrón (p. ej. editados a mano) en vez de
    fallar: el correlativo sigue desde el mayor número válido.
    """
    mayor = 0
    for c in codigos_existentes:
        if c and c.upper().startswith("EQ-"):
            try:
                mayor = max(mayor, int(c[3:]))
            except ValueError:
                continue
    return f"EQ-{mayor + 1:03d}"


# ---------------------------------------------------------------------------
# Prioridad sugerida (spec §5.1)
# ---------------------------------------------------------------------------
CONDICIONES = {
    "detenido": "Detenido",
    "degradado": "Funciona con problemas",
    "anomalia": "Algo raro (sigue funcionando)",
}
RESPALDO_ENTRO = {"si": "Sí", "no": "No", "no_se": "No sé"}

# Criticidad × columna de condición. Columna 0: detenido sin respaldo
# operando; 1: detenido con respaldo operando, o degradado; 2: anomalía.
_PRIORIDAD = {
    "A": ("P1", "P2", "P3"),
    "B": ("P2", "P2", "P3"),
    "C": ("P3", "P3", "P3"),
}
PRIORIDADES = {"P1": "P1 · alarma", "P2": "P2 · en el día", "P3": "P3 · planificar"}


def prioridad_sugerida(criticidad: Optional[str], condicion: str, respaldo_entro: Optional[str]) -> str:
    """P1/P2/P3 según la criticidad y lo reportado.

    `criticidad` es la del **servicio sin respaldo** (la consecuencia de
    perderlo); el respaldo del momento entra por la columna. Así, si el que
    quedó solo falla, el aviso nace P1 aunque su criticidad nominal sea B.
    Quien llama pasa `respaldo_entro=None` cuando no hay respaldo disponible.

    "No sé" si entró el respaldo se trata como "no": ante la duda, el aviso
    se atiende como si el equipo estuviera solo. Un equipo sin criticidad (no
    debería existir: la encuesta es obligatoria) se trata como C.
    """
    if condicion == "detenido":
        col = 1 if respaldo_entro == "si" else 0
    elif condicion == "degradado":
        col = 1
    else:
        col = 2
    return _PRIORIDAD.get(criticidad or "C", _PRIORIDAD["C"])[col]


def prioridad_mas_alta(prioridades: list[str]) -> str:
    """P1 gana a P2 gana a P3 (el orden alfabético coincide)."""
    return min(p for p in prioridades if p) if any(prioridades) else "P3"


# ---------------------------------------------------------------------------
# Estados de la OT y tramos de tiempo (spec §5.2)
# ---------------------------------------------------------------------------
ESTADOS_OT = {
    "pendiente": "Pendiente",
    "espera_contratista": "Espera contratista",
    "espera_repuesto": "Espera repuesto",
    "pendiente_pago": "Pendiente de pago",
    "en_ejecucion": "En ejecución",
    "reparada": "Reparada (por verificar)",
    "cerrada": "Cerrada",
    "anulada": "Anulada",
}
# «Pendiente de pago»: el contratista no parte o no entrega hasta que se le
# pague la factura. Es una espera como la del repuesto: el equipo sigue
# detenido, y el tramo mide cuánto demora el pago (decisión 2026-09-30).
ESTADOS_ABIERTOS = ("pendiente", "espera_contratista", "espera_repuesto", "pendiente_pago",
                    "en_ejecucion", "reparada")
ESTADOS_FINALES = ("cerrada", "anulada")

# Cada estado dice quién tiene la pelota; el tramo es lo que acumula mientras
# la OT está en él. "aviso" es el tiempo entre la detección y la aceptación.
TRAMO_DE_ESTADO = {
    "aviso": "reaccion",
    "pendiente": "gestion",
    "espera_contratista": "contratista",
    "espera_repuesto": "repuesto",
    "pendiente_pago": "pago",
    "en_ejecucion": "ejecucion",
    "reparada": "verificacion",
}
TRAMOS = {
    "reaccion": "Reacción",
    "gestion": "Gestión",
    "contratista": "Contratista",
    "repuesto": "Repuesto",
    "pago": "Pago",
    "ejecucion": "Ejecución",
    "verificacion": "Verificación",
}


def transicion_valida(desde: str, hasta: str) -> bool:
    """Entre estados abiertos se puede ir y volver (ejecución → espera de
    repuesto → ejecución). Desde cualquier abierto se puede anular. Cerrar es
    un paso aparte (pide causa y acción), no un cambio de estado suelto. Las
    OT cerradas o anuladas no se reabren: una falla nueva es una OT nueva."""
    if desde in ESTADOS_FINALES or desde == hasta:
        return False
    if hasta == "anulada":
        return desde in ESTADOS_ABIERTOS
    return desde in ESTADOS_ABIERTOS and hasta in ESTADOS_ABIERTOS


def tramos(inicio, eventos: list, fin) -> dict:
    """Horas acumuladas por tramo.

    - `inicio`: detección de la falla (el reloj parte ahí, no al aceptar).
    - `eventos`: [(estado_hasta, ocurrido_at)] en orden; el primero es la
      aceptación, así que inicio → primer evento es la **reacción**.
    - `fin`: cierre de la OT, o ahora si sigue abierta.

    Un evento con hora anterior a la previa (hora mal digitada) aporta 0, no
    horas negativas. Devuelve {tramo: horas, ..., "total": horas}.
    """
    out = {t: 0.0 for t in TRAMOS}
    marcas = [("aviso", inicio)] + list(eventos)
    for i, (estado, desde) in enumerate(marcas):
        hasta = marcas[i + 1][1] if i + 1 < len(marcas) else fin
        tramo = TRAMO_DE_ESTADO.get(estado)
        if tramo and desde and hasta:
            out[tramo] += max(0.0, (hasta - desde).total_seconds() / 3600.0)
    out["total"] = sum(out[t] for t in TRAMOS)
    return out


def fuera_de_plazo(inicio, plazo_horas, fin) -> bool:
    """El plazo se cuenta desde la detección hasta que el equipo vuelve a
    servicio (o hasta ahora, si sigue abierta)."""
    if not inicio or plazo_horas is None or not fin:
        return False
    return (fin - inicio).total_seconds() / 3600.0 > float(plazo_horas)


def formato_duracion(horas: Optional[float]) -> str:
    """2.5 → '2 h 30 min' · 30 → '1 d 6 h' · 0.2 → '12 min'."""
    if horas is None:
        return "—"
    minutos = int(round(float(horas) * 60))
    if minutos < 60:
        return f"{minutos} min"
    h, m = divmod(minutos, 60)
    if h < 24:
        return f"{h} h {m:02d} min" if m else f"{h} h"
    d, h = divmod(h, 24)
    return f"{d} d {h} h" if h else f"{d} d"


def folio_aviso(i: int) -> str:
    return f"A-{i:04d}"


def folio_ot(i: int) -> str:
    return f"OT-{i:04d}"


# ---------------------------------------------------------------------------
# Resumen de fallas menores (spec §6.4)
# ---------------------------------------------------------------------------
VENTANA_RESUMEN_H = 2.0


def corte_vigente(hora: float, cortes: list[float], ventana: float = VENTANA_RESUMEN_H) -> Optional[float]:
    """El corte del resumen que corresponde enviar a esta hora, o None.

    Un corte (8,5 = 08:30) queda vigente durante `ventana` horas: si el
    servidor estuvo caído justo a esa hora, el resumen sale al volver, pero no
    horas más tarde (a esa altura ya no «resume» la mañana). Si hay varios
    vigentes, gana el más reciente.
    """
    vigentes = [c for c in cortes if c <= hora < c + ventana]
    return max(vigentes) if vigentes else None


def en_horario(dia_semana: int, hora: float, dias: Optional[str], desde, hasta) -> bool:
    """¿Está dentro del horario de la persona? (formato de mnt_personas:
    días 0 = lunes separados por coma, horas decimales). Sin horario = nunca:
    el resumen es para quien lo pidió con horario (guardar_persona lo exige)."""
    if not dias or desde is None or hasta is None:
        return False
    if str(dia_semana) not in [d.strip() for d in dias.split(",")]:
        return False
    lo, hi = float(desde), float(hasta)
    return (lo <= hora < hi) if lo <= hi else (hora >= lo or hora < hi)


def parse_cortes(valor: Optional[str]) -> list[float]:
    """«8:30, 14:00» → [8.5, 14.0]; ignora lo mal escrito."""
    out = []
    for x in (valor or "").split(","):
        try:
            h = parse_hora(x)
        except ValueError:
            continue
        if h is not None:
            out.append(h)
    return sorted(out)


# ---------------------------------------------------------------------------
# Destinos de un equipo (a qué atiende)
# ---------------------------------------------------------------------------
def normalizar_destinos(refs: list[str], unidad_de_estanque: dict[str, str]) -> list[tuple[str, str]]:
    """Limpia la lista de destinos marcados: `sitio:crianza`, `unit:<id>`, `pond:<id>`.

    - Descarta lo mal formado.
    - Si está el sitio completo, es lo único que queda (lo demás sobra).
    - Un estanque cuya unidad ya está marcada completa sobra.

    `unidad_de_estanque` mapea id de estanque → id de su unidad (texto).
    Devuelve [(tipo, id)] sin duplicados y en orden estable.
    """
    limpio = set()
    for r in refs or []:
        tipo, _, ident = (r or "").strip().partition(":")
        if tipo in ("sitio", "unit", "pond") and ident:
            limpio.add((tipo, ident))
    sitios = sorted(x for x in limpio if x[0] == "sitio")
    if sitios:
        return sitios
    unidades = {i for t, i in limpio if t == "unit"}
    estanques = {i for t, i in limpio if t == "pond" and unidad_de_estanque.get(i) not in unidades}
    orden = {"unit": 0, "pond": 1}
    return sorted([("unit", u) for u in unidades] + [("pond", p) for p in estanques],
                  key=lambda x: (orden[x[0]], x[1].zfill(12)))


_NUMERO_FINAL = re.compile(r"^(.*?)(\d+)\s*$")


def nombre_redundancia(nombre: str, existentes: list[str]) -> str:
    """Nombre para la redundancia de un equipo: sube el número final al
    siguiente libre ("Soplador 3" → "Soplador 4"; si "Soplador 4" ya existe,
    "Soplador 5"). Sin número final agrega " 2". Compara sin mayúsculas.
    """
    usados = {e.strip().lower() for e in existentes if e}
    m = _NUMERO_FINAL.match(nombre.strip())
    if m:
        base, n, ancho = m.group(1), int(m.group(2)), len(m.group(2))
    else:
        base, n, ancho = nombre.strip() + " ", 1, 1
    while True:
        n += 1
        # Respeta ceros a la izquierda: "Bomba 01" → "Bomba 02".
        candidato = f"{base}{n:0{ancho}d}"
        if candidato.lower() not in usados:
            return candidato


# ---------------------------------------------------------------------------
# Horario (formato de water_quality_alert_recipients)
# ---------------------------------------------------------------------------
DIAS_LABELS = ["Lu", "Ma", "Mi", "Ju", "Vi", "Sá", "Do"]


def parse_hora(valor: Optional[str]) -> Optional[float]:
    """'8:30' → 8.5 · '8,5' → 8.5 · '17' → 17.0 · '' → None. Levanta si no es hora."""
    if valor is None:
        return None
    s = str(valor).strip().replace(",", ".")
    if not s:
        return None
    if ":" in s:
        hh, mm = s.split(":", 1)
        h = int(hh) + int(mm) / 60.0
    else:
        h = float(s)
    if not 0 <= h <= 24:
        raise ValueError(f"Hora fuera de rango: {valor}")
    return round(h, 2)


def formato_hora(h) -> str:
    if h is None:
        return ""
    hh, mm = divmod(int(round(float(h) * 60)), 60)
    return f"{hh:02d}:{mm:02d}"
