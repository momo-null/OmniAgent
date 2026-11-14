"""统一日志工具模块

提供全局统一的日志输出功能，支持控制台和文件输出，自动轮转日志文件。
**每进程独占一份日志文件**（2026-11-14）：Windows 下两个进程不能共享同一个
RotatingFileHandler 目标文件——先到者握住句柄，后到者轮转改名永远 PermissionError，
且 rollover 失败的那条记录整条丢弃（曾致 backend/网关共享一份文件丢日志 7 天）。
入口进程自报家门（``OMNI_LOG_TAG``，见 backend/server.py 与
harness/channel_gateway.py 顶部），未标识的进程按 pid 兜底隔离。
"""
import glob
import os
import logging
import time
from logging.handlers import RotatingFileHandler
from typing import Optional
import yaml

#: 仓库根锚点：把 cwd 相对路径钉成绝对路径（不同启动方式不再各建各的 temp/）
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _resolve_log_file(base: str) -> str:
    """日志文件路径解析：tag / pid 后缀 + 仓库根锚定（见模块 docstring）。"""
    if not os.path.isabs(base):
        base = os.path.join(_REPO_ROOT, base)
    base = os.path.normpath(base)
    tag = os.environ.get("OMNI_LOG_TAG") or f"p{os.getpid()}"
    stem, ext = os.path.splitext(base)
    return f"{stem}-{tag}{ext}"


def _prune_stale_pid_logs(base: str, keep_days: int = 2) -> None:
    """清掉过期的 pid 兜底日志（仅 pid 兜底进程自查）。

    活进程的日志被自身句柄锁着，Windows 上删不动——天然不会误删在用文件。
    """
    stem, ext = os.path.splitext(os.path.abspath(base))
    cutoff = time.time() - keep_days * 86400
    for p in glob.glob(glob.escape(f"{stem}-p") + "*" + glob.escape(ext)):
        try:
            if os.path.getmtime(p) < cutoff:
                os.remove(p)
        except OSError:
            pass


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """获取全局日志实例

    Args:
        name: 日志实例名称，默认使用root logger

    Returns:
        配置完成的日志实例
    """
    # 加载配置：项目根 config.yaml 是**可选**的出厂默认
    # （不存在时全部走代码缺省 + ~/.omniagent/config.yaml，避免「删掉配置文件就起不来」）。
    config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.yaml")
    config = {}
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
    except (FileNotFoundError, OSError):
        config = {}
    if not isinstance(config, dict):
        config = {}

    logger_config = config.get("logger", {})
    log_level = logger_config.get("level", "INFO")
    _base = os.path.join(_REPO_ROOT, logger_config.get("log_file", "./temp/omniagent.log"))
    log_file = _resolve_log_file(_base)
    if not os.environ.get("OMNI_LOG_TAG"):
        _prune_stale_pid_logs(_base)
    max_file_size = logger_config.get("max_file_size", "10MB")
    backup_count = logger_config.get("backup_count", 5)

    # 解析文件大小
    size_suffix = max_file_size[-2:].lower()
    size_value = int(max_file_size[:-2])
    if size_suffix == "kb":
        max_bytes = size_value * 1024
    elif size_suffix == "mb":
        max_bytes = size_value * 1024 * 1024
    elif size_suffix == "gb":
        max_bytes = size_value * 1024 * 1024 * 1024
    else:
        max_bytes = 10 * 1024 * 1024

    # 创建日志目录
    os.makedirs(os.path.dirname(log_file), exist_ok=True)

    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, log_level.upper()))

    # 避免重复添加handler
    if logger.handlers:
        return logger

    # 日志格式
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # 控制台输出
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # 文件输出
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger
