@echo off
rem STEP 1 of 2 - One-time login (do this BEFORE starting the service).
rem A browser window opens - scan the QR with the Xiaohongshu app.
rem Do NOT log in to the same account on any other web client afterwards
rem (it kicks the service session). Then run scripts\xiaohongshu_mcp.bat.
chcp 65001 >nul
"%USERPROFILE%\.omniagent\tools\xiaohongshu-mcp\xiaohongshu-login.exe"
pause
