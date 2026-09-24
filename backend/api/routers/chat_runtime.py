"""核心会话运行时：/chat /wake /stop /inject /stream + 派发与 SSE（零逻辑改动）。"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

from backend.api.routers.helpers import (
    AGENT_MAIN,
    Any,
    Dict,
    List,
    Optional,
    TOOL_REGISTRY,
    _collect_user_corrections,
    _config_hash,
    _cursor_lock,
    _ensure_outbox,
    _finish_task,
    _is_any_running,
    _is_task_running,
    _make_brain_cfg,
    _merge_message_step,
    _merge_thinking_step,
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

def _dispatch_chat(task_id: str, history: List[Dict[str, Any]], max_steps: int, full_access: Optional[bool] = None, agent_id: str = AGENT_MAIN):
    """统一 agent 入口（单入口处理闲聊 + 双 agent 任务执行）。

    一个会话 = 一个 task（首条消息由调用方在外部建好 task 并传入 task_id）。

    双 agent 协作模型（planner + executor）：
    - planner（主模型）：规划 / 反思，看全局。
    M10 统一入口（run_task）：
    - 主 agent（主模型）默认自己把任务做完；需要并行时它调 dispatch，
      编排层用 Send 扇出子 agent（runtime.executor 指向的模型，可配本地高频模型）。
    - 不再有「两层」的固定角色划分。

    单链路统一（2026-09-17）：闲聊与执行走同一条 run_task 入口，不再做"首条轻量 probe 判断"。
    每轮消息直接进 run_task，由大脑自己决定纯文本回答（方案 B 收尾）还是调用工具执行；
    后续消息（task_id 已有）沿用同一 task 上下文。
    """
    # 阶段 1：运行体句柄存于 RuntimeManager 复合键 (task_id, agent_id)，不再用全局 _loop
    _last_answer = ""  # 累积大脑最后一段口播，作为结尾「结果框」内容
    turn_steps: List[Dict[str, Any]] = []  # 本轮 agent 的执行轨迹（思考 + 工具调用）
    # 新的一轮：清空实时过程快照，避免 GET /live 把上一轮的残留过程回放给前端
    clear_live_snapshot(task_id)
    try:
        from omni_core.local.loop import ToolLoop, TaskSpec

        brain_cfg, executor_cfg = _make_brain_cfg()
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

        def _on_tool_call(role: str, name: str, arguments: str, result: str, model: str):
            # 后端校验（verify / verify_done）属内核门控，不进用户对话区（理由仍走 debug 面板）
            if name in ("verify", "verify_done"):
                return
            # 对话区实时「工具调用」卡片 -> SSE `toolcall` 事件 -> 前端对话框
            push_tool_call(role, name, arguments, result, model, task_id=task_id)
            turn_steps.append({"type": "tool_call", "model": model, "name": name,
                               "arguments": arguments or "", "result": result or ""})

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

        loop = ToolLoop(brain_cfg, executor_cfg=executor_cfg, verbose=True,
                        full_access=full_access,
                        config_snapshot=cfg, agent_id=agent_id,
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

        # 阶段 1：把运行级配置快照哈希、project_id、工具 registry、环境后端
        # 写入 context，使「每个 run 可完整导出运行配置」成立（阶段 1 验收项 + 目标 3）。
        if rec is not None:
            rec.config_hash = _config_hash(cfg)
            rec.project_id = _pid
            rec.tool_registry = TOOL_REGISTRY
            rec.execution_backend = loop.exec

        # B3：用服务端会话记录（含结构化 steps）重建模型上下文，使模型记得上一轮做了什么。
        # 前端回传的 history 只有扁平 role/content，不含 steps，故以会话存储为准。
        _session_records: List[Dict[str, Any]] = []
        if _pid and _sid:
            try:
                _session_records = ProjectStore.read_session(_pid, _sid)
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

        def _push(role: str, text: str, extra: Optional[dict] = None, debug: bool = False):
            push_chat(role, text, extra=extra, debug=debug, task_id=task_id)

        last_user = ""
        for m in reversed(history):
            if m.get("role") == "user":
                last_user = m.get("content", "")
                break

        # K2/K4：从会话记录提取 C₁ 裁决基准（上一轮 assistant 结论）与用户纠偏
        # （K4 第三蒸馏来源，config 关时返回空）。仅读 session，零写入。
        _prev_assistant = ""
        for r in reversed(_session_records):
            if (r.get("role") == "assistant") and (r.get("content") or "").strip():
                _prev_assistant = r.get("content", "")
                break
        _corrections = _collect_user_corrections(_session_records, _prev_assistant, last_user)

        # ---- 单链路统一（2026-09-17 用户确认）：不再做闲聊 probe 快判 ----
        # 每轮消息直接进统一入口 run_task：模型自己判断纯文本回答（方案 B 收尾）
        # 还是调工具执行。删 probe 后少一次 LLM 调用；闲聊/任务/追问展示统一；
        # 会话历史经 TaskSpec.history 单路注入（替代旧 prior_context，不再双重注入）。

        # ---- 执行任务：统一入口（主 agent，按需派发子 agent） ----
        # F2.2：收尾语义来自 task.json（task_mode，缺省 oneshot；缺省行为不变）
        _task_mode = str((_meta or {}).get("task_mode", "oneshot") or "oneshot")
        spec = TaskSpec(objective=last_user, done_when="",
                        task_id=task_id, project_id=None, max_steps=max_steps,
                        history=list(model_messages), corrections=_corrections,
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
        result = loop.run_task(spec)
        _reason = result.get("reason") or ""
        # K1：Curator 蒸馏/合并完成后，复用 SSE debug 通道推 memory_updated 事件，
        # 前端右栏轻提示 + Memory Tab 刷新统计。仅在有实际产物时推送，避免噪音。
        _cr = (result.get("curator_report") or {})
        _distilled = int(_cr.get("rollouts_distilled", 0) or 0)
        _merged = int(_cr.get("memory_merged", 0) or 0)
        if _distilled > 0 or _merged > 0:
            push_chat("system", "记忆已更新（蒸馏/合并）",
                      extra={"kind": "memory_updated", "payload": {"distilled": _distilled, "merged": _merged}},
                      debug=True, task_id=task_id)
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
        # K2：任务结束内嵌采集信号（A=success / B=assistant 结论 / C1=对话批准 / C2=观测评审）。
        # 纯前向、零写入任务原始数据；异常静默不影响主流程。
        try:
            from omni_core.local import signals as _sig
            _sig.collect_run_signal(
                task_id=task_id,
                run_id=str(result.get("run_id") or f"r_{int(time.time())}"),
                success=bool(result.get("success")),
                assistant_text=conclusion,
                objective=last_user,
                prev_assistant=_prev_assistant,
                next_user_msg=last_user,
                terminal_observation=None,
            )
        except Exception:
            pass

        _extra = {"steps": turn_steps, "meta": meta, "final": True}
        # 校验结论（如「无校验条件，信任大脑」）只进调试面板，不污染用户对话
        if _reason:
            _debug_push("verify", {"title": "完成校验", "reason": _reason})
        # 大模型最后给出的结论 + 完整过程流：归到 agent 一角色（不再发独立 system 回执；
        # 机器状态收进 meta，作为 agent turn 的小字脚注），自然沉在对话最末。
        if conclusion or turn_steps:
            _push("agent", conclusion, extra=_extra)
            _append_assistant(conclusion, extra=_extra)
    except Exception as e:
        tb = traceback.format_exc()
        push_chat("agent", f"❌ 对话循环失败：{type(e).__name__}: {e}",
                  extra={"traceback": tb[:2000]}, task_id=task_id)
    finally:
        rec = manager.get(task_id, agent_id)
        if rec is not None:
            rec.loop = None
        _finish_task(task_id, agent_id)
        # 更新 task 状态为 done/failed（P1.4 任务状态机）
        try:
            _task_store().update(task_id, state="done")
        except Exception:
            pass

def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

async def _stream_gen(task_id: str = ""):
    """SSE 流：按 task_id 订阅，只推送该 task 的事件。

    task_id 为空（全局订阅，前端首屏默认）时，透明跟随「当前正在运行」的 task 的
    专属 outbox —— 否则前端在拿到 task_id 之前（status 事件到达前的窗口）会收不到
    任何 chat/thinking/toolcall 事件，表现为「对话区全程无输出」。无运行中的 task
    时退化为空全局 outbox（兼容）。
    """
    sub_id = task_id or "_global"
    try:
        while True:
            # 全局订阅透明跟随当前运行中的 task，消除订阅竞态丢事件
            eff = sub_id
            if eff == "_global":
                rid = _running_task_id()
                if rid:
                    eff = rid
            box = _ensure_outbox(eff)

            # 1c) thinking outbox（对话区实时「深度思考」块）
            tmsgs = list(box["thinking"])
            box["thinking"].clear()
            for m in tmsgs:
                yield _sse("thinking", m)

            # 1e) message outbox（对话区实时「口播」气泡，真流式增量）
            mmsgs = list(box["message"])
            box["message"].clear()
            for m in mmsgs:
                yield _sse("message", m)

            # 1d) toolcall outbox（对话区实时「工具调用」卡片）
            tcmsgs = list(box["toolcall"])
            box["toolcall"].clear()
            for m in tcmsgs:
                yield _sse("toolcall", m)

            # 1) chat outbox（task-scoped，含结尾结果框 / system 状态）殿后，确保沉在对话最末
            chat_msgs = list(box["chat"])
            box["chat"].clear()
            for m in chat_msgs:
                yield _sse("chat", m)

            # 1b) debug outbox（task-scoped，旁路推送，右栏日志）
            dmsgs = list(box["debug"])
            box["debug"].clear()
            for m in dmsgs:
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
async def stream(task_id: str = ""):
    """SSE 流。支持 ?task_id=xxx 订阅特定 task 的事件（task-scoped）。"""
    if task_id:
        paths = _paths()
        try:
            paths.validate_identifier(task_id, "task_id")
        except ValueError:
            return JSONResponse({"ok": False, "error": "task_id 格式不合法"}, status_code=422)
    return StreamingResponse(_stream_gen(task_id), media_type="text/event-stream",
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
    push_chat("system", f"💬 已注入指示：{text}", task_id=task_id)
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

    # 原子检查：已有任务在跑则拒绝
    if _rt._is_any_running():
        _rid = _rt._running_task_id()
        return JSONResponse({"ok": False, "error": f"已有任务在运行（task_id={_rid}），请等待结束或先停止"}, status_code=409)

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

    # P2.1: 持久化会话历史——每条 user/assistant 消息写 jsonl
    project_id = meta.get("project_id", "")
    session_id = meta.get("session_id", "")
    if project_id and session_id:
        try:
            ProjectStore.append_message(project_id, session_id, "user", last_user, task_id=task_id)
        except Exception:
            pass

    # 原子标记 task 为 running（阶段 0.5：复合键 (task_id, "main")）
    if not _try_start_task(task_id, AGENT_MAIN):
        _rid = _rt._running_task_id()
        return JSONResponse({"ok": False, "error": f"已有任务在运行（task_id={_rid}），请等待结束或先停止"}, status_code=409)

    # 更新 task 状态为 running
    try:
        TaskStore.update(task_id, state="running")
    except Exception:
        pass

    push_chat("user", last_user, task_id=task_id)
    threading.Thread(target=_rt._dispatch_chat,
                     args=(task_id, messages, max_steps, full_access, AGENT_MAIN), daemon=True).start()
    return JSONResponse({"ok": True, "task_id": task_id, "msg": "已下发"})

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
