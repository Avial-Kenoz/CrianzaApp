"""Parsers de campos de formulario: coma decimal, vacío y basura → None.

Había una copia por módulo (calidad de agua, ensilaje, alertas, reproducción,
vistas) con diferencias de borde que nadie había elegido. Aquí quedan explícitas.
"""
from decimal import Decimal, InvalidOperation
from typing import Any, Optional


def parse_decimal(value: Any) -> Optional[Decimal]:
    """Decimal tolerando coma decimal y espacios; None si viene vacío o no es número."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    raw = str(value).strip().replace(",", ".")
    if raw == "":
        return None
    try:
        return Decimal(raw)
    except (InvalidOperation, ValueError):
        return None


def parse_float(value: Any) -> Optional[float]:
    d = parse_decimal(value)
    return None if d is None else float(d)


def parse_int(value: Any, truncate: bool = False) -> Optional[int]:
    """Entero o None. Con `truncate`, "12,7" → 12 (campos que el operador escribe
    con decimales por costumbre); sin él, un decimal no es un entero válido."""
    if truncate:
        d = parse_decimal(value)
        return None if d is None or not d.is_finite() else int(d)
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def require_decimal(value: Optional[str], field_name: str) -> Optional[Decimal]:
    """Como `parse_decimal`, pero un valor escrito que no es número es un error
    que se le muestra al usuario (vacío sigue siendo None)."""
    if value is None or str(value).strip() == "":
        return None
    d = parse_decimal(value)
    if d is None:
        raise ValueError(f"{field_name} no es numérico.")
    return d
