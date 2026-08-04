from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
import os
from dotenv import load_dotenv

# BASE DE DATOS DE CRIANZAAPP = fastapp_etapa1 (usuario fastapp_user). Ver CLAUDE.md.
# OJO: load_dotenv() NO pisa variables ya definidas en el SO. El SO tiene
# DATABASE_URL=produccion_faena (PlantaApp), que GANA sobre el .env de abajo.
# Por eso los scripts deben FORZAR DATABASE_URL=...fastapp_etapa1 (el servidor de
# producción ya lo hace vía start_server.cmd: set DATABASE_URL=%CRIANZA_DB_URL%).
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://user:password@localhost/fastapp_etapa1")

if DATABASE_URL.startswith("postgresql://") and "+" not in DATABASE_URL.split("://", 1)[0]:
	DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()
