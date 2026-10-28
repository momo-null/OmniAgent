"""核心会话运行时：/chat /wake /stop /inject /stream + 派发与 SSE（零逻辑改动）。"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from omni_core.tools.base import TOOL_REGISTRY

from backend.api.routers.helpers import (
    AGENT_MAIN,
    Any,
    Dict,
    List,
    Optional,
    _config_hash,
    _cursor_lock,
    _ensure_outbox,
    _finish_task,
    _is_any_running,
    _is_task_running,
    _make_brain_cfg,
    _merge_message_step,
    _merge_thinking_step,
    _mode_brain_cfg,
    _paths,
    _project_store,
    _read_latest_collected,
    _read_skills,
    _read_world,
    _running_task_id,
    _session_records_to_model_messages,
    _task_store,
    _trajectory_dir,
    _try_start_task,
    asyncio,
    outbox_drain,
    outbox_reset_cursor,
    sse_close,
    sse_open,
    clear_live_snapshot,
    config,
    json,
    live_snapshot,
    manager,
    push_chat,
    push_message_stream,
    push_thinking,
    push_thinking_stream,
    push_tool_call,
    threading,
    time,
    traceback,
)

# 兼容 shim：/chat、/wake 处理器经由 router_runtime 模块命名空间解析被单测 monkeypatch 的名字
# （_dispatch_chat / _is_any_running / _is_task_running / _running_task_id），使外部对
# router_runtime 的打桩对处理器生效（拆分前这些名字同属 router_runtime 模块）。
import backend.api.router_runtime as _rt

router = APIRouter(tags=["runtime"])

# 注意力预算 / did_work 判定用：这些工具是"机制"或"只读"，不产出事实，因此不算干活。
# 判定口径（plan §3.3）：本轮有**真实**工具调用（写文件 / 跑命令 / 抓网页 …）或派出去
# 的任务确实执行了 → did_work=True，L2 场景维护才允许写。
_META_TOOLS = frozenset({
    "task_done", "verify", "verify_done", "escalate", "record", "dispatch", "todo_write",
    "read_scene", "read_task_digest", "load_skill", "search_skill",
    "memory_remember", "memory_recall", "memory_update", "memory_forget", "memory_overview",
    "msg_send_text", "msg_send_card", "msg_recent",
})


def _did_work(turn_steps: List[Dict[str, Any]], result: Optional[Dict[str, Any]] = None) -> bool:
    """本轮是否"真的干了活"（L2 写的唯一门控维度）。

    按 did_work 而不是按入口模式：闲聊不产场景，但语音派出去的任务照常维护场景
    （那些活的 did_work 由 worker 那一轮决定）。
    """
    for s in turn_steps or []:
        if s.get("type") != "tool_call":
            continue
        name = str(s.get("name") or "")
        if name and name not in _META_TOOLS:
            return True
    if list((result or {}).get("dispatch_plan") or []):
        return True       # 派了活（handoff）：任务本身算产出
    return False


def _dispatch_chat(task_id: str, history: List[Dict[str, Any]], max_steps: int, full_access: Optional[bool] = None, agent_id: str = AGENT_MAIN, model: Optional[str] = None, mode: str = "auto", persona: str = ""):
    """统一 agent 入口（单入口处理闲聊 + 双 agent 任务执行）。

    一个会话 = 一个 task（首条消息由调用方在外部建好 task 并传入 task_id）。

    双 agent 协作模型（planner + executor）：
    - planner（主模型）：规划 / 反思，看全局。
    M10 统一入口（run_task）：
    - 主 agent（主模型）默认自己把任务做完；需要并行时它调 dispatch，
      编排层用 Send 扇出子 agent（模型目录 worker 槽位指向的模型，可配本地高频模型）。
    - 不再有「两层」的固定角色划分。

    单链路统一（2026-09-17）：闲聊与执行走同一条 run_task 入口，不再做"首条轻量 probe 判断"。
    每轮消息直接进 run_task，由大脑自己决定纯文本回答（方案 B 收尾）还是调用工具执行；
    后续消息（task_id 已有）沿用同一 task 上下文。

    ``model``：本次运行的模型选择 ``"<provider_id>/<model_id>"``（模型路由，2026-09-29），
    仅作用于 main 槽位；空 = 用目录默认 / 配置。
    """
    # 接线延迟打点起点（plan §2 验收项）：从"收到派发"到"请求发出"这段耗时要能量出来
    _ts_entry = time.time()
    # lane（plan §4）：mode=task 是"干活"，其余是"对话"。只影响互斥与回播，不改链路。
    _lane = "work" if str(mode or "").lower() == "task" else "chat"
    # 阶段 1：运行体句柄存于 RuntimeManager 复合键 (task_id, agent_id)，不再用全局 _loop
    _last_answer = ""  # 累积大脑最后一段口播，作为结尾「结果框」内容
    loop = None  # ToolLoop 句柄（finally 收尾补投需要；构造失败时为 None）
    _force = False  # force 打断标记（TAM 区读取即消费；finally 用于跳过补投）
    _pending_injections: List[Dict[str, Any]] = []  # 收尾补投：未消费注入
    turn_steps: List[Dict[str, Any]] = []  # 本轮 agent 的执行轨迹（思考 + 工具调用）
    # 增量落库（2026-10-04）：过程记录每 ~20 步刷一条 partial 到会话 jsonl，
    # 崩溃/被杀不再整轮蒸发；终态 final 收口，重放时按 run_key 并回单轮（coalesce）。
    run_key = f"rk_{int(time.time() * 1000)}"
    _flushed = 0
    _PARTIAL_FLUSH_EVERY = 20
    _MAX_STEPS_IN_RECORD = 300  # 超过则 final 省略 steps（重放走 partial 拼接），防巨型单行
    # 新的一轮：清空实时过程快照，避免 GET /live 把上一轮的残留过程回放给前端
    clear_live_snapshot(task_id)
    try:
        from omni_core.local.loop import ToolLoop, TaskSpec

        # mode 决定模型槽（chat → voice 槽，缺省回退主槽），链路本身不变
        brain_cfg, executor_cfg = _mode_brain_cfg(model, mode)
        # 阶段 0.5：运行级配置快照——一次性加载，运行期内 ToolLoop 不再重读全局 config
        cfg = config.load_config() or {}

        def _debug_push(kind: str, payload: dict):
            # 内核调试日志 -> task debug outbox -> SSE `debug` 事件 -> 前端日志窗口
            title = payload.get("title") or kind
            push_chat("debug", title, extra={"kind": kind, "payload": payload},
                      debug=True, task_id=task_id)

        def _on_thinking(role: str, content: str, model: str):
            # 对话区实时「深度思考」块 -> SSE `thinking` 事件 -> 前端对话框
            push_thinking(role, content, model, task_id=task_id)
            if content:
                turn_steps.append({"type": "thinking", "model": model, "content": content})
            _maybe_flush_partial()

        def _on_tool_call(role: str, name: str, arguments: str, result: str, model: str):
            # 后端校验（verify / verify_done）属内核门控，不进用户对话区（理由仍走 debug 面板）
            if name in ("verify", "verify_done"):
                return
            # 对话区实时「工具调用」卡片 -> SSE `toolcall` 事件 -> 前端对话框
            push_tool_call(role, name, arguments, result, model, task_id=task_id)
            turn_steps.append({"type": "tool_call", "model": model, "name": name,
                               "arguments": arguments or "", "result": result or ""})
            _maybe_flush_partial()

        def _on_llm_delta(role: str, kind: str, block_id: str, text: str, model: str):
            # 真流式增量（问题3-B）：推理链 -> thinking 块，口播 -> message 气泡（问题1）
            # 累积大脑最后一段口播文本，用于结尾「结果框」（用户只需大模型最后给出的结果）
            nonlocal _last_answer
            if kind == "message" and role == "brain":
                _last_answer = text
            if kind == "message":
                push_message_stream(role, model, block_id, text, task_id=task_id)
                # 修复：流式口播也累积进 turn_steps，使终态 AgentTurn 能重建「结论」气泡
                # （与 thinking 对称；非流式路径由 _on_thinking 外的 message 写入互斥）
                _merge_message_step(turn_steps, block_id, model, text)
            else:
                push_thinking_stream(role, model, block_id, text, task_id=task_id)
                # 修复：流式推理链也累积进 turn_steps，使终态 AgentTurn 能重建「深度思考」卡片
                # （非流式路径由 _on_thinking 写入，此处补齐流式路径，二者互斥）
                _merge_thinking_step(turn_steps, block_id, model, text)
            _maybe_flush_partial()

        loop = ToolLoop(brain_cfg, executor_cfg=executor_cfg, verbose=True,
                        full_access=full_access,
                        config_snapshot=cfg, agent_id=agent_id,
                        mode=mode, persona=persona, dispatch_ts=_ts_entry,
                        on_debug=_debug_push,
                        on_thinking=_on_thinking, on_tool_call=_on_tool_call,
                        on_llm_delta=_on_llm_delta)
        loop.reset_stop()
        rec = manager.get(task_id, agent_id)
        if rec is not None:
            rec.loop = loop
            rec.config_snapshot = cfg

        # 仓储句柄先绑定：本函数后面还会用到（原先只在 finally 里绑定，
        # 导致上面这行先引用时被当成未绑定的局部变量 → UnboundLocalError）
        TaskStore = _task_store()
        ProjectStore = _project_store()

        # P2.1: 持久化通道——把 assistant 回包也写入 session jsonl（与 user 一起补全对话恢复）
        _meta = TaskStore.get(task_id)
        _pid = _meta.get("project_id", "") if _meta else ""
        _sid = _meta.get("session_id", "") if _meta else ""

        # 阶段 1：把运行级配置快照哈希、project_id、工具 registry、已装载插件
        # 写入 context，使「每个 run 可完整导出运行配置」成立（阶段 1 验收项 + 目标 3）。
        if rec is not None:
            rec.config_hash = _config_hash(cfg)
            rec.project_id = _pid
            rec.tool_registry = TOOL_REGISTRY
            rec.loaded_plugins = sorted({
                p.unit for p in TOOL_REGISTRY.values() if p.source == "plugin"
            })

        # B3：用服务端会话记录（含结构化 steps）重建模型上下文，使模型记得上一轮做了什么。
        # 前端回传的 history 只有扁平 role/content，不含 steps，故以会话存储为准。
        _session_records: List[Dict[str, Any]] = []
        if _pid and _sid:
            try:
                # coalesce：partial 增量记录并回单轮，模型上下文不碎片化、不重复
                _session_records = ProjectStore.coalesce_records(
                    ProjectStore.read_session(_pid, _sid))
            except Exception:
                pass
        if not _session_records:
            _session_records = list(history)  # 兜底：用前端回传的扁平历史
        model_messages = _session_records_to_model_messages(_session_records)

        def _append_assistant(text: str, extra: Optional[dict] = None):
            if _pid and _sid:
                try:
                    ProjectStore.append_message(_pid, _sid, "assistant", text,
                                                task_id=task_id, extra=extra)
                except Exception:
                    pass

        def _maybe_flush_partial(force: bool = False):
            """把新增过程步增量落一条 partial agent 记录（崩溃不丢、终态防巨型单行）。"""
            nonlocal _flushed
            if not _pid or not _sid:
                return
            if not (force or len(turn_steps) - _flushed >= _PARTIAL_FLUSH_EVERY):
                return
            if len(turn_steps) <= _flushed:
                return
            try:
                ProjectStore.append_message(
                    _pid, _sid, "assistant", "", task_id=task_id,
                    extra={"steps": turn_steps[_flushed:], "partial": True, "run_key": run_key})
                _flushed = len(turn_steps)
            except Exception:
                pass

        def _push(role: str, text: str, extra: Optional[dict] = None, debug: bool = False):
            push_chat(role, text, extra=extra, debug=debug, task_id=task_id)

        last_user = ""
        for m in reversed(history):
            if m.get("role") == "user":
                last_user = m.get("content", "")
                break

        # ---- 单链路统一（2026-09-17 用户确认）：不再做闲聊 probe 快判 ----
        # 每轮消息直接进统一入口 run_task：模型自己判断纯文本回答（方案 B 收尾）
        # 还是调工具执行。删 probe 后少一次 LLM 调用；闲聊/任务/追问展示统一；
        # 会话历史经 TaskSpec.history 单路注入（替代旧 prior_context，不再双重注入）。

        # ---- 执行任务：统一入口（主 agent，按需派发子 agent） ----
        # F2.2：收尾语义来自 task.json（task_mode，缺省 oneshot；缺省行为不变）。
        # 闲聊/文本型任务的纯文本收尾判 success 是正常语义，不强制 daemon。
        _task_mode = str((_meta or {}).get("task_mode", "oneshot") or "oneshot")
        spec = TaskSpec(objective=last_user, done_when="",
                        task_id=task_id, project_id=None, max_steps=max_steps,
                        history=list(model_messages),
                        task_mode=_task_mode)
        _debug_push("task_init", {
            "title": "任务下发",
            "objective": last_user,
            "brain_model": (brain_cfg or {}).get("model"),
            "brain_base_url": (brain_cfg or {}).get("base_url"),
            "executor_model": ((executor_cfg or {}).get("model")
                               if (executor_cfg and executor_cfg.get("enabled")) else "回退主模型(brain)"),
            "max_steps": max_steps,
        })
        result = None  # 防御：run_task 异常时 finally 仍能安全判 paused
        _turn_ts0 = time.time()  # 本轮起点：产物登记表按 ts 过滤出本轮新增产物
        result = loop.run_task(spec)
        _reason = result.get("reason") or ""
        # force 打断标记（读取即消费）：本轮被「立即发送」硬停
        _force = False
        _rec_f = manager.get(task_id, agent_id)
        if _rec_f is not None and getattr(_rec_f, "force_interrupted", False):
            _force = True
            _rec_f.force_interrupted = False
        # 记忆写链**异步化**（plan §3.3）：收尾只入队，不进主链路。
        # flush_brain 是 3 次 LLM（提炼 / 价值判 / 一致性判）+ L2 维护（墙钟上限 300s），
        # 同步跑就是语音每句话都要吃的尾巴（实测 0~7.4s，最坏 45s+）。
        # force 打断的轮次跳过入队：打断要快；消息留在 capture 缓冲里，随下一轮正常收口。
        if not _force:
            try:
                from omni_core import memory_writer
                _pid = TaskStore.project_of(task_id)
                _dw = _did_work(turn_steps, result)
                memory_writer.enqueue(
                    _pid, brain_cfg, source_task=task_id,
                    persona=getattr(loop, "persona", "") or persona,
                    did_work=_dw,
                    # chat（闲聊）不产 episode：流水账会把库填满、把真事实挤下去
                    allow_episode=(str(mode or "auto").lower() != "chat"),
                    on_done=lambda _st: _debug_push(
                        "memory_flush", {"title": "记忆沉淀(异步)", **_st}),
                )
                _debug_push("memory_enqueue", {"title": "记忆入队", "did_work": _dw,
                                               "mode": mode, "pending": memory_writer.pending()})
            except Exception:
                pass
        # 把本轮执行轨迹（思考 + 工具调用）连同结论，作为「归属 agent 的一整轮」持久化：
        # 既供前端刷新后恢复完整过程流，也供下一轮模型上下文回填（根治跨轮失忆）。
        meta = {
            "success": result.get("success"),
            "steps": result.get("steps"),
            "escalated": result.get("escalated"),
            "collected": result.get("collected_count"),
        }
        # 结论直取（B10，2026-09-17 用户确认拆除 68333ee 的抽取链）：
        # LLM 收尾轮 message 就是最终结果（闲聊回答 = 任务结论，同一种东西），
        # 不再从 record/工具结果里「合成」结论。兜底链：
        # 1) _last_answer：模型收尾口播（方案 B 纯文本收尾 / 收尾轮 content）；
        # 2) _reason：task_done 显式收尾时模型给出的 reason（覆盖工具轮 content 为空的
        #    推理模型——实测 v4-pro 工具轮 content 为空字符串）；
        # 3) 中性提示：两者皆空的最终兜底，绝不用推理自言自语当结论。
        if _last_answer:
            conclusion = _last_answer
        elif _reason:
            conclusion = _reason
        else:
            conclusion = f"任务已完成（共执行 {meta.get('steps')} 步）。详细结果见上方执行步骤。"
        # force 打断的轮次：口播半句落库时显式标记，用户可见「这轮是被打断的」
        if _force and conclusion:
            conclusion += " ……[被打断]"
        # 终态记录：步数超上限则省略 steps（重放由 partial 拼接），防巨型单行 jsonl
        _steps_omitted = len(turn_steps) > _MAX_STEPS_IN_RECORD
        if _steps_omitted:
            _maybe_flush_partial(force=True)
        # 本轮新增产物（登记表按 ts 过滤；tmp 已在 run_task 终态被抢救到持久 artifacts/）
        _turn_artifacts: List[Dict[str, Any]] = []
        try:
            from omni_core.local.artifacts import load_artifacts
            _turn_artifacts = [
                a for a in load_artifacts(task_id)
                if float(a.get("ts") or 0) >= _turn_ts0 - 1.0
            ][-12:]
        except Exception:
            _turn_artifacts = []
        _extra = {"steps": [] if _steps_omitted else turn_steps, "meta": meta, "final": True,
                  "run_key": run_key, **({"steps_omitted": True} if _steps_omitted else {}),
                  **({"artifacts": _turn_artifacts} if _turn_artifacts else {})}
        # 校验结论（如「无校验条件，信任大脑」）只进调试面板，不污染用户对话
        if _reason:
            _debug_push("verify", {"title": "完成校验", "reason": _reason})
        # 大模型最后给出的结论 + 完整过程流：归到 agent 一角色（不再发独立 system 回执；
        # 机器状态收进 meta，作为 agent turn 的小字脚注），自然沉在对话最末。
        if conclusion or turn_steps:
            _push("agent", conclusion, extra=_extra)
            _append_assistant(conclusion, extra=_extra)
        # work lane 完成回播（plan §4）：干活的任务跑完，给主人一句话（可播报）。
        # 带 extra.announce，语音桥（旁路订阅者）据此决定要不要念出来；
        # 正文一句话，不刷屏——完整细节留在该任务自己的对话流里。
        if _lane == "work" and conclusion:
            try:
                _one = (conclusion or "").strip().splitlines()[0][:80]
                if _one:
                    push_chat("system", _one,
                              extra={"announce": True, "lane": "work",
                                     "task_id": task_id},
                              task_id=task_id)
            except Exception:
                pass
    except Exception as e:
        tb = traceback.format_exc()
        push_chat("agent", f"❌ 对话循环失败：{type(e).__name__}: {e}",
                  extra={"traceback": tb[:2000]}, task_id=task_id)
    finally:
        rec = manager.get(task_id, agent_id)
        if rec is not None:
            rec.loop = None
            # force 标记兜底清除（异常路径可能未走到消费点，防下一轮误跳沉淀）
            rec.force_interrupted = False
        _finish_task(task_id, agent_id)
        # daemon 纯文本汇报后 paused：本轮结束但任务保持活跃，等用户下条消息续跑，
        # 不标 done（否则 chat 多轮会话被误判结束、无法续跑）。仅当 run_task 正常返回
        # 且声明 paused 时跳过终态标记；异常/其它终态仍走原 done 收口。
        if not (result and result.get("paused")):
            # 更新 task 状态为 done/failed（P1.4 任务状态机）
            try:
                _task_store().update(task_id, state="done")
            except Exception:
                pass
        # M8 收尾补投：本轮最后一次模型请求之后注入的消息不会再有请求边界，
        # 留在通道里会随 loop 销毁而丢失（表现为「消息落库但模型不回应」——
        # LLM 已答完、用户在收尾窗口发消息即触发）。这里取走未消费注入并
        # 自动续跑一轮（对齐 /wake 语义：消息已落库，补一次执行）。
        # force 打断轮不补投：通道随旧轮作废，新消息已由 force 路径派发。
        if loop is not None and not _force:
            try:
                _pending_injections = loop.drain_human_injections() or []
            except Exception:
                _pending_injections = []
        if _pending_injections:
            try:
                if _try_start_task(task_id, agent_id):
                    try:
                        _task_store().update(task_id, state="running")
                    except Exception:
                        pass
                    threading.Thread(target=_rt._dispatch_chat,
                                     args=(task_id, _pending_injections, max_steps,
                                           full_access, agent_id, model),
                                     daemon=True).start()
            except Exception:
                pass

def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

async def _stream_gen(task_id: str = "", sub: str = ""):
    """SSE 流：按 task_id 订阅，只推送该 task 的事件。

    task_id 为空（全局订阅，前端首屏默认）时，透明跟随「当前正在运行」的 task 的
    专属 outbox —— 否则前端在拿到 task_id 之前（status 事件到达前的窗口）会收不到
    任何 chat/thinking/toolcall 事件，表现为「对话区全程无输出」。无运行中的 task
    时退化为空全局 outbox（兼容）。

    ``sub``（plan M3「outbox 多播扇出」）：非空 = **旁路订阅者**（语音桥 / 第二屏）。
    它不清空 outbox，只按自己的 seq 游标取新增，因此可以和前端主订阅并存，
    两边都能拿到完整事件流，不再互相抢事件。
    """
    sub_id = task_id or "_global"
    _sub = str(sub or "").strip()
    _eff_last = ""
    sse_open()
    try:
        while True:
            # 全局订阅透明跟随当前运行中的 task，消除订阅竞态丢事件
            eff = sub_id
            if eff == "_global":
                rid = _running_task_id()
                if rid:
                    eff = rid
            _eff_last = eff
            parts = outbox_drain(eff, _sub)

            # 顺序：思考 → 口播 → 工具卡 → 审批 → chat 殿后（保证沉在对话最末）→ debug
            for m in parts.get("thinking", []):
                yield _sse("thinking", m)
            for m in parts.get("message", []):
                yield _sse("message", m)
            for m in parts.get("toolcall", []):
                yield _sse("toolcall", m)
            for m in parts.get("approval", []):
                yield _sse("approval", m)
            for m in parts.get("approval_resolved", []):
                yield _sse("approval_resolved", m)
            for m in parts.get("chat", []):
                yield _sse("chat", m)
            for m in parts.get("debug", []):
                yield _sse("debug", m)

            # 2) status + 数据快照（task-scoped）
            tid = task_id or _running_task_id() or ""
            running = _is_task_running(tid) if tid else _is_any_running()
            _ctx = manager.get(tid, AGENT_MAIN) if tid else None
            yield _sse("status", {
                "running": running,
                "task_id": tid,
                "project_id": _ctx.project_id if _ctx else "",
                "config_hash": _ctx.config_hash if _ctx else "",
            })
            if tid:
                yield _sse("skills", {"skills": _read_skills(tid)})
                yield _sse("world", {"world": _read_world(tid)})
                yield _sse("collected", {"collected": _read_latest_collected(tid)})

            # 3) trajectory 增量（task-scoped）
            if tid:
                d = _trajectory_dir(tid)
                if d.is_dir():
                    files = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
                    if files:
                        f = files[0]
                        cursor_key = tid
                        with _cursor_lock:
                            cur = _traj_cursor.get(cursor_key)
                            start = cur[1] if (cur and cur[0] == str(f)) else 0
                        try:
                            lines = f.read_text(encoding="utf-8").splitlines()
                            for ln in lines[start:]:
                                if ln.strip():
                                    yield _sse("trajectory_append", {"step": json.loads(ln)})
                            with _cursor_lock:
                                _traj_cursor[cursor_key] = (str(f), len(lines))
                        except Exception:
                            pass

            # 4) 感知推送（去场景化 §9 S3）：不再每 1.8s 无脑推 screenshot。

            # 真流式打字机（问题3-B）：缩短轮询间隔，使 token 增量尽快推到前端。
            # 增量已按 block_id 合并到单条 outbox 项，不会因高频 flush 而刷爆。
            await asyncio.sleep(0.3)
    except asyncio.CancelledError:
        return
    finally:
        # 旁路订阅者断开：清掉游标，下次重连从当前窗口续读（不重放整段历史）
        if _sub:
            try:
                outbox_reset_cursor(_eff_last or sub_id, _sub)
            except Exception:
                pass
        sse_close()

@router.get("/live")
async def live(task_id: str = ""):
    """当前轮实时过程快照（页面刷新 / 切任务后回放用）。

    实时过程只写内存 outbox，且被 SSE 消费即清空、不落盘；进行中的这一轮在会话历史
    里也不存在。故另存一份**按 id upsert 的有界快照**，供前端挂载/切任务时种进过程流，
    避免「执行中刷新后全程无输出、直到本轮结束才一次性出现」。

    running=false 时快照无意义（该轮已结束，历史里已有带 steps 的完整轮次），前端据此跳过。
    """
    tid = task_id or (_running_task_id() or "")
    if tid:
        try:
            _paths().validate_identifier(tid, "task_id")
        except ValueError:
            return JSONResponse({"ok": False, "error": "task_id 格式不合法"}, status_code=422)
    data = live_snapshot(tid)
    data["task_id"] = tid
    data["running"] = _is_task_running(tid) if tid else _is_any_running()
    return JSONResponse(data)

@router.get("/stream")
async def stream(task_id: str = "", sub: str = ""):
    """SSE 流。``?task_id=xxx`` 订阅特定 task；``?sub=voice`` 声明旁路订阅者（多播）。

    ``sub`` 为空 = 主订阅者（前端，消费即清空）；非空 = 旁路订阅者（语音桥 / 第二屏），
    按自己的游标读、不清空，两者可同时订阅同一 task（plan M3）。
    """
    if task_id:
        paths = _paths()
        try:
            paths.validate_identifier(task_id, "task_id")
        except ValueError:
            return JSONResponse({"ok": False, "error": "task_id 格式不合法"}, status_code=422)
    return StreamingResponse(_stream_gen(task_id, sub), media_type="text/event-stream",
                            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@router.post("/stop")
async def api_stop(request: Request):
    """用户主动停止运行中的 agent（按 task_id + agent_id 寻址；缺省回退当前运行体）。

    阶段 0.5：控制接口不再依赖全局 _loop，全部按 (task_id, agent_id) 查 AgentRun。
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body or {}
    task_id = str(body.get("task_id") or "") or (_running_task_id() or "")
    agent_id = str(body.get("agent_id") or AGENT_MAIN)
    rec = manager.get(task_id, agent_id)
    loop = rec.loop if rec else None
    if loop is not None:
        loop.request_stop()
    _finish_task(task_id, agent_id)
    # P1.4: 用户主动停止 → task 状态置 aborted（区分于自然 done/failed）
    if task_id:
        try:
            TaskStore = _task_store()
            meta = TaskStore.get(task_id)
            if meta and meta.get("state") not in ("done", "failed"):
                TaskStore.update(task_id, state="aborted")
        except Exception:
            pass
    push_chat("system", "⏹ 已请求停止，正在中断当前执行", task_id=task_id)
    return JSONResponse({"ok": True, "msg": "已请求停止"})

