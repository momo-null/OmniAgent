"""P1：工具插件加载器契约测试（17 例）。

覆盖（见 doc/plans/tool-plugin-master-plan.md §1.7）：
装载/派发、configure 私有节、钩子顺序与 PluginContext、失败隔离与回滚、
保留名与重名保护、manifest 校验、目录过滤、幂等、last_report、shutdown，
以及真实装载仓库 ``plugins/``（fs_pro 分页读取 + 带上下文检索）。
"""
import json
import sys
import textwrap
from pathlib import Path
from typing import Any, Dict

import pytest

import omni_core.tools  # noqa: F401  确保 builtin 工具已注册（保留名/覆盖用例依赖）
from omni_core.tools.base import TOOL_REGISTRY, call_tool, function_tool
from omni_core.tools import loader

#: 模块导入时的内核工具基线（此刻尚无任何 load_plugins 调用）。
#: 每个用例都从这份基线起步：否则「其它用例/其它测试文件已装载过插件」会让
#: TOOL_REGISTRY 里残留插件工具，再次装载会被判成"覆盖已注册工具"。
_BASE_REGISTRY = dict(TOOL_REGISTRY)

# ── 插件样例 ───────────────────────────────────────────────────────────────

_ECHO_BODY = """
    from omni_core.tools.base import function_tool

    @function_tool(description="回声", group="demo")
    def demo_echo(text: str) -> dict:
        \"\"\"回声。

        Args:
            text: 文本
        \"\"\"
        return {"ok": True, "echo": text}
"""

_CONFIGURE_BODY = """
    from omni_core.tools.base import function_tool

    SEEN = {}

    def configure(cfg):
        SEEN.update(cfg)

    @function_tool(description="回声", group="demo")
    def demo_cfg_echo(text: str) -> dict:
        \"\"\"回声。

        Args:
            text: 文本
        \"\"\"
        return {"ok": True}
"""

_HOOK_ORDER_BODY = """
    from omni_core.tools.base import function_tool

    ORDER = []
    CTX = {}

    def configure(cfg):
        ORDER.append("configure")

    def startup(ctx):
        ORDER.append("startup")
        CTX["vision"] = getattr(ctx, "vision", "MISSING")
        CTX["execution"] = getattr(ctx, "execution", "MISSING")
        CTX["config_is_dict"] = isinstance(getattr(ctx, "config", None), dict)

    @function_tool(description="回声", group="demo")
    def demo_hook_echo(text: str) -> dict:
        \"\"\"回声。

        Args:
            text: 文本
        \"\"\"
        return {"ok": True}
"""

_STARTUP_FAIL_BODY = """
    from omni_core.tools.base import function_tool

    def startup(ctx):
        raise RuntimeError("startup boom")

    @function_tool(description="回声", group="demo")
    def demo_rb_echo(text: str) -> dict:
        \"\"\"回声。

        Args:
            text: 文本
        \"\"\"
        return {"ok": True}
"""


def _named_tool_body(func_name: str, tool_name: str) -> str:
    """生成「注册成指定工具名」的插件入口源码。"""
    return f"""
    from omni_core.tools.base import function_tool

    @function_tool(name={tool_name!r}, description="回声", group="demo")
    def {func_name}(text: str) -> dict:
        \"\"\"回声。

        Args:
            text: 文本
        \"\"\"
        return {{"ok": True}}
"""


def _shutdown_body(func_name: str, raising: bool) -> str:
    """生成带 shutdown 钩子的插件入口源码。"""
    body = "raise RuntimeError('shutdown boom')" if raising else "SHUT.append(1)"
    return f"""
    from omni_core.tools.base import function_tool

    SHUT = []

    def shutdown():
        {body}

    @function_tool(description="回声", group="demo")
    def {func_name}(text: str) -> dict:
        \"\"\"回声。

        Args:
            text: 文本
        \"\"\"
        return {{"ok": True}}
"""


def _cfg(root: Any, plugins: Dict[str, Any] = None) -> Dict[str, Any]:
    """构造最小 cfg：plugin_dirs 指向 root，可选注入各插件私有节。"""
    runtime: Dict[str, Any] = {"tools": {"plugin_dirs": [str(root)]}}
    if plugins:
        runtime["plugins"] = plugins
    return {"runtime": runtime}


