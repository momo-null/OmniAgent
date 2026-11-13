"""scene_executor(TAM scene-extractor 移植)回归测试。

覆盖:沙箱(逃逸拒绝/空写拒绝/超限拒绝)、软删清理、文件名归一化、索引 sync+自愈、
快照还原、容量预警、带外信号→画像强制维护链路、水位与 gate、prompt 家族 tripwire、
L1 原子增量进料(游标 + origin 分组)。
LLM 一律脚本化假客户端(多轮 tool_calls 序列),不真连。
"""
import json

import pytest
import config as config_mod
from omni_core.brain.llm import BrainReply, ToolCall
from omni_core import memory_brain as mb
from omni_core.local import scene_executor
from omni_core.local import runtime_paths as _RP

_BRAIN = {"base_url": "http://127.0.0.1:9", "model": "m", "api_key": "k"}

_BLOCK_A = (
    "-----META-START-----\ncreated: T0\nupdated: T1\nsummary: 窗口环境事实\nheat: 2\n"
    "-----META-END-----\n\n## 环境事实\n- 事实A\n"
)

_BLOCK_NEW = (
    "-----META-START-----\ncreated: T9\nupdated: T9\nsummary: D盘工具链\nheat: 1\n"
    "-----META-END-----\n\n## 可靠操作方式\n- 用 PowerShell 递归统计\n"
)


@pytest.fixture
def iso(monkeypatch, tmp_path):
    """隔离用户目录 + brain 库 + 记忆 gate 开(其余键透传缺省)。"""
    monkeypatch.setattr(_RP, "_GLOBAL", tmp_path / ".omniagent")
    _RP.ensure_global_dirs()
    monkeypatch.setattr(mb, "_BRAIN_PATH", str(tmp_path / "brain.db"))
    monkeypatch.setattr(mb, "_ARCHIVE_DIR", str(tmp_path / "archive"))
    monkeypatch.setattr(config_mod, "get_config",
                        lambda k, d=None: True if k == mb._GATE else d)
    return tmp_path


def _mk_project(pid: str) -> str:
    from omni_core.local.task_store import TaskStore
    return TaskStore.create(objective="x", project_id=pid)["task_id"]


def _write_block(pid: str, name: str, content: str) -> None:
    d = scene_executor.scene_blocks_dir(pid)
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(content, encoding="utf-8")


def _install_fake_llm(monkeypatch, script, calls_log, raise_on_chat=None):
    """脚本化假客户端:script 逐轮给 str(纯文本)或 {"content","tool_calls":[{name,args}]}。"""

    class _Fake:
        def __init__(self, cfg, timeout=180.0, on_debug=None):
            self.calls = calls_log

        def chat(self, messages, tools=None, tool_choice="auto"):
            self.calls.append(list(messages))
            if raise_on_chat is not None:
                raise raise_on_chat
            i = len(self.calls) - 1
            if i >= len(script):
                return BrainReply(content="done")
            item = script[i]
            if isinstance(item, str):
                return BrainReply(content=item)
            return BrainReply(content=str(item.get("content") or ""), tool_calls=[
                ToolCall(name=c["name"], args=dict(c.get("args") or {}),
                         id=c.get("id") or f"call{i}{j}")
                for j, c in enumerate(item.get("tool_calls") or [])])

    monkeypatch.setattr(scene_executor, "LLMClient", _Fake)
    return _Fake


# --- 块格式与文件名 -----------------------------------------------------------
def test_parse_scene_block_meta_and_fallback():
    meta, body = scene_executor.parse_scene_block(_BLOCK_A)
    assert meta == {"created": "T0", "updated": "T1", "summary": "窗口环境事实", "heat": 2}
    assert body.startswith("## 环境事实")
    # 无 META 降级:全文当正文
    meta2, body2 = scene_executor.parse_scene_block("纯正文没有 META")
    assert meta2["heat"] == 0 and meta2["summary"] == "" and body2 == "纯正文没有 META"


def test_normalize_scene_filename_rules():
    n = scene_executor.normalize_scene_filename
    assert n("Coffee (Yirgacheffe).md") == "Coffee-Yirgacheffe.md"
    assert n("日常 生活.md") == "日常-生活.md"
    assert n("a  b.md") == "a-b.md"
    assert n("x?*|.md") == "x.md"
    assert n("...md") == "scene.md"
    assert n("sub/dir/name.md") == "name.md"


