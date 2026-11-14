"""一次性迁移：把通道配置并进插件配置（阶段 4 收口）。

背景：通道从来不是独立实体，只是「声明了 events 面的插件」。配置也该只有一份：
``~/.omniagent/plugins/<name>.yaml``。本脚本把旧 ``~/.omniagent/channels/<name>.yaml``
的字段原样并过去，**包括 ``enabled``**——旧文件若明确关闭该通道，迁移后必须仍关闭；
新路径已有同名字段时以新值为准（新源优先）。

用法（仓库根）：
    python scripts/migrate_plugin_configs.py --dry-run   # 只打印 diff
    python scripts/migrate_plugin_configs.py             # 打印 diff 后落盘

安全约定：
- 打印每份文件的 unified diff 再写；写前把目标文件备份成 ``<name>.yaml.bak-<日期>``；
- 旧文件不删除，重命名成 ``<name>.yaml.bak-<日期>``（随时可改名回退）；
- ``channels/routes.yaml`` 不迁：它是全局聊天路由表，不是任何插件的参数。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import difflib
import re
import sys
from pathlib import Path
from typing import Any, Dict

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from omni_core.local import runtime_paths as P  # noqa: E402

_LEGACY_NAME = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def _legacy_channel_config_file(name: str) -> Path:
    """旧通道配置路径解析只留在迁移脚本里；运行时不再认识这个路径。"""
    if not _LEGACY_NAME.fullmatch(name):
        raise ValueError(f"通道名不合法: {name!r}")
    return P.channels_root() / f"{name}.yaml"


def _load(path: Path) -> Dict[str, Any]:
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _dump(doc: Dict[str, Any]) -> str:
    import yaml

    return yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, default_flow_style=False)


def _diff(name: str, before: str, after: str) -> str:
    return "".join(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile=f"{name}.yaml(旧)", tofile=f"{name}.yaml(新)"))


def plan_one(name: str) -> Dict[str, Any]:
    legacy = _legacy_channel_config_file(name)
    target = P.plugin_config_file(name)
    old_doc = _load(legacy)
    merged: Dict[str, Any] = dict(old_doc)
    if target.is_file():
        merged.update(_load(target))                 # 新源优先，避免覆盖用户已改的值
    before = target.read_text(encoding="utf-8") if target.is_file() else ""
    after = _dump(merged)
    return {"name": name, "legacy": legacy, "target": target,
            "before": before, "after": after}


def main() -> int:
    ap = argparse.ArgumentParser(description="通道配置 → 插件配置（一次性）")
    ap.add_argument("--dry-run", action="store_true", help="只打印 diff，不落盘")
    args = ap.parse_args()

    root = P.channels_root()
    if not root.is_dir():
        print(f"[migrate] 没有旧配置目录 {root}（跳过）")
        return 0
    names = sorted(p.stem for p in root.glob("*.yaml") if p.stem != "routes")
    if not names:
        print("[migrate] 没有待合并的通道配置（跳过）")
        return 0

    date = _dt.date.today().strftime("%Y%m%d")
    changed = 0
    for name in names:
        item = plan_one(name)
        diff = _diff(name, item["before"], item["after"])
        print(f"\n=== {name}（保留 enabled，新路径优先）")
        print(diff or "(无差异：目标文件已是合并后内容)")
        if args.dry_run:
            continue
        target: Path = item["target"]
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_file():
            target.rename(target.with_name(target.name + f".bak-{date}"))
        target.write_text(item["after"], encoding="utf-8")
        legacy: Path = item["legacy"]
        legacy.rename(legacy.with_name(legacy.name + f".bak-{date}"))
        changed += 1
        print(f"[migrate] 已写入 {target}；旧文件 → {legacy.name}.bak-{date}")

    if args.dry_run:
        print("\n[migrate] dry-run：以上为将要落盘的内容")
    else:
        print(f"\n[migrate] 完成：{changed} 个通道配置并进插件配置")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
