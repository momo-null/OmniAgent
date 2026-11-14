"""网关主入口 —— 常驻进程：消息通道 + 定时任务 + 派发队列，一体启动。

用法（OmniAgent 仓库根）：
    python -m harness.channel_gateway                # 全量（声明了 events 的插件）
    python -m harness.channel_gateway --no-channels  # 只跑调度器（不连任何通道时先跑 cron）
    python -m harness.channel_gateway --no-scheduler # 只跑通道（检查通道连通性）

云端部署：systemd/任务计划程序托管本入口；本地调试直接跑。
配置：``~/.omniagent/plugins/<name>.yaml``（每插件一文件，阶段 4 起为主源；
过渡期回落 ``~/.omniagent/channels/<name>.yaml``）+ ``channels/routes.yaml``（聊天路由）。
不再读主配置的 channels:/chat_routes: 段，也不再有“通道”专属注册表——通道 = 声明了
events 面的插件。
"""
from __future__ import annotations

import os
import signal
import sys
import time
from typing import Any, Dict, List, Optional

from . import dispatch
from .events import registry as context
from .scheduler import Scheduler, submit_via_web

_SINGLETON_HANDLE: Any = None


def _acquire_singleton() -> bool:
    """网关单实例锁：后端、桌面壳、人手启动都可能并发拉起，只能有一个调度器/通道集合。

    Windows 用命名 mutex（进程死亡自动释放，无陈旧文件）；其它平台用文件锁。
    """
    global _SINGLETON_HANDLE
    if os.name == "nt":
        try:
            import ctypes

            handle = ctypes.windll.kernel32.CreateMutexW(None, False, "Local\\OmniAgentHarnessGateway")
            if not handle:
                return True  # 拿不到句柄不阻断（极少数受限环境）
            if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
                return False
            _SINGLETON_HANDLE = handle
            return True
        except Exception:  # noqa: BLE001 —— 锁失败不应让网关起不来
            return True
    try:
        import fcntl

        path = os.path.expanduser("~/.omniagent/runtime/harness.lock")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        f = open(path, "a+b")
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _SINGLETON_HANDLE = f
        return True
    except OSError:
        return False
    except Exception:  # noqa: BLE001
        return True



def channel_config(name: str) -> Dict[str, Any]:
    """插件配置（阶段 4 起单源）：``~/.omniagent/plugins/<name>.yaml`` 优先，
    缺失回落旧路径 ``~/.omniagent/channels/<name>.yaml`` 并打 warning（三个月后删回落）。

    与 ``plugin_config``（omni_core/tools/loader.py）同一布局：一插件一文件。通道连接
    信息（token/ws_url 等）是部署事实，不该和 brain/runtime 等 core 配置挤在主配置里。
    """
    plugin_file = None
    legacy_file = None
    try:
        from omni_core.local.runtime_paths import channel_config_file, plugin_config_file

        plugin_file = plugin_config_file(name)
        legacy_file = channel_config_file(name)
    except Exception:
        return {}
    path = plugin_file if plugin_file is not None and plugin_file.is_file() else legacy_file
    if path is None or not path.is_file():
        return {}
    if path is legacy_file and plugin_file is not None:
        print(f"[harness] 通道 {name} 仍在用旧配置 {path.name}（建议运行 scripts/migrate_plugin_configs.py）")
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[harness] 通道 {name} 配置读取失败（忽略）: {e}")
        return {}
    cfg = data if isinstance(data, dict) else {}
    # 能力层开关单源（§0.6-A3）：plugin.yaml > manifest.enabled。
    # 旧 ``channels/<name>.yaml`` 里的 enabled 阶段 4 起忽略（合并时会删掉该字段）。
    try:
        from .events import discover

        meta = discover.manifests().get(name) or {}
        if meta:
            cfg["enabled"] = bool(meta.get("enabled", cfg.get("enabled", True)))
    except Exception:
        pass
    return cfg


def _discovered() -> Dict[str, Any]:
    """manifest 清单（宿主的唯一插件认知来源）。"""
    from .events import discover

    return discover.manifests()


def _channel_names(discovered: Optional[Dict[str, Any]] = None) -> List[str]:
    """声明了 events 面的插件名（旧「通道」概念已并入插件：不再扫 channels/*.yaml）。"""
    table = discovered if discovered is not None else _discovered()
    return [name for name, meta in table.items() if meta.get("events")]


def _load_routes() -> Dict[str, Any]:
    """聊天路由表（实现已抽到 ``harness.chat_routes``，与通道层共用一份规则）。"""
    from .chat_routes import load_routes

    return load_routes()


def build_adapter(name: str):
    """按 manifest 构造事件源适配器（**不连接**），供非网关进程使用。

    背景：``msg_send_*`` 工具取通道走本进程的注册表（``harness.events.registry``），只有
    网关进程里有实例。在 Web 后端进程里让 agent 主动发消息时，注册表是空的 → 工具返回
    "无可用通道"，模型只能绕去 shell 调 OneBot HTTP（实测发生过：绕了 9 步）。

    这里只构造「会发消息」的句柄：QQ 这类 explicit 通道的发送走 HTTP，不依赖 WS 长连接，所以
    **不 connect** 也能正常发；返回 None 表示插件不存在 / 能力层关闭 / 构造失败
    （fail-closed，门限口径与网关一致：``plugin.yaml`` > ``manifest.enabled``）。
    """
    try:
        from .events import registry

        meta = _discovered().get(name) or {}
        if not meta or not meta.get("enabled", True) or not meta.get("events"):
            return None
        if not meta.get("configured"):
            return None
        return registry.build(name, channel_config(name),
                              (meta.get("events") or {}).get("factory"))
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

        mod = import_module(f"plugins.{name}.channel")
        factory = getattr(mod, "client", None)
        return factory(channel_config(name)) if callable(factory) else None
    except Exception:
        return None


