"""CosyVoice2 本地推理运行时（plugins/voice 专用，内核零 import）。

来源与许可：
- ``espnet/``、``mask.py``、``common.py``、``flow.py``、``cfm.py``、``decoder.py``、
  ``hift.py``、``llm.py``：FunAudioLLM/CosyVoice @ tag v2.0（Apache-2.0），仅做
  import 重写与训练/流式/vllm 裁剪；
- ``shim.py``：diffusers 最小子集（参数名/形状对齐）+ Matcha-TTS 组件（MIT）；
- ``feats.py``、``textnorm.py``、``engine.py``：手写胶水（对齐官方管道公式）。

推理代码与官方权重（llm.pt / flow.pt / hift.pt / speech_tokenizer_v2.onnx /
campplus.onnx，~/.omniagent/models/cosyvoice2/）经 state dict 严格加载验证。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 重依赖（torch 等）只在引擎实例化时导入，保持 import 本包零开销
    from cosyvoice2_rt.engine import CosyVoice2Engine

__all__ = ["CosyVoice2Engine"]


def __getattr__(name):
    if name == "CosyVoice2Engine":
        from cosyvoice2_rt.engine import CosyVoice2Engine

        return CosyVoice2Engine
    raise AttributeError(name)
