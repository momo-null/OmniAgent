"""M2：视觉通道 + SoM 单测（纯 CPU / mock，无需 GPU）。

覆盖：
- parse_ui_nodes：u2 XML -> 元素列表（含 bounds 解析、零面积过滤）
- build_som：画编号框 -> 标注图 + marks（id/ref/bounds/center 正确）
- LocalVision.describe：OpenAI 兼容多模态请求构建 + 响应解析（mock client）
- VisionRuntime：vision_describe / som_ground / tap_mark（fake exec + mock client）
- vision_tool：SoM marks 由插件自持（α 决策 `som://last_result`），运行时不持有
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image, ImageDraw

from omni_core.tools.vision_runtime import (
    LocalVision,
    VisionRuntime,
    build_som,
    parse_ui_nodes,
)
from omni_core.tools import vision_tool


_SAMPLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<hierarchy>
  <node index="0" text="登录" resource-id="com.ex:id/login" class="android.widget.Button" bounds="[100,200][300,260]"/>
  <node index="1" text="" resource-id="com.ex:id/icon" class="android.widget.ImageView" bounds="[10,10][20,20]"/>
  <node index="2" text="取消" resource-id="com.ex:id/cancel" class="android.widget.Button" bounds="[0,0][0,0]"/>
  <node index="3" text="搜索" resource-id="" class="android.widget.TextView" bounds="[400,500][560,540]"/>
</hierarchy>
"""


def _make_image(path, size=(600, 600)):
    img = Image.new("RGB", size, (30, 30, 30))
    d = ImageDraw.Draw(img)
    d.rectangle([100, 200, 300, 260], outline=(0, 255, 0), width=3)
    d.rectangle([400, 500, 560, 540], outline=(0, 255, 0), width=3)
    img.save(path)
    return path


def test_parse_ui_nodes():
    els = parse_ui_nodes(_SAMPLE_XML)
    # 3 个有效（第 3 个零面积被过滤）
    assert len(els) == 3
    assert els[0]["bounds"] == [100, 200, 300, 260]
    assert els[0]["resource_id"] == "com.ex:id/login"
    assert els[0]["text"] == "登录"
    # 零面积节点未纳入
    assert all(e["bounds"][2] > e["bounds"][0] and e["bounds"][3] > e["bounds"][1] for e in els)


def test_build_som_marks():
    tmp = tempfile.mkdtemp()
    img_path = os.path.join(tmp, "screen.png")
    _make_image(img_path)
    out_path = os.path.join(tmp, "som.png")
    marked, marks = build_som(img_path, parse_ui_nodes(_SAMPLE_XML), out_path=out_path)
    assert os.path.isfile(marked)
    assert len(marks) == 3
    # 编号连续从 1 开始
    assert [m["id"] for m in marks] == [1, 2, 3]
    # ref / center 正确
    assert marks[0]["ref"] == "com.ex:id/login"
    assert marks[0]["center"] == [200, 230]
    # 标注图确实画了红框（左上角像素附近有标记）
    som_img = Image.open(marked).convert("RGB")
    assert som_img.size == (600, 600)


class _FakeClient:
    def __init__(self, content="视觉回答"):
        self._content = content
        self.last_payload = None
        self.last_url = None

    def post(self, url, json=None):
        self.last_url = url
        self.last_payload = json
        content = self._content

        class _Resp:
            status_code = 200
            text = ""

            def json(self):
                return {"choices": [{"message": {"content": content}}]}

        return _Resp()


def test_local_vision_describe():
    tmp = tempfile.mkdtemp()
    img_path = os.path.join(tmp, "s.png")
    _make_image(img_path)
    cli = _FakeClient(content="这是一个登录按钮")
    v = LocalVision("http://127.0.0.1:8085", "qwen3.5-4b-vl", client=cli)
    out = v.describe(img_path, "这是什么？")
    assert out == "这是一个登录按钮"
    # 多模态消息：文本 + image_url
    content = cli.last_payload["messages"][0]["content"]
    assert any(c["type"] == "text" for c in content)
    assert any(c["type"] == "image_url" for c in content)
    assert cli.last_payload["model"] == "qwen3.5-4b-vl"


class _FakeExec:
    def __init__(self, img_path, xml):
        self._img = img_path
        self._xml = xml
        self.tapped = None

    def screenshot(self, save_path=None):
        return {"ok": True, "path": self._img}

    def get_ui_tree(self):
        return {"ok": True, "ui_tree": self._xml}

    def tap_by_id(self, resource_id):
        self.tapped = resource_id
        return {"ok": True, "resource_id": resource_id}


def test_vision_runtime_flow():
    tmp = tempfile.mkdtemp()
    img_path = os.path.join(tmp, "screen.png")
    _make_image(img_path)
    fake_exec = _FakeExec(img_path, _SAMPLE_XML)
    cli = _FakeClient(content="请点登录")
    vr = VisionRuntime(
        fake_exec,
        {"enabled": True, "model": "qwen3.5-4b-vl", "base_url": "http://fake"},
        client=cli,
    )
    vr.enabled = True

    # vision_describe
    r1 = vr.vision_describe("界面上有什么？")
    assert r1["ok"] is True
    assert r1["description"] == "请点登录"

    # som_ground -> 拿到 marks（状态由 vision_tool 插件自持）
    r2 = vr.som_ground()
    assert r2["ok"] is True
    assert r2["element_count"] == 3
    assert os.path.isfile(r2["marked_image"])

    vision_tool.bind_vision_runtime(vr)
    assert vr.tap_mark(r2["marks"][0])["ok"] is True
    assert fake_exec.tapped == "com.ex:id/login"

    # 插件侧：marks 自持 + 按 id 点击
    assert vision_tool.som_ground()["ok"] is True
    assert vision_tool.som_last_result()["count"] == 3
    assert vision_tool.tap_by_mark(1)["ok"] is True
    # 未知 mark
    assert vision_tool.tap_by_mark(999)["ok"] is False


def test_vision_runtime_holds_no_marks_state():
    """α 决策：跨步 marks 由 tool 插件自持，运行时不持有。"""
    vr = VisionRuntime(None, {"enabled": True})
    assert not hasattr(vr, "_last_marks")
    assert hasattr(vision_tool, "_last_marks")


def test_vision_dispatch_disabled():
    # 未启用时不应构造 VisionRuntime（交由 tool_loop 控制）；这里验证 VisionRuntime
    # 在 runtime["vision"] 为 None 时，dispatch 侧返回明确错误。
    vr = VisionRuntime(None, {"enabled": False})
    vr.enabled = False
    assert vr.vision_describe("x")["ok"] is False
    assert vr.som_ground()["ok"] is False