def start_channels() -> int:
    """拉起所有声明了 events 的插件（能力层启用才连）；返回成功数。

    工厂不再由宿主硬编码：``registry.bootstrap`` 按 manifest 的
    ``events.factory`` 用 importlib 构造，失败记 ``failed`` 并跳过。
    """
    from .events import lifecycle, registry

    discovered = _discovered()
    names = _channel_names(discovered)
    built = registry.bootstrap(names, channel_config, discovered)
    started = 0
    for name in names:
        meta = discovered.get(name) or {}
        rec = built.get(name) or {}
        state = rec.get("state")
        if state == "disabled":
            lifecycle.mark_disabled(name, "plugin.yaml/manifest enabled=false", meta)
            print(f"[harness] 事件源 {name} 未启用（plugin.yaml/manifest enabled=false）")
            continue
        if not meta.get("configured"):
            lifecycle.mark_disabled(name, "未配置（plugins/<name>.yaml 缺失）", meta)
            print(f"[harness] 事件源 {name} 未配置（跳过）")
            continue
        if state == "failed":
            lifecycle.mark_failed(name, rec.get("error") or "", meta)
            print(f"[harness] 事件源 {name} 构造失败: {rec.get('error')}")
            continue
        adapter = rec.get("adapter")
        if adapter is None:
            lifecycle.mark_failed(name, "工厂返回 None", meta)
            print(f"[harness] 事件源 {name} 无适配器（跳过）")
            continue
        lifecycle.mark_starting(name, meta)
        try:
            adapter.connect(_on_message)
            context.register(adapter)
            lifecycle.bind(name, adapter, meta)
            started += 1
            print(f"[harness] 事件源 {name} 已连接（transport={(meta.get('events') or {}).get('transport')}）")
        except Exception as e:  # noqa: BLE001
            lifecycle.mark_failed(name, e, meta)
            print(f"[harness] 事件源 {name} 启动失败: {e}")
    lifecycle.write_snapshot()
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
    print(f"[harness] 收到消息 <{msg.channel}:{msg.chat_id}> {preview}")
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
                    # 回发模式由通道自声明（QQ=explicit 显式发送；IM 直连=auto）
                    reply_mode=str(getattr(msg, "reply_mode", "auto") or "auto"),
                    # 触发来源（named / wave）：named 轮才启用「该发没发」兜底代发
                    origin=str(getattr(msg, "origin", "named") or "named"),
                    tools_enabled=bool(getattr(msg, "allow_tools", False)),
                    # 多模态附件只作用于当轮（会话记录仍写文本投影，不落 base64）
                    attachments=list(getattr(msg, "attachments", None) or []),
                    # 入站是语音：仅 auto 通道（Web/IM 直连）据此自动语音回发；
                    # explicit 通道（QQ 等）不自动回发，发不发语音由模型调 msg_send_voice 决定。
                    voice_reply=bool(getattr(msg, "voice", False)))


def _route_chat(msg) -> tuple:
    """消息 → (project_id, session_id)（规则见 ``harness.chat_routes``）。

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
        print(f"[harness] 插件预装载失败（语音等能力可能不可用）: {e}")


def main() -> int:
    if not _acquire_singleton():
        print("[harness] 已有网关实例在运行，本进程退出")
        return 0
    no_channels = "--no-channels" in sys.argv
    no_scheduler = "--no-scheduler" in sys.argv
    print("[harness] 启动（Ctrl+C 退出）")
    dispatch.ensure_worker()
    _ensure_plugins()
    # 本地服务统一拉起（model_hub 清单）：网关独立运行时也要保证向量/语音可用。
    # 幂等：端口已占用即视为已有实例；失败不阻断网关启动。
    try:
        from model_hub.services import ServiceManager

        ServiceManager().ensure_started()
    except Exception as e:  # noqa: BLE001
        print(f"[harness] 本地服务拉起失败（已忽略）: {e}")

    # 插件自带服务（manifest lifecycle.spawn）与事件源体检：同一执行侧（ServiceManager）
    from .events import lifecycle

    try:
        for r in lifecycle.ensure_plugin_services():
            print(f"[harness] 插件服务 {r.get('name')}: {r.get('result')}")
    except Exception as e:  # noqa: BLE001
        print(f"[harness] 插件服务拉起失败（已忽略）: {e}")
    lifecycle.start_health_loop()

    n = 0 if no_channels else start_channels()
    if no_channels:
        # 不连通道也要落状态：否则 Web 端读不到快照，会把「网关在跑、只是没连通道」
        # 误报成「网关未运行」，插件页也跟着被挡住。
        from .events import discover

        for name in discover.events_units():
            meta = discover.manifests().get(name) or {}
            lifecycle.mark_disabled(name, "以 --no-channels 启动（未连接事件源）", meta)
        lifecycle.write_snapshot()
    if not no_channels and n == 0:
        print("[harness] ⚠ 无任何通道连接（检查 ~/.omniagent/channels/ 下的通道配置）")

    sched = None
    if not no_scheduler:
        sched = Scheduler(submit_via_web)
        sched.start()
        print("[harness] 调度器已启动（jobs 热加载）")

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
    try:
        from .events import lifecycle as _lifecycle

        _lifecycle.stop_health_loop()
    except Exception:
        pass
    print("[harness] 退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
