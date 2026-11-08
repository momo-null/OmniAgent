"""小红书只读采集插件离线测试（fake MCP transport，零网络）。

红线守门：插件必须零写操作；MCP 返回结构漂移时宽容归一。
"""
import importlib.util
import pathlib

import pytest

# 插件目录不是包，按 loader 同款方式装载；exec 后立即注销全局注册（防污染 loader 测试）
_spec = importlib.util.spec_from_file_location(
    "xiaohongshu_plugin_under_test",
    pathlib.Path(__file__).resolve().parent.parent / "plugins" / "xiaohongshu" / "plugin.py")
xp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(xp)
from omni_core.tools.base import TOOL_REGISTRY as _REG  # noqa: E402

for _n in ("xhs_list_feeds", "xhs_search", "xhs_feed_detail"):
    _REG.pop(_n, None)
xhs_list_feeds = xp.xhs_list_feeds
xhs_search = xp.xhs_search
xhs_feed_detail = xp.xhs_feed_detail


class _FakeAdapter:
    def __init__(self):
        self.calls = []

    def _mcp_call(self, tool, args=None):
        self.calls.append((tool, args))
        if tool == "list_feeds":
            return {"ok": True, "data": {"items": [
                {"id": "feed1", "xsecToken": "t1",
                 "noteCard": {"displayTitle": "推荐笔记", "desc": "描述",
                              "user": {"nickname": "路人", "userId": "u1"},
                              "interactInfo": {"likedCount": "1.2万", "collectedCount": "3000",
                                               "commentCount": "456"}}}]}}
        if tool == "search_feeds":
            return {"ok": True, "data": {"items": [
                {"id": "feed2", "xsecToken": "t2",
                 "noteCard": {"displayTitle": "搜索结果", "type": "视频",
                              "user": {"nickname": "作者"},
                              "interactInfo": {"likedCount": "800"}}}]}}
        if tool == "get_feed_detail":
            return {"ok": True, "data": {
                "id": "feed1", "noteCard": {"displayTitle": "推荐笔记"},
                "comments": [
                    {"comment_id": "c1", "nickname": "评论者",
                     "content": "说得好", "likeCount": "12", "ipLocation": "上海"}]}}
        return {"ok": False, "error": "unknown tool"}


@pytest.fixture
def fake(monkeypatch):
    a = _FakeAdapter()
    monkeypatch.setattr(xp, "_ADAPTER", a)
    return a


def test_xhs_list_feeds(fake):
    ret = xhs_list_feeds(5)
    assert ret["ok"] is True and ret["count"] == 1
    note = ret["data"][0]
    assert note["feed_id"] == "feed1" and note["xsec_token"] == "t1"
    assert note["title"] == "推荐笔记" and note["liked"] == "1.2万"


def test_xhs_search(fake):
    ret = xhs_search("逛吃", 5)
    assert ret["ok"] is True
    assert ret["data"][0]["feed_id"] == "feed2"
    assert fake.calls[0] == ("search_feeds", {"keyword": "逛吃"})


def test_xhs_search_requires_keyword():
    ret = xhs_search("  ")
    assert ret["ok"] is False and "关键词为空" in ret["error"]


def test_xhs_feed_detail(fake):
    ret = xhs_feed_detail("feed1", "t1")
    assert ret["ok"] is True
    assert ret["data"]["comment_count"] == 1
    assert ret["data"]["comments"][0]["content"] == "说得好"
    assert fake.calls[0] == ("get_feed_detail", {"feed_id": "feed1", "xsec_token": "t1"})


def test_mcp_unreachable_gives_clear_error(monkeypatch):
    class _Broken:
        def _mcp_call(self, tool, args=None):
            return {"ok": False, "error": "MCP 端点不可达（http://127.0.0.1:18060/mcp）"}

    monkeypatch.setattr(xp, "_ADAPTER", _Broken())
    ret = xhs_list_feeds()
    assert ret["ok"] is False and "MCP 端点不可达" in ret["error"]


def test_plugin_registers_no_write_tools():
    import inspect

    names = [n for n, f in inspect.getmembers(xp, inspect.isfunction)
             if getattr(f, "__name__", "").startswith("xhs_")]
    assert set(names) <= {"xhs_list_feeds", "xhs_search", "xhs_feed_detail"}
    for n in names:
        assert not any(k in n for k in ("publish", "post", "comment_to", "like", "follow"))
