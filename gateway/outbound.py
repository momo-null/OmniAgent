"""出站守则（对外身份「马甲」统一注入契约，plan §8，2026-11-08）。

一份结构、一处归一逻辑、两处消费、零适配器特判：

- **键统一**：所有 ``channels/<name>.yaml`` 用同一套 ``outbound`` 通用键，
  禁止 ``qq_*`` / ``bili_*`` 平台专属键——平台差异只是值。
- **马甲不是人格**：``identity`` 只是对外自称，人格统一在角色卡（character.md）；
  不写人格覆盖声明、不分记忆仓。定量规矩（这条怎么发）在通道配置，定性（什么调性）
  在角色卡，事实（主人是谁）在画像——三层正交（§8.2 裁决）。
- **归一**：``resolve(channel, chat_id)`` 是唯一的配置读取+拍平入口，
  三个消费方共用本模块：
    · ``gateway/dispatch.py``   —— 网关入站：拍平结果作「本轮场合自述」附到模型输入
                                  （display_text 保留原话，会话历史不污染）；
    · ``chat_runtime.py``       —— Web 入口：同构（channels/web.yaml 缺省不存在 → 全空零影响）；
    · ``plugins/messaging``     —— 机械闸（唯一执行点）：发送时按 (通道, 会话) 解析，
                                  超限拆条/拒发。
- **实现偏离说明（对 §8.5「写线程上下文」）**：改为发送时现解析。原因：Web 后端
  多并发请求，进程级单值上下文会串轮；cron 又无入站来源、目标在发送时才确定。
  解析逻辑单一来源（本模块），三场景（群/私聊/定时）语义一致。
- **作用面**：run 级，不常驻、不进记忆；``context_key`` 只选择参数不过滤记忆（H-7/H-8）。
- **require_review**：schema 预留，机械执行（通用待发队列）属第二期（§8.7-5），
  本版本不读取不执行。

配置 schema（所有通道同构，模板见 examples/channels/*.yaml）::

    outbound:
      identity: ""            # 对外自称（可选；不写人格覆盖声明）
      style: ""               # 本通道对外说话规矩（自然语言，喂模型）
      max_chars: 0            # 单条字数上限，0=不限
      max_sentences: 0        # 单条句数上限，0=不限
      allow_multi: true       # 允许连发多条（超限时拆条；false=拒发让模型自己精简）
      require_review: false   # 先审后发（第二期接通用待发队列；本版本预留）
      by_context:             # 场合覆写：context_key = chat_id 首段（group/private/
        group:   { max_chars: 60,  max_sentences: 2 }   # comment/dynamic/chat…），内核不认识它
        private: { max_chars: 200, max_sentences: 5 }
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

#: schema 白名单：拍平时只保留这些键（防配置里混进未知键直通模型）
_KEYS = ("identity", "style", "max_chars", "max_sentences", "allow_multi", "require_review")

_SENT_RE = re.compile(r"[^。！？!?；;\n]*[。！？!?；;\n]+|[^。！？!?；;\n]+")


def context_key_of(chat_id: str) -> str:
    """场合键 = chat_id 首段：group:333→group / private:1→private / comment:…→comment /
    dynamic→dynamic / chat:<sid>→chat。无冒号整串当键（命中不了 by_context 就用基础值）。"""
    return (chat_id or "").split(":", 1)[0].strip()


def load_outbound(channel: str) -> Dict[str, Any]:
    """读 ``channels/<channel>.yaml`` 的 outbound 块；通道无配置/文件缺失 → {}。"""
    if not channel:
        return {}
    try:
        from .channel_gateway import channel_config

        cfg = channel_config(channel) or {}
    except Exception:
        return {}
    ob = cfg.get("outbound")
    return ob if isinstance(ob, dict) else {}


def flatten(outbound: Dict[str, Any], context_key: str = "") -> Dict[str, Any]:
    """基础值 + by_context[context_key] 覆写，白名单键拍平；未知键丢弃。"""
    ob = outbound if isinstance(outbound, dict) else {}
    flat: Dict[str, Any] = {}
    for k in _KEYS:
        if k in ob and ob[k] not in (None, ""):
            flat[k] = ob[k]
    override = ob.get("by_context")
    if isinstance(override, dict) and context_key and context_key in override:
        ov = override.get(context_key)
        if isinstance(ov, dict):
            for k in _KEYS:
                if k in ov and ov[k] not in (None, ""):
                    flat[k] = ov[k]
    return flat


def resolve(channel: str, chat_id: str) -> Dict[str, Any]:
    """唯一归一入口：读通道配置 → 按 (chat_id 首段) 选覆写 → 拍平。"""
    return flatten(load_outbound(channel), context_key_of(chat_id))


def _sentences(text: str) -> List[str]:
    """按中英句读切句（保留句尾标点），空段丢弃。"""
    parts = [p.strip() for p in _SENT_RE.findall(text or "") if p.strip()]
    return parts


def enforce(flat: Dict[str, Any], text: str) -> Tuple[List[str], str]:
    """机械闸：超限按句拆条（一条一个意思）或拒发。

    返回 (拆条列表, "") 或 ([], 错误文案)。无配置/未超限 → ([原文], "")。
    单句本身超长拆不动 → 拒发（让模型精简，不做字符级硬切）。
    """
    text = (text or "").strip()
    if not text:
        return [], "空文本不发送"
    max_chars = int(flat.get("max_chars") or 0)
    max_sents = int(flat.get("max_sentences") or 0)
    allow_multi = bool(flat.get("allow_multi", True))
    sents = _sentences(text)
    over_chars = bool(max_chars and len(text) > max_chars)
    over_sents = bool(max_sents and len(sents) > max_sents)
    if not over_chars and not over_sents:
        return [text], ""
    if not allow_multi:
        return [], (f"超出本场合出站上限（单条≤{max_chars or '∞'}字≤{max_sents or '∞'}句），"
                    "本场合不允许拆条，请精简为一条")
    chunks: List[str] = []
    cur: List[str] = []
    cur_len = 0
    for s in sents:
        if max_chars and len(s) > max_chars:
            return [], (f"存在单句 {len(s)} 字超过上限 {max_chars}，拆条也放不下，"
                        "请把这句精简后再发")
        if cur and ((max_chars and cur_len + len(s) > max_chars)
                    or (max_sents and len(cur) >= max_sents)):
            chunks.append("".join(cur))
            cur, cur_len = [], 0
        cur.append(s)
        cur_len += len(s)
    if cur:
        chunks.append("".join(cur))
    return chunks, ""


def voice_error(flat: Dict[str, Any], text: str) -> str:
    """语音的上限约束：只拒不拆（拆条朗读很怪，模型该自己分句改用文字或精简）。"""
    text = (text or "").strip()
    max_chars = int(flat.get("max_chars") or 0)
    max_sents = int(flat.get("max_sentences") or 0)
    over_chars = bool(max_chars and len(text) > max_chars)
    over_sents = bool(max_sents and len(_sentences(text)) > max_sents)
    if not over_chars and not over_sents:
        return ""
    return (f"超出本场合出站上限（单条≤{max_chars or '∞'}字≤{max_sents or '∞'}句），"
            "语音不拆条：请精简，或改用文字并拆成几条短发")


def note(flat: Dict[str, Any]) -> str:
    """拍平结果 → 「本轮场合自述」文本（喂模型；空配置返回空串）。

    机械上限同时告知模型（闸是兜底，先让模型自己发对）。
    """
    if not flat:
        return ""
    parts: List[str] = []
    identity = str(flat.get("identity") or "").strip()
    style = str(flat.get("style") or "").strip()
    if identity:
        parts.append(f"对外自称「{identity}」")
    if style:
        parts.append(style)
    max_chars = int(flat.get("max_chars") or 0)
    max_sents = int(flat.get("max_sentences") or 0)
    allow_multi = bool(flat.get("allow_multi", True))
    limits: List[str] = []
    if max_chars:
        limits.append(f"单条≤{max_chars}字")
    if max_sents:
        limits.append(f"≤{max_sents}句")
    if limits:
        tail = "要说的事多就拆成几条短发，一条一个意思" if allow_multi else "不要拆条，超了就精简"
        parts.append("；".join(limits) + f"（{tail}）")
    if not parts:
        return ""
    return "（对外守则：" + "；".join(parts) + "）"