def test_unique_path_conflict_suffix(iso):
    pid = "d-exec-conf"
    _write_block(pid, "a.md", _BLOCK_A)
    d = scene_executor.scene_blocks_dir(pid)
    p = scene_executor._unique_path(d, "a.md")
    assert p.name == "a-2.md"


# --- 沙箱 ---------------------------------------------------------------------
def test_sandbox_rejects_traversal_empty_overlimit_unknown(iso):
    d = scene_executor.scene_blocks_dir("d-exec-sb")
    # 路径逃逸:归一化后钉死在沙箱内(读不存在的归一名 → 报不存在,而非读外部文件)
    r = scene_executor._scene_dispatch("scene_read", {"scene_file": "../../etc/passwd"}, d)
    assert r["ok"] is False
    # 空写拒绝
    r = scene_executor._scene_dispatch("scene_write", {"scene_file": "a.md", "content": "   "}, d)
    assert r["ok"] is False and "空" in r["error"]
    # 超限拒绝
    r = scene_executor._scene_dispatch("scene_write",
                                       {"scene_file": "a.md", "content": "x" * 5000}, d)
    assert r["ok"] is False and "上限" in r["error"]
    # 未知操作拒绝
    r = scene_executor._scene_dispatch("shell_exec", {}, d)
    assert r["ok"] is False and "未知操作" in r["error"]


def test_scene_write_read_edit_roundtrip(iso):
    d = scene_executor.scene_blocks_dir("d-exec-rw")
    r = scene_executor._scene_dispatch("scene_write",
                                       {"scene_file": "w.md", "content": _BLOCK_NEW}, d)
    assert r["ok"] is True and r["file"] == "w.md"
    r = scene_executor._scene_dispatch("scene_edit",
                                       {"scene_file": "w.md", "old_text": "PowerShell",
                                        "new_text": "pwsh"}, d)
    assert r["ok"] is True
    r = scene_executor._scene_dispatch("scene_read", {"scene_file": "w.md"}, d)
    assert "pwsh" in r["content"]
    r = scene_executor._scene_dispatch("scene_edit",
                                       {"scene_file": "w.md", "old_text": "不存在", "new_text": "x"}, d)
    assert r["ok"] is False


def test_edit_cannot_grow_past_cap_but_can_shrink_an_oversize_block(iso):
    """2026-11-05 对齐：4000 硬上限两路同守;读端不截断,所以超限块能被自己精简回去。"""
    d = scene_executor.scene_blocks_dir("d-exec-cap")
    over = _BLOCK_NEW + "x" * 4200
    d.mkdir(parents=True, exist_ok=True)
    (d / "big.md").write_text(over, encoding="utf-8")
    # 读端不截断:整块可见(历史上截到 4000 会让整理 agent 看不见"自己超了")
    r = scene_executor._scene_dispatch("scene_read", {"scene_file": "big.md"}, d)
    assert r["ok"] is True and len(r["content"]) == len(over)
    # 追加式编辑被拒(撑大)
    r = scene_executor._scene_dispatch("scene_edit",
                                       {"scene_file": "big.md", "old_text": "PowerShell",
                                        "new_text": "PowerShell 递归统计时加 -NoProfile 提速明显"}, d)
    assert r["ok"] is False and "上限" in r["error"]
    # 缩量编辑放行(即便文件仍在上限之上——否则永远出不去)
    r = scene_executor._scene_dispatch("scene_edit",
                                       {"scene_file": "big.md", "old_text": "x" * 4200,
                                        "new_text": ""}, d)
    assert r["ok"] is True
    left = (d / "big.md").read_text(encoding="utf-8")
    assert len(left) < scene_executor._BLOCK_MAX_CHARS
    # 整体重写仍受硬上限:超线直接拒
    r = scene_executor._scene_dispatch("scene_write",
                                      {"scene_file": "big.md", "content": "y" * 4001}, d)
    assert r["ok"] is False and "上限" in r["error"]


