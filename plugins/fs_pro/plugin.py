"""fs_pro：文件系统增强插件（分页读取 + 带上下文检索）。

定位（见 doc/plans/tool-plugin-master-plan.md）：把「工具能力增强」做成
放一个目录的插件事务——不碰内核，只新增工具。

- 入口与 builtin 完全同构：模块级 ``@function_tool`` + Google docstring 出 schema；
- ``configure(cfg)`` 读 ``config.runtime.plugins.fs_pro`` 的私有节。
"""
from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Any, Dict, List

from omni_core.tools.base import function_tool

#: 插件私有配置（configure 覆盖；缺省值以本文件为唯一出处）
_CFG: Dict[str, Any] = {"max_output": 8000}

#: 检索时跳过的通用基础设施目录（与业务无关，避免扫依赖树）
_SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", ".idea"}


def configure(cfg: dict) -> None:
    """读取插件私有配置节 ``config.runtime.plugins.fs_pro``。

    Args:
        cfg: 该插件的私有配置（缺省空 dict）。
    """
    cfg = cfg or {}
    try:
        _CFG["max_output"] = int(cfg.get("max_output", 8000) or 8000)
    except (TypeError, ValueError):
        _CFG["max_output"] = 8000


@function_tool(description="按行区间读取文件的指定片段（大文件分页读取，避免一次性灌满上下文）", group="fs_pro")
def read_range(path: str, offset: int = 0, limit: int = 200) -> dict:
    """按行区间读取文件片段。

    Args:
        path: 文件路径。
        offset: 起始行下标（0 起，含）。
        limit: 最多读取的行数。
    """
    try:
        target = Path(path).expanduser()
        if not target.is_file():
            return {"ok": False, "error": f"文件不存在或不是普通文件: {path}"}
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        total = len(lines)
        start = max(0, int(offset or 0))
        count = max(0, int(limit if limit is not None else 200))
        end = min(total, start + count)
        content = "\n".join(
            f"{start + i + 1:>6}\t{line}" for i, line in enumerate(lines[start:end])
        )
        max_output = int(_CFG.get("max_output") or 8000)
        if len(content) > max_output:
            content = content[:max_output]
        return {
            "ok": True,
            "path": str(target),
            "total_lines": total,
            "offset": start,
            "end_line": end,
            "has_more": end < total,
            "content": content,
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@function_tool(description="递归正则检索文件内容，并返回每个命中行前后的上下文行", group="fs_pro")
def search_with_context(
    pattern: str,
    path: str = ".",
    glob: str = "*",
    context: int = 2,
    max_matches: int = 20,
) -> dict:
    """递归正则检索并带上下文返回命中。

    Args:
        pattern: 正则表达式。
        path: 起始目录或单个文件。
        glob: 文件名过滤（fnmatch 语法，如 "*.py"）。
        context: 每个命中返回的前后上下文行数。
        max_matches: 最多返回的命中数。
    """
    try:
        rx = re.compile(pattern)
    except re.error as e:
        return {"ok": False, "error": f"正则表达式非法: {e}"}

    try:
        root = Path(path).expanduser()
        if root.is_file():
            files: List[Path] = [root]
        elif root.is_dir():
            files = sorted(
                (p for p in root.rglob("*") if p.is_file()),
                key=lambda p: str(p),
            )
        else:
            return {"ok": False, "error": f"路径不存在: {path}"}

        ctx_lines = max(0, int(context or 0))
        limit = max(1, int(max_matches or 20))
        matches: List[Dict[str, Any]] = []
        truncated = False

        for file in files:
            if any(part in _SKIP_DIRS or part.startswith(".") for part in file.parts[:-1]):
                continue
            if not fnmatch.fnmatch(file.name, glob):
                continue
            try:
                lines = file.read_text(encoding="utf-8", errors="replace").splitlines()
            except Exception:
                continue
            for idx, text in enumerate(lines):
                if not rx.search(text):
                    continue
                lo = max(0, idx - ctx_lines)
                hi = min(len(lines), idx + ctx_lines + 1)
                matches.append(
                    {
                        "file": str(file),
                        "line": idx + 1,
                        "text": text,
                        "context": [
                            {"line": i + 1, "text": lines[i]} for i in range(lo, hi)
                        ],
                    }
                )
                if len(matches) >= limit:
                    truncated = True
                    break
            if truncated:
                break

        return {
            "ok": True,
            "pattern": pattern,
            "count": len(matches),
            "truncated": truncated,
            "matches": matches,
        }
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
