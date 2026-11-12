"""记忆检索召回回归（2026-10-28 起；2026-11-02 P0 扩展分布断言；2026-11-13 自锚定）。

背景一（2026-10-28）：FTS5 trigram 对 2 字中文词恒 0 召回的修复验证。
修复：_fts_query_terms 切词分类（≥3 字符 FTS 短语/3-gram OR，2 字 LIKE 兜底，
1 字丢弃），_search_conn/_decide 候选召回全部走同一助手。

背景二（2026-11-02，plan memory-recall P0 验收）：分布断言——
满额注入率 <25%、top1 重复率 <20%、0 召回率 ≤10%、平均注入字符 150–250。
分布只在 --live（真实 brain.db，只读、reinforce=False 不污染访问计数）下断言；
合成库仅跑召回正确性。

背景三（2026-11-13，分支 feat/vector-recall S1）：测量尺自锚定——旧 40 样本锚定
2026-11-02 的旧语料，语料换域后三项 FAIL 全是「问库里已不存在的内容」。改为：
--anchor 从现库 active 原子采样、LLM 生成**转述问句**（不抄原词），连同核验过的
负样本写本地 fixture（含真实记忆内容，**永不入仓**）；分布断言改在自锚定正样本上
算原四项口径，第五项单列负样本断言（负样本召回必须为 0——向量臂不得凭空召回）。
「同义命中率」（正样本中期望原子进 top-k 的比例）作为基线指标打印——它是向量臂
A/B 的被测对象，FTS-only 基线值偏低是**预期结果**，不算失败。

用法：python scripts/verify_memory_recall.py [--live] [--anchor]
  默认对临时合成库跑断言表；--live 对真实 brain.db 做对照 + 分布断言
  （fixture 缺失时自动先锚定）；--anchor 强制重新锚定（LLM，数分钟）。
  任何断言失败退出码非 0——动记忆层先跑它。
"""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from omni_core import memory_brain as mb  # noqa: E402
from omni_core.memory_brain import _search_conn  # noqa: E402

SCHEMA = [
    "CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT)",
    "CREATE TABLE atoms(record_id TEXT PRIMARY KEY, content TEXT, kind TEXT, "
    "source_task TEXT, version INTEGER, created TEXT, updated TEXT)",
    "CREATE VIRTUAL TABLE atoms_fts USING fts5("
    "content, record_id UNINDEXED, tokenize='trigram')",
]

# 语料：覆盖 2 字中文词/3 字词/ASCII 词/标点/长句形态
CORPUS = [
    ("a1", "助手「糖糖」拒绝把裙子、白丝等角色扮演内容包装成任务条目来执行。"),
    ("a2", "每日生成「盘前分析_日期.md」，收盘另需登记 close 报告存档"),
    ("a3", "报告固定包含一句话结论、纪律状态表、持仓快照、自选买入点及与上一份的变化"),
    ("a4", "该组合使用 R1-R12 纪律体系（破线、止盈、卫星仓位等），默认动作是无动作"),
    ("a5", "记忆层为四层结构：L0会话原文、L1原子记忆、L2场景块、L3用户画像。"),
    ("a6", "用户持有ETF类投资，关注指数编制方案变动对其的影响。"),
    ("a7", "「对话边界与越界请求」场景块记录越界请求的应对策略，边界保持一致。"),
    ("a8", "鲸鲸是有人类身体的鲸鱼娘，平时是人类女孩形象。"),
]

# 断言表：query → LIKE 真值应等于召回数（2 字词走 LIKE 兜底，≥3 字符走 FTS）
QUERIES = ["白丝", "报告", "盘前", "收盘", "记忆", "边界", "买入点", "卫星仓位", "ETF", "鲸鲸"]

# 负样本种子：日常问句，锚定时核验「现库零召回」后才入 fixture——
# 它们的职责是量「她该说不知道」的问题不会被凭空召回（见 distribution_checks）。
NEGATIVE_SEEDS = ["今天天气怎么样", "帮我写一首诗", "推荐一部电影", "如何做红烧肉"]

ANCHOR_SEED = 20261113
ANCHOR_SAMPLE_LIMIT = 36
ANCHOR_MIN_CONTENT = 20
ANCHOR_PROMPT = (
    "你是检索测试集构造器。给一条记忆原文，写一条用户可能会自然问出的问题："
    "1) 问的必须是这条记忆里的内容；2) 必须换说法——用同义词或换个角度问，"
    "不得与原文出现超过 4 个字的连续相同片段；3) 一句口语问句，不超过 24 字。"
    '只返回 JSON:{"q": "..."}'
)


def _fixture_path() -> str:
    return os.path.join(os.path.dirname(mb._BRAIN_PATH), "verify_recall_samples.json")


