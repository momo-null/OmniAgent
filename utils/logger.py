"""统一日志工具模块

提供全局统一的日志输出功能，支持控制台和文件输出，自动轮转日志文件。
"""
import os
import logging
from logging.handlers import RotatingFileHandler
from typing import Optional
import yaml


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
    log_file = logger_config.get("log_file", "./temp/omniagent.log")
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
