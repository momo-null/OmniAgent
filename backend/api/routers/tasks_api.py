"""任务 / 项目 / 技能回放接口（零逻辑改动）。"""
from __future__ import annotations

import os

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from backend.api.routers.helpers import (
    _call_skill,
    _is_task_running,
    _paths,
    _project_store,
    _read_skills,
    _running_task_id,
    _snapshot,
    _task_store,
    _try_start_task,
    threading,
)

router = APIRouter(tags=["runtime"])

@router.post("/tasks/{task_id}/open_folder")
async def api_task_open_folder(task_id: str):
    """打开任务落盘目录（用户在任务菜单点「打开文件夹」——人工动作直开，非 agent 工具面）。

    task_id 经 runtime_paths 通用标识符校验（拒路径穿越）；目录不存在返回 404。
    Windows 用 os.startfile，macOS/Linux 用 open/xdg-open。
    """
    try:
        from omni_core.local.runtime_paths import task_dir
        d = task_dir(task_id)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=422)
    if not d.is_dir():
        return JSONResponse({"ok": False, "error": f"任务目录不存在: {task_id}"}, status_code=404)
    try:
        import subprocess
        import sys
        if sys.platform == "win32":
            os.startfile(str(d))  # noqa: S606 - 用户主动点击，路径已过标识符校验
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(d)])
        else:
            subprocess.Popen(["xdg-open", str(d)])
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"打开失败: {e}"}, status_code=500)
    return JSONResponse({"ok": True, "path": str(d)})

@router.get("/projects")
async def list_projects():
    """列出已有的 project（路径 slug）与 task（扁平实体），不写死任何场景。"""
    try:
        from omni_core.local.runtime_paths import projects_root, tasks_root
        projects = sorted(p.name for p in projects_root().iterdir() if p.is_dir())
        tasks = sorted(t.name for t in tasks_root().iterdir() if t.is_dir())
    except Exception:
        projects, tasks = [], []
    return JSONResponse({"projects": projects, "tasks": tasks})

@router.post("/skill/run")
async def api_skill_run(request: Request):
    from backend.api.schemas_runtime import SkillRequest
    from pydantic import ValidationError
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
    try:
        req = SkillRequest.model_validate(payload)
    except ValidationError as e:
        return JSONResponse({"ok": False, "error": f"请求参数校验失败: {e.errors()}"}, status_code=422)
    if not _try_start_task(req.task_id):
        _rid = _running_task_id()
        return JSONResponse({"ok": False, "error": f"已有任务在运行（task_id={_rid}），请等待结束或先停止"}, status_code=409)
    threading.Thread(target=_call_skill, args=(req.task_id, req.skill_name), daemon=True).start()
    return JSONResponse({"ok": True, "msg": f"调用技能 {req.skill_name}"})

@router.post("/skill/delete")
async def api_skill_delete(request: Request):
    from backend.api.schemas_runtime import SkillRequest
    from pydantic import ValidationError
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
    try:
        req = SkillRequest.model_validate(payload)
    except ValidationError as e:
        return JSONResponse({"ok": False, "error": f"请求参数校验失败: {e.errors()}"}, status_code=422)
    try:
        from omni_core.local.skill_library import SkillLibrary
        ok = SkillLibrary(task_id=req.task_id).delete(req.skill_name)
        return JSONResponse({"ok": ok})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

@router.post("/skill/promote")
async def api_skill_promote(request: Request):
    """A7「设为全局」：project 技能显式提升为全局（唯一到 global 的路径，用户手动）。

    project 副本会被移除（global 在 project 内直接可见可用，但 project 优先——
    不移除则提升不可见）。
    """
    from backend.api.schemas_runtime import SkillRequest
    from pydantic import ValidationError
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
    try:
        req = SkillRequest.model_validate(payload)
    except ValidationError as e:
        return JSONResponse({"ok": False, "error": f"请求参数校验失败: {e.errors()}"}, status_code=422)
    try:
        from omni_core.local.skill_library import SkillLibrary
        res = SkillLibrary(task_id=req.task_id).promote_to_global(req.skill_name)
        return JSONResponse(res, status_code=200 if res.get("ok") else 404)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

@router.get("/tasks")
async def list_tasks(state: str = ""):
    """平铺列出 task（按 created_at 倒序），可选 ?state=pending|running|done|failed|aborted。"""
    TaskStore = _task_store()
    tasks = TaskStore.list(state=state or None)
    return JSONResponse({"tasks": tasks, "total": len(tasks)})

