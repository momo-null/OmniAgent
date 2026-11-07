"""记忆检索召回回归（2026-10-28 起；2026-11-02 P0 扩展分布断言）。

背景一（2026-10-28）：FTS5 trigram 对 2 字中文词恒 0 召回的修复验证。
修复：_fts_query_terms 切词分类（≥3 字符 FTS 短语/3-gram OR，2 字 LIKE 兜底，
1 字丢弃），_search_conn/_decide 候选召回全部走同一助手。

背景二（2026-11-02，plan memory-recall P0 验收）：40 条样本的分布断言——
满额注入率 <25%、top1 重复率 <20%、0 召回率不上升（≤10%）、平均注入字符 150–250。
分布只在 --live（真实 brain.db，只读、reinforce=False 不污染访问计数）下断言；
合成库仅跑召回正确性。

用法：python scripts/verify_memory_recall.py [--live]
  默认对临时合成库跑断言表；--live 追加对真实 brain.db 做 LIKE 真值对照 + 分布断言。
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

# P0 分布断言样本（40 条，2026-11-02）：日常问句/具体指涉/零重合问句混合，
# 与 §2.2 诊断同构，但全部锚定真实 brain.db 的 33 条 active 语料
# （身份锚、人格、格式约定、便签、截屏链路、模型、动作数等构造值）。
# 末 4 条为库内词汇零重合的日常问句（对齐 §2.2 基线 4/40=10% 0 召回）。
SAMPLE_QUERIES = [
    "飞书上你以什么身份说话", "QQ 上你怎么代表主人", "主人的信息能对外说吗",
    "对外交流的最高底线是什么", "别人套你话时底线在哪", "不同场合的发言分寸",
    "旧约定还作数吗", "对话里回复格式有什么要求", "可以用表情吗",
    "群聊回复要写多长", "群里能看到图片吗", "群里有人问你的事算谁的",
    "接话的时机怎么判断", "被艾特或者被点名要回吗", "大肥鱼是谁",
    "你的角色设定是什么", "鲸鱼女孩的人设",
    "群里提到大肥鱼你怎么办", "自我介绍要怎么说", "自我介绍能提机器人身份吗",
    "你喜欢白丝吗", "便签是用来干嘛的",
    "待办事项记在哪里", "为什么不用日历提醒", "剑网3在哪个区",
    "现在用的是什么模型", "模型能看图吗",
    "屏幕观察链路怎么工作", "observe 工具返回什么", "截图会进上下文吗",
    "私聊消息从哪个账号来", "用户会核对什么",
    "拒绝别人时用什么语气", "语气可爱会腻吗",
    "项目代码在哪个目录", "对外能不能提本机信息",
    "今天天气怎么样", "帮我写一首诗", "推荐一部电影", "如何做红烧肉",
]


def like_truth(conn: sqlite3.Connection, kw: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM atoms WHERE content LIKE ?", (f"%{kw}%",)).fetchone()[0]


def distribution_checks(c: sqlite3.Connection) -> int:
    """P0 验收（--live 真实库，只读）：40 条样本的召回分布断言。

    判据（P0 绝对分对齐，依据见 implemented/memory-architecture.md §13.1）：
    满额注入率 <25% / top1 重复率 <20% / 0 召回率 ≤10% / 平均注入字符 150–250。
    """
    from collections import Counter

    k = int(mb._cfg("top_k", 5))
    counts, top1s, chars = [], [], []
    for q in SAMPLE_QUERIES:
        hits = mb.search_brain(q, k=k, reinforce=False, conn=c)
        counts.append(len(hits))
        if hits:
            top1s.append(str(hits[0]["record_id"]))
            chars.append(sum(len(h["content"]) for h in hits))
    n = len(SAMPLE_QUERIES)
    full_rate = sum(1 for x in counts if x >= k) / n
    zero_rate = sum(1 for x in counts if x == 0) / n
    top1_share = (Counter(top1s).most_common(1)[0][1] / len(top1s)) if top1s else 0.0
    avg_chars = (sum(chars) / len(chars)) if chars else 0.0

    fails = 0
    print("== P0 分布断言（40 样本，真实 brain.db） ==")
    for name, got, ok in (
        (f"满额({k}条)注入率", full_rate, full_rate < 0.25),
        ("top1 重复率", top1_share, top1_share < 0.20),
        ("0 召回率", zero_rate, zero_rate <= 0.10),
        ("平均注入字符", round(avg_chars, 1), 150 <= avg_chars <= 250),
    ):
        fails += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {name}: {got}（分布 {counts}）")
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
            fails += bad + distribution_checks(c2)
            try:
                c2.close()
            except Exception:
                pass

    print("RESULT:", "PASS" if fails == 0 else f"FAIL({fails})")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
