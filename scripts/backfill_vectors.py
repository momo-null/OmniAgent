"""向量欠账回填（分支 feat/vector-recall，doc/plans/vector-recall-2026-11-13.md §8）。

用法：python scripts/backfill_vectors.py [--dry-run]
- 执行前 SQLite 在线备份到 memory/archive/（范式照 backfill_narrative_index.py）；
- --dry-run 只报告欠账量（l0 水位之后 + atoms hash 不匹配）；
- 循环调用 _vector_catch_up（大预算）直到欠账清零或 sidecar 不可用。
"""
import os
import sqlite3
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from omni_core import memory_brain as mb  # noqa: E402
from omni_core.local import embedder  # noqa: E402


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _counts(conn: sqlite3.Connection) -> dict:
    model = mb._vec_model()
    wm_row = conn.execute("SELECT v FROM meta WHERE k='vectors_l0_wm'").fetchone()
    wm = int(wm_row[0] or 0) if wm_row else 0
    l0_total = conn.execute(
        "SELECT COUNT(*), COALESCE(MAX(rowid), 0) FROM l0_messages").fetchone()
    l0_pending = conn.execute(
        "SELECT COUNT(*) FROM l0_messages WHERE rowid > ?", (wm,)).fetchone()[0]
    arows = conn.execute(
        "SELECT a.record_id, a.content FROM atoms a WHERE a.state='active'").fetchall()
    existing = {r[0]: r[1] for r in conn.execute(
        "SELECT record_id, src_hash FROM vectors WHERE source='atom' AND model=?",
        (model,)).fetchall()}
    a_pending = sum(1 for rid, content in arows
                    if mb._vec_src_hash(content) != existing.get(rid))
    return {"l0_total": l0_total[0], "l0_max_rowid": l0_total[1], "l0_watermark": wm,
            "l0_pending": l0_pending, "atom_active": len(arows),
            "atom_pending": a_pending, "model": model}


def main() -> int:
    dry = "--dry-run" in sys.argv
    if not mb._vec_enabled():
        print("runtime.knowledge.memory.vector.enabled=False——先开开关")
        return 1
    if not embedder.ready():
        print("embed sidecar 不可用（先跑 scripts/setup_embed_svc.py 并确认服务）")
        return 1
    print("sidecar ok:", embedder.service_url())

    conn = mb.open_brain()
    counts = _counts(conn)
    conn.close()
    print("欠账对账:", counts)
    if dry:
        return 0

    # 在线备份（数据保留纪律：动库前留档）
    bak_dir = os.path.join(os.path.dirname(mb._BRAIN_PATH), "archive")
    os.makedirs(bak_dir, exist_ok=True)
    bak = os.path.join(bak_dir, f"backup-vectors-{_now().replace(':', '')}.db")
    src = sqlite3.connect(mb._BRAIN_PATH)
    dst = sqlite3.connect(bak)
    src.backup(dst)
    dst.close()
    src.close()
    print("备份 →", bak)

    total = 0
    while True:
        n = mb._vector_catch_up(budget=64, time_budget=30.0)
        if not n:
            break
        total += n
        print(f"  已嵌 {total} 条…")
        if embedder.ready() is False:
            print("sidecar 中途失联——停止（剩余欠账下次续）")
            break
    conn = mb.open_brain()
    after = _counts(conn)
    conn.close()
    print("回填完成:", after, f"| 本次共嵌 {total} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