# --- 维护主流程 ----------------------------------------------------------------
def test_maintenance_full_flow_soft_delete_index_signal(iso, monkeypatch):
    pid = "d-exec-flow"
    _write_block(pid, "old.md", _BLOCK_A)
    script = [
        {"tool_calls": [{"name": "scene_read", "args": {"scene_file": "old.md"}}]},
        {"tool_calls": [
            {"name": "scene_write", "args": {"scene_file": "new.md", "content": _BLOCK_NEW}},
            {"name": "scene_write", "args": {"scene_file": "old.md", "content": "[DELETED]"}},
        ]},
        "已合并 2 个场景并新建 1 个。\n[PERSONA_UPDATE_REQUEST]\nreason: 用户重心从日常事务转向工程治理\n[/PERSONA_UPDATE_REQUEST]",
    ]
    calls = []
    _install_fake_llm(monkeypatch, script, calls)

    stats: dict = {}
    scene_executor.maintain_scenes(
        pid, _BRAIN,
        '[{"id": "m_x1", "content": "新记忆甲", "kind": "fact", "created_at": "T1"}]',
        stats=stats)

    d = scene_executor.scene_blocks_dir(pid)
    assert not (d / "old.md").exists(), "软删块应被工程侧清理"
    assert (d / "new.md").exists()
    meta, body = scene_executor.parse_scene_block((d / "new.md").read_text(encoding="utf-8"))
    assert meta["summary"] == "D盘工具链" and meta["heat"] == 1
    entries = scene_executor.read_scene_index(pid)
    assert [e["filename"] for e in entries] == ["new.md"]
    assert stats["created"] == 1 and stats["deleted"] == 1
    assert stats["persona_update_request"] == "用户重心从日常事务转向工程治理"
    # 首轮 prompt 含新记忆列表/容量行/文件清单
    first = calls[0]
    assert "新记忆列表" in first[1]["content"] and "已有场景文件清单" in first[1]["content"]
    assert "新记忆甲" in first[1]["content"], "L1 原子要原样进 prompt(带 content)"
    assert "1 / 15" in first[1]["content"]
    # 首轮 assistant 的 tool_calls 被回填 tool 结果消息
    assert any(m.get("role") == "tool" for m in calls[1])


def test_maintenance_failure_restores_backup(iso, monkeypatch):
    pid = "d-exec-restore"
    _write_block(pid, "keep.md", _BLOCK_A)
    calls = []
    _install_fake_llm(monkeypatch, [], calls, raise_on_chat=RuntimeError("boom"))
    stats: dict = {}
    scene_executor.maintain_scenes(pid, _BRAIN, "证据", stats=stats)
    assert "error" in stats and "boom" in stats["error"]
    assert (scene_executor.scene_blocks_dir(pid) / "keep.md").exists(), "失败必须整目录还原"


def test_empty_evidence_and_no_blocks_skips_llm(iso, monkeypatch):
    calls = []
    _install_fake_llm(monkeypatch, [], calls)
    stats: dict = {}
    scene_executor.maintain_scenes("d-exec-empty", _BRAIN, "  ", stats=stats)
    assert calls == [] and "error" not in stats


def test_gate_off_no_llm(iso, monkeypatch):
    monkeypatch.setattr(config_mod, "get_config",
                        lambda k, d=None: False if k == mb._GATE else d)
    calls = []
    _install_fake_llm(monkeypatch, [], calls)
    mb.remember("某条事实", brain_cfg=_BRAIN)
    scene_executor.maybe_maintain("d-exec-gate", _BRAIN, stats={})
    assert calls == []


def test_missing_llm_cfg_skips_without_eating_the_cursor(iso, monkeypatch):
    """没配 LLM 时整轮不做，且**游标不推进**——不能让"喂了但没人吃"的原子被划过去。"""
    pid = "d-exec-nocfg"
    _mk_project(pid)
    _write_block(pid, "a.md", _BLOCK_A)
    calls = []
    _install_fake_llm(monkeypatch, [], calls)
    mb.remember("等着被整理的事实", brain_cfg=_BRAIN)
    stats: dict = {}
    scene_executor.maybe_maintain(pid, {"base_url": "", "model": ""}, stats=stats)
    assert calls == [] and stats.get("l2_skipped_no_llm") == 1
    assert not scene_executor.get_watermark(pid, scene_executor._INDEX_LAST_ATOM_TS)
    # 换上可用配置 → 同一条原子还在料里
    scene_executor.maybe_maintain(pid, _BRAIN, stats={})
    assert calls, "补上 LLM 配置后，未被消费的那条原子应照常进进料"
    assert "等着被整理的事实" in calls[-1][1]["content"]


