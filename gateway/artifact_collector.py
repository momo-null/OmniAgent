"""任务产物收编桥（零内核，plan §3 双环的落盘环节）。

沙箱把任务的文件写隔离进任务产物区（``tasks/<tid>/artifacts/``），本模块在运行
结束后把产物按**任务自己写的相对路径**原样归位回项目目录
（``projects/<pid>/corpus|diary|...``）。

落位由任务决定，本模块不猜、不改写：
- ``artifacts/corpus/hot.md`` → ``projects/<pid>/corpus/hot.md``
- ``artifacts/diary/2026-11-09-觉醒.md`` → ``projects/<pid>/diary/2026-11-09-觉醒.md``
- 任意新子目录（``knowledge/``、``days/``）同样按原路径创建

规则只增不删：永不删除项目内既有文件、内容相同不重复写。

挂点：``gateway/dispatch._run_task_once`` 收尾（有 project_id 的任务才收）。
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import List, Optional

from utils import get_logger

logger = get_logger("gateway.artifact_collector")


def collect(project_id: str, task_id: str, *,
            projects_root: Optional[Path] = None,
            artifacts_root: Optional[Path] = None) -> List[str]:
    """把任务产物区的文件按相对路径归位进项目目录。返回落位的文件路径列表。"""
    from omni_core.local.runtime_paths import project_dir, task_artifacts_dir

    try:
        art = Path(artifacts_root) if artifacts_root else task_artifacts_dir(task_id)
        # projects_root 覆盖的是「根」：项目目录仍要拼上 project_id（测试注入用）
        proj = (Path(projects_root) / project_id) if projects_root else project_dir(project_id)
    except Exception:
        return []
    if not (art.is_dir() and proj.is_dir()):
        return []

    placed: List[str] = []
    for item in sorted(art.rglob("*")):
        try:
            if not item.is_file():
                continue
            rel = item.relative_to(art)
            if ".." in rel.parts:  # 防御：产物区外路径一律不碰
                continue
            dest = proj / rel
            if dest.exists() and dest.read_bytes() == item.read_bytes():
                continue  # 内容没变不重复写
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, dest)
            placed.append(str(dest))
        except Exception as e:  # noqa: BLE001 —— 收编是增强项，单项失败不影响其余
            logger.warning("产物归位失败 %s: %s: %s", item, type(e).__name__, e)

    if placed:
        logger.info("产物归位 %s 项 -> %s", len(placed), proj.name)
    return placed
