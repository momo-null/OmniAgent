"""文件系统操作自研外层 tool 插件（平级，由 LLM 直接调用）。

覆盖用户点名的能力：读取 / 整理 / 修改电脑上的文件（文档、表格、代码、图片等）、
管理项目目录、生成报告文件。

默认启用（`group="filesystem"`）。安全边界：直接操作宿主机文件系统，路径由调用方
给定，不做额外沙箱（与 shell 同类风险，需用户知情——本组默认开是因为风险低于 shell，
且文件读写是通用 agent 的基础能力）。
"""
from pathlib import Path
from typing import Any, Dict, List, Optional

from omni_core.tools.base import function_tool


_LIMITS = {
    "read_limit": 2000,
    "max_output": 8000,
}


def configure(cfg: Optional[dict]) -> None:
    """按 config.runtime.filesystem 注入限流参数。"""
    cfg = cfg or {}
    try:
        _LIMITS["read_limit"] = int(cfg.get("read_limit", _LIMITS["read_limit"]))
    except (TypeError, ValueError):
        pass
    try:
        _LIMITS["max_output"] = int(cfg.get("max_output", _LIMITS["max_output"]))
    except (TypeError, ValueError):
        pass


def _clip(text: str, limit: int) -> str:
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"...[截断 {len(text) - limit} 字符]"


def _safe(path: str) -> Path:
    return Path(path).expanduser()


def _is_discipline_file(p: Path) -> bool:
    """F4.1：纪律文件（AGENTS.md）对 agent 只读，禁止任何写入/覆盖/编辑。"""
    return p.name.lower() == "agents.md"


@function_tool(
    description="读取文本文件内容（可限制行数）。用于查看文档/代码/配置/表格等。",
    group="filesystem",
)
def read_file(path: str, limit: int = 0) -> Dict[str, Any]:
    """读取文件。

    Args:
        path: 文件绝对或相对路径
        limit: 最多返回行数（0=全部）
    """
    p = _safe(path)
    if not p.exists():
        return {"ok": False, "error": f"文件不存在: {path}"}
    if not p.is_file():
        return {"ok": False, "error": f"不是文件: {path}"}
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    lines = text.splitlines()
    if limit and limit > 0:
        lines = lines[:limit]
    return {
        "ok": True,
        "path": str(p),
        "lines": len(lines),
        "content": _clip("\n".join(lines), _LIMITS["max_output"]),
    }


@function_tool(
    description="写入/覆盖文本文件。用于生成报告、写脚本、保存结果（自动建父目录）。",
    group="filesystem",
)
def write_file(path: str, content: str) -> Dict[str, Any]:
    """写入文件（覆盖）。

    Args:
        path: 目标路径
        content: 文件内容
    """
    p = _safe(path)
    if _is_discipline_file(p):
        return {"ok": False, "error": "AGENTS.md 为只读纪律文件，禁止写入/覆盖"}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {"ok": True, "path": str(p), "bytes": len(content.encode("utf-8"))}


@function_tool(
    description="在文件内做字符串替换（首个匹配），用于精准修改某段内容。",
    group="filesystem",
)
def edit_file(path: str, old_string: str, new_string: str) -> Dict[str, Any]:
    """替换文件内首个 old_string 为 new_string。

    Args:
        path: 文件路径
        old_string: 待替换文本（须在文件中唯一存在）
        new_string: 替换后文本
    """
    p = _safe(path)
    if _is_discipline_file(p):
        return {"ok": False, "error": "AGENTS.md 为只读纪律文件，禁止写入/覆盖"}
    if not p.is_file():
        return {"ok": False, "error": f"文件不存在: {path}"}
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if old_string not in text:
        return {"ok": False, "error": "old_string 未在文件中找到"}
    if text.count(old_string) > 1:
        return {"ok": False, "error": "old_string 出现多次，请提供更唯一的片段"}
    p.write_text(text.replace(old_string, new_string, 1), encoding="utf-8")
    return {"ok": True, "path": str(p)}


@function_tool(
    description="列举目录内容（文件/子目录）。",
    group="filesystem",
)
def list_dir(path: str = ".") -> Dict[str, Any]:
    """列举目录。

    Args:
        path: 目录路径（默认当前）
    """
    p = _safe(path)
    if not p.exists():
        return {"ok": False, "error": f"目录不存在: {path}"}
    if not p.is_dir():
        return {"ok": False, "error": f"不是目录: {path}"}
    items: List[Dict[str, str]] = []
    try:
        for child in sorted(p.iterdir()):
            items.append({"name": child.name, "type": "dir" if child.is_dir() else "file"})
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {"ok": True, "path": str(p), "count": len(items), "items": items}


@function_tool(
    description="在文件/目录树中递归搜索匹配文本，返回命中行（正则）。",
    group="filesystem",
)
def search_content(
    pattern: str,
    path: str = ".",
    glob: str = "*",
    max_matches: int = 50,
) -> Dict[str, Any]:
    """递归搜索文本。

    Args:
        pattern: 正则模式
        path: 搜索根（默认当前）
        glob: 文件过滤（如 *.py）
        max_matches: 最多返回命中数
    """
    import re

    root = _safe(path)
    if not root.exists():
        return {"ok": False, "error": f"路径不存在: {path}"}
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return {"ok": False, "error": f"非法正则: {e}"}
    matches: List[Dict[str, Any]] = []
    try:
        targets = root.rglob(glob) if root.is_dir() else [root]
        for f in targets:
            if not f.is_file():
                continue
            try:
                lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
            except Exception:
                continue
            for i, line in enumerate(lines, 1):
                if rx.search(line):
                    matches.append({"file": str(f), "line": i, "text": line.rstrip()})
                    if len(matches) >= max_matches:
                        break
            if len(matches) >= max_matches:
                break
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {"ok": True, "matches": len(matches), "hits": matches}


@function_tool(
    description="创建目录（含父目录）。",
    group="filesystem",
)
def mkdir(path: str) -> Dict[str, Any]:
    """创建目录。

    Args:
        path: 目录路径
    """
    p = _safe(path)
    try:
        p.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {"ok": True, "path": str(p)}


@function_tool(
    description="生成报告文件（等价于 write_file，语义更明确：把整理后的结果落盘成报告）。",
    group="filesystem",
)
def generate_report(path: str, content: str) -> Dict[str, Any]:
    """生成一份报告文件。

    Args:
        path: 报告路径
        content: 报告内容
    """
    return write_file(path, content)
