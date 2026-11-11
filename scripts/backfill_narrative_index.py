"""幂等补齐叙事索引指针（一次性数据修复，不改内核、不删任何数据）。

背景：磁盘上的日记/关系卡正文一直在，但清库事故后只有少数条目在
``narrative_index`` 里，其余 ``narrative_search`` 翻不到。本脚本从每份文件的
META 区（正文是唯一真相）补出 summary/topics/日期指针，只补**缺失**的
(doc_id, kind)，已存在的一律跳过；执行前用 SQLite 在线备份整库。

用法：python scripts/backfill_narrative_index.py [--dry-run]
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from omni_core import memory_brain as mb  # noqa: E402

META_RE = re.compile(r"-----META-START-----(.*?)-----META-END-----", re.S)
KEY_RE = lambda k: re.compile(r"\b" + k + r":\s*(.*?)(?=\s+[A-Za-z_]+:|$)", re.S)


def _meta(text: str) -> str:
    m = META_RE.search(text)
    return (m.group(1) if m else "").strip()


def _val(meta: str, key: str) -> str:
    m = KEY_RE(key).search(meta)
    return (m.group(1) if m else "").strip()


def _topics(meta: str):
    m = re.search(r"topics:\s*(\[[^\]]*\])", meta, re.S)
    if not m:
        return []
    try:
        v = json.loads(m.group(1))
        return [str(x) for x in v if x][:12]
    except (ValueError, TypeError):
        return []


def _date(meta: str, fallback: str) -> str:
    # 文件名即日期真相：老日记的 META.created 常是后来补写的时间，不能当日期用。
    m = re.search(r"(\d{4}-\d{2}-\d{2})", fallback)
    if m:
        return m.group(1)
    mm = re.search(r"(\d{4})-(\d{2})", fallback)   # 月记
    if mm:
        return f"{mm.group(1)}-{mm.group(2)}-01"
    for key in ("created", "updated", "date"):
        v = _val(meta, key)[:10]
        if re.match(r"\d{4}-\d{2}-\d{2}", v):
            return v
    return ""


def _body(text: str) -> str:
    return META_RE.sub("", text).strip()


def _kind_of(name: str) -> str:
    if name.startswith("diary-month-"):
        return "month"
    if name.startswith("diary-"):
        return "diary"
    return "relation"


def _collect(root: str):
    out = []
    for sub in ("diary", "relations"):
        d = os.path.join(root, sub)
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".md"):
                continue
            with open(os.path.join(d, fn), "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
            meta = _meta(text)
            kind = _kind_of(fn)
            doc_id = fn if kind in ("diary", "month") else os.path.splitext(fn)[0]
            day = _date(meta, fn)
            topics = _topics(meta)
            if kind == "relation":
                # 出口安全（N-10）：外部场合只准给摘要，绝不能把印象卡正文写进
                # narrative_index.summary —— 正文是文件真相，索引只存指针。
                name = _val(meta, "name") or doc_id
                uid = _val(meta, "uid") or doc_id
                tail = "；话题：" + "、".join(topics[:4]) if topics else ""
                summary = f"与 {name}({uid}) 的往来印象卡{tail}"
            else:
                summary = _val(meta, "summary") or _body(text).replace("\n", " ")[:120]
            out.append({"doc_id": doc_id, "kind": kind, "date": day,
                        "summary": summary[:200], "topics": topics})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--refresh-relations", action="store_true",
                    help="重建关系卡指针摘要（只改索引行，不动正文文件）")
    args = ap.parse_args()
    if not mb.enabled():
        print("memory disabled; abort")
        return 1
    mem_dir = os.path.dirname(mb._BRAIN_PATH)
    files = _collect(mem_dir)
    conn = mb.open_brain()
    try:
        have = {(str(r[0]), str(r[1])) for r in
                conn.execute("SELECT doc_id, kind FROM narrative_index")}
    finally:
        conn.close()
    if args.refresh_relations:
        todo = [x for x in files if x["kind"] == "relation"]
    else:
        todo = [x for x in files if (x["doc_id"], x["kind"]) not in have]
    print(f"files={len(files)} indexed={len(have)} missing={len(todo)}")
    for x in todo[:8]:
        print(f"  + {x['kind']:8s} {x['doc_id']:28s} {x['date']} {x['summary'][:50]}")
    if args.dry_run or not todo:
        return 0

    archive = os.path.join(mem_dir, "archive")
    os.makedirs(archive, exist_ok=True)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S")
    backup = os.path.join(archive, f"backup-before-backfill-{stamp}.db")
    src = mb.open_brain()
    try:
        dst = sqlite3.connect(backup)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    print(f"backup: {backup}")

    done = 0
    for x in todo:
        d2 = x["date"]
        if x["kind"] == "month" and d2:
            d2 = d2[:7] + "-31"           # 月记覆盖整月（按字符串区间比较，宽松无妨）
        ok = mb.upsert_narrative_index(
            doc_id=x["doc_id"], kind=x["kind"],
            date_from=x["date"], date_to=d2,
            summary=x["summary"], topics=x["topics"])
        done += 1 if ok else 0
        print(f"  {'ok' if ok else 'FAIL'} {x['kind']} {x['doc_id']}")
    conn = mb.open_brain()
    try:
        n_idx = conn.execute("SELECT COUNT(*) FROM narrative_index").fetchone()[0]
        n_fts = conn.execute("SELECT COUNT(*) FROM narrative_fts").fetchone()[0]
    finally:
        conn.close()
    print(f"written={done}/{len(todo)} narrative_index={n_idx} narrative_fts={n_fts}")
    return 0 if done == len(todo) else 2


if __name__ == "__main__":
    raise SystemExit(main())
