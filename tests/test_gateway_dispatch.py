"""网关派发（dispatch）离线单测：会话复用、标题/历史分离、显式发送。

不触达 LLM：把 ToolLoop 换成假实现，只验证落库与回发语义。
"""
import json
import threading

import config as cfg_mod
from gateway import dispatch
from omni_core.local.task_store import ProjectStore, TaskStore


class _FakeLoop:
    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs

    def run_task(self, spec):
        return {"success": True, "summary": "好的"}


def _fake_brain(monkeypatch):
    monkeypatch.setattr(cfg_mod, "load_config", lambda: {"brain": {"api_key": "test"}})
    import omni_core.local.loop as loop_mod
    monkeypatch.setattr(loop_mod, "ToolLoop", _FakeLoop)


def test_chat_window_reuses_single_task(monkeypatch):
    """同一 (project, session) 的两条消息 → 同一个 task（侧栏只一条，历史连续）。"""
    _fake_brain(monkeypatch)

    r1 = dispatch.dispatch_sync("（这是你的 QQ 场合自述）第一条", session_id="s_qq_reuse",
                                title="与「阿伟」(222)的对话", display_text="第一条")
    r2 = dispatch.dispatch_sync("（这是你的 QQ 场合自述）第二条", session_id="s_qq_reuse",
                                title="与「阿伟」(222)的对话", display_text="第二条")

    assert r1.task_id == r2.task_id
    tasks = [t for t in TaskStore.list() if t.get("session_id") == "s_qq_reuse"]
    assert len(tasks) == 1
    # 标题 = 通道自声明的展示名；objective 仍是模型侧目标（第一轮原文），两者分离
    assert tasks[0]["title"] == "与「阿伟」(222)的对话"
    assert tasks[0]["state"] == "done"
    assert tasks[0]["finished_at"]

    msgs = [m for m in ProjectStore.read_session(tasks[0]["project_id"], "s_qq_reuse")
            if m.get("role") == "user"]
    # 会话历史写消息原文（场合自述只喂模型，不进气泡）
    assert [m["content"] for m in msgs] == ["第一条", "第二条"]


def test_no_session_id_creates_separate_tasks(monkeypatch):
    """cron/一次性任务（无 session_id）不复用：每次一条新任务。"""
    _fake_brain(monkeypatch)
    r1 = dispatch.dispatch_sync("定时任务 A")
    r2 = dispatch.dispatch_sync("定时任务 A")
    assert r1.task_id != r2.task_id


# === 跨进程事件桥（B0）：轮次开始/结束通知 Web 端刷新 ======================

def test_ui_notified_on_run_start_and_done(monkeypatch):
    """持久会话：run_started + run_done 各通知一次，且指向同一个 task。"""
    _fake_brain(monkeypatch)
    calls = []
    monkeypatch.setattr(dispatch, "_notify_ui",
                        lambda kind, tid, pid="": calls.append((kind, tid, pid)))
    r = dispatch.dispatch_sync("在吗", session_id="s_qq_notify", title="与「阿伟」的对话")
    assert [c[0] for c in calls] == ["run_started", "run_done"]
    assert all(c[1] == r.task_id for c in calls)


def test_ui_not_notified_for_temp_task(monkeypatch):
    """临时任务（cron 无项目、无会话）不进 UI 列表，不必通知。"""
    _fake_brain(monkeypatch)
    calls = []
    monkeypatch.setattr(dispatch, "_notify_ui",
                        lambda kind, tid, pid="": calls.append((kind, tid, pid)))
    dispatch.dispatch_sync("一次性任务")
    assert calls == []


# === 显式发送（QQ 等 IM）：模型调 msg_send_text 才发，最终回答不再自动外发 =====

class _Reply:
    """最小 ReplyChannel 桩：记录真正发出去的内容。"""

    channel_name = "napcat"
    chat_id = "group:745710148"

    def __init__(self):
        self.sent: list = []

    def send_text(self, text):
        self.sent.append(text)
        return {"ok": True}

    def send_card(self, payload):
        return {"ok": True}

    def send_voice(self, text):
        self.sent.append(("voice", text))
        return {"ok": False, "error": "no tts"}


class _LoopNoSend(_FakeLoop):
    """模型这一轮只说内部话，没有调发送工具。"""

    def run_task(self, spec):
        self.kwargs["on_llm_delta"]("brain", "message", "b1", "这条不用回。", "m")
        return {"success": True, "summary": "这条不用回。"}


