"""QQ 清唱链路离线测试：send_sing 的 record 段发送/去重 + msg_sing 工具的曲库查找与降级。

不连真实 NapCat、不加载 RVC 模型——capability 用注册表里的 fake。
"""
import os
import sys
import wave

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gateway.channels.napcat import NapcatAdapter  # noqa: E402
from omni_core.tools.base import TOOL_REGISTRY  # noqa: E402


@pytest.fixture(scope="module")
def msg_mod():
    """真 messaging 插件**延迟到用例期** import，用完把新注册的工具撤掉。

    顶层 import 会在 collection 阶段就把 7 个 msg_* 写进全局 TOOL_REGISTRY，
    而 test_plugin_loader 的 `_BASE_REGISTRY` 基线同样在 collection 期捕获 →
    把插件工具当成内核基线继承，随后装载真 `plugins/` 时判成「覆盖已注册工具」
    （HEAD 之前没有任何测试顶层 import 过 messaging，全量因此一直是绿的）。
    仓内既有惯例见 tests/test_m3_tools.py：要注册的模块在 fixture 里 import。
    """
    before = set(TOOL_REGISTRY)
    from plugins.messaging import plugin as m
    try:
        yield m
    finally:
        for name in [n for n in TOOL_REGISTRY if n not in before]:
            del TOOL_REGISTRY[name]


@pytest.fixture
def adapter():
    return NapcatAdapter({"enabled": True})


def _wav_file(tmp_path, seconds=0.2, sr=16000) -> str:
    import io

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(b"\x00\x00" * int(sr * seconds))
    p = tmp_path / "sing.wav"
    p.write_bytes(buf.getvalue())
    return str(p)


# === adapter.send_sing ===

def test_send_sing_missing_file(adapter):
    r = adapter.send_sing("private:222", "Z:/nope.wav", "song:x")
    assert r["ok"] is False and "不存在" in r["error"]


def test_send_sing_local_uses_wav_file_uri(adapter, monkeypatch, tmp_path):
    """本机 napcat：record 段带 .wav 扩展名 file URI（napcat 靠扩展名转 silk）。"""
    sent = []
    monkeypatch.setattr(adapter, "_api",
                        lambda a, p: sent.append((a, p)) or {"ok": True, "data": {}})
    r = adapter.send_sing("group:456", _wav_file(tmp_path), "song:qinghuaci")
    assert r["ok"] is True
    action, payload = sent[0]
    assert action == "send_group_msg" and payload["group_id"] == 456
    seg = payload["message"][0]
    assert seg["type"] == "record"
    assert seg["data"]["file"].startswith("file:///")
    assert seg["data"]["file"].lower().endswith(".wav")


def test_send_sing_remote_uses_base64(adapter, monkeypatch, tmp_path):
    sent = []
    adapter.http_url = "http://10.0.0.8:3000"
    monkeypatch.setattr(adapter, "_api",
                        lambda a, p: sent.append((a, p)) or {"ok": True, "data": {}})
    r = adapter.send_sing("private:222", _wav_file(tmp_path), "song:x")
    assert r["ok"] is True
    assert sent[0][1]["message"][0]["data"]["file"].startswith("base64://")


def test_send_sing_dedup_skips(adapter, monkeypatch, tmp_path):
    """同歌去重：_is_dup_send 命中 → ok=True + duplicate 标记，不发 record。"""
    sent = []
    monkeypatch.setattr(adapter, "_api",
                        lambda a, p: sent.append((a, p)) or {"ok": True, "data": {}})
    monkeypatch.setattr(adapter, "_is_dup_send", lambda cid, t: True)
    r = adapter.send_sing("private:222", _wav_file(tmp_path), "song:qinghuaci")
    assert r["ok"] is True and r.get("duplicate") is True
    assert sent == []


# === msg_sing 工具 ===

# _sing_manifest 返回的是解包后的 songs 字典（manifest.json 的 "songs" 字段内容）
_MANIFEST = {
    "qinghuaci": {"title": "青花瓷", "aliases": ["qhc"], "chorus": "C:/x/q.wav", "key": 0},
}


class _StubAdapter:
    name = "napcat"

    def reply_to(self, chat_id):
        return _StubReply()


class _StubReply:
    def __init__(self):
        self.sent = []

    def send_sing(self, wav_path, dedup_text=""):
        self.sent.append((wav_path, dedup_text))
        return {"ok": True}


@pytest.fixture
def stub_resolve(msg_mod, monkeypatch):
    """绕开真实通道解析（网关未运行）。"""
    monkeypatch.setattr(msg_mod, "_resolve",
                        lambda channel, target: (_StubAdapter(), "group:456", None))


def test_sing_lookup_by_alias(msg_mod, monkeypatch):
    monkeypatch.setattr(msg_mod, "_sing_manifest", lambda: _MANIFEST)
    assert msg_mod._sing_lookup("qhc")["title"] == "青花瓷"
    assert msg_mod._sing_lookup("青花瓷")["key"] == 0
    assert msg_mod._sing_lookup("qinghuaci")["chorus"] == "C:/x/q.wav"
    assert msg_mod._sing_lookup("不存在的歌") == {}


