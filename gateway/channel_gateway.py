"""网关主入口 —— 常驻进程：消息通道 + 定时任务 + 派发队列，一体启动。

用法（OmniAgent 仓库根）：
    python -m gateway.channel_gateway                # 全量（通道按 config.channels 启用者）
    python -m gateway.channel_gateway --no-channels  # 只跑调度器（无飞书时先跑 cron）
    python -m gateway.channel_gateway --no-scheduler # 只跑通道（调飞书连通性）

云端部署：systemd/任务计划程序托管本入口；本地调试直接跑。
配置：~/.omniagent/config.yaml 的 channels: 段（v1.1 §3 示例）。
"""
from __future__ import annotations

import signal
import sys
import time

from . import dispatch
from .channels import context
from .scheduler import Scheduler

_ADAPTER_FACTORIES = {}  # name -> build(cfg)  惰性注册（避免 import 不必要 SDK）


def _register_factories() -> None:
    from .channels import feishu, wecom
    _ADAPTER_FACTORIES["feishu"] = feishu.build
    _ADAPTER_FACTORIES["wecom"] = wecom.build


def start_channels() -> int:
    """按 config.channels 启用项拉起适配器；返回成功数。"""
    import config
    _register_factories()
    channels_cfg = (config.load_config() or {}).get("channels", {}) or {}
    started = 0
    for name, cfg in channels_cfg.items():
        factory = _ADAPTER_FACTORIES.get(name)
        if factory is None:
            print(f"[gateway] 未知通道 {name}（跳过；可用: {sorted(_ADAPTER_FACTORIES)}）")
            continue
        try:
            adapter = factory(cfg)
            if adapter is None:
                print(f"[gateway] 通道 {name} 未启用（enabled=false）")
                continue
            adapter.connect(_on_message)
            context.register(adapter)
            started += 1
            print(f"[gateway] 通道 {name} 已连接")
        except Exception as e:
            print(f"[gateway] 通道 {name} 启动失败: {e}")
    return started


def _on_message(msg) -> None:
    """入站消息 → 打字机回执 → 派发。

    会话语义（对齐 hermes thread 绑定）：同一聊天窗口（channel:chat_id）固定绑定一个
    项目会话——消息带完整上下文连续对话；不传项目则归 default 项目下的固定会话。
    （旧语义「每消息=独立任务」改为固定会话，用户预期是与 agent 连续聊天。）
    """
    dispatch.ensure_worker()
    preview = msg.text[:60].replace("\n", " ")
    print(f"[gateway] 收到消息 <{msg.channel}:{msg.chat_id}> {preview}")
    try:
        if msg.reply is not None:
            msg.reply.send_text(f"⏳ 处理中…（{preview[:30]}）")
    except Exception:
        pass
    # 聊天路由：msg/<channel>/<chat_id> → 哪个项目 + 哪个会话（config.chat_routes 可覆盖）
    project_id, session_id = _route_chat(msg)
    dispatch.submit(msg.text, source=f"msg:{msg.channel}", reply=msg.reply,
                    project_id=project_id or None, session_id=session_id or None)


def _route_chat(msg) -> tuple:
    """消息 → (project_id, session_id)。默认：default 项目 + 通道固定会话。"""
    from . import config as _cfg_mod  # noqa: F401  (仅确保 config 已加载)
    import config as _config
    routes = (_config.load_config() or {}).get("chat_routes", {}) or {}
    key = f"{msg.channel}:{msg.chat_id}"
    rule = routes.get(key) or {}
    project_id = rule.get("project_id") or ""
    # 会话 ID：通道+聊天唯一，形如 s_feishu_<chat 哈希>（跨重启稳定）
    import hashlib
    h = hashlib.md5(key.encode("utf-8")).hexdigest()[:12]
    session_id = rule.get("session_id") or f"s_feishu_{h}" if msg.channel == "feishu" \
        else (rule.get("session_id") or f"s_{msg.channel}_{h}")
    return project_id, session_id


def main() -> int:
    no_channels = "--no-channels" in sys.argv
    no_scheduler = "--no-scheduler" in sys.argv
    print("[gateway] 启动（Ctrl+C 退出）")
    dispatch.ensure_worker()

    n = 0 if no_channels else start_channels()
    if not no_channels and n == 0:
        print("[gateway] ⚠ 无任何通道连接（检查 config.channels / FEISHU_APP_SECRET）")

    sched = None
    if not no_scheduler:
        sched = Scheduler(dispatch.submit)
        sched.start()
        print("[gateway] 调度器已启动（jobs 热加载）")

    stop = {"flag": False}

    def _sig(*_):
        stop["flag"] = True

    signal.signal(signal.SIGINT, _sig)
    try:
        signal.signal(signal.SIGTERM, _sig)
    except Exception:
        pass
    while not stop["flag"]:
        time.sleep(1)
    if sched:
        sched.stop()
    print("[gateway] 退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
