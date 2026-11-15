"""出厂初始化：把 examples 模板拷成一份可读的 ~/.omniagent 数据根。

行为约定：
- 逐份拷贝 examples/ 下的同名模板到数据根对应路径；**目标已存在一律跳过**，
  永不覆盖（升级 / 重跑都安全）。
- 拷入出厂角色卡 character.md（「小澄」：有性情的白纸，零关系零记忆预设），
  同样只在缺失时落盘。
- 结束时做一次出厂自检（全部目标就位即通过），并打印下一步指引。

用法：
    python scripts/setup_factory.py                 # 数据根 = ~/.omniagent
    python scripts/setup_factory.py --home <dir>    # 自定义数据根（等效 OMNIAGENT_HOME）
    OMNIAGENT_HOME=<dir> python scripts/setup_factory.py

本脚本只用标准库；依赖安装属于 venv 的事，不在这里做。
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = REPO_ROOT / "examples"

# (examples 内相对路径, 数据根内相对路径)
TEMPLATE_MAP: list[tuple[str, str]] = [
    ("models.json", "models.json"),
    ("config.yaml", "config.yaml"),
    ("mcp.json", "mcp.json"),
    ("voice_service.yaml", "voice_service.yaml"),
    ("character.md", "character.md"),
    ("channels/routes.yaml", "channels/routes.yaml"),
    ("cron/jobs.json", "cron/jobs.json"),
    ("plugins/bilibili.yaml", "plugins/bilibili.yaml"),
    ("plugins/feishu.yaml", "plugins/feishu.yaml"),
    ("plugins/napcat.yaml", "plugins/napcat.yaml"),
    ("plugins/voice.yaml", "plugins/voice.yaml"),
    ("plugins/wecom.yaml", "plugins/wecom.yaml"),
    ("plugins/xiaohongshu.yaml", "plugins/xiaohongshu.yaml"),
]


def resolve_home(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    env = os.environ.get("OMNIAGENT_HOME", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return Path.home() / ".omniagent"


def main() -> int:
    parser = argparse.ArgumentParser(description="OmniAgent 出厂初始化（拷模板，永不覆盖）")
    parser.add_argument("--home", default=None, help="数据根目录（默认 ~/.omniagent，可用 OMNIAGENT_HOME 覆盖）")
    args = parser.parse_args()

    home = resolve_home(args.home)
    print(f"数据根：{home}")

    created: list[str] = []
    kept: list[str] = []
    missing_src: list[str] = []
    for src_rel, dst_rel in TEMPLATE_MAP:
        src = EXAMPLES / src_rel
        dst = home / dst_rel
        if not src.exists():
            missing_src.append(src_rel)
            continue
        if dst.exists():
            kept.append(dst_rel)
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        created.append(dst_rel)

    if missing_src:
        print("!! 仓库模板缺失（examples 不完整，请确认在仓库根运行）：")
        for rel in missing_src:
            print(f"   - {rel}")
        return 2

    if created:
        print("已创建：")
        for rel in created:
            print(f"  + {rel}")
    if kept:
        print("已存在（跳过，未覆盖）：")
        for rel in kept:
            print(f"  = {rel}")

    # 出厂自检：全部目标就位
    absent = [dst_rel for _, dst_rel in TEMPLATE_MAP if not (home / dst_rel).exists()]
    if absent:
        print("!! 出厂自检未通过，以下文件仍缺失：")
        for rel in absent:
            print(f"   - {rel}")
        return 2
    print("出厂自检通过：模板齐了。")

    print(
        "\n下一步：\n"
        "  1) 起服务：.\\start_all.bat（后端 :8000 + 前端 :5173）\n"
        "  2) 打开 http://localhost:5173 → 设置 → 模型，填 provider 与 key\n"
        "     （或直接编辑 models.json：模板在 examples/models.json，逐键有注释）\n"
        "  3) 回到对话页，发第一条消息——她到家的第一天开始了\n"
        "\n可选能力（要用再装）：\n"
        "  - 语音：pip install -r requirements-voice.txt（模型另见 doc/plans/voice-tone-2026-10-31.md）\n"
        "  - 向量召回：python scripts/setup_embed_svc.py（独立 venv + bge-m3，支持 HF 镜像）\n"
        "  - 看图 / 桌面操作：pip install -r requirements-vision.txt\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
