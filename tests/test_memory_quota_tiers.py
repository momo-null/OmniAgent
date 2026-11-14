"""读侧自取配额分档（2026-11-14）：chat 档 / task 档（N-5 run 语义 + source_kind 分档）。

- chat（Web/QQ 消息轮）＝原档 total / narrative；
- task（cron/beat/手动派发）＝task_total / task_narrative 单独档：批量记忆维护
  （一次修多条/自查补漏）是任务轮的正当形态，不被查询档拦腰斩断；
- 边界缺失（异常兜底/直调轮）按 chat 档，不放水。

库落 tmp_path，不碰真实数据；配额值 monkeypatch 固定，隔离 runtime 配置差异。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from omni_core import memory_brain  # noqa: E402
from omni_core.local.loop import context_signal  # noqa: E402
from omni_core.tools import memory_tool as mt  # noqa: E402


@pytest.fixture
def brain(tmp_path, monkeypatch):
    db = tmp_path / "brain.db"
    monkeypatch.setattr(memory_brain, "_BRAIN_PATH", str(db))
    monkeypatch.setattr(memory_brain, "_ARCHIVE_DIR", str(tmp_path / "archive"))
    monkeypatch.setattr(memory_brain, "enabled", lambda: True)
    return db


@pytest.fixture(autouse=True)
def _quota_env(monkeypatch):
    """配额值固定（chat 2/1、task 4/2），隔离 runtime 配置；边界与账本用后即清。"""
    import config as cfg_mod

    real = cfg_mod.get_config
    table = {"runtime.knowledge.memory.read_quota.total": 2,
             "runtime.knowledge.memory.read_quota.narrative": 1,
             "runtime.knowledge.memory.read_quota.task_total": 4,
             "runtime.knowledge.memory.read_quota.task_narrative": 2}
    monkeypatch.setattr(cfg_mod, "get_config",
                        lambda key, default=None:
                        table[key] if key in table else real(key, default))
    yield
    context_signal._BOUNDARY.clear()
    mt._run_stats["active"] = False


def _recall():
    return mt.memory_recall(query="白丝", k=5)


def test_chat_tier_blocks_at_total(brain):
    """消息轮：原档不动，第 total+1 次被拒且文案带本档数字。"""
    context_signal.set_boundary(source_kind="chat")
    mt.reset_recall_stats()
    assert _recall()["ok"] and _recall()["ok"]
    r = _recall()
    assert not r["ok"] and "≤2" in str(r.get("error") or "")


def test_task_tier_extends_quota(brain):
    """任务轮：同一次数继续可用，到 task_total 才拦——beat 批量维护不被斩断。"""
    context_signal.set_boundary(source_kind="task")
    mt.reset_recall_stats()
    for _ in range(4):
        assert _recall()["ok"]
    r = _recall()
    assert not r["ok"] and "≤4" in str(r.get("error") or "")


def test_no_boundary_defaults_to_chat_tier(brain):
    """边界缺失按 chat 档兜底，不放水。"""
    mt.reset_recall_stats()
    assert _recall()["ok"] and _recall()["ok"]
    assert not _recall()["ok"]


def test_quota_left_reports_current_tier(brain):
    """quota_left 按当前档报余量：task 档满额=task_total 而非 chat 档。"""
    context_signal.set_boundary(source_kind="task")
    mt.reset_recall_stats()
    assert mt.quota_left()["total"] == 4
    context_signal.set_boundary(source_kind="chat")
    mt.reset_recall_stats()
    assert mt.quota_left()["total"] == 2