# 在线入库的真实条目形状：站点标题又长又花，能记住的是**点歌时那句歌名**（aliases）
_MANIFEST_ONLINE = {
    "bilibili_9f2a9fa23b": {"title": "【4K60FPS】陈奕迅《孤勇者》燃到深情! 谁说站在光里的才算英雄！",
                             "aliases": ["孤勇者", "4k60fps陈奕迅《孤勇者》燃到深情"],
                             "chorus": "C:/x/gy.wav", "key": 5},
    "qinghuaci": {"title": "青花瓷", "aliases": ["qhc"], "chorus": "C:/x/q.wav", "key": 0},
}


def test_sing_lookup_wrapped_and_punctuated(msg_mod, monkeypatch):
    """点歌话里**嵌着**已知歌名、或带空格/书名号 → 仍要认库内那条。

    漏判的代价不是"唱得差"，是同一首歌再走一遍在线获取（下载+分离 1–2 分钟，
    群友以为它忘了刚才答应过的事），所以反向匹配也算命中，长命中优先。
    """
    monkeypatch.setattr(msg_mod, "_sing_manifest", lambda: _MANIFEST_ONLINE)
    for q in ["孤勇者", "陈奕迅 孤勇者", "唱一首《孤勇者》呗", "  青花瓷 ", "qinghuaci"]:
        assert msg_mod._sing_lookup(q).get("_id"), q
    assert msg_mod._sing_lookup("孤勇者")["_id"] == "bilibili_9f2a9fa23b"
    assert msg_mod._sing_lookup("完全没听过的歌") == {}


def test_msg_sing_unknown_song(msg_mod, monkeypatch, stub_resolve):
    """库外 + 总闸关 → 「这首我还不会唱」。闸必须显式钉死：本用例过去依赖
    ~/.omniagent/plugins/voice.yaml 恰好没开 sing_online，在线曲库一旦开闸就变成
    返回 pending（断言直接失真）。"""
    from plugins.voice import song_source as ss

    monkeypatch.setattr(msg_mod, "_sing_manifest", lambda: _MANIFEST)
    monkeypatch.setattr(ss, "online_enabled", lambda: False)
    r = msg_mod.msg_sing("孤勇者", channel="napcat", target="group:456")
    assert r["ok"] is False and "没有这首歌" in r["error"]
    assert "不会唱" in r.get("note", "")


def test_msg_sing_capability_missing(msg_mod, monkeypatch, stub_resolve):
    monkeypatch.setattr(msg_mod, "_sing_manifest", lambda: _MANIFEST)

    class _CapMod:
        @staticmethod
        def get_capability(name):
            return None

    import omni_core.tools.capability as cap_mod
    orig = cap_mod.get_capability
    cap_mod.get_capability = lambda name: None
    try:
        r = msg_mod.msg_sing("青花瓷", channel="napcat", target="group:456")
    finally:
        cap_mod.get_capability = orig
    assert r["ok"] is False and "voice_sing" in r["error"]


def test_msg_sing_happy_path(msg_mod, monkeypatch, tmp_path):
    """曲库命中 → capability 转换 → reply.send_sing 发送（song: 前缀去重键）。"""
    out_file = tmp_path / "out.wav"
    out_file.write_bytes(b"RIFFfake")
    monkeypatch.setattr(msg_mod, "_sing_manifest", lambda: _MANIFEST)
    calls = {}

    class _CapMod:
        @staticmethod
        def get_capability(name):
            def _cap(input_wav, key=0, seed=1234, index_rate=0.66):
                calls["cap"] = {"input": input_wav, "key": key, "seed": seed,
                                "index_rate": index_rate}
                return {"out": str(out_file), "sr": 48000, "seconds": 3.2, "dur": 50.0}
            return _cap

    class _Reply:
        def __init__(self):
            self.sent = []

        def send_sing(self, wav_path, dedup_text=""):
            self.sent.append((wav_path, dedup_text))
            return {"ok": True}

    holder = {}

    class _Adapter:
        name = "napcat"

        def reply_to(self, chat_id):
            holder["reply"] = _Reply()
            return holder["reply"]

    import omni_core.tools.capability as cap_mod
    orig = cap_mod.get_capability
    cap_mod.get_capability = _CapMod.get_capability
    # 原写法是直接赋值 msg_mod._resolve，会把 stub 永久漏给后面的用例（monkeypatch
    # 捕获到的"原值"已经是这个 lambda）——统一走 monkeypatch，用例结束自动还原。
    monkeypatch.setattr(msg_mod, "_resolve", lambda channel, target: (_Adapter(), "group:456", None))
    try:
        r = msg_mod.msg_sing("青花瓷", channel="napcat", target="group:456")
    finally:
        cap_mod.get_capability = orig
    assert r["ok"] is True and r.get("song") == "青花瓷"
    assert calls["cap"]["key"] == 0 and calls["cap"]["input"] == "C:/x/q.wav"
    assert holder["reply"].sent == [(str(out_file), "song:qinghuaci")]


