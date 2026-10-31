"""GPT-SoVITS 引擎适配包（薄封装，无 vendored 推理代码）。

与 cosyvoice2_rt（已删）不同：GS 的推理代码不进仓库，直接用
``external/GPT-SoVITS``（上游 clone，gitignore 内）的 ``TTS_infer_pack``，
本包只做三方法契约适配 + 路径拼装。上游许可证见 external/GPT-SoVITS/LICENSE（MIT）。
"""
from .engine import GptSovitsEngine

__all__ = ["GptSovitsEngine"]
