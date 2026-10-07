"""配置模块 - 加载 config.yaml + ~/.omniagent/config.yaml 覆盖

设计（对齐通用 agent「config 只放基础默认，完整配置放用户目录」模式）：
- ``config.yaml``（项目根）：基础/默认配置（依赖项、出厂默认值），可提交、
  可含明文 key 作默认值（但真 key 应移出仓库，见下）。
- ``~/.omniagent/config.yaml``：**用户全局配置**，启动时 deepMerge 覆盖项目 config。
  真 API key、``local_model.auto_start`` 等本机专属项只存这里，不进版本库。
- 加载顺序：base(项目 config.yaml) ← override(用户 ~/.omniagent/config.yaml)。
"""
import os
import json
import logging
import yaml
from typing import Dict, Any
from pathlib import Path

_logger = logging.getLogger("config")

_ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(_ROOT, "config.yaml")
# 用户全局配置：~/.omniagent/config.yaml（去明文 key，跨项目共享）
SETTINGS_PATH = os.path.join(os.path.expanduser("~"), ".omniagent", "config.yaml")
# 外部 MCP 配置：独立文件，不进 config.yaml（避免主配置被反复改动）
MCP_CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".omniagent", "mcp.json")
# 模型目录（提供方 / 模型清单）：独立文件，与通用配置隔离。
# 设计：厂商侧信息（base_url / api_key / 模型清单）属于「外部目录」而非引擎参数，
# 单独存放使 config.yaml 保持场景/厂商无关，也便于整体导入导出与备份。
MODELS_CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".omniagent", "models.json")

_config_cache: Dict[str, Any] = {}
_settings_cache: Dict[str, Any] = {}
_mcp_cache: Dict[str, Any] = {}
_models_cache: Dict[str, Any] = {}
_path_logged = False
_base_warned = False  # 项目级 config.yaml 废弃告警只打一次


def _log_path_once() -> None:
    """首次访问时打印设置文件路径，方便从日志核对持久化位置（排查「配置丢失」）。"""
    global _path_logged
    if not _path_logged:
        _path_logged = True
        _logger.info("用户设置文件: %s", SETTINGS_PATH)


def load_settings() -> Dict[str, Any]:
    """读取 ~/.omniagent/config.yaml（用户全局配置）。不存在返回空 dict。"""
    global _settings_cache
    _log_path_once()
    if _settings_cache:
        return _settings_cache
    try:
        if os.path.exists(SETTINGS_PATH):
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                _settings_cache = yaml.safe_load(f.read()) or {}
                return _settings_cache
    except Exception:
        pass
    _settings_cache = {}
    return _settings_cache


def save_settings(settings: Dict[str, Any]) -> None:
    """写回 ~/.omniagent/config.yaml，并失效 config 缓存。"""
    global _settings_cache, _config_cache
    _log_path_once()
    os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(settings, f, allow_unicode=True, sort_keys=False)
    _logger.info("已写回用户设置 -> %s", SETTINGS_PATH)
    _settings_cache = settings
    _config_cache = {}


