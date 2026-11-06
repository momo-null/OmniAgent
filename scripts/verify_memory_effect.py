"""记忆效果验证（plan personal-agent-memory §8.4）：L1 离线回放 + §8.4.3 指标计算。

三层验证中的**可自动化层**：
- L1 离线回放：标注集（构造值，只落 ``.codebuddy/tmp``，不入仓库）逐条跑当前
  检索面，对照 M0 基线的 A 侧回放（``memory-baseline-l1-replay.json``）算
  ``context_rank_gain`` / 命中率 / 不劣化；
- Narrative 结构指标：``stale_diary_hit_rate``（A 侧结构性 absent=0，B 侧应有值）；
- P0 回归相对基线不劣化（满额/top1/0召回 ≤ 基线+0.05，平均字符 ±20%）；
- silent_cost：``<current-context>`` ≤200 字符、叙事生成 LLM 调用数不变（由单测锁）。

L2（真机前后对照 ≥2 场合 × 30 轮）与 L3（人工抽样 30 轮）需要真机运行与人工判定，
不在本脚本范围——跑完把本报告交给主人做这两层。

用法：python scripts/verify_memory_effect.py
输出：``.codebuddy/tmp/memory-effect-report.json``（不入库）。
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TMP = os.path.join(ROOT, ".codebuddy", "tmp")
ANNOTATIONS = os.path.join(TMP, "memory-annotations.json")
BASELINE = os.path.join(TMP, "memory-baseline.json")
BASELINE_REPLAY = os.path.join(TMP, "memory-baseline-l1-replay.json")
REPORT = os.path.join(TMP, "memory-effect-report.json")

CONTEXTS = ["group:A", "group:B"]     # 与 M0 A 侧回放同一组占位场合键（构造值）


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[warn] 读不到 {path}: {e}")
        return None


def l1_replay_b(mb):
    """B 侧回放：同一批标注集跑当前代码（只读，reinforce=False）。"""
    out = []
    ann = _load(ANNOTATIONS) or {}
    conn = mb.open_brain()
    try:
        for item in ann.get("l1") or []:
            query, expect = item["query"], item["expect"]
            ranks = {}
            for ctx in ["base"] + CONTEXTS:
                hits = mb.search_brain(query, reinforce=False, conn=conn,
                                       chat_id=("" if ctx == "base" else ctx))
                ranks[ctx] = [{"id": h["record_id"], "score": h["score"],
                               "ctx_hit": bool(h.get("ctx_hit"))} for h in hits]
            out.append({"query": query, "expect": expect, "ranks": ranks})
    finally:
        conn.close()
    return out


def l1_metrics(b_items, a_items, mb):
    """§8.4.3：context_rank_gain（场合相关条目的位次前移量）+ 命中率 + 不劣化。"""
    a_by_q = {it["query"]: it["ranks"] for it in (a_items or [])}
    gains, hit_a, hit_b = [], 0, 0
    regress = []
    for it in b_items:
        q, expect = it["query"], it["expect"]
        b_ranks, a_ranks = it["ranks"], (a_by_q.get(q) or {}).get("base") or []
        b_ids = [h["id"] for h in b_ranks["base"]]
        a_ids = [h["id"] for h in a_ranks]
        if expect in a_ids:
            hit_a += 1
        if expect in b_ids:
            hit_b += 1
        # 期望条目在「场合上下文」相对 base 的位次和（跨两个场合平均）
        g = []
        for ctx in CONTEXTS:
            b_ctx = [h["id"] for h in b_ranks.get(ctx) or []]
            if expect not in b_ctx:
                continue
            r_ctx = b_ctx.index(expect)
            r_base = b_ids.index(expect) if expect in b_ids else len(b_ids)
            g.append(max(0, r_base - r_ctx))
        if g:
            gains.append(sum(g) / len(g))
        # 不劣化：base 召回里期望条目的位次不得比 A 侧更差（同库同语料，允许并列）
        if expect in a_ids and expect in b_ids:
            if b_ids.index(expect) > a_ids.index(expect):
                regress.append({"query": q, "a_rank": a_ids.index(expect),
                                "b_rank": b_ids.index(expect)})
    n = max(1, len(b_items))
    return {"context_rank_gain_mean": round(sum(gains) / len(gains), 3) if gains else 0.0,
            "context_gain_queries": len(gains),
            "l1_hit_rate_a": round(hit_a / n, 3),
            "l1_hit_rate_b": round(hit_b / n, 3),
            "regressed": regress,
            "note": ("标注条目均为存量 atoms（chat_id 标注列为空，按 §7.2-2 不回填），"
                     "场合加权对其位次影响≈0 是预期——增益随新 atoms 的标注累积"
                     "（机制由 test_memory_recall 锁）")}


def narrative_metrics(mb):
    """R6/R8 结构指标：>7 天日记可命中（stale_diary_hit_rate，目标 ≥0.90）。"""
    ann = _load(ANNOTATIONS) or {}
    items = ann.get("narrative") or []
    stale, total, misses, month_hit = 0, 0, [], 0
    month_total = 0
    for item in items:
        q, pid, doc = item["query"], item["project"], item["doc_id"]
        rows = mb.search_narrative(q, limit=8)
        # 存储形态为 ``pid:文件名``（跨项目同名去冲突）——按期望 doc_id 后缀比对
        ids = [r["doc_id"] for r in rows]
        hit = [d for d in ids if d == doc or d.endswith(":" + doc)]
        is_month = doc.startswith("diary-month-")
        if is_month:
            month_total += 1
            month_hit += 1 if hit else 0
        else:
            total += 1
            stale += 1 if hit else 0
            if not hit:
                misses.append({"query": q, "expect": doc, "got": ids[:3]})
        if not hit:
            if is_month:
                month_total = month_total
    return {"stale_diary_hit_rate": round(stale / max(1, total), 3) if total else None,
            "stale_diary_annotations": total,
            "month_hit_rate": round(month_hit / max(1, month_total), 3) if month_total else None,
            "misses": misses,
            "note": "A 侧（M0）该通道结构性不存在 → hit_rate=0；B 侧 ≥0.90 为通过"}


def p0_regression(mb):
    """P0 四项相对基线不显著劣化（§8.4.1 口径，不调阈值）。"""
    from collections import Counter
    base = _load(BASELINE) or {}
    b0 = (base.get("recall_p0") or {})
    sys.path.insert(0, ROOT)
    from scripts.verify_memory_recall import SAMPLE_QUERIES
    k = int(mb._cfg("top_k", 5))
    counts, top1s, chars = [], [], []
    conn = mb.open_brain()
    try:
        for q in SAMPLE_QUERIES:
            hits = mb.search_brain(q, k=k, reinforce=False, conn=conn)
            counts.append(len(hits))
            if hits:
                top1s.append(str(hits[0]["record_id"]))
                chars.append(sum(len(h["content"]) for h in hits))
    finally:
        conn.close()
    n = len(SAMPLE_QUERIES)
    now = {"full_rate": sum(1 for x in counts if x >= k) / n,
           "top1_share": (Counter(top1s).most_common(1)[0][1] / len(top1s)) if top1s else 0.0,
           "zero_rate": sum(1 for x in counts if x == 0) / n,
           "avg_chars": (sum(chars) / len(chars)) if chars else 0.0}
    verdict = {}
    if b0:
        verdict = {
            "full_rate_ok": now["full_rate"] <= b0.get("full_rate", 0) + 0.05,
            "top1_ok": now["top1_share"] <= b0.get("top1_share", 0) + 0.05,
            "zero_ok": now["zero_rate"] <= b0.get("zero_rate", 1) + 0.05,
            "chars_ok": abs(now["avg_chars"] - b0.get("avg_chars", now["avg_chars"]))
            <= 0.2 * max(1.0, b0.get("avg_chars", 1.0)),
        }
    return {"baseline": b0, "now": {k_: round(v, 4) for k_, v in now.items()},
            "verdict": verdict,
            "pass": all(verdict.values()) if verdict else None}


def silent_cost(mb):
    """静默成本核对：稳定段预算 / 反思频次由单测锁，这里只记当前快照。"""
    from omni_core.local.loop import context_signal as cs
    cs._CURRENT.clear()
    cs._CURRENT.update({"chat_id": "group:x", "peer": "甲(1)",
                        "recent_topics": ["话题一", "话题二", "话题三", "话题四"],
                        "tone": "这是一个用来核对预算的语气句子"})
    block = cs.render()
    return {"context_block_chars": len(block), "context_block_limit": 200,
            "ok": len(block) <= 200,
            "note": "叙事生成 LLM 调用数不变（topics 复用同一次调用）由 "
                    "tests/test_memory_diary.py::test_maintain_diary_writes_and_idempotent 锁"}


def main() -> int:
    from omni_core import memory_brain as mb
    report: dict = {"generated": mb._now(),
                    "plan": "doc/plans/personal-agent-memory-2026-11-05.md §8.4"}
    a_items = (_load(BASELINE_REPLAY) or {}).get("items") or []
    b_items = l1_replay_b(mb)
    report["l1"] = l1_metrics(b_items, a_items, mb)
    report["narrative"] = narrative_metrics(mb)
    report["p0_regression"] = p0_regression(mb)
    report["silent_cost"] = silent_cost(mb)
    report["pending_human"] = {
        "l2_ab_live": "真机前后对照（≥2 场合 × 各 30 轮）需网关实跑采集，主人操作",
        "l3_manual_sample": "从 B 组抽 30 轮人工判「像不像同一个圈子里说话」/ 串台率",
        "wrong_context_rate": 0 if False else "待 L2/L3（目标 = 0）",
        "cross_context_leak_rate": "待 L2/L3（目标 = 0）",
    }
    os.makedirs(TMP, exist_ok=True)
    with open(REPORT, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(json.dumps({k: report[k] for k in ("l1", "narrative", "p0_regression",
                                             "silent_cost")},
                     ensure_ascii=False, indent=1)[:1600])
    print("report ->", REPORT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
