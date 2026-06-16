"""
Cierra las sesiones de muestreo que quedaron abiertas (closed_at IS NULL).

Las 10 sesiones tienen datos reales pero nunca se cerraron. Al cerrarlas:
- pond_lot_stats recibe sampled_at = session.registry_date (fecha correcta)
- pond.biomass_current se recalcula para esos estanques
- lot.biomass_current se actualiza
- Se guarda un checkpoint 'sampling' por estanque

Uso:
    cd C:/Users/admin-server/Desktop/CrianzaApp
    python -m scripts.close_open_sessions
"""
import sys, os
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(_ROOT, ".env"), override=True)

from app.db.session import SessionLocal
from app.models.sampling_sessions import SamplingSession
from app.api.views import _close_sampling_session

SESSION_IDS = [2398, 2399, 2402, 2404, 2405, 2407, 2408, 2409, 2411, 2412]


def run():
    db = SessionLocal()
    try:
        for sid in SESSION_IDS:
            session = db.query(SamplingSession).filter(SamplingSession.id == sid).first()
            if not session:
                print(f"  SKIP {sid}: no encontrada")
                continue
            if session.closed_at is not None:
                print(f"  SKIP {sid}: ya estaba cerrada ({session.closed_at})")
                continue

            print(f"  Cerrando sesion {sid} (pond_id={session.pond_id}, registry_date={session.registry_date})...", end=" ")
            _close_sampling_session(session, db)
            db.commit()
            print("OK")

        print("\nListo. Verifica los valores en existencia_lote y existencia_estanque.")

    except Exception as exc:
        db.rollback()
        print(f"\nERROR: {exc}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    run()
