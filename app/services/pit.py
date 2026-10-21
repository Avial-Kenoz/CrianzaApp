"""Forma canónica de un PIT tag: la única regla para guardarlo y buscarlo.

El lector antepone ceros (0007CF009C ≡ 7CF009C, mismo valor hex del tag), así que
se comparan sin ellos. Los sufijos de reuso (_N, _R) no llevan ceros. Se trata
siempre como texto, nunca como número.

Antes había cuatro copias de esta regla (modelo, vistas, sexado y la API de
peces) y la de la API no quitaba los ceros: `confirm-faena-crotales`, que llama
PlantaApp al cerrar una faena, no habría encontrado un crotal escaneado con
ceros. Toda normalización de PIT pasa por aquí.
"""
from typing import Optional

from sqlalchemy import func


def canonical_pit(value: Optional[str]) -> str:
    """PIT en mayúsculas, sin espacios ni ceros a la izquierda; "" si viene vacío."""
    if value is None:
        return ""
    s = str(value).strip().upper()
    return s.lstrip("0") or s


def canonical_pit_column(column):
    """La misma regla del lado SQL, para comparar contra filas guardadas antes de
    que el modelo canonicalizara al escribir."""
    return func.upper(func.ltrim(func.trim(column), "0"))
