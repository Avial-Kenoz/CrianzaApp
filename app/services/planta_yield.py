"""Factor de conversión biomasa→caviar, obtenido desde PlantaApp.

PlantaApp es el dueño del dato: expone los rendimientos productivos agregados
(biomasa→caviar bruto→envasado) en `POST /api/integration/v1/caviar-yield-factor`.
CrianzaApp lo consume para estimar cuánto caviar "guarda" en los estanques de
depuración, sin acoplar el render a que PlantaApp esté disponible:

  1. Si el último valor en caché (archivo JSON) es fresco (< TTL suave) se usa tal cual.
  2. Si está viejo/ausente se intenta refrescar contra PlantaApp con timeout corto.
     - Éxito con dato válido → se persiste y se devuelve el valor fresco.
     - Fallo/timeout/dato degenerado → se devuelve **el último bueno guardado**.
  3. Solo si nunca hubo un valor bueno se cae al constante por defecto.

Así el render casi nunca toca la red y nunca se rompe, y el fallback es el último
factor real observado, no un número fijo.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# --- Configuración (env con defaults sensatos) -----------------------------

PLANTA_YIELD_URL = (
    os.getenv("PLANTA_YIELD_URL")
    or "http://127.0.0.1:8000/api/integration/v1/caviar-yield-factor"
).strip()

INTEGRATION_SHARED_SECRET = (
    os.getenv("INTEGRATION_SHARED_SECRET") or "kenoz-integration-dev-secret-change-me"
).strip()
INTEGRATION_SOURCE_KEY = (os.getenv("INTEGRATION_SOURCE_KEY") or "crianzaapp").strip() or "crianzaapp"

# Ventana móvil de rendimiento (días) que se pide a PlantaApp.
YIELD_WINDOW_DAYS = int(os.getenv("CAVIAR_YIELD_WINDOW_DAYS") or "30")
# TTL suave: cuánto vale un valor en caché antes de intentar refrescar.
YIELD_TTL_SECONDS = int(os.getenv("CAVIAR_YIELD_TTL_SECONDS") or str(12 * 3600))
# Timeout corto para no bloquear el render si PlantaApp no responde.
YIELD_TIMEOUT_SECONDS = float(os.getenv("CAVIAR_YIELD_TIMEOUT_SECONDS") or "4")
# Fallback fijo (fracción biomasa→envasado) solo si nunca hubo un valor bueno.
YIELD_FACTOR_DEFAULT = float(os.getenv("CAVIAR_YIELD_FACTOR_DEFAULT") or "0.166")

_CACHE_FILE = Path(
    os.getenv("CAVIAR_YIELD_CACHE_FILE")
    or (Path(__file__).resolve().parent.parent / "data" / "caviar_yield_factor.json")
)

_lock = threading.Lock()


def _now_epoch() -> float:
    return time.time()


def _load_cache() -> Optional[dict]:
    try:
        with _CACHE_FILE.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and data.get("biomasa_to_envasado_pct"):
            return data
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    return None


def _save_cache(payload: dict) -> None:
    try:
        _CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _CACHE_FILE.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(tmp, _CACHE_FILE)  # escritura atómica
    except OSError:
        # La caché es best-effort; si no se puede escribir seguimos con el valor en memoria.
        pass


def _fetch_from_planta(window_days: int) -> Optional[dict]:
    """POST firmado a PlantaApp. Devuelve el JSON de respuesta o None ante cualquier fallo."""
    if not INTEGRATION_SHARED_SECRET or not PLANTA_YIELD_URL:
        return None

    body_bytes = json.dumps(
        {"window_days": window_days}, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    ts = str(int(_now_epoch()))
    nonce = secrets.token_hex(16)
    signed_message = f"{ts}.{nonce}.".encode("utf-8") + body_bytes
    signature = hmac.new(
        INTEGRATION_SHARED_SECRET.encode("utf-8"), signed_message, hashlib.sha256
    ).hexdigest()

    req = urllib.request.Request(
        PLANTA_YIELD_URL,
        data=body_bytes,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Integration-Key": INTEGRATION_SOURCE_KEY,
            "X-Integration-Timestamp": ts,
            "X-Integration-Nonce": nonce,
            "X-Integration-Signature": signature,
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=YIELD_TIMEOUT_SECONDS) as resp:
            if not (200 <= int(resp.status) < 300):
                return None
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError, OSError):
        return None

    # Descartar respuestas degeneradas (sin datos en la ventana) para no pisar un buen valor.
    if not isinstance(data, dict):
        return None
    try:
        pct = float(data.get("biomasa_to_envasado_pct") or 0.0)
    except (TypeError, ValueError):
        return None
    if pct <= 0:
        return None

    data["fetched_at_epoch"] = _now_epoch()
    data["fetched_at"] = datetime.now(timezone.utc).isoformat()
    return data


def _decorate(payload: dict, origin: str, stale: bool) -> dict:
    pct = float(payload.get("biomasa_to_envasado_pct") or 0.0)
    return {
        "factor": pct / 100.0,
        "biomasa_to_envasado_pct": pct,
        "biomasa_to_caviar_bruto_pct": payload.get("biomasa_to_caviar_bruto_pct"),
        "caviar_bruto_to_envasado_pct": payload.get("caviar_bruto_to_envasado_pct"),
        "window_days": payload.get("window_days", YIELD_WINDOW_DAYS),
        "source_computed_at": payload.get("computed_at"),
        "fetched_at": payload.get("fetched_at"),
        "origin": origin,   # "live" | "cache" | "default"
        "stale": stale,     # True si no se pudo refrescar y se usó caché/default
    }


def _default_factor() -> dict:
    return {
        "factor": YIELD_FACTOR_DEFAULT,
        "biomasa_to_envasado_pct": round(YIELD_FACTOR_DEFAULT * 100.0, 2),
        "biomasa_to_caviar_bruto_pct": None,
        "caviar_bruto_to_envasado_pct": None,
        "window_days": YIELD_WINDOW_DAYS,
        "source_computed_at": None,
        "fetched_at": None,
        "origin": "default",
        "stale": True,
    }


def get_caviar_yield_factor(window_days: int = YIELD_WINDOW_DAYS) -> dict:
    """Devuelve el factor biomasa→caviar envasado con su procedencia.

    Nunca lanza: ante cualquier problema devuelve el último valor bueno guardado
    o, en su defecto, el constante por defecto.
    """
    with _lock:
        cached = _load_cache()

        if cached:
            age = _now_epoch() - float(cached.get("fetched_at_epoch") or 0.0)
            if age < YIELD_TTL_SECONDS:
                return _decorate(cached, origin="cache", stale=False)

        fresh = _fetch_from_planta(window_days)
        if fresh:
            _save_cache(fresh)
            return _decorate(fresh, origin="live", stale=False)

        if cached:
            # No se pudo refrescar: usamos el último bueno que teníamos.
            return _decorate(cached, origin="cache", stale=True)

    return _default_factor()
