"""强制释放所有 llama-server.exe 进程（无需后端存活）

后端异常退出后，llama-server 仍以独立进程组残留、占用显存。此脚本
直接按进程名杀掉所有 llama-server.exe，用于「一键释放」的兜底手段。

用法:
    python scripts/kill_llama.py
"""
import sys

import psutil

LLAMA_SERVER_EXE = "llama-server.exe"


def main() -> int:
    killed = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            if (proc.info.get("name") or "").lower() == LLAMA_SERVER_EXE:
                pid = proc.info.get("pid")
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
                killed.append(pid)
        except Exception as e:  # 进程可能已退出
            print(f"  跳过 pid={proc.info.get('pid')}: {e}", file=sys.stderr)

    if killed:
        print(f"已释放 {len(killed)} 个 llama-server 进程: {killed}")
        return 0
    print("未发现运行中的 llama-server 进程。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
