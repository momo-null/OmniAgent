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
from backend.api.runtime_manager import manager, AGENT_MAIN

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CONFIG_PATH = ROOT / "config.yaml"

_traj_cursor: Dict[str, Any] = {}
_cursor_lock = threading.Lock()

# SSE 连接计数（纯计数，供 /api/runtime/memory 诊断判断 _stream_gen 协程是否被回收）
_sse_open_n = 0
_sse_n_lock = threading.Lock()


def sse_open() -> None:
    global _sse_open_n
    with _sse_n_lock:
        _sse_open_n += 1


def sse_close() -> None:
    global _sse_open_n
    with _sse_n_lock:
        _sse_open_n -= 1


def sse_connections() -> int:
    with _sse_n_lock:
        return _sse_open_n


def live_stats() -> Dict[str, Dict[str, list]]:
    """live 过程快照条目快照（浅拷贝引用，仅用于统计条目数与体积）。"""
    with _live_lock:
        return {tid: {ch: list(d.values()) for ch, d in box.items()}
                for tid, box in _LIVE_SNAPSHOT.items()}

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
_LIVE_CHANNELS = ("thinking", "message", "toolcall", "approvals")
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
    # S2：任务终态——待批卡全部置 cancelled（唤醒阻塞的工具线程）、任务级审批记忆清空。
    # 不允许任务结束后还有卡悬着（§6.2）。
    try:
        from backend.services.approvals import cancel_task_approvals
        cancel_task_approvals(task_id)
    except Exception:
        pass

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

    与内核 tool_loop 落盘路径一致，统一用 task 维度布局。
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

def push_approval(card: Any, task_id: str = "") -> None:
    """S2 审批卡：SSE `approval` 事件 + live 快照 approvals 通道（按 approval_id upsert）。

    card 为 omni_core.tools.policy.ApprovalCard（鸭子类型读取，避免反向依赖内核类型）。
    """
    tid = task_id or _running_task_id() or "_global"
    entry = {
        "approval_id": card.approval_id,
        "task_id": tid,
        "tool": card.tool, "unit": card.unit, "risk": card.risk,
        "arguments": card.arguments or {},
        "created_at": card.created_at, "wait_seconds": card.wait_seconds,
        "ts": time.time(),
    }
    _ensure_outbox(tid)["approval"].append(entry)
    _live_push(tid, "approvals", entry["approval_id"], entry)


def push_approval_resolved(approval_id: str, outcome: str, task_id: str = "") -> None:
    """S2 决议 / 超时 / 取消：SSE `approval_resolved` 事件；并从 live 快照移除该卡。"""
    tid = task_id or _running_task_id() or "_global"
    _ensure_outbox(tid)["approval_resolved"].append(
        {"approval_id": approval_id, "outcome": outcome, "ts": time.time()})
    with _live_lock:
        try:
            _live_box(tid)["approvals"].pop(approval_id, None)
        except Exception:
            pass


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

def _make_brain_cfg(model: Optional[str] = None):
    """构造大脑配置（含 worker 透传），供统一 /chat 入口复用。

    职责切分（模型路由真源收敛，2026-09-29）：
    - **端点**唯一真源是模型目录 ``~/.omniagent/models.json``：main 槽位由
      ``omni_core.brain.router.resolve_slot`` 解析（显式选择 > 目录默认）。
      目录里没有 main 选择时这里就是空端点，LLMClient 会给出明确报错——
      不再回退 config 里的任何端点键（旧通道已下线）。
    - **引擎参数**仍在 ``config.yaml``：此处只从顶层 ``brain`` 挑非端点键
      （``long_task`` / ``maxInputTokens`` / ``reasoning_mode``）。

    ``model`` 为 ``"<provider_id>/<model_id>"`` 选择，经白名单校验后才覆盖；
    非法选择被忽略（不报错），避免请求体注入任意端点。
    """
    cfg = _config()
    # 引擎参数（历史压缩 / 上下文预算 / 推理模式）——与端点无关，始终来自 config。
    # 显式挑键：config.brain 里残留的端点键（base_url/model/api_key）一律不透传。
    brain_src = cfg.get("brain") or {}
    brain_cfg: Dict[str, Any] = {
        "reasoning_mode": brain_src.get("reasoning_mode", "native"),
        "maxInputTokens": brain_src.get("maxInputTokens", 0),
        "long_task": brain_src.get("long_task") or {},
    }

    try:
        from omni_core.brain import router as model_router

        selected = model_router.resolve_slot("main", model)
        if selected:
            brain_cfg = {**brain_cfg, **selected}
    except Exception:  # noqa: BLE001
        pass

    # 子 agent 模型：目录 worker 槽位（无选择 = 空 dict = 主模型兼任）
    executor_cfg: Dict[str, Any] = {}
    try:
        from omni_core.brain import router as model_router

        executor_cfg = dict(model_router.resolve_slot("worker") or {})
    except Exception:  # noqa: BLE001
        executor_cfg = {}

    # 子 agent 模型启用但缺 key → 占位，避免 LLMClient 在无 key 端点直接报错
    if executor_cfg and not executor_cfg.get("api_key"):
        env_key = executor_cfg.get("api_key_env", "OMNI_EXECUTOR_API_KEY")
        if not os.environ.get(env_key):
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

