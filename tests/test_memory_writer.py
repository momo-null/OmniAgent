"""记忆写链异步化（plan §3.3）单测：入队即取 buffer、单 worker 串行、满队列丢最旧、
失败隔离、did_work / allow_episode 透传。
"""
import os
import queue
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from omni_core import memory_brain, memory_writer  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """每个用例前后都等队列排空再清（worker 线程常驻，避免上一例的尾巴串进来）。"""
    memory_writer.wait_idle(5.0)
    memory_writer.reset()
    yield
    memory_writer.wait_idle(5.0)
    memory_writer.reset()


def test_enqueue_pops_capture_at_enqueue_time(monkeypatch):
    """capture 缓冲必须在**入队时**取走——否则 worker 醒来会把下一轮消息也算进来。"""
    popped = []

    def fake_pop(pid):
        popped.append(pid)
        return [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]

    calls = []

    def fake_flush(pid, cfg=None, source_task="", msgs=None,
                   allow_write=True, allow_atoms=True):
        calls.append({"pid": pid, "msgs": msgs,
                      "allow_write": allow_write})
        return {"stored": 1}

    monkeypatch.setattr(memory_brain, "pop_capture", fake_pop)
    monkeypatch.setattr(memory_brain, "flush_brain", fake_flush)
    done = []
    assert memory_writer.enqueue("p1", {"m": "x"}, source_task="t1",
                                 did_work=False,
                                 on_done=done.append) is True
    assert popped == ["p1"], "入队时就该 pop"
    assert memory_writer.wait_idle(5.0)
    assert calls and calls[0]["msgs"][0]["content"] == "a"
    assert calls[0]["allow_write"] is False, "did_work=False 必须门掉 L2 写"
    assert done and done[0]["stored"] == 1, "on_done 要拿到 stats"


def test_worker_serializes_and_counts(monkeypatch):
    """单 worker 串行：两轮按入队顺序处理，processed 计数对齐。"""
    order = []

    def fake_flush(pid, cfg=None, source_task="", msgs=None,
                   allow_write=True, allow_atoms=True):
        order.append(source_task)
        time.sleep(0.02)
        return {}

    monkeypatch.setattr(memory_brain, "flush_brain", fake_flush)
    memory_writer.enqueue("p", None, source_task="t1", msgs=[])
    memory_writer.enqueue("p", None, source_task="t2", msgs=[])
    assert memory_writer.wait_idle(5.0)
    assert order == ["t1", "t2"]
    assert memory_writer.stats()["processed"] == 2


def test_full_queue_drops_oldest_without_raising(monkeypatch):
    """写链跟不上时丢最旧的：主链路绝不能被队列满卡住或抛异常。"""
    monkeypatch.setattr(memory_writer, "_ensure_worker", lambda: None)   # 不消费
    monkeypatch.setattr(memory_writer, "_q", queue.Queue(maxsize=1))
    assert memory_writer.enqueue("p", None, source_task="t1", msgs=[]) is True
    assert memory_writer.enqueue("p", None, source_task="t2", msgs=[]) is True
    assert memory_writer.stats()["dropped"] == 1
    assert memory_writer.pending() == 1
    item = memory_writer._q.get_nowait()
    assert item["source_task"] == "t2", "保留最新的一轮"


def test_flush_failure_is_isolated(monkeypatch):
    """单轮失败只记 failed，worker 继续活着处理下一轮。"""
    seen = []

    def flaky(pid, cfg=None, source_task="", msgs=None,
              allow_write=True, allow_atoms=True):
        seen.append(source_task)
        if source_task == "bad":
            raise RuntimeError("boom")
        return {"stored": 2}

    monkeypatch.setattr(memory_brain, "flush_brain", flaky)
    memory_writer.enqueue("p", None, source_task="bad", msgs=[])
    memory_writer.enqueue("p", None, source_task="good", msgs=[])
    assert memory_writer.wait_idle(5.0)
    st = memory_writer.stats()
    assert seen == ["bad", "good"]
    assert st["failed"] == 1 and st["processed"] == 1
    assert st["last"]["stats"]["stored"] == 2


def test_enqueue_never_raises_on_bad_input(monkeypatch):
    """入队路径任何异常都吞掉并返回 False（写链是增强项，不是主链路依赖）。"""
    def boom(pid):
        raise RuntimeError("capture broken")

    monkeypatch.setattr(memory_brain, "pop_capture", boom)
    assert memory_writer.enqueue("p", None) is False


def test_enqueue_throttled_merges_until_threshold(monkeypatch):
    """群聊限流入队：首次放行，之后条数不够且未到间隔 → 不入队（消息留缓冲合并）。"""
    memory_writer.reset_throttle()
    sent = []
    monkeypatch.setattr(memory_writer, "enqueue", lambda *a, **k: sent.append(k) or True)
    monkeypatch.setattr(memory_brain, "buffer_stats",
                        lambda pid: {"msgs": 3, "chars": 30, "dropped": 0})
    assert memory_writer.enqueue_throttled("p", {"m": "x"}, min_msgs=12,
                                           min_interval=600.0) is True   # 首次：放行
    assert sent[0].get("allow_atoms") is False, "群聊只沉淀叙事层，不产 L1 原子"
    assert len(sent) == 1
    # 紧接着再来一轮、条数仍不够 → 被限流（留在缓冲里随下次合并）
    assert memory_writer.enqueue_throttled("p", {"m": "x"}, min_msgs=12,
                                           min_interval=600.0) is False
    assert len(sent) == 1
    # 攒够条数 → 放行
    monkeypatch.setattr(memory_brain, "buffer_stats",
                        lambda pid: {"msgs": 12, "chars": 300, "dropped": 0})
    assert memory_writer.enqueue_throttled("p", {"m": "x"}, min_msgs=12,
                                           min_interval=600.0) is True
    assert len(sent) == 2


