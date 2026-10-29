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
    from .channels import feishu, napcat, wecom
    _ADAPTER_FACTORIES["feishu"] = feishu.build
    _ADAPTER_FACTORIES["wecom"] = wecom.build
    _ADAPTER_FACTORIES["napcat"] = napcat.build


def build_adapter(name: str):
    """按 ``config.channels.<name>`` 构造适配器（**不连接**），供非网关进程使用。

    背景：``msg_send_*`` 工具取通道走本进程的注册表（``channels.context``），只有网关
    进程里有实例。在 Web 后端进程里让 agent 主动发消息时，注册表是空的 → 工具返回
    "无可用通道"，模型只能绕去 shell 调 OneBot HTTP（实测发生过：绕了 9 步）。

    这里只构造「会发消息」的句柄：napcat 的发送走 HTTP，不依赖 WS 长连接，所以
    **不 connect** 也能正常发；返回 None 表示通道未知或未启用（fail-closed）。
    """
    try:
        _register_factories()
        factory = _ADAPTER_FACTORIES.get(name)
        if factory is None:
            return None
        import config as _config
        cfg = ((_config.load_config() or {}).get("channels") or {}).get(name) or {}
        return factory(cfg)
    except Exception:
        return None


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
    """入站消息 → 派发。

    会话语义（对齐 hermes thread 绑定）：同一聊天窗口（channel:chat_id）固定绑定一个
    项目会话——消息带完整上下文连续对话；不传项目则归 default 项目下的固定会话。
    （旧语义「每消息=独立任务」改为固定会话，用户预期是与 agent 连续聊天。）

    刻意**不发「处理中」回执**：消息通道对面是人，先弹一句机器味的状态提示破坏拟人感
    （且每条消息都多占一次发信配额）。进度反馈由通道自身能力承担，不在此硬发。
    """
    dispatch.ensure_worker()
    preview = msg.text[:60].replace("\n", " ")
    print(f"[gateway] 收到消息 <{msg.channel}:{msg.chat_id}> {preview}")
    # 聊天路由：msg/<channel>/<chat_id> → 哪个项目 + 哪个会话（config.chat_routes 可覆盖）
    project_id, session_id = _route_chat(msg)
    # 能力门透传：外部可达通道默认 allow_tools=False → 该来源任务为纯对话（无任何工具）。
    # 网关只读通道自声明，不判断"这是谁发的"——判定权在通道适配器。
    dispatch.submit(msg.text, source=f"msg:{msg.channel}", reply=msg.reply,
                    project_id=project_id or None, session_id=session_id or None,
                    # 标题/历史用通道自声明的「人话」（text 里可能带场合自述前缀，
                    # 只该喂模型，不该进侧栏标题与会话气泡）
                    title=str(getattr(msg, "title", "") or ""),
                    display_text=str(getattr(msg, "display_text", "") or "") or msg.text,
                    # 回发模式由通道自声明（QQ=explicit 显式发送；飞书/企微=auto 直连）
                    reply_mode=str(getattr(msg, "reply_mode", "auto") or "auto"),
                    tools_enabled=bool(getattr(msg, "allow_tools", False)),
                    # 多模态附件只作用于当轮（会话记录仍写文本投影，不落 base64）
                    attachments=list(getattr(msg, "attachments", None) or []),
                    # 入站是语音：仅 auto 通道（Web/飞书直连）据此自动语音回发；
                    # explicit 通道（QQ 等）不自动回发，发不发语音由模型调 msg_send_voice 决定。
                    voice_reply=bool(getattr(msg, "voice", False)))


def _route_chat(msg) -> tuple:
    """消息 → (project_id, session_id)。默认：default 项目 + 通道固定会话。

    路由规则（``config.chat_routes``）两级匹配，精确优先：
    - ``"napcat:private:123456"`` → 单个会话；
    - ``"napcat:*"``             → 整条通道（所有 QQ 私聊/群）→ 同一项目。
    配了 project_id 的消息不再是「临时任务」（临时任务跑完即从任务索引删除，
    Web 任务列表里看不到），而是持久归属到该项目、可在界面点开看过程。
    """
    import config as _config
    routes = (_config.load_config() or {}).get("chat_routes", {}) or {}
    key = f"{msg.channel}:{msg.chat_id}"
    rule = routes.get(key) or routes.get(f"{msg.channel}:*") or {}
    project_id = rule.get("project_id") or ""
    # 会话 ID：通道+聊天唯一，形如 s_feishu_<chat 哈希>（跨重启稳定）
    import hashlib
    h = hashlib.md5(key.encode("utf-8")).hexdigest()[:12]
    session_id = rule.get("session_id") or f"s_feishu_{h}" if msg.channel == "feishu" \
        else (rule.get("session_id") or f"s_{msg.channel}_{h}")
    return project_id, session_id


def _ensure_plugins() -> None:
    """预装载插件（幂等）：语音等 capability 在插件 startup 时注册，网关进程必须
    先装载一次——否则第一条语音消息到达时 capability 还不存在（worker 里 ToolLoop
    装载发生在派发之后，晚于语音转写）。"""
    try:
        import config as _config

        from omni_core.tools.loader import load_plugins

        load_plugins(_config.load_config() or {})
    except Exception as e:
        print(f"[gateway] 插件预装载失败（语音等能力可能不可用）: {e}")


def main() -> int:
    no_channels = "--no-channels" in sys.argv
    no_scheduler = "--no-scheduler" in sys.argv
    print("[gateway] 启动（Ctrl+C 退出）")
    dispatch.ensure_worker()
    _ensure_plugins()

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
