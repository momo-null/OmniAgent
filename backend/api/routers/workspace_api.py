"""右侧栏工作区 API（产物 / 文件预览 / git 变更 / 终端）。

供 Web 右侧栏（RightDock）消费：
- ``GET  /artifacts``：任务产物登记表（agent 写出的文件清单，会话卡「产物」同源）；
- ``GET  /files/read``：产物文本预览（限该任务登记过的文件 + 任务目录树内文件）；
- ``GET  /files/raw``：产物原文件流（图片预览 / 下载；同一允许集）；
- ``GET  /git/status`` / ``GET /git/diff``：工作区 git 变更（用户自选仓库路径）；
- ``POST /terminal``：终端命令执行（与 shell 工具同一子进程模式，带墙钟超时）。

安全口径（与内核一致，本地单机应用）：
- files/* 只服务「该任务产物登记表 + 该任务目录树」内的路径（任务隔离，拒路径穿越）；
- git/* / terminal 接受用户给的路径并真实执行 git/命令——与 shell_exec 同风险档，
  本应用本身就是宿主机 agent 控制台，不另设沙箱。
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse

router = APIRouter(tags=["workspace"])

_TEXT_EXTS = {
    ".md", ".markdown", ".txt", ".log", ".json", ".jsonl", ".yaml", ".yml", ".toml",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".css", ".html", ".htm", ".xml", ".csv",
    ".sh", ".bat", ".cmd", ".ps1", ".sql", ".ini", ".cfg", ".conf", ".env",
    ".go", ".rs", ".java", ".c", ".h", ".cpp", ".hpp", ".cs", ".rb", ".php",
}
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".svg"}

_READ_MAX_CHARS = 200_000
_DIFF_MAX_CHARS = 120_000


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[截断，共 {len(text)} 字符]"


def _run(argv: List[str], cwd: Optional[str] = None, timeout: float = 15.0) -> Dict[str, Any]:
    """跑一条外部命令（列表 argv，不经过 shell），统一超时与编码兜底。"""
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, cwd=cwd,
            shell=False, encoding="utf-8", errors="replace",
        )
        return {"code": proc.returncode, "out": proc.stdout or "", "err": proc.stderr or ""}
    except subprocess.TimeoutExpired:
        return {"code": -1, "out": "", "err": f"执行超时（>{timeout}s）"}
    except FileNotFoundError:
        return {"code": -1, "out": "", "err": f"未找到可执行文件: {argv[0]}"}
    except Exception as e:
        return {"code": -1, "out": "", "err": f"{type(e).__name__}: {e}"}


# ---- 允许集解析：任务产物登记表 + 任务目录树 --------------------------------

def _resolve_preview_target(task_id: str, path: str) -> Optional[Path]:
    """校验 (task_id, path) 是否可预览：文件必须存在，且

    - 位于该任务登记的产物路径中（含 tmp 抢救后的 artifacts/ 落位），或
    - 位于该任务目录树（tasks/<task_id>/ 任意子路径，含 tmp / artifacts）。
    """
    from omni_core.local.runtime_paths import task_dir, validate_identifier
    try:
        validate_identifier(task_id, "task_id")
    except ValueError:
        return None
    p = Path(path or "").expanduser()
    if not p.is_absolute() or not p.is_file():
        return None
    try:
        rp = p.resolve()
    except Exception:
        return None
    # 1) 产物登记表命中
    try:
        from omni_core.local.artifacts import load_artifacts
        for e in load_artifacts(task_id):
            try:
                if Path(str(e.get("path") or "")).resolve() == rp:
                    return rp
            except Exception:
                continue
    except Exception:
        pass
    # 2) 任务目录树内
    try:
        root = task_dir(task_id).resolve()
        if rp == root or root in rp.parents:
            return rp
    except Exception:
        pass
    return None


# ---- 产物登记表 --------------------------------------------------------------

@router.get("/artifacts")
async def list_artifacts(task_id: str = ""):
    """返回任务产物登记表（右侧栏「文件」页与结果卡「产物」同源）。"""
    from omni_core.local.artifacts import load_artifacts
    from omni_core.local.runtime_paths import validate_identifier
    try:
        validate_identifier(task_id, "task_id")
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=422)
    entries = load_artifacts(task_id)
    return JSONResponse({"ok": True, "task_id": task_id, "artifacts": entries})


# ---- 文件预览 ----------------------------------------------------------------

@router.get("/files/read")
async def read_file_preview(task_id: str = "", path: str = ""):
    """读取产物文件文本内容（限允许集）；图片/二进制只回元信息，内容走 /files/raw。"""
    rp = _resolve_preview_target(task_id, path)
    if rp is None:
        return JSONResponse({"ok": False, "error": "文件不存在或不在当前任务允许集内"}, status_code=404)
    ext = rp.suffix.lower()
    try:
        stat = rp.stat()
        meta = {
            "ok": True, "path": str(rp), "name": rp.name,
            "size": int(stat.st_size), "mtime": int(stat.st_mtime),
            "kind": "text",
        }
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=500)
    if ext in _IMAGE_EXTS:
        meta["kind"] = "image"
        return JSONResponse(meta)
    head = b""
    try:
        with open(rp, "rb") as f:
            head = f.read(8192)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=500)
    if b"\x00" in head:
        meta["kind"] = "binary"
        return JSONResponse(meta)
    try:
        text = rp.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=500)
    meta["truncated"] = len(text) > _READ_MAX_CHARS
    meta["content"] = _clip(text, _READ_MAX_CHARS)
    return JSONResponse(meta)


@router.get("/files/raw")
async def read_file_raw(task_id: str = "", path: str = "", download: int = 0):
    """产物原文件流（<img> 预览 / 下载）；允许集与 /files/read 一致。"""
    rp = _resolve_preview_target(task_id, path)
    if rp is None:
        return JSONResponse({"ok": False, "error": "文件不存在或不在当前任务允许集内"}, status_code=404)
    return FileResponse(
        str(rp),
        filename=rp.name if download else None,
        content_disposition_type="attachment" if download else "inline",
    )


# ---- git 变更 ----------------------------------------------------------------

_PORCELAIN_LABEL = {
    "M": "修改", "A": "新增", "D": "删除", "R": "重命名", "C": "复制",
    "U": "冲突", "?": "未跟踪", "!": "忽略", "T": "类型变更",
}


def _git_toplevel(repo: Path) -> Optional[Path]:
    r = _run(["git", "rev-parse", "--show-toplevel"], cwd=str(repo))
    if r["code"] == 0:
        top = r["out"].strip()
        if top:
            return Path(top)
    return None


@router.get("/git/status")
async def git_status(path: str = ""):
    """git 仓库概览：分支 + porcelain 变更清单（用户自选仓库路径）。"""
    repo = Path(path or "").expanduser()
    if not repo.is_dir():
        return JSONResponse({"ok": False, "error": f"目录不存在: {repo}"}, status_code=404)
    top = _git_toplevel(repo)
    if top is None:
        return JSONResponse({"ok": True, "is_repo": False, "path": str(repo)})
    branch = ""
    rb = _run(["git", "branch", "--show-current"], cwd=str(top))
    if rb["code"] == 0:
        branch = rb["out"].strip()
    st = _run(["git", "status", "--porcelain=v1"], cwd=str(top), timeout=20.0)
    entries: List[Dict[str, str]] = []
    if st["code"] == 0:
        for line in st["out"].splitlines():
            if len(line) < 4:
                continue
            x, y, file = line[0], line[1], line[3:]
            # 重命名形如 "R  old -> new"，展示取新名
            if " -> " in file:
                file = file.split(" -> ", 1)[1]
            label = _PORCELAIN_LABEL.get(x, "") or _PORCELAIN_LABEL.get(y, "") or "变更"
            entries.append({"x": x, "y": y, "file": file.strip(), "label": label})
    return JSONResponse({
        "ok": True, "is_repo": True, "path": str(repo), "repo": str(top),
        "branch": branch, "entries": entries, "error": st["err"].strip(),
    })


@router.get("/git/diff")
async def git_diff(path: str = "", file: str = ""):
    """单文件 diff（含暂存；未跟踪文件合成「新文件」伪 diff）。"""
    repo = Path(path or "").expanduser()
    if not repo.is_dir():
        return JSONResponse({"ok": False, "error": f"目录不存在: {repo}"}, status_code=404)
    top = _git_toplevel(repo)
    if top is None:
        return JSONResponse({"ok": False, "error": "该目录不是 git 仓库"}, status_code=400)
    rel = (file or "").strip().strip('"')
    if not rel:
        return JSONResponse({"ok": False, "error": "缺少 file 参数"}, status_code=422)
    # 防参数注入：文件名以 "-" 开头一律拒绝（不会被当成 git 选项）
    if rel.startswith("-"):
        return JSONResponse({"ok": False, "error": "非法文件名"}, status_code=422)
    st = _run(["git", "status", "--porcelain=v1", "--", rel], cwd=str(top))
    if st["code"] != 0:
        return JSONResponse({"ok": False, "error": st["err"].strip() or "git status 失败"}, status_code=500)
    if st["out"].startswith("??"):
        # 未跟踪：合成新增伪 diff
        fpath = top / rel
        body = ""
        try:
            if fpath.is_file():
                body = fpath.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            body = f"（读取失败: {e}）"
        lines = "".join(f"+{ln}\n" for ln in body.splitlines()[:5000])
        diff = f"diff --git a/{rel} b/{rel}\nnew file\n--- /dev/null\n+++ b/{rel}\n{lines}"
        return JSONResponse({"ok": True, "file": rel, "untracked": True,
                             "diff": _clip(diff, _DIFF_MAX_CHARS)})
    d = _run(["git", "diff", "HEAD", "--unified=3", "--", rel], cwd=str(top), timeout=20.0)
    if d["code"] != 0:
        # 无 HEAD（空仓库）等场景退回普通 diff
        d = _run(["git", "diff", "--unified=3", "--", rel], cwd=str(top), timeout=20.0)
    text = (d["out"] or "").strip()
    if not text:
        text = d["err"].strip() or "（无变更内容）"
    return JSONResponse({"ok": True, "file": rel, "untracked": False,
                         "diff": _clip(text, _DIFF_MAX_CHARS)})


# ---- 终端 --------------------------------------------------------------------

_SHELLS = {
    "cmd": ["cmd.exe", "/c"],
    "powershell": ["powershell.exe", "-NoProfile", "-Command"],
    "bash": ["bash", "-c"],
}


@router.post("/terminal")
async def api_terminal(request: Request):
    """右侧栏终端：执行一条命令并回传输出（每次独立子进程，带墙钟超时）。

    body: {command, cwd?, shell?, timeout_sec?}。cwd 缺省 = 后端进程工作目录。
    """
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
    command = str((payload or {}).get("command") or "")
    if not command.strip():
        return JSONResponse({"ok": False, "error": "command 为空"}, status_code=422)
    shell = str((payload or {}).get("shell") or ("powershell" if sys.platform == "win32" else "bash"))
    if shell not in _SHELLS:
        return JSONResponse({"ok": False, "error": f"不支持的 shell: {shell}，可选 {list(_SHELLS)}"},
                            status_code=422)
    cwd = Path(str((payload or {}).get("cwd") or "")).expanduser()
    if not str(cwd):
        cwd = Path.cwd()
    if not cwd.is_dir():
        return JSONResponse({"ok": False, "error": f"工作目录不存在: {cwd}"}, status_code=422)
    try:
        timeout = float((payload or {}).get("timeout_sec") or 60)
    except (TypeError, ValueError):
        timeout = 60.0
    timeout = max(1.0, min(timeout, 300.0))
    t0 = time.time()
    r = _run(_SHELLS[shell] + [command], cwd=str(cwd), timeout=timeout)
    stdout = (r["out"] or "").rstrip()
    stderr = (r["err"] or "").rstrip()
    output = stdout or stderr
    return JSONResponse({
        "ok": r["code"] == 0,
        "returncode": r["code"],
        "stdout": stdout[-100_000:],
        "stderr": stderr[-40_000:],
        "output": output[-100_000:],
        "cwd": str(cwd),
        "elapsed_ms": int((time.time() - t0) * 1000),
    })