class _LoopWithSend(_FakeLoop):
    """模型主动调了 msg_send_text。"""

    def run_task(self, spec):
        cb = self.kwargs["on_tool_call"]
        cb("brain", "msg_send_text",
           json.dumps({"text": "晚上好呀"}, ensure_ascii=False),
           json.dumps({"ok": True}, ensure_ascii=False), "m")
        self.kwargs["on_llm_delta"]("brain", "message", "b1", "已回一句。", "m")
        return {"success": True, "summary": "已回一句。"}


def _use_loop(monkeypatch, cls):
    _fake_brain(monkeypatch)
    import omni_core.local.loop as loop_mod
    monkeypatch.setattr(loop_mod, "ToolLoop", cls)


def _assistant_records(session_id):
    task = [t for t in TaskStore.list() if t.get("session_id") == session_id][0]
    recs = ProjectStore.read_session(task["project_id"], session_id)
    return [r for r in recs if r.get("role") == "assistant"]


def test_explicit_mode_does_not_auto_send(monkeypatch):
    """explicit：模型没调发送工具 → 一个字符都不发出去，历史标记未发送。"""
    _use_loop(monkeypatch, _LoopNoSend)
    reply = _Reply()
    dispatch.dispatch_sync("（这是你的 QQ）在吗", session_id="s_qq_explicit_1",
                           display_text="在吗", reply=reply, reply_mode="explicit")
    assert reply.sent == []
    rec = _assistant_records("s_qq_explicit_1")[-1]
    assert rec["extra"]["send"] == {"mode": "explicit", "count": 0}
    # 独白仍留在任务里（给主人看），只是没发出去
    assert "这条不用回" in rec["content"]


def test_explicit_mode_records_what_was_sent(monkeypatch):
    """explicit：模型调了 msg_send_text → 历史里记真实发出的内容 + count。"""
    _use_loop(monkeypatch, _LoopWithSend)
    reply = _Reply()
    dispatch.dispatch_sync("（这是你的 QQ）在吗", session_id="s_qq_explicit_2",
                           display_text="在吗", reply=reply, reply_mode="explicit")
    assert reply.sent == []  # dispatch 不代发（工具自己发，这里是桩没真发）
    rec = _assistant_records("s_qq_explicit_2")[-1]
    assert rec["extra"]["send"] == {"mode": "explicit", "count": 1}
    assert rec["content"] == "晚上好呀"


def test_auto_mode_still_auto_sends(monkeypatch):
    """auto（飞书/Web 直连）：最终回答自动回发，行为不变。"""
    _use_loop(monkeypatch, _FakeLoop)
    reply = _Reply()
    dispatch.dispatch_sync("在吗", session_id="s_direct_1",
                           display_text="在吗", reply=reply, reply_mode="auto")
    assert reply.sent == ["好的"]


def test_internal_reason_never_sent(monkeypatch):
    """内部判定文案（reason）不是人话：取不到可外发文本就静默，不凑占位语。"""
    _use_loop(monkeypatch, _FakeLoop)
    reply = _Reply()
    monkeypatch.setattr(dispatch, "_extract_summary", lambda result: "")
    dispatch.dispatch_sync("在吗", session_id="s_direct_2",
                           display_text="在吗", reply=reply, reply_mode="auto")
    assert reply.sent == []


def test_sent_texts_parses_python_repr_result():
    """工具结果经 SDK 序列化可能是 repr（单引号）——也要认出来（回归）。"""
    steps = [{"type": "tool_call", "name": "msg_send_text",
              "arguments": '{"text": "别瞎说"}',
              "result": "{'ok': True, 'message_id': 1, 'chunks': 1}"}]
    assert dispatch._sent_texts(steps) == ["别瞎说"]


class _LoopNoted(_FakeLoop):
    """模拟真实发送工具：成功后由工具侧记账（context.note_sent）。"""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.text = "记一笔"

    def run_task(self, spec):
        from gateway.channels import context as ch_ctx

        ch_ctx.note_sent(self.text)
        return {"success": True}


def test_explicit_mode_counts_tool_side_notes(monkeypatch):
    """发送记账以工具侧为准（不依赖工具结果字符串解析），且每轮清零。"""
    _fake_brain(monkeypatch)
    import omni_core.local.loop as loop_mod
    monkeypatch.setattr(loop_mod, "ToolLoop", _LoopNoted)

    dispatch.dispatch_sync("在吗", session_id="s_qq_noted_1", display_text="在吗",
                           reply=_Reply(), reply_mode="explicit")
    rec = _assistant_records("s_qq_noted_1")[-1]
    assert rec["extra"]["send"] == {"mode": "explicit", "count": 1}
    assert rec["content"] == "记一笔"

    # 第二轮只记一次 → count 仍为 1（说明 reset 生效，没有累加）
    dispatch.dispatch_sync("再来", session_id="s_qq_noted_1", display_text="再来",
                           reply=_Reply(), reply_mode="explicit")
    rec2 = _assistant_records("s_qq_noted_1")[-1]
    assert rec2["extra"]["send"]["count"] == 1


