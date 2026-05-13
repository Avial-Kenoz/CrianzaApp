@echo off
setlocal

echo ============================================================
echo  CRIANZAAPP - ENTORNO DEV ^(puerto 8003^)
echo  Base de datos: fastapp_etapa1
echo  NO afecta produccion
echo ============================================================

set "CRIANZA_ROOT=C:\Users\admin-server\Desktop\CrianzaApp"
set "CRIANZA_DB_URL=postgresql://fastapp_user:fastapp_pass@localhost:5432/fastapp_etapa1"

if not exist "%CRIANZA_ROOT%\.venv\Scripts\uvicorn.exe" (
    echo [ERROR] No existe uvicorn en "%CRIANZA_ROOT%\.venv\Scripts\uvicorn.exe"
    goto :end
)

cd /d "%CRIANZA_ROOT%"
set "DATABASE_URL=%CRIANZA_DB_URL%"
.venv\Scripts\uvicorn.exe app.main:app --host 0.0.0.0 --port 8003

:end
endlocal
