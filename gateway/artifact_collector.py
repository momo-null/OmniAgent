"""任务产物收编桥（零内核，plan §3 双环的落盘环节）。

沙箱把任务的文件写隔离进任务产物区（``tasks/<tid>/artifacts/``，且相对路径会被
拍平——``corpus/hot.md`` 落成 ``artifacts/hot.md``）：这是任务写隔离的设计使然。
本模块在运行结束后把**语料类产物**归位回项目目录，让下一次觉醒/采集能读到
（``projects/<pid>/corpus|diary/``）。规则窄且只增不删：

- 产物顶层 ``*.md``：
  - 文件名以 ``YYYY-MM-DD`` 开头 → 项目 ``diary/``（运营日记）；
  - 其余 → 项目 ``corpus/``（语料三件套等）；
- 产物子目录 → 项目同名子目录（存在才收，不新建顶层）；
- 非 ``.md`` 文件不收（图片等走别的通路）；永不删除、只覆盖同名。

挂点：``gateway/dispatch._run_task_once`` 收尾（有 project_id 的任务才收）。
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import List

from utils import get_logger

logger = get_logger("gateway.artifact_collector")

_DATE_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}")


def collect(project_id: str, task_id: str, *,
            projects_root: Optional[Path] = None,
            artifacts_root: Optional[Path] = None) -> List[str]:
    """把任务产物区的语料归位进项目目录。返回落位的文件路径列表。"""
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

    def _place(src: Path, target_dir: Path) -> None:
        target_dir.mkdir(parents=True, exist_ok=True)
        dest = target_dir / src.name
        if dest.exists() and dest.read_bytes() == src.read_bytes():
            return  # 内容没变不重复写
        shutil.copy2(src, dest)
        placed.append(str(dest))

    for item in sorted(art.iterdir()):
        if item.is_file() and item.suffix == ".md":
            sub = "diary" if _DATE_PREFIX.match(item.name) else "corpus"
            _place(item, proj / sub)
        elif item.is_dir():
            target_dir = proj / item.name
            if target_dir.is_dir():
                for f in sorted(item.iterdir()):
                    if f.is_file() and f.suffix == ".md":
                        _place(f, target_dir)
    if placed:
        logger.info("产物收编 %s 项 -> %s", len(placed), proj.name)
    return placed