def load_mcp_config() -> Dict[str, Any]:
    """读取外部 MCP 配置（独立存储于 ~/.omniagent/mcp.json）。

    优先读 mcp.json；旧位置 config.runtime.mcp 不再读取。
    返回形如 ``{"enabled": bool, "servers": [...]}`` 的 dict，缺失则为空 dict。
    """
    global _mcp_cache
    if _mcp_cache:
        return _mcp_cache
    try:
        if os.path.exists(MCP_CONFIG_PATH):
            with open(MCP_CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    _mcp_cache = data
                    return _mcp_cache
    except Exception:
        pass
    # 只认 ~/.omniagent/mcp.json（旧位置 config.runtime.mcp 不再读取）
    _mcp_cache = {}
    return _mcp_cache


def save_mcp_config(mcp: Dict[str, Any]) -> None:
    """写回 ~/.omniagent/mcp.json 并失效缓存（不再写入 config.yaml）。"""
    global _mcp_cache
    os.makedirs(os.path.dirname(MCP_CONFIG_PATH), exist_ok=True)
    with open(MCP_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(mcp, f, ensure_ascii=False, indent=2)
    _logger.info("已写回 MCP 配置 -> %s", MCP_CONFIG_PATH)
    _mcp_cache = mcp


def load_models_config() -> Dict[str, Any]:
    """读取模型目录 ~/.omniagent/models.json（``{"providers": {...}, "defaults": {...}}``）。

    与 MCP 配置同构：独立文件、独立缓存；文件缺失/损坏一律返回空 dict（不阻断启动）。
    """
    global _models_cache
    if _models_cache:
        return _models_cache
    try:
        if os.path.exists(MODELS_CONFIG_PATH):
            with open(MODELS_CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    _models_cache = data
                    return _models_cache
    except Exception:
        pass
    _models_cache = {}
    return _models_cache


def save_models_config(models: Dict[str, Any]) -> None:
    """写回 ~/.omniagent/models.json 并失效缓存。"""
    global _models_cache
    os.makedirs(os.path.dirname(MODELS_CONFIG_PATH), exist_ok=True)
    with open(MODELS_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(models, f, ensure_ascii=False, indent=2)
    _logger.info("已写回模型目录 -> %s", MODELS_CONFIG_PATH)
    _models_cache = models


def deep_merge(target: Dict[str, Any], source: Dict[str, Any]) -> Dict[str, Any]:
    """递归合并 source 到 target（source 优先）。返回新 dict。"""
    result = dict(target)
    for key, val in source.items():
        if (
            val is not None
            and isinstance(val, dict)
            and isinstance(result.get(key), dict)
        ):
            result[key] = deep_merge(result[key], val)
        else:
            result[key] = val
    return result


def load_config() -> Dict[str, Any]:
    """加载合并后配置：config.yaml 为 base，~/.omniagent/config.yaml 覆盖。"""
    global _config_cache
    if _config_cache:
        return _config_cache
    base: Dict[str, Any] = {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            base = yaml.safe_load(f.read()) or {}
    except FileNotFoundError:
        print("配置文件未找到")
        base = {}
    except Exception as e:  # noqa: BLE001
        print(f"配置加载失败: {e}")
        base = {}

    settings = load_settings()
    _config_cache = deep_merge(base, settings) if settings else base
    return _config_cache


def get_config(path: str = "", default: Any = None) -> Any:
    """点分路径取值，如 get_config('runtime.backend')。"""
    cfg = load_config()
    if not path:
        return cfg
    node: Any = cfg
    for k in path.split("."):
        if node is None or not isinstance(node, dict):
            return default
        node = node.get(k)
    return node if node is not None else default


def load_base_config() -> Dict[str, Any]:
    """只读项目根 config.yaml（**不含**用户 ~/.omniagent 覆盖）。

    供设置面板计算「用户改动差集」：写回用户文件时只持久化与出厂默认不同的键，
    避免把项目默认值整份快照进 ~/.omniagent/config.yaml（否则项目侧后续改动
    会被这份旧快照盖住，表现为「配置变回默认」）。

    注：项目级 config.yaml 不再生效（缺省值全部下沉到代码 CONTEXT_DEFAULTS，
    可调项一律走 Web 写入 ~/.omniagent）。文件存在时只 WARN 一次，不做报错。
    """
    global _base_warned
    if os.path.exists(CONFIG_PATH) and not _base_warned:
        _base_warned = True
        _logger.warning(
            "检测到项目级 %s —— 该文件不再生效：缺省值在代码里（config.CONTEXT_DEFAULTS），"
            "可调项请走 Web 设置面板（写入 %s）。它若含旧默认值会盖住代码缺省，建议删除。",
            CONFIG_PATH, SETTINGS_PATH,
        )
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f.read()) or {}
    except Exception:  # noqa: BLE001
        return {}


# ---------------------------------------------------------------------------
# 运行时缺省值（代码唯一出处）
# ---------------------------------------------------------------------------
# 项目根已无 config 文件：这些旋钮全部以代码为唯一出处，不依赖「配置里有没有写」。
# Web 面板只展示/修改生效值，落盘时与缺省相同的键一律剔除（见 load_effective_defaults）。
# 新增调优项请加在这里 + 在 Web 暴露入口，不要新建项目级 config 文件。
CONTEXT_DEFAULTS: Dict[str, Any] = {
    "brain": {
        # 模型输入预算（Token）：>0 启用 Model 适配层粘性压缩；0 = 显式关闭。
        # 当下主流大模型多为 1M 上下文，取 300000 起步（阈值 = 300k×0.5 = 150k）。
        "maxInputTokens": 300000,
        "long_task": {
            # 压缩后尾部保留比例（占压缩阈值的比例）
            "retain_ratio": 0.5,
            # Prune-First：压缩前对超长工具输出做首尾截断
            "prune": {
                "threshold_chars": 8192,
                "head_chars": 4096,
                "tail_chars": 1024,
            },
        },
    },
    "runtime": {
        # 执行后端：host（宿主机屏幕）| emulator（Android，需自行提供 adb_serial）
        "backend": "host",
        # 升级/终止判定（缺省见 tool_loop._DEFAULT_ESCALATION）
        "escalation": {
            "verify_fail_max": 3,       # verify 连续失败达此次数 → 升级
            "wallclock_sec": 120,       # 单步墙钟超时（秒）
            "no_confidence_hard": True, # 无置信度时按硬失败处理
            "worker_history_keep": 3,   # 升级时保留的子任务历史条数
        },
        # 内核工具限流（缺省见 shell_tool._LIMITS；配置缺项即用缺省，不会报错）
        # 插件自有配置（filesystem / web / vision …）一律不进本文件，见 ~/.omniagent/plugins/<name>.yaml
        "shell_exec": {"timeout_sec": 30.0, "max_output": 8000},
        # S0 权限模式与 S2 审批（随 run 快照进入 run，改后下次 run 生效）
        # （2026-10-03：S1 路径围栏整体删除，写入不设防——无 allow_write_roots）
        "security": {
            "mode": "standard",        # standard | read_only | full_access（full_access=全局免审批）
            "approval": {
                "wait_seconds": 600,   # 审批等待上限（秒）；0 = 无限等；超时按拒绝处理
            },
            "audit": True,             # 审计落盘开关（~/.omniagent/audit/YYYY-MM.jsonl）
        },
        "long_task": {
            "repeat_guard": 3,
            # F2.4 主链墙钟（秒）：0 = 不检查（大步数预算下默认不检查，避免被 120s 子任务墙钟腰斩）
            "wallclock_sec": 0,
            # F4.1b 纪律文件（AGENTS.md）：run 起始读一次并锁进 system prompt；false = 零注入
            "inject_instructions": True,
            # 注入块字符上限（纪律块与记忆块共用），超限截断并标注
            "instructions_limit": 8192,
        },
        # 知识层 gate（TAM 移植，现行架构见 doc/plans/memory-architecture.md）：
        # memory.enabled = 记忆轴总开关（注入 + L1 提炼 + L2 场景维护全链路；设置页「伙伴 → 记忆」）
        "knowledge": {
            "memory": {"enabled": False},
        },
    },
    # 知识层（TAM 移植；现行架构见 doc/plans/memory-architecture.md）
    "knowledge": {
        # 记忆检索注入 gate（memory_tam；池子 ≥20 条前零注入）
        "memory": {"enabled": False},
        # 画像：注入开关（默认开）+ 自动维护（新 atoms 攒够 N 条 → LLM 增量重写画像）
        "profile": {
            "enabled": True,
            "auto_maintain": True,
            "trigger_every_n": 20,
        },
        # 场景块（L2，scene_executor = TAM scene-extractor 移植）：
        # max_blocks 容量上限（TAM maxScenes 缺省 15，三级预警强制合并）；
        # prompt_mode 提炼家族开关（personal=场景知识；work 家族将来加 prompt 常量即接入）
        "scene": {
            "max_blocks": 15,
            "prompt_mode": "personal",
        },
    },
    # skill 轴 · 结构化宏提取开关（默认关：押的是回放省步价值假设，
    # 负结果纪律 2 连败停手）
    "skill": {
        "auto_distill": False,
        # 回放硬重放白名单（工具白名单制）：
        # 确定性工具可重放；GUI 坐标类不在列 → 降「参考建议」
        "replay_allow_tools": ["shell_exec", "read_file", "write_file",
                               "list_dir", "search_content"],
        # 缓存淘汰：candidate 过期 / active 闲置且低效用 → 移入 _archive/
        "evict_candidate_ttl_days": 14,
        "evict_active_idle_days": 30,
    },
}


def load_effective_defaults() -> Dict[str, Any]:
    """代码缺省为底、项目 config.yaml 覆盖之（用户层不参与）。

    用作落盘差集的基准：与「代码缺省 + 项目配置」相同的键都不写进用户文件，
    既保证用户文件干净，也避免「前端把 0 保存回去 = 静默关闭能力」。
    """
    return deep_merge(CONTEXT_DEFAULTS, load_base_config())


def reload_config() -> None:
    """失效缓存（测试或外部修改后强制重读）。"""
    global _config_cache, _settings_cache, _mcp_cache, _models_cache
    _config_cache = {}
    _settings_cache = {}
    _mcp_cache = {}
    _models_cache = {}
    load_config()


def get_cuda_path() -> str:
    """获取 CUDA 工具链根路径（config.cuda.path → 环境变量 CUDA_PATH）。"""
    cuda_path = get_config("cuda.path", "")
    if cuda_path:
        return cuda_path
    return os.environ.get("CUDA_PATH", "")
