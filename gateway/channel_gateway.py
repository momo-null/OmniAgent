"""网关主入口 —— 常驻进程：消息通道 + 定时任务 + 派发队列，一体启动。

用法（OmniAgent 仓库根）：
    python -m gateway.channel_gateway                # 全量（channels/ 下有配置文件的通道）
    python -m gateway.channel_gateway --no-channels  # 只跑调度器（无飞书时先跑 cron）
    python -m gateway.channel_gateway --no-scheduler # 只跑通道（调飞书连通性）

云端部署：systemd/任务计划程序托管本入口；本地调试直接跑。
配置：``~/.omniagent/channels/<name>.yaml``（每通道一文件，与 plugins/ 同构）+
``channels/routes.yaml``（聊天路由）。不再读主配置的 channels:/chat_routes: 段。
"""
from __future__ import annotations

import signal
import sys
import time
from typing import Any, Dict, List

from . import dispatch
from .channels import context
from .scheduler import Scheduler, submit_via_web

_ADAPTER_FACTORIES = {}  # name -> build(cfg)  惰性注册（避免 import 不必要 SDK）


def channel_config(name: str) -> Dict[str, Any]:
    """通道自有配置 ``~/.omniagent/channels/<name>.yaml``（缺失/损坏返回空 dict）。

    与 ``plugin_config``（omni_core/tools/loader.py）完全对称：一通道一文件。
    通道连接信息（token/ws_url 等）是部署事实，不该和 brain/runtime 等 core 配置
    挤在同一份主配置里；独立成文件也为通道插件化预留了同一套布局。
    """
    try:
        from omni_core.local.runtime_paths import channel_config_file

        path = channel_config_file(name)
    except Exception:
        return {}
    if not path.is_file():
        return {}
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[gateway] 通道 {name} 配置读取失败（忽略）: {e}")
        return {}
    return data if isinstance(data, dict) else {}


def _channel_names() -> List[str]:
    """枚举 ``channels/`` 下的通道配置文件名（routes.yaml 是路由表，不是通道）。"""
    try:
        from omni_core.local.runtime_paths import channels_root

        return sorted(p.stem for p in channels_root().glob("*.yaml") if p.stem != "routes")
    except Exception:
        return []


def _load_routes() -> Dict[str, Any]:
    """聊天路由表（实现已抽到 ``gateway.chat_routes``，与通道层共用一份规则）。"""
    from .chat_routes import load_routes

    return load_routes()


def _register_factories() -> None:
    from .channels import bilibili, feishu, napcat, wecom, xiaohongshu
    _ADAPTER_FACTORIES["bilibili"] = bilibili.build
    _ADAPTER_FACTORIES["feishu"] = feishu.build
    _ADAPTER_FACTORIES["wecom"] = wecom.build
    _ADAPTER_FACTORIES["napcat"] = napcat.build
    _ADAPTER_FACTORIES["xiaohongshu"] = xiaohongshu.build


