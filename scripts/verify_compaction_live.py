"""真机验证 _CompactionModel：用真实 LLM 做摘要回调，构造超阈值中文 items，
验证压缩能否触发并产出 [历史压缩摘要]，同时打印 _estimate_tokens 真实值。

不依赖 agent 自主行为——直接驱动 Model 层压缩逻辑，确定性验证机制本身工作。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from openai import OpenAI
from omni_core.brain.sdk_loop import (
    _CompactionModel,
    _estimate_tokens,
    SUMMARY_INSTRUCTION,
    _item_text,
)

cfg = config.load_config()
bc = cfg.get("brain", {})
api_key = bc.get("api_key") or os.environ.get(bc.get("api_key_env", ""), "")
client = OpenAI(api_key=api_key, base_url=bc.get("base_url"))
MODEL = bc.get("model") or "deepseek-v4.1-flash"
print(f"[verify] model={MODEL} base_url={bc.get('base_url')}")


def real_summarize(items, si=None):
    text = "\n".join(_item_text(i) for i in items)
    msgs = []
    if si:
        msgs.append({"role": "system", "content": str(si)})
    msgs.append({"role": "user", "content": SUMMARY_INSTRUCTION + "\n\n" + text[:40000]})
    try:
        r = client.chat.completions.create(
            model=MODEL, messages=msgs, max_tokens=2000, temperature=0.2
        )
        return r.choices[0].message.content
    except Exception as e:  # noqa: BLE001
        print("[verify] summarize error:", repr(e))
        return None


# 构造超长中文 items：重复读取一个真实大文件
src = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "omni_core", "brain", "sdk_loop.py",
)
with open(src, "r", encoding="utf-8") as f:
    base = f.read()
items = [{"role": "user", "content": base * 6}]  # 放大到远超阈值
est = _estimate_tokens(items)
print(f"[verify] 构造 items 估算 token = {est}")


def has_summary(out):
    return out is not None and any(
        "[历史压缩摘要]" in str(_item_text(i)) for i in out
    )


for thr in (8000, 50000, 225000):
    m = _CompactionModel(
        None,
        threshold_tokens=thr,
        retain_ratio=0.5,
        summarize=real_summarize,
        hard_ceiling_tokens=thr * 2,
        keep_rounds=8,
    )
    out = m._compact("系统：你是压缩测试", items)
    print(
        f"[verify] 阈值={thr:>7} -> 触发压缩={has_summary(out)}  "
        f"(压缩前估算={est}, 压缩后估算={_estimate_tokens(out) if out else 'N/A'})"
    )
