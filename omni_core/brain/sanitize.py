"""标签残片消毒助手（2026-11-15 事故族，三层防线的单一实现）。

背景（生产实测）：中转站偶发把 DeepSeek 推理段/工具调用残片序列化进 content
流，观测形态：
  ① 空包裹 ``<thinking> </thinking>``——一旦进历史，后续调用 100% 模仿（实测
    479/479），run 内雪崩 → 输入侧进历史前必须剥掉（sdk_loop._ContentSanitizeModel）；
  ② 畸形残片 ``<memory_recall">``——native tool call 组装失败后泄漏进收尾文本，
    会被兜底代发给用户 → 收尾判定处拦截重试（instructions._plain_finish_judge）；
  ③ 孤立前导标签 ``<butong>\\n正文``——剥前缀取正文（记录侧 emitter）。

三层（输入/记录/收尾）共用本模块同一实现，避免规则漂移。只删不增；lead 剥壳
只认 snake_case（含 ``_``）或畸形（带 ``"``）标签——HTML 代码回复（``<div>`` 等）
零误伤。``<butong>``（无下划线，2/1635 例）为已知覆盖盲区，由计数观察。

本模块零依赖（仅 re），可被 brain 与 local.loop 双侧安全导入（无循环）。
"""
import re
from typing import Dict

# 空包裹：`<tag>\s*</tag>`（可连排）；任意标签名——空包裹零信息量，命中即垃圾
_EMPTY_TAG_WRAPPER_RE = re.compile(r"(?:<([A-Za-z_][A-Za-z0-9_-]{0,40})>\s*</\1>\s*)+")
# 前导开标签：正常闭合 `<tag>` / 畸形 `<tag">`（native tool call 残片形态）/
# 空格变体 `<tag >`；带属性的形式（`<tag class=..>`）不匹配——HTML 零误伤
_LEAD_TAG_RE = re.compile(r"^<(?P<name>[A-Za-z_][A-Za-z0-9_-]{0,40})\s*\"?\s*>\s*")
# snake_case 判定：前导孤立标签只剥「工具名/协议名形状」的——HTML 标签（div/p/
# span 等几乎从不带下划线）零误伤
_SNAKE_NAME_RE = re.compile(r"[A-Za-z]_[A-Za-z0-9_-]*|_[A-Za-z0-9_-]+")


def strip_tag_junk(text: str) -> str:
    """剥掉空标签包裹与**前导**标签残片，返回干净正文（不改变其余内容）。

    幂等；只在命中污染形态时改写，干净文本逐字节原样返回（strip 过首尾空白）。
    重复套用（一对包裹里还有一对、多个前导标签连排）由循环收敛处理。
    """
    t = str(text or "")
    prev = None
    while prev != t:
        prev = t
        t = _EMPTY_TAG_WRAPPER_RE.sub("", t)
        m = _LEAD_TAG_RE.match(t)
        if m and (_SNAKE_NAME_RE.search(m.group("name")) or '"' in t[:m.end()]):
            t = t[m.end():]
    return t.strip()


def is_pure_tag_fragment(text: str) -> bool:
    """收尾文本是否为**纯**标签残片（剥完为空，或整体是无闭合括号的畸形开标签）。

    实测量例：``<memory_recall">``（16 字符）。正常回复（包括以 ``<3``、代码块、
    ``<div class=..>`` 开头的）都不命中。
    """
    t = str(text or "").strip()
    if not t:
        return False
    if strip_tag_junk(t) == "":
        return True
    return bool(re.fullmatch(r"<[A-Za-z_][A-Za-z0-9_-]{0,40}\"?>", t)
                or re.fullmatch(r"<[A-Za-z_][A-Za-z0-9_-]{0,40}\"", t))


# 事故族剥壳计数（观测面）：中转站还在不在漏、哪个环节在剥，由此对出。
# 进程级累计（跨 run 看趋势；run 收尾处非零则落一条轨迹行做 per-run 归属）。
SANITIZE_STATS: Dict[str, int] = {"input_strips": 0, "record_strips": 0, "fragment_retries": 0}