@router.post("/inject")
async def api_inject(request: Request):
    """M8 软注入：运行中插入人类纠偏（不打断当前 turn，主 agent 下一轮可见）。

    阶段 0.5：按 (task_id, agent_id) 寻址运行体；缺省回退当前运行体。
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body or {}
    text = str((body or {}).get("text") or "").strip()
    if not text:
        return JSONResponse({"ok": False, "error": "text 不能为空"}, status_code=400)
    task_id = str(body.get("task_id") or "") or (_running_task_id() or "")
    agent_id = str(body.get("agent_id") or AGENT_MAIN)
    rec = manager.get(task_id, agent_id)
    loop = rec.loop if rec else None
    if loop is None:
        return JSONResponse({"ok": False, "error": "当前没有运行中的任务"}, status_code=409)
    ok = loop.inject_message(text)
    if not ok:
        return JSONResponse({"ok": False, "error": "注入失败（编排未运行或无 checkpointer）"},
                            status_code=409)
    # 作为用户消息落入对话区（与正常聊天同一 user 气泡样式），而非 system 提示
    push_chat("user", text, task_id=task_id)
    # 落库到会话历史（与 /chat、/wake 同链路），刷新后仍在
    try:
        TaskStore = _task_store()
        ProjectStore = _project_store()
        _meta = TaskStore.get(task_id)
        _pid = _meta.get("project_id", "") if _meta else ""
        _sid = _meta.get("session_id", "") if _meta else ""
        if _pid and _sid:
            ProjectStore.append_message(_pid, _sid, "user", text, task_id=task_id)
    except Exception:
        pass
    return JSONResponse({"ok": True, "msg": "已注入"})

@router.post("/chat")
async def api_chat(request: Request):
    """统一 agent 入口：一个会话 = 一个 task。

    用户消息进来后：
    - 若 task_id 为空（首条消息）：建 task 实体（落盘），objective 先用首条消息，
      agent 开始回后由前端/调用方用提炼出的任务名回填。
    - 否则沿用既有 task 历史。
    - 内部跑通用 agent 循环（brain.chat → tool_calls 派发 → 标准 role=tool 回填 →
      继续），闲聊与执行同一入口，由大脑自决。
    """
    from backend.api.schemas_runtime import ChatRequest
    from pydantic import ValidationError
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
    try:
        req = ChatRequest.model_validate(payload)
    except ValidationError as e:
        return JSONResponse({"ok": False, "error": f"请求参数校验失败: {e.errors()}"}, status_code=422)
    messages = [{"role": m.role, "content": m.content} for m in req.messages]
    task_id = req.task_id or ""
    # 默认步数上限：前端未传（或传 0）时回退到 config 的 runtime.default_max_steps
    # （替代原先写死的 40），该值由 Web 设置面板持久化到 ~/.omniagent/config.yaml。
    max_steps = req.max_steps or int(config.get_config("runtime.default_max_steps", 40))
    full_access = req.full_access
    # 未显式传 full_access 时，回退到该 task 持久化的默认值（写入 task.json）
    if full_access is None and task_id:
        try:
            TaskStore = _task_store()
            meta = TaskStore.get(task_id)
            if meta:
                full_access = bool(meta.get("full_access", False))
        except Exception:
            pass
    last_user = messages[-1].get("content", "") if messages else ""
    if not last_user:
        return JSONResponse({"ok": False, "error": "最后一条消息内容为空"}, status_code=422)

    # 原子检查：已有任务在跑则拒绝；force=True（立即发送 / barge-in）例外——
    # 仅打断**同一 task** 的运行轮（不隐式杀别的会话），硬停后立即派发新消息。
    if _rt._is_any_running():
        _rid = _rt._running_task_id()
        if not req.force:
            return JSONResponse({"ok": False, "error": f"已有任务在运行（task_id={_rid}），请等待结束或先停止"}, status_code=409)
        if not task_id or _rid != task_id:
            return JSONResponse({"ok": False, "error": f"运行中的是另一个任务（task_id={_rid}），不能在本会话强制发送"}, status_code=409)
        _rec_f = manager.get(_rid, AGENT_MAIN)
        _loop_f = _rec_f.loop if _rec_f else None
        if _loop_f is None:
            return JSONResponse({"ok": False, "error": "当前任务没有可打断的运行体"}, status_code=409)
        # 打断标记由 _dispatch_chat 读取即消费：跳过本轮 TAM 沉淀（不背提炼尾巴）、口播半句标 [被打断]
        _rec_f.force_interrupted = True
        _loop_f.request_stop()
        # 等旧线程自然收尾（finally 里 _finish_task 清 running 态）——不提前 flip 状态，
        # 避免与旧线程 finally 收尾竞态。request_stop 是硬取消（S1），收尾通常 <1s。
        _deadline = time.time() + 5.0
        while _rt._is_any_running() and time.time() < _deadline:
            await asyncio.sleep(0.1)
        if _rt._is_any_running():
            _rec_f.force_interrupted = False
            return JSONResponse({"ok": False, "error": "打断超时，上一轮仍在收尾，请稍后重试"}, status_code=409)
        # 等旧线程尾批 outbox 推送（流式增量 / 终态记录）落定，保证时序：
        # 旧轮内容 → [被打断] → 新消息。只等 running=False 会撞上尾批推送未落定的竞态，
        # 前端按 SSE 到达顺序渲染，旧轮内容会被排到新消息下面。
        await asyncio.sleep(0.35)

    # 首条消息：自动建 task（落盘），agent 开始回后才算真正产生会话
    TaskStore = _task_store()
    ProjectStore = _project_store()
    if not task_id:
        meta = TaskStore.create(objective=last_user.strip())
        task_id = meta["task_id"]
    else:
        meta = TaskStore.get(task_id)
        if meta is None:
            return JSONResponse({"ok": False, "error": "task not found"}, status_code=404)
        # 「新建会话」（POST /tasks 空 objective，挂在某项目下）首条消息自动命名，
        # 与「首消息建任务」的 objective 来源对齐；已有名字的任务绝不覆盖。
        if not str(meta.get("objective") or "").strip():
            try:
                TaskStore.update(task_id, objective=last_user.strip()[:4000])
                meta = TaskStore.get(task_id)
            except Exception:
                pass

    # 会话人格（plan §3.1「人格 = 会话参数」）：显式请求 > 任务持久化 > 配置缺省。
    # 持久化的目的是 **handoff 透传**：chat 模式派出去的任务、以及同一个 task 的后续
    # 轮次，都继承同一个人格，否则语音里交代的事会落成默认人格的记忆。
    _persona = str(req.persona or "").strip() or str((meta or {}).get("persona") or "")
    if req.persona and _persona != str((meta or {}).get("persona") or ""):
        try:
            TaskStore.update(task_id, persona=_persona)
            meta = TaskStore.get(task_id) or meta
        except Exception:
            pass

    # P2.1: 持久化会话历史——每条 user/assistant 消息写 jsonl
    project_id = meta.get("project_id", "")
    session_id = meta.get("session_id", "")
    if project_id and session_id:
        try:
            ProjectStore.append_message(project_id, session_id, "user", last_user, task_id=task_id)
        except Exception:
            pass

    # lane（plan §4）：mode=task 走 work lane，其余走 chat lane。二者互不阻塞
    # （干活期间照常聊天），但同一个 task 永远只允许一个 run。
    _lane = "work" if req.mode == "task" else "chat"
    # 原子标记 task 为 running（阶段 0.5：复合键 (task_id, "main")）
    if not _try_start_task(task_id, AGENT_MAIN, lane=_lane):
        _rid = _rt._running_task_id()
        return JSONResponse({"ok": False, "error": f"已有任务在运行（task_id={_rid}），请等待结束或先停止"}, status_code=409)

    # 更新 task 状态为 running
    try:
        TaskStore.update(task_id, state="running")
    except Exception:
        pass

    push_chat("user", last_user, task_id=task_id)
    threading.Thread(target=_rt._dispatch_chat,
                     args=(task_id, messages, max_steps, full_access, AGENT_MAIN, req.model,
                           req.mode, _persona), daemon=True).start()
    return JSONResponse({"ok": True, "task_id": task_id, "msg": "已下发",
                         "mode": req.mode, "persona": _persona})

@router.post("/wake")
async def api_wake(request: Request):
    """T5.1（SA-1 MVP）：通用事件唤醒端点——让**暂停/已有任务**被外部事件续跑。

    与 ``/chat`` 完全同链路（会话追加 -> 置 running -> 后台派发执行），差别只在
    触发来源：本端点面向「事件 / 定时 / 外部系统」，消息自动标注 ``[事件唤醒]``
    以便区分人工消息与事件触发。纯通用机制，无任何领域 / 业务硬编码预设。

    请求体：``{"task_id": str, "message": str}``

    错误码：400（参数缺失/空消息/非法 JSON）、404（任务不存在）、
    409（已有任务在运行，杜绝并发冲突）。
    """
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "请求体不是合法 JSON"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"ok": False, "error": "请求体必须是 JSON 对象"}, status_code=400)

    task_id = str(payload.get("task_id") or "").strip()
    message = str(payload.get("message") or "").strip()
    if not task_id:
        return JSONResponse({"ok": False, "error": "task_id 不能为空"}, status_code=400)
    if not message:
        return JSONResponse({"ok": False, "error": "message 不能为空"}, status_code=400)

    TaskStore = _task_store()
    try:
        meta = TaskStore.get(task_id)
    except Exception:
        meta = None
    if meta is None:
        return JSONResponse({"ok": False, "error": "task not found"}, status_code=404)

    # 并发冲突：已有任务在跑（含本任务自身）一律 409
    if _rt._is_any_running() or _rt._is_task_running(task_id):
        return JSONResponse(
            {"ok": False, "error": f"已有任务在运行（task_id={_rt._running_task_id()}），请等待结束或先停止"},
            status_code=409)

    # 事件消息：统一标注来源，便于区分人工 / 事件触发
    wake_text = f"[事件唤醒] {message}"
    messages = [{"role": "user", "content": wake_text}]

    # 持久化会话历史（与 /chat 同链路）
    ProjectStore = _project_store()
    project_id = meta.get("project_id", "")
    session_id = meta.get("session_id", "")
    if project_id and session_id:
        try:
            ProjectStore.append_message(project_id, session_id, "user", wake_text, task_id=task_id)
        except Exception:
            pass

    if not _try_start_task(task_id, AGENT_MAIN):
        return JSONResponse(
            {"ok": False, "error": f"已有任务在运行（task_id={_rt._running_task_id()}），请等待结束或先停止"},
            status_code=409)
    try:
        TaskStore.update(task_id, state="running")
    except Exception:
        pass

    push_chat("user", wake_text, task_id=task_id)
    max_steps = int(config.get_config("runtime.default_max_steps", 40))
    threading.Thread(target=_rt._dispatch_chat,
                     args=(task_id, messages, max_steps, None, AGENT_MAIN), daemon=True).start()
    return JSONResponse({"ok": True, "task_id": task_id, "msg": "已唤醒"})