def test_feed_is_l1_atom_delta_and_cursor_advances(iso, monkeypatch):
    """2026-11-05 对齐 TAM：进料=游标后的 L1 原子增量;零增量整轮不调 LLM;跑完推游标。"""
    pid = "d-exec-feed"
    _mk_project(pid)
    _write_block(pid, "a.md", _BLOCK_A)
    calls = []
    _install_fake_llm(monkeypatch, [{"tool_calls": [
        {"name": "scene_write", "args": {"scene_file": "a.md",
                                         "content": _BLOCK_A.replace("heat: 2", "heat: 3")}}]},
        "已更新。"], calls)

    # ① 池子里一条原子都没有 → 直接跳过，一次 LLM 都不调
    stats0: dict = {}
    scene_executor.maybe_maintain(pid, _BRAIN, stats=stats0)
    assert calls == [] and stats0.get("l2_skipped_no_new_atoms") == 1

    # ② 有了原子 → 进料进 prompt，且带 id/content
    rid1 = mb.remember("D 盘空间紧张，新东西优先放 D", brain_cfg=_BRAIN)
    stats1: dict = {}
    scene_executor.maybe_maintain(pid, _BRAIN, stats=stats1)
    assert calls, "有新原子应触发维护"
    assert "D 盘空间紧张" in calls[0][1]["content"] and rid1 in calls[0][1]["content"]
    # 游标已推进到这条原子的 updated
    assert scene_executor.get_watermark(pid, scene_executor._INDEX_LAST_ATOM_TS)

    # ③ 同一批原子不该再喂第二次（时间节流位也已被 900s 拦住，这里直接绕过节流验游标）
    calls.clear()
    monkeypatch.setattr(scene_executor, "_L2_MIN_INTERVAL_S", 0)
    stats2: dict = {}
    scene_executor.maybe_maintain(pid, _BRAIN, stats=stats2)
    assert calls == [], "游标之后无增量 → 不重复喂同一批原子"
    assert stats2.get("l2_skipped_no_new_atoms") == 1

    # ④ 新原子才喂，且只喂新的那条
    calls.clear()
    mb.remember("只喂增量这一条", brain_cfg=_BRAIN)
    scene_executor.maybe_maintain(pid, _BRAIN, stats={})
    assert calls and "只喂增量这一条" in calls[0][1]["content"]
    assert "D 盘空间紧张" not in calls[0][1]["content"], "旧原子不应重复进进料"


def test_feed_is_single_pool_no_origin_grouping(iso, monkeypatch):
    """一池增量、不分组（INV-9，废除 origin 过滤）。

    ``origin`` 降级为纯溯源值，不再作 L2 进料过滤条件：任何项目的进料都取
    同一份一池增量。``origin=''``（手记 ``remember``）与各域原子同批可见——
    存储是一池（单一人格拥有全部记忆），路由只发生在消费端。
    """
    pid = "d-exec-pool"
    _mk_project(pid)
    mb.remember("全局手记：跨域可见", brain_cfg=_BRAIN)
    mb.remember("别的域的事实：不再路由", brain_cfg=_BRAIN, origin="default")

    # ① 进料面：一池——任何项目都看到全部 active 原子（不过滤）
    feed_here = [a["content"] for a in mb.atoms_since("", pid)]
    assert "全局手记：跨域可见" in feed_here
    assert "别的域的事实：不再路由" in feed_here
    feed_other = [a["content"] for a in mb.atoms_since("", "default")]
    assert feed_here == feed_other

    # ② 读取面不过滤（一池，不变）
    assert mb.search_brain("不再路由"), "search_brain 不该按 origin 切池"

    # ③ 进料进 prompt 的就是整池增量（场合标签 context_key 只作渲染元信息）
    _write_block(pid, "a.md", _BLOCK_A)
    calls: list = []
    _install_fake_llm(monkeypatch, [{"tool_calls": [
        {"name": "scene_write", "args": {"scene_file": "a.md",
                                         "content": _BLOCK_A.replace("heat: 2", "heat: 3")}}]},
        "已更新。"], calls)
    scene_executor.maybe_maintain(pid, _BRAIN, stats={})
    assert calls and "全局手记" in calls[0][1]["content"]
    assert "不再路由" in calls[0][1]["content"], "一池增量应整批进 prompt"


