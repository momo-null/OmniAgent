"""最小模型判定客户端（plan §3「LLM 提炼内容，脚本治理结构」/ §8.2-1「语义判断全部交模型」）。

注入层此前没有模型通路（选档全脚本）；§8 回写要求去重、检索选档、
技能提炼等**语义判断**交给模型。本模块只做一件事：给一段 brain 配置，发一次
补全请求，解析出 JSON——不做任何业务判定，不落任何盘。

准入条件（B5 纪律：谁写 / 谁读 / 读不到会怎样）：
- **谁写**：无人。本模块是纯读的判定原语；所有产物（选档结果 / 判重结论 / 提炼
  候选）由调用方决定是否落盘。
- **谁读**：``knowledge_inject``（注入选档、技能目录选档
  提炼，``skill.auto_distill`` 开关打开时）。
- **读不到会怎样**：配置缺失 / 无 api_key / 请求失败 / 解析失败 → 返回 ``None``，
  调用方退回纯脚本路径（原序 / 精确去重 / 不产出）。失败降级是**契约**而非兜底
  （§3 硬约束：绝不拖垮主链路）。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

#: 判定调用的默认超时（秒）。选档发生在任务组装期（§3.2 取舍：约 0.8s 一档），
#: 超时上限压住最坏情况——判定卡死不得拖慢任务启动。
DEFAULT_TIMEOUT_S = 10.0


def chat_json(brain_cfg: Optional[Dict[str, Any]], system: str, user: str,
              timeout: float = DEFAULT_TIMEOUT_S) -> Optional[Any]:
    """发一次补全并解析首个 JSON 值；任何失败 → ``None``（调用方降级）。

    ``brain_cfg`` 需要 ``base_url`` / ``model`` / ``api_key``（与 LLMClient 同构）；
    为空或缺 api_key 视为「无模型」，直接返回 ``None``。
    """
    if not brain_cfg or not (brain_cfg.get("base_url") and brain_cfg.get("model")):
        return None
    try:
        from omni_core.brain.llm import LLMClient

        client = LLMClient(dict(brain_cfg), timeout=timeout)
        reply = client.chat([
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ])
        return _extract_json(getattr(reply, "content", "") or "")
    except Exception:
        return None


def _extract_json(text: str) -> Optional[Any]:
    """从模型回复里抠出第一个 JSON 值（容忍代码围栏与前后缀噪声）；失败 → None。"""
    t = str(text or "").strip()
    if not t:
        return None
    # 剥 ```json ... ``` 围栏
    if t.startswith("```"):
        t = t.strip("`")
        if t.lower().startswith("json"):
            t = t[4:]
    try:
        return json.loads(t)
    except Exception:
        pass
    start = t.find("{")
    if start < 0:
        start = t.find("[")
    if start < 0:
        return None
    closer = "}" if t[start] == "{" else "]"
    end = t.rfind(closer)
    if end <= start:
        return None
    try:
        return json.loads(t[start:end + 1])
    except Exception:
        return None


def rank_indices(brain_cfg: Optional[Dict[str, Any]], system: str, user: str,
                 count: int, timeout: float = DEFAULT_TIMEOUT_S) -> List[int]:
    """选档判定：模型从编号候选里挑相关项，返回去重后的 0-based 编号（按相关度序）。

    解析 ``{"relevant": [...]}``；编号越界 / 重复 / 非整数的项丢弃；任何失败 →
    空列表（调用方保持原序，§3.2「无模型时保持原序、不做相关性判断」）。
    """
    verdict = chat_json(brain_cfg, system, user, timeout=timeout)
    if not isinstance(verdict, dict):
        return []
    raw = verdict.get("relevant")
    if not isinstance(raw, list):
        return []
    out: List[int] = []
    for item in raw:
        try:
            idx = int(item)
        except (TypeError, ValueError):
            continue
        if 0 <= idx < count and idx not in out:
            out.append(idx)
    return out