_RECAP_STEPS = 12   # 跨轮回填的工具调用上限（防单轮历史撑爆上下文）


def _session_records_to_model_messages(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """会话记录 -> OpenAI 风格 messages（跨轮记忆）。

    agent 轮的历史工具调用以**结构化 tool_calls + 成对 tool 结果消息**回填，
    与 run 内 SDK 累积的 items 形态保持一致。

    此前此处把工具调用压成 `[工具] name(args) -> result` 的纯文本 recap 塞进
    assistant content——等于给模型示范「工具调用是用文本写的」，导致跨轮续跑时
    模型改用 DSML / `<tool_code>` 之类文本模仿、真实工具调用归零（0 action）。
    结构化回填消除该诱因：不针对任何具体退化格式做清洗，故格式换马甲也不会复发。
    """
    out: List[Dict[str, Any]] = []
    for r in records or []:
        role = r.get("role")
        if role == "user":
            # 空文本不入上下文：历史里存在 content="" 的合法记录（增量 partial 落盘
            # 见 _maybe_flush_partial），原样喂给端点会被拒（空 user 消息），故跳过。
            _uc = (r.get("content") or "").strip()
            if _uc:
                out.append({"role": "user", "content": _uc})
        elif role in ("agent", "assistant"):
            # 会话落库写 "assistant"、SSE 写 "agent"——同一发送者，两边都要认
            content = (r.get("content") or "").strip()
            steps = (r.get("extra") or {}).get("steps") or []
            calls = [s for s in steps if s.get("type") == "tool_call" and s.get("name")]
            if len(calls) > _RECAP_STEPS:
                calls = calls[-_RECAP_STEPS:]
            if calls:
                base = len(out)
                tool_calls = []
                for i, s in enumerate(calls):
                    args_raw = (s.get("arguments") or "").strip()
                    try:
                        json.loads(args_raw)      # 仅校验可解析，非法参数退化为 {}
                        args = args_raw
                    except Exception:
                        args = "{}"
                    tool_calls.append({
                        "id": f"hist_{base}_{i}",
                        "type": "function",
                        "function": {"name": s.get("name", ""), "arguments": args},
                    })
                out.append({"role": "assistant", "content": content, "tool_calls": tool_calls})
                for i, s in enumerate(calls):
                    out.append({
                        "role": "tool",
                        "tool_call_id": tool_calls[i]["id"],
                        "content": (s.get("result") or "").strip()[:2000] or "(无结果)",
                    })
            elif content:
                out.append({"role": "assistant", "content": content})
        elif role == "system":
            _sc = (r.get("content") or "").strip()
            if _sc:
                out.append({"role": "system", "content": _sc})
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
        # 知识分层 A6：合并目录（project 优先 + global），条目标注 scope——
        # 前端据 scope 决定是否显示「设为全局」（A7 手动提升）。
        for s in sl.list_all():
            out.append({
                "name": s.name,
                "objective_pattern": s.objective_pattern,
                "status": s.metadata.status,
                "scope": "global" if s.metadata.scope == "global" else "project",
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
        # 阶段 1：每个 run 可完整导出运行配置
        "context": ctx.export() if ctx else None,
    }

def _profile_enabled() -> bool:
    """知识层弱注入（user_profile）是否开启，取自配置 runtime.knowledge.profile.enabled（默认开）。"""
    cfg = _config()
    return bool((((cfg.get("runtime") or {}).get("knowledge") or {}).get("profile") or {}).get("enabled", True))

