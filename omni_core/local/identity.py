"""主人身份（跨表面归一 + Web 认领）。

「用户画像」时代（单用户工具）的 ``user`` 概念在多表面拟人部署里已解体：她是主体，
面对一群对话者。主人是其中最重要的一位（admin），且跨表面出现（Web 控制台恒为主人；
通道侧可有多个标识）。本模块负责两件事：

- **归一**：把「这条消息是主人说的」统一成规范 speaker ``主人(master)``——
  ``master`` 是**合成 key**（非真实标识），使所有表面聚到同一张关系卡
  ``relations/master.md``，换号/增删号都不动卡片；真实标识只活在配置名单里，
  不落 brain.db / 会话 jsonl（与 N-10 红线同向）。
- **名单**：配置 ``knowledge.relations.master_uids``（列表，由 Web「主人身份」
  认领界面读写，人不需要知道键名；本地配置盘，值含真实标识不进仓库）。
  通道消息按「名单内任一标识命中」归一；Web 控制台**恒为主人，零配置**。

分层（INV-4 同款）：边界层（网关 ``_on_message`` / Web 入口 ``chat_runtime``）调用
归一；关系卡维护按 ``MASTER_KEY`` 识别主人卡；内核不解析通道词。
"""
from __future__ import annotations

from typing import List

#: 主人卡的分组键（合成，非真实标识）：speaker 形态 ``<称呼>(master)``、
#: 卡片 ``relations/master.md``。恒定，与配置名单的内容无关。
MASTER_KEY = "master"

#: 称呼缺省值：主人卡不存在 / 卡里没有称呼时的回退。程序一致性要求：
#: 从不介绍 = 一切如旧（字节级同今日行为）。
_MASTER_NAME = "主人"

#: 称呼缓存（mtime 失效）：master_speaker 在消息入站路径上，每次 stat 一次文件可接受，
#: 重读/重解析只在卡被维护后发生。
_NAME_CACHE: dict = {"mtime": None, "name": None}


def master_display_name() -> str:
    """主人卡 META 里的称呼（``name:``）——**她的记忆是称呼的唯一宿主**，无配置。

    读 ``relations/master.md`` 的 META name（由关系卡维护写入：素材里有明确的
    自我介绍/称呼时，维护者更新它）；无卡 / 无 name / 解析失败 → 回退
    ``主人``（程序一致性：从不介绍 = 行为与今日完全一致）。
    """
    try:
        from omni_core.local import runtime_paths as rp

        path = rp.global_relations() / f"{MASTER_KEY}.md"
        if not path.exists():
            return _MASTER_NAME
        mtime = path.stat().st_mtime
        if _NAME_CACHE["mtime"] == mtime and _NAME_CACHE["name"] is not None:
            return _NAME_CACHE["name"]
        name = _MASTER_NAME
        meta = path.read_text(encoding="utf-8").split("-----META-END-----", 1)[0]
        for ln in meta.splitlines():
            s = ln.strip()
            if s.startswith("name:"):
                value = s[len("name:"):].strip()
                if value:
                    name = value[:40]
                break
        _NAME_CACHE.update({"mtime": mtime, "name": name})
        return name
    except Exception:
        return _MASTER_NAME


def master_uids() -> List[str]:
    """配置的主人标识名单（原样字符串列表；未配置返回空）。

    兼容字符串与列表两种写法；脏值（非字符串/空白）静默丢弃。
    """
    try:
        import config
        raw = config.get_config("knowledge.relations.master_uids", [])
    except Exception:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(x).strip() for x in raw if str(x or "").strip()]


def master_speaker() -> str:
    """主人的规范 speaker 形态 ``<称呼>(master)``——称呼来自主人卡（她的记忆）。

    Web 控制台恒为主人（零配置有身份）；通道侧靠 :func:`normalize_master` 按名单匹配。
    卡里还没有称呼时回退缺省 ``主人``；称呼随关系卡维护演化，历史行保留当时署名。
    """
    return f"{master_display_name()}({MASTER_KEY})"


def is_master_key(uid: str) -> bool:
    """uid 是否为主人卡的合成 key（关系卡维护用它识别主人分支）。"""
    return str(uid or "").strip() == MASTER_KEY


def speaker_uid(speaker: str) -> str:
    """从 ``name(uid)`` 形态的 speaker 里取 uid；不合规返回空串。"""
    value = str(speaker or "").strip()
    if "(" not in value or not value.endswith(")"):
        return ""
    return value.rpartition("(")[2].rstrip(")")


def normalize_master(speaker: str, uid: str = "") -> str:
    """边界层归一入口：speaker 或裸 uid 命中名单任一标识 → 规范形态；否则原样返回。

    网关在拼 ``name(uid)`` 兜底**之前**调用，保证主人在任何通道（显示名各异、
    只给裸标识）都落到同一张关系卡上。已是规范形态（uid == MASTER_KEY）原样通过。
    """
    value = str(speaker or "")
    if is_master_key(speaker_uid(value)):
        return value
    uids = master_uids()
    if not uids:
        return value
    raw_uid = str(uid or "").strip()
    if raw_uid and raw_uid in uids:
        return master_speaker()
    if speaker_uid(value) in uids:
        return master_speaker()
    return value


def resolve_peer_uids(peer: str) -> List[str]:
    """读侧归一：peer 检索键（裸 uid / ``name(uid)`` / 规范标签）→ 归一后的 uid 集合。

    认领归一（2026-11-12）：uid 命中认领名单（或给定 master key / 规范标签）→
    ``[MASTER_KEY]``，使**旧身份的检索对应到主人卡**（关系卡收编后旧 uid 不再有
    自己的卡，读侧必须跟随认领关系）；否则 ``[原 uid]``。空输入返回空。
    写侧无需此解析——网关归一在消息落库前完成，卡片素材源天然按 master 分组。
    """
    value = str(peer or "").strip()
    if not value:
        return []
    raw = speaker_uid(value) or value
    if is_master_key(raw) or value == master_speaker():
        return [MASTER_KEY]
    if raw in master_uids():
        return [MASTER_KEY]
    return [raw]
