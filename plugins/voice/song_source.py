# -*- coding: utf-8 -*-
"""曲库在线源协议 + 本地源插件加载器。

设计（plan §5 在线曲库，2026-11-03 与用户对齐）：
- 仓库只含**协议与管线**；各网站实现（搜索/下载方式不同）放本地目录，
  默认 ``~/.omniagent/song_sources/``（配置 ``sing_sources_dir``），**不入仓**——
  公开仓滥用面红线：仓里没有可运行的"点歌下载器"，只有接口。
- 每个源是一个 .py 模块，暴露::

      SOURCE_ID: str
      def search(keyword: str, limit: int = 3) -> list[dict]
          # [{ "id": ..., "title": ..., "duration_s": ...}, ...] 按可信度排序
      def fetch(candidate: dict, out_dir: str) -> str
          # 下载该候选的音轨，落地为文件并返回路径

  异常一律抛给调用方；单个源模块加载失败 = 该源不可用，不影响其他源。
- 加载器无状态：每次 acquire 重新加载（本地模块可热改，不用重启网关）。
"""
import importlib.util
import os
from typing import Any, Dict, List

from utils import get_logger

logger = get_logger("voice.song_source")

_DEFAULT_SOURCES_DIR = os.path.join(os.path.expanduser("~"), ".omniagent", "song_sources")

#: 源模块必须暴露的接口（防御式校验，缺一样就跳过该模块）
_REQUIRED = ("SOURCE_ID", "search", "fetch")


def sources_dir() -> str:
    """本地源目录（配置 ``sing_sources_dir``，默认 ~/.omniagent/song_sources/）。"""
    from plugins.voice.sing_runtime import voice_cfg

    d = str(voice_cfg().get("sing_sources_dir") or _DEFAULT_SOURCES_DIR)
    return d


def online_enabled() -> bool:
    """在线曲库总闸（配置 ``sing_online``，默认关——拍板 §10.2：库外获取是 step 4 降级路径）。"""
    from plugins.voice.sing_runtime import voice_cfg

    return bool(voice_cfg().get("sing_online", False))


def load_sources() -> Dict[str, Any]:
    """加载本地目录下全部源模块，返回 {SOURCE_ID: module}。

    加载失败/接口缺失的模块记日志跳过；目录不存在 = 无源（空 dict）。
    """
    d = sources_dir()
    if not os.path.isdir(d):
        return {}
    out: Dict[str, Any] = {}
    for name in sorted(os.listdir(d)):
        if not name.endswith(".py") or name.startswith("_"):
            continue
        path = os.path.join(d, name)
        try:
            spec = importlib.util.spec_from_file_location(f"sing_source_{name[:-3]}", path)
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            missing = [k for k in _REQUIRED if not hasattr(mod, k)]
            if missing:
                logger.warning("曲库源 %s 缺接口 %s，跳过", name, missing)
                continue
            sid = str(mod.SOURCE_ID)
            if sid in out:
                logger.warning("曲库源 SOURCE_ID 重复：%s（%s），跳过后者", sid, name)
                continue
            out[sid] = mod
        except Exception as e:  # noqa: BLE001 —— 单源坏了不拖垮其他源
            logger.warning("曲库源 %s 加载失败，跳过: %s: %s", name, type(e).__name__, e)
    logger.info("曲库在线源已加载：%s", sorted(out) or "（无）")
    return out
