"""ModelManager 职责拆分 · 模型启动/预热/校验与侧注持久化（逐字搬迁，零逻辑改动）。"""
import json
import os
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

import psutil
import yaml

import config
from utils import get_logger
from model_hub.meta import ModelMetaData

logger = get_logger("model_manager")


class ServeMixin:
    """模型启动/预热/校验与侧注持久化。"""

    def start_model(
        self,
        name: str,
        wait_ready: bool = True,
        timeout: int = 180,
        overrides: Optional[Dict[str, Any]] = None,
        profile: Optional[str] = None,
        gguf_path: Optional[str] = None,
        models_dir: Optional[str] = None,
    ) -> Dict[str, Any]:
        """启动模型（按 name 或 gguf_path 定位）

        Args:
            name: 模型名（精确/模糊匹配扫描结果）
            gguf_path: 直接指定 GGUF 路径（优先于 name 查找）
            wait_ready: 是否等待健康检查通过
            overrides: 逐参数覆盖
            profile: 预设名
            models_dir: 扫描目录（name 查找时用到）

        Returns:
            {name, port, pid, status}
        """
        # 定位 GGUF
        if not gguf_path:
            entry = self._find_model_by_name(name, models_dir)
            if not entry:
                raise ValueError(f"模型未找到: {name}")
            gguf_path = entry["gguf_path"]
        if not os.path.isabs(gguf_path):
            gguf_path = self._resolve_path(gguf_path)
        if not os.path.exists(gguf_path):
            raise FileNotFoundError(f"GGUF 文件不存在: {gguf_path}")

        run_name = name or os.path.splitext(os.path.basename(gguf_path))[0]
        if run_name in self.processes and self._is_process_alive(
            self.processes[run_name].get("pid")
        ):
            old = self.processes[run_name]
            logger.warning("模型 %s 已在运行 (pid=%s)", run_name, old.get("pid"))
            return {
                "name": run_name,
                "port": old["port"],
                "pid": old["pid"],
                "status": "already_running",
            }

        params = self._resolve_launch_params(
            gguf_path, name=run_name, overrides=overrides, profile=profile
        )
        # 端口：0/None/指定但被占用 → 自动分配
        port = params.get("port") or 0
        if not port or not self._port_free(port):
            port = self._alloc_port()
        params["port"] = port

        args = self._build_server_args_from_params(params)
        env = self._build_env()

        logger.info(
            "启动模型 %s, 端口=%d, ctx=%d, gpu_layers=%d, threads=%d ...",
            run_name, port, params["ctx_size"], params["gpu_layers"], params["threads"],
        )

        log_dir = os.path.join(os.path.dirname(self.config_path), "temp")
        os.makedirs(log_dir, exist_ok=True)
        out_log = os.path.join(log_dir, f"llama_server_{run_name}.out")
        err_log = os.path.join(log_dir, f"llama_server_{run_name}.err")

        with open(out_log, "w", encoding="utf-8") as fo, open(err_log, "w", encoding="utf-8") as fe:
            proc = subprocess.Popen(
                args,
                stdout=fo,
                stderr=fe,
                env=env,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )

        self.processes[run_name] = {
            "pid": proc.pid,
            "port": port,
            "process": proc,
            "started_at": time.time(),
            "out_log": out_log,
            "err_log": err_log,
            "params": params,
        }

        result = {"name": run_name, "port": port, "pid": proc.pid, "status": "started"}

        if wait_ready:
            status, logs = self._wait_ready_or_fail(proc, port, run_name, timeout)
            result["status"] = status
            if logs:
                result["logs"] = logs
            if status == "ready":
                logger.info("模型 %s 已就绪 (pid=%s, port=%d)", run_name, proc.pid, port)
            elif status == "failed":
                logger.error("模型 %s 启动即退出 (pid=%s)，日志尾部已随响应返回",
                             run_name, proc.pid)
            else:
                logger.warning("模型 %s 健康检查超时 (pid=%s)", run_name, proc.pid)

        # 轻量 warmup 生成：就绪后发一次极短请求，触发 CUDA graph 捕获，
        # 避免首个用户请求因首 token 延迟而超时。失败仅记录日志，不影响启动状态。
        if result["status"] == "ready":
            self._warmup_generate(port)

        return result

    def start_model_path(
        self,
        gguf_path: str,
        name: Optional[str] = None,
        wait_ready: bool = True,
        timeout: int = 180,
        overrides: Optional[Dict[str, Any]] = None,
        profile: Optional[str] = None,
    ) -> Dict[str, Any]:
        """启动扫描发现的未注册 GGUF 文件（自动分配端口）

        与 ``start_model`` 类似，但直接以路径定位，并读取其侧注参数。
        """
        if not os.path.isabs(gguf_path):
            gguf_path = self._resolve_path(gguf_path)
        if not os.path.exists(gguf_path):
            raise FileNotFoundError(f"GGUF 文件不存在: {gguf_path}")
        if name is None:
            name = os.path.splitext(os.path.basename(gguf_path))[0]
        if name in self.processes and self._is_process_alive(self.processes[name].get("pid")):
            old = self.processes[name]
            return {"name": name, "port": old["port"], "pid": old["pid"], "status": "already_running"}

        # 与 start_model 共用同一份参数解析。此前这里是**内联复制的副本**，
        # 于是投影自动探测 / extra_args 透传 / 侧注合并每加一处就要改两遍、
        # 漏一遍就出现「按路径启动」与「按名字启动」行为不一致。
        params = self._resolve_launch_params(
            gguf_path, name=name, overrides=overrides, profile=profile
        )

        port = params.get("port") or 0
        if not port or not self._port_free(port):
            port = self._alloc_port()
        params["port"] = port

        args = self._build_server_args_from_params(params)
        env = self._build_env()

        logger.info("启动未注册模型 %s (path=%s), 端口=%d ...", name, gguf_path, port)

        log_dir = os.path.join(os.path.dirname(self.config_path), "temp")
        os.makedirs(log_dir, exist_ok=True)
        out_log = os.path.join(log_dir, f"llama_server_{name}.out")
        err_log = os.path.join(log_dir, f"llama_server_{name}.err")

        with open(out_log, "w", encoding="utf-8") as fo, open(err_log, "w", encoding="utf-8") as fe:
            proc = subprocess.Popen(
                args,
                stdout=fo,
                stderr=fe,
                env=env,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )

        self.processes[name] = {
            "pid": proc.pid,
            "port": port,
            "process": proc,
            "started_at": time.time(),
            "out_log": out_log,
            "err_log": err_log,
            "params": params,
        }

        result = {"name": name, "port": port, "pid": proc.pid, "status": "started"}
        if wait_ready:
            status, logs = self._wait_ready_or_fail(proc, port, name, timeout)
            result["status"] = status
            if logs:
                result["logs"] = logs
            if status == "failed":
                logger.error("未注册模型 %s 启动即退出，日志尾部已随响应返回", name)
        return result

    def _wait_ready_or_fail(
        self, proc: Any, port: int, name: str, timeout: int
    ) -> Tuple[str, Optional[Dict[str, str]]]:
        """等待就绪；**进程提前退出**则立即判失败并取回 stderr 尾部。

        为什么需要：``extra_args`` 是自由透传通道，写错参数会让 llama-server
        秒退（如 unrecognized argument）。此前只能等满 ``timeout``（默认 180s）
        才报 timeout，用户白等三分钟且看不到原因。

        Returns:
            ``(status, logs)``：status ∈ {ready, failed, timeout}；
            logs 仅在 failed 时非空（``{"out": ..., "err": ...}``）。
        """
        ready = self._wait_health_check(port, timeout, proc=proc)
        if ready:
            return "ready", None
        if proc.poll() is not None:              # 进程已退出 = 启动参数/环境有错
            return "failed", self.get_model_logs(name, lines=20)
        return "timeout", None

    def _warmup_generate(self, port: int, timeout: int = 120) -> None:
        """就绪后发一次极短生成，触发 CUDA graph 捕获，避免首个用户请求因
        首 token 延迟而超时。失败仅记录日志，不影响启动状态。"""
        try:
            self._chat_completion(port, "hi", timeout=timeout, max_tokens=1)
            logger.info("模型 warmup 生成完成（端口=%d）", port)
        except Exception as e:
            logger.warning("模型 warmup 生成失败（不影响启动）: %s", e)

    def validate_model(
        self,
        name: str,
        prompt: Optional[str] = None,
        image_path: Optional[str] = None,
        overrides: Optional[Dict[str, Any]] = None,
        profile: Optional[str] = None,
        gguf_path: Optional[str] = None,
        models_dir: Optional[str] = None,
        timeout: int = 180,
    ) -> Dict[str, Any]:
        """校验模型：仅对「已在运行」的实例发测试请求，不自动启动。

        B-约束（用户明确要求）：只有用户点击「启动」才会拉起模型；
        校验不得自行启动模型。若模型未运行，直接返回明确错误，交由前端提示。
        """
        if prompt is None:
            prompt = "请用一句话介绍自己。"
        # B：未运行的模型无法校验，直接告知先启动，不起模型
        if name not in self.processes:
            return {
                "name": name, "ok": False, "reply": "",
                "prompt": prompt,
                "error": "模型未运行，请先点击「启动」再校验",
                "base_url": "", "model": name,
            }
        try:
            port = self.processes[name]["port"]
            base_url = f"http://127.0.0.1:{port}/v1"
            model_id = self._fetch_model_id(port) or name
            reply = self._chat_completion(port, prompt, image_path, timeout=120)
            return {"name": name, "ok": True, "reply": reply, "prompt": prompt,
                    "error": None, "base_url": base_url, "model": model_id}
        except Exception as e:
            logger.warning("模型 %s 校验失败: %s", name, e)
            port = self.processes.get(name, {}).get("port")
            return {"name": name, "ok": False, "reply": "", "prompt": prompt,
                    "error": str(e),
                    "base_url": f"http://127.0.0.1:{port}/v1" if port else "",
                    "model": name}

    def save_model_meta(
        self, name: str, req: Any, models_dir: Optional[str] = None
    ) -> Dict[str, Any]:
        """保存/更新模型侧注（.meta.json）

        Args:
            name: 模型名（用于查找 GGUF，若 req 未提供 gguf_path）
            req: ModelMetaSaveRequest 实例（含 gguf_path 与可选字段）
            models_dir: 扫描目录（name 查找时用到）

        Returns:
            {name, gguf_path, meta_path, status}
        """
        gguf_path = getattr(req, "gguf_path", None)
        if not gguf_path:
            entry = self._find_model_by_name(name, models_dir)
            if not entry:
                raise ValueError(f"模型未找到: {name}")
            gguf_path = entry["gguf_path"]
        if not os.path.isabs(gguf_path):
            gguf_path = self._resolve_path(gguf_path)

        # 仅处理客户端「显式发送」的字段：未传的字段保持不动，
        # 显式传 null 的字段视为删除（回退默认值）。
        updates = req.model_dump(exclude_unset=True)
        updates.pop("gguf_path", None)

        meta_path = ModelMetaData.save_to(gguf_path, updates)
        result_name = updates.get("name") or name
        logger.info("已保存侧注: %s -> %s", result_name, meta_path)
        return {
            "name": result_name,
            "gguf_path": gguf_path,
            "meta_path": meta_path,
            "status": "saved",
        }

    def _fetch_model_id(self, port: int) -> Optional[str]:
        """从运行中的 llama-server 拉取真实 model id（GET /v1/models）。

        返回首个模型的 id；接口不可用或响应不符合预期时返回 None，
        交由调用方回退到模型名（如 MiniCPM5-2B-Q4_K_M）。
        """
        try:
            url = f"http://127.0.0.1:{port}/v1/models"
            with urllib.request.urlopen(url, timeout=10) as resp:
                out = json.loads(resp.read().decode("utf-8"))
            data = out.get("data") or []
            if data and data[0].get("id"):
                return str(data[0]["id"])
        except Exception:
            pass
        return None

    def _chat_completion(
        self,
        port: int,
        prompt: str,
        image_path: Optional[str] = None,
        timeout: int = 120,
        max_tokens: int = 256,
        enable_thinking: bool = False,
    ) -> str:
        """向运行中的 llama-server 发一次 chat 请求，返回回复文本

        Args:
            timeout: 单次请求超时（秒）。默认 120：模型加载后**首次生成**会触发
                CUDA graph 捕获 / 首 token 延迟，常需 30~60s，旧默认 30s 会误杀
                首个真实请求（server 端其实 200 完成了，只是客户端先超时）。
            max_tokens: 生成上限，warmup 用极小值即可。
            enable_thinking: 是否开启模型思考链。默认 False —— 本地执行层做快速
                决策/工具调用，Qwen3.x 等模型默认开启 thinking 会把 token 全耗在
                reasoning_content 上导致 content 为空（finish_reason=length），
                执行场景需直接答案，故默认关闭。
        """
        content: Any = prompt
        if image_path and os.path.exists(image_path):
            import base64
            with open(image_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("utf-8")
            content = [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ]
        payload: Dict[str, Any] = {
            "model": "local",
            "messages": [{"role": "user", "content": content}],
            "temperature": 0.1,
            "max_tokens": max_tokens,
        }
        # Qwen3.x 等支持 enable_thinking 的模型，关闭思考链以拿到直接答案
        if not enable_thinking:
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        url = f"http://127.0.0.1:{port}/v1/chat/completions"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            out = json.loads(resp.read().decode("utf-8"))
        return out["choices"][0]["message"]["content"]
