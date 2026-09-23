"""X3 运行控制台 · 运行时路由共享辅助层（由 router_runtime.py 拆出，零逻辑改动）。

集中承载所有路由共用的 store 单例、outbox 读写、配置快照、push_* SSE 事件、
消息/步骤格式化、记忆/快照/技能数据读取等模块级函数与全局量。
"""
from __future__ import annotations

import asyncio
import collections
import copy
import itertools
import json
import os
import re
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

import config  # 全局配置（config.yaml + ~/.omniagent/config.yaml 合并）
from omni_core.tools.base import TOOL_REGISTRY  # 运行体携带的工具 registry
from backend.api.runtime_manager import manager, AGENT_MAIN

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CONFIG_PATH = ROOT / "config.yaml"

_traj_cursor: Dict[str, Any] = {}
_cursor_lock = threading.Lock()

def _task_store():
    from omni_core.local.task_store import TaskStore
    return TaskStore

def _project_store():
    from omni_core.local.task_store import ProjectStore
    return ProjectStore

def _paths():
    """runtime_paths 模块（用于路由层校验标识符）。"""
    from omni_core.local import runtime_paths
    return runtime_paths

def _ensure_outbox(task_id: str) -> Dict[str, "collections.deque"]:
    return manager.ensure_outbox(task_id)

# ── 当前轮「实时过程快照」（刷新 / 切任务后回放） ─────────────────────────────
# outbox 是「消费即清空」的瞬时队列（见 chat_runtime._stream_gen），且实时过程不落盘；
# 因此页面刷新或切换 task 后，进行中的这一轮没有任何可回放来源（前端 processLogs 空，
# history jsonl 里也没有该轮）。这里另存一份**按 id upsert 的有界快照**，由
# GET /api/runtime/live 提供给前端在挂载/切任务时回放——不改 SSE 协议本身。
_LIVE_CHANNELS = ("thinking", "message", "toolcall")
_LIVE_LIMIT = 200                     # 每通道保留条数上限（超出丢最旧）
_LIVE_SNAPSHOT: Dict[str, Dict[str, "collections.OrderedDict"]] = {}
_live_lock = threading.RLock()        # 写入在 agent 线程、读取在请求线程
_live_seq = itertools.count(1)        # 给无 id 的一次性事件补稳定 key（toolcall 等）


def _live_box(task_id: str) -> Dict[str, "collections.OrderedDict"]:
    """取（必要时新建）某 task 的 live 快照容器。"""
    tid = task_id or "_global"
    with _live_lock:
        box = _LIVE_SNAPSHOT.get(tid)
        if box is None:
            box = {k: collections.OrderedDict() for k in _LIVE_CHANNELS}
            _LIVE_SNAPSHOT[tid] = box
        return box


def _live_push(task_id: str, channel: str, key: str, entry: dict) -> None:
    """把一条过程项写入 live 快照（同 key 原地覆盖，超限丢最旧）。"""
    box = _live_box(task_id)
    with _live_lock:
        d = box[channel]
        d[key] = entry
        while len(d) > _LIVE_LIMIT:
            d.popitem(last=False)


def live_snapshot(task_id: str) -> Dict[str, Any]:
    """返回某 task 当前轮的实时过程快照（深拷贝，供路由序列化）。"""
    box = _live_box(task_id)
    with _live_lock:
        return {ch: [dict(e) for e in box[ch].values()] for ch in _LIVE_CHANNELS}


def clear_live_snapshot(task_id: str) -> None:
    """清空某 task 的实时过程快照（每轮开始时调用，避免残留上一轮）。"""
    tid = task_id or "_global"
    with _live_lock:
        _LIVE_SNAPSHOT.pop(tid, None)

def _try_start_task(task_id: str, agent_id: str = AGENT_MAIN) -> bool:
    """原子地标记 (task_id, agent_id) 为 running。已有运行体在跑返回 False。"""
    return manager.try_start(task_id, agent_id)

def _finish_task(task_id: str, agent_id: str = AGENT_MAIN) -> None:
    """标记 (task_id, agent_id) 为非运行。"""
    manager.finish(task_id, agent_id)

def _is_any_running() -> bool:
    return manager.is_any_running()

