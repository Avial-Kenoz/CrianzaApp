"""Cliente mínimo de la API de Telegram para el bot de mantenimiento.

Mismo criterio que `app/services/notify_telegram.py` (urllib, UTF-8 explícito,
nunca levanta), pero con **su propio token** (`MNT_TELEGRAM_TOKEN`): es un bot
distinto del de calidad de agua (spec §6.1), y además de mandar tiene que
LEER mensajes (getUpdates), botones y archivos.

El token se lee en cada llamada, no al importar: una app arrancada sin token
sigue funcionando y el bot simplemente no arranca.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any, Optional

logger = logging.getLogger("mnt_telegram")

API = "https://api.telegram.org/bot{}/{}"
FILE_API = "https://api.telegram.org/file/bot{}/{}"
MAX_LEN = 4000
TIMEOUT_S = 15


def token() -> str:
    return (os.getenv("MNT_TELEGRAM_TOKEN") or "").strip()


def configurado() -> bool:
    return bool(token())


class TelegramError(Exception):
    pass


class Api:
    """Llamadas a Telegram. El bot recibe una instancia; las pruebas le pasan
    un doble con los mismos métodos, sin red."""

    def __init__(self, tk: Optional[str] = None):
        self.tk = tk or token()

    def _llamar(self, metodo: str, payload: dict, timeout: float = TIMEOUT_S) -> Any:
        if not self.tk:
            raise TelegramError("falta MNT_TELEGRAM_TOKEN en el entorno")
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(API.format(self.tk, metodo), data=data,
                                     headers={"Content-Type": "application/json; charset=utf-8"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                out = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detalle = ""
            try:
                detalle = json.loads(e.read().decode("utf-8")).get("description", "")
            except Exception:
                pass
            # 403: la persona bloqueó al bot o nunca le escribió.
            # 409: OTRO proceso está leyendo con este token (spec §6.1).
            raise TelegramError(f"HTTP {e.code} {detalle}"[:300]) from None
        except Exception as e:                       # red caída, DNS, timeout
            raise TelegramError(f"{type(e).__name__}: {e}"[:300]) from None
        if not out.get("ok"):
            raise TelegramError(str(out.get("description") or out)[:300])
        return out.get("result")

    # --- lectura ---------------------------------------------------------------
    def get_updates(self, offset: int, timeout: int = 25) -> list:
        return self._llamar("getUpdates", {
            "offset": offset, "timeout": timeout,
            "allowed_updates": ["message", "callback_query"],
        }, timeout=timeout + 10) or []

    def descargar(self, file_id: str) -> bytes:
        info = self._llamar("getFile", {"file_id": file_id})
        url = FILE_API.format(self.tk, info["file_path"])
        with urllib.request.urlopen(url, timeout=30) as resp:
            return resp.read()

    # --- escritura -------------------------------------------------------------
    def enviar(self, chat_id: str, texto: str, botones: Optional[list] = None) -> Optional[int]:
        """`botones`: filas de (texto, callback_data). Devuelve message_id."""
        payload = {"chat_id": chat_id, "disable_web_page_preview": True,
                   "text": texto if len(texto) <= MAX_LEN else texto[:MAX_LEN - 20] + "\n… (recortado)"}
        if botones:
            payload["reply_markup"] = {"inline_keyboard": [
                [{"text": t, "callback_data": d} for t, d in fila] for fila in botones]}
        res = self._llamar("sendMessage", payload)
        return (res or {}).get("message_id")

    def quitar_botones(self, chat_id: str, message_id: int, texto: Optional[str] = None) -> None:
        """Tras apretar un botón se quitan los de ese mensaje: evita dobles
        toques y deja en el chat la respuesta elegida."""
        try:
            if texto:
                self._llamar("editMessageText", {"chat_id": chat_id, "message_id": message_id,
                                                 "text": texto[:MAX_LEN]})
            else:
                self._llamar("editMessageReplyMarkup", {"chat_id": chat_id, "message_id": message_id,
                                                        "reply_markup": {"inline_keyboard": []}})
        except TelegramError:
            logger.debug("no se pudo editar el mensaje %s", message_id, exc_info=True)

    def responder_boton(self, callback_id: str, texto: Optional[str] = None) -> None:
        try:
            self._llamar("answerCallbackQuery", {"callback_query_id": callback_id, **({"text": texto} if texto else {})})
        except TelegramError:
            pass