def _make_plugin(root: Path, name: str, body: str = None, manifest: Any = None,
                 entry: bool = True) -> Path:
    """在 root 下造一个插件包目录。manifest=None 表示不写 plugin.json。"""
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    if manifest is not None:
        (directory / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    if entry and body is not None:
        (directory / "plugin.py").write_text(textwrap.dedent(body), encoding="utf-8")
    return directory


@pytest.fixture(autouse=True)
def _clean_loader_state():
    """用例前后：注册表恢复成内核基线 + 清 loader 幂等状态与动态模块缓存。"""

    def _restore() -> None:
        TOOL_REGISTRY.clear()
        TOOL_REGISTRY.update(_BASE_REGISTRY)
        loader._loaded_packages.clear()
        loader._loaded_modules.clear()
        loader._last_report = None
        for module_name in [m for m in sys.modules if m.startswith("_omni_plugin_")]:
            sys.modules.pop(module_name, None)

    _restore()
    yield
    _restore()


# ① 装载成功 → 组注册 + 按名派发 -------------------------------------------
def test_load_registers_and_dispatches(tmp_path):
    _make_plugin(tmp_path, "demo", _ECHO_BODY, {"name": "demo"})
    report = loader.load_plugins(_cfg(tmp_path))
    assert report.loaded == ["demo"]
    assert report.failed == []
    assert TOOL_REGISTRY["demo_echo"].group == "demo"
    assert call_tool("demo_echo", {"text": "hi"})["echo"] == "hi"


# ② configure 收到插件私有节 -----------------------------------------------
def test_configure_receives_private_section(tmp_path):
    _make_plugin(tmp_path, "demo", _CONFIGURE_BODY, {"name": "demo"})
    report = loader.load_plugins(_cfg(tmp_path, plugins={"demo": {"max_output": 5}}))
    assert report.loaded == ["demo"]
    assert loader.plugin_module("demo").SEEN == {"max_output": 5}


# ③ 钩子顺序 configure→startup 且收到 PluginContext ------------------------
def test_hook_order_and_plugin_context(tmp_path):
    _make_plugin(tmp_path, "demo", _HOOK_ORDER_BODY, {"name": "demo"})
    vision, execution = object(), object()
    report = loader.load_plugins(
        _cfg(tmp_path),
        ctx=loader.PluginContext(vision=vision, execution=execution, config={"k": 1}),
    )
    assert report.loaded == ["demo"]
    module = loader.plugin_module("demo")
    assert module.ORDER == ["configure", "startup"]
    assert module.CTX["vision"] is vision
    assert module.CTX["execution"] is execution
    assert module.CTX["config_is_dict"] is True


# ④ 单包 import 炸不殃及同批 ----------------------------------------------
def test_bad_package_does_not_break_batch(tmp_path):
    _make_plugin(tmp_path, "aaa_bad", "raise RuntimeError('boom')\n", {"name": "aaa_bad"})
    _make_plugin(tmp_path, "bbb_ok", _ECHO_BODY, {"name": "bbb_ok"})
    report = loader.load_plugins(_cfg(tmp_path))
    assert report.loaded == ["bbb_ok"]
    assert [f["name"] for f in report.failed] == ["aaa_bad"]
    assert "demo_echo" in TOOL_REGISTRY  # 同批正常包不受影响


# ⑤ startup 炸 → 回滚已注册工具 -------------------------------------------
def test_startup_failure_rolls_back_registration(tmp_path):
    _make_plugin(tmp_path, "demo_rb", _STARTUP_FAIL_BODY, {"name": "demo_rb"})
    report = loader.load_plugins(_cfg(tmp_path))
    assert report.loaded == []
    assert [f["name"] for f in report.failed] == ["demo_rb"]
    assert "demo_rb_echo" not in TOOL_REGISTRY


# ⑥ 占用保留名 → 拒载 -----------------------------------------------------
def test_reserved_tool_name_rejected(tmp_path):
    original = TOOL_REGISTRY["run_python"]
    _make_plugin(tmp_path, "reserved", _named_tool_body("rp_echo", "run_python"),
                 {"name": "reserved"})
    report = loader.load_plugins(_cfg(tmp_path))
    assert report.loaded == []
    assert "保留名" in report.failed[0]["error"]
    assert TOOL_REGISTRY["run_python"] is original  # 原对象未被替换


# ⑦ 覆盖 builtin → 拒载且原对象未替换 -------------------------------------
def test_override_builtin_rejected(tmp_path):
    # 现造一个"已注册的内核工具"作靶子：不依赖具体 builtin 名单
    # （P2 起 filesystem/shell/web 已迁出内核，不能再拿 shell_exec 当 builtin）。
    @function_tool(name="fake_builtin_tool", description="占位 builtin", group="demo")
    def _fake_builtin(text: str = "") -> dict:
        """占位。"""
        return {"ok": True}

    original = TOOL_REGISTRY["fake_builtin_tool"]
    _make_plugin(tmp_path, "override", _named_tool_body("ob_echo", "fake_builtin_tool"),
                 {"name": "override"})
    report = loader.load_plugins(_cfg(tmp_path))
    assert report.loaded == []
    assert "覆盖已注册工具" in report.failed[0]["error"]
    assert TOOL_REGISTRY["fake_builtin_tool"] is original


# ⑧ manifest 缺失 / 非法 JSON → fail --------------------------------------
def test_missing_or_invalid_manifest_fails(tmp_path):
    _make_plugin(tmp_path, "no_manifest", _ECHO_BODY, manifest=None)
    _make_plugin(tmp_path, "bad_json", _ECHO_BODY, manifest=None)
    (tmp_path / "bad_json" / "plugin.json").write_text("{not json", encoding="utf-8")
    report = loader.load_plugins(_cfg(tmp_path))
    assert report.loaded == []
    assert {f["name"] for f in report.failed} == {"no_manifest", "bad_json"}


# ⑨ manifest.name 与目录名不一致 → fail -----------------------------------
def test_manifest_name_mismatch_fails(tmp_path):
    _make_plugin(tmp_path, "demo_mm", _ECHO_BODY, {"name": "other"})
    report = loader.load_plugins(_cfg(tmp_path))
    assert report.loaded == []
    assert "不一致" in report.failed[0]["error"]


# ⑩ manifest disabled → skip ----------------------------------------------
def test_manifest_disabled_skipped(tmp_path):
    _make_plugin(tmp_path, "demo_off", _ECHO_BODY, {"name": "demo_off", "disabled": True})
    report = loader.load_plugins(_cfg(tmp_path))
    assert report.loaded == [] and report.failed == []
    assert [s["name"] for s in report.skipped] == ["demo_off"]
    assert "demo_echo" not in TOOL_REGISTRY


# ⑪ 目录过滤：点开头/无入口静默忽略，非标识符记 skip ----------------------
def test_directory_filters(tmp_path):
    hidden = tmp_path / ".hidden"
    hidden.mkdir()
    (hidden / "plugin.py").write_text("x = 1", encoding="utf-8")
    bad_name = tmp_path / "not-an-ident"
    bad_name.mkdir()
    (bad_name / "plugin.py").write_text("x = 1", encoding="utf-8")
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "readme.txt").write_text("x", encoding="utf-8")
    report = loader.load_plugins(_cfg(tmp_path))
    assert report.loaded == [] and report.failed == []
    assert [s["name"] for s in report.skipped] == ["not-an-ident"]


