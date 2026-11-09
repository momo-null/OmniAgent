"""定时任务 allow_atoms 开关：job 配置 → scheduler → dispatch → 记忆入队 全链路 plumbing。

不触达 LLM（ToolLoop 换假实现、memory_writer.enqueue 拦截捕获）。
背景（2026-11-09 实证）：cron 的 objective 是配置文本不是主人说话，
提炼器会把操作细节误当「用户规则」收进 L1——所以任务级开关**缺省关**，
主人口径来源（Web 手动派发等）不传键保持允许。
"""
from gateway import dispatch, scheduler


# ── scheduler 侧：_fire / run_now 按 job.allow_atoms 传参，缺省 False ──────────

def _mk_scheduler(captured):
    return scheduler.Scheduler(
        lambda *args, **kw: captured.update(kw) or "t_xxx")


def test_fire_default_off(monkeypatch):
    captured: dict = {}
    s = _mk_scheduler(captured)
    monkeypatch.setattr(scheduler, "_save_state", lambda st: None)
    job = {"id": "j1", "cron": "* * * * *", "objective": "做点事", "enabled": True}
    s._fire(job, "2026-11-09 10:00", source="cron")
    assert captured["allow_atoms"] is False
    assert captured["source"] == "cron:j1"


def test_fire_explicit_on(monkeypatch):
    captured: dict = {}
    s = _mk_scheduler(captured)
    monkeypatch.setattr(scheduler, "_save_state", lambda st: None)
    job = {"id": "j2", "cron": "* * * * *", "objective": "做点事",
           "enabled": True, "allow_atoms": True}
    s._fire(job, "2026-11-09 10:00", source="cron")
    assert captured["allow_atoms"] is True


def test_run_now_default_off(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(scheduler, "load_jobs",
                        lambda: [{"id": "j3", "cron": "* * * * *",
                                  "objective": "做点事", "enabled": True}])
    monkeypatch.setattr(scheduler, "record_run", lambda *a, **k: None)
    import gateway.dispatch as d
    monkeypatch.setattr(d, "submit",
                        lambda *a, **kw: captured.update(kw) or "t_xxx")
    assert scheduler.run_now("j3") == "t_xxx"
    assert captured["allow_atoms"] is False
    assert captured["source"] == "manual:j3"


# ── dispatch 侧：item.allow_atoms → memory_writer.enqueue（task 进料道）───────

class _FakeLoop:
    def __init__(self, *args, **kwargs):
        pass

    def run_task(self, spec):
        return {"success": True, "summary": "好的"}


def _fake_brain(monkeypatch):
    import config as cfg_mod
    monkeypatch.setattr(cfg_mod, "load_config",
                        lambda: {"brain": {"reasoning_mode": "native"}})
    from omni_core.brain import router as model_router
    monkeypatch.setattr(model_router, "_catalog", lambda: {
        "providers": {"testprov": {"label": "t", "base_url": "http://127.0.0.1:9",
                                   "api_key": "test",
                                   "models": [{"id": "m1", "label": "m1"}]}},
        "defaults": {"main": "testprov/m1"},
    })
    import omni_core.local.loop as loop_mod
    monkeypatch.setattr(loop_mod, "ToolLoop", _FakeLoop)
    monkeypatch.setattr(dispatch, "_notify_ui", lambda *a, **k: None)


def test_task_run_enqueues_allow_atoms_off(monkeypatch):
    _fake_brain(monkeypatch)
    captured: dict = {}
    from omni_core import memory_writer
    monkeypatch.setattr(memory_writer, "enqueue",
                        lambda pid, cfg, **kw: captured.update(kw) or None)
    dispatch.dispatch_sync("做点事", project_id="proj_x", allow_atoms=False)
    assert captured["allow_atoms"] is False


def test_task_run_default_keeps_atoms_on(monkeypatch):
    """非 cron 来源（主人口径）不传键 → 维持既有行为（允许产原子）。"""
    _fake_brain(monkeypatch)
    captured: dict = {}
    from omni_core import memory_writer
    monkeypatch.setattr(memory_writer, "enqueue",
                        lambda pid, cfg, **kw: captured.update(kw) or None)
    dispatch.dispatch_sync("做点事", project_id="proj_x")
    assert captured["allow_atoms"] is True