def test_extract_summary_ignores_reason_and_dump():
    """_extract_summary 不取 reason、也不 JSON dump 整个结果（防内部字段外泄）。"""
    assert dispatch._extract_summary({"reason": "无校验条件，信任大脑", "steps": 3}) == ""
    assert dispatch._extract_summary({"summary": "好的"}) == "好的"
    assert dispatch._extract_summary("纯字符串") == "纯字符串"


def test_reply_context_visible_from_other_thread():
    """工具跑在 async_bridge 的事件循环线程 → 回发句柄必须跨线程可见。"""
    from gateway.channels import context as ch_ctx

    class _R:
        pass

    r = _R()
    ch_ctx.set_current_reply(r)
    seen = {}
    t = threading.Thread(target=lambda: seen.update(
        reply=ch_ctx.get_current_reply()))
    t.start()
    t.join()
    assert seen["reply"] is r
    ch_ctx.set_current_reply(None)


def test_msg_send_target_resolved_across_threads():
    """msg_send_text 缺省 target 依赖来源句柄：换线程也要能解析出会话（回归）。"""
    import importlib.util
    from pathlib import Path

    from gateway.channels import context as ch_ctx

    path = Path(__file__).resolve().parents[1] / "plugins" / "messaging" / "plugin.py"
    spec = importlib.util.spec_from_file_location("_msg_plugin_ctx_test", path)
    plugin = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plugin)

    class _Adapter:
        name = "ctx_test_chan"  # 唯一名，避免污染同进程其它用例的注册表
        default_chat_id = ""

    class _SrcReply:
        channel_name = "ctx_test_chan"
        chat_id = "private:121244478"

        def send_text(self, text):
            return {"ok": True}

        def send_card(self, payload):
            return {"ok": True}

    ch_ctx.register(_Adapter())
    ch_ctx.set_current_reply(_SrcReply())
    out = {}
    t = threading.Thread(target=lambda: out.update(
        zip(("adapter", "chat_id", "err"), plugin._resolve("", ""))))
    t.start()
    t.join()
    ch_ctx.set_current_reply(None)
    assert out["err"] == ""
    assert out["chat_id"] == "private:121244478"  # 缺省发回来源会话


def test_explicit_mode_grants_send_tool_only(monkeypatch):
    """explicit 通道纯对话也要放行「说话」工具；auto 通道不放行（工具仍全关）。"""
    captured = {}

    class _Cap(_LoopNoSend):
        def __init__(self, *a, **k):
            captured.update(k)
            super().__init__(*a, **k)

    _fake_brain(monkeypatch)
    import omni_core.local.loop as loop_mod
    monkeypatch.setattr(loop_mod, "ToolLoop", _Cap)

    dispatch.dispatch_sync("在吗", session_id="s_qq_tools_1", reply=_Reply(),
                           reply_mode="explicit", tools_enabled=False)
    assert captured["tools_enabled"] is False
    assert captured["tool_allowlist"] == ["msg_send_text", "msg_send_voice", "msg_send_card",
                                          "msg_group_history"]

    dispatch.dispatch_sync("在吗", session_id="s_direct_tools_1", reply=_Reply(),
                           reply_mode="auto", tools_enabled=False)
    assert captured["tool_allowlist"] is None


# ── msg_send_voice：发送方式由模型选，通道不支持就明确报错 ────────────────