# ⑫ 双重 load 幂等 --------------------------------------------------------
def test_second_load_is_idempotent(tmp_path):
    _make_plugin(tmp_path, "demo2", _ECHO_BODY, {"name": "demo2"})
    first = loader.load_plugins(_cfg(tmp_path))
    second = loader.load_plugins(_cfg(tmp_path))
    assert first.loaded == ["demo2"]
    assert second.loaded == []
    assert [s["name"] for s in second.skipped] == ["demo2"]


# ⑬ plugin_dirs 不存在 → 空报告 -------------------------------------------
def test_missing_plugin_dir_gives_empty_report(tmp_path):
    report = loader.load_plugins(_cfg(tmp_path / "nope"))
    assert report.loaded == [] and report.skipped == [] and report.failed == []


# ⑭ 保留名集合定义 --------------------------------------------------------
def test_reserved_tool_names_defined():
    expected = {"run_python", "task_done", "verify", "escalate", "record", "plan"}
    assert expected <= set(loader.RESERVED_TOOL_NAMES)


# ⑮ last_report 缓存 ------------------------------------------------------
def test_last_report_cached(tmp_path):
    _make_plugin(tmp_path, "demo3", _ECHO_BODY, {"name": "demo3"})
    report = loader.load_plugins(_cfg(tmp_path))
    assert loader.last_report() is report
    payload = report.as_dict()
    assert set(payload) == {"loaded", "skipped", "failed"}
    assert payload["loaded"] == ["demo3"]


# ⑯ shutdown 被调用且异常吞噬 ---------------------------------------------
def test_shutdown_called_and_errors_swallowed(tmp_path):
    _make_plugin(tmp_path, "demo_sd_ok", _shutdown_body("demo_sd_ok", raising=False),
                 {"name": "demo_sd_ok"})
    _make_plugin(tmp_path, "demo_sd_bad", _shutdown_body("demo_sd_bad", raising=True),
                 {"name": "demo_sd_bad"})
    report = loader.load_plugins(_cfg(tmp_path))
    assert set(report.loaded) == {"demo_sd_ok", "demo_sd_bad"}
    loader.shutdown_plugins()  # 抛错的包被吞掉，不向外抛
    assert loader.plugin_module("demo_sd_ok").SHUT == [1]


# ⑰ 真实装载仓库 plugins/：fs_pro 分页语义 + 带上下文检索 -----------------
def test_repo_plugins_fs_pro(tmp_path):
    report = loader.load_plugins({})  # 缺省 plugin_dirs = ["plugins"]（相对仓库根）
    assert "fs_pro" in report.loaded, report.as_dict()
    assert report.failed == []

    repo_root = Path(__file__).resolve().parents[1]
    req = repo_root / "requirements.txt"

    page = call_tool("read_range", {"path": str(req), "offset": 2, "limit": 3})
    assert page["ok"] is True, page
    assert page["offset"] == 2 and page["end_line"] == 5
    assert page["has_more"] is True
    assert page["total_lines"] > 5
    first_line = page["content"].splitlines()[0]
    assert first_line.split("\t")[0].strip() == "3", page["content"][:80]

    found = call_tool("search_with_context",
                      {"pattern": "openai-agents", "path": str(req), "context": 1})
    assert found["ok"] is True, found
    assert found["count"] >= 1
    assert isinstance(found["matches"][0]["context"], list)
    assert found["matches"][0]["context"], "命中应带上下文行数组"
