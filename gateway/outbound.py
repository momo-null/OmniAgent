"""通道出站参数的归一与机械闸。

- **键统一**：所有 ``channels/<name>.yaml`` 用同一套 ``outbound`` 通用键，
  禁止平台专属键——平台差异只是值。
- **归一**：``resolve(channel, chat_id)`` 是唯一的配置读取+拍平入口，
  三个消费方共用本模块：
    · ``gateway/dispatch.py``   —— 网关入站：拍平结果作「本轮场合自述」附到模型输入
                                  （display_text 保留原话，会话历史不污染）；
    · ``chat_runtime.py``       —— Web 入口：同构（channels/web.yaml 缺省不存在 → 全空零影响）；
    · ``plugins/messaging``     —— 机械闸（唯一执行点）：发送时按 (通道, 会话) 解析，
                                  空行分条 / 超限续拆 / 拒发。
- **发送时解析**：Web 后端多并发请求，进程级单值上下文会串轮；cron 又无入站来源、
  目标在发送时才确定。解析逻辑单一来源（本模块），三场景语义一致。
- **作用面**：run 级，不常驻、不进记忆；场合键只选择参数不过滤记忆。
- **require_review**：schema 预留，机械执行（通用待发队列）属第二期，本版本不读取不执行。

配置 schema（所有通道同构，模板见 examples/channels/*.yaml）::

    outbound:
      identity: ""            # 对外自称（可选；不写人格覆盖声明）
      style: ""               # 本通道对外说话规矩（自然语言，喂模型）
      max_chars: 0            # 单条字数上限，0=不限
      max_sentences: 0        # 单条句数上限，0=不限
      max_chunks: 0           # 一轮最多发几条，0=不限；拆条后仍超 → 拒发让模型删减
      allow_multi: true       # 允许连发多条（超限时拆条；false=拒发让模型自己精简）
      require_review: false   # 先审后发（第二期接通用待发队列；本版本预留）
      by_context:             # 场合覆写：context_key = chat_id 首段（group/private/
        group:   { max_chars: 40, max_sentences: 1, max_chunks: 3 }   # comment/dynamic/chat…），内核不认识它
        private: { max_chars: 80, max_sentences: 2, max_chunks: 4 }
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

#: schema 白名单：拍平时只保留这些键（防配置里混进未知键直通模型）
_KEYS = ("identity", "style", "max_chars", "max_sentences", "max_chunks",
         "allow_multi", "require_review")

_SENT_RE = re.compile(r"[^。！？!?；;\n]*[。！？!?；;\n]+|[^。！？!?；;\n]+")

#: 空行（含只有空白的行）= 模型在 text 里显式写出的「这几句要分成几条发」。
#: 段内单换行只是排版，不当分条信号。
_PARA_RE = re.compile(r"\n[ \t]*\n+")


def split_paragraphs(text: str) -> List[str]:
    """按空行切段：段首尾空白去掉、空段丢弃，段内单换行原样保留。

    这是出口侧唯一认「分条意图」的地方：**不依赖 outbound 配置**——通道没配
    ``max_chars`` / ``max_sentences`` 时，模型写了几段就发几条。
    """
    normalized = (text or "").replace("\r\n", "\n")
    return [p.strip() for p in _PARA_RE.split(normalized) if p.strip()]


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


#: 只按句读切片段（保留句尾标点）；单换行留在片段内部，不当句子边界。
_PIECE_RE = re.compile(r"[^。！？!?；;]*[。！？!?；;]+|[^。！？!?；;]+")


def _pieces(text: str) -> List[str]:
    """切句但保留句尾标点与段内单换行；纯空白片段丢弃。"""
    return [p for p in _PIECE_RE.findall(text or "") if p.strip()]


def _pack_one(text: str, max_chars: int, max_sents: int,
              allow_multi: bool) -> Tuple[List[str], str]:
    """单段内部：未超限原样返回；超限按句续拆或拒发（重组时保留段内换行）。"""
    pieces = _pieces(text)
    over_chars = bool(max_chars and len(text) > max_chars)
    over_sents = bool(max_sents and len(pieces) > max_sents)
    if not over_chars and not over_sents:
        return [text], ""
    if not allow_multi:
        return [], (f"超出本场合出站上限（单条≤{max_chars or '∞'}字≤{max_sents or '∞'}句），"
                    "本场合不允许拆条，请精简为一条")
    chunks: List[str] = []
    cur: List[str] = []
    cur_len = 0
    for p in pieces:
        if max_chars and len(p.strip()) > max_chars:
            return [], (f"存在单句 {len(p.strip())} 字超过上限 {max_chars}，拆条也放不下，"
                        "请把这句精简后再发")
        if cur and ((max_chars and cur_len + len(p) > max_chars)
                    or (max_sents and len(cur) >= max_sents)):
            chunks.append("".join(cur).strip())
            cur, cur_len = [], 0
        cur.append(p)
        cur_len += len(p)
    if cur:
        chunks.append("".join(cur).strip())
    return [c for c in chunks if c], ""


def enforce(flat: Dict[str, Any], text: str,
            split_blank_lines: bool = True) -> Tuple[List[str], str]:
    """机械闸：空行先分条，再对每条独立套上限（超限按句续拆或拒发）。

    - **空行 = 模型显式写出的分条意图，不依赖配置**：写了几段就发几条，通道没配
      ``max_chars`` / ``max_sentences`` 也照分；段内单换行只是排版，不误拆。
    - 每段独立套上限：段内超限才按句续拆；单句本身超长拆不动 → 拒发让模型精简。
    - ``max_chunks``：一轮最多发几条（拆条之后的最终条数）。超了拒发、提示删减，
      不截断也不静默丢内容——模型自己重写才可能说人话。
    - ``allow_multi=false``：多段直接拒发并要求合成一条（不静默吞掉分条意图），
      单段超限仍按原口径拒发。
    - ``split_blank_lines=False`` 给「本就只该一条」的调用点（图片说明）用，
      避免分段后只取首段导致内容丢失。
    """
    text = (text or "").strip()
    if not text:
        return [], "空文本不发送"
    max_chars = int(flat.get("max_chars") or 0)
    max_sents = int(flat.get("max_sentences") or 0)
    max_chunks = int(flat.get("max_chunks") or 0)
    allow_multi = bool(flat.get("allow_multi", True))
    paras = split_paragraphs(text) if split_blank_lines else [text]
    if len(paras) > 1 and not allow_multi:
        return [], (f"本场合不允许拆条（写了 {len(paras)} 段），请把想说的合成一条再发")
    chunks: List[str] = []
    for para in paras:
        sub, err = _pack_one(para, max_chars, max_sents, allow_multi)
        if err:
            return [], err
        chunks.extend(sub)
    if max_chunks and len(chunks) > max_chunks:
        return [], (f"本场合一轮最多说 {max_chunks} 条（这条拆成了 {len(chunks)} 条），"
                    "请删减想说的内容后重发")
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
            "语音不拆条：请精简，或改用文字分条发送")


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
    max_chunks = int(flat.get("max_chunks") or 0)
    allow_multi = bool(flat.get("allow_multi", True))
    limits: List[str] = []
    if max_chars:
        limits.append(f"单条≤{max_chars}字")
    if max_sents:
        limits.append(f"≤{max_sents}句")
    if max_chunks:
        limits.append(f"一轮最多{max_chunks}条")
    if limits:
        tail = "可拆成多条发送" if allow_multi else "不要拆条，超了就精简"
        parts.append("；".join(limits) + f"（{tail}）")
    if not parts:
        return ""
    return "（对外守则：" + "；".join(parts) + "）"
