"""迁移守卫：把「以后会悄悄退回去」的几件事钉成测试。

每条失败信息都直接给「怎么改」，因为守卫的价值在于让人一眼知道该往哪走；
白名单例外集中在本文件顶部，新增例外必须写清理由，而不是散在断言里。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PY = sorted(p for p in REPO.rglob("*.py") if not any(
    s in p.parts for s in (".venv", "tests", "node_modules", "temp", "doc", "examples", "scripts",
                           ".codebuddy", "external")
))
WEB = sorted(p for p in (REPO / "web" / "src").rglob("*") if p.suffix in (".ts", ".tsx"))

#: 平台名：出现在 harness/ 或 web/ 就说明宿主又长回了「某平台专用逻辑」
PLATFORMS = re.compile(r"napcat|feishu|wecom|bilibili|xiaohongshu|飞书|企业微信|小红书|B站", re.I)

#: plugins/ 只允许从 harness.events 取契约与注册表；messaging 是宿主自己的发送门面，
#: 按计划 §7「长期不拆」保留（它在宿主目录里，不随第三方插件分发）。
PLUGIN_HOST_ALLOW = {"harness.events"}
PLUGIN_HOST_EXEMPT_DIRS = {("plugins", "messaging")}

#: 旧包名（区分 `channel_gateway` 这种合法模块名：前后不能紧贴单词字符）
LEGACY_HOST_RE = re.compile(r"(?<![A-Za-z0-9_])gateway(?![A-Za-z0-9_])")

#: 老门禁字段：收敛到 manifest 的 tools.source=mcp 后不应再出现（用户配置不在仓库内）。
#: 唯一例外 = mcp_meta 里那一处带「过渡兼容」注释的旧字段读取（再下一个版本删）。
DEAD_FIELD = "replace_plugin_tools"
DEAD_FIELD_COMPAT = {"omni_core/tools/mcp_meta.py"}


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


def _rel(p: Path) -> str:
    return p.relative_to(REPO).as_posix()


def _imports(src: str) -> list[str]:
    """粗粒度取 import 的模块前缀（只管 static import，动态 import_module 由下面单独查）。"""
    out = []
    for m in re.finditer(r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))", src, re.M):
        out.append(m.group(1) or m.group(2))
    return out


def test_harness_has_no_platform_specifics():
    """harness/ 是宿主骨架：不写任何平台名（平台在 plugins/<name>/ 里自持）。"""
    hits = []
    for p in PY:
        if p.relative_to(REPO).parts[0] != "harness":
            continue
        for i, line in enumerate(_read(p).splitlines(), 1):
            if PLATFORMS.search(line):
                hits.append(f"{_rel(p)}:{i}: {line.strip()}")
    assert not hits, "harness/ 不得出现平台名：\n" + "\n".join(hits)


def test_web_has_no_platform_specifics():
    """前端同理：插件页签/入口由 /plugins/ui 注入，不写死平台名与页签。"""
    hits = []
    for p in WEB:
        for i, line in enumerate(_read(p).splitlines(), 1):
            if PLATFORMS.search(line):
                hits.append(f"{_rel(p)}:{i}: {line.strip()}")
    assert not hits, "web/src 不得出现平台名：\n" + "\n".join(hits)


def test_plugins_do_not_import_host_internals():
    """插件只通过 manifest 能力 + harness.events 契约与宿主交互，不 import 宿主实现模块。"""
    bad = []
    for p in PY:
        parts = p.relative_to(REPO).parts
        if parts[0] != "plugins":
            continue
        if any(parts[:len(d)] == d for d in PLUGIN_HOST_EXEMPT_DIRS):
            continue
        for mod in _imports(_read(p)):
            allowed = mod == "harness.events" or mod.startswith("harness.events.")
            if mod == "harness" or (mod.startswith("harness.") and not allowed):
                bad.append(f"{_rel(p)}: {mod}")
    assert not bad, (
        "plugins/ 只能 import harness.events（契约/注册表），"
        "其他宿主模块请走 manifest 能力或插件自带:\n" + "\n".join(bad)
    )


def test_plugin_manifest_required_sections():
    """每份 manifest 都必须自报家门：name/enabled/requires（能力面不可省）。"""
    bad = []
    for d in sorted((REPO / "plugins").iterdir()):
        if not d.is_dir() or not (d / "plugin.json").is_file():
            continue
        try:
            doc = json.loads(_read(d / "plugin.json"))
        except json.JSONDecodeError as e:
            bad.append(f"{d.name}/plugin.json: 坏 JSON ({e})")
            continue
        if doc.get("name") != d.name:
            bad.append(f"{d.name}/plugin.json: name={doc.get('name')!r} 与目录名不一致")
        for key in ("enabled", "requires"):
            if key not in doc:
                bad.append(f"{d.name}/plugin.json: 缺 {key}")
    assert not bad, "manifest 契约不完整：\n" + "\n".join(bad)


def test_no_legacy_host_package():
    """旧包名与 shim 目录：出现即说明有人把阶段 7 的正名回退了。"""
    assert not (REPO / "gateway").exists(), "gateway/ 已改名为 harness/，请沿用 harness 包路径"
    assert not (REPO / "harness" / "channels").exists(), "通道适配器已搬进 plugins/<name>/channel.py，harness/ 不再有 channels/"
    bad = []
    for p in PY + WEB:
        src = _read(p)
        if LEGACY_HOST_RE.search(src):
            bad.append(f"{_rel(p)}: 残留旧宿主名 gateway")
        if "sys.modules[__name__]" in src:
            bad.append(f"{_rel(p)}: sys.modules 别名 shim 已废弃")
        if DEAD_FIELD in src and _rel(p) not in DEAD_FIELD_COMPAT:
            bad.append(f"{_rel(p)}: {DEAD_FIELD} 已由 manifest tools.source=mcp 取代")
    assert not bad, "\n".join(bad)


def test_no_platform_names_in_kernel_modules():
    """omni_core/ 与 model_hub/server 层不得反向认识某个具体平台或宿主实现。"""
    bad = []
    for p in PY:
        top = p.relative_to(REPO).parts[0]
        if top == "omni_core":
            for i, line in enumerate(_read(p).splitlines(), 1):
                if re.search(r"xiaohongshu|xhs|小红书", line, re.I):
                    bad.append(f"{_rel(p)}:{i}: {line.strip()}")
        elif top == "model_hub" and "servers" in p.parts:
            for mod in _imports(_read(p)):
                if mod == "harness" or mod.startswith("harness."):
                    bad.append(f"{_rel(p)}: {mod}")
    assert not bad, "内核不得反向依赖具体平台/宿主实现：\n" + "\n".join(bad)
