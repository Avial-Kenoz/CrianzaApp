"""Resincroniza los flags derivados de los muestreos de biofiltro guardados.

Los muestreos persisten los flags calculados al momento de ingresarlos. Si
cambian las especificaciones de test, el factor k o los umbrales, los
muestreos viejos NO se recalculan solos. Este script vuelve a evaluar cada
muestreo con la configuración vigente (umbrales + specs de test en la BD) y
actualiza los campos derivados donde difieran.

Uso (desde la raíz, apuntando a la BD de CrianzaApp):
    DATABASE_URL=postgresql://fastapp_user:fastapp_pass@localhost/fastapp_etapa1 \
        ./.venv/Scripts/python.exe scripts/recompute_biofilter_flags.py
Agregar --apply para escribir los cambios (sin él, solo muestra el diff).
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.session import SessionLocal  # noqa: E402
from app.models.biofilter_readings import BiofilterReading  # noqa: E402
from app.api.water_quality import load_thresholds, load_test_specs  # noqa: E402
from app.services import water_quality as wq  # noqa: E402


def _f(x):
    return float(x) if x is not None else None


def main(apply: bool) -> int:
    db = SessionLocal()
    try:
        th = load_thresholds(db)
        specs = load_test_specs(db)
        rows = db.query(BiofilterReading).order_by(BiofilterReading.id).all()
        changed = 0
        for r in rows:
            reading = {
                "in_ph": _f(r.in_ph), "in_temp_c": _f(r.in_temp_c),
                "in_nh4_n": _f(r.in_nh4_n), "in_no2_n": _f(r.in_no2_n), "in_no3_n": _f(r.in_no3_n),
                "out_ph": _f(r.out_ph), "out_temp_c": _f(r.out_temp_c),
                "out_nh4_n": _f(r.out_nh4_n), "out_no2_n": _f(r.out_no2_n), "out_no3_n": _f(r.out_no3_n),
            }
            res = wq.evaluate_biofilter(reading, thresholds=th, test_specs=specs)
            diffs = []
            for attr, new in (("n_balance_flag", res.n_balance_flag),
                              ("ph_delta_flag", res.ph_delta_flag),
                              ("temp_delta_flag", res.temp_delta_flag),
                              ("alarm_level", res.alarm_level)):
                if getattr(r, attr) != new:
                    diffs.append(f"{attr}: {getattr(r, attr)} -> {new}")
                    if apply:
                        setattr(r, attr, new)
            if diffs:
                changed += 1
                print(f"id {r.id} ({r.reading_date}): " + "; ".join(diffs))
        if apply:
            db.commit()
        print(f"\n{'Actualizados' if apply else 'Con cambios (dry-run)'}: {changed} de {len(rows)}")
        if not apply and changed:
            print("Ejecuta con --apply para persistir.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main("--apply" in sys.argv))