def _load_msg_plugin(tag: str):
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "plugins" / "messaging" / "plugin.py"
    spec = importlib.util.spec_from_file_location(tag, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _VoiceAdapter:
    def __init__(self, name: str, reply):
        self.name = name
        self.default_chat_id = "private:777"
        self._reply = reply

    def reply_to(self, chat_id):
        return self._reply


class _VoiceReply:
    def __init__(self, support_voice=True):
        self.channel_name = "voice_chan"
        self.chat_id = "private:777"
        self.voices = []
        self.texts = []
        if support_voice:
            self.send_voice = self._voice

    def _voice(self, text):
        self.voices.append(text)
        return {"ok": True, "message_id": 1}

    def send_text(self, text):
        self.texts.append(text)
        return {"ok": True}

    def send_card(self, payload):
        return {"ok": True}


def test_msg_send_voice_sends_voice_and_notes_sent():
    """支持语音的通道 → 真的走 send_voice，并记进「本轮已发出」。"""
    from gateway.channels import context as ch_ctx

    plugin = _load_msg_plugin("_msg_plugin_voice_ok")
    reply = _VoiceReply()
    ch_ctx.register(_VoiceAdapter("voice_ok_chan", reply))
    ch_ctx.reset_sent()
    try:
        r = plugin.msg_send_voice("你好呀", channel="voice_ok_chan")
        assert r["ok"] is True
        assert reply.voices == ["你好呀"] and reply.texts == []
        assert ch_ctx.take_sent() == ["你好呀"]
    finally:
        ch_ctx.reset_sent()


def test_msg_send_voice_unsupported_channel_errors():
    """通道无 send_voice → 明确报错并指路 msg_send_text（不静默改发文字）。"""
    from gateway.channels import context as ch_ctx

    plugin = _load_msg_plugin("_msg_plugin_voice_unsupported")
    reply = _VoiceReply(support_voice=False)
    ch_ctx.register(_VoiceAdapter("voice_no_chan", reply))
    r = plugin.msg_send_voice("你好呀", channel="voice_no_chan")
    assert r["ok"] is False and "msg_send_text" in r["error"]
    assert reply.texts == [] and ch_ctx.take_sent() == []


def test_msg_send_text_no_longer_auto_switches_to_voice(monkeypatch):
    """msg_send_text 不再因为「入站是语音」偷偷改发语音（回归）。"""
    from gateway.channels import context as ch_ctx

    plugin = _load_msg_plugin("_msg_plugin_text_only")
    reply = _VoiceReply()
    ch_ctx.register(_VoiceAdapter("voice_mix_chan", reply))
    ch_ctx.reset_sent()
    try:
        r = plugin.msg_send_text("说点文字", channel="voice_mix_chan")
        assert r["ok"] is True
        assert reply.texts == ["说点文字"] and reply.voices == []
    finally:
        ch_ctx.reset_sent()


# ── msg_group_history：群上下文自助查询（A 自动带少量，B 由模型按需多拉） ──

def test_msg_group_history_reads_adapter_history():
    from gateway.channels import context as ch_ctx

    plugin = _load_msg_plugin("_msg_plugin_group_hist")
    reply = _VoiceReply()
    adapter = _VoiceAdapter("group_hist_chan", reply)
    calls = []

    def _hist(gid, limit=20):
        calls.append((gid, limit))
        return [{"who": "张三", "text": "你好", "message_id": "1"}]

    adapter.group_history = _hist
    ch_ctx.register(adapter)
    try:
        r = plugin.msg_group_history(group_id="333", limit=5, channel="group_hist_chan")
        assert r["ok"] is True and r["group_id"] == "333" and r["count"] == 1
        assert calls == [("333", 5)]
    finally:
        ch_ctx.set_current_reply(None)


def test_msg_group_history_needs_group_or_group_id():
    """私聊里不传 group_id → 明确报错（不猜群号）。"""
    from gateway.channels import context as ch_ctx

    plugin = _load_msg_plugin("_msg_plugin_group_hist_missing")
    reply = _VoiceReply()  # chat_id = private:777
    adapter = _VoiceAdapter("group_hist_chan2", reply)
    adapter.group_history = lambda gid, limit=20: []
    ch_ctx.register(adapter)
    ch_ctx.set_current_reply(reply)
    try:
        r = plugin.msg_group_history(channel="group_hist_chan2")
        assert r["ok"] is False and "group_id" in r["error"]
    finally:
        ch_ctx.set_current_reply(None)


def test_msg_group_history_unsupported_channel_errors():
    """通道没有群历史能力 → 明确错误，而不是静默返回空。"""
    plugin = _load_msg_plugin("_msg_plugin_group_hist_unsupported")
    r = plugin.msg_group_history(group_id="333", channel="napcat_unsupported_chan")
    assert r["ok"] is False and "无可用通道" in r["error"]


# ── 外部来源加固：发送目标锁死，不能借它跨通道/跨会话外发 ─────────────────

class _RecReply:
    def __init__(self, chan, cid):
        self.channel_name = chan
        self.chat_id = cid
        self.texts = []
        self.voices = []

    def send_text(self, text):
        self.texts.append(text)
        return {"ok": True}

    def send_card(self, payload):
        return {"ok": True}

    def send_voice(self, text):
        self.voices.append(text)
        return {"ok": True}


class _RecAdapter:
    """记录 reply_to 拿到了哪个会话（验证目标是否被强制改写）。"""

    def __init__(self, name, group_hist=None):
        self.name = name
        self.default_chat_id = ""
        self.targets = []
        self._group_hist = group_hist

    def reply_to(self, chat_id):
        self.targets.append(chat_id)
        return _RecReply(self.name, chat_id)

    def group_history(self, gid, limit=20):
        if callable(self._group_hist):
            return self._group_hist(gid, limit)
        return [{"who": "张三", "text": "群里在聊", "message_id": "1"}]


def test_external_source_locks_send_target():
    """外部来源（陌生人/群友）→ 忽略 channel/target，只发回来源会话。"""
    from gateway.channels import context as ch_ctx

    plugin = _load_msg_plugin("_msg_plugin_ext_lock")
    src = _RecAdapter("src_chan")
    other = _RecAdapter("other_chan")
    ch_ctx.register(src)
    ch_ctx.register(other)
    ch_ctx.set_current_reply(_RecReply("src_chan", "group:333"))
    ch_ctx.set_source_external(True)
    try:
        r = plugin.msg_send_text("hi", channel="other_chan", target="private:999")
        assert r["ok"] is True
        assert other.targets == []                 # 没走别的通道
        assert src.targets == ["group:333"]        # 只发回来源会话
        r2 = plugin.msg_send_voice("你好", channel="other_chan", target="private:999")
        assert r2["ok"] is True
        assert other.targets == []
        assert src.targets == ["group:333", "group:333"]
    finally:
        ch_ctx.set_current_reply(None)
        ch_ctx.set_source_external(False)


def test_external_source_limits_group_history():
    """外部来源只能查当前群；传别的群号一律拒绝。"""
    from gateway.channels import context as ch_ctx

    plugin = _load_msg_plugin("_msg_plugin_ext_group")
    ch_ctx.register(_RecAdapter("src_chan2"))
    ch_ctx.set_current_reply(_RecReply("src_chan2", "group:333"))
    ch_ctx.set_source_external(True)
    try:
        bad = plugin.msg_group_history(group_id="999", channel="src_chan2")
        assert bad["ok"] is False and "当前群" in bad["error"]
        ok = plugin.msg_group_history(channel="src_chan2")
        assert ok["ok"] is True and ok["group_id"] == "333"
    finally:
        ch_ctx.set_current_reply(None)
        ch_ctx.set_source_external(False)


def test_trusted_source_keeps_explicit_target():
    """主人自己发起（外部=false）时，channel/target 参数照旧生效（不误伤正常用法）。"""
    from gateway.channels import context as ch_ctx

    plugin = _load_msg_plugin("_msg_plugin_trusted")
    src = _RecAdapter("src_chan3")
    other = _RecAdapter("other_chan3")
    ch_ctx.register(src)
    ch_ctx.register(other)
    ch_ctx.set_current_reply(_RecReply("src_chan3", "group:333"))
    ch_ctx.set_source_external(False)
    try:
        r = plugin.msg_send_text("hi", channel="other_chan3", target="private:999")
        assert r["ok"] is True
        assert other.targets == ["private:999"]
        assert src.targets == []
    finally:
        ch_ctx.set_current_reply(None)


def test_dispatch_marks_external_source_during_run(monkeypatch):
    """dispatch 跑任务期间打标（tools_enabled=False → external），结束后复位。"""
    from gateway.channels import context as ch_ctx

    seen = {}

    class _Cap(_FakeLoop):
        def run_task(self, spec):
            seen["external"] = ch_ctx.get_source_external()
            return {"success": True, "summary": "好的"}

    monkeypatch.setattr(cfg_mod, "load_config", lambda: {"brain": {"api_key": "test"}})
    import omni_core.local.loop as loop_mod
    monkeypatch.setattr(loop_mod, "ToolLoop", _Cap)

    dispatch.dispatch_sync("在吗", session_id="s_ext_1", reply=_Reply(),
                           reply_mode="explicit", tools_enabled=False)
    assert seen["external"] is True
    assert ch_ctx.get_source_external() is False

    seen.clear()
    dispatch.dispatch_sync("在吗", session_id="s_ext_2", reply=_Reply(),
                           reply_mode="auto", tools_enabled=True)
    assert seen["external"] is False
