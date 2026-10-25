"""定时任务路由 —— jobs CRUD + 立即运行（前端「定时任务」页数据源）。

数据真源：~/.omniagent/cron/jobs.json（scheduler 每 tick 热加载，改完即生效）。
注意：scheduler 是网关进程里的线程，本路由跑在 backend 进程——两者通过
jobs.json 文件解耦（backend 只读写文件，不触碰调度器状态）。
"""
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException

from datetime import datetime
from typing import Optional

from gateway import scheduler as cron_store

router = APIRouter(tags=["cron"])


@router.get("/api/cron/jobs")
async def list_jobs() -> Dict[str, Any]:
    """jobs + 派生字段（人话 cron / 下次执行时间+倒计时秒）。"""
    now = datetime.now()
    out = []
    for j in cron_store.load_jobs():
        item = dict(j)
        item["describe"] = cron_store.cron_describe(j.get("cron", ""))
        nxt = cron_store.next_fire_after(j["cron"], now) if j.get("enabled") else None
        item["next_run_at"] = nxt.isoformat(timespec="seconds") if nxt else None
        item["next_in_seconds"] = int((nxt - now).total_seconds()) if nxt else None
        out.append(item)
    return {"ok": True, "jobs": out}


@router.put("/api/cron/jobs")
async def save_jobs(body: Dict[str, Any]) -> Dict[str, Any]:
    """整表保存（与 settings 同款简单语义）。逐条校验+防化石。"""
    jobs: List[Dict[str, Any]] = body.get("jobs", [])
    if not isinstance(jobs, list):
        raise HTTPException(422, "body.jobs 须为数组")
    ids = set()
    for i, job in enumerate(jobs):
        if not isinstance(job, dict):
            raise HTTPException(422, f"jobs[{i}] 须为对象")
        err = cron_store.validate_job(job)
        if err:
            raise HTTPException(422, f"jobs[{i}] {err}")
        if job["id"] in ids:
            raise HTTPException(422, f"jobs[{i}] id 重复: {job['id']}")
        ids.add(job["id"])
    cron_store.save_jobs(jobs)
    return {"ok": True, "count": len(jobs)}


@router.post("/api/cron/jobs/{job_id}/run")
async def run_job(job_id: str) -> Dict[str, Any]:
    """立即运行一次（不经 cron；结果按 job.notify_channel 推送）。

    注意：本接口在 backend 进程内派发，会拉起一个本地 ToolLoop——
    与网关进程的任务互不共享队列（个人使用规模下可接受）。
    """
    task_id = cron_store.run_now(job_id)
    if not task_id:
        raise HTTPException(404, f"job 不存在: {job_id}")
    return {"ok": True, "task_id": task_id}


@router.get("/api/cron/runs")
async def list_runs(job_id: str = "", limit: int = 100) -> Dict[str, Any]:
    """运行记录（新→旧）。job_id 空则全部（「运行记录」总Tab用）。"""
    return {"ok": True, "runs": cron_store.load_runs(job_id=job_id, limit=limit)}


@router.get("/api/cron/projects")
async def list_projects() -> Dict[str, Any]:
    """可选 project 列表（jobs 的 project_id 下拉数据源；取 ~/.omniagent/projects/）。"""
    from pathlib import Path
    proj_dir = Path.home() / ".omniagent" / "projects"
    names = sorted(p.name for p in proj_dir.iterdir() if p.is_dir()) if proj_dir.exists() else []
    if "default" not in names:
        names.insert(0, "default")
    return {"ok": True, "projects": names}
