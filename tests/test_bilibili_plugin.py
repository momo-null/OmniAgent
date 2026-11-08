"""B站只读采集插件离线测试（M1 外环/M3 回流的工具面；fake adapter，零网络）。

红线守门：插件必须零写操作——本文件同时断言工具清单里没有任何写语义函数。
"""
import importlib.util
import pathlib

import pytest

# 插件目录不是包（loader 按 spec_from_file_location 文件导入），测试用同款方式装载。
# ⚠ exec_module 会把 bili_* 工具注册进全局 TOOL_REGISTRY——必须立即注销，
# 否则 test_plugin_loader 装真插件时撞「覆盖已注册工具」（全量顺序依赖的脏状态）。
_spec = importlib.util.spec_from_file_location(
    "bilibili_plugin_under_test",
    pathlib.Path(__file__).resolve().parent.parent / "plugins" / "bilibili" / "plugin.py")
bp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bp)
from omni_core.tools.base import TOOL_REGISTRY as _REG  # noqa: E402

for _n in ("bili_hot", "bili_search", "bili_video_info",
           "bili_video_comments", "bili_video_subtitle", "bili_self_dynamics"):
    _REG.pop(_n, None)
bili_hot = bp.bili_hot
bili_search = bp.bili_search
bili_video_info = bp.bili_video_info
bili_video_comments = bp.bili_video_comments
bili_video_subtitle = bp.bili_video_subtitle
bili_self_dynamics = bp.bili_self_dynamics


class _FakeAdapter:
    def __init__(self):
        self.self_uid = "9000"
        self.calls = []

    def wbi_get(self, url, params):
        self.calls.append(("wbi", url, params))
        if "popular" in url:
            return {"body": {"code": 0, "data": {"list": [
                {"bvid": "BV1xx", "aid": 1, "title": "热门视频", "desc": "简介",
                 "owner": {"name": "UP主", "mid": 9001},
                 "stat": {"view": 100, "like": 10, "reply": 2, "danmaku": 3}}]}}}
        if "player/wbi/v2" in url:
            return {"body": {"code": 0, "data": {"subtitle": {"subtitles": [
                {"lan": "ai-zh", "subtitle_url": "//aisubtitle.hdslb.com/syn.json"}]}}}}
        if "search" in url:
            return {"body": {"code": 0, "data": {"result": [
                {"type": "video", "bvid": "BV2yy", "aid": 2,
                 "title": '<em class="keyword">搜索</em>结果',
                 "description": "描述", "author": "作者", "mid": 9002,
                 "play": 50, "like": 5, "review": 1}]}}}
        if "reply/wbi/main" in url:
            return {"body": {"code": 0, "data": {"replies": [
                {"rpid": 101, "member": {"uname": "路人"}, "mid": 9003,
                 "content": {"message": "评论区一句话"}, "like": 7, "rcount": 0}],
                "cursor": {"all_count": 1}}}}
        return {"body": {"code": -400}}

    def _http(self, method, url, params=None, **kw):
        self.calls.append(("http", url, params))
        if "view" in url:
            return {"body": {"code": 0, "data": {
                "bvid": "BV3zz", "aid": 3, "title": "视频标题", "desc": "视频简介",
                "owner": {"name": "UP", "mid": 9004}, "stat": {"view": 9},
                "pages": [{"cid": 77}]}}}
        if "aisubtitle" in url:
            return {"body": {"body": [
                {"from": 0.0, "to": 1.5, "content": "第一句"},
                {"from": 65.0, "to": 67.0, "content": "第二句"}]}}
        if "dynamic_new" in url:
            import json as _j
            card = _j.dumps({"item": {"description": "我的测试动态", "reply": 1}})
            return {"body": {"code": 0, "data": {"cards": [
                {"desc": {"dynamic_id": 555, "type": 2, "view": 10, "like": 2,
                          "comment": 1, "repost": 0, "timestamp": 1700000000},
                 "card": card}]}}}
        return {"body": {"code": -400}}


@pytest.fixture
def fake(monkeypatch):
    a = _FakeAdapter()
    monkeypatch.setattr(bp, "_ADAPTER", a)
    return a


