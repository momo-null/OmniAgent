"""GPT-SoVITS v2 微调引擎适配（``external/GPT-SoVITS`` 的 TTS_infer_pack 薄封装）。

与被退役的 CosyVoice2 适配（cosyvoice2_rt）实现同一个三方法契约：

- ``__init__(gs_dir, model_dir, voices_dir, vits="", t2s="")``
- ``list_voices() -> list``：``voices/`` 下每个 wav 一个音色，同名 .txt = 韵律参考文本
- ``synthesize(text, voice) -> (samples, sr)``：float32 单声道

范式差异（为什么换引擎）：CosyVoice2 zero-shot 每次推理拿 5.5s 参考临时捏音色，
分布外音源上漂移大；GS 微调把音色学进 SoVITS 权重，参考音频只管韵律。
微调与验收数据见 ``doc/plans/voice-tone-2026-10-31.md`` §9。

运行环境：本模块必须在 **GPT-SoVITS 独立 venv** 的 python 下运行（依赖 torch /
transformers>=4.51 / funasr 等，与主 .venv 隔离），由 plugin.py 按配置拉起。

对上游 clone 打过三处补丁（重 clone 后需重打，见 ``doc/plans/voice-tone-2026-10-31.md`` §9.4）：
TTS.py 两处 torchaudio.load→soundfile（torch 2.10 委托 torchcodec）；s1_train.py
单卡免 DDP；bucket_sampler 加 dist.is_initialized() 守卫。
"""
from __future__ import annotations

import os
import sys
from glob import glob
from typing import List, Tuple


class GptSovitsEngine:
    def __init__(self, gs_dir: str, model_dir: str, voices_dir: str,
                 vits: str = "", t2s: str = "") -> None:
        self._voices_dir = voices_dir
        if not os.path.isdir(gs_dir):
            raise FileNotFoundError(f"GPT-SoVITS 仓库不存在: {gs_dir}")
        # GPT_SoVITS 内部模块按仓库根解析相对资源（text/G2PWModel 等）
        os.chdir(gs_dir)
        for p in (gs_dir, os.path.join(gs_dir, "GPT_SoVITS")):
            if p not in sys.path:
                sys.path.insert(0, p)
        # 英文 g2p 的 nltk 语料（纯中文用不到，存在则挂上）
        nltk_data = os.path.join(gs_dir, "nltk_data")
        if os.path.isdir(nltk_data):
            import nltk

            if nltk_data not in nltk.data.path:
                nltk.data.path.insert(0, nltk_data)

        pre = os.path.join(model_dir, "pretrained_models")
        vits = vits or os.path.join(model_dir, "weights", "doro_e12_s288.pth")
        t2s = t2s or os.path.join(model_dir, "weights", "doro-e30.ckpt")
        missing = [p for p in (vits, t2s,
                               os.path.join(pre, "chinese-hubert-base"),
                               os.path.join(pre, "chinese-roberta-wwm-ext-large"))
                   if not os.path.exists(p)]
        if missing:
            raise FileNotFoundError("GPT-SoVITS 模型缺失: " + ", ".join(missing))

        # 文本前端（chinese2.py）在模块 import 时就初始化 G2PW 多音字模型，
        # 其 bert 路径读环境变量 bert_path（缺省是仓库相对路径）——必须在
        # TTS 模块 import 之前设好绝对路径。
        os.environ["bert_path"] = os.path.join(pre, "chinese-roberta-wwm-ext-large")

        from GPT_SoVITS.TTS_infer_pack.TTS import TTS, TTS_Config

        cfg = TTS_Config(os.path.join(gs_dir, "GPT_SoVITS", "configs", "tts_infer.yaml"))
        cfg.device = "cuda" if __import__("torch").cuda.is_available() else "cpu"
        cfg.version = "v2"
        cfg.cnhuhbert_base_path = os.path.join(pre, "chinese-hubert-base")
        cfg.bert_base_path = os.path.join(pre, "chinese-roberta-wwm-ext-large")
        cfg.vits_weights_path = vits
        cfg.t2s_weights_path = t2s
        self._tts = TTS(cfg)

    # ── 契约 ──
    def list_voices(self) -> List[str]:
        out = []
        for wav in sorted(glob(os.path.join(self._voices_dir, "*.wav"))):
            out.append(os.path.splitext(os.path.basename(wav))[0])
        return out

    def synthesize(self, text: str, voice: str = "") -> Tuple:
        voices = self.list_voices()
        if not voice or voice not in voices:
            if not voices:
                raise RuntimeError("voices/ 下没有任何音色")
            voice = voices[0]
        ref = os.path.join(self._voices_dir, voice + ".wav")
        txt = os.path.splitext(ref)[0] + ".txt"
        prompt = ""
        if os.path.isfile(txt):
            with open(txt, encoding="utf-8") as f:
                prompt = f.read().strip()
        inputs = {
            "text": text,
            "text_lang": "zh",
            "ref_audio_path": ref,
            "prompt_text": prompt,
            "prompt_lang": "zh",
            "top_k": 15,
            "top_p": 1.0,
            "temperature": 1.0,
            "speed_factor": 1.0,
            "seed": -1,
            "return_fragment": False,
        }
        import numpy as np

        parts: List = []
        sr = 32000
        for chunk in self._tts.run(inputs):
            if isinstance(chunk, tuple):
                sr, pcm = chunk
                parts.append(np.asarray(pcm, dtype=np.int16))
            else:
                parts.append(np.asarray(chunk).astype(np.int16))
        if not parts:
            raise RuntimeError("合成结果为空")
        samples = np.concatenate(parts).astype(np.float32) / 32768.0
        return samples, int(sr)
