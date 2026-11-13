#!/usr/bin/env python3
"""自取观测审计：从 ``memory_recall-YYYY-MM.jsonl`` 里筛出「谁在什么时候翻了记忆」。

观测本身每 run 一行（`memory_tool.log_recall_run` 落盘，见 §8.3），本脚本只读，
不改任何状态。默认只看**群聊里发生过检索的轮次**——那几条才是「有人在试探 / 她主动
去翻旧账」的信号，其余轮次她只是在接话，不用盯。

用法::

    python scripts/recall_audit.py                 # 本月，群聊，有检索的轮次
    python scripts/recall_audit.py --days 2        # 只看最近两天
    python scripts/recall_audit.py --all           # 连私聊/Web 一起看
    python scripts/recall_audit.py --peer <uid>
    python scripts/recall_audit.py --month 2026-11 --json

退出码恒 0（审计是看板，不是闸）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List


def _obs_path(month: str) -> str:
    root = os.path.join(os.path.expanduser("~"), ".omniagent", "memory")
    return os.path.join(root, f"memory_recall-{month}.jsonl")


def _local(ts: str) -> str:
    try:
        return datetime.fromisoformat(str(ts)).astimezone().strftime("%m-%d %H:%M")
    except Exception:
        return str(ts or "")[:16]


def _load(path: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not os.path.exists(path):
        return rows
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if isinstance(rec, dict):
                rows.append(rec)
    return rows


def _tools(rec: Dict[str, Any]) -> str:
    by = rec.get("by_tool") or {}
    if not isinstance(by, dict) or not by:
        return "-"
    return ", ".join(f"{k}×{v}" for k, v in sorted(by.items(), key=lambda kv: -int(kv[1] or 0)))


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="记忆自取观测审计（只读）")
    ap.add_argument("--month", default=datetime.now().strftime("%Y-%m"),
                    help="观测分片月份 YYYY-MM（缺省本月）")
    ap.add_argument("--path", default="", help="直接指定 jsonl 路径（覆盖 --month）")
    ap.add_argument("--days", type=int, default=0, help="只看最近 N 天（0=不限）")
    ap.add_argument("--all", action="store_true", help="包含私聊 / Web 场合")
    ap.add_argument("--peer", default="", help="只看发言人包含该子串的记录")
    ap.add_argument("--json", action="store_true", help="原样输出 JSON 行")
    args = ap.parse_args(argv)

    path = args.path or _obs_path(args.month)
    rows = _load(path)
    if not rows:
        print(f"没有观测记录：{path}")
        return 0

    since = None
    if args.days and args.days > 0:
        since = datetime.now(timezone.utc) - timedelta(days=args.days)

    picked: List[Dict[str, Any]] = []
    for rec in rows:
        if not rec.get("any_fetch") and not int(rec.get("calls") or 0):
            continue
        ck = str(rec.get("context_key") or "")
        if not args.all and not ck.startswith("group:"):
            continue
        if args.peer and args.peer not in str(rec.get("peer") or ""):
            continue
        if since is not None:
            try:
                if datetime.fromisoformat(str(rec.get("ts") or "")) < since:
                    continue
            except Exception:
                continue
        picked.append(rec)

    if args.json:
        for rec in picked:
            print(json.dumps(rec, ensure_ascii=False))
        return 0

    if not picked:
        scope = "全部场合" if args.all else "群聊"
        print(f"{scope}内没有检索行为（{path}，共 {len(rows)} 个 run）")
        return 0

    print(f"# {path}")
    print(f"# {len(picked)}/{len(rows)} 个 run 有检索（{'全部场合' if args.all else '仅群聊'}）\n")
    print(f"{'时间':<12} {'场合':<20} {'发言人':<22} {'工具':<44} 命中 拦截")
    print("-" * 116)
    for rec in picked:
        print(f"{_local(rec.get('ts')):<12} "
              f"{str(rec.get('context_key') or '')[:19]:<20} "
              f"{str(rec.get('peer') or '')[:21]:<22} "
              f"{_tools(rec)[:43]:<44} "
              f"{int(rec.get('hits') or 0):>4} {int(rec.get('quota_blocked') or 0):>4}")

    # 按发言人聚合：谁最常在群聊里让她翻记忆——这就是试探信号
    per_peer: Dict[str, int] = {}
    blocked = 0
    for rec in picked:
        per_peer[str(rec.get("peer") or "(空)")] = per_peer.get(str(rec.get("peer") or "(空)"), 0) + 1
        blocked += int(rec.get("quota_blocked") or 0)
    print("\n## 按发言人")
    for who, n in sorted(per_peer.items(), key=lambda kv: -kv[1]):
        print(f"  {n:>3} 次  {who}")
    if blocked:
        print(f"\n## 配额拦截 {blocked} 次——她这轮被额度卡住了（不是「没有记录」）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
