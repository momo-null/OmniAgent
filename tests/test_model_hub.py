"""model_hub 纯逻辑单元测试（重组后从 llm_runtime 迁入）

registry / schemas / meta 不依赖真实模型或 GPU，可在 CI 环境直接跑。

model-meta-refactor (2026-07-11) 后，模型发现改为「扫描目录 + .meta.json 侧注」，
不再依赖 config 注册表。本文件同步移除 registry 相关测试，
新增 scan_and_build_models / save_model_meta / get_default_model 等扫描式测试。
"""
import json
from unittest.mock import patch, MagicMock

import pytest

from model_hub.manager import ModelManager
from model_hub.meta import ModelMetaData
from model_hub.schemas import (
    ModelInfo,
    ModelStatus,
    ActiveModel,
    GpuInfo,
    TrainingStatus,
    AgentStatus,
    ModelMetaSaveRequest,
)


def _make_mgr():
    """构造一个不读磁盘 config 的 ModelManager 实例（仅测逻辑）"""
    mgr = ModelManager.__new__(ModelManager)
    mgr.config_path = "x"          # 仅用于推导日志目录（dirname），不读真实文件
    mgr.config = {
        # 注意：已无 llm_runtime.models 注册表；仅保留 preset 供合并测试
        "model_presets": {"openclaw": {"ctx_size": 24576, "gpu_layers": 12}},
    }
    mgr.llama_root = "ext/llama"
    mgr.models_dir = "D:/AI/Models"
    mgr.presets = mgr.config["model_presets"]
    mgr.processes = {}
    return mgr


def test_resolve_launch_params_merges():
    m = _make_mgr()
    p = m._resolve_launch_params(
        "m1", overrides={"gpu_layers": 50}, profile="openclaw",
    )
    assert p["ctx_size"] == 24576      # 来自 preset
    assert p["gpu_layers"] == 50        # override 覆盖 preset
    assert p["threads"] == 8           # 基础值保留


def test_alloc_port_skips_used():
    m = _make_mgr()
    m.processes = {"running": {"port": 8085}}  # 8085 被占用
    with patch.object(ModelManager, "_port_free", side_effect=lambda p: True):
        port = m._alloc_port()
    # 8085 跳过（在 used 中），取下一个空闲端口 8086
    assert port == 8086


def test_start_model_path_autolloc_and_popen():
    m = _make_mgr()
    fake_proc = MagicMock()
    fake_proc.pid = 1234
    with patch("model_hub.manager.subprocess.Popen", return_value=fake_proc) as popen, \
         patch.object(m, "_wait_health_check", return_value=True), \
         patch.object(m, "_get_server_exe", return_value="llama-server.exe"), \
         patch.object(m, "_build_env", return_value={}), \
         patch.object(m, "_port_free", return_value=True), \
         patch("os.path.exists", return_value=True):
        res = m.start_model_path("D:/x/Qwen.gguf", name="x")
    assert res["status"] == "ready"
    assert res["port"] == 8085          # 8085 空闲 → 自动分配
    assert "x" in m.processes
    popen.assert_called_once()
    args = popen.call_args[0][0]
    assert "-m" in args and "D:/x/Qwen.gguf" in args


def test_validate_model_not_running_returns_error_no_start():
    """B-约束：模型未运行时，校验不得自行启动 / 停止模型，直接返回明确错误。"""
    m = _make_mgr()
    fake_proc = MagicMock()
    with patch("model_hub.manager.subprocess.Popen", return_value=fake_proc) as popen, \
         patch.object(m, "stop_model", return_value={"status": "stopped"}) as stop:
        res = m.validate_model("m1", gguf_path="D:/a.gguf", prompt="介绍自己")
    assert res["ok"] is False
    assert "未运行" in (res["error"] or "")
    popen.assert_not_called()      # 校验不得自行启动模型
    stop.assert_not_called()       # 也没去停模型


def test_validate_model_running_chats():
    """模型已运行时，校验对 running 实例发测试请求并返回回复。"""
    m = _make_mgr()
    m.processes["m1"] = {"port": 8085}
    with patch.object(m, "_chat_completion", return_value="你好，我是模型。") as chat:
        res = m.validate_model("m1", prompt="介绍自己")
    assert res["ok"] is True
    assert res["reply"] == "你好，我是模型。"
    chat.assert_called_once()


