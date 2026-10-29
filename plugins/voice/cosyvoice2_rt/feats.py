"""特征提取（与官方 CosyVoice2 管道逐位对齐的三类 mel 特征）。

- ``whisper_log_mel``：128 维 whisper log-mel（speech_tokenizer_v2.onnx 的输入，
  openai-whisper==20231117 的等价实现——不补零、stft(400,160,hann,center=True) 丢末帧、
  slaney/slaney 滤波器组、log10 clamp 1e-10、max-8、(x+4)/4）；
- ``kaldi_fbank80``：kaldi 80 维 fbank + 去均值（campplus.onnx 的输入）；
- ``matcha_mel_24k``：matcha/librosa 风格 80 维 24kHz mel（flow 的 prompt_feat，
  n_fft=1920 hop=480 fmax=8000，reflect pad、log clamp 1e-5）。

librosa 滤波器组用 torchaudio.functional.melscale_fbanks 等价替换
（librosa 默认 htk=False ⇒ mel_scale="slaney"；norm 参数一一对应）。
"""
from functools import lru_cache

import numpy as np
import torch
import torchaudio
import torchaudio.compliance.kaldi as kaldi
from torchaudio.functional import melscale_fbanks

_WHISPER_N_FFT = 400
_WHISPER_HOP = 160


def load_wav(path: str, target_sr: int) -> torch.Tensor:
    """(1, N) 单声道、重采样到 target_sr。soundfile 解码（torchaudio 2.10 的 load
    要 torchcodec；参考音频与语音输入均为 wav/flac，soundfile 足够且项目已在用）。"""
    import soundfile as sf

    data, sr = sf.read(str(path), dtype="float32", always_2d=True)
    wav = torch.from_numpy(data.T).mean(dim=0, keepdim=True)
    if sr != target_sr:
        wav = torchaudio.functional.resample(wav, sr, target_sr)
    return wav


@lru_cache(maxsize=4)
def _fbank(n_freqs: int, n_mels: int, sr: int, f_max, norm) -> torch.Tensor:
    return melscale_fbanks(n_freqs=n_freqs, f_min=0.0, f_max=f_max, n_mels=n_mels,
                           sample_rate=sr, norm=norm, mel_scale="slaney")


def whisper_log_mel(wav16: torch.Tensor) -> np.ndarray:
    """16k 单声道 (1, N) → float32 (1, 128, T)，speech tokenizer 的 feats 输入。

    与官方依赖 openai-whisper==20231117 的调用逐位对齐：
    ``log_mel_spectrogram(speech, n_mels=128)`` 默认 ``padding=0``——**不做任何
    补零**（tokenizer 的 encoder 是非因果全局注意力，补零长度不同会改变全部
    token 的值）；滤波器组 = librosa.filters.mel(16k, 400, 128)（slaney 尺度 +
    slaney 归一，torchaudio ``mel_scale="slaney", norm="slaney"`` 等价）。"""
    wav = wav16.flatten()
    window = torch.hann_window(_WHISPER_N_FFT, device=wav.device)
    stft = torch.stft(wav, _WHISPER_N_FFT, _WHISPER_HOP, window=window, return_complex=True)
    magnitudes = stft[..., :-1].abs() ** 2
    filters = _fbank(_WHISPER_N_FFT // 2 + 1, 128, 16000, 16000 / 2, "slaney")
    mel = filters.T @ magnitudes  # torchaudio fbank 是 (n_freq, n_mel)，librosa 语义是 (n_mel, n_freq)
    log_spec = torch.clamp(mel, min=1e-10).log10()
    log_spec = torch.maximum(log_spec, log_spec.max() - 8.0)
    log_spec = (log_spec + 4.0) / 4.0
    return log_spec.unsqueeze(0).numpy().astype(np.float32)


def kaldi_fbank80(wav16: torch.Tensor) -> np.ndarray:
    """16k 单声道 (1, N) → float32 (T, 80)，campplus 的 input。"""
    feat = kaldi.fbank(wav16, num_mel_bins=80, dither=0, sample_frequency=16000)
    feat = feat - feat.mean(dim=0, keepdim=True)
    return feat.numpy().astype(np.float32)


def matcha_mel_24k(wav24: torch.Tensor) -> torch.Tensor:
    """24k 单声道 (1, N) → (1, 80, T)（官方 _extract_speech_feat 再 squeeze/transpose）。"""
    n_fft, hop, win = 1920, 480, 1920
    filters = _fbank(n_fft // 2 + 1, 80, 24000, 8000.0, None).to(wav24.dtype)
    y = torch.nn.functional.pad(wav24, ((n_fft - hop) // 2,) * 2, mode="reflect")
    spec = torch.stft(y, n_fft, hop, win_length=win, window=torch.hann_window(win, device=y.device),
                      center=False, pad_mode="reflect", onesided=True, return_complex=True)
    spec = torch.sqrt(spec.abs() ** 2 + 1e-9)
    mel = filters.T @ spec  # (M, F) @ (1, F, T) → (1, M, T)
    return torch.log(torch.clamp(mel, min=1e-5))
