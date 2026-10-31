"""TTS 输出频响补偿（与具体引擎无关的通用后处理）。

零样本克隆对参考音频很敏感：参考若是窄带 / 变声器 / 低码率来源，合成结果常
出现「中频塌陷 + 高频嘶声」（500-1000Hz 人声厚度区掉一半、4k 以上多出十倍），
听感就是"老式收音机"。这里按（中心频率, 增益dB, 对数频率带宽）做平滑钟形增益，
再对某频率之上做固定斜率缓降。

曲线是**通用语音清晰度补偿**，不含任何音色 / 场景信息（换引擎也适用）。
"""
from __future__ import annotations

#: （中心频率 Hz, 增益 dB, 对数频率带宽）——正增益补厚度，负增益压箱音/刺耳
EQ_BANDS = (
    (340.0, -2.0, 0.28),     # 200-500Hz 箱音轻微收敛
    (800.0, 7.0, 0.22),      # 500-1100Hz 人声厚度补回
    (3200.0, -2.0, 0.20),    # 2.5-4kHz 刺耳感轻压
)
#: 此频率以上开始缓降（压编码 / 变声器嘶声）
EQ_ROLLOFF_HZ = 4000.0
EQ_ROLLOFF_DB_PER_DECADE = 26.0
EQ_ROLLOFF_MAX_DB = 12.0
#: 输出峰值（留 1dB 余量，避免转码削波）
EQ_PEAK = 0.9


def equalize(samples, sr: int):
    """波形 → 频响补偿后的波形（同长度 float32）。空输入原样返回。"""
    import numpy as np

    n = len(samples)
    if n == 0:
        return samples
    spec = np.fft.rfft(samples)
    freqs = np.fft.rfftfreq(n, 1.0 / sr)
    lf = np.log10(np.maximum(freqs, 1.0))
    gain_db = np.zeros_like(freqs)
    for center, gain, width in EQ_BANDS:
        gain_db += gain * np.exp(-0.5 * ((lf - np.log10(center)) / width) ** 2)
    hi = freqs > EQ_ROLLOFF_HZ
    gain_db[hi] -= np.clip(
        (lf[hi] - np.log10(EQ_ROLLOFF_HZ)) * EQ_ROLLOFF_DB_PER_DECADE,
        0.0, EQ_ROLLOFF_MAX_DB)
    out = np.fft.irfft(spec * (10.0 ** (gain_db / 20.0)), n)
    peak = float(np.abs(out).max())
    if peak > 0:
        out = out / peak * EQ_PEAK
    return out.astype(np.float32)
