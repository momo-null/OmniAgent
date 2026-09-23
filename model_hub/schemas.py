"""Pydantic 响应/请求模型

供 backend router 层构建结构化 JSON 响应与解析请求体使用。

重构说明（model-meta-refactor, 2026-07-11）：
- ``ModelInfo`` 扩展为「扫描 + 侧注合并」的统一模型信息。
- 新增 ``ModelMetaSaveRequest`` 用于保存侧注文件。
- ``LaunchParams`` 增加可选 ``gguf_path``（启动未注册模型时直接定位文件）。
- 删除旧的 ``ScanModel``（扫描与注册已统一为 ``ModelInfo``）。
"""
from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class ModelInfo(BaseModel):
    """统一模型信息（扫描 + 侧注合并结果）"""
    name: str
    gguf_path: str = ""
    description: str = ""
    size_mb: float = 0.0
    quant: str = "unknown"
    gpu_layers: int = 99
    ctx_size: int = 8192
    threads: int = 8
    port: Optional[int] = None  # None = 自动分配
    reasoning_budget: int = 0
    tags: List[str] = []
    mmproj_path: Optional[str] = None
    has_mmproj: bool = False
    has_meta: bool = False  # 是否存在 .meta.json 侧注
    status: str = "stopped"
    pid: Optional[int] = None


class ModelStatus(BaseModel):
    """模型详细状态（含运行态与日志）"""
    name: str
    status: str = "stopped"
    port: int = 8085
    pid: Optional[int] = None
    process_alive: Optional[bool] = None
    healthy: Optional[bool] = None
    uptime_s: Optional[float] = None
    started_at: Optional[float] = None
    logs: Optional[Dict[str, str]] = None


class ActiveModel(BaseModel):
    """运行中的模型摘要"""
    name: str
    port: int
    pid: Optional[int] = None
    started_at: Optional[float] = None
    uptime_s: float = 0.0
    process_alive: bool = False
    healthy: bool = False


class GpuInfo(BaseModel):
    """GPU 显存信息"""
    name: str = "unknown"
    memory_total_mb: float = 0.0
    memory_used_mb: float = 0.0
    memory_free_mb: float = 0.0
    error: Optional[str] = None


class TrainingStatus(BaseModel):
    """训练流水线运行状态"""
    step: str = "idle"
    running: bool = False
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    last_error: Optional[str] = None


class AgentStatus(BaseModel):
    """智能体运行状态"""
    running: bool = False
    scene: str = "office"
    max_steps: int = 10
    last_error: Optional[str] = None
    step: int = 0
    history_len: int = 0
    last_perception: Optional[Dict[str, Any]] = None


class LaunchParams(BaseModel):
    """一次启动的逐参数覆盖（UI 精准控制）"""
    threads: Optional[int] = None
    ctx_size: Optional[int] = None
    gpu_layers: Optional[int] = None
    port: Optional[int] = None
    reasoning_budget: Optional[int] = None
    use_mmproj: Optional[bool] = None
    profile: Optional[str] = None
    gguf_path: Optional[str] = None  # 直接定位 GGUF 文件（扫描发现的未注册模型）


class ModelValidateRequest(LaunchParams):
    """模型校验请求（启动->测试->停止）"""
    prompt: Optional[str] = None
    image_path: Optional[str] = None


class ModelStartPathRequest(BaseModel):
    """启动扫描发现的未注册 GGUF"""
    path: str
    name: Optional[str] = None
    threads: Optional[int] = None
    ctx_size: Optional[int] = None
    gpu_layers: Optional[int] = None
    port: Optional[int] = None
    reasoning_budget: Optional[int] = None
    use_mmproj: Optional[bool] = None
    profile: Optional[str] = None


class ModelMetaSaveRequest(BaseModel):
    """保存模型侧注（.meta.json）的请求体

    所有字段均为可选：
    - 非 None 字段写入/覆盖侧注
    - 显式传 None 的已知字段：从侧注中删除（回退默认值）
    """
    gguf_path: str  # 目标 GGUF 文件路径（侧注同名同目录）
    name: Optional[str] = None
    description: Optional[str] = None
    tags: Optional[List[str]] = None
    mmproj_path: Optional[str] = None
    ctx_size: Optional[int] = None
    gpu_layers: Optional[int] = None
    threads: Optional[int] = None
    port: Optional[int] = None
    reasoning_budget: Optional[int] = None


class ModelValidationResult(BaseModel):
    """模型校验结果"""
    name: str
    ok: bool
    reply: str = ""
    prompt: str = ""
    error: Optional[str] = None
    base_url: str = ""        # 本地 llama-server 实际端点（http://127.0.0.1:{port}/v1）
    model: str = ""           # 真实 model id（/v1/models 取，回退模型名）