def test_watermark_throttles_and_signal_forces_profile(iso, monkeypatch):
    pid = "d-exec-wm"
    _mk_project(pid)
    _write_block(pid, "a.md", _BLOCK_A)
    script = [
        {"tool_calls": [{"name": "scene_write",
                         "args": {"scene_file": "a.md",
                                  "content": _BLOCK_A.replace("heat: 2", "heat: 3")}}]},
        "已更新。",
    ]
    calls = []
    _install_fake_llm(monkeypatch, script, calls)
    mb.remember("窗口环境另有事实A", brain_cfg=_BRAIN)
    stats: dict = {}
    scene_executor.maybe_maintain(pid, _BRAIN, stats=stats)
    assert calls and stats.get("persona_update_request") is None
    # 有实际动作 → 时间节流位推进(仍落 scene_index.json);再调一次被 900s 节流(无新 LLM 调用)
    assert scene_executor.get_watermark(pid), "节流位应落在 scene_index.json"
    n_calls = len(calls)
    mb.remember("第二条事实", brain_cfg=_BRAIN)
    scene_executor.maybe_maintain(pid, _BRAIN, stats={})
    assert len(calls) == n_calls
    # 信号链路:场景 agent 落画像信号 → 巩固强制重写画像(无视 atom 条数)
    seen = {}

    def fake_chat_json(cfg, system, user, timeout=60.0):
        seen["user"] = user
        return {"profile": "新画像"}

    monkeypatch.setattr(mb.llm_judge, "chat_json", fake_chat_json)
    mb.request_profile_update("用户环境从 C 盘迁到 D 盘")
    assert mb.maintain_profile(_BRAIN) is True
    assert "场景整理请求画像更新" in seen.get("user", ""), "信号应作为优先证据并入"
    from omni_core.local.runtime_paths import self_profile
    assert self_profile().read_text(encoding="utf-8") == "新画像"
    conn = mb.open_brain()
    row = conn.execute("SELECT v FROM meta WHERE k='persona_update_request'").fetchone()
    conn.close()
    assert (row[0] if row else "") == "", "信号消费后应清空"


# --- 消费端 --------------------------------------------------------------------
def test_nav_selfheal_and_read_block(iso):
    pid = "d-exec-nav"
    _write_block(pid, "b-high.md", _BLOCK_A)  # heat 2
    _write_block(pid, "b-low.md", _BLOCK_A.replace("heat: 2", "heat: 1")
                 .replace("窗口环境事实", "低热度块"))
    entries = scene_executor.load_scene_nav(pid)  # 索引缺失 → 自愈重建
    assert [e["filename"] for e in entries] == ["b-high.md", "b-low.md"]
    assert entries[0]["heat"] == 2
    assert scene_executor.read_scene_block(pid, "b-low") .startswith("-----META-START-----")
    assert scene_executor.read_scene_block(pid, "不存在.md") == ""


# --- prompt 家族 ----------------------------------------------------------------
def test_agent_cfg_raises_max_tokens_floor():
    """推理模型的思考 token 计入 max_tokens:主链 2048 会 length 截断零输出(真机实测)。"""
    cfg = scene_executor._agent_cfg({"base_url": "u", "model": "m",
                                     "request": {"max_tokens": 2048, "temperature": 0.3}})
    assert cfg["request"]["max_tokens"] == 8192
    assert cfg["request"]["temperature"] == 0.3, "其余请求参数保留"
    hi = scene_executor._agent_cfg({"request": {"max_tokens": 32000}})
    assert hi["request"]["max_tokens"] == 32000, "caller 更高的设置不动"


def test_personal_prompt_carries_discipline_tripwires():
    p = scene_executor._scene_system_prompt()
    assert "人设" in p and "流水账" in p and "当前任务目标" in p
    assert "[DELETED]" in p and "至少 2 个最相似场景" in p
    assert "PERSONA_UPDATE_REQUEST" in p and "scene_read" in p
    assert "heat" in p.lower() and "MERGE" in p
    # 2026-11-05 对齐 TAM：进料说清是 L1 原子、块是蒸馏物不是第二抄本、章节预算回来
    assert "L1 原子" in p and "第二抄本" in p
    assert "≤1500" in p and "≤400" in p and "4000" in p
    assert "会话要点" not in p and "世界状态" not in p, "旧进料口径要整段删干净"


def test_prompt_mode_switch(iso, monkeypatch):
    table = {"knowledge.scene.prompt_mode": "work"}
    scene_executor._SCENE_PROMPTS["work"] = "WORK-FAMILY-PROMPT"
    try:
        monkeypatch.setattr(config_mod, "get_config", lambda k, d=None: table.get(k, d))
        assert scene_executor._scene_system_prompt() == "WORK-FAMILY-PROMPT"
        table["knowledge.scene.prompt_mode"] = "未知家族"
        assert scene_executor._scene_system_prompt() == scene_executor._SCENE_PROMPTS["personal"], \
            "未知值回退 personal"
    finally:
        scene_executor._SCENE_PROMPTS.pop("work", None)