def _is_task_running(task_id: str, agent_id: str = AGENT_MAIN) -> bool:
    return manager.is_running(task_id, agent_id)

def _running_task_id() -> Optional[str]:
    """返回当前运行中的 task_id（单任务互斥，至多一个）。"""
    return manager.running_task_id()

def _config_hash(cfg: dict) -> str:
    """配置快照的稳定哈希（阶段 1：每个 run 可完整导出运行配置）。"""
    import hashlib
    import json

    try:
        s = json.dumps(cfg, sort_keys=True, ensure_ascii=False)
    except Exception:
        s = repr(cfg)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]

def _enabled_groups(cfg: dict) -> List[str]:
    """运行级启用的能力组（取自 config.runtime.tools.groups；未显式收窄则为空表）。"""
    groups = ((cfg.get("runtime") or {}).get("tools") or {}).get("groups")
    return list(groups) if isinstance(groups, list) else []

def _config() -> dict:
    """加载生效配置（config.yaml 为 base，~/.omniagent/config.yaml 覆盖）。

    走 config.load_config() 以合并前端设置面板写入的 settings，
    使 /chat 真正受设置面板控制（双模型/三通道开关等）。
    """
    try:
        import config as _cfg_mod
        return _cfg_mod.load_config()
    except Exception:
        pass
    try:
        import yaml
        if CONFIG_PATH.exists():
            return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except Exception:
        pass
    return {}

def _trajectory_dir(task_id: str) -> Path:
    """Plan C：轨迹按 task 维度存放（agents/local/runtime_paths.task_trajectory）。

    与内核 tool_loop 落盘路径一致，不再使用已废弃的 project_trajectory / data/ 布局。
    """
    try:
        from omni_core.local.runtime_paths import tasks_root, task_trajectory
    except Exception:
        tasks_root = lambda: ROOT / ".omniagent" / "tasks"  # type: ignore
        task_trajectory = lambda tid: tasks_root() / tid  # type: ignore
    if not task_id:
        return tasks_root()
    try:
        return task_trajectory(task_id)
    except Exception:
        return tasks_root() / task_id

def push_chat(role: str, text: str, extra: Optional[dict] = None, debug: bool = False,
              task_id: str = ""):
    """推一条消息到 task 的 outbox。

    - debug=False：进 chat outbox，作为用户可见的对话流（user/agent/system 语义消息）。
    - debug=True：进 debug outbox，内核调试信息（如任务下发、编排细节），
      不污染用户对话区，仅经 SSE `debug` 事件旁路推送（前端可忽略/折叠）。
    - task_id：消息归属的 task；空串则推到当前运行中的 task（兜底）。
    """
    tid = task_id or _running_task_id() or "_global"
    entry = {"role": role, "text": text, "ts": time.time(), "extra": extra or {}, "task_id": tid}
    box = _ensure_outbox(tid)
    if debug:
        box["debug"].append(entry)
    else:
        box["chat"].append(entry)

def push_thinking(role: str, content: str, model: str = "", task_id: str = ""):
    """推一条「深度思考」事件到对话区（左栏），经 SSE `thinking` 事件实时渲染。

    role: brain / executor（来源模型）；content: 本轮思考文本（推理模型即真实思考链）。
    """
    if not content:
        return
    tid = task_id or _running_task_id() or "_global"
    entry = {"role": role, "content": content, "model": model, "ts": time.time(),
             "id": f"th-{next(_live_seq)}"}
    _ensure_outbox(tid)["thinking"].append(entry)
    _live_push(tid, "thinking", entry["id"], entry)

def push_tool_call(role: str, name: str, arguments: str, result: str,
                   model: str = "", task_id: str = ""):
    """推一条「工具调用」事件到对话区（左栏），经 SSE `toolcall` 事件实时渲染。

    name/arguments/result 构成一条完整的 react 动作卡（模型调了什么、结果如何）。
    """
    tid = task_id or _running_task_id() or "_global"
    entry = {
        "role": role, "name": name, "arguments": arguments or "",
        "result": result or "", "model": model, "ts": time.time(),
        # id：前端据此 upsert —— 刷新时 live 快照回放与后续 SSE 增量不会变成两张卡
        "id": f"tc-{next(_live_seq)}",
    }
    _ensure_outbox(tid)["toolcall"].append(entry)
    _live_push(tid, "toolcall", entry["id"], entry)

