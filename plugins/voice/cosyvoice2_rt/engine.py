"""CosyVoice2 引擎编排（本包唯一的手写胶水层，其余为 vendored 上游代码）。

流水线（整段合成，与官方 CosyVoice2Model 非流式路径一致）：
  参考音频 → (whisper 128-mel → speech_tokenizer_v2.onnx) = prompt 语音 token
           → (kaldi 80-mel → campplus.onnx) = 音色向量 192 维
           → (matcha 80-mel @24k) = flow 的 prompt_feat
  文本 → 轻量规整（cn2an 数字转汉字）→ split_paragraph（≤80 字/段）
      → Qwen2LM（llm.pt，RAS 采样）→ 语音 token
      → CausalMaskedDiffWithXvec（flow.pt，10 步 Euler-CFM）→ mel
      → HiFTGenerator（hift.pt）→ 24kHz 波形

构造参数逐项来自官方 ``cosyvoice2.yaml``（模型目录里有原件可对照）。
音色 = 参考音频零样本克隆：``voices/<名字>.wav`` 一段即一个音色；同名 ``.txt``
存在时按 zero-shot 模式（LLM 带上 prompt 文本+语音），否则 cross-lingual 模式。
prompt token / 音色向量 / prompt_feat 按 (路径, mtime) 缓存，每段参考音频只算一次。
"""
import re
import threading
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Dict

import numpy as np
import onnxruntime as ort
import torch

from cosyvoice2_rt import feats, textnorm
from cosyvoice2_rt.cfm import CausalConditionalCFM
from cosyvoice2_rt.common import ras_sampling
from cosyvoice2_rt.decoder import CausalConditionalDecoder
from cosyvoice2_rt.espnet.upsample_encoder import UpsampleConformerEncoder
from cosyvoice2_rt.flow import CausalMaskedDiffWithXvec
from cosyvoice2_rt.hift import ConvRNNF0Predictor, HiFTGenerator
from cosyvoice2_rt.llm import Qwen2Encoder, Qwen2LM

#: 官方 cosyvoice2.yaml 的固定参数
SAMPLE_RATE = 24000
SPEECH_TOKEN_SIZE = 6561
LLM_INPUT_SIZE = 896
LLM_OUTPUT_SIZE = 896
SPK_EMBED_DIM = 192
TOKEN_FRAME_RATE = 25
TOKEN_MEL_RATIO = 2

#: 必需文件（相对模型目录）
_REQUIRED_FILES = (
    "llm.pt", "flow.pt", "hift.pt", "speech_tokenizer_v2.onnx",
    "campplus.onnx", "tokenizer/config.json",
)

#: llm.pt 是绑定词表（tie_word_embeddings）存档：没有 lm_head 权重属预期，
# 且 Qwen2LM 推理只取 hidden_states，从不走 lm_head——除此之外键必须严格对上。
_ALLOWED_MISSING_LLM = {"llm.model.lm_head.weight"}

_SPECIAL_TOKENS = {
    "eos_token": "<|endoftext|>",
    "pad_token": "<|endoftext|>",
    "additional_special_tokens": [
        "<|im_start|>", "<|im_end|>", "<|endofprompt|>",
        "[breath]", "<strong>", "</strong>", "[noise]",
        "[laughter]", "[cough]", "[clucking]", "[accent]",
        "[quick_breath]", "<laughter>", "</laughter>",
        "[hissing]", "[sigh]", "[vocalized-noise]",
        "[lipsmack]", "[mn]",
    ],
}


def missing_files(model_dir: str) -> list:
    base = Path(model_dir).expanduser()
    return [str(base / rel) for rel in _REQUIRED_FILES if not (base / rel).is_file()]