def build_adapter(name: str):
    """按 ``channels/<name>.yaml`` 构造适配器（**不连接**），供非网关进程使用。

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
        return factory(channel_config(name))
    except Exception:
        return None


def channel_client(name: str):
    """按通道名构造**只读客户端**（不 connect、不受 enabled 门限）。

    插件只读工具与控制面 API 只依赖本宿主能力，不 import 具体通道实现：通道实现
    搬家时只改这里的模块解析，调用方零改动。与 ``build_adapter`` 的区别是后者管
    发送路径（fail-closed，未启用返回 None），本函数管读取路径——``enabled`` 只
    决定网关要不要自动拉起长连接，不该让只读工具在手动联调时也不可用。

    通道模块需提供 ``client(cfg)``；未知/损坏/构造失败一律返回 None（调用方报错）。
    """
    if not name or not name.isidentifier():
        return None
    try:
        from importlib import import_module

        mod = import_module(f".channels.{name}", __package__)
        factory = getattr(mod, "client", None)
        return factory(channel_config(name)) if callable(factory) else None
    except Exception:
        return None


def start_channels() -> int:
    """拉起 ``channels/`` 下所有已启用通道；返回成功数。"""
    _register_factories()
    started = 0
    for name in _channel_names():
        factory = _ADAPTER_FACTORIES.get(name)
        if factory is None:
            print(f"[gateway] 未知通道 {name}（跳过；可用: {sorted(_ADAPTER_FACTORIES)}）")
            continue
        try:
            adapter = factory(channel_config(name))
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
    # 聊天路由：msg/<channel>/<chat_id> → 哪个项目 + 哪个会话（channels/routes.yaml 可覆盖）
    project_id, session_id = _route_chat(msg)
    # 结构化说话人：适配层能归一就直接用；只给 user_id 的通道在边界层补成
    # 通用的 ``标识(标识)`` 形态，使任何通道的人都能进同一套人物聚合。
    # 主人归一（2026-11-11）：speaker/uid 命中主人在任何通道都落到同一张关系卡——
    # 必须在下面的 ``uid(uid)`` 兜底**之前**做，否则群名片各异/私聊裸 uid 会裂成多卡。
    from omni_core.local.identity import normalize_master
    _speaker = str(getattr(msg, "speaker", "") or "")
    _uid = str(getattr(msg, "user_id", "") or "")
    _speaker = normalize_master(_speaker, _uid)
    if not _speaker and _uid:
        _speaker = f"{_uid}({_uid})"
    # 能力门透传：外部可达通道默认 allow_tools=False → 该来源任务为纯对话（无任何工具）。
    # 网关只读通道自声明，不判断"这是谁发的"——判定权在通道适配器。
    dispatch.submit(msg.text, source=f"msg:{msg.channel}", reply=msg.reply,
                     speaker=_speaker,
                    project_id=project_id or None, session_id=session_id or None,
                    # 标题/历史用通道自声明的「人话」（text 里可能带场合自述前缀，
                    # 只该喂模型，不该进侧栏标题与会话气泡）
                    title=str(getattr(msg, "title", "") or ""),
                    display_text=str(getattr(msg, "display_text", "") or "") or msg.text,
                    # 回发模式由通道自声明（QQ=explicit 显式发送；飞书/企微=auto 直连）
                    reply_mode=str(getattr(msg, "reply_mode", "auto") or "auto"),
                    # 触发来源（named / wave）：named 轮才启用「该发没发」兜底代发
                    origin=str(getattr(msg, "origin", "named") or "named"),
                    tools_enabled=bool(getattr(msg, "allow_tools", False)),
                    # 多模态附件只作用于当轮（会话记录仍写文本投影，不落 base64）
                    attachments=list(getattr(msg, "attachments", None) or []),
                    # 入站是语音：仅 auto 通道（Web/飞书直连）据此自动语音回发；
                    # explicit 通道（QQ 等）不自动回发，发不发语音由模型调 msg_send_voice 决定。
                    voice_reply=bool(getattr(msg, "voice", False)))


def _route_chat(msg) -> tuple:
    """消息 → (project_id, session_id)（规则见 ``gateway.chat_routes``）。

    配了 project_id 的消息不再是「临时任务」（临时任务跑完即从任务索引删除，
    Web 任务列表里看不到），而是持久归属到该项目、可在界面点开看过程。
    """
    from .chat_routes import resolve

    return resolve(msg.channel, msg.chat_id)


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
    # 本地服务统一拉起（model_hub 清单）：网关独立运行时也要保证向量/语音可用。
    # 幂等：端口已占用即视为已有实例；失败不阻断网关启动。
    try:
        from model_hub.services import ServiceManager

        ServiceManager().ensure_started()
    except Exception as e:  # noqa: BLE001
        print(f"[gateway] 本地服务拉起失败（已忽略）: {e}")

    n = 0 if no_channels else start_channels()
    if not no_channels and n == 0:
        print("[gateway] ⚠ 无任何通道连接（检查 ~/.omniagent/channels/ 下的通道配置）")

    sched = None
    if not no_scheduler:
        sched = Scheduler(submit_via_web)
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
