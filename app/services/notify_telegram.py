"""Envío de mensajes por Telegram. Lo más delgado posible.

Sin dependencias nuevas: `urllib`, igual que `scripts/fetch_solar.py`. Todo lo
que sabe de alertas vive en `wq_notify`; acá sólo se manda texto a un chat y se
devuelve qué pasó.

⚠️ **El cuerpo se codifica en UTF-8 explícitamente.** No es decorativo: el texto
lleva “O₂”, “°C” y acentos, y si el encoding queda a merced del entorno,
Telegram responde `400 Bad Request: strings must be encoded in UTF-8` y el aviso
no sale. Pasó en la primera prueba de este bot.

El token sale del entorno (`TELEGRAM_BOT_TOKEN`, que `start_server.cmd` inyecta
desde `PlantaAPP/server.env`) y se lee en cada llamada, no al importar: así
configurarlo no obliga a reiniciar más de una vez, y una app arrancada sin token
puede avisar que le falta en vez de fallar al importar.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional, Tuple

logger = logging.getLogger("notify_telegram")

API = "https://api.telegram.org/bot{}/{}"
TIMEOUT_S = 10
MAX_LEN = 4000          # el límite de Telegram son 4096; se deja aire


def token() -> str:
    return (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()


def configurado() -> bool:
    return bool(token())


def enviar(chat_id: str, texto: str) -> Tuple[bool, Optional[int], Optional[str]]:
    """Manda un mensaje. Devuelve (ok, message_id, error).

    Nunca levanta: el que llama está en medio de un job o de un request y un
    problema de red no puede voltearlo. El error vuelve como texto para quedar
    guardado en la bitácora, que es donde sirve.
    """
    tk = token()
    if not tk:
        return False, None, "falta TELEGRAM_BOT_TOKEN en el entorno"
    if not chat_id:
        return False, None, "destinatario sin chat_id"

    cuerpo = texto if len(texto) <= MAX_LEN else (texto[:MAX_LEN - 20] + "\n… (recortado)")
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": cuerpo,
        "disable_web_page_preview": "true",
    }).encode("utf-8")

    try:
        req = urllib.request.Request(API.format(tk, "sendMessage"), data=data)
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            out = json.loads(resp.read().decode("utf-8"))
        if not out.get("ok"):
            return False, None, str(out.get("description") or out)[:300]
        return True, (out.get("result") or {}).get("message_id"), None
    except urllib.error.HTTPError as e:
        detalle = ""
        try:
            detalle = json.loads(e.read().decode("utf-8")).get("description", "")
        except Exception:
            pass
        # 403 es el caso cotidiano y conviene que se lea claro en la bitácora:
        # la persona bloqueó al bot o nunca inició la conversación.
        return False, None, "HTTP {} {}".format(e.code, detalle)[:300]
    except Exception as e:                      # red caída, DNS, timeout
        return False, None, "{}: {}".format(type(e).__name__, e)[:300]


def actualizar(chat_id: str, message_id: int, texto: str) -> bool:
    """Reescribe un mensaje ya enviado (para tachar lo que se resolvió).

    Telegram deja editar sin límite de tiempo los mensajes propios del bot, y
    editar es mejor que mandar uno nuevo: el hilo no crece con "ya está ok".
    """
    tk = token()
    if not tk or not message_id:
        return False
    data = urllib.parse.urlencode({
        "chat_id": chat_id, "message_id": message_id,
        "text": texto if len(texto) <= MAX_LEN else texto[:MAX_LEN],
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    try:
        req = urllib.request.Request(API.format(tk, "editMessageText"), data=data)
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            return bool(json.loads(resp.read().decode("utf-8")).get("ok"))
    except Exception:
        logger.debug("no se pudo editar el mensaje %s", message_id, exc_info=True)
        return False