def _load_fixture() -> dict:
    import json
    p = _fixture_path()
    if not os.path.exists(p):
        return {}
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _max_common_run(a: str, b: str) -> int:
    """两串最长连续公共片段长度（转述约束检查用；短串 O(nm) 足够）。"""
    if not a or not b:
        return 0
    best, prev = 0, [0] * (len(b) + 1)
    for ca in a:
        cur = [0] * (len(b) + 1)
        for j, cb in enumerate(b, 1):
            if ca == cb:
                cur[j] = prev[j - 1] + 1
                best = max(best, cur[j])
        prev = cur
    return best


def anchor_samples(force: bool = False) -> dict:
    """自锚定：现库 active 原子采样 → LLM 转述问句 → 本地 fixture（不入仓）。

    负样本从种子取，逐条核验「现库零召回」——现库已长出相关内容的种子被丢弃
    （负样本的语义是「该说不知道」，不能拿已经有答案的问题当负样本）。
    """
    import json
    import random
    from datetime import datetime, timezone

    from omni_core.brain import router as model_router
    from omni_core.local.llm_judge import chat_json

    if not force:
        old = _load_fixture()
        if old.get("samples"):
            print(f"  fixture 已存在（{len(old['samples'])} 正 / {len(old.get('negatives', []))} 负，"
                  f"generated {old.get('generated', '?')}）——复用；--anchor 强制重生成")
            return old

    brain_cfg = model_router.resolve_main_with_engine(
        ( __import__("config").load_config() or {}).get("brain", {}) or {})
    if not (brain_cfg.get("base_url") and brain_cfg.get("model")):
        raise SystemExit("锚定需要可用的大脑模型（models.json main 槽位）")

    conn = mb.open_brain()
    rows = conn.execute(
        "SELECT record_id, content, created FROM atoms "
        "WHERE state='active' AND length(content)>=? ORDER BY created",
        (ANCHOR_MIN_CONTENT,)).fetchall()
    conn.close()
    rng = random.Random(ANCHOR_SEED)
    rng.shuffle(rows)

    samples, seen_queries = [], set()
    skipped_overlap = skipped_llm = 0
    for rid, content, created in rows:
        if len(samples) >= ANCHOR_SAMPLE_LIMIT:
            break
        data = chat_json(brain_cfg, ANCHOR_PROMPT, f"记忆原文：{content}", timeout=90.0)
        q = str((data or {}).get("q") or "").strip()
        if not q or len(q) > 40 or q in seen_queries:
            skipped_llm += 1
            continue
        if _max_common_run(q, content) > 4:
            skipped_overlap += 1  # 抄原词了，不是转述
            continue
        seen_queries.add(q)
        samples.append({"rid": rid, "query": q, "content_head": content[:40],
                        "created": created})

    negatives = []
    for q in NEGATIVE_SEEDS:
        if not mb.search_brain(q, k=3, reinforce=False):
            negatives.append(q)
        else:
            print(f"  负样本种子「{q}」现库已有相关内容——丢弃（不能再当负样本）")

    fixture = {"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "seed": ANCHOR_SEED, "samples": samples, "negatives": negatives}
    with open(_fixture_path(), "w", encoding="utf-8") as f:
        json.dump(fixture, f, ensure_ascii=False, indent=1)
    print(f"  锚定完成：{len(samples)} 正 / {len(negatives)} 负"
          f"（跳过：抄原词 {skipped_overlap}、LLM 无效 {skipped_llm}）"
          f" → {_fixture_path()}")
    if len(samples) < 30:
        raise SystemExit(f"有效转述样本不足（{len(samples)} < 30）——检查模型与语料后 --anchor 重试")
    return fixture


def like_truth(conn: sqlite3.Connection, kw: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM atoms WHERE content LIKE ?", (f"%{kw}%",)).fetchone()[0]


def distribution_checks(c: sqlite3.Connection, fixture: dict) -> int:
    """P0 验收（--live 真实库，只读）：自锚定样本的召回分布断言。

    四项口径不变（2026-11-02 P0，依据见 implemented/memory-architecture.md §13.1）：
    满额注入率 <25% / top1 重复率 <20% / 0 召回率 ≤10% / 平均注入字符 150–250，
    在**转述正样本**上计算。第五项单列：负样本召回必须为 0（锚定时已核验零召回，
    之后任何 >0 都是向量臂凭空召回，需人工核对）。同义命中率 = 正样本中期望原子
    进 top-k 的比例——**FTS-only 基线值偏低是预期结果**，它是向量臂 A/B 的被测对象，
    不设通过线。
    """
    from collections import Counter

    k = int(mb._cfg("top_k", 5))
    samples = fixture.get("samples") or []
    negatives = fixture.get("negatives") or []
    active = {r[0] for r in c.execute("SELECT record_id FROM atoms WHERE state='active'")}

    counts, top1s, chars = [], [], []
    hits, evaluated, per_sample, skipped = 0, 0, [], 0
    for s in samples:
        if s["rid"] not in active:
            skipped += 1  # 锚定后该原子被合并/作废——跳过并计入报告
            continue
        rows = mb.search_brain(s["query"], k=k, reinforce=False, conn=c)
        counts.append(len(rows))
        ids = [str(h["record_id"]) for h in rows]
        if rows:
            top1s.append(ids[0])
            chars.append(sum(len(h["content"]) for h in rows))
        hit = s["rid"] in ids
        hits += 1 if hit else 0
        evaluated += 1
        per_sample.append((hit, s["query"], s["content_head"], s["rid"]))

    n = evaluated
    full_rate = sum(1 for x in counts if x >= k) / n if n else 0.0
    zero_rate = sum(1 for x in counts if x == 0) / n if n else 0.0
    top1_share = (Counter(top1s).most_common(1)[0][1] / len(top1s)) if top1s else 0.0
    avg_chars = (sum(chars) / len(chars)) if chars else 0.0
    hit_rate = hits / n if n else 0.0

    neg_total = 0
    for q in negatives:
        neg_total += len(mb.search_brain(q, k=k, reinforce=False, conn=c))

    fails = 0
    print(f"== P0 分布断言（自锚定正样本 {n} 条"
          f"{'，跳过失效 ' + str(skipped) if skipped else ''}；负样本 {len(negatives)} 条） ==")
    for name, got, ok in (
        (f"满额({k}条)注入率", full_rate, full_rate < 0.25),
        ("top1 重复率", top1_share, top1_share < 0.20),
        ("0 召回率", zero_rate, zero_rate <= 0.10),
        ("平均注入字符", round(avg_chars, 1), 150 <= avg_chars <= 250),
    ):
        fails += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: {got}（分布 {counts}）")
    # 第五项（分支 feat/vector-recall S1 新增）：负样本不凭空召回。
    neg_ok = neg_total == 0
    fails += 0 if neg_ok else 1
    print(f"  {'PASS' if neg_ok else 'FAIL'}  负样本召回（须为 0）: {neg_total}")

    # 基线指标（不设通过线——向量臂 A/B 的被测对象）
    print(f"  ── 同义命中率（基线指标，不设线）: {hits}/{n} = {hit_rate:.2f}")
    for ok, q, head, rid in per_sample:
        mark = "HIT " if ok else "MISS"
        print(f"    {mark}  {q}  ←  {head}")
    return fails


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="mem_recall_")
    db = os.path.join(tmp, "t.db")
    conn = sqlite3.connect(db)
    if mb._fts_tokenizer() == "simple":
        mb._ensure_simple_ext(conn)  # simple 模式下连接必须加载扩展（jieba_query）
    for ddl in SCHEMA:
        conn.execute(ddl)
    now = "2026-10-28T00:00:00"
    conn.execute("INSERT INTO meta(k,v) VALUES('fts_schema_version','1')")
    for rid, content in CORPUS:
        conn.execute("INSERT INTO atoms VALUES(?,?,?,?,?,?,?)",
                     (rid, content, "fact", "t_x", 1, now, now))
        conn.execute("INSERT INTO atoms_fts(content, record_id) VALUES(?,?)", (content, rid))
    conn.commit()

    fails = 0
    print("== 断言表：召回数 == LIKE 真值 ==")
    for q in QUERIES:
        truth = like_truth(conn, q)
        got = len(_search_conn(conn, q, 10))
        ok = got == truth
        fails += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {q:<8} truth={truth} recall={got}")

    print("== 附加断言 ==")
    long_hits = _search_conn(conn, "白丝等角色扮演内容为什么一直搜不到呢", 10)
    ok = len(long_hits) >= 1
    fails += 0 if ok else 1
    print(f"  {'PASS' if ok else 'FAIL'}  长句召回 ≥1（命中 {len(long_hits)} 条）")

    punct = _search_conn(conn, "ETF,报告", 10)
    ok = len(punct) >= 1
    fails += 0 if ok else 1
    print(f"  {'PASS' if ok else 'FAIL'}  含标点 query 不抛异常且召回 ≥1（{len(punct)} 条）")

    empty = _search_conn(conn, "   ", 10)
    ok = empty == []
    fails += 0 if ok else 1
    print(f"  {'PASS' if ok else 'FAIL'}  空白 query 返回空列表")

    if "--live" in sys.argv:
        print("== live 真库对照（只读，brain.db） ==")
        try:
            c2 = mb.open_brain()
        except Exception:
            c2 = None
        if c2 is not None:
            bad = 0
            for q in QUERIES:
                truth = like_truth(c2, q)
                got = len(_search_conn(c2, q, 10))
                if truth and got < min(truth, 5):
                    bad += 1
                    print(f"  FAIL  brain/{q}: truth={truth} recall={got}")
            print(f"  {'PASS' if bad == 0 else 'FAIL'}  brain.db 抽查（缺口 {bad}）")
            fixture = anchor_samples(force="--anchor" in sys.argv)
            fails += bad + distribution_checks(c2, fixture)
            try:
                c2.close()
            except Exception:
                pass

    print("RESULT:", "PASS" if fails == 0 else f"FAIL({fails})")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
