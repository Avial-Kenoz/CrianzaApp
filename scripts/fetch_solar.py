# -*- coding: utf-8 -*-
"""Carga la radiacion solar diaria de Parral en `solar_daily`.

Dos fuentes de Open-Meteo, gratis y sin API key:

  - archivo  (reanalisis ERA5, dias ya ocurridos)  -> definitivo
  - forecast (modelo, hasta 16 dias)               -> provisorio

Cada corrida hace lo mismo: pide el pronostico de los proximos dias y
rellena los dias pasados que falten o que sigan marcados como pronostico.
Asi un dia entra como estimacion y se corrige solo cuando pasa.

Se ejecuta a diario por tarea programada (patron Kenoz-*). Si no hay salida a
internet no rompe nada: los dias ya guardados siguen sirviendo y el calculo
que los usa degrada a "sin pronostico", igual que cualquier otro dato que
falta.

Uso:
    set PYTHONPATH=.
    .venv\\Scripts\\python.exe scripts\\fetch_solar.py [--backfill AAAA-MM-DD]
"""
import argparse
import json
import sys
import urllib.request
from datetime import date, timedelta

sys.path.insert(0, ".")
from app.db.session import SessionLocal          # noqa: E402
from app.models.solar_daily import SolarDaily    # noqa: E402

# Parral, VII region. La elevacion que devuelve Open-Meteo para este punto es
# 171 m, contra los 170 que usamos como constante del sitio: coincide.
LAT, LON = -36.143, -71.826
TZ = "America/Santiago"
DAILY = "shortwave_radiation_sum,sunshine_duration,cloud_cover_mean,temperature_2m_max,temperature_2m_min"
FORECAST_DAYS = 10
TIMEOUT = 30


def _get(url):
    with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_forecast():
    url = (f"https://api.open-meteo.com/v1/forecast?latitude={LAT}&longitude={LON}"
           f"&daily={DAILY}&timezone={TZ}&forecast_days={FORECAST_DAYS}")
    return _get(url), True


def fetch_archive(desde, hasta):
    url = (f"https://archive-api.open-meteo.com/v1/archive?latitude={LAT}&longitude={LON}"
           f"&daily={DAILY}&timezone={TZ}&start_date={desde}&end_date={hasta}")
    return _get(url), False


def upsert(db, payload, es_pronostico):
    d = payload.get("daily") or {}
    dias = d.get("time") or []
    n = 0
    for i, f in enumerate(dias):
        dia = date.fromisoformat(f)
        def val(campo, factor=1.0):
            serie = d.get(campo) or []
            v = serie[i] if i < len(serie) else None
            return None if v is None else float(v) * factor
        fila = db.get(SolarDaily, dia)
        # Un archivo nunca se pisa con un pronostico: es al reves.
        if fila is not None and not fila.is_forecast and es_pronostico:
            continue
        if fila is None:
            fila = SolarDaily(day=dia)
            db.add(fila)
        fila.ghi_mj_m2 = val("shortwave_radiation_sum")
        fila.sunshine_hours = val("sunshine_duration", 1.0 / 3600.0)
        fila.cloud_cover_pct = val("cloud_cover_mean")
        fila.temp_max_c = val("temperature_2m_max")
        fila.temp_min_c = val("temperature_2m_min")
        fila.is_forecast = es_pronostico
        fila.source = "open-meteo"
        n += 1
    db.commit()
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", help="fecha inicial para traer del archivo")
    args = ap.parse_args()

    db = SessionLocal()
    try:
        total = 0
        # 1) Archivo: lo pedido explicitamente, o los dias pasados que siguen
        #    guardados como pronostico (hay que corregirlos).
        ayer = date.today() - timedelta(days=1)
        if args.backfill:
            desde = date.fromisoformat(args.backfill)
        else:
            pend = (db.query(SolarDaily.day)
                    .filter(SolarDaily.day <= ayer,
                            SolarDaily.is_forecast.is_(True))
                    .order_by(SolarDaily.day).first())
            desde = pend[0] if pend else None
        if desde and desde <= ayer:
            payload, _ = fetch_archive(desde, ayer)
            n = upsert(db, payload, False)
            total += n
            print("archivo  %s -> %s : %d dias" % (desde, ayer, n))

        # 2) Pronostico de los proximos dias.
        payload, _ = fetch_forecast()
        n = upsert(db, payload, True)
        total += n
        print("pronostico: %d dias" % n)

        hoy = db.get(SolarDaily, date.today())
        if hoy and hoy.ghi_mj_m2 is not None:
            print("hoy: %.1f MJ/m2, %s%% de nubes" % (
                float(hoy.ghi_mj_m2),
                ("%.0f" % float(hoy.cloud_cover_pct)) if hoy.cloud_cover_pct else "?"))
        print("total escrito: %d" % total)
    finally:
        db.close()


if __name__ == "__main__":
    main()
