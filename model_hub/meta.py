"""模型侧注文件（.meta.json）读写

每个 GGUF 文件可附带一个同名的 ``.meta.json`` 侧注文件，
保存该模型的持久化启动参数（threads / ctx_size / gpu_layers / port /
reasoning_budget / mmproj_path / name / description / tags）。

设计边界（model-meta-refactor, 2026-07-11）：
- 侧注文件与 GGUF 同目录、同名（仅扩展名不同）。
- meta 不存在时，模型使用代码默认值；meta 损坏时记录 warning 并视为无 meta。
- 保存时仅覆盖非 None 字段；显式传 None 表示删除该字段（回退默认值）。
"""
import json
import os
from typing import Any, Dict, List, Optional

# meta 文件支持的字段（白名单，避免写入无关键）
KNOWN_FIELDS = (
    "name",
    "description",
    "tags",
    "mmproj_path",
    "ctx_size",
    "gpu_layers",
    "threads",
    "port",
    "reasoning_budget",
)


class ModelMetaData:
    """单个 GGUF 模型的侧注数据（纯数据类）"""

    def __init__(
        self,
        name: Optional[str] = None,
        description: Optional[str] = None,
        tags: Optional[List[str]] = None,
        mmproj_path: Optional[str] = None,
        ctx_size: Optional[int] = None,
        gpu_layers: Optional[int] = None,
        threads: Optional[int] = None,
        port: Optional[int] = None,
        reasoning_budget: Optional[int] = None,
    ):
        self.name = name
        self.description = description
        self.tags = tags
        self.mmproj_path = mmproj_path
        self.ctx_size = ctx_size
        self.gpu_layers = gpu_layers
        self.threads = threads
        self.port = port
        self.reasoning_budget = reasoning_budget

    # ── 路径计算 ──────────────────────────────────
    @staticmethod
    def _meta_path(gguf_path: str) -> str:
        """由 GGUF 绝对路径推导同名 .meta.json 路径"""
        return os.path.splitext(gguf_path)[0] + ".meta.json"

    # ── 读取 ──────────────────────────────────────
    @classmethod
    def load_from(cls, gguf_path: str) -> Optional["ModelMetaData"]:
        """读取 GGUF 同目录的侧注文件

        Args:
            gguf_path: GGUF 文件绝对路径

        Returns:
            解析成功的 ``ModelMetaData``；文件不存在或 JSON 损坏时返回 None
        """
        meta_path = cls._meta_path(gguf_path)
        if not os.path.exists(meta_path):
            return None
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            from utils import get_logger

            get_logger("model_meta").warning(
                "meta 文件损坏，跳过: %s (%s)", meta_path, e
            )
            return None
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: Any) -> Optional["ModelMetaData"]:
        """从 dict 构造（忽略未知字段）"""
        if not isinstance(data, dict):
            return None
        tags = data.get("tags")
        if not isinstance(tags, list):
            tags = []
        return cls(
            name=data.get("name"),
            description=data.get("description"),
            tags=tags,
            mmproj_path=data.get("mmproj_path"),
            ctx_size=data.get("ctx_size"),
            gpu_layers=data.get("gpu_layers"),
            threads=data.get("threads"),
            port=data.get("port"),
            reasoning_budget=data.get("reasoning_budget"),
        )

    # ── 序列化 ────────────────────────────────────
    def to_dict(self) -> Dict[str, Any]:
        """仅输出非 None 字段"""
        d: Dict[str, Any] = {}
        if self.name is not None:
            d["name"] = self.name
        if self.description is not None:
            d["description"] = self.description
        if self.tags is not None:
            d["tags"] = self.tags
        if self.mmproj_path is not None:
            d["mmproj_path"] = self.mmproj_path
        if self.ctx_size is not None:
            d["ctx_size"] = self.ctx_size
        if self.gpu_layers is not None:
            d["gpu_layers"] = self.gpu_layers
        if self.threads is not None:
            d["threads"] = self.threads
        if self.port is not None:
            d["port"] = self.port
        if self.reasoning_budget is not None:
            d["reasoning_budget"] = self.reasoning_budget
        return d

    # ── 写入（合并） ──────────────────────────────
    @classmethod
    def save_to(cls, gguf_path: str, updates: Dict[str, Any]) -> str:
        """合并更新写入侧注文件

        合并规则：
        - ``updates`` 中非 None 的字段覆盖现有值
        - ``updates`` 中为 None 的已知字段：从文件中删除（回退默认值）
        - 未知字段忽略

        Args:
            gguf_path: GGUF 文件绝对路径（侧注文件同名同目录）
            updates: 要合并的字段 dict

        Returns:
            .meta.json 文件的绝对路径
        """
        meta_path = cls._meta_path(gguf_path)
        existing: Dict[str, Any] = {}
        if os.path.exists(meta_path):
            try:
                with open(meta_path, "r", encoding="utf-8") as f:
                    existing = json.load(f) or {}
            except (json.JSONDecodeError, OSError):
                existing = {}

        for k, v in updates.items():
            if k not in KNOWN_FIELDS:
                continue
            if v is not None:
                existing[k] = v
            elif k in existing:
                del existing[k]

        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2, ensure_ascii=False)
        return meta_path
