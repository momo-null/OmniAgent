"""测试 omni_core.brain.resolve：新 schema 解析 + 旧 schema 回退。

不依赖真实 config.yaml / 网络：直接构造 cfg dict 喂 resolve_agent_model。
"""
import pytest

from omni_core.brain.resolve import resolve_agent_model, resolve_all_agent_models
from omni_core.brain import sdk_model


# --- 新 schema 夹具 ----------------------------------------------------------
def _new_schema_cfg():
    return {
        "llm": {
            "providers": {
                "online": {
                    "provider": "openai-compatible",
                    "base_url": "https://example/v1",
                    "model": "demo-model",
                    "api_key": "sk-online",
                    "api_key_env": "OMNI_BRAIN_API_KEY",
                    "request": {"temperature": 0.3},
                },
                "local4b": {
                    "provider": "openai-compatible",
                    "base_url": "http://127.0.0.1:8085",
                    "model": "qwen3.5-4b-vl",
                    "capabilities": {"vision": True},
                },
            }
        },
        "runtime": {
            "agents": {
                "main": {"enabled": True, "model": "online", "max_steps": 40},
                "worker": {"model": "local4b", "dispatchable": []},
            }
        },
    }


def test_resolve_new_schema_main():
    cfg = _new_schema_cfg()
    out = resolve_agent_model(cfg, "main")
    assert out["model"] == "demo-model"
    assert out["base_url"] == "https://example/v1"
    assert out["api_key"] == "sk-online"
    # agent 自身字段保留，provider 引用（model_ref）已被解析剔除
    assert out.get("max_steps") == 40
    assert "model_ref" not in out
    # 新 schema 默认启用
    assert out.get("enabled") is True


def test_resolve_new_schema_worker_default_enabled():
    cfg = _new_schema_cfg()
    out = resolve_agent_model(cfg, "worker")
    assert out["model"] == "qwen3.5-4b-vl"
    assert out["base_url"] == "http://127.0.0.1:8085"
    assert out.get("capabilities", {}).get("vision") is True
    # worker 未显式 enabled -> 默认 True（有 provider 引用即生效）
    assert out.get("enabled") is True


def test_resolve_new_schema_explicit_disabled():
    cfg = _new_schema_cfg()
    cfg["runtime"]["agents"]["worker"]["enabled"] = False
    out = resolve_agent_model(cfg, "worker")
    assert out.get("enabled") is False


def test_resolve_new_schema_agent_fields_preserved():
    cfg = _new_schema_cfg()
    out = resolve_agent_model(cfg, "main")
    assert out.get("max_steps") == 40
    # 未声明的字段不污染
    assert "dispatchable" not in out


# --- 旧 schema 回退 ----------------------------------------------------------
def _old_schema_cfg():
    return {
        "brain": {
            "provider": "openai-compatible",
            "base_url": "https://old/v1",
            "model": "old-main",
            "api_key": "sk-old",
        },
        "runtime": {
            "executor": {
                "enabled": True,
                "provider": "openai-compatible",
                "base_url": "http://127.0.0.1:8085",
                "model": "old-worker",
            }
        },
    }


def test_resolve_old_schema_main():
    cfg = _old_schema_cfg()
    out = resolve_agent_model(cfg, "main")
    assert out["model"] == "old-main"
    assert out["base_url"] == "https://old/v1"


def test_resolve_old_schema_worker():
    cfg = _old_schema_cfg()
    out = resolve_agent_model(cfg, "worker")
    assert out["model"] == "old-worker"
    assert out.get("enabled") is True


def test_resolve_worker_missing_returns_empty():
    # 无 agents、无 runtime.executor -> 空 dict（调用方退化为主模型兼任）
    cfg = {"brain": {"model": "x"}}
    assert resolve_agent_model(cfg, "worker") == {}


def test_resolve_unknown_provider_falls_back_to_old():
    # agents 存在但 model 引用的 provider 不存在 -> 回退旧 schema
    cfg = _old_schema_cfg()
    cfg["runtime"]["agents"] = {"main": {"model": "ghost"}}
    out = resolve_agent_model(cfg, "main")
    assert out["model"] == "old-main"


def test_resolve_all_empty_when_no_agents():
    assert resolve_all_agent_models({"brain": {}}) == {}


# --- 解析结果可被 build_sdk_model 消费（无网络） ------------------------------
def test_resolve_output_consumable_by_sdk_model():
    cfg = _new_schema_cfg()
    out = resolve_agent_model(cfg, "main")
    # 构造 SDK Model 不发起网络请求，仅校验 shape 兼容
    model = sdk_model.build_sdk_model(out, timeout=5)
    assert model is not None
