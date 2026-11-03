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
    monkeypatch.setattr(cfg_mod, "load_config", lambda: {"brain": {"reasoning_mode": "native"}})
    # 端点真源是 models.json（router 解析）：测试里 stub 目录，不读真实用户配置
    from omni_core.brain import router as model_router
    monkeypatch.setattr(model_router, "_catalog", lambda: {
        "providers": {"testprov": {"label": "t", "base_url": "http://127.0.0.1:9",
                                   "api_key": "test", "models": [{"id": "m1", "label": "m1", "vision": True}]}},
        "defaults": {"main": "testprov/m1"},
    })
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
    chat_id = "group:333333"

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
        chat_id = "private:100001"

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
    assert out["chat_id"] == "private:100001"  # 缺省发回来源会话


def test_explicit_mode_grants_send_tool_only(monkeypatch):
    """explicit 通道纯对话也要放行「说话」工具 + 只读自取工具；auto 通道不放行（工具仍全关）。"""
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
    allow = captured["tool_allowlist"]
    # 说话（三种方式）+ 群上下文只读 + 记忆/场景自取（P1 方案 C：检索结果永不自动注入，
    # 被点名 run 也得自己能查）——全是只读或回来源会话的动作，无写操作。
    assert set(allow) >= {"msg_send_text", "msg_send_voice", "msg_send_card",
                          "msg_group_history", "msg_group_image",
                          "memory_recall", "read_scene"}
    assert not any(x.startswith(("memory_remember", "memory_update", "tool_", "shell"))
                   for x in allow), "外部通道不得放行写记忆或通用执行工具"

    dispatch.dispatch_sync("在吗", session_id="s_direct_tools_1", reply=_Reply(),
                           reply_mode="auto", tools_enabled=False)
    assert captured["tool_allowlist"] is None


def test_dispatch_passes_chat_scope_to_l0_index(monkeypatch, tmp_path):
    """P2：派发写会话气泡时把「哪个聊天窗口 + 消息还是任务」一起交给 L0 索引。

    IM 来源 kind=chat（人话），cron/手动派发 kind=task（机器指令）——同一张表里不区分，
    conversation_search 就会把派单提示词当聊天旧事命中（检索串味）。
    """
    from omni_core import memory_brain

    seen = []
    monkeypatch.setattr(memory_brain, "index_l0_message",
                        lambda sid, role, text, **kw: seen.append((sid, role, text, kw)) or "")

    _use_loop(monkeypatch, _LoopNoSend)
    dispatch.dispatch_sync("今晚吃什么", session_id="s_qq_l0", project_id="d-l0",
                           reply=_Reply(), reply_mode="explicit", tools_enabled=False,
                           source="msg:napcat")
    user_rows = [s for s in seen if s[1] == "user" and "今晚吃什么" in s[2]]
    assert user_rows, seen
    assert user_rows[0][3]["chat_id"] == "group:333333"
    assert user_rows[0][3]["kind"] == "chat"

    seen.clear()
    dispatch.dispatch_sync("跑一下日报", session_id="s_cron_l0", project_id="d-l0c",
                           source="cron")
    task_rows = [s for s in seen if s[1] == "user" and "跑一下日报" in s[2]]
    assert task_rows and task_rows[0][3]["kind"] == "task"
    assert task_rows[0][3]["chat_id"] == ""       # 无回发句柄 = 没有 IM 聊天窗口


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