@router.get("/tasks/{task_id}")
async def get_task(task_id: str):
    paths = _paths()
    paths.validate_identifier(task_id, "task_id")
    TaskStore = _task_store()
    meta = TaskStore.get(task_id)
    if meta is None:
        return JSONResponse({"ok": False, "error": "task not found"}, status_code=404)
    # 附上快照的资产概览（world / collected / trajectory 行数），便于前端展示
    snap = _snapshot(task_id)
    return JSONResponse({"task": meta, "snapshot": snap})

@router.post("/tasks")
async def create_task(request: Request):
    """创建 task（不自动运行）。body: {objective, done_when?, project_id?}。"""
    from backend.api.schemas_runtime import CreateTaskRequest
    from pydantic import ValidationError
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
    try:
        req = CreateTaskRequest.model_validate(payload)
    except ValidationError as e:
        return JSONResponse({"ok": False, "error": f"请求参数校验失败: {e.errors()}"}, status_code=422)
    TaskStore = _task_store()
    meta = TaskStore.create(
        objective=req.objective,
        done_when=req.done_when,
        project_id=req.project_id,
    )
    return JSONResponse({"ok": True, "task": meta})

@router.post("/tasks/{task_id}/state")
async def update_task_state(task_id: str, request: Request):
    """更新 task 状态 / 字段。body: {state?, success?, ...}。"""
    from backend.api.schemas_runtime import UpdateTaskRequest
    from pydantic import ValidationError
    paths = _paths()
    paths.validate_identifier(task_id, "task_id")
    TaskStore = _task_store()
    if TaskStore.get(task_id) is None:
        return JSONResponse({"ok": False, "error": "task not found"}, status_code=404)
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
    try:
        req = UpdateTaskRequest.model_validate(payload)
    except ValidationError as e:
        return JSONResponse({"ok": False, "error": f"请求参数校验失败: {e.errors()}"}, status_code=422)
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    try:
        meta = TaskStore.update(task_id, **fields)
        return JSONResponse({"ok": True, "task": meta})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

@router.delete("/tasks/{task_id}")
async def delete_task(task_id: str):
    """删除 task：移除索引（task.json）并递归删除其整个 workspace 目录。

    直接清理 ~/.omniagent/tasks/<task_id>/（含 trajectory / world_model /
    collected 等全部资产）。仅接受已存在的 task_id，避免误删。
    C6（knowledge-layering）：连带删除该 task 在项目下的会话 jsonl——
    否则项目目录残留孤儿会话、``session_count`` 虚高。
    """
    paths = _paths()
    paths.validate_identifier(task_id, "task_id")
    TaskStore = _task_store()
    meta = TaskStore.get(task_id)
    if meta is None:
        return JSONResponse({"ok": False, "error": "task not found"}, status_code=404)
    # 安全保护：运行中的任务不允许删除
    if _is_task_running(task_id):
        return JSONResponse({"ok": False, "error": "task 正在运行，不能删除"}, status_code=409)
    try:
        from omni_core.local import runtime_paths as P
        import shutil
        d = P.task_dir(task_id)
        # 安全校验：必须是 tasks_root 的直接子目录，且真实存在
        # 纪律：仅清理 task 级目录（trajectory / world_model / skills 等任务私有资产），
        # 仅清理 task 级目录，不动全局共享资产（技能库 / 画像等）。
        if d.exists() and d.resolve().parent == P.tasks_root().resolve():
            shutil.rmtree(d, ignore_errors=True)
        TaskStore.remove(task_id)
        # C6：删项目下的会话 jsonl（task.json 里记录了归属）
        removed_session = False
        try:
            pid = str(meta.get("project_id") or "")
            sid = str(meta.get("session_id") or "")
            if pid and sid:
                sf = P.session_file(pid, sid)
                if sf.is_file():
                    sf.unlink()
                    removed_session = True
        except Exception:
            pass
        return JSONResponse({"ok": True, "task_id": task_id, "removed_session": removed_session})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

@router.get("/projects/meta")
async def list_projects_meta():
    """返回 project 元数据列表（含 session_count / last_used_at）。

    先做一次会话落点自愈（reconcile）：早期版本保存到项目只改 task.json
    不搬 jsonl，这里按归属把残留会话搬回，session_count 才能反映真实历史。
    """
    ProjectStore = _project_store()
    try:
        ProjectStore.reconcile_sessions()
    except Exception:
        pass
    return JSONResponse({"projects": ProjectStore.list()})

@router.get("/projects/{project_id}/sessions")
async def list_project_sessions(project_id: str):
    paths = _paths()
    paths.validate_identifier(project_id, "project_id")
    ProjectStore = _project_store()
    sessions = ProjectStore.list_sessions(project_id)
    return JSONResponse({"project_id": project_id, "sessions": sessions})

