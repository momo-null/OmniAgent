"""MCP 按需激活（lazy activation）单测。

覆盖方案 §9 的核心断言，全部离线（fake server + 临时状态文件，不连真实 MCP）：

- 状态机：端口不通 → ``offline``（且 ≤ 400ms）；就绪探针 false → ``unready``；true → ``ready``
- 缓存：TTL 内命中；坏 JSON 容忍；``clear_state`` 生效
- 目录：``<available_mcp_servers>`` 渲染、``max_items``/``desc_limit`` 截断、不承诺可用
- 配置：``mode`` 缺省 lazy、``split_configs`` 拆分、``build_mcp_servers(mode_filter=...)``
- 注入：``McpSession.activate`` 只在 ready 时 append；未 ready 不注入；幂等
- 派发：``mcp_call`` 未激活自动激活 + transform 归一
- 零污染：元工具不注册进 ``TOOL_REGISTRY``
"""
from __future__ import annotations

import json
import socket
import sys
import time
from types import SimpleNamespace

import pytest

from omni_core.tools import mcp_state
from omni_core.tools.mcp_meta import ActivationSlot, McpSession, build_lazy_bundle, build_mcp_tools
from omni_core.tools.mcp_servers import build_mcp_servers, split_configs


# ---------------------------------------------------------------- fakes

class FakeMCP:
    """最小 MCP server 替身：async 方法 + 可控登录态。"""

    def __init__(self, name="xiaohongshu", tools=None, ready=True):
        self.name = name
        self.session = object()          # 非 None → probe 跳过 connect
        self.tools = list(tools or ["check_login_status", "list_feeds", "search_feeds"])
        self.ready = ready
        self.connect_calls = 0
        self.call_calls = 0

    async def connect(self, *args, **kwargs):
        self.connect_calls += 1
        self.session = object()

    async def cleanup(self, *args, **kwargs):
        self.session = None

    async def list_tools(self, *args, **kwargs):
        return [SimpleNamespace(name=n, description=f"{n} desc", inputSchema={
            "type": "object",
            "properties": {"feed_id": {"type": "string"}},
            "required": ["feed_id"],
        }) for n in self.tools]

    async def call_tool(self, name, args=None, *a, **k):
        self.call_calls += 1
        if name == "check_login_status":
            payload = {"logged_in": self.ready}
        else:
            payload = {"items": [{"id": "n1", "title": "标题", "noteCard": {"title": "标题"}}]}
        return SimpleNamespace(content=[SimpleNamespace(text=json.dumps(payload))],
                               isError=False, structuredContent=None)

    def invalidate_tools_cache(self):
        return None


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """状态文件隔离：不碰真实 ~/.omniagent，且每个用例从干净缓存起步。"""
    monkeypatch.setenv("OMNI_MCP_STATE_PATH", str(tmp_path / "mcp_state.json"))
    mcp_state.clear_state()
    yield
    mcp_state.clear_state()


