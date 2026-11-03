"""在线曲库离线测试：源插件加载器、总闸、single-flight、manifest 注册、msg_sing pending 路径。

不触网、不加载真源模块——源用 tmp 目录里的 fake 模块，重活全部 monkeypatch。
"""
import json
import os
import sys
import time as _time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from plugins.voice import sing_acquire as acq  # noqa: E402
from plugins.voice import song_source as ss  # noqa: E402
from omni_core.tools.base import TOOL_REGISTRY  # noqa: E402


@pytest.fixture(scope="module")
def msg_mod():
    """真 messaging 插件延迟到用例期 import（顶层 import 会污染全局 TOOL_REGISTRY，
    详见 tests/test_napcat_sing.py 同名 fixture 的注释）。"""
    before = set(TOOL_REGISTRY)
    from plugins.messaging import plugin as m
    try:
        yield m
    finally:
        for name in [n for n in TOOL_REGISTRY if n not in before]:
            del TOOL_REGISTRY[name]


FAKE_SOURCE = '''
import os

SOURCE_ID = "fakesrc"
def search(keyword, limit=3):
    return [{"id": "v999", "title": keyword + "(测试版)", "duration_s": 240}]
def fetch(candidate, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    p = os.path.join(out_dir, "audio.m4a")
    open(p, "wb").write(b"fake")
    return p
'''


@pytest.fixture
def fake_sources_dir(tmp_path, monkeypatch):
    d = tmp_path / "song_sources"
    d.mkdir()
    (d / "bilibili.py").write_text(FAKE_SOURCE, encoding="utf8")
    (d / "broken.py").write_text("raise RuntimeError('坏模块')\n", encoding="utf8")
    (d / "incomplete.py").write_text("SOURCE_ID = 'half'\n", encoding="utf8")
    monkeypatch.setattr(ss, "sources_dir", lambda: str(d))
    return d


def test_online_disabled_by_default(monkeypatch):
    import plugins.voice.sing_runtime as sr

    monkeypatch.setattr(sr, "voice_cfg", lambda: {})  # 无配置 = 默认关
    assert ss.online_enabled() is False


def test_online_enabled_by_config(monkeypatch):
    import plugins.voice.sing_runtime as sr

    monkeypatch.setattr(sr, "voice_cfg", lambda: {"sing_online": True})
    assert ss.online_enabled() is True


def test_load_sources_skips_broken_and_incomplete(fake_sources_dir):
    out = ss.load_sources()
    assert "fakesrc" in out
    assert "broken" not in out and "half" not in out
    assert callable(out["fakesrc"].search)


def test_acquire_gate_off_raises(fake_sources_dir, monkeypatch):
    import plugins.voice.sing_runtime as sr

    monkeypatch.setattr(sr, "voice_cfg", lambda: {})
    with pytest.raises(acq.AcquireError) as e:
        acq.acquire("青花瓷")
    assert "总闸" in str(e.value)


def test_acquire_no_sources(fake_sources_dir, monkeypatch):
    import plugins.voice.sing_runtime as sr

    monkeypatch.setattr(sr, "voice_cfg", lambda: {"sing_online": True})
    monkeypatch.setattr(ss, "load_sources", lambda: {})
    with pytest.raises(acq.AcquireError) as e:
        acq.acquire("青花瓷")
    assert "源" in str(e.value)


def test_acquire_single_flight(fake_sources_dir, monkeypatch):
    """同 query 进行中 → InFlightError（msg_sing 回"在唱了"的依据）。"""
    import plugins.voice.sing_runtime as sr

    monkeypatch.setattr(sr, "voice_cfg", lambda: {"sing_online": True})
    with acq._INFLIGHT_LOCK:
        acq._INFLIGHT["青花瓷"] = 0.0
    try:
        with pytest.raises(acq.InFlightError):
            acq.acquire("青花瓷")
    finally:
        with acq._INFLIGHT_LOCK:
            acq._INFLIGHT.pop("青花瓷", None)


def test_acquire_full_pipeline_registers_manifest(fake_sources_dir, monkeypatch, tmp_path):
    """全链（重活全 mock）：搜索命中 → fetch → 分离/切/key stub → manifest 注册 + 条目可用。"""
    import plugins.voice.sing_runtime as sr

    monkeypatch.setattr(sr, "voice_cfg", lambda: {"sing_online": True})
    monkeypatch.setattr(acq, "_MANIFEST_PATH", str(tmp_path / "manifest.json"))
    monkeypatch.setattr(acq, "_separate",
                        lambda audio, song_id: str(tmp_path / f"{song_id}_vocals.wav"))
    monkeypatch.setattr(acq, "_worker_action",
                        lambda p: ({"out": str(tmp_path / "c.wav"), "start_s": 40.0, "dur": 50.0}
                                   if p["action"] == "cut_chorus"
                                   else {"p5": 220.0, "p50": 300.0, "p95": 450.0,
                                         "key": 1, "out_frac": 0.01, "voiced": 4000}))
    entry = acq.acquire("青花瓷")
    assert entry["song_id"].startswith("fakesrc_")
    assert entry["key"] == 1 and entry["chorus"].endswith("_c50s.wav")
    songs = json.load(open(tmp_path / "manifest.json", encoding="utf8"))["songs"]
    assert entry["song_id"] in songs and songs[entry["song_id"]]["key"] == 1
    # 二次获取：manifest 命中不了 acquire（acquire 无视 manifest 直接重下——
    # 这是设计事实：去重靠 L1 查找在上层），但 single-flight 后 inflight 清空
    assert "青花瓷" not in acq._INFLIGHT


