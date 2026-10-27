@echo off
rem ============================================================
rem  OmniAgent - desktop packaging script (Windows)
rem  usage:
rem    pack_desktop.bat            -> build Web UI + package desktop app (NSIS installer + portable)
rem    pack_desktop.bat desktop    -> skip Web build, package desktop only (when web/dist is fresh)
rem    pack_desktop.bat web        -> build Web UI only (web/dist, served same-origin by backend)
rem  output: desktop\dist\OmniAgent Setup <ver>.exe + OmniAgent-Portable-<ver>.exe
rem          desktop\dist\win-unpacked\OmniAgent.exe  (unpacked, run directly to verify)
rem  note: the desktop shell does NOT bundle the frontend; at runtime the backend serves
rem        web/dist same-origin. Always build the Web UI before a release package.
rem ============================================================
setlocal
set "PROJECT_DIR=%~dp0"
set "MODE=%~1"

cd /d "%PROJECT_DIR%"

if /i "%MODE%"=="desktop" goto :desktop
if /i "%MODE%"=="web" goto :web
if not "%MODE%"=="" goto :usage

:web
echo [1/2] Building Web UI (web/dist) ...
if not exist "%PROJECT_DIR%web\node_modules" (
    echo [ERROR] web\node_modules missing. Run first: cd web ^&^& npm install
    exit /b 1
)
pushd web
call npm run build
if errorlevel 1 (
    echo [ERROR] Web UI build failed
    popd
    exit /b 1
)
popd
echo [1/2] Web UI build done.
if /i "%MODE%"=="web" goto :done

:desktop
echo [2/2] Packaging desktop app ...
if not exist "%PROJECT_DIR%desktop\node_modules" (
    echo [ERROR] desktop\node_modules missing. Run first: cd desktop ^&^& npm install
    exit /b 1
)
rem a running OmniAgent.exe locks dist\win-unpacked and breaks packaging -- warn early
tasklist /FI "IMAGENAME eq OmniAgent.exe" 2>nul | find /I "OmniAgent.exe" >nul
if not errorlevel 1 (
    echo [WARN] OmniAgent.exe is running; it may lock the output dir and fail packaging. Close it first.
)
pushd desktop
call npm run dist
if errorlevel 1 (
    echo [ERROR] Desktop packaging failed
    popd
    exit /b 1
)
popd

echo.
echo Packaging done. Output in desktop\dist\:
for %%F in ("desktop\dist\OmniAgent Setup*.exe" "desktop\dist\OmniAgent-Portable-*.exe") do (
    if exist "%%~fF" echo   %%~fF
)
goto :done

:usage
echo [ERROR] Usage: pack_desktop.bat [desktop^|web]. No arg = full package (Web UI + desktop).
exit /b 1

:done
echo Done.