# === 只读工具 ===

def test_bili_hot(fake):
    ret = bili_hot(5)
    assert ret["ok"] is True and ret["count"] == 1
    item = ret["data"][0]
    assert item["bvid"] == "BV1xx" and item["owner"] == "UP主" and item["play"] == 100


def test_bili_search_strips_em_tags(fake):
    ret = bili_search("搜索", 5)
    assert ret["ok"] is True
    assert ret["data"][0]["title"] == "搜索结果"  # 高亮标签被剥掉


def test_bili_video_info(fake):
    ret = bili_video_info(bvid="BV3zz")
    assert ret["ok"] is True and ret["data"]["title"] == "视频标题"
    assert fake.calls[-1][2] == {"bvid": "BV3zz"}


def test_bili_video_info_requires_target():
    ret = bili_video_info()
    assert ret["ok"] is False and "至少给一个" in ret["error"]


def test_bili_video_subtitle(fake):
    ret = bili_video_subtitle(bvid="BV3zz")
    assert ret["ok"] is True and ret["data"]["has_subtitle"] is not False
    transcript = ret["data"]["transcript"]
    assert "[00:00] 第一句" in transcript
    assert "[01:05] 第二句" in transcript   # 时间戳格式
    # 协议相对 URL 被补成 https（实盘坑：B站返回 //aisubtitle.hdslb.com/...）
    assert any(c[1].startswith("https://aisubtitle.hdslb.com/") for c in fake.calls)


def test_bili_video_subtitle_no_subtitle(fake, monkeypatch):
    def no_sub(*args, **kw):  # 兼容 wbi_get(url, params) 与 _http(method, url, params=) 两种调用形
        joined = " ".join(str(a) for a in args)
        if "player/wbi/v2" in joined:
            return {"body": {"code": 0, "data": {"subtitle": {"subtitles": []}}}}
        if "view" in joined:
            return {"body": {"code": 0, "data": {"title": "无字幕视频", "pages": [{"cid": 1}]}}}
        return {"body": {"code": 0, "data": {}}}

    monkeypatch.setattr(fake, "wbi_get", no_sub)
    monkeypatch.setattr(fake, "_http", no_sub)
    ret = bili_video_subtitle(aid=9)
    assert ret["ok"] is True and ret["data"]["has_subtitle"] is False
    assert "别假装看过" in ret["note"]


def test_bili_video_comments(fake):
    ret = bili_video_comments(oid=123, type=17, n=5)
    assert ret["ok"] is True and ret["count"] == 1
    assert ret["data"][0]["message"] == "评论区一句话"


def test_bili_self_dynamics(fake):
    ret = bili_self_dynamics(5)
    assert ret["ok"] is True and ret["data"][0]["dynamic_id"] == 555
    assert ret["data"][0]["text"] == "我的测试动态" and ret["data"][0]["reply"] == 1


def test_tools_without_channel(monkeypatch):
    monkeypatch.setattr(bp, "_ADAPTER", None)
    monkeypatch.setattr("gateway.channel_gateway.channel_config",
                        lambda name: (_ for _ in ()).throw(RuntimeError("无配置")))
    ret = bili_hot()
    assert ret["ok"] is False and "通道不可用" in ret["error"]


# === 红线守门：零写操作 ===

def test_plugin_registers_no_write_tools():
    import inspect

    from omni_core.tools import base

    names = [n for n, f in inspect.getmembers(bp, inspect.isfunction)
             if getattr(f, "__name__", "").startswith("bili_")]
    assert set(names) <= {"bili_hot", "bili_search", "bili_video_info",
                          "bili_video_comments", "bili_video_subtitle",
                          "bili_self_dynamics"}
    for n in names:
        assert "send" not in n and "post" not in n and "like" not in n and "follow" not in n
    # 所有注册的工具 risk 都未声明（S1 纯/读漏斗）
    assert not any(getattr(f, "risk", None) for n, f in inspect.getmembers(bp, inspect.isfunction)
                   if n.startswith("bili_"))