def _free_port() -> int:
    """拿一个确定没人监听的端口（bind 后立刻 close）。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return int(port)


@pytest.fixture(autouse=True)
def _assume_port_open(monkeypatch):
    """除显式测端口的用例，其余一律把端口探活视为"通"。

    否则这些用例会依赖"本机刚好有服务监听那个端口"——换台机器就红。
    """
    monkeypatch.setattr(mcp_state, "port_open", lambda *a, **k: True)


def _lazy_cfg(**over):
    cfg = {
        "name": "xiaohongshu", "mode": "lazy", "enabled": True,
        "url": "http://127.0.0.1:18080/mcp",
        "desc": "演示只读：列表 / 搜索 / 详情",
        "readiness": {"probe": "check_login_status", "extract": "logged_in",
                      "expect": True, "hint": "先扫码", "ttl_sec": 600},
        # 测试里显式打开就绪探针，才能覆盖"未登录 → 不注入"路径。
        # 真实服务不这么配：这类登录探针可能要十几秒（见方案 §12 实测）。
        "probe_readiness_on_activate": True,
        "tool_filter": {"include": ["check_login_status", "list_feeds", "search_feeds"]},
    }
    cfg.update(over)
    return cfg


# ---------------------------------------------------------------- 状态机

def test_probe_port_closed_is_offline_and_fast(monkeypatch):
    monkeypatch.setattr(mcp_state, "port_open", lambda *a, **k: False)
    cfg = _lazy_cfg(url=f"http://127.0.0.1:{_free_port()}/mcp")
    srv = FakeMCP()
    st = mcp_state.probe("xiaohongshu", cfg, srv, force=True)
    assert st.state == mcp_state.OFFLINE
    assert st.ready is False
    assert st.hint                      # 有恢复动作可转述
    assert st.latency_ms <= 400, f"端口探测不该慢：{st.latency_ms}ms"


def test_probe_unready_when_not_logged_in():
    cfg = _lazy_cfg()
    srv = FakeMCP(ready=False)
    st = mcp_state.probe("xiaohongshu", cfg, srv, force=True)
    assert st.state == mcp_state.UNREADY
    assert st.ready is False
    assert st.reason                          # probe_mismatch / not_logged_in
    assert "扫码" in st.hint


def test_probe_ready_and_ttl_cache():
    cfg = _lazy_cfg()
    srv = FakeMCP(ready=True)
    st = mcp_state.probe("xiaohongshu", cfg, srv, force=True)
    assert st.state == mcp_state.READY and st.ready is True
    assert set(st.tools) >= {"list_feeds"}
    # TTL 内非 force：直接命中缓存（不再调 call_tool）
    cached = mcp_state.probe("xiaohongshu", cfg, srv)
    assert cached.ts == st.ts
    assert srv.call_calls == 1             # 只有第一次探针


def test_probe_bad_state_file_tolerated(tmp_path, monkeypatch):
    path = tmp_path / "broken" / "mcp_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not-json", encoding="utf-8")
    monkeypatch.setenv("OMNI_MCP_STATE_PATH", str(path))
    assert mcp_state.load_all() == {}


def test_state_roundtrip_and_clear():
    st = mcp_state.ServerState(name="x", state=mcp_state.READY, ready=True, tools=["a"])
    mcp_state.save_state(st)
    got = mcp_state.load_state("x")
    assert got is not None and got.ready and got.tools == ["a"]
    mcp_state.clear_state("x")
    assert mcp_state.load_state("x") is None


# ---------------------------------------------------------------- 目录

def test_catalog_message_shape_and_truncation():
    cfg = {"servers": [_lazy_cfg(desc="x" * 200)], "catalog": {"desc_limit": 20, "max_items": 5}}
    text = mcp_state.format_catalog_message(cfg)
    assert text.startswith("<available_mcp_servers>")
    assert "`xiaohongshu`" in text
    assert "x" * 20 + "…" in text
    assert "mcp_activate" in text
    # 没探过 → 不承诺可用
    assert "未探活" in text


def test_catalog_respects_inject_off():
    cfg = {"servers": [_lazy_cfg()], "catalog": {"inject": False}}
    assert mcp_state.format_catalog_message(cfg) == ""


def test_catalog_shows_offline_state_after_probe(monkeypatch):
    monkeypatch.setattr(mcp_state, "port_open", lambda *a, **k: False)
    cfg = {"servers": [_lazy_cfg(url=f"http://127.0.0.1:{_free_port()}/mcp")]}
    mcp_state.probe("xiaohongshu", cfg["servers"][0], FakeMCP(), force=True)
    text = mcp_state.format_catalog_message(cfg)
    assert "offline" in text and "未启动" in text


# ---------------------------------------------------------------- 配置 / 构造

def test_mode_defaults_to_eager_and_split():
    """缺省 eager：升级不得让既有（没写 mode 的）server 静默变懒。"""
    eager = {"name": "a", "command": "python"}                        # 无 mode → eager
    explicit_lazy = {"name": "b", "url": "http://127.0.0.1:1/mcp", "mode": "lazy"}
    assert mcp_state.is_lazy_cfg(eager) is False
    assert mcp_state.is_lazy_cfg(explicit_lazy) is True
    e, l = split_configs([eager, explicit_lazy, {"name": "off", "enabled": False}])
    assert [c["name"] for c in e] == ["a"]
    assert [c["name"] for c in l] == ["b"]


def test_build_mcp_servers_mode_filter():
    cfgs = [{"name": "a", "command": "python", "mode": "eager"},
            {"name": "b", "url": "http://127.0.0.1:18080/mcp", "mode": "lazy"}]
    all_servers = build_mcp_servers(cfgs)
    assert len(all_servers) == 2                      # 缺省 all：向后兼容
    assert [s.name for s in build_mcp_servers(cfgs, mode_filter="eager")] == ["a"]
    assert [s.name for s in build_mcp_servers(cfgs, mode_filter="lazy")] == ["b"]


def test_dynamic_filter_applies_subset():
    """SDK 每次 list_tools 都重新应用 filter → 改槽即生效（子集注入的核心机制）。"""
    slot = ActivationSlot()
    cfgs = [{"name": "xiaohongshu", "url": "http://127.0.0.1:18080/mcp",
             "tool_filter": {"include": ["list_feeds", "search_feeds"]}}]
    srv = build_mcp_servers(cfgs, activation=lambda n: slot.allowed)[0]
    tools = [SimpleNamespace(name="list_feeds"), SimpleNamespace(name="search_feeds")]
    ctx = SimpleNamespace(server_name="xiaohongshu")

    def visible():
        return [t.name for t in tools if srv.tool_filter(ctx, t)]

    assert visible() == ["list_feeds", "search_feeds"]   # None = 白名单全量
    slot.allowed = {"list_feeds"}
    assert visible() == ["list_feeds"]                   # 子集生效
    slot.allowed = {"search_feeds", "not_in_include"}
    assert visible() == ["search_feeds"]                 # include 是上限：越界名不生效


def test_build_lazy_bundle_none_when_no_lazy():
    assert build_lazy_bundle([{"name": "a", "command": "python", "mode": "eager"}]) == (
        None, None, None)
    cfgs, servers, slots = build_lazy_bundle([_lazy_cfg()])
    assert cfgs and "xiaohongshu" in servers and "xiaohongshu" in slots


# ---------------------------------------------------------------- 激活 / 派发

def test_activate_injects_only_when_ready():
    srv = FakeMCP(ready=True)
    agent = SimpleNamespace(mcp_servers=[])
    sess = McpSession([_lazy_cfg()], {"xiaohongshu": srv}, log=lambda _m: None)
    sess.bind(agent)
    out = sess.activate("xiaohongshu")
    assert out["ok"] is True and out["state"] == mcp_state.READY
    assert srv in agent.mcp_servers                     # 下一块 SDK 会枚举到
    assert [t["name"] for t in out["tools"]] == srv.tools
    # 必填参数只给名字+类型，不回传完整 schema（省 token）
    assert out["tools"][0]["required"] == [{"name": "feed_id", "type": "string"}]

    again = sess.activate("xiaohongshu")
    assert again.get("already") is True
    assert len(agent.mcp_servers) == 1                  # 幂等：不重复 append


def test_activate_not_ready_does_not_inject():
    srv = FakeMCP(ready=False)
    agent = SimpleNamespace(mcp_servers=[])
    sess = McpSession([_lazy_cfg()], {"xiaohongshu": srv})
    sess.bind(agent)
    out = sess.activate("xiaohongshu")
    assert out["ok"] is False and out["state"] == mcp_state.UNREADY
    assert agent.mcp_servers == []                      # 未就绪不暴露工具
    assert out["hint"] and out["retry_after_sec"]


def test_activate_unknown_server():
    sess = McpSession([_lazy_cfg()], {"xiaohongshu": FakeMCP()})
    out = sess.activate("nope")
    assert out["ok"] is False and "available" in out


def test_mcp_call_auto_activates_and_transforms():
    srv = FakeMCP(ready=True)
    agent = SimpleNamespace(mcp_servers=[])
    cfg = _lazy_cfg()
    cfg["transform"] = {"list_feeds": "xhs_notes"}
    sess = McpSession([cfg], {"xiaohongshu": srv})
    sess.bind(agent)
    out = sess.call("xiaohongshu", "list_feeds", {})
    assert out["ok"] is True
    # transform 走 plugins.xiaohongshu.normalize.xhs_notes（归一结构，不是原始 items）
    assert out["content"][0]["title"] == "标题"
    assert "xiaohongshu" in sess.armed


def test_mcp_call_tool_error_is_returned_not_raised():
    class Boom(FakeMCP):
        async def call_tool(self, name, args=None, *a, **k):
            raise RuntimeError("boom")

    sess = McpSession([_lazy_cfg()], {"xiaohongshu": Boom(ready=True)})
    out = sess.call("xiaohongshu", "list_feeds", {})
    assert out["ok"] is False and out["state"] == mcp_state.DEGRADED
    # 异常被归一成"半死"状态（不是抛给模型），error 里带类型便于排查
    assert "RuntimeError" in out["error"]


# ---------------------------------------------------------------- 零污染

def test_meta_tools_not_registered_globally():
    """元工具是 per-run 装配物：import 与构造都不得污染 TOOL_REGISTRY。"""
    from omni_core.tools.base import TOOL_REGISTRY

    build_mcp_tools(McpSession([]))
    for name in ("mcp_catalog", "mcp_activate", "mcp_call"):
        assert name not in TOOL_REGISTRY

# ---------------------------------------------------------------- spawn（按需拉起本机服务）

def test_spawn_missing_command_falls_back_offline(monkeypatch):
    """配了 spawn 但 exe 不在 → 仍然是 offline，且 hint 用 spawn.hint（不是通用文案）。"""
    monkeypatch.setattr(mcp_state, "port_open", lambda *a, **k: False)
    cfg = _lazy_cfg(url=f"http://127.0.0.1:{_free_port()}/mcp",
                    spawn={"command": "D:/definitely-not-here/xhs.exe",
                           "hint": "手动运行 plugins/xiaohongshu/xiaohongshu_mcp.bat"})
    st = mcp_state.probe("xiaohongshu", cfg, FakeMCP(), force=True)
    assert st.state == mcp_state.OFFLINE
    assert st.reason == "spawn_command_missing"
    assert "xiaohongshu_mcp.bat" in st.hint


def test_spawn_not_called_when_port_open(monkeypatch):
    """端口已通（用户自己起了 / 上一次拉起还在）→ 绝不再拉第二份。"""
    import subprocess

    started = []
    monkeypatch.setattr(mcp_state, "port_open", lambda *a, **k: True)
    monkeypatch.setattr(subprocess, "Popen",
                        lambda argv, **k: started.append(argv) or None)
    cfg = _lazy_cfg(url=f"http://127.0.0.1:{_free_port()}/mcp",
                    spawn={"command": sys.executable})
    st = mcp_state.probe("xiaohongshu", cfg, FakeMCP(ready=True), force=True)
    assert started == []
    assert st.state == mcp_state.READY


def test_spawn_starts_service_when_port_closed(monkeypatch):
    """端口不通 → 拉起一次 → 端口就绪 → 继续协议级，最终 ready。"""
    import subprocess

    started = []
    seq = {"n": 0}

    def fake_port_open(host, port, timeout_s=0.3):
        seq["n"] += 1
        return seq["n"] >= 3          # 前两次（probe + 锁内双检）不通，拉起后通

    monkeypatch.setattr(mcp_state, "port_open", fake_port_open)
    monkeypatch.setattr(subprocess, "Popen",
                        lambda argv, **k: started.append(argv) or None)
    cfg = _lazy_cfg(url=f"http://127.0.0.1:{_free_port()}/mcp",
                    spawn={"command": sys.executable, "ready_timeout_s": 2})
    st = mcp_state.probe("xiaohongshu", cfg, FakeMCP(ready=True), force=True)
    assert started and started[0][0] == sys.executable, "应拉起一次且 argv[0] 是配置的 exe"
    assert st.state == mcp_state.READY
    assert st.latency_ms < 3000, "不该等满 ready_timeout"


def test_readiness_ok_requires_caller_words():
    """内核零业务文案：没给词表就判 unknown，不猜；通用键仍可判就绪。"""
    assert mcp_state.readiness_ok("已登录", {}) == (False, "probe_unknown")
    assert mcp_state.readiness_ok({"ok": True}, {}) == (True, "")


def test_channel_login_probe_cfg_judges_both_sides():
    """通道自持词表：否定优先、肯定判就绪；不再出现传空 cfg 恒判否。"""
    from gateway.channels import xiaohongshu

    cfg = xiaohongshu._login_probe_cfg()
    assert mcp_state.readiness_ok("未登录，请先扫码", cfg)[0] is False
    assert mcp_state.readiness_ok("not logged in", cfg)[0] is False
    assert mcp_state.readiness_ok("已登录", cfg)[0] is True
    assert mcp_state.readiness_ok("logged_in", cfg)[0] is True
    # 通道 yaml 的 readiness 段可覆盖词表，不必改代码
    custom = xiaohongshu._login_probe_cfg({"readiness": {"ok_words": ["通行"]}})
    assert mcp_state.readiness_ok("允许通行", custom)[0] is True


def test_replaced_units_is_single_source():
    """双轨收口判定：未声明 = 空集（行为零变化），显式声明才接管。"""
    from omni_core.tools.mcp_meta import replaced_units

    assert replaced_units(None) == set()
    assert replaced_units([{"name": "u1"}]) == set()
    assert replaced_units([{"name": "u1", "replace_plugin_tools": True},
                           {"name": "u2"}]) == {"u1"}