def _upsert(box: "collections.deque", channel: str, block_id: str, entry: dict):
    """按 block_id 在通道内 upsert（存在则原地更新 content，保留原 ts 防时间线跳序）。"""
    q = box[channel]
    for i, e in enumerate(q):
        if e.get("id") == block_id:
            entry["ts"] = e.get("ts", entry["ts"])
            q[i] = entry
            return
    q.append(entry)

def push_thinking_stream(role: str, model: str, block_id: str, content: str, task_id: str = ""):
    """流式「深度思考」增量：按 block_id upsert，content 为「截至当前完整文本」。"""
    tid = task_id or _running_task_id() or "_global"
    entry = {"role": role, "content": content or "", "model": model, "id": block_id, "ts": time.time()}
    _upsert(_ensure_outbox(tid), "thinking", block_id, entry)
    _live_push(tid, "thinking", block_id, entry)

def push_message_stream(role: str, model: str, block_id: str, content: str, task_id: str = ""):
    """流式「口播」增量：按 block_id upsert，content 为「截至当前完整文本」（问题1/3）。"""
    tid = task_id or _running_task_id() or "_global"
    entry = {"role": role, "content": content or "", "model": model, "id": block_id, "ts": time.time()}
    _upsert(_ensure_outbox(tid), "message", block_id, entry)
    _live_push(tid, "message", block_id, entry)

def _make_brain_cfg():
    """构造在线大脑配置（含 executor 透传），供统一 /chat 入口复用。

    M-new：主模型 / 子 agent 模型均经 ``omni_core.brain.resolve`` 解析，
    优先新 schema（runtime.agents.<id>.model -> llm.providers），回退旧 schema
    （顶层 brain / runtime.executor）。
    """
    cfg = _config()
    from omni_core.brain.resolve import resolve_agent_model

    brain_cfg = resolve_agent_model(cfg, "main")
    # 思考路由模式：从配置读取，缺省 native（模型原生吐 reasoning token）
    brain_cfg = dict(brain_cfg)
    brain_cfg["reasoning_mode"] = (cfg.get("brain") or {}).get("reasoning_mode", "native")
    executor_cfg = resolve_agent_model(cfg, "worker")

    # 子 agent 模型启用但缺 key → 占位，避免 LLMClient 在无 key 端点直接报错
    if executor_cfg.get("enabled") and not executor_cfg.get("api_key"):
        env_key = executor_cfg.get("api_key_env", "OMNI_EXECUTOR_API_KEY")
        if not os.environ.get(env_key):
            executor_cfg = dict(executor_cfg)
            executor_cfg["api_key"] = "dummy"
    return brain_cfg, executor_cfg

def _append_task_name(task_id: str, name: str):
    """agent 提炼出的任务名回写 task 索引（仅当有变化）。"""
    try:
        TaskStore = _task_store()
        meta = TaskStore.get(task_id)
        if meta and meta.get("objective") != name:
            TaskStore.update(task_id, objective=name)
    except Exception:
        pass

def _steps_to_text(steps: List[Dict[str, Any]]) -> str:
    """把结构化执行步骤压成紧凑文本，回填进模型上下文（供其回答「你做了什么」）。"""
    lines = []
    for s in steps or []:
        t = s.get("type")
        if t == "thinking":
            c = (s.get("content") or "").strip()
            if c:
                lines.append(f"[思考] {c[:500]}")
        elif t == "tool_call":
            name = s.get("name", "")
            args = (s.get("arguments") or "").strip()
            res = (s.get("result") or "").strip()
            head = f"[工具] {name}" + (f"({args[:200]})" if args else "")
            lines.append(head + (f" -> {res[:500]}" if res else ""))
    return "\n".join(lines)

