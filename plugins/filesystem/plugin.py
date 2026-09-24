"""文件系统操作官方插件（P2 自 omni_core/tools/filesystem_tool.py 迁出，函数体零改动）。

覆盖用户点名的能力：读取 / 整理 / 修改电脑上的文件（文档、表格、代码、图片等）、
管理项目目录；并内置大文件分页读取与带上下文的递归检索。

默认启用（`unit="filesystem"`）。安全边界：直接操作宿主机文件系统，路径由调用方
给定，不做额外沙箱（与 shell 同类风险，需用户知情——本组默认开是因为风险低于 shell，
且文件读写是通用 agent 的基础能力）。路径围栏由安全线 S1 统一注入（见
doc/plans/sandbox-permission-design.md），本插件不自行实现权限判断。

2026-09-25 归一（用户定：文件操作只留一套，读写不拆）——原独立插件 `fs_pro` 已并入：
`read_range` → `read_file(offset=, limit=)`；`search_with_context` → `search_content(context=)`；
`generate_report`（`write_file` 纯别名）删除。
"""
from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from omni_core.tools.base import function_tool


_LIMITS = {
    "read_limit": 2000,
    "max_output": 8000,
}

#: 递归检索时跳过的通用基础设施目录（与业务无关，避免扫依赖树）
_SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", ".idea"}


def configure(cfg: Optional[dict]) -> None:
    """注入本插件自有配置（``~/.omniagent/plugins/filesystem.yaml``）。

    loader 传入的就是本插件的配置 dict（含 ``enabled`` 与自有参数），
    不是 ``config.runtime.*``（内核不持有插件私有参数）。

    Args:
        cfg: 本插件自有配置。
    """
    cfg = cfg or {}
    for key in ("read_limit", "max_output"):
        try:
            _LIMITS[key] = int(cfg.get(key, _LIMITS[key]))
        except (TypeError, ValueError):
            pass


def _clip(text: str, limit: int) -> str:
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"...[截断 {len(text) - limit} 字符]"


def _safe(path: str) -> Path:
    """相对路径解析到当前任务临时目录（tasks/<task_id>/tmp/）；绝对路径原样。"""
    from omni_core.tools.workspace import resolve_path
    return resolve_path(path)


def _is_discipline_file(p: Path) -> bool:
    """F4.1：纪律文件（AGENTS.md）对 agent 只读，禁止任何写入/覆盖/编辑。"""
    return p.name.lower() == "agents.md"


@function_tool(
    description="读取文本文件内容，可按行区间分页（大文件避免一次性灌满上下文）。返回带行号前缀的"
                "内容，并附 total_lines / end_line / has_more，便于判断是否还有更多。",
    unit="filesystem",
)
def read_file(path: str, offset: int = 0, limit: int = 0) -> Dict[str, Any]:
    """读取文件（可分页）。

    Args:
        path: 文件绝对或相对路径（相对路径基准 = 当前任务临时目录）。
        offset: 起始行下标（0 起，含）。
        limit: 最多返回行数（0 = 读到文件末尾）。
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
    total = len(lines)
    start = max(0, int(offset or 0))
    count = max(0, int(limit or 0))
    end = total if count == 0 else min(total, start + count)
    body = "\n".join(f"{start + i + 1:>6}\t{line}" for i, line in enumerate(lines[start:end]))
    return {
        "ok": True,
        "path": str(p),
        "total_lines": total,
        "offset": start,
        "end_line": end,
        "has_more": end < total,
        "content": _clip(body, _LIMITS["max_output"]),
    }


@function_tool(
    description="写入/覆盖文本文件。用于生成报告、写脚本、保存结果（自动建父目录）。",
    unit="filesystem",
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
    unit="filesystem",
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
    unit="filesystem",
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
    description="在文件/目录树中递归正则检索，返回命中行；context>0 时附命中行前后各 N 行的窗口（含命中行）。",
    unit="filesystem",
)
def search_content(
    pattern: str,
    path: str = ".",
    glob: str = "*",
    context: int = 0,
    max_matches: int = 50,
) -> Dict[str, Any]:
    """递归检索文件内容（可带上下文）。

    Args:
        pattern: 正则模式
        path: 搜索根（目录或单个文件；相对路径基准 = 当前任务临时目录）
        glob: 文件名过滤（fnmatch 语法，如 *.py）
        context: 命中行**前后各 N 行**的窗口（含命中行；0 = 不返回，命中行已在 text 里）
        max_matches: 最多返回命中数
    """
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return {"ok": False, "error": f"非法正则: {e}"}
    root = _safe(path)
    if not root.exists():
        return {"ok": False, "error": f"路径不存在: {path}"}
    ctx_lines = max(0, int(context or 0))
    limit = max(1, int(max_matches or 50))
    if root.is_file():
        files: List[Path] = [root]
    else:
        try:
            files = sorted((f for f in root.rglob(glob) if f.is_file()),
                           key=lambda f: str(f))
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    matches: List[Dict[str, Any]] = []
    truncated = False
    try:
        for f in files:
            if any(part in _SKIP_DIRS or part.startswith(".") for part in f.parts[:-1]):
                continue
            if not fnmatch.fnmatch(f.name, glob):
                continue
            try:
                lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
            except Exception:
                continue
            for idx, text in enumerate(lines):
                if not rx.search(text):
                    continue
                if ctx_lines > 0:
                    lo = max(0, idx - ctx_lines)
                    hi = min(len(lines), idx + ctx_lines + 1)
                    window = [{"line": i + 1, "text": lines[i]} for i in range(lo, hi)]
                else:
                    window = []
                matches.append({
                    "file": str(f),
                    "line": idx + 1,
                    "text": text.rstrip(),
                    "context": window,
                })
                if len(matches) >= limit:
                    truncated = True
                    break
            if truncated:
                break
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {
        "ok": True,
        "pattern": pattern,
        "count": len(matches),
        "truncated": truncated,
        "matches": matches,
    }


@function_tool(
    description="创建目录（含父目录）。",
    unit="filesystem",
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
