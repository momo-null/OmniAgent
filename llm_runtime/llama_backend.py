"""llama.cpp GGUF 推理后端"""
import os
import subprocess
import tempfile
from typing import Dict, Any

from utils import get_logger
from .base import LLMRuntime


class LlamaRuntime(LLMRuntime):
    """GGUF 推理后端"""

    def __init__(self, llama_root: str, gguf_model: str):
        self.llama_root = llama_root
        self.gguf_model = gguf_model
        self.logger = get_logger("llama_runtime")

    def _resolve_path(self, path: str) -> str:
        if os.path.isabs(path):
            return path
        return os.path.join(os.path.dirname(os.path.dirname(__file__)), path)

    def generate(self, prompt: str, params: Dict[str, Any]) -> str:
        llama_root = self._resolve_path(self.llama_root)
        gguf_model = self._resolve_path(self.gguf_model)
        cli_path = os.path.join(llama_root, "llama-completion.exe")

        if not os.path.exists(cli_path):
            raise FileNotFoundError(f"llama-completion.exe 不存在: {cli_path}")
        if not os.path.exists(gguf_model):
            raise FileNotFoundError(f"GGUF 模型不存在: {gguf_model}")

        max_tokens = int(params.get("max_tokens", 512))
        temperature = float(params.get("temperature", 0.1))
        top_p = float(params.get("top_p", 0.9))
        ctx_size = int(params.get("ctx_size", 0))
        timeout_seconds = int(params.get("timeout_seconds", 180))

        with tempfile.NamedTemporaryFile("w", delete=False, suffix=".txt", encoding="utf-8") as f:
            f.write(prompt)
            prompt_path = f.name

        cmd = [
            cli_path,
            "-m", gguf_model,
            "-f", prompt_path,
            "-n", str(max_tokens),
            "--temp", str(temperature),
            "--top-p", str(top_p),
            "-no-cnv",
            "--no-display-prompt",
            "--no-warmup",
            "--simple-io",
            "--verbosity", "1"
        ]
        if ctx_size > 0:
            cmd.extend(["--ctx-size", str(ctx_size)])

        try:
            self.logger.info("调用 llama.cpp 推理: %s", " ".join(cmd[:4]) + " ...")
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds
            )
        finally:
            try:
                os.remove(prompt_path)
            except OSError:
                pass

        if result.returncode != 0:
            msg = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(f"llama.cpp 推理失败: {msg}")

        output = result.stdout.strip()
        if not output:
            err = result.stderr.strip()
            raise RuntimeError(f"llama.cpp 未返回输出: {err}")

        return output
