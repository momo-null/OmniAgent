"""llama-server API 推理后端

连接本地 llama-server 的 OpenAI 兼容接口，
支持同步和流式推理。
"""
import json
import urllib.request
import urllib.error
from typing import Dict, Any, Optional, Generator

from utils import get_logger
from .base import LLMRuntime

logger = get_logger("server_backend")


class LlamaServerRuntime(LLMRuntime):
    """连接本地 llama-server 的推理后端

    llama-server 启动后暴露 OpenAI 兼容接口，
    本类通过 HTTP 请求与之交互。
    """

    def __init__(self, base_url: str = "http://127.0.0.1:8085",
                 default_max_tokens: int = 512,
                 default_temperature: float = 0.1,
                 timeout_seconds: int = 120):
        self.base_url = base_url.rstrip("/")
        self.default_max_tokens = default_max_tokens
        self.default_temperature = default_temperature
        self.timeout_seconds = timeout_seconds
        self.logger = get_logger("server_runtime")

    def generate(self, prompt: str, params: Dict[str, Any]) -> str:
        """同步生成 — 调用 /v1/chat/completions"""
        max_tokens = int(params.get("max_tokens", self.default_max_tokens))
        temperature = float(params.get("temperature", self.default_temperature))
        top_p = float(params.get("top_p", 0.9))

        payload = {
            "model": "local",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": top_p,
            "stream": False,
        }

        return self._call_chat_completions(payload)

    def generate_with_messages(self, messages: list, params: Dict[str, Any]) -> str:
        """多轮对话生成 — 传入 messages 列表"""
        max_tokens = int(params.get("max_tokens", self.default_max_tokens))
        temperature = float(params.get("temperature", self.default_temperature))
        top_p = float(params.get("top_p", 0.9))

        payload = {
            "model": "local",
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": top_p,
            "stream": False,
        }

        return self._call_chat_completions(payload)

    def generate_stream(self, prompt: str, params: Dict[str, Any]) -> Generator[str, None, None]:
        """流式生成 — 返回逐 token 的生成器"""
        max_tokens = int(params.get("max_tokens", self.default_max_tokens))
        temperature = float(params.get("temperature", self.default_temperature))

        payload = {
            "model": "local",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
        }

        yield from self._call_chat_completions_stream(payload)

    def generate_stream_with_messages(self, messages: list, params: Dict[str, Any]) -> Generator[str, None, None]:
        """多轮对话流式生成"""
        max_tokens = int(params.get("max_tokens", self.default_max_tokens))
        temperature = float(params.get("temperature", self.default_temperature))

        payload = {
            "model": "local",
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
        }

        yield from self._call_chat_completions_stream(payload)

    def health_check(self) -> bool:
        """检查 llama-server 是否就绪"""
        try:
            url = f"{self.base_url}/health"
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("status") == "ok"
        except Exception:
            return False

    # ── 内部方法 ──────────────────────────────────────────

    def _call_chat_completions(self, payload: Dict[str, Any]) -> str:
        """调用 /v1/chat/completions 同步接口"""
        url = f"{self.base_url}/v1/chat/completions"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        self.logger.info("调用 llama-server: %s", url)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace") if e.fp else ""
            raise RuntimeError(f"llama-server 请求失败 (HTTP {e.code}): {err_body[:500]}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"llama-server 连接失败: {e.reason}")
        except Exception as e:
            raise RuntimeError(f"llama-server 请求异常: {e}")

        try:
            result = json.loads(body)
        except json.JSONDecodeError:
            raise RuntimeError(f"llama-server 返回非 JSON: {body[:300]}")

        if "error" in result:
            msg = result["error"].get("message", str(result["error"]))
            raise RuntimeError(f"llama-server 返回错误: {msg}")

        choices = result.get("choices")
        if not choices:
            raise RuntimeError(f"llama-server 返回无 choices: {body[:300]}")

        content = choices[0].get("message", {}).get("content", "")
        self.logger.info("llama-server 调用成功，返回 %d 字符", len(content))
        return content.strip()

    def _call_chat_completions_stream(self, payload: Dict[str, Any]) -> Generator[str, None, None]:
        """调用 /v1/chat/completions 流式接口，逐 token 返回"""
        try:
            import httpx
        except ImportError:
            # 降级为非流式
            self.logger.warning("httpx 未安装，降级为同步模式")
            yield self._call_chat_completions(payload)
            return

        url = f"{self.base_url}/v1/chat/completions"
        headers = {"Content-Type": "application/json"}

        with httpx.stream(
            "POST", url, json=payload, headers=headers, timeout=self.timeout_seconds
        ) as response:
            for line in response.iter_lines():
                if not line or not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str.strip() == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                    delta = chunk.get("choices", [{}])[0].get("delta", {})
                    token = delta.get("content", "")
                    if token:
                        yield token
                except json.JSONDecodeError:
                    continue