@router.get("/projects/{project_id}/sessions/{session_id}")
async def read_project_session(project_id: str, session_id: str, limit: int = 0):
    paths = _paths()
    paths.validate_identifier(project_id, "project_id")
    paths.validate_identifier(session_id, "session_id")
    ProjectStore = _project_store()
    msgs = ProjectStore.read_session(project_id, session_id, limit=limit or None)
    return JSONResponse({"project_id": project_id, "session_id": session_id, "messages": msgs})

@router.post("/projects")
async def create_project(request: Request):
    """创建项目（C2）：slug 即唯一 id，显示名（别名）存元数据文件。"""
    from backend.api.schemas_runtime import CreateProjectRequest
    from pydantic import ValidationError
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
    try:
        req = CreateProjectRequest.model_validate(payload)
    except ValidationError as e:
        return JSONResponse({"ok": False, "error": f"请求参数校验失败: {e.errors()}"}, status_code=422)
    try:
        ProjectStore = _project_store()
        ProjectStore.ensure(req.project_id)
        if (req.display_name or "").strip():
            ProjectStore.set_display_name(req.project_id, req.display_name.strip())
        return JSONResponse({"ok": True, "id": req.project_id,
                             "display_name": req.display_name or req.project_id})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

@router.put("/projects/{project_id}/alias")
async def set_project_alias(project_id: str, request: Request):
    """改项目显示名（C3）：别名不进目录名，slug 仍是唯一 id（validate_identifier
    拒绝中文/空格，故显示名只能走元数据文件）。"""
    from backend.api.schemas_runtime import ProjectAliasRequest
    from pydantic import ValidationError
    paths = _paths()
    paths.validate_identifier(project_id, "project_id")
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
    try:
        req = ProjectAliasRequest.model_validate(payload)
    except ValidationError as e:
        return JSONResponse({"ok": False, "error": f"请求参数校验失败: {e.errors()}"}, status_code=422)
    try:
        ProjectStore = _project_store()
        ProjectStore.set_display_name(project_id, req.display_name.strip())
        return JSONResponse({"ok": True, "id": project_id, "display_name": req.display_name.strip()})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

@router.delete("/projects/{project_id}")
async def delete_project(project_id: str):
    """删除项目（C5）：**项目是容器，删容器即删内容**。

    先逐个走任务删除语义（含各自的会话 jsonl 清理，C6），再删项目目录
    （含其下全部会话与 project 级知识资产 skills/ memory/——不做"占用可见"
    之类的维护 UI，plan §5）。返回 ``{deleted_tasks: N}`` 供前端提示。
    """
    paths = _paths()
    paths.validate_identifier(project_id, "project_id")
    ProjectStore = _project_store()
    TaskStore = _task_store()
    all_tasks = TaskStore.list()
    owned = [t for t in all_tasks if str(t.get("project_id") or "") == project_id]
    deleted = 0
    errors: list = []
    for t in owned:
        try:
            r = await delete_task(str(t.get("task_id", "")))
            if r.status_code == 200:
                deleted += 1
            else:
                errors.append({"task_id": t.get("task_id"), "error": "delete failed"})
        except Exception as e:
            errors.append({"task_id": t.get("task_id"), "error": str(e)})
    try:
        ProjectStore.remove(project_id)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return JSONResponse({"ok": True, "id": project_id, "deleted_tasks": deleted,
                         "errors": errors})

@router.get("/tasks/{task_id}/skills")
async def list_task_skills(task_id: str):
    paths = _paths()
    paths.validate_identifier(task_id, "task_id")
    return JSONResponse({"task_id": task_id, "skills": _read_skills(task_id)})

@router.get("/tasks/{task_id}/history")
async def get_task_history(task_id: str):
    """获取 task 关联的会话历史（P2.2：刷新/重开后可恢复）。

    从 task 元数据取 project_id + session_id，读 jsonl 返回消息列表。
    """
    paths = _paths()
    paths.validate_identifier(task_id, "task_id")
    TaskStore = _task_store()
    meta = TaskStore.get(task_id)
    if meta is None:
        return JSONResponse({"ok": False, "error": "task not found"}, status_code=404)
    project_id = meta.get("project_id") or ""
    session_id = meta.get("session_id") or ""
    messages: list = []
    if project_id and session_id:
        ProjectStore = _project_store()
        try:
            # coalesce：增量 partial 记录并回单轮（超长 run 重放不碎、不重复）
            messages = ProjectStore.coalesce_records(
                ProjectStore.read_session(project_id, session_id))
        except Exception:
            pass
    return JSONResponse({
        "task_id": task_id,
        "project_id": project_id,
        "session_id": session_id,
        "messages": messages,
    })