def test_chat_completion_parses_reply():
    m = _make_mgr()
    fake_resp = MagicMock()
    fake_resp.read.return_value = b'{"choices":[{"message":{"content":"hello"}}]}'
    cm = MagicMock()
    cm.__enter__.return_value = fake_resp
    cm.__exit__.return_value = False
    with patch("model_hub.manager.urllib.request.urlopen", return_value=cm):
        out = m._chat_completion(8085, "hi")
    assert out == "hello"


def test_tail_logs_returns_new_lines():
    m = _make_mgr()
    m.processes = {"m1": {"out_log": "o", "err_log": "e"}}
    with patch.object(m, "get_model_logs",
                      return_value={"out": "l1\nl2\nl3", "err": ""}):
        res = m.tail_logs("m1", prev_seen=1)
    assert res["lines"] == ["l2", "l3"]
    assert res["seen"] == 3


def test_scan_and_build_models(tmp_path):
    """扫描目录 → 合并 .meta.json，得到统一模型信息"""
    mgr = ModelManager.__new__(ModelManager)
    mgr.processes = {}
    mgr.models_dir = str(tmp_path)

    gguf = tmp_path / "qwen.gguf"
    gguf.write_text("")
    (tmp_path / "qwen.meta.json").write_text(json.dumps({
        "name": "qwen", "ctx_size": 4096, "gpu_layers": 20, "tags": ["default"],
    }))

    models = mgr.scan_and_build_models()
    assert len(models) == 1
    m = models[0]
    assert m["name"] == "qwen"
    assert m["ctx_size"] == 4096
    assert m["gpu_layers"] == 20
    assert m["tags"] == ["default"]
    assert m["has_meta"] is True
    assert m["has_mmproj"] is False
    assert m["status"] == "stopped"


def test_get_default_model(tmp_path):
    """默认模型 = tags 含 default 的第一个；无模型返回 None"""
    mgr = ModelManager.__new__(ModelManager)
    mgr.processes = {}
    mgr.models_dir = str(tmp_path)

    # 无模型目录内容 → None
    assert mgr.get_default_model() is None

    (tmp_path / "a.gguf").write_text("")
    (tmp_path / "b.gguf").write_text("")
    (tmp_path / "b.meta.json").write_text(
        json.dumps({"name": "b", "tags": ["default"]})
    )
    assert mgr.get_default_model() == "b"


def test_save_model_meta(tmp_path):
    """保存侧注：仅更新显式字段；显式 null 删除该字段；未传字段保持"""
    mgr = ModelManager.__new__(ModelManager)
    mgr.processes = {}

    gguf = tmp_path / "M.gguf"
    gguf.write_text("")
    (tmp_path / "M.meta.json").write_text(
        json.dumps({"description": "old", "ctx_size": 8192})
    )

    # 1) 仅更新 gpu_layers，description 不传 → 应保持
    req = ModelMetaSaveRequest(gguf_path=str(gguf), gpu_layers=50)
    res = mgr.save_model_meta("M", req)
    assert res["status"] == "saved"
    data = json.loads((tmp_path / "M.meta.json").read_text(encoding="utf-8"))
    assert data["gpu_layers"] == 50
    assert data["description"] == "old"          # 未传，保持

    # 2) 显式传 null → 从侧注删除 ctx_size（回退默认）
    req2 = ModelMetaSaveRequest(gguf_path=str(gguf), ctx_size=None)
    mgr.save_model_meta("M", req2)
    data2 = json.loads((tmp_path / "M.meta.json").read_text(encoding="utf-8"))
    assert "ctx_size" not in data2                 # 已被删除
    assert data2["gpu_layers"] == 50
    assert data2["description"] == "old"


def test_schemas_instantiate():
    assert ModelInfo(name="x").name == "x"
    assert ModelStatus(name="x").status == "stopped"
    assert ActiveModel(name="x", port=1).port == 1
    assert GpuInfo().name == "unknown"
    assert TrainingStatus().running is False
    assert AgentStatus().running is False


# ── 透传参数（extra_args）与投影兜底 ─────────────────────────────────────────
# 设计：模型启动参数很精细，不可能由前端穷举 ⇒ 除内核掌管的少数开关
# （模型路径 / host / 端口 / mmproj）外全部可透传；且参数**只写模型目录侧注**
# （.meta.json，随模型可移植），不进项目 config。

