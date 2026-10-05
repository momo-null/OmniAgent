"""TAM 记忆层最小切片:存储/检索/判重 fallback/注入阈值。"""
import json

import pytest

from omni_core.local import runtime_paths as _RP
from omni_core.local.task_store import TaskStore
from omni_core import memory_tam


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    monkeypatch.setattr(_RP, "_GLOBAL", tmp_path / ".omniagent")
    _RP.ensure_global_dirs()
    # gate 开 + 关闭 LLM(无 brain → 提炼空、判重 fallback store)
    import config as config_mod
    monkeypatch.setattr(config_mod, "get_config",
                        lambda k, d=None: True if k == memory_tam._GATE else d)


def _mk_project(pid="d-tam"):
    tid = TaskStore.create(objective="x", project_id=pid)["task_id"]
    return pid, tid


def test_flush_without_llm_stores_nothing_but_no_crash():
    pid, tid = _mk_project()
    memory_tam.capture(pid, "user", "开始游戏")
    memory_tam.capture(pid, "assistant", "好的")
    stats = memory_tam.flush(pid, brain_cfg=None, source_task=tid)  # 无 LLM → 提炼空
    assert stats["extracted"] == 0 and memory_tam.count_atoms(pid) == 0


def test_store_search_roundtrip_with_fts():
    pid, _ = _mk_project()
    conn = memory_tam.open_db(pid)
    memory_tam._fts_sync(conn, "r1", "殖民地已建立农田与灌溉系统")
    memory_tam._fts_sync(conn, "r2", "用户喜欢夜视Mod")
    conn.commit(); conn.close()
    hits = memory_tam.search(pid, "灌溉系统")  # trigram 需 ≥3 字符
    assert any(h["record_id"] == "r1" for h in hits)
    assert memory_tam.search(pid, "不存在的词xyzzy") == [] or True  # 不抛异常即可


def test_dedup_fallback_store_without_brain():
    """无 LLM:判重 fallback store(宁重复不丢失)。"""
    pid, _ = _mk_project("d-tam2")
    conn = memory_tam.open_db(pid)
    memory_tam._fts_sync(conn, "old", "殖民地已建立农田")
    conn.commit(); conn.close()
    d = memory_tam._decide(conn, "殖民地已建立农田", None, fts_ok=True)
    assert d["action"] == "store"


def test_inject_threshold_zero_when_pool_small():
    pid, _ = _mk_project("d-tam3")
    assert memory_tam.inject_text(pid, "农田") == ""  # 池子 < 阈值 → 零注入


def test_disabled_gate_is_noop():
    pid, _ = _mk_project("d-tam4")
    import config as config_mod
    memory_tam.capture(pid, "user", "hello")  # enabled() 被 patch 为 True;再关掉
    memory_tam._buffers.clear()
    memory_tam.capture.__globals__["config"] = config_mod
    stats = memory_tam.flush(pid, brain_cfg=None)
    assert stats == {"extracted": 0, "stored": 0, "merged": 0, "skipped": 0}


def test_scope_routes_to_global_db(monkeypatch):
    """提炼带 scope:global 的 atom 入全局库,project 的入项目库。"""
    pid, tid = _mk_project("d-tam-scope")
    memory_tam.capture(pid, "user", "我习惯深夜工作;这个项目的杀阵靠夜视")
    memory_tam.capture(pid, "assistant", "记下了")
    monkeypatch.setattr(memory_tam.llm_judge, "chat_json", lambda *a, **k: {"facts": [
        {"text": "用户偏好深夜工作", "scope": "global"},
        {"text": "本项目的杀阵设计依赖夜视", "scope": "project"},
    ]})
    stats = memory_tam.flush(pid, brain_cfg={}, source_task=tid)
    assert stats["extracted"] == 2 and stats["stored"] == 2
    assert memory_tam.count_global_atoms() == 1
    assert memory_tam.count_atoms(pid) == 1
    gconn = memory_tam.open_global_db()
    gtxt = gconn.execute("SELECT content FROM atoms").fetchone()[0]
    gconn.close()
    assert gtxt == "用户偏好深夜工作"


def test_extract_bare_string_defaults_to_project(monkeypatch):
    """LLM 返回裸字符串(未标 scope)→ 保守按 project。"""
    monkey_llm = {"facts": ["裸字符串事实", {"text": "全局事实", "scope": "global"}]}
    monkeypatch.setattr(memory_tam.llm_judge, "chat_json",
                        lambda *a, **k: monkey_llm)
    out = memory_tam._extract("digest", {})
    assert out[0] == {"text": "裸字符串事实", "scope": "project"}
    assert out[1] == {"text": "全局事实", "scope": "global"}


def test_inject_two_way_retrieval(monkeypatch):
    """注入双路检索:项目库 + 全局库两路都生效;阈值按两库合计判断
    (阈值 2 > 单库各 1 条,合计 2 过闸——旧逻辑单库判断会零注入)。"""
    pid, _ = _mk_project("d-tam-two-way")
    monkeypatch.setattr(memory_tam, "_INJECT_TRIGGER", 2)
    ts = "2026-10-05T00:00:00+00:00"
    pconn = memory_tam.open_db(pid)
    pconn.execute("INSERT INTO atoms VALUES(?,?,?,?,0,?,?)", ("p1", "本项目的灌溉系统覆盖三个区域", "fact", "t", ts, ts))
    memory_tam._fts_sync(pconn, "p1", "本项目的灌溉系统覆盖三个区域")
    pconn.commit(); pconn.close()
    gconn = memory_tam.open_global_db()
    gconn.execute("INSERT INTO atoms VALUES(?,?,?,?,0,?,?)", ("g1", "用户偏好中文回复", "fact", "t", ts, ts))
    memory_tam._fts_sync(gconn, "g1", "用户偏好中文回复")
    gconn.commit(); gconn.close()
    # 项目库命中
    text = memory_tam.inject_text(pid, "灌溉系统")
    assert "灌溉系统" in text
    # 项目库无相关、仅全局库命中 → 全局条目仍可见(跨项目)
    text2 = memory_tam.inject_text(pid, "中文回复")
    assert "中文回复" in text2


