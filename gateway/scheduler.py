"""定时任务调度器（宿主，接入设计 v1.1 §5）。

- jobs 存 ``~/.omniagent/cron/jobs.json``（热加载：每 tick 重读，改文件即生效，前端 CRUD 零重启）
- **project 关联**：job.project_id → dispatch → TaskSpec.project_id →
  三层 AGENTS.md 自动取 projects/<pid>/AGENTS.md，trajectory 按项目落盘
- **防化石铁律**：objective 只许一句话；jobs.json 禁止路径/脚本名/数据源
  （hermes 化石任务实证——细节由被调 MCP server 单点供给）
- 错过补偿：启动时回扫最近触发点，在 miss_grace_minutes 内补跑一次
- cron 表达式：内置 5 字段解析（分 时 日 月 周），支持 * / - , ——零第三方依赖
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

_JOBS_FILE = Path.home() / ".omniagent" / "cron" / "jobs.json"
_STATE_FILE = Path.home() / ".omniagent" / "cron" / "state.json"
_RUNS_FILE = Path.home() / ".omniagent" / "cron" / "runs.jsonl"

DEFAULT_JOBS: List[Dict[str, Any]] = [
    {
        "id": "etf-premarket",
        "name": "ETF盘前分析",
        "cron": "0 8 * * 1-5",
        "objective": "执行ETF盘前分析",
        "done_when": "",  # 空：finish验证器感知不到MCP副作用，完成判据由objective内工具返回自证（勿填"已落盘"类条件，agent会耗尽步数自证）
        "max_steps": 40,
        "project_id": "etf-analysis",
        "notify_channel": "feishu",
        "enabled": False,   # 姐姐确认飞书通道跑通后自行启用
        "miss_grace_minutes": 60,
        "note": "P0 三段之一；启用前确认 mcp.json 已注册 etf-analysis",
    },
    {
        "id": "etf-midday",
        "name": "ETF午盘简报",
        "cron": "35 11 * * 1-5",
        "objective": "执行ETF午盘分析",
        "done_when": "",  # 空：finish验证器感知不到MCP副作用，完成判据由objective内工具返回自证（勿填"已落盘"类条件，agent会耗尽步数自证）
        "max_steps": 30,
        "project_id": "etf-analysis",
        "notify_channel": "feishu",
        "enabled": False,
        "miss_grace_minutes": 45,
        "note": "",
    },
    {
        "id": "etf-close",
        "name": "ETF收盘分析",
        "cron": "35 15 * * 1-5",
        "objective": "执行ETF收盘分析",
        "done_when": "",  # 空：finish验证器感知不到MCP副作用，完成判据由objective内工具返回自证（勿填"已落盘"类条件，agent会耗尽步数自证）
        "max_steps": 40,
        "project_id": "etf-analysis",
        "notify_channel": "feishu",
        "enabled": False,
        "miss_grace_minutes": 120,
        "note": "",
    },
]

_FOSSIL_BANNED = ("D:\\", "D:/", "python", ".py", "/mnt/", "http")


# ─────────────────────────── jobs 存取（后端 API 共用） ───────────────────────────

def load_jobs() -> List[Dict[str, Any]]:
    if not _JOBS_FILE.exists():
        _JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _JOBS_FILE.write_text(json.dumps({"jobs": DEFAULT_JOBS}, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        data = json.loads(_JOBS_FILE.read_text(encoding="utf-8"))
        return data.get("jobs", [])
    except Exception:
        return []


def save_jobs(jobs: List[Dict[str, Any]]) -> None:
    _JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _JOBS_FILE.write_text(json.dumps({"jobs": jobs}, ensure_ascii=False, indent=1), encoding="utf-8")


def validate_job(job: Dict[str, Any]) -> Optional[str]:
    """校验+防化石：返回错误信息或 None。"""
    for key in ("id", "cron", "objective"):
        if not str(job.get(key, "")).strip():
            return f"缺字段或为空: {key}"
    hits = [b for b in _FOSSIL_BANNED if b in str(job.get("objective", ""))]
    if hits:
        return f"防化石铁律：objective 禁止包含路径/脚本名（命中 {hits}）——细节由 MCP/工具单点供给"
    if not cron_match(job["cron"], datetime.now()):
        pass  # 语法错误在 cron_match 内部抛 ValueError，由调用方接
    return None


def _load_state() -> Dict[str, str]:
    try:
        return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: Dict[str, str]) -> None:
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


# ─────────────────────────── 内置 cron 解析（5字段，零依赖） ───────────────────────────

def _parse_field(expr: str, lo: int, hi: int) -> set:
    out = set()
    for part in expr.split(","):
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            step = int(step_s)
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            a_s, b_s = part.split("-", 1)
            a, b = int(a_s), int(b_s)
        else:
            a = b = int(part)
            if step != 1:
                b = hi  # "n/step" 语义 = 从 n 到上限每 step
        for v in range(a, b + 1, step):
            if lo <= v <= hi:
                out.add(v)
    return out


def cron_match(expr: str, dt: datetime) -> bool:
    """标准 5 字段（分 时 日 月 周，周日=0/7）。语义：日与周都限定时取**并集**（标准 cron）。非法表达式抛 ValueError。"""
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(f"cron 须为 5 字段: {expr!r}")
    minute, hour, dom, month, dow = fields
    dow = dow.replace("7", "0")
    if not getattr(cron_match, "_cache", None):
        cron_match._cache = {}  # type: ignore[attr-defined]
    sets = cron_match._cache.get(expr)  # type: ignore[attr-defined]
    if sets is None:
        sets = (_parse_field(minute, 0, 59), _parse_field(hour, 0, 23),
                _parse_field(dom, 1, 31), _parse_field(month, 1, 12),
                _parse_field(dow, 0, 6))
        cron_match._cache[expr] = sets  # type: ignore[attr-defined]
    m_s, h_s, dom_s, mon_s, dow_s = sets
    if dt.minute not in m_s or dt.hour not in h_s or dt.month not in mon_s:
        return False
    dow_cron = (dt.weekday() + 1) % 7  # cron 0=周日 ↔ Python weekday() 0=周一
    dom_restricted, dow_restricted = dom != "*", dow != "*"
    if dom_restricted and dow_restricted:
        return dt.day in dom_s or dow_cron in dow_s
    return dt.day in dom_s and dow_cron in dow_s  # 单边限定时另一边为全集，等价于只看限定边


def last_fire_before(expr: str, now: datetime, scan_hours: int = 36) -> Optional[datetime]:
    """回扫最近一次应触发时刻（错过补偿用）。"""
    t = now.replace(second=0, microsecond=0) - timedelta(minutes=1)
    for _ in range(scan_hours * 60):
        try:
            if cron_match(expr, t):
                return t
        except ValueError:
            return None
        t -= timedelta(minutes=1)
    return None


def next_fire_after(expr: str, now: datetime, scan_hours: int = 36) -> Optional[datetime]:
    """前扫下一次应触发时刻（前端「N 分钟后执行」用）。"""
    t = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(scan_hours * 60):
        try:
            if cron_match(expr, t):
                return t
        except ValueError:
            return None
        t += timedelta(minutes=1)
    return None


def cron_describe(expr: str) -> str:
    """cron → 人话（卡片展示用；识别不了的回原文）。"""
    try:
        f = expr.split()
        if len(f) != 5:
            return expr
        mi, h, dom, mon, dow = f
        hm = f"{int(h):02d}:{int(mi):02d}" if h != "*" and mi != "*" else ""
        if dom == "*" and mon == "*" and dow == "*" and h == "*" and mi.startswith("*/"):
            return f"每 {mi[2:]} 分钟"
        if dom == "*" and mon == "*" and dow == "*" and h != "*" and mi == "*":
            return f"每小时（第 {h} 分）"
        if dom == "*" and mon == "*" and dow in ("1-5",):
            return f"工作日 {hm}" if hm else expr
        if dom == "*" and mon == "*" and dow == "*":
            return f"每天 {hm}" if hm else expr
        if dom == "*" and mon == "*" and dow == "0,6":
            return f"每周末 {hm}" if hm else expr
        return expr
    except Exception:
        return expr


def record_run(job_id: str, task_id: str, status: str, summary: str = "", error: str = "") -> None:
    """运行记录（前端「运行记录」时间线数据源）。追加式 jsonl。"""
    rec = {"ts": datetime.now().isoformat(timespec="seconds"), "job_id": job_id,
           "task_id": task_id, "status": status, "summary": summary[:800], "error": error[:300]}
    try:
        _RUNS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _RUNS_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def load_runs(job_id: str = "", limit: int = 100) -> List[Dict[str, Any]]:
    if not _RUNS_FILE.exists():
        return []
    recs: List[Dict[str, Any]] = []
    try:
        for line in _RUNS_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if not job_id or r.get("job_id") == job_id:
                recs.append(r)
    except Exception:
        pass
    return recs[-limit:][::-1]  # 新→旧


# ─────────────────────────── 调度主循环 ───────────────────────────

class Scheduler:
    def __init__(self, submit_fn):
        self._submit = submit_fn
        self._state = _load_state()
        self._last_minute_key = ""
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self) -> None:
        self._catchup()
        self._thread = threading.Thread(target=self._loop, name="cron-scheduler", daemon=True)
        self._thread.start()

    # ── 错过补偿：启动回扫 ──
    def _catchup(self) -> None:
        now = datetime.now()
        for job in load_jobs():
            if not job.get("enabled"):
                continue
            try:
                due = last_fire_before(job["cron"], now)
            except ValueError:
                continue
            if not due:
                continue
            last = self._state.get(job["id"], "")
            due_key = due.strftime("%Y-%m-%d %H:%M")
            if due_key <= last:
                continue  # 该次已执行
            grace = float(job.get("miss_grace_minutes", 60))
            if (now - due).total_seconds() / 60 > grace:
                continue  # 超窗跳过（下次触发日志可见）
            self._fire(job, due_key, source="catchup")

    def _loop(self) -> None:
        while not self._stop.wait(20):
            now = datetime.now()
            minute_key = now.strftime("%Y-%m-%d %H:%M")
            if minute_key == self._last_minute_key:
                continue
            self._last_minute_key = minute_key
            for job in load_jobs():  # 每 tick 热加载（前端改 jobs 即生效）
                if not job.get("enabled"):
                    continue
                try:
                    hit = cron_match(job["cron"], now)
                except ValueError as e:
                    print(f"[scheduler] job {job.get('id')} cron 非法: {e}")
                    continue
                if hit and self._state.get(job["id"], "") < minute_key:
                    self._fire(job, minute_key, source="cron")

    def _fire(self, job: Dict[str, Any], fire_key: str, source: str) -> None:
        print(f"[scheduler] 触发 {job.get('name', job['id'])}（{source}）objective={job['objective'][:40]}")
        try:
            self._submit(
                job["objective"],
                job.get("done_when", ""),
                int(job.get("max_steps", 40)),
                source=f"cron:{job['id']}",
                project_id=job.get("project_id") or None,
                notify_channel=job.get("notify_channel") or None,
            )
        finally:
            self._state[job["id"]] = fire_key
            _save_state(self._state)

    def stop(self) -> None:
        self._stop.set()


def run_now(job_id: str) -> Optional[str]:
    """「立即运行」（前端按钮/调试用）：绕过 cron 直接派发。返回 task_id 或 None。"""
    from . import dispatch
    job = next((j for j in load_jobs() if j["id"] == job_id), None)
    if not job:
        return None
    return dispatch.submit(
        job["objective"], job.get("done_when", ""), int(job.get("max_steps", 40)),
        source=f"manual:{job_id}", project_id=job.get("project_id") or None,
        notify_channel=job.get("notify_channel") or None, job_id=job_id,
    )