def test_display_title_prefers_song_name():
    """入库标题必须是"能念出口的歌名"：B站 SEO 长标题直接当 song 字段念给群友=刷屏。"""
    assert acq._display_title("【4K60FPS】陈奕迅《孤勇者》燃到深情! 谁说站在光里的才算英雄！",
                              "孤勇者") == "孤勇者"
    assert acq._display_title("【Hi-Res】孤勇者-陈奕迅", "孤勇者") == "孤勇者"
    assert acq._display_title("[Live] 后来 现场版", "后来") == "后来"
    assert acq._display_title("青花瓷 周杰伦", "青花瓷") == "青花瓷"
    # 什么都没有时退回群友自己说的歌名（不返回空串，话术不能用空歌名）
    assert acq._display_title("", "某首歌") == "某首歌"


def test_media_duration_never_fakes_zero(tmp_path):
    """时长量不到 = None（**不许用 0 冒充"0 秒"**——0 会被硬筛当成"片段"整首否掉）。"""
    assert acq._media_duration(str(tmp_path / "不存在.mp4")) in (None,)
    bogus = tmp_path / "audio.m4a"
    bogus.write_bytes(b"fake")
    # 坏文件：ffprobe 报错 → None（有 ffprobe）或解析不出 → None，总之不是 0
    assert acq._media_duration(str(bogus)) != 0
    assert acq._stem_duration(str(bogus)) is None


# === msg_sing 的 pending 路径 ===

_STUB_SONGS = {"qinghuaci": {"title": "青花瓷", "chorus": "C:/x/q.wav", "key": 0}}


def test_msg_sing_pending_when_gate_on(msg_mod, monkeypatch, tmp_path):
    """曲库未命中 + 总闸开 + 有源 → 立即返回 pending（线程被 stub，不真跑）。"""
    started = {}

    class _FakeThread:
        def __init__(self, target=None, args=(), name=None, daemon=None):
            started["target"] = target

        def start(self):
            started["started"] = True

    monkeypatch.setattr(msg_mod, "_sing_manifest", lambda: _STUB_SONGS)
    monkeypatch.setattr(msg_mod, "threading",
                        type("T", (), {"Thread": _FakeThread, "daemon": None}))
    monkeypatch.setattr(msg_mod, "_resolve",
                        lambda c, t: (type("A", (), {"reply_to": lambda s, cid: None})(), "group:1", None))
    monkeypatch.setattr(ss, "online_enabled", lambda: True)
    monkeypatch.setattr(ss, "load_sources", lambda: {"fakesrc": object()})
    r = msg_mod.msg_sing("孤勇者", channel="napcat", target="group:1")
    assert r["ok"] is True and r.get("pending") is True
    assert started.get("started") is True and callable(started.get("target"))


def test_msg_sing_l1_hit_still_synchronous(msg_mod, monkeypatch):
    """曲库命中 → 仍走同步转换路径（pending 不劫持已入库的歌）。"""
    calls = {}

    class _Reply:
        def send_sing(self, wav_path, dedup_text=""):
            calls["sent"] = (wav_path, dedup_text)
            return {"ok": True}

    class _Adapter:
        name = "napcat"

        def reply_to(self, chat_id):
            return _Reply()

    class _CapMod:
        @staticmethod
        def get_capability(name):
            return lambda input_wav, key=0, seed=1234, index_rate=0.66: {
                "out": "C:/x/out.wav", "sr": 48000, "seconds": 3.0, "dur": 50.0}

    monkeypatch.setattr(msg_mod, "_sing_manifest", lambda: _STUB_SONGS)
    monkeypatch.setattr(msg_mod, "_resolve",
                        lambda c, t: (_Adapter(), "group:1", None))
    import omni_core.tools.capability as cap_mod
    orig = cap_mod.get_capability
    cap_mod.get_capability = _CapMod.get_capability
    monkeypatch.setattr(ss, "online_enabled", lambda: True)
    monkeypatch.setattr(ss, "load_sources", lambda: {"fakesrc": object()})
    try:
        r = msg_mod.msg_sing("青花瓷", channel="napcat", target="group:1")
    finally:
        cap_mod.get_capability = orig
    assert r["ok"] is True and r.get("pending") is None
    assert calls["sent"] == ("C:/x/out.wav", "song:qinghuaci")


