"""P3 · 私聊会话窗口化的回归锁（依据见 implemented/memory-architecture.md §13.1）。

常驻上下文只留最近 ``_WINDOW_TURNS`` 轮且 ≤``_WINDOW_MAX_CHARS`` 字；更早的不进 messages
（原文仍在 L0 表里，靠 conversation_search 现查）。顺带：重建上下文不再整份解析 jsonl。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.api.routers import helpers as hp  # noqa: E402
from omni_core.local.task_store import ProjectStore  # noqa: E402


def _rounds(n: int, text_len: int = 20):
    recs = []
    for i in range(n):
        recs.append({"role": "user", "content": f"第{i}问" + "问" * text_len})
        recs.append({"role": "assistant", "content": f"第{i}答" + "答" * text_len,
                     "extra": {"steps": [{"type": "tool_call", "name": "shell_exec"}]}})
    return recs


def test_window_keeps_last_12_rounds_and_folds_earlier():
    out = hp._session_records_to_model_messages(_rounds(30))
    users = [m for m in out if m["role"] == "user"]
    assert len(users) == hp._WINDOW_TURNS, "只留最近 12 轮"
    assert users[0]["content"].startswith("第18问")
    fold = [m for m in out if m["role"] == "system"]
    assert len(fold) == 1 and "更早的 18 轮对话已折叠" in fold[0]["content"]
    assert "conversation_search" in fold[0]["content"], "折叠提示要给出找回途径"


def test_window_respects_char_budget():
    out = hp._session_records_to_model_messages(_rounds(12, text_len=1200))
    body = sum(len(str(m.get("content") or "")) for m in out if m["role"] != "system")
    assert body <= hp._WINDOW_MAX_CHARS + 1400, "超预算的轮必须出局（容一条超长尾轮）"
    assert [m for m in out if m["role"] == "user"][0]["content"].startswith("第11问")


def test_short_session_is_untouched():
    recs = _rounds(3)
    out = hp._session_records_to_model_messages(recs)
    assert not [m for m in out if m["role"] == "system"], "没超出窗口就不该插折叠提示"
    assert len([m for m in out if m["role"] == "user"]) == 3


def test_window_keeps_cards_of_every_round():
    """窗口按「轮」计，任务卡不被整轮切掉。"""
    out = hp._session_records_to_model_messages(_rounds(20))
    cards = [m for m in out if "【任务卡】" in str(m.get("content") or "")]
    assert len(cards) == hp._WINDOW_TURNS, "留下的每一轮都还带着自己的任务卡"


def test_read_session_limit_parses_only_the_tail(monkeypatch, tmp_path):
    """limit 是「少解析」不是「解析完再切」：坏行在尾部之外也必须不影响读取。"""
    from omni_core.local import runtime_paths as P
    path = P.session_file("d-win", "s_win")
    path.parent.mkdir(parents=True, exist_ok=True)
    good = ['{"role":"user","content":"第%d轮"}' % i for i in range(50)]
    path.write_text("{坏行}\n" + "\n".join(good), encoding="utf-8")
    assert len(ProjectStore.read_session("d-win", "s_win")) == 50      # 全量读：坏行跳过
    tail = ProjectStore.read_session("d-win", "s_win", limit=10)
    assert [m["content"] for m in tail] == ["第%d轮" % i for i in range(40, 50)]