class CosyVoice2Engine:
    """懒加载的 CosyVoice2 合成器单例（由 plugins/voice 持有，线程安全）。"""

    def __init__(self, model_dir: str, voices_dir: str, logger=None):
        self.model_dir = Path(model_dir).expanduser()
        self.voices_dir = Path(voices_dir).expanduser()
        self.log = logger
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self._lock = threading.Lock()
        self._voice_cache: Dict[tuple, dict] = {}
        self._tokenizer = None
        self._inflect = None
        missing = missing_files(str(self.model_dir))
        if missing:
            raise RuntimeError(
                "CosyVoice2 模型缺失: " + ", ".join(missing)
                + "（llm.pt/Qwen tokenizer 见 doc/plans/voice-m3-2026-10-29.md §模型）")
        self._build()
        self.log and self.log.info("CosyVoice2 引擎就绪（device=%s）", self.device)

    # ------------------------------------------------------------------
    # 模型构造与加载
    # ------------------------------------------------------------------
    def _build(self):
        dev = self.device
        tok_cfg = str(self.model_dir / "tokenizer" / "config.json")

        self.llm = Qwen2LM(
            llm_input_size=LLM_INPUT_SIZE, llm_output_size=LLM_OUTPUT_SIZE,
            speech_token_size=SPEECH_TOKEN_SIZE,
            llm=Qwen2Encoder(tok_cfg),
            sampling=partial(ras_sampling, top_p=0.8, top_k=25, win_size=10, tau_r=0.1),
        )
        sd = torch.load(self.model_dir / "llm.pt", map_location="cpu", weights_only=True)
        missing, unexpected = self.llm.load_state_dict(sd, strict=False)
        if unexpected or (set(missing) - _ALLOWED_MISSING_LLM):
            raise RuntimeError(
                f"llm.pt 与 Qwen2LM 结构不一致: missing={sorted(missing)} unexpected={sorted(unexpected)}")
        self.llm.llm.model.tie_weights()  # lm_head 与 embed_tokens 保持绑定（Qwen2.5 语义）
        self.llm.to(dev).eval()

        self.flow = CausalMaskedDiffWithXvec(
            input_size=512, output_size=80, spk_embed_dim=SPK_EMBED_DIM,
            output_type="mel", vocab_size=SPEECH_TOKEN_SIZE,
            input_frame_rate=TOKEN_FRAME_RATE, only_mask_loss=True,
            token_mel_ratio=TOKEN_MEL_RATIO, pre_lookahead_len=3,
            encoder=UpsampleConformerEncoder(
                output_size=512, attention_heads=8, linear_units=2048, num_blocks=6,
                dropout_rate=0.1, positional_dropout_rate=0.1, attention_dropout_rate=0.1,
                normalize_before=True, input_layer="linear", pos_enc_layer_type="rel_pos_espnet",
                selfattention_layer_type="rel_selfattn", input_size=512,
                use_cnn_module=False, macaron_style=False, static_chunk_size=25),
            decoder=CausalConditionalCFM(
                in_channels=240, n_spks=1, spk_emb_dim=80,
                cfm_params=SimpleNamespace(sigma_min=1e-06, solver="euler", t_scheduler="cosine",
                                           training_cfg_rate=0.2, inference_cfg_rate=0.7,
                                           reg_loss_type="l1"),
                estimator=CausalConditionalDecoder(
                    in_channels=320, out_channels=80, channels=[256], dropout=0.0,
                    attention_head_dim=64, n_blocks=4, num_mid_blocks=12, num_heads=8,
                    act_fn="gelu", static_chunk_size=25 * TOKEN_MEL_RATIO,
                    num_decoding_left_chunks=-1)),
        )
        self.flow.load_state_dict(
            torch.load(self.model_dir / "flow.pt", map_location="cpu", weights_only=True),
            strict=True)
        self.flow.to(dev).eval()

        self.hift = HiFTGenerator(
            in_channels=80, base_channels=512, nb_harmonics=8, sampling_rate=SAMPLE_RATE,
            nsf_alpha=0.1, nsf_sigma=0.003, nsf_voiced_threshold=10,
            upsample_rates=[8, 5, 3], upsample_kernel_sizes=[16, 11, 7],
            istft_params={"n_fft": 16, "hop_len": 4},
            resblock_kernel_sizes=[3, 7, 11],
            resblock_dilation_sizes=[[1, 3, 5], [1, 3, 5], [1, 3, 5]],
            source_resblock_kernel_sizes=[7, 7, 11],
            source_resblock_dilation_sizes=[[1, 3, 5], [1, 3, 5], [1, 3, 5]],
            lrelu_slope=0.1, audio_limit=0.99,
            f0_predictor=ConvRNNF0Predictor(num_class=1, in_channels=80, cond_channels=512))
        self.hift.load_state_dict(
            torch.load(self.model_dir / "hift.pt", map_location="cpu", weights_only=True),
            strict=True)
        self.hift.to(dev).eval()

        option = ort.SessionOptions()
        option.intra_op_num_threads = 4
        self._sess_token = ort.InferenceSession(
            str(self.model_dir / "speech_tokenizer_v2.onnx"),
            sess_options=option, providers=["CPUExecutionProvider"])
        self._sess_spk = ort.InferenceSession(
            str(self.model_dir / "campplus.onnx"),
            sess_options=option, providers=["CPUExecutionProvider"])

    # ------------------------------------------------------------------
    # 文本前端
    # ------------------------------------------------------------------
    def _get_tokenizer(self):
        if self._tokenizer is None:
            from transformers import AutoTokenizer

            self._tokenizer = AutoTokenizer.from_pretrained(str(self.model_dir / "tokenizer"))
            self._tokenizer.add_special_tokens(_SPECIAL_TOKENS)
        return self._tokenizer

    def _qwen_encode(self, text: str) -> list:
        tok = self._get_tokenizer()
        return tok([text], return_tensors="pt")["input_ids"][0].cpu().tolist()

    def _split_text(self, text: str) -> list:
        text = text.replace("\n", "")
        text = textnorm.replace_blank(text)
        text = textnorm.replace_corner_mark(text)
        if textnorm.contains_chinese(text):
            text = textnorm.zh_spell_numbers(text)
            text = text.replace(".", "。")
            text = text.replace(" - ", "，")
            text = textnorm.remove_bracket(text)
            text = re.sub(r"[，,、]+$", "。", text)
            lang = "zh"
        else:
            if self._inflect is None:
                import inflect

                self._inflect = inflect.engine()
            text = textnorm.spell_out_number(text, self._inflect)
            lang = "en"
        segs = textnorm.split_paragraph(text, self._qwen_encode, lang,
                                        token_max_n=80, token_min_n=60,
                                        merge_len=20, comma_split=False)
        return [s for s in segs if not textnorm.is_only_punctuation(s)]

    # ------------------------------------------------------------------
    # 音色（参考音频）
    # ------------------------------------------------------------------
    def list_voices(self) -> list:
        if not self.voices_dir.is_dir():
            return []
        return sorted(p.stem for p in self.voices_dir.glob("*.wav"))

    def _voice_context(self, voice: str) -> dict:
        names = self.list_voices()
        if not names:
            raise RuntimeError(
                "CosyVoice2 缺少参考音色：把一段 5-10s 干净人声存为 "
                f"{self.voices_dir}/<名字>.wav（可多段=多音色；同名 .txt 写转写文本可启用 "
                "zero-shot 模式，见 doc/plans/voice-m3-2026-10-29.md）")
        name = voice or names[0]
        path = self.voices_dir / f"{name}.wav"
        if not path.is_file():
            raise RuntimeError(f"音色不存在: {name}（可选: {', '.join(names)}）")
        key = (str(path), path.stat().st_mtime)
        ctx = self._voice_cache.get(key)
        if ctx is not None:
            return ctx

        wav16 = feats.load_wav(str(path), 16000)
        wav24 = feats.load_wav(str(path), 24000)

        mel_in = feats.whisper_log_mel(wav16)
        tokens = self._sess_token.run(None, {
            self._sess_token.get_inputs()[0].name: mel_in,
            self._sess_token.get_inputs()[1].name:
                np.array([mel_in.shape[2]], dtype=np.int32),
        })[0].flatten().tolist()

        emb = self._sess_spk.run(None, {
            self._sess_spk.get_inputs()[0].name: feats.kaldi_fbank80(wav16)[None],
        })[0].flatten().tolist()

        feat = feats.matcha_mel_24k(wav24).to(dtype=torch.float32,
                                              device=self.device).transpose(1, 2)  # (1, T, 80)
        token_t = torch.tensor([tokens], dtype=torch.int32, device=self.device)
        embedding = torch.tensor([emb], dtype=torch.float32, device=self.device)

        # 官方 frontend_zero_shot 的 24000 对齐分支：speech_feat % speech_token = 2
        n = min(feat.shape[1] // TOKEN_MEL_RATIO, token_t.shape[1])
        token_t, feat = token_t[:, :n], feat[:, :n * TOKEN_MEL_RATIO]

        # 同名 .txt = 参考音频的转写文本 → zero-shot（LLM 带上 prompt）；否则 cross-lingual
        txt = path.with_suffix(".txt")
        if txt.is_file():
            prompt_ids = self._qwen_encode(txt.read_text(encoding="utf-8").strip())
            prompt_text = torch.tensor([prompt_ids], dtype=torch.int32, device=self.device)
        else:
            prompt_text = torch.zeros(1, 0, dtype=torch.int32, device=self.device)

        ctx = {
            "token": token_t,
            "feat": feat,
            "embedding": embedding,
            "prompt_text": prompt_text,
            # cross-lingual：LLM 不带 prompt 语音 token，flow 条件仍用 prompt
            "llm_prompt_token": token_t if prompt_text.numel() else
                                torch.zeros(1, 0, dtype=torch.int32, device=self.device),
        }
        if len(self._voice_cache) >= 4:  # 换音色后旧缓存按插入序淘汰
            self._voice_cache.pop(next(iter(self._voice_cache)))
        self._voice_cache[key] = ctx
        self.log and self.log.info("CosyVoice2 音色已缓存: %s（%d 语音 token）", name, n)
        return ctx

    # ------------------------------------------------------------------
    # 合成
    # ------------------------------------------------------------------
    def synthesize(self, text: str, voice: str = "") -> tuple:
        """文本 → (np.float32 波形, 采样率)。空文本抛 ValueError；失败抛 RuntimeError。"""
        text = (text or "").strip()
        if not text:
            raise ValueError("text 为空")
        with self._lock:
            ctx = self._voice_context(voice)
            segs = self._split_text(text)
            if not segs:
                raise RuntimeError("文本规整后无可合成内容（纯标点？）")
            audios = []
            for seg in segs:
                text_token = torch.tensor([self._qwen_encode(seg)], dtype=torch.int32,
                                          device=self.device)
                text_len = torch.tensor([text_token.shape[1]], dtype=torch.int32,
                                        device=self.device)
                autocast = torch.autocast("cuda", dtype=torch.bfloat16,
                                          enabled=self.device.type == "cuda")
                with autocast:
                    tokens = list(self.llm.inference(
                        text=text_token, text_len=text_len,
                        prompt_text=ctx["prompt_text"],
                        prompt_text_len=torch.tensor([ctx["prompt_text"].shape[1]],
                                                     dtype=torch.int32, device=self.device),
                        prompt_speech_token=ctx["llm_prompt_token"],
                        prompt_speech_token_len=torch.tensor(
                            [ctx["llm_prompt_token"].shape[1]], dtype=torch.int32,
                            device=self.device),
                        embedding=ctx["embedding"],
                        sampling=TOKEN_FRAME_RATE, max_token_text_ratio=20,
                        min_token_text_ratio=2, uuid=""))
                if not tokens:
                    raise RuntimeError(f"LLM 未产出语音 token（段: {seg[:20]}…）")
                token = torch.tensor([tokens], dtype=torch.int32, device=self.device)
                mel, _ = self.flow.inference(
                    token=token,
                    token_len=torch.tensor([token.shape[1]], dtype=torch.int32,
                                           device=self.device),
                    prompt_token=ctx["token"],
                    prompt_token_len=torch.tensor([ctx["token"].shape[1]],
                                                  dtype=torch.int32, device=self.device),
                    prompt_feat=ctx["feat"],
                    prompt_feat_len=torch.tensor([ctx["feat"].shape[1]], dtype=torch.int32,
                                                 device=self.device),
                    embedding=ctx["embedding"], streaming=False, finalize=True)
                speech, _ = self.hift.inference(
                    speech_feat=mel, cache_source=torch.zeros(1, 1, 0, device=self.device))
                audios.append(speech.detach().cpu())  # (1, N)
            wav = torch.cat(audios, dim=1)
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
            return wav.flatten().numpy().astype(np.float32), SAMPLE_RATE