def test_msg_sing_second_request_while_acquiring_says_zai_chang_le(msg_mod, monkeypatch):
    """**同一首正在后台练时再点 → 直接回"在唱了"，不起第二个线程**。

    原实现派线程前不查 in-flight：二次请求会再起一个线程，acquire 抛 InFlightError
    被兜底 except 抓住，对群友说"这首练劈了……换一首我试试？"——而第一次任务稍后
    照样会把歌唱出来。话术与事实相反（plan §5 并发护栏）。
    """
    started = []

    class _FakeThread:
        def __init__(self, target=None, args=(), name=None, daemon=None):
            started.append(target)

        def start(self):
            started.append("start")

    monkeypatch.setattr(msg_mod, "_sing_manifest", lambda: _STUB_SONGS)
    monkeypatch.setattr(msg_mod, "threading",
                        type("T", (), {"Thread": _FakeThread, "daemon": None}))
    monkeypatch.setattr(msg_mod, "_resolve",
                        lambda c, t: (type("A", (), {"reply_to": lambda s, cid: None})(),
                                      "group:1", None))
    monkeypatch.setattr(ss, "online_enabled", lambda: True)
    monkeypatch.setattr(ss, "load_sources", lambda: {"fakesrc": object()})
    key = msg_mod._sing_busy_key("孤勇者", "acq")
    msg_mod._SING_BUSY[key] = _time.time()
    try:
        r = msg_mod.msg_sing("孤勇者", channel="napcat", target="group:1")
    finally:
        msg_mod._SING_BUSY.pop(key, None)
    assert r["ok"] is True and r.get("inflight") is True
    assert r.get("pending") is None
    assert started == [], "已在练的歌不该再起线程"


def test_async_sing_inflight_error_stays_silent(msg_mod, monkeypatch):
    """竞态抢到 InFlightError：只记日志，**不发"练劈了"**（歌会被第一次那程发出来）。"""
    spoken = []

    class _Reply:
        def send_text(self, text):
            spoken.append(text)

    class _Adapter:
        def reply_to(self, chat_id):
            return _Reply()

    def _boom(query):
        raise acq.InFlightError(query)

    monkeypatch.setattr(acq, "acquire", _boom)
    key = msg_mod._sing_busy_key("孤勇者", "acq")
    msg_mod._SING_BUSY[key] = _time.time()
    msg_mod._async_sing(key, "孤勇者", _Adapter(), "group:1")
    assert spoken == []
    assert key not in msg_mod._SING_BUSY, "线程退出必须释放占位"


def test_async_sing_failure_still_speaks_and_releases(msg_mod, monkeypatch):
    """在线全链真失败 → 失败也要说话（不许静默吞），并且释放占位。"""
    spoken = []

    class _Reply:
        def send_text(self, text):
            spoken.append(text)

    class _Adapter:
        def reply_to(self, chat_id):
            return _Reply()

    def _boom(query):
        raise acq.AcquireError("下载", "源挂了")

    monkeypatch.setattr(acq, "acquire", _boom)
    key = msg_mod._sing_busy_key("孤勇者", "acq")
    msg_mod._SING_BUSY[key] = _time.time()
    msg_mod._async_sing(key, "孤勇者", _Adapter(), "group:1")
    assert spoken and "练劈了" in spoken[0]
    assert key not in msg_mod._SING_BUSY


def test_double_import_shares_one_sing_worker():
    """护栏：RVC worker 单例在"同一文件被 exec 两遍"下仍必须只有一份。

    loader 按**文件路径**把插件装载成 `_omni_plugin_voice`，而 sing_acquire /
    song_source 按**包名** import `plugins.voice.plugin`——同一份 plugin.py 两次
    装载、两个模块对象。若 `_SING_WORKER` 定义在 plugin.py，就会有两条 RVC 子进程
    各自持锁抢 GPU（2026-11-03 实测踩过：pid 44632 与 9104 共存 5 分钟）。
    修法是把跨模块共享状态搬进只按包名 import 的 sing_runtime；这条用例守住它。
    """
    import importlib.util

    import plugins.voice.plugin as vp_pkg
    import plugins.voice.sing_runtime as sr

    path = os.path.join(os.path.dirname(os.path.abspath(vp_pkg.__file__)), "plugin.py")
    spec = importlib.util.spec_from_file_location("_voice_plugin_second_load", path)
    second = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(second)          # 模拟 loader 的按路径装载

    assert second._SING_WORKER is sr._SING_WORKER is vp_pkg._SING_WORKER
    assert second.sing is sr.sing            # 能力函数同源，注册的是同一个实现
