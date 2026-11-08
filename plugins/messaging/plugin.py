"""通用消息发送插件（接入设计 v1.1 §2 工具侧）。

- 工具按**行为**命名（msg_send_text / msg_send_voice / msg_send_card），通道只是参数——
  接十个通道工具清单不变（通用化红线）。用什么方式说（文字/语音）由模型自己选，
  工具不按通道状态偷偷改写发送形式。
- 通道实现来自 gateway/channels 注册表（与网关同进程闭环）：
  · channel 缺省 = 当前任务来源通道（dispatch 注入的线程上下文）
  · target  缺省 = 来源会话 / 通道默认会话
- 网关未运行时（如本地 CLI 跑 run_agent_autonomous），工具返回明确的
  {"ok": false, "error": "无可用通道"}——不猜、不静默。
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from omni_core.tools.base import function_tool
from utils import get_logger

logger = get_logger("messaging.sing")

#: 本插件自有配置（~/.omniagent/plugins/messaging.yaml），网关未跑时的缺省通道
_CFG: Dict[str, Any] = {"default_channel": ""}


def configure(cfg: Optional[dict]) -> None:
    cfg = cfg or {}
    for key in ("default_channel",):
        if key in cfg:
            _CFG[key] = cfg[key]


def _resolve(channel: str, target: str):
    """解析 (adapter, chat_id)。返回 (adapter, chat_id, err)。

    **外部可达通道下强制锁死目标**：`context.get_source_external()` 为真（QQ 群/
    陌生人私聊这类他人能发消息进来的入口）时，忽略模型给的 channel/target，只发回
    来源会话。否则群里的人可以让它 `channel="feishu"` 或 `target="private:别人"`
    —— 不碰任何高危工具也能把消息从别的通道发出去，绕过工具白名单做跳板。
    """
    from gateway.channels import context

    cur = context.get_current_reply()
    if context.get_source_external():
        if cur is None:
            return None, None, "外部来源无回发句柄，已拒绝发送（防跨会话外发）"
        if not (getattr(cur, "channel_name", "") or ""):
            # 没通道名就会回退到插件配置的 default_channel → 等于跨通道发，直接拒
            return None, None, "外部来源缺少通道标识，已拒绝发送（防串道）"
        channel = ""
        target = getattr(cur, "chat_id", "") or ""
    # 通道名：显式 > 当前来源 > 插件配置缺省
    ch_name = channel or ""
    if not ch_name and cur is not None:
        ch_name = getattr(cur, "channel_name", "") or ""
    if not ch_name:
        ch_name = _CFG.get("default_channel", "")
    adapter = context.get_adapter(ch_name) if ch_name else None
    if adapter is None and ch_name:
        # 非网关进程（如 Web 后端）里没有注册表实例 → 按 config.channels.<name> 临时
        # 构造一个「只用于发送」的句柄（不建立长连接）。QQ 的发送走 OneBot HTTP，
        # 因此不依赖网关进程；这样 msg_send_text 在任何进程都能一次成功，模型不必
        # 绕去 shell 手撸 curl（实测踩过：绕了 9 步）。
        try:
            from gateway.channel_gateway import build_adapter

            adapter = build_adapter(ch_name)
        except Exception:
            adapter = None
    if adapter is None:
        return None, None, f"无可用通道（channel={ch_name or '未指定'}；网关未运行或通道未启用）"

    # 目标：显式 > 当前来源会话
    chat_id = target or ""
    if not chat_id and cur is not None:
        chat_id = getattr(cur, "chat_id", "") or ""
    if not chat_id:
        chat_id = getattr(adapter, "default_chat_id", "") or ""
    if not chat_id:
        return adapter, None, "未指定 target 且无来源会话/默认会话"
    return adapter, chat_id, ""


def _external_note(result: Dict[str, Any]) -> Dict[str, Any]:
    """外部来源下给发送结果补一句说明：channel/target 被忽略、只发回了本会话。

    让模型知道「我指定别的会话没生效」，避免它以为发出去了而实际发在来源群。
    """
    try:
        from gateway.channels import context as _ctx

        if isinstance(result, dict) and result.get("ok") and _ctx.get_source_external():
            extra = "外部来源：只能发回本会话，channel/target 参数已被忽略"
            result["note"] = f"{result['note']}；{extra}" if result.get("note") else extra
    except Exception:  # 非网关进程 / 极端导入失败：不影响发送结果
        pass
    return result


def _outbound_flat(adapter: Any, chat_id: str) -> Dict[str, Any]:
    """按 (通道, 会话) 解析出站守则（§8 统一契约：归一逻辑只在 gateway.outbound）。

    发送时现解析而非线程上下文：Web 并发不串轮、cron 无来源也能命中
    （明确 target 时按 target 解析，与外部来源锁死回来源会话的语义一致）。
    """
    try:
        from gateway.outbound import resolve as _ob_resolve

        return _ob_resolve(str(getattr(adapter, "name", "") or ""), chat_id or "")
    except Exception:
        return {}


def _outbound_chunks(adapter: Any, chat_id: str, text: str) -> Tuple[List[str], str]:
    """机械闸：超限拆条或拒发（fail-open：守则解析异常不拦发送，同 _external_note 宽容）。"""
    try:
        from gateway.outbound import enforce as _ob_enforce

        return _ob_enforce(_outbound_flat(adapter, chat_id), text)
    except Exception:
        return [text], ""


def _do_send_text(reply: Any, chunks: List[str], _ctx: Any) -> Dict[str, Any]:
    """按守则拆好的 chunks 逐条发送并记账；返回聚合结果（未过 _external_note）。

    记账同既有口径：duplicate 跳过不算真发送；中间条失败即止并报第几条，
    已发出的条目不撤回（记账已留痕）。
    """
    result: Dict[str, Any] = {}
    for i, chunk in enumerate(chunks):
        result = reply.send_text(chunk)
        if (_ctx is not None and isinstance(result, dict) and result.get("ok")
                and not result.get("duplicate")):
            _ctx.note_sent(chunk)
        if not (isinstance(result, dict) and result.get("ok")):
            if len(chunks) > 1:
                err = result.get("error", "") if isinstance(result, dict) else repr(result)
                return {**(result if isinstance(result, dict) else {"ok": False}),
                        "error": f"拆条发送在第 {i + 1}/{len(chunks)} 条失败: {err}"}
            return result if isinstance(result, dict) else {"ok": False, "error": repr(result)}
    if len(chunks) > 1:
        result = {**(result if isinstance(result, dict) else {"ok": True}),
                  "note": f"按出站守则拆为 {len(chunks)} 条发送"}
    return result


@function_tool(
    description="明确本轮不向任何会话发出内容（保持沉默），把「发不发」的判断收口成一个"
                "显式动作。调用后对方收不到任何消息；只写最终回答既不算发送，也不算"
                "明确的沉默决定。不需要判断场合、不需要参数，决定了不发就调用它。",
    unit="messaging",
)
def msg_stay_silent(reason: str = "") -> Dict[str, Any]:
    """显式沉默：不投递、不改通道状态，只留下一条可被轨迹记录的结果。

    Args:
        reason: 可选的沉默理由（只进本轮轨迹，不外发）。
    """
    return {"ok": True, "stayed_silent": True,
            "note": "本轮保持沉默：未向任何会话发出内容。",
            "reason": str(reason or "").strip()}


@function_tool(
    description="发送纯文本消息到消息通道。channel=通道名（如 napcat/feishu）；"
                "target=会话标识，按通道自身的格式：napcat 为 private:<QQ号> 或 group:<群号>，"
                "feishu 为 chat_id。不填则发回本任务的来源会话。发送失败会返回明确错误。",
    unit="messaging",
)
def msg_send_text(text: str, channel: str = "", target: str = "") -> Dict[str, Any]:
    """发纯文本。

    Args:
        text: 文本内容。
        channel: 通道名（feishu/wecom…，缺省=来源通道）。
        target: 会话 ID（缺省=来源会话/通道默认会话）。
    """
    adapter, chat_id, err = _resolve(channel, target)
    if err:
        return {"ok": False, "error": err}
    try:
        from gateway.channels import context as _ctx
    except Exception:  # 非网关进程/极端导入失败：不影响发送本身
        _ctx = None
    try:
        reply = adapter.reply_to(chat_id)
        # §8 出站守则机械闸（唯一执行点）：超限拆条/拒发；文风靠配置声明，闸只管长度。
        # 只发文本。要发语音走 msg_send_voice——发送方式由模型自己选，
        # 不在工具里按「入站是不是语音」偷偷改写（否则模型以为发文字、对方却收到语音）。
        chunks, gate_err = _outbound_chunks(adapter, chat_id, text)
        if gate_err:
            return {"ok": False, "error": gate_err}
        result = _do_send_text(reply, chunks, _ctx)
        return _external_note(result)
    except Exception as e:
        return {"ok": False, "error": f"发送失败: {e}"}


@function_tool(
    description="发语音消息：把文字朗读成语音发给对方（**说话**，不是唱歌、没有旋律/伴奏；"
                "只适合口语化短句，不要塞链接/代码/长列表）。对方发来语音、或闲聊短句时用它更自然。"
                "通道不支持语音会返回明确错误，届时改用 msg_send_text。",
    unit="messaging",
)
def msg_send_voice(text: str, channel: str = "", target: str = "") -> Dict[str, Any]:
    """发语音（TTS 朗读）。

    Args:
        text: 要朗读的内容（口语化、简短）。
        channel: 通道名（缺省=来源通道）。
        target: 会话 ID（缺省=来源会话/通道默认会话）。
    """
    adapter, chat_id, err = _resolve(channel, target)
    if err:
        return {"ok": False, "error": err}
    # §8 出站守则：语音同样受上限约束——只拒不拆（拆条朗读很怪，模型该自己精简
    # 或改用文字拆几条短发）。
    try:
        from gateway.outbound import voice_error as _ob_voice_err

        _verr = _ob_voice_err(_outbound_flat(adapter, chat_id), text)
        if _verr:
            return {"ok": False, "error": _verr}
    except Exception:
        pass
    try:
        reply = adapter.reply_to(chat_id)
        send_voice = getattr(reply, "send_voice", None)
        if not callable(send_voice):
            return {"ok": False,
                    "error": f"通道 {getattr(adapter, 'name', '') or '该'} 不支持语音，请改用 msg_send_text"}
        result = send_voice(text)
        if not isinstance(result, dict):
            return {"ok": False, "error": f"语音发送返回异常: {result!r}"}
        if result.get("ok") and not result.get("duplicate"):
            try:
                from gateway.channels import context as _ctx

                _ctx.note_sent(text)  # 记账：本轮真的发出去了（去重跳过不算）
            except Exception:  # 记账失败不影响已发出的消息
                pass
        return _external_note(result)
    except Exception as e:
        return {"ok": False, "error": f"语音发送失败: {e}"}


@function_tool(
    description="发送一张本地图片到消息通道（当前主要用于B站带图动态：先准备本地图片文件，"
                "再调本工具发出，可附一句文字说明。配额与出站守则同样生效）。",
    unit="messaging",
)
def msg_send_image(image_path: str, text: str = "", channel: str = "", target: str = "") -> Dict[str, Any]:
    """发本地图片。

    Args:
        image_path: 本地图片文件路径（先由文件工具生成/合成好）。
        text: 附带的文字说明（可选）。
        channel: 通道名（缺省=来源通道）。
        target: 会话 ID（缺省=来源会话/通道默认会话）。
    """
    import os

    path = str(image_path or "").strip()
    if not path or not os.path.isfile(path):
        return {"ok": False, "error": f"图片文件不存在: {path}"}
    adapter, chat_id, err = _resolve(channel, target)
    if err:
        return {"ok": False, "error": err}
    try:
        reply = adapter.reply_to(chat_id)
        send_image = getattr(reply, "send_image", None)
        if not callable(send_image):
            return {"ok": False,
                    "error": f"通道 {getattr(adapter, 'name', '') or '该'} 不支持图片发送"}
        # 出站守则：文字说明过长度闸（图片动态按动态配额计，闸在通道侧）
        chunks, gate_err = _outbound_chunks(adapter, chat_id, text or "")
        if gate_err:
            return {"ok": False, "error": gate_err}
        result = send_image(path, chunks[0] if chunks else "")
        if not isinstance(result, dict):
            return {"ok": False, "error": f"图片发送返回异常: {result!r}"}
        if result.get("ok") and not result.get("duplicate") and not result.get("queued"):
            try:
                from gateway.channels import context as _ctx

                _ctx.note_sent(f"[图片:{os.path.basename(path)}] {chunks[0] if chunks else ''}".strip())
            except Exception:
                pass
        return _external_note(result)
    except Exception as e:
        return {"ok": False, "error": f"图片发送失败: {e}"}


@function_tool(
    description="发送一张表情包（通道贴纸库里的图片表情，活跃气氛的点缀）。"
                "name=贴纸名（贴纸库文件名，不带扩展名也认）；不填 name 则随机挑一张。"
                "偶尔点缀用，不要每条消息都发。通道未启用表情或贴纸库为空会返回明确错误，"
                "届时改用 msg_send_text。",
    unit="messaging",
)
def msg_send_sticker(name: str = "", channel: str = "", target: str = "") -> Dict[str, Any]:
    """发表情包（图片贴纸）。

    Args:
        name: 贴纸名（贴纸库文件名；不填=随机挑一张）。
        channel: 通道名（缺省=来源通道）。
        target: 会话 ID（缺省=来源会话/通道默认会话）。
    """
    adapter, chat_id, err = _resolve(channel, target)
    if err:
        return {"ok": False, "error": err}
    try:
        reply = adapter.reply_to(chat_id)
        send_sticker = getattr(reply, "send_sticker", None)
        if not callable(send_sticker):
            return {"ok": False,
                    "error": f"通道 {getattr(adapter, 'name', '') or '该'} 不支持表情包"}
        result = send_sticker(str(name or "").strip())
        if not isinstance(result, dict):
            return {"ok": False, "error": f"表情发送返回异常: {result!r}"}
        if result.get("ok") and not result.get("duplicate"):
            try:
                from gateway.channels import context as _ctx

                _ctx.note_sent(f"[表情:{result.get('sticker') or name}]")
            except Exception:  # 记账失败不影响已发出的消息
                pass
        return _external_note(result)
    except Exception as e:
        return {"ok": False, "error": f"表情发送失败: {e}"}


def _sing_manifest() -> Dict[str, Any]:
    """曲库 manifest（~/.omniagent/models/sing/songs/manifest.json）；缺失=空曲库。"""
    import json
    import os

    path = os.path.join(os.path.expanduser("~"), ".omniagent", "models", "sing",
                        "songs", "manifest.json")
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf8") as f:
            data = json.load(f)
        return data.get("songs") if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 —— manifest 坏了不炸工具，返回空让上层说"不会唱"
        return {}


def _sing_norm(s: Any) -> str:
    """点歌话术归一：去空白与常见标点、小写（群友说"陈奕迅 的《孤勇者》!"要能认得出）。"""
    return re.sub(r"[\s、,，。.!！?？~～·\"'“”《》〈〉()（）\[\]{}]+", "", str(s or "")).lower()


def _sing_lookup(song_id: str) -> Dict[str, Any]:
    """按 id / 别名 / 歌名包含匹配曲库条目；命中条目附 ``_id``=规范曲库键；找不到返回 {}。

    两遍匹配：**正向**（点的话就是歌名/别名，或其子串）与**反向**（话里嵌着已知歌名，
    如"唱一首陈奕迅的孤勇者呗"）。反向取最长命中，避免短别名乱撞。漏了这一层会
    直接掉进在线获取——同一首歌被下两遍、群里等两分钟，比唱错更难看。
    """
    songs = _sing_manifest()
    sid = _sing_norm(song_id)
    if not sid:
        return {}
    if sid in songs:
        return {**songs[sid], "_id": sid}
    best, best_len, best_id = None, 0, ""
    for key, item in songs.items():
        title_n = _sing_norm(item.get("title"))
        needles = [title_n]
        needles += [a for a in (_sing_norm(x) for x in (item.get("aliases") or [])) if a]
        needles.append(_sing_norm(key))
        needles = [n for n in needles if n]
        # 正向：点的话 == 歌名/别名，或是它们的子串（"青花"→青花瓷）
        if sid in needles or any(len(sid) >= 2 and sid in n for n in needles):
            return {**item, "_id": key}
        # 反向：已知歌名/别名嵌在点歌话里
        for needle in needles:
            if len(needle) >= 2 and needle in sid and len(needle) > best_len:
                best, best_len, best_id = item, len(needle), key
    if best is not None:
        return {**best, "_id": best_id}
    return {}


#: 点歌进行中登记表（plan §5 并发护栏）：**同一首在练/在转时二次点歌，直接回"在唱了"，
#: 不排队不重算**——转换 ~10s、在线全链 1–2min，重复跑既抢 GPU 又会在群里发两遍。
#: value=占位时间戳；超过 TTL 视为陈旧自动回收（守护线程被硬杀 / 起线程失败时不留死锁）。
_SING_BUSY: Dict[str, float] = {}
_SING_BUSY_LOCK = threading.Lock()
_SING_BUSY_TTL = 900.0


def _sing_busy_claim(key: str) -> bool:
    """占位成功=True（可以开工）；已在进行中=False（调用方据此回"在唱了"）。"""
    now = time.time()
    with _SING_BUSY_LOCK:
        for k, t in list(_SING_BUSY.items()):
            if now - t > _SING_BUSY_TTL:
                _SING_BUSY.pop(k, None)
        if key in _SING_BUSY:
            return False
        _SING_BUSY[key] = now
        return True


def _sing_busy_release(key: str) -> None:
    with _SING_BUSY_LOCK:
        _SING_BUSY.pop(key, None)


def _sing_busy_key(raw: str, prefix: str) -> str:
    """归一化点歌键：``song:<曲库id>`` / ``acq:<去空格小写歌名>``。"""
    return f"{prefix}:" + re.sub(r"\s+", "", str(raw or "")).strip().lower()


@function_tool(
    description="唱歌：从曲库点一首歌，把副歌清唱成语音发出去（**清唱、无伴奏**）。song_id 支持"
                "歌名/别名（如 '青花瓷' 或 'qinghuaci'）。曲库内转换约几秒到十几秒；曲库外的歌"
                "在在线曲库开启时会先应下（'我练练'），后台获取约 1–2 分钟后自动发——两者都属正常，"
                "曲库外且在线曲库未开启时才回复'这首我还不会唱'。返回里 ``inflight``=true 表示这首"
                "已经在唱/已经在练了，回一句'在唱了，等一下'就行，**不要再调一次**（重算没用）。",
    unit="messaging",
)
def msg_sing(song_id: str, channel: str = "", target: str = "") -> Dict[str, Any]:
    """点歌清唱：曲库内副歌 → SVC 转换 → 语音发送；曲库外走在线获取（若开启）。

    Args:
        song_id: 歌名 / 别名 / 曲库 id（见曲库 manifest）。
        channel: 通道名（缺省=来源通道）。
        target: 会话 ID（缺省=来源会话）。
    """
    adapter, chat_id, err = _resolve(channel, target)
    if err:
        return {"ok": False, "error": err}
    item = _sing_lookup(song_id)
    if not item:
        # L1 miss → 在线曲库（step 4，拍板 §10.2）：总闸开 + 有源插件才应下；
        # 后台线程全链获取→转换→发送，**失败也要补投说话**（_async_sing）。
        try:
            from plugins.voice import song_source as _ss
        except Exception:  # noqa: BLE001
            _ss = None
        if _ss is not None and _ss.online_enabled() and _ss.load_sources():
            busy = _sing_busy_key(song_id, "acq")
            if not _sing_busy_claim(busy):
                # 同一首已在后台练：不重开任务、不排队（plan §5 并发护栏）
                return {"ok": True, "inflight": True,
                        "note": "回复群友：这首我正在练呢，练好了第一个发你"}
            try:
                threading.Thread(target=_async_sing, args=(busy, song_id, adapter, chat_id),
                                 name="sing-acquire", daemon=True).start()
            except Exception as e:  # noqa: BLE001 —— 起线程失败不能把占位永久留着
                _sing_busy_release(busy)
                return {"ok": False, "error": f"后台获取启动失败: {e}",
                        "note": "回复群友：这首我还不会唱，换一首试试"}
            return {"ok": True, "pending": True,
                    "note": "已应下这首（后台搜歌/分离/转换约 1–2 分钟，完成后自动发语音）；"
                            "回复群友：好嘞我练练，唱好了发你"}
        return {"ok": False, "error": "曲库里没有这首歌",
                "note": "回复群友：这首我还不会唱，换一首试试"}
    chorus = str(item.get("chorus") or "")
    if not chorus:
        return {"ok": False, "error": "曲库条目缺 chorus 段"}
    try:
        from omni_core.tools.capability import get_capability
    except Exception:
        get_capability = None
    cap = get_capability("voice_sing") if get_capability else None
    if cap is None:
        return {"ok": False, "error": "voice 插件未启用（无 voice_sing 能力）"}
    # 库内歌同样要 in-flight 去重：转换 ~10s 且 worker 串行，二次点歌若放行，
    # 第二次只是白抢 GPU（发送侧 dup 拦得住重复刷屏，拦不住重算）。
    busy = _sing_busy_key(item.get("_id") or song_id, "song")
    if not _sing_busy_claim(busy):
        return {"ok": True, "inflight": True,
                "note": "回复群友：这首正在唱，稍等一下下就好"}
    try:
        try:
            meta = cap(chorus, key=int(item.get("key") or 0),
                       seed=int(item.get("seed") or 1234),
                       index_rate=float(item.get("index_rate") or 0.66))
        except Exception as e:
            return {"ok": False, "error": f"唱歌转换失败: {e}",
                    "note": "回复群友：这首我唱砸了，换一首试试"}
        try:
            reply = adapter.reply_to(chat_id)
            send_sing = getattr(reply, "send_sing", None)
            if not callable(send_sing):
                return {"ok": False,
                        "error": f"通道 {getattr(adapter, 'name', '') or '该'} 不支持语音，请改用 msg_send_text"}
            title = str(item.get("title") or song_id)
            result = send_sing(meta["out"], dedup_text=busy)
            if not isinstance(result, dict):
                return {"ok": False, "error": f"清唱发送返回异常: {result!r}"}
            if result.get("ok"):
                result.setdefault("song", title)
                result.setdefault("seconds", meta.get("seconds"))
            return _external_note(result)
        except Exception as e:
            return {"ok": False, "error": f"清唱发送失败: {e}"}
    finally:
        _sing_busy_release(busy)


def _async_sing(busy_key: str, query: str, adapter, chat_id: str) -> None:
    """后台线程：库外歌 获取→转换→发送。**失败也要补投说话**（拍板 §10.2，不许静默吞）。

    守护线程随网关退出而消亡：重启丢任务 = 群友等到的是沉默而非承诺过的歌——
    已知的 MVP 缺口（真正的 run 级补投要做后端 auto-chain 接线，plan §5）。
    ``busy_key`` 由调用方占位，这里负责释放。
    """
    try:
        from plugins.voice import sing_acquire as _acq
    except Exception as e:  # noqa: BLE001 —— 走到这里 voice 包必然可导入（msg_sing 探测过）
        logger.warning("在线曲库模块导入失败（query=%s）: %s", query, e)
        _sing_busy_release(busy_key)
        return
    try:
        entry = _acq.acquire(query)
        from omni_core.tools.capability import get_capability

        cap = get_capability("voice_sing")
        if cap is None:
            raise RuntimeError("voice_sing 能力缺失")
        meta = cap(entry["chorus"], key=int(entry.get("key") or 0),
                   seed=int(entry.get("seed") or 1234),
                   index_rate=float(entry.get("index_rate") or 0.66))
        reply = adapter.reply_to(chat_id)
        send_sing = getattr(reply, "send_sing", None)
        if not callable(send_sing):
            raise RuntimeError("通道不支持语音发送")
        # 发送去重键与库内路径同namespace：入库后用 ``song:<song_id>``，
        # 这样"在线练成 → 之后库里再点同一首"能正确命中刷屏保护。
        r = send_sing(meta["out"], dedup_text="song:" + str(entry["song_id"]).lower())
        if not (isinstance(r, dict) and r.get("ok")):
            raise RuntimeError(str((r or {}).get("error") or "发送失败"))
        logger.info("在线曲库完成并发送：%s（%s）", entry["song_id"], entry["title"])
    except _acq.InFlightError:
        # 极窄竞态：本层占位之后、acquire 占位之前被同 query 的其它请求抢走。
        # 那一次已经口播过"我练练"，歌也照样会发出来——这里再说话就是重复刷屏。
        logger.info("同歌获取已被占下，静默退出（query=%s）", query)
    except Exception as e:  # noqa: BLE001 —— 失败也要说话
        logger.warning("在线曲库失败（query=%s）: %s: %s", query, type(e).__name__, e)
        try:
            reply = adapter.reply_to(chat_id)
            reply.send_text("这首练劈了……换一首我试试？")
        except Exception:  # noqa: BLE001 —— 连失败播报都发不出去就只留日志
            logger.warning("在线曲库失败播报也发送失败（chat=%s）", chat_id)
    finally:
        _sing_busy_release(busy_key)


@function_tool(
    description="看某个群的消息：不带 keyword=最近消息（实时，不含你自己发的）——被 @ 但接不上话、"
                "想确认大家在聊什么时用；带 keyword=按关键词翻这个群的旧记录（跨天，含你自己说的话）。"
                "只有支持群历史的通道可用（QQ 支持）。",
    unit="messaging",
)
def msg_group_history(group_id: str = "", limit: int = 20, channel: str = "",
                      keyword: str = "") -> Dict[str, Any]:
    """群最近消息，或按关键词翻该群的历史记录。

    Args:
        group_id: 群号（缺省=当前所在群）。
        limit: 最多几条（1-50，缺省 20）。
        channel: 通道名（缺省=来源通道）。
        keyword: 关键词（非空则改为检索该群的旧消息原文，用 2-4 字最有效）。
    """
    adapter, chat_id, err = _resolve(channel, "")
    if err:
        return {"ok": False, "error": err}
    from gateway.channels import context as _ctx

    gid = str(group_id or "").strip()
    if _ctx.get_source_external():
        # 外部来源只准看当前群：不能让群里的人借它读机器人所在的其他群
        kind, _, ident = (chat_id or "").partition(":")
        if kind != "group":
            return {"ok": False, "error": "外部来源只允许查看当前群的消息"}
        if gid and gid != ident:
            return {"ok": False, "error": "外部来源只能查看当前群的历史（其他群号已拒绝）"}
        gid = ident
    elif not gid:
        kind, _, ident = (chat_id or "").partition(":")
        if kind != "group":
            return {"ok": False, "error": "只有群聊有群历史；当前不在群里，需显式传 group_id"}
        gid = ident
    kw = str(keyword or "").strip()
    if kw:
        # P2（2026-11-02）：关键词路径转查 L0 逐字索引（brain.db.l0_messages）——
        # OneBot 的实时历史只有最近几十条，翻旧事必须查本地表；查不到就明确说没有。
        try:
            from omni_core.tools import memory_tool as _mtq
            blocked = _mtq._quota_block("msg_group_history")
            if blocked:
                return blocked
        except Exception:
            pass   # 配额面缺失不影响查询本身
        try:
            from omni_core import memory_brain
        except Exception:
            memory_brain = None
        sid = ""
        try:
            from gateway.chat_routes import resolve as _resolve_route
            ch_name = str(getattr(adapter, "name", "") or channel or "")
            sid = _resolve_route(ch_name, f"group:{gid}")[1]
        except Exception:
            sid = ""
        rows = (memory_brain.search_l0(kw, context_key=f"group:{gid}", session_id=sid,
                                       limit=max(1, min(20, int(limit or 8))))
                if memory_brain is not None else [])
        try:
            from omni_core.tools import memory_tool as _mt
            _mt.note_self_fetch(len(rows), str(rows[0]["id"]) if rows else "",
                                tool="msg_group_history")
        except Exception:
            pass   # 记账失败不影响本次查询
        return {"ok": True, "group_id": gid, "keyword": kw, "count": len(rows),
                "note": "按时间正序检索到的群内旧消息原文（本地索引，非实时接口）"
                        if rows else "这个群里没有匹配「%s」的旧消息" % kw,
                "messages": [f"[{str(r['when'])[:16].replace('T', ' ')}] "
                             f"{r['who'] or ('我' if r['role'] == 'assistant' else '对方')}："
                             f"{r['text']}" for r in rows]}

    fn = getattr(adapter, "group_history", None)
    if not callable(fn):
        return {"ok": False, "error": f"通道 {getattr(adapter, 'name', '') or '该'} 不提供群历史"}
    try:
        rows = fn(gid, limit)
    except Exception as e:
        return {"ok": False, "error": f"取群历史失败: {e}"}
    rows = list(rows or [])
    return {"ok": True, "group_id": gid, "count": len(rows),
            "note": "按时间正序，已跳过你自己发的消息", "messages": rows}


@function_tool(
    description="看群里某条消息的图片：传 msg_group_history 结果或群上下文里 [图片] 消息的 "
                "message_id，图片会作为当轮附件注入，用你自己的视觉直接看（不是文字描述）。"
                "仅 QQ 群图片可用；消息里没有图会返回明确错误。",
    unit="messaging",
)
def msg_group_image(message_id: str, group_id: str = "", channel: str = "") -> Dict[str, Any]:
    """看群消息里的图。

    Args:
        message_id: 图片所在消息的 ID（群历史/上下文里每条都带）。
        group_id: 群号（缺省=当前所在群）。
        channel: 通道名（缺省=来源通道）。
    """
    adapter, chat_id, err = _resolve(channel, "")
    if err:
        return {"ok": False, "error": err}
    from gateway.channels import context as _ctx

    gid = str(group_id or "").strip()
    if _ctx.get_source_external():
        # 外部来源只准看当前群：防群里的人借它读其他群的图（与 msg_group_history 同界）
        kind, _, ident = (chat_id or "").partition(":")
        if kind != "group":
            return {"ok": False, "error": "外部来源只允许查看当前群的图片"}
        if gid and gid != ident:
            return {"ok": False, "error": "外部来源只能查看当前群（其他群号已拒绝）"}
        gid = ident
    elif not gid:
        kind, _, ident = (chat_id or "").partition(":")
        if kind != "group":
            return {"ok": False, "error": "只有群聊能按会话取图；当前不在群里，需显式传 group_id"}
        gid = ident
    fn = getattr(adapter, "group_image_data_urls", None)
    if not callable(fn):
        return {"ok": False, "error": f"通道 {getattr(adapter, 'name', '') or '该'} 不支持取群图片"}
    try:
        urls, gerr = fn(gid, str(message_id or "").strip())
    except Exception as e:
        return {"ok": False, "error": f"取图失败: {e}"}
    if gerr:
        return {"ok": False, "error": gerr}
    return {"ok": True, "group_id": gid, "message_id": str(message_id),
            "count": len(urls),
            "note": "图片已作为当轮附件返回（请求边界可见，一次性，不进会话历史）",
            "attachments": [{"type": "image", "data_url": u} for u in urls]}


@function_tool(
    description="发送原生卡片消息（交互卡片）。payload 为通道原生卡片结构（飞书=新版卡片 schema 直接作为 content）。",
    unit="messaging",
)
def msg_send_card(payload: Dict[str, Any], channel: str = "", target: str = "") -> Dict[str, Any]:
    """发原生卡片。

    Args:
        payload: 通道原生卡片结构（飞书新版卡片 JSON 直接放这里，勿包 {"card": ...} 外层——230099 坑）。
        channel: 通道名（缺省=来源通道）。
        target: 会话 ID（缺省=来源会话/通道默认会话）。
    """
    adapter, chat_id, err = _resolve(channel, target)
    if err:
        return {"ok": False, "error": err}
    try:
        reply = adapter.reply_to(chat_id)
        return _external_note(reply.send_card(payload))
    except Exception as e:
        return {"ok": False, "error": f"发送失败: {e}"}


# ── 消息往来查询（跨会话） ────────────────────────────────────────────────
# 动机：一个会话里发生的事，换个会话（Web ↔ QQ）她就不知道了，只能 shell 翻文件解析
# （实测绕了 7~9 步）。这里给她一条直路：谁找过、什么时候、她当时怎么回的。
# 数据源就是会话记录本身（projects/<pid>/s_*.jsonl）：不新增存储，不依赖网关进程。

#: 通道写进 user 文本首行的「场合说明」前缀（见 gateway/channels/napcat.py）
_CTX_PREFIXES = ("（这是你的 QQ",)
_PEER_RE = re.compile(r"「(.+?)」\((\d+)\)")


def _strip_ctx(content: str) -> Tuple[str, str]:
    """剥掉首行场合说明，返回 (正文, 对方标识)。"""
    text = content or ""
    first, sep, rest = text.partition("\n")
    if first.startswith(_CTX_PREFIXES):
        m = _PEER_RE.search(first)
        peer = f"{m.group(1)}({m.group(2)})" if m else ""
        return (rest if sep else ""), peer
    return text, ""


def _channel_of_session(session_id: str) -> str:
    """会话 id → 通道名：`s_napcat_<hash>` → napcat；无通道段（如 `s_<hash>`）→ local。"""
    parts = (session_id or "").split("_")
    if len(parts) >= 3 and parts[0] == "s":
        return parts[1]
    return "local"


def _parse_ts(value: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(str(value))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _collect_recent(channel: str, hours: float, max_files: int = 24) -> List[Dict[str, Any]]:
    """扫最近改动的会话文件，配成「对方说 → 我回」的往来组，按时间倒序。"""
    from omni_core.local import runtime_paths as P

    root = P.projects_root()
    if not root.exists():
        return []
    files = sorted(root.glob("*/s_*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max(0.0, float(hours)))

    pairs: List[Dict[str, Any]] = []
    for path in files[:max_files]:
        sid = path.stem
        ch = _channel_of_session(sid)
        if channel and ch != channel:
            continue
        pending: Optional[Dict[str, Any]] = None
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        for line in lines:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            role = str(rec.get("role") or "")
            if role not in ("user", "assistant"):
                continue
            ts = _parse_ts(rec.get("ts") or "")
            if ts is not None and ts < cutoff:
                continue
            content = str(rec.get("content") or "").strip()
            if not content:
                continue
            if role == "user":
                body, peer = _strip_ctx(content)
                pending = {"ts": ts, "said": body, "peer": peer,
                           "project": path.parent.name, "session": sid, "channel": ch}
            elif pending is not None:
                pending["me"] = content
                pairs.append(pending)
                pending = None

    epoch = datetime.min.replace(tzinfo=timezone.utc)
    pairs.sort(key=lambda p: p["ts"] or epoch, reverse=True)
    return pairs


@function_tool(
    description="查看最近的消息往来（跨会话/跨项目）：谁找过你、什么时候、你当时怎么回的。"
                "回答『有人找我吗』『我上次怎么跟他说的』用它，不要用 shell 去翻会话文件。",
    unit="messaging",
)
def msg_recent(channel: str = "", limit: int = 20, hours: float = 24) -> Dict[str, Any]:
    """最近消息往来。

    Args:
        channel: 只看某个通道（napcat/feishu…；缺省=全部）
        limit: 最多返回多少组往来（对方一句 + 我一句算一组）
        hours: 时间窗（小时）
    """
    try:
        pairs = _collect_recent(channel, hours)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    try:
        cap = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        cap = 20

    items: List[Dict[str, Any]] = []
    for p in pairs[:cap]:
        items.append({
            "when": p["ts"].isoformat() if p["ts"] else "",
            "channel": p["channel"],
            "project": p["project"],
            "who": p["peer"] or ("主人" if p["channel"] == "local" else ""),
            "said": (p["said"] or "")[:600],
            "my_reply": (p.get("me") or "")[:600],
        })
    return {
        "ok": True,
        "count": len(items),
        "hours": hours,
        "note": "按时间倒序；said=对方说的（已剥掉通道场合前缀），my_reply=我当时回他的",
        "messages": items,
    }
