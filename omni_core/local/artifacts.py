"""任务产物登记表（artifacts registry）。

设计：
- agent 通过工具**实际写出**的文件（write_file / edit_file / 截图等）在此登记：
  ``tasks/<task_id>/artifacts.json``，条目 ``{path, name, bytes, tool, ts}``，按 path 去重（后写覆盖）；
- ``tmp/`` 里的产物默认随任务终态清理（graph_runner），清理前 ``preserve_tmp_artifacts``
  把登记过的 tmp 文件**抢救复制**到持久目录 ``tasks/<task_id>/artifacts/``，并回写登记表路径——
  会话结束后前端仍能在右侧栏预览这些产物（会话结果卡展示的也是登记表）；
- 登记以「工具真实落盘」为准（不猜 shell 重定向），写失败/未落盘不登记；
- 未绑定 task（ContextVar 无 task_id）时静默跳过，绝不影响工具主流程。

本模块只被工具层（plugins/environments）与 graph_runner 清理路径调用，全部 try 兜底。
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List

from omni_core.local.runtime_paths import (
    task_artifacts_dir,
    task_artifacts_json,
    task_tmp,
)

#: 登记表容量上限（按 path 去重后的条目数；超出丢最旧）
_MAX_ENTRIES = 200


def _registry(task_id: str) -> Path:
    return task_artifacts_json(task_id)


def load_artifacts(task_id: str) -> List[Dict[str, Any]]:
    """读取某任务的产物登记表（文件不存在 / 损坏返回空列表）。"""
    if not task_id:
        return []
    try:
        raw = _registry(task_id).read_text(encoding="utf-8")
        data = json.loads(raw)
        items = data.get("artifacts") if isinstance(data, dict) else data
        return [a for a in (items or []) if isinstance(a, dict)]
    except Exception:
        return []


def _save(task_id: str, entries: List[Dict[str, Any]]) -> None:
    path = _registry(task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"artifacts": entries[-_MAX_ENTRIES:]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def record_artifact(path: str, tool: str = "", task_id: str = "") -> Optional[Dict[str, Any]]:
    """登记一个 agent 刚写出的文件。

    Args:
        path: 文件路径（相对路径由调用方先解析成绝对路径再传入）。
        tool: 产生该文件的工具名（write_file / edit_file / screenshot …）。
        task_id: 显式 task；留空时取当前 ContextVar 绑定的任务。

    Returns:
        登记成功的条目；未绑定任务 / 文件不存在等情况下返回 None（静默）。
    """
    tid = (task_id or "").strip()
    if not tid:
        try:
            from omni_core.tools.workspace import current_task
            tid = current_task() or ""
        except Exception:
            tid = ""
    if not tid:
        return None
    try:
        p = Path(path)
        if not p.is_absolute():
            return None
        if not p.is_file():
            return None
        entry = {
            "path": str(p),
            "name": p.name,
            "bytes": int(p.stat().st_size),
            "tool": str(tool or ""),
            "ts": round(time.time(), 3),
        }
        entries = load_artifacts(tid)
        entries = [e for e in entries if str(e.get("path") or "") != str(p)]
        entries.append(entry)
        _save(tid, entries)
        return entry
    except Exception:
        return None


def preserve_tmp_artifacts(task_id: str) -> None:
    """任务终态：把登记表中落在 ``tmp/`` 里的产物复制到持久 ``artifacts/`` 并回写路径。

    只处理登记过的文件（登记 = agent 主动写出的产物）；tmp 里未登记的中间文件
    仍按原语义删除。复制冲突按 ``<stem>-2<ext>`` 顺延，绝不覆盖。
    """
    if not task_id:
        return
    try:
        tmp_root = task_tmp(task_id).resolve()
        dest_root = task_artifacts_dir(task_id)
        entries = load_artifacts(task_id)
        if not entries:
            return
        changed = False
        for e in entries:
            try:
                raw = str(e.get("path") or "")
                if not raw:
                    continue
                p = Path(raw)
                if not p.is_file():
                    continue
                rp = p.resolve()
                if rp != tmp_root and tmp_root not in rp.parents:
                    continue  # 不在 tmp/ 下的产物原样保留（登记路径即最终路径）
                dest_root.mkdir(parents=True, exist_ok=True)
                dest = dest_root / p.name
                k = 2
                while dest.exists():
                    dest = dest_root / f"{p.stem}-{k}{p.suffix}"
                    k += 1
                shutil.copy2(str(rp), str(dest))
                e["path"] = str(dest)
                e["name"] = dest.name
                e["bytes"] = int(dest.stat().st_size)
                changed = True
            except Exception:
                continue
        if changed:
            _save(task_id, entries)
    except Exception:
        pass
