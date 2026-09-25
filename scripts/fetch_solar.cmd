@echo off
REM Carga diaria de radiacion solar de Parral en solar_daily.
REM Lo llama la tarea programada Kenoz-Solar. Existe como .cmd para que la
REM tarea no tenga que anidar comillas: schtasks y PowerShell las rompen de
REM formas distintas y el error no se ve hasta que la tarea no corre.

cd /d "%~dp0.."

REM El DATABASE_URL del SO apunta a produccion_faena (PlantaApp). CrianzaApp
REM vive en fastapp_etapa1: hay que forzarlo o el script escribe en la base
REM equivocada.
set "DATABASE_URL=postgresql+psycopg://fastapp_user:fastapp_pass@localhost/fastapp_etapa1"
set "PYTHONPATH=."
set "PYTHONIOENCODING=utf-8"

.venv\Scripts\python.exe scripts\fetch_solar.py
exit /b %ERRORLEVEL%
