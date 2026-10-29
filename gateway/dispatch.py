"""dispatch —— 公共派发库（通道无关，接入设计 v1.1 §6）

职责：objective → ToolLoop（复用 run_agent_autonomous 既有模式）→ 结果回发。
- **project 关联**：job/消息携带 project_id → 写入 TaskSpec.project_id →
  三层 AGENTS.md 注入自动取 projects/<pid>/AGENTS.md，trajectory/world-model
  也按项目归属落盘。定时任务因此与 project 天然关联。
- 串行队列：通道消息与 cron 任务共用一条 FIFO（单 worker 串行，不并发）。
- 结果回发：reply 非空（消息触发）→ 完成后经 ReplyChannel 回发；
  reply=None + notify_channel（cron）→ broadcast 到该通道默认会话。
- 失败直推：任务异常/超时由本库**不经 agent** 直接经通道 API 推错误文本
  （应急语义——agent 死了也要让调用方知道任务没成）。
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .channels import context
from .channels.base import ReplyChannel


@dataclass
class DispatchResult:
    task_id: str
    ok: bool
    summary: str = ""
    result: Dict[str, Any] = field(default_factory=dict)
    error: str = ""


_QUEUE: "queue.Queue[Dict[str, Any]]" = queue.Queue()
_WORKER: Optional[threading.Thread] = None


def _extract_summary(result: Any) -> str:
    """从 ToolLoop.run_task 的返回里抠出**可外发**的文本（结构防御式）。

    只认几个"人话"字段：``reason`` 是内核收尾判定文案（如「无校验条件，信任大脑」），
    曾经被当成回复发进聊天窗口；结构兜底的 JSON dump 也会把内部字段一起外泄。
    因此这里**绝不**取 reason、也不 dump 整个结果——没有可外发文本就返回空。
    """
    if isinstance(result, str):
        return result.strip()[:500]
    if not isinstance(result, dict):
        return ""
    for key in ("summary", "final", "final_answer", "answer", "output", "message"):
        v = result.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()[:500]
    return ""


#: 后端地址（桌面壳拉起网关时注入实际端口，默认按 8000）
_BACKEND_URL_ENV = "OMNIAGENT_BACKEND_URL"
_BACKEND_URL_DEFAULT = "http://127.0.0.1:8000"


def _notify_ui(kind: str, task_id: str, project_id: str = "") -> None:
    """跨进程事件桥（B0）：告诉 Web 后端「这个会话变了」，让前端不用刷新就更新。

    网关与 Web 后端是两个进程，SSE outbox 只活在后端内存里，所以网关跑完一轮
    前端根本不知道。这里 POST 后端内部端点，由后端推 ``tasks_changed`` 事件，
    前端收到后刷新任务列表、并按需重拉当前会话的历史。

    尽力而为：后端没起 / 端口不对 / 被拒 → 全部静默跳过，绝不拖慢消息处理。
    """
    if not task_id:
        return
    base = (os.environ.get(_BACKEND_URL_ENV) or _BACKEND_URL_DEFAULT).rstrip("/")
    try:
        import requests

        requests.post(f"{base}/api/runtime/internal/tasks_changed",
                      json={"kind": kind, "task_id": task_id,
                            "project_id": project_id or ""},
                      timeout=1.0)
    except Exception:
        pass


def _sent_texts(steps: List[Dict[str, Any]]) -> List[str]:
    """兜底：从本轮步骤里抠「成功发出的文本」（工具结果可能是 JSON 也可能是 repr）。"""
    import ast

    def _loads(raw: Any) -> Any:
        if isinstance(raw, (dict, list)):
            return raw
        text = str(raw or "").strip()
        if not text:
            return {}
        for parse in (json.loads, ast.literal_eval):
            try:
                v = parse(text)
                if isinstance(v, dict):
                    return v
            except Exception:
                continue
        return {}

    out: List[str] = []
    for s in steps or []:
        if s.get("type") != "tool_call" or s.get("name") != "msg_send_text":
            continue
        args = _loads(s.get("arguments"))
        res = _loads(s.get("result"))
        if isinstance(res, dict) and res.get("ok"):
            t = str(args.get("text") or "").strip()
            if t:
                out.append(t)
    return out


def _send_voice_reply(reply: ReplyChannel, body: str, wanted: bool) -> None:
    """语音对语音：入站是语音时优先语音回发（TTS）；不支持/失败 → 降级纯文本。"""
    send_voice = getattr(reply, "send_voice", None) if wanted else None
    if callable(send_voice):
        try:
            r = send_voice(body)
        except Exception as e:  # noqa: BLE001 —— 语音链路任何异常都降级，不让主人收不到话
            r = {"ok": False, "error": str(e)}
        if r.get("ok"):
            return
        print(f"[dispatch] 语音回发失败（降级文本）: {r.get('error', '')}")
    reply.send_text(body)


def _run_task_once(item: Dict[str, Any]) -> DispatchResult:
    task_id = item["task_id"]
    reply: Optional[ReplyChannel] = item.get("reply")
    notify_channel: Optional[str] = item.get("notify_channel")
    job_id: str = item.get("job_id", "")
    real_task_id = ""      # TaskStore 登记后的正式 ID（异常路径未创建时保持空）
    temp_task = False      # 无关联项目的临时任务：完成即删

    # 回发工具函数：优先消息来源句柄；cron 则用 notify 通道的默认会话
    def _send(text: str) -> None:
        try:
            if reply is not None:
                reply.send_text(text)
            elif notify_channel:
                adapter = context.get_adapter(notify_channel)
                if adapter:
                    adapter.broadcast(text)
        except Exception as e:  # 回发失败不影响任务状态
            print(f"[dispatch] 回发失败(任务{task_id}): {e}")

    try:
        import config
        from omni_core.local.loop import ToolLoop, TaskSpec
        from omni_core.local.task_store import TaskStore, ProjectStore

        brain_cfg = (config.load_config() or {}).get("brain", {})
        if not (brain_cfg.get("api_key") or brain_cfg.get("api_key_env")):
            raise RuntimeError("大脑未配置（config.brain 或环境变量），无法派发任务")

        # ── 任务落 TaskStore（UI 项目/会话列表实时可见过程）──
        # 三种形态：
        # ① 聊天窗口（带 session_id，如 QQ 私聊/群）→ **复用**该窗口已有的任务：
        #    一个窗口在侧栏只占一条，历史连续累积（对齐 Web「一个会话 = 一个 task」）；
        # ② 有关联项目的一次性任务（cron）→ 每次新建，持久留在项目下；
        # ③ 无项目无会话 → 临时任务，跑完即删（结果留 runs.jsonl）。
        project_id = item.get("project_id") or None
        session_id = item.get("session_id") or None
        display_text = str(item.get("display_text") or item["objective"])
        title = str(item.get("title") or "")
        # 回发模式（通道自声明，见 InboundMessage.reply_mode）：
        # - explicit（QQ 等 IM）：不自动发最终回答，只有模型调 msg_send_text 才发出；
        # - auto（Web/飞书这类"直接对模型说话"）：最终回答自动回发（原行为）。
        explicit = str(item.get("reply_mode") or "auto").strip().lower() == "explicit"
        existing = TaskStore.find_by_session(project_id, session_id)
        if existing is not None:
            meta = existing
            real_task_id = str(meta["task_id"])
            # 重跑同一会话：状态回 running，清掉上一轮的收尾字段
            TaskStore.update(real_task_id, state="running", success=None, finished_at=None)
            if title and not str(meta.get("title") or ""):  # 老任务补标题（一次性回填）
                try:
                    TaskStore.update(real_task_id, title=title)
                except Exception:
                    pass
        else:
            temp_task = project_id is None and session_id is None
            meta = TaskStore.create(objective=item["objective"], done_when=item.get("done_when", ""),
                                    project_id=project_id, session_id=session_id, title=title)
            real_task_id = meta["task_id"]
            TaskStore.update(real_task_id, state="running")
        if job_id:  # 派发即落 running 记录（详情页秒见反馈）
            from .scheduler import record_run
            record_run(job_id, real_task_id, "running")
        if not temp_task:  # 持久会话：告诉 Web 端「这条会话开始跑了」
            _notify_ui("run_started", real_task_id, str(meta.get("project_id") or ""))

        # ── 会话轨迹持久化（与 Web 端 chat_runtime 同格式）：无头任务（cron/消息）──
        # 也写入 projects/<pid>/sessions/<sid>.jsonl，前端打开任务即见完整过程流
        # （partial 增量 + final 收口，coalesce_records 可并回单轮）；否则只有
        # tasks/ 下的轨迹 jsonl，Web 历史接口读不到，界面空白。
        pid = meta.get("project_id") or ""
        sid = meta.get("session_id") or ""
        run_key = f"rk_{int(time.time() * 1000)}"
        turn_steps: list = []
        _flushed = 0
        _last_answer = ""
        # 显式模式：本轮到底发出去了什么（前端据此标「已发送 / 未发送」）
        send_info: Dict[str, Any] = {}

        def _flush_partial(force: bool = False) -> None:
            nonlocal _flushed
            if not (pid and sid):
                return
            if not (force or len(turn_steps) - _flushed >= 20):
                return
            if len(turn_steps) <= _flushed:
                return
            try:
                ProjectStore.append_message(
                    pid, sid, "assistant", "", task_id=real_task_id,
                    extra={"steps": turn_steps[_flushed:], "partial": True, "run_key": run_key})
                _flushed = len(turn_steps)
            except Exception:
                pass

        def _on_thinking(role: str, content: str, model: str) -> None:
            if role == "brain":
                turn_steps.append({"type": "thinking", "model": model, "content": content})
                _flush_partial()

        def _on_tool_call(role: str, name: str, arguments: str, result: str, model: str) -> None:
            if name in ("verify", "verify_done"):
                return
            turn_steps.append({"type": "tool_call", "model": model, "name": name,
                               "arguments": arguments or "", "result": result or ""})
            _flush_partial()

        def _on_llm_delta(role: str, kind: str, block_id: str, text: str, model: str) -> None:
            nonlocal _last_answer
            if kind == "message" and role == "brain":
                _last_answer = text

        def _record_final(content: str) -> None:
            if not (pid and sid):
                return
            extra: Dict[str, Any] = {"steps": turn_steps, "run_key": run_key}
            if explicit:
                # 显式模式：标记这一轮到底发出去没有（前端画「已发送 / 未发送」）
                extra["send"] = dict(send_info)
            try:
                ProjectStore.append_message(
                    pid, sid, "assistant", content, task_id=real_task_id, extra=extra)
            except Exception:
                pass

        if pid and sid:
            try:
                # 会话气泡写消息原文（display_text），场合自述只喂模型不进历史
                ProjectStore.append_message(pid, sid, "user", display_text,
                                            task_id=real_task_id)
            except Exception:
                pass

        # ── 固定会话连续对话：读会话历史注入 TaskSpec.history（对齐 Web /chat 链路）──
        # B3 同款：以服务端会话记录（含结构化 steps）重建模型上下文，跨消息记忆唯一来源
        history_items = []
        if pid and sid:
            try:
                _recs = ProjectStore.coalesce_records(ProjectStore.read_session(pid, sid))
                for r in _recs:
                    role = r.get("role")
                    if role not in ("user", "assistant"):
                        continue
                    ex = r.get("extra") or {}
                    content = str(r.get("content") or "")
                    steps = ex.get("steps") or []
                    # agent 轮：把已执行的步骤摘要并入口播文本，模型记得做过什么
                    if role == "assistant" and steps:
                        tool_names = [s.get("name") for s in steps if s.get("type") == "tool_call"]
                        thought = next((s.get("content", "") for s in reversed(steps)
                                        if s.get("type") == "thinking"), "")
                        seg = f"[上轮执行] 工具: {', '.join(filter(None, tool_names)) or '无'}"
                        if thought:
                            seg += f"；要点: {thought[:200]}"
                        content = f"{content}\n{seg}".strip()
                    if content:
                        history_items.append({"role": role, "content": content})
                history_items = history_items[-40:]  # 防超长（消息级滑窗，压缩归大脑）
            except Exception:
                history_items = []

        loop = ToolLoop(brain_cfg, verbose=False,
                        on_thinking=_on_thinking, on_tool_call=_on_tool_call,
                        on_llm_delta=_on_llm_delta,
                        # 外部消息（他人可达的通道）默认纯对话：见 submit 的 tools_enabled
                        tools_enabled=bool(item.get("tools_enabled", True)),
                        # 显式发送通道：纯对话也不给工具，但必须放行「说话」这几个动作
                        # （文字/语音/卡片——用什么方式说是模型自己的选择）+ 群历史查询
                        # （被 @ 接不上话时自己能看上下文，只读、不改任何状态）
                        tool_allowlist=(["msg_send_text", "msg_send_voice", "msg_send_card",
                                         "msg_group_history"] if explicit else None))
        spec = TaskSpec(
            objective=item["objective"],
            done_when=item.get("done_when", ""),
            max_steps=item.get("max_steps") or 40,
            task_id=real_task_id,
            project_id=meta.get("project_id"),
            history=history_items,
            attachments=list(item.get("attachments") or []),
        )
        # 当前任务回发句柄（线程绑定）→ msg_send_* 工具缺省发回来源会话。
        # 语音不再走隐式改写：发不发语音由模型调 msg_send_voice 决定。
        # 外部可达通道（tools_enabled=False）额外打标：发送工具只准回来源会话，
        # 防止"群友让它往飞书/别的 QQ 发消息"这种绕过工具白名单的跨通道外发。
        context.set_current_reply(reply)
        # 只有「他人发起的消息」才算外部来源；cron（reply=None）不是外部输入，
        # 不该被锁掉它主动发消息的能力。
        context.set_source_external(
            reply is not None and not bool(item.get("tools_enabled", True)))
        context.reset_sent()  # 清空上一轮的「已发出」记账
        try:
            result = loop.run_task(spec)
        finally:
            context.set_current_reply(None)
            context.set_source_external(False)

        summary = _extract_summary(result)
        # 最终口播（真实回答）；内部判定文案只进任务详情/日志，绝不外发
        answer = (_last_answer or "").strip() or summary
        # 显式模式：会话里记「实际发出去的内容」；没发出就记内部说明（前端标未发送）。
        # 首选工具侧的记账（唯一事实来源），步骤解析仅作兜底（结果字符串可能是 repr）。
        _noted = context.take_sent()
        sent_texts = (_noted or _sent_texts(turn_steps)) if explicit else []
        if explicit:
            send_info.update({"mode": "explicit", "count": len(sent_texts)})
        _record_final("\n\n".join(sent_texts) if sent_texts else answer)
        # run_task 真实返回：success / escalated / steps / trajectory_file …
        ok = bool(result.get("success", True)) and not result.get("escalated") if isinstance(result, dict) else True
        try:
            TaskStore.update(real_task_id, state="done" if ok else "failed", success=ok)
        except Exception:
            pass
        if temp_task:  # 临时任务：结果已记 runs.jsonl，索引即删（列表不再显示）
            try:
                TaskStore.remove(real_task_id)
            except Exception:
                pass
        else:  # 持久会话：通知 Web 端刷新该会话（B0 事件桥）
            _notify_ui("run_done", real_task_id, pid)
        dr = DispatchResult(task_id=real_task_id, ok=ok, summary=summary, result=result if isinstance(result, dict) else {"raw": str(result)})
        if job_id:
            from .scheduler import record_run
            record_run(job_id, real_task_id, "ok" if ok else "error", summary=answer)
        # 回发文案分两种场景（关键区别：对面是不是人）：
        # - reply 非空 = 消息通道里的人说的 → **只发正文**，不加 ✅/任务号/来源前缀
        #   （机器味的状态标签破坏拟人感，且来源标识对人无意义）；
        # - reply 为空 = cron / 广播推送 → 保留标识，便于在群里认出是哪条定时任务。
        _body = answer[:3500]
        if reply is None:
            # cron / 广播：没有"对话对象"，结果本身就是要推出去的东西
            _send(f"{'✅' if ok else '⚠️'} [{item.get('source', 'task')} {real_task_id[:8]}]\n"
                  f"{_body or '(这一轮没有产出内容)'}")
        elif not explicit and _body:
            # 直连通道（Web / 飞书这类"直接对模型说话"）：最终回答自动回发（原行为）。
            # 没有可外发文本就静默——绝不拿内部判定/占位语凑一句发给真人。
            _send_voice_reply(reply, _body, bool(item.get("voice_reply")))
        # explicit（QQ 等 IM）：不代发——模型已经自己调 msg_send_text 说话；
        # 没调用就这一轮静默，顶多在任务里留一段「未发送」的内部说明。
        return dr
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        print(f"[dispatch] 任务失败 {task_id}: {err}\n{traceback.format_exc()[-800:]}")
        # 失败也写会话终态：已收集的 steps 原样收口（前端能看到失败前做了什么）。
        # 防御：异常可能发生在记录器定义之前（import/TaskStore 失败），按局部名守卫。
        try:
            if "turn_steps" in locals():
                _record_final(str(((locals().get("result") or {}).get("reason")) or err))
        except Exception:
            pass
        # 失败也必须有结果：task 状态落库（若已创建）+ runs 记录
        try:
            from omni_core.local.task_store import TaskStore
            if real_task_id:
                TaskStore.update(real_task_id, state="failed", success=False)
                if temp_task:
                    TaskStore.remove(real_task_id)
        except Exception:
            pass
        if job_id:
            from .scheduler import record_run
            record_run(job_id, real_task_id or task_id, "error", error=err)
        if real_task_id and not temp_task:  # 失败也通知：前端要看到终态与错误，别停在"跑着"
            _notify_ui("run_done", real_task_id, str((locals().get("meta") or {}).get("project_id") or ""))
        # 失败直推（不经 agent——它可能正是死因）。同上方原则：对面是人就只说人话，
        # traceback / 任务号只进日志与任务详情，不甩进聊天窗口。
        if reply is not None:
            _send("我这出了点问题，这次没办成，稍后再试一次？")
        else:
            _send(f"🚨 任务失败 [{item.get('source', 'task')} {task_id[:8]}]: {err}"
                  f"（由网关直发，agent 可能已死）")
        return DispatchResult(task_id=real_task_id or task_id, ok=False, error=err)


def _worker_loop() -> None:
    while True:
        item = _QUEUE.get()
        if item is None:
            break
        try:
            _run_task_once(item)
        except Exception:  # _run_task_once 已兜底；此处防御 worker 自身
            traceback.print_exc()
        finally:
            _QUEUE.task_done()


def ensure_worker() -> None:
    """惰性启动串行 worker（网关/调度器共用）。"""
    global _WORKER
    if _WORKER is None or not _WORKER.is_alive():
        _WORKER = threading.Thread(target=_worker_loop, name="dispatch-worker", daemon=True)
        _WORKER.start()


def submit(objective: str, done_when: str = "", max_steps: int = 40, *,
           source: str = "manual", project_id: Optional[str] = None,
           reply: Optional[ReplyChannel] = None,
           notify_channel: Optional[str] = None, job_id: Optional[str] = None,
           session_id: Optional[str] = None,
           title: str = "",
           display_text: str = "",
           reply_mode: str = "auto",
           tools_enabled: bool = True,
           attachments: Optional[List[Dict[str, Any]]] = None,
           voice_reply: bool = False) -> str:
    """异步提交（网关消息/cron 用）。返回 task_id。

    session_id：显式指定会话（消息通道用 ``msg:<channel>:<chat_id>`` 固定会话——
    同一聊天窗口连续对话共享上下文，对齐 hermes 的 thread 绑定语义；不传则每任务新会话）。
    **带 session_id 的提交会复用该会话已有的 task**，一个聊天窗口在任务列表里只有一条。

    title：会话展示名（人看的标题，通道自声明，如「与「昵称」(1234)的对话」）；
    空则前端回退 objective。objective 是模型侧的目标文本，可能带通道附加的场合说明，
    不适合直接当标题，故两者分离。

    display_text：消息原文（不含场合说明），写会话历史用；空则回退 objective。

    reply_mode：通道自声明的回发模式。``"auto"``（缺省）= 最终回答自动回发；
    ``"explicit"`` = 不代发，只有模型显式调 ``msg_send_text`` 才发出（QQ 等 IM）。
    explicit 模式下纯对话通道仍会放行发送工具（tool_allowlist），否则模型无法说话。

    tools_enabled：能力门。False = 纯对话（无任何工具，模型物理上无法产生 tool_call）。
    外部可达的通道（他人能给账号发消息）必须传 False——否则任何给账号发消息的人
    都能驱动 shell/文件/设备工具。

    attachments：当轮多模态附件（dict 列表，如 ``{"type":"image","data_url":...}``）。
    **只作用于本次模型输入**，不进会话记录（历史仍按 objective 文本落盘，避免 base64
    撑爆 jsonl）。用 dict 泛化，以后加文件 / 音频不用改契约。

    voice_reply：入站消息来自语音 → 支持 send_voice 的 ReplyChannel 用语音回发
    （语音对语音），失败自动降级纯文本。
    """
    ensure_worker()
    task_id = uuid.uuid4().hex[:16]
    _QUEUE.put({"task_id": task_id, "objective": objective, "done_when": done_when,
                "max_steps": max_steps, "source": source, "project_id": project_id,
                "reply": reply, "notify_channel": notify_channel, "job_id": job_id or "",
                "session_id": session_id or "", "title": title or "",
                "display_text": display_text or "",
                "reply_mode": str(reply_mode or "auto").strip().lower(),
                "tools_enabled": bool(tools_enabled),
                "attachments": list(attachments or []),
                "voice_reply": bool(voice_reply)})
    return task_id


def dispatch_sync(objective: str, done_when: str = "", max_steps: int = 40, *,
                  source: str = "manual", project_id: Optional[str] = None,
                  reply: Optional[ReplyChannel] = None,
                  notify_channel: Optional[str] = None, job_id: Optional[str] = None,
                  session_id: Optional[str] = None,
                  title: str = "", display_text: str = "", reply_mode: str = "auto",
                  tools_enabled: bool = True,
                  attachments: Optional[List[Dict[str, Any]]] = None) -> DispatchResult:
    """同步执行（CLI/测试/「立即运行」用）。"""
    return _run_task_once({"task_id": uuid.uuid4().hex[:16], "objective": objective,
                           "done_when": done_when, "max_steps": max_steps, "source": source,
                           "project_id": project_id, "reply": reply, "notify_channel": notify_channel,
                           "job_id": job_id or "", "session_id": session_id or "",
                           "title": title or "",
                           "display_text": display_text or "",
                           "reply_mode": str(reply_mode or "auto").strip().lower(),
                           "tools_enabled": bool(tools_enabled),
                           "attachments": list(attachments or [])})


def queue_size() -> int:
    return _QUEUE.qsize()
