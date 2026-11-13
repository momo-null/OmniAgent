@echo off
rem Bilibili channel QR login (double-click me).
rem Scans with the Bilibili app; credentials are saved to %USERPROFILE%\.omniagent\bilibili_credentials.json
rem After login, restart the gateway so polling starts.
chcp 65001 >nul
cd /d "%~dp0..\.."
python -m gateway.channels.bilibili --login
echo.
pause
