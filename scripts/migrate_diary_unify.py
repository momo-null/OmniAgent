"""一次性迁移：把「按项目一天多篇」的日记/月记合并为全局「一天一篇」（plan §11 H-4）。

背景：旧实现每个当日有活动的项目各写一份 ``<pid>/memory/scene_blocks/diary-<日期>.md``，
把她的一天切成几个互不知情的分身。统一后落点是全局 ``~/.omniagent/memory/diary/``，
``doc_id`` 按日期唯一（裸文件名），场合只作内容标注。

做法（**不丢任何正文、不删任何文件**）：

1. 按文件名分组，同名的多篇合并成一篇：META 取最早 created / 最晚 updated、
   topics·chat_ids·peers 去重合并，正文完整保留并用分隔标记注明来源项目；
2. 旧文件**移动**到 ``~/.omniagent/memory/archive/diary-unify-<时间戳>/<pid>/`` 归档，
   同时备份 ``brain.db``——随时可原样回滚；
3. 重建 ``narrative_index``（force），清理各项目 ``scene_index.json`` 里的日记残留。

幂等：目标文件已存在则跳过合并（仍归档旧文件）；可重复执行。
默认 dry-run 只报告，加 ``--apply`` 才落盘。

用法：python scripts/migrate_diary_unify.py [--apply]
报告：.codebuddy/tmp/diary-unify-report.json（不入库）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from omni_core.local import runtime_paths as rp          # noqa: E402
from omni_core.local import scene_executor as se          # noqa: E402

META_START = se._META_START
META_END = se._META_END


def _meta_extras(raw: str) -> dict:
    out: dict = {}
    a, b = raw.find(META_START), raw.find(META_END)
    if a < 0 or b < 0 or b < a:
        return out
    for line in raw[a + len(META_START):b].strip().splitlines():
        m = re.match(r"^([A-Za-z_][A-Za-z_0-9]*):\s*(.*)$", line.strip())
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def _json_list(value: str) -> list:
    try:
        parsed = json.loads(value or "[]")
    except Exception:
        return []
    return [str(x) for x in parsed if str(x).strip()] if isinstance(parsed, list) else []


def _uniq(items) -> list:
    seen, out = set(), []
    for x in items:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _merge(files) -> str:
    """files: [(pid, Path)] —— 完整保留各篇正文，不重新生成、不编造内容。"""
    parsed = []
    for pid, path in sorted(files, key=lambda x: x[0]):
        raw = path.read_text(encoding="utf-8")
        meta, body = se.parse_scene_block(raw)
        parsed.append((pid, meta, _meta_extras(raw), (body or "").strip()))

    created = min((m.get("created") or "" for _, m, _, _ in parsed if m.get("created")),
                  default="") or datetime.now(timezone.utc).isoformat()
    updated = max((m.get("updated") or "" for _, m, _, _ in parsed if m.get("updated")),
                  default="") or created
    # 合并后只剩一行索引，必须保留**所有源篇**的摘要词面，否则按内容检索会漏。
    # INV-12 索引只存 summary + topics、不存正文；拼接各级摘要仍属摘要范畴。
    summary = "；".join(_uniq(
        str(m.get("summary") or "").strip()
        for _, m, _, _ in parsed if str(m.get("summary") or "").strip()))[:190]

    topics, chat_ids, peers = [], [], []
    t_from = t_to = ""
    basis = "local"
    for _, _, extra, _ in parsed:
        topics += _json_list(extra.get("topics"))
        chat_ids += _json_list(extra.get("chat_ids"))
        peers += _json_list(extra.get("peers"))
        if extra.get("time_from"):
            t_from = min(t_from or extra["time_from"], extra["time_from"])
        if extra.get("time_to"):
            t_to = max(t_to or extra["time_to"], extra["time_to"])
        if extra.get("date_basis") and extra["date_basis"] != "local":
            basis = "legacy"

    lines = [META_START, f"created: {created}", f"updated: {updated}",
             f"summary: {summary}", "heat: 0"]
    for key, values in (("topics", topics), ("chat_ids", chat_ids), ("peers", peers)):
        merged = _uniq(values)
        if merged:
            lines.append(f"{key}: " + json.dumps(merged, ensure_ascii=False))
    if t_from:
        lines.append(f"time_from: {t_from}")
    if t_to:
        lines.append(f"time_to: {t_to}")
    lines.append(f"date_basis: {basis}")
    lines.append("merged_from: " + json.dumps([pid for pid, _, _, _ in parsed],
                                              ensure_ascii=False))
    lines.append(META_END)

    bodies = []
    for idx, (pid, _, _, body) in enumerate(parsed):
        if not body:
            continue
        bodies.append(body if idx == 0
                      else f"（同一篇叙事的另一段，来自 {pid}）\n{body}")
    return "\n".join(lines) + "\n" + "\n\n---\n\n".join(bodies) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="实际落盘（缺省只预览）")
    ap.add_argument("--refresh", action="store_true",
                    help="从最近归档重算全局文件的 META（配合 --apply；不改归档、不重复归档）")
    args = ap.parse_args()

    gdir = se.diary_dir()
    old = sorted(rp.projects_root().glob("*/memory/scene_blocks/diary-*.md"))
    groups = defaultdict(list)
    for path in old:
        groups[path.name].append((path.parent.parent.parent.name, path))

    report = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "plan": "doc/plans/personal-agent-memory-2026-11-05.md §11 H-4",
        "apply": bool(args.apply),
        "global_dir": str(gdir),
        "old_files": len(old),
        "distinct_names": len(groups),
        "merged_groups": sum(1 for v in groups.values() if len(v) > 1),
        "merged": 0, "skipped_existing": 0, "archived": 0,
        "backup": "", "index_rebuild": {}, "scene_index_synced": [],
    }
    if not args.apply:
        report["preview_names"] = sorted(groups)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    if args.refresh:
        archives = sorted((rp.global_omni() / "memory" / "archive").glob("diary-unify-*"))
        if not archives:
            print("no diary-unify archive found")
            return 1
        src = archives[-1]
        refreshed = defaultdict(list)
        for path in sorted(src.glob("*/*.md")):
            refreshed[path.name].append((path.parent.name, path))
        gdir.mkdir(parents=True, exist_ok=True)
        for name, files in sorted(refreshed.items()):
            (gdir / name).write_text(_merge(files), encoding="utf-8")
            report["merged"] += 1
        report["refresh_from"] = str(src)
        try:
            from omni_core import memory_brain as mb
            report["index_rebuild"] = (mb.migrate_narrative_once(force=True)
                                       if mb.enabled() else {"skipped": "memory.enabled=false"})
        except Exception as exc:                            # noqa: BLE001
            report["index_rebuild"] = {"error": f"{type(exc).__name__}: {exc}"}
        out = ROOT / ".codebuddy" / "tmp" / "diary-unify-report.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    archive = rp.global_omni() / "memory" / "archive" / f"diary-unify-{stamp}"
    archive.mkdir(parents=True, exist_ok=True)
    report["backup"] = str(archive)

    brain_db = rp.global_omni() / "memory" / "brain.db"
    if brain_db.is_file():
        shutil.copy2(brain_db, archive / "brain.db")

    gdir.mkdir(parents=True, exist_ok=True)
    for name, files in sorted(groups.items()):
        dst = gdir / name
        if dst.exists():
            report["skipped_existing"] += 1
        else:
            dst.write_text(_merge(files), encoding="utf-8")
            report["merged"] += 1
        for pid, path in files:
            target = archive / pid
            target.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(target / path.name))
            report["archived"] += 1

    # 索引重建：仅在记忆层启用时执行，否则会清空索引
    try:
        from omni_core import memory_brain as mb
        if mb.enabled():
            report["index_rebuild"] = mb.migrate_narrative_once(force=True)
        else:
            report["index_rebuild"] = {"skipped": "memory.enabled=false，需启用后手动重建"}
    except Exception as exc:                                # noqa: BLE001
        report["index_rebuild"] = {"error": f"{type(exc).__name__}: {exc}"}

    for idx in rp.projects_root().glob("*/memory/scene_index.json"):
        pid = idx.parent.parent.name
        try:
            se.sync_scene_index(pid)
            report["scene_index_synced"].append(pid)
        except Exception:
            continue

    out = ROOT / ".codebuddy" / "tmp" / "diary-unify-report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("report ->", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
