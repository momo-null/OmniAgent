@echo off
rem ============================================================
rem  OmniAgent - one-click start backend + frontend (Windows)
rem  usage:
rem    start_all.bat           -> start backend + frontend (2 windows)
rem    start_all.bat backend   -> backend only
rem    start_all.bat web       -> frontend only
rem    start_all.bat stop      -> stop both (kill :8000 / :5173)
rem ============================================================
setlocal
set "PROJECT_DIR=%~dp0"
set "PY=%PROJECT_DIR%.venv\Scripts\python.exe"
set "MODE=%~1"

cd /d "%PROJECT_DIR%"

if /i "%MODE%"=="stop" goto :stop

if not exist "%PY%" (
    echo [ERROR] venv missing: %PY%
    echo   run first: python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)
if not exist "%PROJECT_DIR%web\node_modules" (
    echo [WARN] web\node_modules missing, run first: cd web ^&^& npm install
)

if /i "%MODE%"=="backend" (
    echo starting backend window ...
    start "OmniAgent Backend" cmd /k ""%PY%" -m uvicorn backend.server:app --host 127.0.0.1 --port 8000"
    goto :done
)
if /i "%MODE%"=="web" (
    echo starting frontend window ...
    start "OmniAgent Web" cmd /k "cd /d web && npm run dev"
    goto :done
)

echo starting backend + frontend ...
start "OmniAgent Backend" cmd /k ""%PY%" -m uvicorn backend.server:app --host 127.0.0.1 --port 8000"
start "OmniAgent Web" cmd /k "cd /d web && npm run dev"
echo two windows opened: [OmniAgent Backend] [OmniAgent Web]
echo stop: start_all.bat stop

:done
pause
goto :eof

:stop
echo stopping :8000 / :5173 ...
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":8000" ^| findstr LISTENING') do taskkill /PID %%P /F
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":5173" ^| findstr LISTENING') do taskkill /PID %%P /F
echo done.
pause
