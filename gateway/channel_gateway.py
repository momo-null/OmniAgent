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

from . import context, dispatch
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
    """入站消息 → 打字机回执 → 派发（每消息=独立任务，上下文由 agent 调 MCP 现取）。"""
    dispatch.ensure_worker()
    preview = msg.text[:60].replace("\n", " ")
    print(f"[gateway] 收到消息 <{msg.channel}:{msg.chat_id}> {preview}")
    try:
        if msg.reply is not None:
            msg.reply.send_text(f"⏳ 处理中…（{preview[:30]}）")
    except Exception:
        pass
    dispatch.submit(msg.text, source=f"msg:{msg.channel}", reply=msg.reply)


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