def test_msg_sing_convert_failure_has_fallback_note(msg_mod, monkeypatch, stub_resolve):
    """转换失败 → 明确错误 + 降级话术（失败也要说话，plan §5 并发护栏）。"""
    monkeypatch.setattr(msg_mod, "_sing_manifest", lambda: _MANIFEST)

    class _CapMod:
        @staticmethod
        def get_capability(name):
            def _cap(input_wav, key=0, seed=1234, index_rate=0.66):
                raise RuntimeError("声库未部署")
            return _cap

    import omni_core.tools.capability as cap_mod
    orig = cap_mod.get_capability
    cap_mod.get_capability = _CapMod.get_capability
    try:
        r = msg_mod.msg_sing("青花瓷", channel="napcat", target="group:456")
    finally:
        cap_mod.get_capability = orig
    assert r["ok"] is False and "唱砸了" in r.get("note", "")


def test_msg_sing_library_inflight_does_not_reconvert(msg_mod, monkeypatch, tmp_path):
    """库内歌正在唱 → inflight=True，**不再跑第二次转换**（plan §5：不排队不重算）。

    转换 ~10s 且 worker 串行；放行第二次只会白抢一次 GPU（发送侧 dup 拦得住刷屏、
    拦不住重算）。这是本次修的行为契约。
    """
    import time as _time

    import omni_core.tools.capability as cap_mod
    from plugins.messaging.plugin import _SING_BUSY

    monkeypatch.setattr(msg_mod, "_sing_manifest", lambda: _MANIFEST)
    monkeypatch.setattr(msg_mod, "_resolve",
                        lambda channel, target: (_StubAdapter(), "group:456", None))
    key = msg_mod._sing_busy_key("qinghuaci", "song")
    _SING_BUSY[key] = _time.time()
    called = {}

    def _cap(*a, **kw):
        called["cap"] = True
        return {"out": str(tmp_path / "o.wav"), "seconds": 1.0}

    orig = cap_mod.get_capability
    cap_mod.get_capability = lambda name: _cap
    try:
        r = msg_mod.msg_sing("青花瓷", channel="napcat", target="group:456")
    finally:
        cap_mod.get_capability = orig
        _SING_BUSY.pop(key, None)
    assert r["ok"] is True and r.get("inflight") is True
    assert "在唱" in r.get("note", "")
    assert called == {}          # 一点没碰 GPU


def test_msg_sing_happy_path_releases_busy(msg_mod, monkeypatch, tmp_path):
    """转换结束必须释放占位，否则这首歌从此永远"在唱了"。"""
    import omni_core.tools.capability as cap_mod
    from plugins.messaging.plugin import _SING_BUSY

    out_file = tmp_path / "out.wav"
    out_file.write_bytes(b"RIFFfake")
    monkeypatch.setattr(msg_mod, "_sing_manifest", lambda: _MANIFEST)
    monkeypatch.setattr(msg_mod, "_resolve",
                        lambda channel, target: (_StubAdapter(), "group:456", None))
    orig = cap_mod.get_capability
    cap_mod.get_capability = lambda name: (lambda *a, **kw: {"out": str(out_file), "seconds": 1.0})
    try:
        r = msg_mod.msg_sing("青花瓷", channel="napcat", target="group:456")
        assert r["ok"] is True
        assert _SING_BUSY == {}, "占位没释放"
    finally:
        cap_mod.get_capability = orig
        _SING_BUSY.clear()


def test_send_sing_dedup_key_symmetric(adapter, monkeypatch, tmp_path):
    """不传 dedup_text 时，"查"和"记"必须是同一个键。

    原实现查 `dedup_text or wav_path`、记 `dedup_text or "[唱歌] basename"`：
    dedup_text 为空时两边永不相等 → 刷屏保护形同虚设（且 wav_path 是按
    key/seed 哈希的产物名，本来也不稳定）。
    """
    wav = _wav_file(tmp_path)
    sent = []
    monkeypatch.setattr(adapter, "_api",
                        lambda a, p: sent.append((a, p)) or {"ok": True, "data": {}})
    monkeypatch.setattr(adapter, "_napcat_is_local", lambda: False)  # base64 路径，避开临时文件
    r1 = adapter.send_sing("group:456", wav)
    r2 = adapter.send_sing("group:456", wav)
    assert r1["ok"] is True
    assert r2.get("duplicate") is True
    assert len(sent) == 1, "第二次应当被去重拦下，不该再发 record"


def test_dispatch_allowlist_contains_msg_sing():
    """外部通道 allowlist 必须含 msg_sing（工具面 = 注册 ∩ 通道门，漏了模型拿不到）。"""
    import inspect

    from gateway import dispatch

    assert "msg_sing" in inspect.getsource(dispatch)