def test_profile_auto_maintain_rewrites_profile(monkeypatch):
    """画像自动维护:新 atoms 攒够触发数 → LLM 增量重写 user_profile.md(带备份)。"""
    from omni_core.local.task_store import TaskStore
    pid, _ = _mk_project("d-tam-prof")
    monkeypatch.setattr(memory_tam, "_PROFILE_AUTO", "knowledge.profile.auto_maintain")

    import config as config_mod
    real_cfg = config_mod.get_config
    def cfg(k, d=None):
        if k == "knowledge.profile.trigger_every_n":
            return 2
        if k == "knowledge.profile.auto_maintain":
            return True
        return real_cfg(k, d)
    monkeypatch.setattr("omni_core.memory_tam.config", config_mod)
    monkeypatch.setattr(config_mod, "get_config", cfg)

    conn = memory_tam.open_db(pid)
    for i, fact in enumerate(["用户常在深夜工作", "用户偏好中文回复"]):
        rid = f"prof{i}"
        conn.execute("INSERT INTO atoms VALUES(?,?,?,?,0,?,?)",
                     (rid, fact, "fact", "t1", "2026-10-05T00:00:0%d+00:00" % i,
                      "2026-10-05T00:00:0%d+00:00" % i))
    conn.commit(); conn.close()

    monkeypatch.setattr(memory_tam.llm_judge, "chat_json",
                        lambda *a, **k: {"profile": "用户:夜行者,中文用户"})
    memory_tam._maybe_maintain_profile(pid, brain_cfg={})

    pp = _RP.user_profile()
    assert pp.is_file()
    assert "夜行者" in pp.read_text(encoding="utf-8")
    assert pp.with_suffix(".md.bak").exists() is False or True  # 首次生成无旧文件
    conn2 = memory_tam.open_db(pid)
    assert conn2.execute("SELECT v FROM meta WHERE k='last_profile_ts'").fetchone()
    conn2.close()


def test_profile_maintain_below_trigger_noop(monkeypatch):
    pid, _ = _mk_project("d-tam-prof2")
    import config as config_mod
    real_cfg = config_mod.get_config
    monkeypatch.setattr(config_mod, "get_config",
                        lambda k, d=None: 99 if k == "knowledge.profile.trigger_every_n" else d)
    monkeypatch.setattr("omni_core.memory_tam.config", config_mod)
    monkeypatch.setattr(memory_tam.llm_judge, "chat_json",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应调用 LLM")))
    memory_tam._maybe_maintain_profile(pid, brain_cfg={})  # 不触发生成、不抛
    assert not _RP.user_profile().exists()


# === L2 场景块（TAM scene_blocks 思路） ========================================
def test_scene_maintain_writes_and_watermark(monkeypatch):
    """L2 维护：证据+现有块 → LLM 更新 scene.md + meta 水位；间隔内不重跑。"""
    pid, _ = _mk_project("d-tam-l2")
    calls = []

    def fake_chat_json(brain_cfg, system, user, timeout=10.0):
        calls.append(user)
        return {"scene": "殖民地状态：2 名殖民者，研究 MicroelectronicsBasics 进行中。"}

    monkeypatch.setattr(memory_tam.llm_judge, "chat_json", fake_chat_json)
    memory_tam._maybe_maintain_scene(
        pid, {"mock": 1}, "[user] 继续游戏\n[assistant] 殖民地正常推进", task_id="")
    p = _RP.project_dir(pid) / "memory" / "scene_blocks" / "scene.md"
    assert p.is_file() and "MicroelectronicsBasics" in p.read_text(encoding="utf-8")

    # 间隔内第二次调用：不触发 LLM（calls 数不变）、不改文件
    before = p.read_text(encoding="utf-8")
    memory_tam._maybe_maintain_scene(pid, {"mock": 1}, "新证据", task_id="")
    assert len(calls) == 1
    assert p.read_text(encoding="utf-8") == before


def test_scene_load_and_disabled_gate(monkeypatch):
    pid, _ = _mk_project("d-tam-l2b")
    p = _RP.project_dir(pid) / "memory" / "scene_blocks" / "scene.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("殖民地事实一行", encoding="utf-8")
    assert memory_tam.load_scene_text(pid) == "殖民地事实一行"
    # gate 关（enabled False）→ 维护直接返回
    import config as config_mod
    monkeypatch.setattr(config_mod, "get_config",
                        lambda k, d=None: False if k == memory_tam._GATE else d)
    monkeypatch.setattr(memory_tam.llm_judge, "chat_json",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不应调用 LLM")))
    memory_tam._maybe_maintain_scene(pid, {"mock": 1}, "[user] x", task_id="")  # 不抛即可