def test_extra_args_roundtrip_in_meta(tmp_path):
    """extra_args 随侧注持久化；字符串写法（手写侧注）被规范化为 token 列表。"""
    mgr = ModelManager.__new__(ModelManager)
    mgr.processes = {}
    gguf = tmp_path / "M.gguf"
    gguf.write_text("")

    req = ModelMetaSaveRequest(gguf_path=str(gguf),
                               extra_args=["-fa", "on", "-ub", "512"])
    res = mgr.save_model_meta("M", req)
    assert res["status"] == "saved"
    data = json.loads((tmp_path / "M.meta.json").read_text(encoding="utf-8"))
    assert data["extra_args"] == ["-fa", "on", "-ub", "512"]
    assert ModelMetaData.load_from(str(gguf)).extra_args == ["-fa", "on", "-ub", "512"]

    # 宽容形式：手写 "‑fa on" 字符串
    (tmp_path / "M.meta.json").write_text(
        json.dumps({"extra_args": "-fa on --no-mmproj-offload"}), encoding="utf-8"
    )
    assert ModelMetaData.load_from(str(gguf)).extra_args == \
        ["-fa", "on", "--no-mmproj-offload"]


def test_resolve_launch_params_auto_detects_mmproj(tmp_path):
    """A1 回归：侧注未写 mmproj_path 时，启动参数解析应自动探测同目录投影。

    此前只有「列表展示」路径做了探测，两条「启动」路径都漏 ⇒ 前端显示
    has_mmproj=true、启动却不带 --mmproj，模型静默退化成纯文本。
    """
    m = _make_mgr()
    gguf = tmp_path / "Mini.gguf"
    gguf.write_text("")
    (tmp_path / "mmproj-model-f16.gguf").write_text("")       # 同目录投影
    (tmp_path / "Mini.meta.json").write_text(
        json.dumps({"ctx_size": 8192, "extra_args": ["-fa", "on"]}), encoding="utf-8"
    )

    p = m._resolve_launch_params(str(gguf))
    assert p["mmproj_path"] and p["mmproj_path"].endswith("mmproj-model-f16.gguf")
    assert p["extra_args"] == ["-fa", "on"]

    with patch.object(m, "_get_server_exe", return_value="llama-server.exe"):
        args = m._build_server_args_from_params(p)
    assert "--mmproj" in args
    assert args[-2:] == ["-fa", "on"]      # 透传参数追加在末尾（后置可覆盖具名字段）


def test_extra_args_rejects_reserved_switch(tmp_path):
    """内核掌管的开关不得透传：--port 会让健康检查/端口账记录错端口。"""
    m = _make_mgr()
    gguf = tmp_path / "M.gguf"
    gguf.write_text("")
    with patch.object(m, "_get_server_exe", return_value="llama-server.exe"):
        for bad in (["--port", "9999"], ["--port=9999"], ["-m", "x.gguf"], ["--mmproj", "p.gguf"]):
            with pytest.raises(ValueError, match="内核掌管"):
                m._build_server_args_from_params(
                    {"gguf_path": str(gguf), "extra_args": bad}
                )


def test_start_path_fails_fast_when_process_exits(tmp_path):
    """进程秒退（如透传参数写错）→ 立即判 failed 并带日志，不再白等 180s。"""
    m = _make_mgr()
    gguf = tmp_path / "M.gguf"
    gguf.write_text("")
    fake_proc = MagicMock()
    fake_proc.pid = 4321
    fake_proc.poll.return_value = 1                    # 进程已退出
    err = "error: invalid argument: --non-existent-flag"
    with patch("model_hub.manager.subprocess.Popen", return_value=fake_proc), \
         patch.object(m, "_get_server_exe", return_value="llama-server.exe"), \
         patch.object(m, "_build_env", return_value={}), \
         patch.object(m, "_port_free", return_value=True), \
         patch.object(m, "_health_check", return_value=False), \
         patch.object(m, "get_model_logs", return_value={"out": "", "err": err}), \
         patch("os.path.exists", return_value=True):
        res = m.start_model_path(str(gguf), name="M")
    assert res["status"] == "failed"
    assert "invalid argument" in res["logs"]["err"]
