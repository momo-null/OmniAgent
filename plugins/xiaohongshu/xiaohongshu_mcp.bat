@echo off
rem STEP 2 of 2 - Start the xiaohongshu-mcp service (headless; MCP endpoint on
rem http://127.0.0.1:18060/mcp). Run xiaohongshu_login.bat FIRST (one time; same folder).
rem First service start may download a Chromium (~100MB) automatically - keep this
rem window open. Verify from repo root: python -m gateway.channels.xiaohongshu --login-status
chcp 65001 >nul
"%USERPROFILE%\.omniagent\tools\xiaohongshu-mcp\xiaohongshu-mcp.exe"
pause
