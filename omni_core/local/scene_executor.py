"""L2 多文件场景执行器 —— TAM scene-extractor 完整移植(agent 循环形态)。

机制总览见 doc/plans/memory-architecture.md §2.3;触发与水位在 memory_tam(薄壳)。
机制(TAM 原样):多文件主题块 ``scene_blocks/*.md`` + 带工具的整理 agent 循环
(工具白名单硬限 read/write/edit,workspace 钉死本目录)+ 三级容量预警(强制 MERGE)
+ 软删协议(写 ``[DELETED]``,工程侧事后 unlink)+ 文件名归一化 + ``scene_index.json``
(META 的投影缓存,维护后全量重建,唯一写入口在工程侧)。

尺寸取 TAM 产品默认:块 ≤1500 字符(prompt 约束)、max_blocks 15、迭代 ≤32、
墙钟 ≤300s——均为长期使用形态,``knowledge.scene.max_blocks`` 可配置。

失败契约:维护前整目录快照,任何异常整目录还原;清理/归一化/索引各阶段非致命;
任何失败不阻断主流程(flush 静默)。
"""
from __future__ import annotations

import json
import re
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from omni_core.brain.llm import LLMClient
from omni_core.local import runtime_paths as rp

# ---------------------------------------------------------------- 常量(TAM 值)

_META_START = "-----META-START-----"
_META_END = "-----META-END-----"
_DELETED = "[DELETED]"
_BLOCK_MAX_CHARS = 4000        # 工程硬上限(scene_write 拒绝超限;prompt 约束 1500)
_MAX_ITERATIONS = 32           # TAM max_iterations 上限
_WALL_SECONDS = 300.0          # TAM extract timeoutMs
_REQ_TIMEOUT_S = 300.0
_DEFAULT_MAX_BLOCKS = 15       # TAM maxScenes 缺省
#: 维护 agent 的 max_tokens 下限:推理模型的思考 token 也计入预算,
#: 主链的 2048 会出现「finish_reason=length 且零输出」(真机实测)——整理必须预留写块空间
_MIN_AGENT_MAX_TOKENS = 8192

_SCENE_READ_TOOL_CHARS = 4000  # read_scene 工具单块读取上限


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _max_blocks() -> int:
    try:
        import config
        return int(config.get_config("knowledge.scene.max_blocks", _DEFAULT_MAX_BLOCKS) or _DEFAULT_MAX_BLOCKS)
    except Exception:
        return _DEFAULT_MAX_BLOCKS


# ---------------------------------------------------------------- 块格式

def parse_scene_block(raw: str) -> Tuple[Dict[str, str], str]:
    """解析场景块:``META 区 + 正文``。无 META 降级(全文当正文,meta 全零值)。"""
    meta = {"created": "", "updated": "", "summary": "", "heat": 0}
    text = (raw or "").strip()
    start = text.find(_META_START)
    end = text.find(_META_END)
    if start == -1 or end == -1 or end < start:
        return meta, text
    meta_lines = text[start + len(_META_START):end].strip().splitlines()
    for ln in meta_lines:
        m = re.match(r"^(created|updated|summary|heat):\s*(.*)$", ln.strip())
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if key == "heat":
            try:
                meta["heat"] = int(val)
            except ValueError:
                meta["heat"] = 0
        else:
            meta[key] = val
    body = text[end + len(_META_END):].strip()
    return meta, body


# ---------------------------------------------------------------- 文件名归一化(TAM filename-normalizer)

def normalize_scene_filename(name: str) -> str:
    """场景块文件名归一化:允许中英文/数字/``-_ .``;多词用 ``-``;空名兜底 scene.md。"""
    base = re.sub(r"^.*[\\/]", "", str(name or ""))
    base = re.sub(r"\.md$", "", base, flags=re.IGNORECASE)
    base = re.sub(r"[\s\u00A0\u3000]+", "-", base)
    base = re.sub(r"[()\[\]{}<>'\"`,;:!?*|/\\=&%$#@^~+]", "", base)
    base = re.sub(r"-{2,}", "-", base)
    base = re.sub(r"_{2,}", "_", base)
    base = re.sub(r"\.{2,}", ".", base)
    base = re.sub(r"^[-_.]+|[-_.]+$", "", base)
    return (base or "scene") + ".md"