def _session_records_to_model_messages(records: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """会话记录 -> OpenAI 风格 messages；agent 轮附带执行步骤 recap，根治跨轮失忆。"""
    out: List[Dict[str, str]] = []
    for r in records or []:
        role = r.get("role")
        if role == "user":
            out.append({"role": "user", "content": r.get("content", "")})
        elif role == "agent":
            parts = [r.get("content") or ""]
            steps = (r.get("extra") or {}).get("steps") or []
            if steps:
                parts.append("（上一轮 agent 的执行记录）\n" + _steps_to_text(steps))
            text = "\n\n".join(p for p in parts if p)
            if text:
                out.append({"role": "assistant", "content": text})
        elif role == "system":
            out.append({"role": "system", "content": r.get("content", "")})
    return out

def _merge_thinking_step(steps, block_id, model, content):
    """把流式推理链累积进 turn_steps（按 block_id 去重 upsert）。

    修复：流式下 _on_thinking 不被调用，推理链只经 _on_llm_delta 推了实时卡片，
    没写进 turn_steps；终态 AgentTurn 用 extra.steps 重建时 steps 里无 thinking →
    思考卡消失。此处补齐流式路径，与非流式 _on_thinking 写入互斥、不重复。
    """
    for s in steps:
        if s.get("type") == "thinking" and s.get("id") == block_id:
            s["content"] = content
            s["model"] = model
            return
    steps.append({"type": "thinking", "id": block_id, "model": model, "content": content})

def _merge_message_step(steps, block_id, model, content):
    """把流式口播（message）累积进 turn_steps（按 block_id 去重 upsert），与 thinking 对称。

    修复：流式下口播只经 _on_llm_delta 推了实时气泡，没写进 turn_steps；终态
    AgentTurn 用 extra.steps 重建时 steps 里无 message → 结论退化成 m.text 一体气泡
    （无打字机）。此处补齐，使结果也像深度思考一样终态持久且可流式重建。
    """
    for s in steps:
        if s.get("type") == "message" and s.get("id") == block_id:
            s["content"] = content
            s["model"] = model
            return
    steps.append({"type": "message", "id": block_id, "model": model, "content": content})

_EXEC_TOOLS = {"tap_text", "tap_text_region", "collect_list", "press_keycode",
               "ocr_screenshot", "screenshot", "tap_by_id"}

def _call_skill(task_id: str, skill_name: str):
    def _push(role: str, text: str, extra: Optional[dict] = None):
        push_chat(role, text, extra=extra, task_id=task_id)

    _push("system", f"▶ 调用技能：{skill_name}  (task={task_id})")
    try:
        from omni_core.local.skill_library import SkillLibrary
        from devices import ExecutionModule

        sl = SkillLibrary(task_id=task_id)
        skill = sl.load(skill_name)
        if skill is None:
            _push("agent", f"❌ 技能不存在：{skill_name}")
            return
        _push("agent",
              f"技能「{skill.name}」\n目标模式：{skill.objective_pattern}\n"
              f"状态：{skill.metadata.status}  成功率：{skill.metadata.success_count}/{skill.metadata.total_uses}")
        exec = ExecutionModule()
        for i, sub in enumerate(skill.substeps, 1):
            tool = sub.tool
            args = sub.args or {}
            if tool not in _EXEC_TOOLS:
                _push("system", f"  · 跳过非执行类步骤 [{i}] {tool}（plan/record/task_done 由大脑处理，确定性回放仅跑执行动作）")
                continue
            _push("system", f"  · 执行 [{i}] {tool}({json.dumps(args, ensure_ascii=False)})")
            try:
                fn = getattr(exec, tool)
                res = fn(**args) if args else fn()
                _push("agent", f"    结果：{json.dumps(_trim(res), ensure_ascii=False)[:300]}")
            except Exception as e:
                _push("agent", f"    ⚠ 执行失败：{type(e).__name__}: {e}")
            time.sleep(0.3)
        _push("agent", "✅ 技能回放完成")
    except Exception as e:
        tb = traceback.format_exc()
        _push("agent", f"❌ 技能调用失败：{type(e).__name__}: {e}", extra={"traceback": tb[:1500]})
    finally:
        _finish_task(task_id)

def _trim(o: Any, n: int = 6) -> Any:
    if isinstance(o, dict):
        return {k: _trim(v, n) for k, v in list(o.items())[:n]}
    if isinstance(o, list):
        return [_trim(x, n) for x in o[:n]]
    if isinstance(o, str) and len(o) > 300:
        return o[:300] + "…"
    return o

def _read_skills(task_id: str) -> List[dict]:
    try:
        from omni_core.local.skill_library import SkillLibrary
        sl = SkillLibrary(task_id=task_id)
        out = []
        # 仅展示全局通用 skill；task 私有 skill 不进 Web 列表、不共享
        for s in sl.list_global():
            out.append({
                "name": s.name,
                "objective_pattern": s.objective_pattern,
                "status": s.metadata.status,
                "success_count": s.metadata.success_count,
                "total_uses": s.metadata.total_uses,
                "substeps": [{"tool": ss.tool, "args": ss.args} for ss in s.substeps],
            })
        return out
    except Exception as e:
        return [{"error": str(e)}]

def _read_world(task_id: str) -> dict:
    try:
        from omni_core.local.world_model import WorldModel
        wm = WorldModel(task_id=task_id)
        if wm.load():
            return {"ok": True, "summary": wm.summary(),
                    "collected_count": len(wm.collected),
                    "checkpoints": len(WorldModel.list_checkpoints(task_id))}
    except Exception:
        pass
    return {"ok": False}

def _read_latest_collected(task_id: str) -> dict:
    try:
        from omni_core.local.runtime_paths import task_collected
        d = task_collected(task_id)
    except Exception:
        d = tasks_root() / task_id / "collected"
    if not d.is_dir():
        return {"items": [], "total": 0}
    files = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return {"items": [], "total": 0}
    try:
        data = json.loads(files[0].read_text(encoding="utf-8"))
        items = data.get("collected", [])
        return {"run_id": data.get("run_id"), "items": items[:200], "total": len(items),
                "file": files[0].name}
    except Exception:
        return {"items": [], "total": 0}

def _read_trajectory(task_id: str, limit: int = 60) -> List[dict]:
    d = _trajectory_dir(task_id)
    if not d.is_dir():
        return []
    files = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return []
    try:
        lines = files[0].read_text(encoding="utf-8").splitlines()
        return [json.loads(ln) for ln in lines[-limit:] if ln.strip()]
    except Exception:
        return []

def _snapshot(task_id: str) -> dict:
    # 解析当前运行 context，用于完整导出运行配置（阶段 1 验收）。
    ctx = manager.get(task_id, AGENT_MAIN) if task_id else None
    if ctx is None and _is_any_running():
        _rid = _running_task_id()
        ctx = manager.get(_rid, AGENT_MAIN) if _rid else None
    return {
        "skills": _read_skills(task_id),
        "world": _read_world(task_id),
        "collected": _read_latest_collected(task_id),
        "trajectory": _read_trajectory(task_id),
        "running": _is_task_running(task_id) if task_id else _is_any_running(),
        "task_id": task_id or _running_task_id() or "",
        "project_id": ctx.project_id if ctx else "",
        "config_hash": ctx.config_hash if ctx else "",
        "enabled_capability_groups": ctx.enabled_capability_groups if ctx else [],
        # 阶段 1：每个 run 可完整导出运行配置
        "context": ctx.export() if ctx else None,
    }

def _memory_enabled() -> bool:
    """知识层弱注入（memory）是否开启，取自生效配置 runtime.knowledge.memory.enabled。"""
    cfg = _config()
    return bool((((cfg.get("runtime") or {}).get("knowledge") or {}).get("memory") or {}).get("enabled", False))

def _read_merged_ids() -> set:
    """读取 memory/merged.json 中已合并的 task_id 集合（幂等去重用）。"""
    try:
        from omni_core.local import runtime_paths as P
        p = P.global_memory() / "merged.json"
        if p.exists():
            return set((json.loads(p.read_text(encoding="utf-8")) or {}).get("merged_task_ids", []))
    except Exception:
        pass
    return set()

def _read_memory_master() -> str:
    """读取 MEMORY.md 全文；不存在返回空串。"""
    try:
        from omni_core.local import runtime_paths as P
        p = P.memory_master()
        return p.read_text(encoding="utf-8") if p.exists() else ""
    except Exception:
        return ""

def _read_memory_summary_chars() -> int:
    """读取注入视图 memory_summary.md 字符数。"""
    try:
        from omni_core.local import runtime_paths as P
        p = P.memory_summary()
        return len(p.read_text(encoding="utf-8")) if p.exists() else 0
    except Exception:
        return 0

def _rollout_header(text: str) -> dict:
    """解析单条 rollout md 头行：distilled_at 与 success。

    头行形如：
    ``- task_id: xxx / objective: ... / success: True / steps: n /
       distilled_at(UTC iso): <iso> / trajectory: tasks/xxx/trajectory.jsonl``
    """
    res = {"distilled_at": "", "success": None}
    try:
        for ln in text.splitlines():
            s = ln.strip()
            if s.startswith("- task_id:"):
                m = re.search(r"distilled_at\(UTC iso\):\s*(\S+)", s)
                if m:
                    res["distilled_at"] = m.group(1).strip()
                sm = re.search(r"/ success:\s*(true|false)", s, re.IGNORECASE)
                if sm:
                    res["success"] = sm.group(1).strip().lower() == "true"
                break
    except Exception:
        pass
    return res

def _read_rollouts_list(limit: int = 50, offset: int = 0) -> dict:
    """倒序列出 memory/rollouts/*.md 元信息，支持分页。

    Returns:
        {"rollouts": [...], "total": int}
    """
    try:
        from omni_core.local import runtime_paths as P
        from omni_core.local.curator import _parse_rollout_sections
        d = P.memory_rollouts()
        if not d.is_dir():
            return {"rollouts": [], "total": 0}
        merged = _read_merged_ids()
        files = sorted(d.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
        total = len(files)
        page = files[offset: offset + limit] if limit > 0 else files[offset:]
        out = []
        for p in page:
            try:
                text = p.read_text(encoding="utf-8")
            except Exception:
                continue
            facts, lessons = _parse_rollout_sections(text)
            hdr = _rollout_header(text)
            out.append({
                "task_id": p.stem,
                "distilled_at": hdr["distilled_at"],
                "success": hdr["success"],
                "facts_n": len(facts),
                "lessons_n": len(lessons),
                "merged": p.stem in merged,
            })
        return {"rollouts": out, "total": total}
    except Exception:
        return {"rollouts": [], "total": 0}

def _read_rollout_detail(task_id: str) -> Optional[dict]:
    """读取单条 rollout 全文 + 解析段 + trajectory 引用。"""
    try:
        from omni_core.local import runtime_paths as P
        from omni_core.local.curator import _parse_rollout_sections
        p = P.memory_rollout_file(task_id)
        if not p.exists():
            return None
        text = p.read_text(encoding="utf-8")
        facts, lessons = _parse_rollout_sections(text)
        hdr = _rollout_header(text)
        traj = ""
        for ln in text.splitlines():
            s = ln.strip()
            if s.startswith("- task_id:"):
                tm = re.search(r"/ trajectory:\s*(\S+)", s)
                if tm:
                    traj = tm.group(1).strip()
                break
        return {
            "task_id": task_id,
            "content": text,
            "trajectory": traj,
            "facts_n": len(facts),
            "lessons_n": len(lessons),
            "distilled_at": hdr["distilled_at"],
            "success": hdr["success"],
        }
    except Exception:
        return None

def _collect_user_corrections(session_records: List[Dict[str, Any]],
                                prev_assistant: str, last_user: str) -> List[str]:
    """K4：从会话记录提取用户纠偏消息（C₁=refuted 且含纠正内容）。

    返回 [user_msg_text]。config ``runtime.curator.corrective_source=false``（默认）时
    返回空（红线②：默认关）。仅读 session，不写任何数据。
    """
    try:
        cfg = _config()
        if not bool((((cfg.get("runtime") or {}).get("curator") or {}).get("corrective_source", False))):
            return []
        from omni_core.local import signals as _sig
        pairs: List[Dict[str, str]] = []
        last_asst = ""
        for r in session_records or []:
            if r.get("role") == "assistant":
                last_asst = r.get("content", "") or ""
            elif r.get("role") == "user":
                pairs.append({"a": last_asst, "u": r.get("content", "") or ""})
        corr: List[str] = []
        for p in pairs:
            u = (p.get("u") or "").strip()
            if not u:
                continue
            if _sig.classify_c1(p.get("a", ""), u) == "refuted" and len(u) >= 4:
                corr.append(u)
        # 去重保持顺序
        seen: set = set()
        out: List[str] = []
        for c in corr:
            if c not in seen:
                seen.add(c)
                out.append(c)
        return out
    except Exception:
        return []