def test_enqueue_throttled_fires_on_interval_for_quiet_chat(monkeypatch):
    """低流量也要喂到巩固期：条数不够但距上次入队超间隔 → 放行一次。"""
    memory_writer.reset_throttle()
    monkeypatch.setattr(memory_brain, "buffer_stats",
                        lambda pid: {"msgs": 2, "chars": 20, "dropped": 0})
    sent = []
    monkeypatch.setattr(memory_writer, "enqueue", lambda *a, **k: sent.append(k) or True)
    assert memory_writer.enqueue_throttled("p", {"m": "x"}, min_msgs=12,
                                           min_interval=600.0) is True    # 首次
    memory_writer._last_enqueue["p"] = time.time() - 601.0           # 假装过了 10 分钟
    assert memory_writer.enqueue_throttled("p", {"m": "x"}, min_msgs=12,
                                           min_interval=600.0) is True    # 到间隔
    assert all(k.get("allow_atoms") is False for k in sent)


def test_enqueue_throttled_allows_atoms_when_requested(monkeypatch):
    """缺省仍只沉淀叙事层；显式打开时任务/观察轮次允许产 L1。"""
    memory_writer.reset_throttle()
    sent = []
    monkeypatch.setattr(memory_writer, "enqueue", lambda *a, **k: sent.append(k) or True)
    monkeypatch.setattr(memory_brain, "buffer_stats",
                        lambda pid: {"msgs": 12, "chars": 300, "dropped": 0})
    assert memory_writer.enqueue_throttled("p", {"m": "x"}, min_msgs=12,
                                           min_interval=600.0) is True
    assert sent[0].get("allow_atoms") is False
    memory_writer.reset_throttle()
    assert memory_writer.enqueue_throttled("p", {"m": "x"}, min_msgs=12,
                                           min_interval=600.0,
                                           allow_atoms=True) is True
    assert sent[1].get("allow_atoms") is True


def test_flush_brain_allow_atoms_false_skips_l1(monkeypatch, tmp_path):
    """allow_atoms=False：不提炼、不写 L1，但 L2 维护/巩固照走（日记靠它）。"""
    from omni_core.local import scene_executor

    monkeypatch.setattr(memory_brain, "enabled", lambda: True)
    monkeypatch.setattr(memory_brain, "maybe_consolidate", lambda *a, **k: None)
    l2 = []
    monkeypatch.setattr(scene_executor, "maybe_maintain",
                        lambda *a, **k: l2.append(k.get("allow_write")))

    def boom(*a, **k):
        raise AssertionError("allow_atoms=False 时不应调用 LLM 提炼")

    monkeypatch.setattr(memory_brain, "_extract_detail", boom)

    stats = memory_brain.flush_brain(
        "p", {"model": "m"},
        msgs=[{"role": "user", "content": "群里有人刷屏"},
              {"role": "assistant", "content": "不管他"}],
        allow_write=True, allow_atoms=False)
    assert stats["stored"] == 0 and stats["l1_skipped"] == 1
    assert l2 == [True], "叙事层维护照走（allow_write 透传）"


def test_flush_brain_stores_episode_and_gates_l2_by_did_work(monkeypatch, tmp_path):
    """端到端（2026-10-29 收窄）：episode 无入口门——价值判是唯一守门人，说留就留；
    did_work=False 仍门掉 L2 写。"""
    from omni_core.local import scene_executor

    db = tmp_path / "brain.db"
    monkeypatch.setattr(memory_brain, "_BRAIN_PATH", str(db))
    monkeypatch.setattr(memory_brain, "_ARCHIVE_DIR", str(tmp_path / "archive"))
    monkeypatch.setattr(memory_brain, "enabled", lambda: True)
    monkeypatch.setattr(memory_brain, "maybe_consolidate", lambda *a, **k: None)
    l2 = []
    monkeypatch.setattr(scene_executor, "maybe_maintain",
                        lambda *a, **k: l2.append(k.get("allow_write")))

    script = [{"facts": [{"text": "用户上周去了西安看兵马俑"}]},
              {"items": [{"idx": 0, "keep": True, "kind": "episode", "importance": 0.6}]}]
    monkeypatch.setattr(memory_brain.llm_judge, "chat_json",
                        lambda *a, **k: script.pop(0))

    stats = memory_brain.flush_brain(
        "p", {"model": "m"}, msgs=[{"role": "user", "content": "我上周去了西安"},
                                   {"role": "assistant", "content": "兵马俑值得一去"}],
        allow_write=False)
    assert stats["stored"] == 1, "episode 无入口门：价值判说留就留"
    assert l2 == [False], "did_work=False 时 L2 收到的 allow_write 必须是 False"

    conn = memory_brain.open_brain()
    try:
        rows = conn.execute("SELECT kind FROM atoms").fetchall()
        assert [r[0] for r in rows] == ["episode"]
    finally:
        conn.close()
    memory_brain._buffers.clear()
