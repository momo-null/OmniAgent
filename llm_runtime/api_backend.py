"""OpenAI-compatible API 推理后端

支持 OpenAI / DeepSeek / 通义千问 / vLLM / Ollama 等兼容接口。
"""
import json
import os
import urllib.request
import urllib.error
from typing import Dict, Any

from utils import get_logger
from .base import LLMRuntime


class ApiRuntime(LLMRuntime):
    """OpenAI-compatible Chat Completions API 后端"""

    def __init__(self, api_config: Dict[str, Any]):
        self.logger = get_logger("api_runtime")
        self.endpoint = api_config.get("endpoint", "https://api.openai.com/v1/chat/completions")
        self.model = api_config.get("model", "gpt-4o")

        api_key = api_config.get("api_key", "")
        if not api_key:
            api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            self.logger.warning("API key 未设置，请配置 api.api_key 或设置环境变量 OPENAI_API_KEY")
        self.api_key = api_key

        self.default_max_tokens = int(api_config.get("max_tokens", 512))
        self.default_temperature = float(api_config.get("temperature", 0.1))
        self.timeout = int(api_config.get("timeout_seconds", 60))

    def generate(self, prompt: str, params: Dict[str, Any]) -> str:
        if not self.api_key:
            raise RuntimeError("API key 未设置，无法调用外部 API")

        max_tokens = int(params.get("max_tokens", self.default_max_tokens))
        temperature = float(params.get("temperature", self.default_temperature))

        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }

        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )

        self.logger.info("调用外部 API: model=%s, endpoint=%s", self.model, self.endpoint)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="replace") if e.fp else ""
            raise RuntimeError(f"API 请求失败 (HTTP {e.code}): {err_body[:500]}")
        except urllib.error.URLError as e:
            raise RuntimeError(f"API 网络错误: {str(e.reason)}")
        except Exception as e:
            raise RuntimeError(f"API 请求异常: {str(e)}")

        try:
            result = json.loads(body)
        except json.JSONDecodeError:
            raise RuntimeError(f"API 返回非 JSON: {body[:300]}")

        if "error" in result:
            msg = result["error"].get("message", str(result["error"]))
            raise RuntimeError(f"API 返回错误: {msg}")

        choices = result.get("choices")
        if not choices:
            raise RuntimeError(f"API 返回无 choices: {body[:300]}")

        content = choices[0].get("message", {}).get("content", "")
        if not content:
            finish = choices[0].get("finish_reason", "unknown")
            raise RuntimeError(f"API 返回空内容 (finish_reason={finish})")

        self.logger.info("API 调用成功，返回 %d 字符", len(content))
        return content.strip()
