@echo off
REM 强制释放所有遗留的 llama-server.exe 进程（后端崩溃残留也能清）
REM 用法：双击运行，或命令行 python scripts/kill_llama.py
python "%~dp0kill_llama.py"
pause
