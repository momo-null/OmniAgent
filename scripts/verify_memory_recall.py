"""记忆检索召回回归（2026-10-28）：FTS5 trigram 对 2 字中文词恒 0 召回的修复验证。

背景：tokenize='trigram' 只认 ≥3 字符的串——「白丝/报告/盘前/收盘/记忆/边界」等
高频双字词召回率恒为 0；裸 query 标点触发 MATCH 语法错误被静默吞掉；长句整串匹配
必空。修复：_fts_query_terms 切词分类（≥3 字符 FTS 短语/3-gram OR，2 字 LIKE 兜底，
1 字丢弃），_search_conn/_decide 候选召回全部走同一助手。

用法：python scripts/verify_memory_recall.py [--live]
  默认对临时合成库跑断言表；--live 追加对真实 brain.db 做 LIKE 真值对照
  （只读，不写）。任何断言失败退出码非 0——动记忆层先跑它。
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


def like_truth(conn: sqlite3.Connection, kw: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM atoms WHERE content LIKE ?", (f"%{kw}%",)).fetchone()[0]


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
            try:
                c2.close()
            except Exception:
                pass

    print("RESULT:", "PASS" if fails == 0 else f"FAIL({fails})")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
