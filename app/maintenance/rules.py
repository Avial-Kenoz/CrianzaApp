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
REGLA_CRITICIDAD_VERSION = "v1"

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
PREGUNTA_RESPALDO = "¿Tiene respaldo?"
OPCIONES_RESPALDO = [("a", "Ninguno"), ("b", "Manual (alguien lo tiene que conectar)"),
                     ("c", "Automático o redundante")]
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


def validar_respuestas(respuestas: dict) -> dict:
    """Normaliza y valida las cuatro respuestas. Levanta si falta alguna.

    El formulario no se puede guardar sin la encuesta completa (spec §4), así
    que acá no hay defaults: una respuesta ausente es un error, no una "c".
    """
    limpio = {}
    for clave, validas in (
        ("impacto", {"a", "b", "c"}),
        ("respaldo", {"a", "b", "c"}),
        ("reposicion", {"a", "b", "c"}),
        ("seguridad_ambiente", {"si", "no"}),
    ):
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