def test_msg_group_history_keyword_queries_l0_index(monkeypatch, tmp_path):
    """P2：带 keyword 的群历史改查本地 L0 索引（OneBot 实时历史只有最近几十条，翻旧事靠它）。"""
    from gateway.channels import context as ch_ctx
    from omni_core import memory_brain

    monkeypatch.setattr(memory_brain, "_BRAIN_PATH", str(tmp_path / "brain.db"))
    monkeypatch.setattr(memory_brain, "enabled", lambda: True)
    monkeypatch.setattr(ch_ctx, "get_source_external", lambda: False)

    plugin = _load_msg_plugin("_msg_plugin_group_hist_kw")
    reply = _VoiceReply()
    adapter = _VoiceAdapter("group_hist_kw_chan", reply)
    used = []
    adapter.group_history = lambda gid, limit=20: used.append(gid) or []
    ch_ctx.register(adapter)
    ch_ctx.set_current_reply(reply)
    memory_brain.index_l0_message("s_g333", "user", "「小明」(10001): 便签放抽屉里",
                                  chat_id="group:333")
    memory_brain.index_l0_message("s_g999", "user", "便签买过三盒", chat_id="group:999")
    try:
        r = plugin.msg_group_history(group_id="333", channel="group_hist_kw_chan",
                                     keyword="便签")
        assert r["ok"] is True and r["count"] == 1
        assert "便签放抽屉里" in r["messages"][0] and "小明(10001)" in r["messages"][0]
        assert used == [], "关键词路径不得再打实时接口"
        empty = plugin.msg_group_history(group_id="333", channel="group_hist_kw_chan",
                                         keyword="不存在的东西")
        assert empty["ok"] is True and empty["count"] == 0
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


# ── msg_group_image：按需取群图（工具结果带 attachments → S9 注入主模型） ──

def test_msg_group_image_returns_attachments():
    from gateway.channels import context as ch_ctx

    plugin = _load_msg_plugin("_msg_plugin_group_image")
    reply = _VoiceReply()
    adapter = _VoiceAdapter("group_image_chan", reply)
    calls = []

    def _img(gid, mid):
        calls.append((gid, mid))
        return ["data:image/jpeg;base64,QUJD"], ""

    adapter.group_image_data_urls = _img
    ch_ctx.register(adapter)
    try:
        r = plugin.msg_group_image(message_id="42", group_id="333",
                                   channel="group_image_chan")
        assert r["ok"] is True and r["count"] == 1 and calls == [("333", "42")]
        assert r["attachments"] == [{"type": "image",
                                     "data_url": "data:image/jpeg;base64,QUJD"}]
        assert "一次性" in r["note"]          # 提示模型附件只在当轮可见
    finally:
        ch_ctx.set_current_reply(None)


def test_msg_group_image_errors_surface():
    """取图失败（无图/跨群/下载失败）→ 通道侧错误原样上抛，不静默。"""
    from gateway.channels import context as ch_ctx

    plugin = _load_msg_plugin("_msg_plugin_group_image_err")
    reply = _VoiceReply()
    adapter = _VoiceAdapter("group_image_chan2", reply)
    adapter.group_image_data_urls = lambda gid, mid: ([], "该消息不属于目标群（已拒绝）")
    ch_ctx.register(adapter)
    try:
        r = plugin.msg_group_image(message_id="42", group_id="333",
                                   channel="group_image_chan2")
        assert r["ok"] is False and "不属于" in r["error"]
    finally:
        ch_ctx.set_current_reply(None)


def test_msg_group_image_unsupported_channel_errors():
    plugin = _load_msg_plugin("_msg_plugin_group_image_unsupported")
    r = plugin.msg_group_image(message_id="42", group_id="333", channel="napcat_no_chan")
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

    _fake_brain(monkeypatch)
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


# === 群上下文种子组装（2026-11-03 杠杆 1/2/3/5）+ 思考链持久化 ==================

class _CapturingLoop:
    """抓 TaskSpec 的假 Loop：验证注入给模型的历史形状。"""
    specs: list = []

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs

    def run_task(self, spec):
        _CapturingLoop.specs.append(spec)
        return {"success": True, "summary": "好的"}


class _ReasoningLoop(_CapturingLoop):
    """模拟 emitter：reasoning 增量（同 block_id 两段）+ message 增量。"""

    def run_task(self, spec):
        cb = self.kwargs.get("on_llm_delta")
        if cb:
            cb("brain", "reasoning", "brain-R0-x", "这是", "m")
            cb("brain", "reasoning", "brain-R0-x", "思考链", "m")
            cb("brain", "message", "brain-M0-x", "回话", "m")
        return super().run_task(spec)


