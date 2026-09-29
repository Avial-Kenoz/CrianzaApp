# -*- coding: utf-8 -*-
"""Muestra los chat_id de quienes le han escrito al bot de alertas.

Telegram no entrega el identificador de nadie que no haya iniciado la
conversacion: no se puede "agregar" a alguien desde el servidor. El flujo para
sumar a un encargado es siempre el mismo y este script cubre el paso 2:

  1. La persona abre @AcuicolaAlertasBot y aprieta Iniciar (o le escribe algo).
  2. Se corre este script, que lista quien escribio y con que chat_id.
  3. Ese numero se pega en Alertas -> Destinatarios, en la app.

Uso:
    set PYTHONPATH=.
    .venv\\Scripts\\python.exe scripts\\telegram_chat_id.py [--probar CHAT_ID]

`--probar` manda un mensaje de prueba a ese chat, que es la forma de confirmar
que el bot puede escribirle ANTES de ponerlo en la lista de destinatarios (si
la persona bloqueo al bot, el envio falla con 403).

Ojo: `getUpdates` devuelve solo lo reciente -- Telegram descarta los mensajes
sin leer despues de 24 h. Si alguien escribio hace dias y no aparece, que
escriba de nuevo.
"""
import argparse
import io
import json
import os
import sys
import urllib.request

sys.path.insert(0, ".")

from app.services import notify_telegram as tg          # noqa: E402

API = "https://api.telegram.org/bot{}/{}"

# El token vive en el server.env de PlantaAPP y lo inyecta start_server.cmd al
# proceso de uvicorn -- o sea que existe DENTRO del servidor y no en una
# terminal cualquiera. Sin este respaldo, el comando que documenta el tutorial
# falla con "falta TELEGRAM_BOT_TOKEN" aunque el canal este perfectamente
# configurado, que es la peor forma de fallar: parece un problema del bot.
SERVER_ENV = os.getenv("PLANTA_SERVER_ENV") or \
    r"C:\Users\admin-server\Desktop\PlantaAPP\server.env"


def _token_del_server_env() -> str:
    try:
        # utf-8-sig: el archivo viene con BOM.
        with io.open(SERVER_ENV, encoding="utf-8-sig") as f:
            for linea in f:
                linea = linea.strip()
                if linea.startswith("TELEGRAM_BOT_TOKEN="):
                    return linea.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _get(metodo, tk):
    with urllib.request.urlopen(API.format(tk, metodo), timeout=15) as r:
        return json.load(r)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--probar", metavar="CHAT_ID",
                    help="manda un mensaje de prueba a ese chat")
    args = ap.parse_args()

    tk = tg.token()
    if not tk:
        tk = _token_del_server_env()
        if tk:
            # Para que `tg.enviar` (--probar) lo encuentre igual que el servidor.
            os.environ["TELEGRAM_BOT_TOKEN"] = tk
    if not tk:
        print("ERROR: no se encontro TELEGRAM_BOT_TOKEN.")
        print("       Se busco en el entorno y en %s" % SERVER_ENV)
        print("       Si el archivo esta en otra ruta: set PLANTA_SERVER_ENV=<ruta>")
        return 1

    yo = _get("getMe", tk)
    if not yo.get("ok"):
        print("ERROR: el token no sirve:", yo.get("description"))
        return 1
    print("Bot: @%s" % yo["result"]["username"])

    if args.probar:
        ok, mid, err = tg.enviar(
            args.probar,
            "Prueba del canal de alertas de calidad de agua.\n"
            "Si lees esto, este chat puede recibir avisos.")
        print("Prueba a %s: %s" % (args.probar, "OK (mensaje %s)" % mid if ok else "FALLO -> %s" % err))
        return 0 if ok else 1

    out = _get("getUpdates", tk)
    chats = {}
    for u in out.get("result", []):
        msg = u.get("message") or u.get("edited_message") or {}
        chat = msg.get("chat") or {}
        if chat.get("id") is not None:
            nombre = " ".join(x for x in [chat.get("first_name"),
                                          chat.get("last_name")] if x)
            chats[chat["id"]] = (nombre or chat.get("title") or "?",
                                 chat.get("type"))
    if not chats:
        print("\nNadie le ha escrito al bot todavia (o ya pasaron las 24 h).")
        print("Pidele a la persona que abra el bot y apriete Iniciar, y volve a correr esto.")
        return 0

    print("\nQuienes pueden recibir avisos:\n")
    print("  %-16s %-30s %s" % ("CHAT_ID", "NOMBRE", "TIPO"))
    for cid, (nombre, tipo) in chats.items():
        print("  %-16s %-30s %s" % (cid, nombre, tipo))
    print("\nPega el CHAT_ID en Alertas -> Destinatarios:")
    print("  %s/views/ui/calidad-agua/alertas/destinatarios"
          % (os.getenv("CRIANZA_BASE_URL") or "http://192.168.1.201:8002"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
