@echo off
rem ============================================================
rem  OmniAgent - start / stop backend + web dev server
rem
rem  Usage:
rem    start_all.bat          -> start backend + web
rem    start_all.bat backend  -> backend only
rem    start_all.bat web      -> web only
rem    start_all.bat stop     -> stop both (kills PIDs on :8000 / :5173)
rem
rem  Why the backend runs WITHOUT A WINDOW (do not "fix" this):
rem    On this machine, starting python.exe in a NEW CONSOLE WINDOW always
rem    dies with 0xc0000142 (STATUS_DLL_INIT_FAILED). Tried and all failed:
rem    relative paths, no nested quotes, launching python directly without
rem    cmd.exe. The backend itself is fine - it starts and binds :8000 when
rem    launched windowless (verified). So: launch hidden + redirect output.
rem      -> backend log: backend.log   (stdout)
rem      -> backend err: backend.err   (stderr)
rem
rem  Why no --reload: on Windows uvicorn --reload spawns a reloader AND a
rem    worker, both binding :8000 -> new connections randomly hang (10060)
rem    or get refused (10061).
rem  Why no "timeout /t N /nobreak >nul" and no trailing "pause": both break
rem    in some shells, or block the caller.
rem ============================================================
setlocal

set "PROJECT_DIR=%~dp0"
set "PYTHON=%PROJECT_DIR%.venv\Scripts\python.exe"
set "HOST=127.0.0.1"
set "PORT=8000"
set "MODE=%~1"

cd /d "%PROJECT_DIR%"

if /i "%MODE%"=="stop" goto :stop

if not exist "%PYTHON%" (
    echo [ERROR] venv not found: %PYTHON%
    echo         setup: python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
    exit /b 1
)

if /i "%MODE%"=="web" goto :start_web

echo [1/2] backend (windowless, logging to backend.log) -^> http://%HOST%:%PORT%
powershell -NoProfile -Command "Start-Process -FilePath '.venv\Scripts\python.exe' -ArgumentList '-m','uvicorn','backend.server:app','--host','%HOST%','--port','%PORT%' -WindowStyle Hidden -RedirectStandardOutput 'backend.log' -RedirectStandardError 'backend.err'"

if /i "%MODE%"=="backend" (
    echo        no window on purpose - check backend.log / backend.err
    goto :done
)

:start_web
if not exist "%PROJECT_DIR%web\node_modules" (
    echo [WARN] web\node_modules missing - run: cd web ^&^& npm install
)
echo [2/2] web -^> http://localhost:5173
start "OmniAgent Web" cmd /k "cd /d web && npm run dev"

:done
echo.
echo Done. Backend runs hidden (see backend.log^); web has its own window.
echo Stop everything with: start_all.bat stop
endlocal
goto :eof

:stop
echo Stopping anything listening on :%PORT% and :5173 ...
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":%PORT%" ^| findstr LISTENING') do taskkill /PID %%P /F
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":5173" ^| findstr LISTENING') do taskkill /PID %%P /F
echo Done.
endlocal
