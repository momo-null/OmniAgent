"""聊天路由：``channels/routes.yaml`` → (project_id, session_id)。

为什么单独成模块：路由解析有两个消费方——网关派发（``channel_gateway``）与**通道自身**
（如 napcat 要按会话所属项目去取最近的日记/场景块来拼场合自述）。规则只能有一份，
否则两边一旦不一致，就会出现「记忆按 A 项目写、自述按 B 项目读」的错配。

规则（两级匹配，精确优先）：
- ``"<channel>:<chat_id>"``（如 ``napcat:group:123``）→ 单个会话；
- ``"<channel>:*"`` → 整条通道。
命不中 → 空 project（由调用方决定退化为 ``default`` 或临时任务）。

session_id 与 chat 绑定（跨重启稳定）：``s_<channel>_<md5(channel:chat_id)[:12]>``，
routes 里显式给 ``session_id`` 时以其为准。
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict, Tuple

from utils import get_logger

logger = get_logger("gateway.chat_routes")


def load_routes() -> Dict[str, Any]:
    """读取路由表（缺失/损坏返回空 dict；绝不抛）。"""
    try:
        from omni_core.local.runtime_paths import channel_routes_file

        path = channel_routes_file()
    except Exception:
        return {}
    if not path.is_file():
        return {}
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001 —— 路由坏了退化为默认项目，不阻断消息
        logger.warning("聊天路由表读取失败（忽略）: %s: %s", type(e).__name__, e)
        return {}
    return data if isinstance(data, dict) else {}


def resolve(channel: str, chat_id: str) -> Tuple[str, str]:
    """(project_id, session_id)；project 为空表示未配置（调用方自行兜底）。"""
    routes = load_routes()
    key = f"{channel}:{chat_id}"
    rule = routes.get(key) or routes.get(f"{channel}:*") or {}
    if not isinstance(rule, dict):
        rule = {}
    project_id = str(rule.get("project_id") or "")
    h = hashlib.md5(key.encode("utf-8")).hexdigest()[:12]
    prefix = "s_feishu_" if channel == "feishu" else f"s_{channel}_"
    session_id = str(rule.get("session_id") or "") or f"{prefix}{h}"
    return project_id, session_id


def resolve_project(channel: str, chat_id: str, default: str = "default") -> str:
    """只取项目（取不到退化为 ``default``）。"""
    project_id = resolve(channel, chat_id)[0]
    return project_id or default