def _rec(role, content, ts, steps=None, silent=False):
    extra: dict = {}
    if steps is not None:
        extra["steps"] = steps
    if silent:
        extra["silent"] = True
    return {"role": role, "content": content, "ts": ts, "extra": extra}


def test_history_builder_filters_silent_time_and_thought():
    """杠杆1/2/3：静默叙述跳过、出窗（>90min）旧话跳过、[上轮执行] 只留工具名。"""
    import time as _time
    from datetime import datetime, timezone

    now = _time.time()
    fmt = lambda sec_ago: datetime.fromtimestamp(now - sec_ago, tz=timezone.utc).isoformat()
    recs = [
        _rec("user", "两小时前的话题", fmt(2 * 3600)),             # 出窗
        _rec("assistant", "这轮不接话", fmt(1800), silent=True),    # 静默 → 跳过
        _rec("assistant", "回话", fmt(60), steps=[
            {"type": "thinking", "content": "很长" * 300},
            {"type": "tool_call", "name": "msg_send_text"},
            {"type": "tool_call", "name": "read_scene"},
        ]),
        _rec("user", "新消息", fmt(30)),
        _rec("system", "系统行", fmt(20)),
    ]
    items = dispatch._build_history_items(recs, explicit=False, now_ts=now)
    assert [i["content"] for i in items] == [
        "回话\n[上轮执行] 工具: msg_send_text, read_scene", "新消息"]
    assert "很长" not in items[0]["content"]              # 思考要点不进历史
    many = [_rec("user", f"m{i}", fmt(i)) for i in range(30)]
    assert len(dispatch._build_history_items(many, explicit=True, now_ts=now)) == 12


def test_silent_run_marked_and_skipped_from_history(monkeypatch):
    """零发言 run 落库带 silent 标；下一轮种子组装不再带它的叙述。"""
    _fake_brain(monkeypatch)
    import omni_core.local.loop as loop_mod
    monkeypatch.setattr(loop_mod, "ToolLoop", _CapturingLoop)
    _CapturingLoop.specs = []
    dispatch.dispatch_sync("（场合自述）第一条", session_id="s_silent", display_text="第一条")
    dispatch.dispatch_sync("（场合自述）第二条", session_id="s_silent", display_text="第二条")
    task = [t for t in TaskStore.list() if t.get("session_id") == "s_silent"][0]
    recs = ProjectStore.read_session(task["project_id"], "s_silent")
    finals = [m for m in recs if m.get("role") == "assistant"
              and not (m.get("extra") or {}).get("partial")]
    assert finals and all((m.get("extra") or {}).get("silent") for m in finals)
    # 第二轮的 history：只剩 user 记录，第一轮的「好的」叙述被剔除（杠杆1）
    hist = _CapturingLoop.specs[-1].history
    assert all(h["role"] == "user" for h in hist)
    assert [h["content"] for h in hist] == ["第一条", "第二条"]


def test_reasoning_delta_persisted_as_thinking_step(monkeypatch):
    """思考链修复：reasoning 增量按 block_id 累积成 thinking step 落库。"""
    _fake_brain(monkeypatch)
    import omni_core.local.loop as loop_mod
    monkeypatch.setattr(loop_mod, "ToolLoop", _ReasoningLoop)
    _CapturingLoop.specs = []
    r = dispatch.dispatch_sync("（场合自述）问题", session_id="s_think", display_text="问题")
    task = [t for t in TaskStore.list() if t["task_id"] == r.task_id][0]
    recs = ProjectStore.read_session(task["project_id"], "s_think")
    finals = [m for m in recs if m.get("role") == "assistant"
              and not (m.get("extra") or {}).get("partial")]
    thinks = [s for s in finals[0]["extra"]["steps"] if s.get("type") == "thinking"]
    assert len(thinks) == 1
    assert thinks[0]["content"] == "这是思考链"          # 同 block_id 两段累积
    assert thinks[0]["id"] == "brain-R0-x"