def scene_blocks_dir(project_id: str) -> Path:
    return rp.project_dir(project_id) / "memory" / "scene_blocks"


def scene_index_path(project_id: str) -> Path:
    return rp.project_dir(project_id) / "memory" / "scene_index.json"


def _resolve_block(scene_dir: Path, name: str) -> Path:
    """把模型给的文件名解析进沙箱:basename 归一 + 钉死目录(拒绝逃逸)。"""
    return scene_dir / normalize_scene_filename(name)


def _unique_path(scene_dir: Path, want: str) -> Path:
    """重名冲突追加 ``-2..-999``(防模型反复产出同名)。"""
    p = scene_dir / want
    if not p.exists():
        return p
    stem = want[:-3]
    for i in range(2, 1000):
        cand = scene_dir / f"{stem}-{i}.md"
        if not cand.exists():
            return cand
    return scene_dir / f"{stem}-overflow.md"


# ---------------------------------------------------------------- 索引(scene_index.json = META 投影缓存)

def sync_scene_index(project_id: str) -> List[Dict[str, Any]]:
    """全量扫 scene_blocks/*.md 重建索引(唯一写入口;LLM 沙箱碰不到该文件)。"""
    scene_dir = scene_blocks_dir(project_id)
    entries: List[Dict[str, Any]] = []
    for p in sorted(scene_dir.glob("*.md")):
        try:
            meta, _ = parse_scene_block(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        entries.append({"filename": p.name, "summary": meta["summary"], "heat": meta["heat"],
                        "created": meta["created"], "updated": meta["updated"]})
    entries.sort(key=lambda e: (-int(e["heat"]), e["filename"]))
    idx = scene_index_path(project_id)
    idx.parent.mkdir(parents=True, exist_ok=True)
    idx.write_text(json.dumps({"version": 1, "scenes": entries}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    return entries


def read_scene_index(project_id: str) -> List[Dict[str, Any]]:
    """读索引(防御性解析;缺失/损坏 → 全量重建一次自愈)。"""
    idx = scene_index_path(project_id)
    entries: List[Dict[str, Any]] = []
    if idx.exists():
        try:
            data = json.loads(idx.read_text(encoding="utf-8"))
            for e in (data or {}).get("scenes") or []:
                if not isinstance(e, dict) or not str(e.get("filename") or ""):
                    continue
                try:
                    heat = int(e.get("heat") or 0)
                except (TypeError, ValueError):
                    heat = 0
                entries.append({"filename": str(e["filename"]),
                                "summary": str(e.get("summary") or ""),
                                "heat": heat,
                                "created": str(e.get("created") or ""),
                                "updated": str(e.get("updated") or "")})
        except Exception:
            entries = []
    if not entries and scene_blocks_dir(project_id).glob("*.md"):
        entries = sync_scene_index(project_id)
    return entries


# ---------------------------------------------------------------- 沙箱工具(3 个,白名单硬限)

_SCENE_TOOLS = [
    {"type": "function", "function": {
        "name": "scene_read",
        "description": "读取一个场景块的完整内容(META+正文)。只能读「已有场景文件清单」中列出的文件。",
        "parameters": {"type": "object", "properties": {
            "scene_file": {"type": "string", "description": "场景块文件名(如 windows-环境事实.md)"},
        }, "required": ["scene_file"]},
    }},
    {"type": "function", "function": {
        "name": "scene_write",
        "description": ("整体写入/新建场景块(content 为完整文件内容:META+正文);"
                        "删除场景块时 content 写 [DELETED]。禁止写空字符串。"),
        "parameters": {"type": "object", "properties": {
            "scene_file": {"type": "string", "description": "场景块文件名(新建或覆盖)"},
            "content": {"type": "string", "description": "完整文件内容(META+正文,或 [DELETED])"},
        }, "required": ["scene_file", "content"]},
    }},
    {"type": "function", "function": {
        "name": "scene_edit",
        "description": "局部替换场景块中的一个片段(小改动用;大改/结构变更请 scene_read 后 scene_write 整体重写)。",
        "parameters": {"type": "object", "properties": {
            "scene_file": {"type": "string", "description": "已存在的场景块文件名"},
            "old_text": {"type": "string", "description": "要替换的原文片段"},
            "new_text": {"type": "string", "description": "替换后的文本(可为空串=删除片段)"},
        }, "required": ["scene_file", "old_text", "new_text"]},
    }},
]


def _op_scene_read(args: Dict[str, Any], scene_dir: Path) -> Dict[str, Any]:
    p = _resolve_block(scene_dir, str(args.get("scene_file") or ""))
    if not p.exists():
        return {"ok": False, "error": f"场景块不存在: {p.name}"}
    return {"ok": True, "content": p.read_text(encoding="utf-8")[:_SCENE_READ_TOOL_CHARS]}


def _op_scene_write(args: Dict[str, Any], scene_dir: Path) -> Dict[str, Any]:
    content = str(args.get("content") or "")
    if not content.strip():
        return {"ok": False, "error": "拒绝空内容(删除场景块请写 [DELETED])"}
    if len(content) > _BLOCK_MAX_CHARS:
        return {"ok": False,
                "error": f"内容 {len(content)} 字符超上限 {_BLOCK_MAX_CHARS},请精简或合并场景"}
    p = _resolve_block(scene_dir, str(args.get("scene_file") or ""))
    scene_dir.mkdir(parents=True, exist_ok=True)
    p.write_text(content.strip() + "\n", encoding="utf-8")
    return {"ok": True, "file": p.name, "chars": len(content.strip())}


def _op_scene_edit(args: Dict[str, Any], scene_dir: Path) -> Dict[str, Any]:
    p = _resolve_block(scene_dir, str(args.get("scene_file") or ""))
    if not p.exists():
        return {"ok": False, "error": f"场景块不存在: {p.name}"}
    raw = p.read_text(encoding="utf-8")
    old = str(args.get("old_text") or "")
    if old not in raw:
        return {"ok": False, "error": "old_text 未在文件中找到"}
    p.write_text(raw.replace(old, str(args.get("new_text") or ""), 1), encoding="utf-8")
    return {"ok": True, "file": p.name}


#: 沙箱操作表(字典派发——内核不对具体工具名做 if 分支,review_lint R2)
_SCENE_OPS = {
    "scene_read": _op_scene_read,
    "scene_write": _op_scene_write,
    "scene_edit": _op_scene_edit,
}


def _scene_dispatch(name: str, args: Dict[str, Any], scene_dir: Path) -> Dict[str, Any]:
    """沙箱派发:白名单操作表之外的名称一律拒绝。"""
    try:
        op = _SCENE_OPS.get(str(name or ""))
        if op is None:
            return {"ok": False, "error": f"未知操作: {name}"}
        return op(args or {}, scene_dir)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _agent_cfg(brain_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """维护 agent 的请求配置:caller 的 max_tokens 低于下限时抬高(只改副本)。"""
    cfg = dict(brain_cfg or {})
    req = dict(cfg.get("request") or {})
    try:
        if int(req.get("max_tokens", 0) or 0) < _MIN_AGENT_MAX_TOKENS:
            req["max_tokens"] = _MIN_AGENT_MAX_TOKENS
            cfg["request"] = req
    except (TypeError, ValueError):
        pass
    return cfg


def _run_agent(brain_cfg: Dict[str, Any], system: str, user: str,
               scene_dir: Path, on_debug=None) -> Tuple[str, int]:
    """TAM CleanContextRunner 等价物:最小工具循环,白名单 3 操作,沙箱钉死。

    返回 ``(最终纯文本, 工具调用次数)``;LLM 调用失败抛异常(由调用方整目录还原)。
    """
    client = LLMClient(_agent_cfg(brain_cfg), timeout=_REQ_TIMEOUT_S, on_debug=on_debug)
    messages: List[Dict[str, Any]] = [{"role": "system", "content": system},
                                      {"role": "user", "content": user}]
    started = time.time()
    ops = 0
    for _ in range(_MAX_ITERATIONS):
        reply = client.chat(messages, tools=_SCENE_TOOLS)
        if not reply.tool_calls:
            return reply.content or "", ops
        calls = []
        for j, tc in enumerate(reply.tool_calls):
            calls.append({"id": tc.id or f"call_{ops}_{j}", "type": "function",
                          "function": {"name": tc.name,
                                       "arguments": json.dumps(tc.args, ensure_ascii=False)}})
        messages.append({"role": "assistant", "content": reply.content or "",
                         "tool_calls": calls})
        for c in calls:
            try:
                args = json.loads(c["function"]["arguments"]) if c["function"]["arguments"] else {}
            except ValueError:
                args = {}
            res = _scene_dispatch(c["function"]["name"], args, scene_dir)
            messages.append({"role": "tool", "tool_call_id": c["id"],
                             "content": json.dumps(res, ensure_ascii=False)})
            ops += 1
        if time.time() - started > _WALL_SECONDS:
            break
    return "", ops


# ---------------------------------------------------------------- 工程侧流水(清理/归一化/索引)

def _cleanup_blocks(scene_dir: Path) -> List[str]:
    """软删清理:``[DELETED]``/空文件/仅 META 无正文 → unlink(TAM Phase 5)。"""
    removed: List[str] = []
    if not scene_dir.exists():
        return removed
    for p in sorted(scene_dir.glob("*.md")):
        try:
            raw = p.read_text(encoding="utf-8").strip()
        except Exception:
            continue
        meta, body = parse_scene_block(raw)
        if not raw or raw == _DELETED or raw.startswith(_DELETED) or not body.strip():
            try:
                p.unlink()
                removed.append(p.name)
            except Exception:
                pass
    return removed


def _normalize_scene_filenames(scene_dir: Path) -> List[Tuple[str, str]]:
    """文件名归一化(幂等;TAM Phase 6)——保证索引只见规范名。"""
    renamed: List[Tuple[str, str]] = []
    if not scene_dir.exists():
        return renamed
    for p in sorted(scene_dir.glob("*.md")):
        want = normalize_scene_filename(p.name)
        if want == p.name:
            continue
        target = _unique_path(scene_dir, want)
        try:
            p.rename(target)
            renamed.append((p.name, target.name))
        except Exception:
            pass
    return renamed


def parse_persona_update_signal(text: str) -> str:
    """带外信号(L2→L3 协调):场景 agent 请求画像重大更新,返回 reason(无则空)。"""
    t = str(text or "")
    m = re.search(r"\[PERSONA_UPDATE_REQUEST\]\s*(?:reason:\s*)?(.+?)\s*\[/PERSONA_UPDATE_REQUEST\]",
                  t, re.S)
    if m:
        return m.group(1).strip()
    m = re.search(r"PERSONA_UPDATE_REQUEST:\s*(.+?)(?:\n|$)", t)
    return m.group(1).strip() if m else ""


# ---------------------------------------------------------------- prompt(personal 家族;prompt_mode 开关预置)

_SCENE_PROMPTS: Dict[str, str] = {
    "personal": (
        "# 场景知识整理架构师(TAM Memory Consolidation 移植)\n\n"
        "## 角色\n"
        "你负责把碎片化的新证据整合进场景知识块(scene_blocks/*.md),为 agent 维护一套"
        "**可跨任务复用**的场景知识库:环境事实、App 怪癖、UI 语义、可靠操作方式。\n\n"
        "## 内容纪律(红线,现文里已有的一并剔除)\n"
        "- 只保留可跨任务复用的知识:环境事实、App 怪癖、UI 语义、可靠操作方式、用户长期偏好;\n"
        "- 禁止角色形象/人设/自我介绍(那属于用户画像);禁止对话寒暄;\n"
        "- 禁止单次任务的过程流水账(某次执行做了哪几步、某次扫描的结果数字);\n"
        "- 禁止「当前任务目标」等易变状态——只写稳定的场景知识。\n\n"
        "## 架构模型\n"
        "- Layer 1 输入:新证据(会话要点与任务世界状态,碎片化、无序);\n"
        "- Layer 2 输出:场景块——**不是清单,是连贯的知识文档**;\n"
        "- 动作只有 Integrate(更新)/ Rewrite(重写)/ Merge(合并)/ Create(创建);禁止简单追加。\n\n"
        "## 文件操作约束(必须严格遵守)\n"
        "1. 只能使用 scene_read / scene_write / scene_edit 三个工具,文件名用相对名;\n"
        "2. scene_read 只能读「已有场景文件清单」列出的文件,禁止猜测或编造;\n"
        "3. 新建/整体重写用 scene_write(path=文件名, content=完整内容含 META);小改动用 scene_edit;\n"
        "4. 删除的唯一方式:scene_write 将内容写为 [DELETED](系统自动清理);"
        "禁止写空字符串;禁止用 [ARCHIVE] 等替代标记;\n"
        "5. 禁止创建报告/汇总类文件;文件名以 .md 结尾,只允许中英文/数字/-_.,多词用 - 连接。\n\n"
        "## 工作流(执行前先完成思维链)\n"
        "### 阶段 0:容量检查(必须先做)\n"
        "统计「现有场景块摘要」顶部标注的场景总数,遵守分级预警:\n"
        "- 红色(≥ 上限):必须先 MERGE(2-4 合 1)减少数量,再处理新证据;\n"
        "- 橙色(= 上限-1):只能 UPDATE,不能 CREATE;\n"
        "- 黄色(接近上限):优先 UPDATE 或 MERGE 相似场景。\n"
        "合并优先级:主题高度重叠 > 叙事弧线相同 > heat 最低的 2-3 个。\n"
        "### 阶段 1:分析新证据,提炼可复用要点\n"
        "任务叙述本身是流水账,但其中常埋有可复用要点(命令写法、超时设置、转义坑、"
        "路径习惯、工具链事实等)——你的职责是把**要点**提炼进对应场景块,"
        "而不是因「证据来自单次任务」就整体不写;结果数字只有作为基准值可复用时才留。\n"
        "### 阶段 2:选择策略(优先级从高到低)\n"
        "- **UPDATE 首选**:默认策略是 UPDATE 不是 CREATE,犹豫时选 UPDATE;heat = 旧 heat + 1;\n"
        "- **MERGE**:合并后 heat = 所有相关块 heat 之和 + 1;"
        "**必须把每个旧块逐个 scene_write 写为 [DELETED]**,不写 = 合并无效;\n"
        "- **CREATE**:新证据揭示了尚无场景块覆盖的新主题(新磁盘/新项目/新工具链)时,"
        "CREATE 是正确选择;总数未达上限才允许;CREATE 前必须 scene_read 至少 2 个最相似场景"
        "确认无归属;每次维护最多新增 1 个场景;新块 heat = 1。\n"
        "### 阶段 3:深度整合\n"
        "严禁把新证据追加到块尾了事;冲突不直接覆盖,记入「待确认/矛盾点」。\n\n"
        "## 场景块文件模板\n"
        f"{_META_START}\n"
        "created: <已有块保持原值;新建用当前时间>\n"
        "updated: <当前时间>\n"
        "summary: <30-60 字摘要,供导航索引>\n"
        "heat: <整数,按上述规则>\n"
        f"{_META_END}\n\n"
        "## 环境事实\n"
        "## 怪癖与坑\n"
        "## 可靠操作方式\n"
        "## 核心叙事\n"
        "## 待确认/矛盾点\n"
        "(可空章节直接删掉;正文短陈述句,每条一行;单块 ≤1500 字符)\n\n"
        "## 完成方式\n"
        "整理完成后,用纯文本简短汇报做了什么(创建/更新/合并/删除了哪些块),无需输出 JSON。\n"
        "仅当确有跨场景重大洞察、认为用户画像需要重大更新时,在汇报末尾输出:\n"
        "[PERSONA_UPDATE_REQUEST]\n"
        "reason: 具体原因\n"
        "[/PERSONA_UPDATE_REQUEST]\n"
    ),
}


def _scene_system_prompt() -> str:
    """按 ``knowledge.scene.prompt_mode`` 选 prompt 家族(未知值回退 personal)。"""
    try:
        import config
        mode = str(config.get_config("knowledge.scene.prompt_mode", "personal") or "personal")
    except Exception:
        mode = "personal"
    return _SCENE_PROMPTS.get(mode) or _SCENE_PROMPTS["personal"]


def _capacity_warning(n: int, max_blocks: int) -> str:
    if n >= max_blocks:
        return (f"红色预警:场景总数 {n} 已达上限 {max_blocks}。"
                f"本轮**第一优先动作**必须是整理合并:先 scene_read 相似块,"
                f"再 MERGE(2-4 合 1)把数量降到上限以下,并逐个 scene_write 旧块为 [DELETED];"
                f"合并完成之前不得输出任何汇报")
    if n == max_blocks - 1:
        return (f"橙色预警:场景总数 {n} 已达 {max_blocks - 1},本轮只能 UPDATE 现有场景,"
                f"禁止 CREATE 新场景")
    if n >= max_blocks - 3:
        return f"黄色预警:场景总数 {n} 接近上限 {max_blocks},优先 UPDATE 或 MERGE 相似场景"
    return ""


# ---------------------------------------------------------------- 维护入口

def maintain_scenes(project_id: str, brain_cfg: Optional[Dict[str, Any]], evidence: str,
                    task_id: str = "", stats: Optional[Dict[str, Any]] = None,
                    on_debug=None) -> Dict[str, Any]:
    """L2 维护主流程:快照 → agent 循环 → 清理/归一化/索引 → (失败)整目录还原。

    ``evidence`` 为新证据文本(会话要点 + 世界状态,由调用方组装)。
    stats 回传:created/updated/deleted/renamed、empty_maintenance、reply_preview
    (模型最终汇报,供观测"模型为何不动手")、persona_update_request。
    """
    stats = stats if stats is not None else {}
    if not brain_cfg or not (brain_cfg.get("base_url") and brain_cfg.get("model")):
        return stats
    scene_dir = scene_blocks_dir(project_id)
    pre_files = {p.name: p for p in scene_dir.glob("*.md")} if scene_dir.exists() else {}
    if not evidence.strip() and not pre_files:
        return stats  # 无现有块也无证据 → 没有可维护的内容

    pre_index = {e["filename"]: e for e in read_scene_index(project_id)}
    pre_hash = {p.name: p.stat().st_mtime_ns for p in pre_files.values()}

    backup = None
    try:
        if scene_dir.exists():
            backup = Path(tempfile.mkdtemp(prefix="scene_bak_")) / "scene_blocks"
            shutil.copytree(scene_dir, backup)

        entries = read_scene_index(project_id)
        n = len(pre_files)
        warning = _capacity_warning(n, _max_blocks())
        summary_lines = [f"**当前场景总数:{n} / {_max_blocks()}**"]
        for e in entries:
            summary_lines.append(f"### {e['filename']}")
            summary_lines.append(f"**热度**: {e['heat']} | **更新**: {e['updated']}")
            summary_lines.append(f"**summary**: {e['summary']}")
        file_list = "\n".join(f"- `{name}`" for name in sorted(pre_files)) or "(当前无已有场景文件)"
        user = (
            "**输出语言**:与「新证据」相同的语言。\n\n"
            + (f"⚠️ **场景数量警告**: {warning}\n\n" if warning else "")
            + f"### 1️⃣ 新证据\n{evidence.strip()[:5500]}\n\n"
            + f"### 2️⃣ 现有场景块摘要\n" + "\n".join(summary_lines) + "\n\n"
            + f"### 3️⃣ 当前时间\n{_now()}\n\n"
            + f"### 📁 已有场景文件清单(仅以下文件可 scene_read)\n{file_list}"
        )
        final_text, _ops = _run_agent(brain_cfg, _scene_system_prompt(), user, scene_dir,
                                      on_debug=on_debug)

        # 工程侧流水:各阶段非致命(TAM Phase 5-7)
        removed = _cleanup_blocks(scene_dir)
        renamed = _normalize_scene_filenames(scene_dir)
        sync_scene_index(project_id)

        post_files = {p.name: p for p in scene_dir.glob("*.md")} if scene_dir.exists() else {}
        created = sorted(set(post_files) - set(pre_hash))
        deleted = sorted(set(pre_hash) - set(post_files))
        body_changed = [name for name in set(post_files) & set(pre_hash)
                        if post_files[name].stat().st_mtime_ns != pre_hash[name]]
        # removed 与 deleted 有交集(先前存在的软删块),只补「本轮新建又本轮清理」的暂态
        stats.update({
            "created": len(created), "updated": len(body_changed),
            "deleted": len(deleted) + len([r for r in removed if r not in pre_hash]),
            "renamed": len(renamed),
            "empty_maintenance": not (created or deleted or body_changed or removed),
            "reply_preview": (final_text or "")[:300],
        })
        if final_text:
            signal = parse_persona_update_signal(final_text)
            if signal:
                stats["persona_update_request"] = signal[:500]
        return stats
    except Exception as e:  # 整目录还原,不掩盖原始失败,也不阻断主流程
        stats["error"] = f"{type(e).__name__}: {e}"
        if backup is not None and backup.exists():
            try:
                if scene_dir.exists():
                    shutil.rmtree(scene_dir)
                shutil.copytree(backup, scene_dir)
                sync_scene_index(project_id)
            except Exception:
                pass
        return stats
    finally:
        if backup is not None and backup.parent.exists():
            shutil.rmtree(backup.parent, ignore_errors=True)


# ---------------------------------------------------------------- 消费端(导航 / 按需读取)

def load_scene_nav(project_id: str, max_entries: int = 20) -> List[Dict[str, Any]]:
    """L2 导航段(system 常驻):全部场景块的 摘要+热度(heat 降序)。全文按需 read_scene。"""
    try:
        entries = [e for e in read_scene_index(project_id) if e.get("summary")]
        return entries[:max_entries]
    except Exception:
        return []


def read_scene_block(project_id: str, name: str) -> str:
    """L2 按需读取端(read_scene 工具用):单块全文,硬上限 _BLOCK_MAX_CHARS。"""
    try:
        p = _resolve_block(scene_blocks_dir(project_id), name)
        if not p.exists():
            return ""
        return p.read_text(encoding="utf-8").strip()[:_BLOCK_MAX_CHARS]
    except Exception:
        return ""


def list_scene_blocks(project_id: str) -> List[Dict[str, Any]]:
    """read_scene 空参时的可用块清单(即索引)。"""
    return load_scene_nav(project_id, max_entries=100)
