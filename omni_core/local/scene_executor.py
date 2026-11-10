"""L2 多文件场景执行器 —— TAM scene-extractor 完整移植(agent 循环形态)。

机制总览见 doc/plans/implemented/memory-architecture.md §2.3;触发与水位在本文件 ``maybe_maintain``
(薄壳),两份水位都存 ``scene_index.json``:时间节流位 ``last_maintain_ts`` 与进料游标位
``last_l2_atom_ts``。
机制(TAM 原样):多文件主题块 ``scene_blocks/*.md`` + 带工具的整理 agent 循环
(工具白名单硬限 read/write/edit,workspace 钉死本目录)+ 三级容量预警(强制 MERGE)
+ 软删协议(写 ``[DELETED]``,工程侧事后 unlink)+ 文件名归一化 + ``scene_index.json``
(META 的投影缓存,维护后全量重建,唯一写入口在工程侧)。

**进料 = L1 原子增量**(2026-11-05 对齐上游 ``utils/pipeline-factory.ts:826``:
``queryMemoryRecords({updatedAfter: cursor})`` → ``extractor.extract(memories)``)。
本层不直接吃会话原文或任务世界状态:一段经历要先经 L1 抽取/去重/合并/冲突裁决才进得来,
这是上游防止场景块无限长的主闸——块是原子库的蒸馏物,不是叙事第二抄本。
叙事层(``diary-*``,本仓库自创、非 TAM)走 L0,与本层无交集。

尺寸取 TAM 产品默认:块 ≤1500 字符(prompt 约束)、max_blocks 15、迭代 ≤32、
墙钟 ≤300s——均为长期使用形态,``knowledge.scene.max_blocks`` 可配置。
工程硬上限 4000 由 scene_write 与 scene_edit 两路共同守(2026-11-05 补齐 edit 一路:
只拦"编辑后更长且超上限",缩量与整体精简照常放行),读取端不截断(TAM read 不截断)。

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
_BLOCK_MAX_CHARS = 4000        # 工程硬上限(scene_write 与 scene_edit 两路共拒超限;prompt 约束 1500)
_MAX_ITERATIONS = 32           # TAM max_iterations 上限
_WALL_SECONDS = 300.0          # TAM extract timeoutMs
_REQ_TIMEOUT_S = 300.0
_DEFAULT_MAX_BLOCKS = 15       # TAM maxScenes 缺省
#: 维护 agent 的 max_tokens 下限:推理模型的思考 token 也计入预算,
#: 主链的 2048 会出现「finish_reason=length 且零输出」(真机实测)——整理必须预留写块空间
_MIN_AGENT_MAX_TOKENS = 8192

#: 单次维护最多带多少条 L1 原子进 prompt(进料游标按 updated 升序消费, backlog 分批走)
_L2_ATOMS_PER_RUN = 40


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _max_blocks() -> int:
    try:
        import config
        return int(config.get_config("knowledge.scene.max_blocks", _DEFAULT_MAX_BLOCKS) or _DEFAULT_MAX_BLOCKS)
    except Exception:
        return _DEFAULT_MAX_BLOCKS


# ---------------------------------------------------------------- 块格式

#: 日记块（plan persona-social-memory）：巩固期生成、维护 agent 只读、导航只露最近 7 篇
_DIARY_PREFIX = "diary-"
_DIARY_NAV_WINDOW = 7

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


def diary_dir() -> Path:
    """全局叙事目录（日记 / 月记一天一篇，跨项目统一；plan §3.3 / §11 H-4）。"""
    return rp.global_diary()


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
    """全量扫 scene_blocks/*.md 重建索引(唯一写入口;LLM 沙箱碰不到该文件)。

    非 ``scenes`` 的同级键(水位等,L2 自有簿记)原样保留。
    """
    scene_dir = scene_blocks_dir(project_id)
    entries: List[Dict[str, Any]] = []
    for p in sorted(scene_dir.glob("*.md")):
        # H-4：日记/月记已迁到全局叙事目录，项目索引不再投影它们（老残留也不收）
        if p.name.startswith(_DIARY_PREFIX):
            continue
        try:
            meta, _ = parse_scene_block(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        entries.append({"filename": p.name, "summary": meta["summary"], "heat": meta["heat"],
                        "created": meta["created"], "updated": meta["updated"]})
    entries.sort(key=lambda e: (-int(e["heat"]), e["filename"]))
    doc = _read_index_doc(project_id)
    doc["version"] = 1
    doc["scenes"] = entries
    _write_index_doc(project_id, doc)
    return entries


# ---- 水位(L2 自有簿记,存 scene_index.json;不依赖任何记忆库) ----
_L2_MIN_INTERVAL_S = 900    # 最小维护间隔(对齐 TAM l2MinIntervalSeconds)
_INDEX_LAST_MAINTAIN = "last_maintain_ts"
#: 进料游标(对齐 TAM checkpoint.last_extraction_updated_time):上次维护消费到的 L1
#: 原子 updated 值。与时间节流位分工不同——前者是"隔多久动一次",后者是"喂到哪儿了"。
_INDEX_LAST_ATOM_TS = "last_l2_atom_ts"


def _read_index_doc(project_id: str) -> Dict[str, Any]:
    idx = scene_index_path(project_id)
    if not idx.exists():
        return {}
    try:
        data = json.loads(idx.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_index_doc(project_id: str, doc: Dict[str, Any]) -> None:
    idx = scene_index_path(project_id)
    idx.parent.mkdir(parents=True, exist_ok=True)
    idx.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")


def get_watermark(project_id: str, key: str = _INDEX_LAST_MAINTAIN) -> str:
    """读 L2 水位(缺失/损坏 → 空串,即"该做一次")。"""
    return str(_read_index_doc(project_id).get(key) or "")


def set_watermark(project_id: str, value: str, key: str = _INDEX_LAST_MAINTAIN) -> None:
    doc = _read_index_doc(project_id)
    doc[key] = value
    _write_index_doc(project_id, doc)


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
        "description": ("局部替换场景块中的一个片段(小改动用;大改/结构变更请 scene_read 后 "
                        "scene_write 整体重写)。编辑后不得超单块上限,超了请改走整体精简。"),
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
    # 不截断(TAM read 原样返回全文):超限块必须整块可见,整理 agent 才谈得上精简重写
    return {"ok": True, "content": p.read_text(encoding="utf-8")}


def _op_scene_write(args: Dict[str, Any], scene_dir: Path) -> Dict[str, Any]:
    content = str(args.get("content") or "")
    if not content.strip():
        return {"ok": False, "error": "拒绝空内容(删除场景块请写 [DELETED])"}
    if len(content) > _BLOCK_MAX_CHARS:
        return {"ok": False,
                "error": f"内容 {len(content)} 字符超上限 {_BLOCK_MAX_CHARS},请精简或合并场景"}
    p = _resolve_block(scene_dir, str(args.get("scene_file") or ""))
    if p.name.startswith(_DIARY_PREFIX):
        return {"ok": False, "error": "日记块为巩固期只读资产，维护不可写"}
    scene_dir.mkdir(parents=True, exist_ok=True)
    p.write_text(content.strip() + "\n", encoding="utf-8")
    return {"ok": True, "file": p.name, "chars": len(content.strip())}


def _op_scene_edit(args: Dict[str, Any], scene_dir: Path) -> Dict[str, Any]:
    p = _resolve_block(scene_dir, str(args.get("scene_file") or ""))
    if not p.exists():
        return {"ok": False, "error": f"场景块不存在: {p.name}"}
    if p.name.startswith(_DIARY_PREFIX):
        return {"ok": False, "error": "日记块为巩固期只读资产，维护不可改"}
    raw = p.read_text(encoding="utf-8")
    old = str(args.get("old_text") or "")
    if old not in raw:
        return {"ok": False, "error": "old_text 未在文件中找到"}
    # Python 的 str.replace 没有 JS 的 $' 展开坑(TAM 侧为此专门换成 replacer 函数,
    # 见 storage-tools.ts:152-156——那里当年的症状就是场景块每次 edit 自乘式膨胀)
    after = raw.replace(old, str(args.get("new_text") or ""), 1)
    if len(after) > _BLOCK_MAX_CHARS and len(after) > len(raw):
        return {"ok": False,
                "error": f"编辑后 {len(after)} 字符超上限 {_BLOCK_MAX_CHARS}:"
                         "局部追加已封死,请改用 scene_write 整体精简或 MERGE"}
    p.write_text(after, encoding="utf-8")
    return {"ok": True, "file": p.name, "chars": len(after)}


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
        if p.name.startswith(_DIARY_PREFIX):
            continue                      # 日记是巩固期资产：清理/归一化不碰
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
        if p.name.startswith(_DIARY_PREFIX):
            continue                      # 日记块名由巩固期生成，已规范
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
        "你负责把碎片化的 L1 原子记忆蒸馏进场景知识块(scene_blocks/*.md),为 agent 维护一套"
        "**可跨任务复用**的场景知识库:环境事实、App 怪癖、UI 语义、可靠操作方式。\n\n"
        "## 内容纪律(红线,现文里已有的一并剔除)\n"
        "- 只保留可跨任务复用的知识:环境事实、App 怪癖、UI 语义、可靠操作方式、用户长期偏好;\n"
        "- 禁止角色形象/人设/自我介绍(那属于用户画像);禁止对话寒暄;\n"
        "- 禁止单次任务的过程流水账(某次执行做了哪几步、某次扫描的结果数字);\n"
        "- 禁止「当前任务目标」等易变状态——只写稳定的场景知识。\n\n"
        "## 架构模型\n"
        "- Layer 1 输入:新记忆列表(L1 原子记忆——已过抽取、去重、合并与冲突裁决的"
        "短句,带 id 与时间);\n"
        "- Layer 2 输出:场景块——**不是清单,是连贯的知识文档**;\n"
        "- 动作只有 Integrate(更新)/ Rewrite(重写)/ Merge(合并)/ Create(创建);禁止简单追加。\n\n"
        "## 文件操作约束(必须严格遵守)\n"
        "1. 只能使用 scene_read / scene_write / scene_edit 三个工具,文件名用相对名;\n"
        "2. scene_read 只能读「已有场景文件清单」列出的文件,禁止猜测或编造;\n"
        "3. 新建/整体重写用 scene_write(path=文件名, content=完整内容含 META);小改动用 scene_edit;"
        "scene_edit 只能改字不能撑大——编辑后超单块上限的请求会被拒,那时改用 scene_write 精简;\n"
        "4. 删除的唯一方式:scene_write 将内容写为 [DELETED](系统自动清理);"
        "禁止写空字符串;禁止用 [ARCHIVE] 等替代标记;\n"
        "5. 禁止创建报告/汇总类文件;文件名以 .md 结尾,只允许中英文/数字/-_.,多词用 - 连接。\n\n"
        "## 工作流(执行前先完成思维链)\n"
        "### 阶段 0:容量检查(必须先做)\n"
        "统计「现有场景块摘要」顶部标注的场景总数,遵守分级预警:\n"
        "- 红色(≥ 上限):必须先 MERGE(2-4 合 1)减少数量,再处理新记忆;\n"
        "- 橙色(= 上限-1):只能 UPDATE,不能 CREATE;\n"
        "- 黄色(接近上限):优先 UPDATE 或 MERGE 相似场景。\n"
        "合并优先级:主题高度重叠 > 叙事弧线相同 > heat 最低的 2-3 个。\n"
        "### 阶段 1:分析新记忆列表,提炼可复用要点\n"
        "每条原子都是一句已裁决的事实/规则/偏好(命令写法、超时设置、转义坑、路径习惯、"
        "工具链事实、长期偏好等)——你的职责是把它**蒸馏**进对应场景块的同主题章节:"
        "同主题就 UPDATE 重写那一节,确无归属才考虑 CREATE。"
        "禁止把原子原文逐字粘进块(逐字记录是 L1 的职责,块是蒸馏物、不是第二抄本);"
        "结果数字只有作为基准值可复用时才留。\n"
        "### 阶段 2:选择策略(优先级从高到低)\n"
        "- **UPDATE 首选**:默认策略是 UPDATE 不是 CREATE,犹豫时选 UPDATE;heat = 旧 heat + 1;\n"
        "- **MERGE**:合并后 heat = 所有相关块 heat 之和 + 1;"
        "**必须把每个旧块逐个 scene_write 写为 [DELETED]**,不写 = 合并无效;\n"
        "- **CREATE**:新记忆揭示了尚无场景块覆盖的新主题(新磁盘/新项目/新工具链)时,"
        "CREATE 是正确选择;总数未达上限才允许;CREATE 前必须 scene_read 至少 2 个最相似场景"
        "确认无归属;每次维护最多新增 1 个场景;新块 heat = 1。\n"
        "### 阶段 3:深度整合\n"
        "严禁把新记忆逐条追加到块尾了事;同一节越写越长就**重写整节**而不是继续加行;"
        "冲突不直接覆盖,记入「待确认/矛盾点」。\n\n"
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
        "(可空章节直接删掉;正文短陈述句,每条一行。预算照 TAM:单块 ≤1500 字符"
        "(工程硬上限 4000,超限的写侧请求直接被拒);「核心叙事」不是列表,是一段连贯描述、"
        "≤400 字,不写流水账,更新时动态整合甚至重写。)\n\n"
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
                    stats: Optional[Dict[str, Any]] = None,
                    on_debug=None) -> Dict[str, Any]:
    """L2 维护主流程:快照 → agent 循环 → 清理/归一化/索引 → (失败)整目录还原。

    ``evidence`` 为「新记忆列表」文本(L1 原子增量的 JSON,由调用方组装)。
    stats 回传:created/updated/deleted/renamed、empty_maintenance、empty_extraction
    (对齐上游同名标记:整理前后都没有任务块)、reply_preview
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

        entries_all = read_scene_index(project_id)
        # diary-* 是巩固期只读资产：不进任务块容量计数/摘要/文件清单（维护 agent 无视野）
        entries = [e for e in entries_all
                   if not str(e.get("filename") or "").startswith(_DIARY_PREFIX)]
        task_files = {n_: p_ for n_, p_ in pre_files.items()
                      if not n_.startswith(_DIARY_PREFIX)}
        n = len(task_files)
        warning = _capacity_warning(n, _max_blocks())
        summary_lines = [f"**当前场景总数:{n} / {_max_blocks()}**"]
        for e in entries:
            summary_lines.append(f"### {e['filename']}")
            summary_lines.append(f"**热度**: {e['heat']} | **更新**: {e['updated']}")
            summary_lines.append(f"**summary**: {e['summary']}")
        file_list = "\n".join(f"- `{name}`" for name in sorted(task_files)) or "(当前无已有场景文件)"
        user = (
            "**输出语言**:与「新记忆列表」相同的语言。\n\n"
            + (f"⚠️ **场景数量警告**: {warning}\n\n" if warning else "")
            + f"### 1️⃣ 新记忆列表(自上次整理后新增/更新的 L1 原子)\n{evidence.strip()}\n\n"
            + f"### 2️⃣ 现有场景块摘要\n" + "\n".join(summary_lines) + "\n\n"
            + f"### 3️⃣ 当前时间\n{_now()}\n\n"
            + f"### 📁 已有场景文件清单(仅以下文件可 scene_read)\n{file_list}"
        )
        final_text, _ops = _run_agent(brain_cfg, _scene_system_prompt(), user, scene_dir,
                                      on_debug=on_debug)
        stats["agent_ops"] = _ops   # 观测 + 游标判定（§5.1 情形⑤：零动作不推进）

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
            # 对齐 TAM emptyExtraction(前后都没有任务块;diary-* 是只读资产,不计入)
            "empty_extraction": not [nm for nm in post_files
                                     if not nm.startswith(_DIARY_PREFIX)],
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


# ---------------------------------------------------------------- 触发薄壳(任务收尾)

def maybe_maintain(project_id: str, brain_cfg: Optional[Dict[str, Any]],
                   stats: Optional[Dict[str, Any]] = None,
                   allow_write: bool = True) -> None:
    """任务收尾触发 L2 维护:gate + 900s 节流 + L1 原子增量进料,机制全在 maintain_scenes。

    - gate:``runtime.knowledge.memory.enabled``(与 L1 同一总开关)
    - 没有可用的 LLM 配置(``base_url``/``model`` 缺一):直接返回并记
      ``l2_skipped_no_llm``,**游标不推进**——不能让"喂了但没人吃"的批次被划过去
    - ``allow_write``:本轮"是否真的干了活"(plan §3.3 ``did_work``)。False → 直接返回,
      **不跑场景 agent 也不推进水位**。门控维度是"有没有产出事实",不是入口模式:
      闲聊/纯对话不产场景,语音派出去的任务照常维护(由 worker 那轮的 did_work 决定)。
      这条也是语音体验的硬依赖——场景 agent 是墙钟最长 300s 的循环,绝不能挂在闲聊尾巴上。
    - **进料 = 游标之后的 L1 原子增量**(对齐上游 pipeline-factory 的
      ``queryMemoryRecords({updatedAfter})`` + ``buildIsolationScope`` 逐组跑 extractor):
      分组键是 ``origin``(= 落库时的 project id),``origin=''`` 的手记全局条目对各库都可见;
      存储仍是一池,路由只发生在进料这一面。零增量 → 整轮不调 LLM 直接返回
      (对齐上游 ``memRecords.length === 0 → skipped``)。本函数不再吃会话要点/世界状态,
      所以"不产 L1"的轮次天然喂不进场景块——叙事归日记(L0),
      知识归原子(L1),两条进料道不再交叉。这是场景块不再无限长的主闸。
    - 水位(两份,都存 ``scene_index.json``):时间节流位 ``last_maintain_ts`` 空维护不推进
      (本地既有约定);进料游标位 ``last_l2_atom_ts`` 只要本轮动过块就推进,
      唯一例外是上游 ``emptyExtraction``(整理前后都没有任务块)——冷启动交白卷要重喂。
      失败(``stats.error``)两路都不推进(对齐上游 success=false 不写 checkpoint)。
    - 带外信号:场景 agent 的 ``[PERSONA_UPDATE_REQUEST]`` → brain meta,下次巩固强制画像重写
    - 任何异常静默,绝不阻断收尾
    """
    stats = stats if stats is not None else {}
    try:
        from omni_core import memory_brain
        if not memory_brain.enabled() or not project_id:
            return
        # 没配好 LLM 就整轮不做,而且**不推游标**:maintain_scenes 对空 cfg 直接返回,
        # 若让它走完这批原子就被"喂了但没人吃"地划过去(对齐上游 success=false 不写 checkpoint)。
        if not brain_cfg or not (brain_cfg.get("base_url") and brain_cfg.get("model")):
            stats["l2_skipped_no_llm"] = 1
            return
        if not allow_write:
            stats["l2_skipped_no_work"] = 1
            return
        last = get_watermark(project_id)
        if last:
            try:
                if (datetime.now(timezone.utc)
                        - datetime.fromisoformat(last)).total_seconds() < _L2_MIN_INTERVAL_S:
                    return
            except Exception:
                pass
        feed = memory_brain.atoms_since(
            get_watermark(project_id, _INDEX_LAST_ATOM_TS), project_id, _L2_ATOMS_PER_RUN)
        if not feed:
            stats["l2_skipped_no_new_atoms"] = 1
            return
        evidence = json.dumps(
            [{"id": a["id"], "content": a["content"], "kind": a["kind"],
              "created_at": a["created_at"],
              # 场合/来源标签只作**渲染元信息**（INV-9：不用于过滤批次）
              "context_key": str(a.get("context_key") or ""),
              "source_kind": str(a.get("source_kind") or "")}
             for a in feed],
            ensure_ascii=False, indent=2)
        maintain_scenes(project_id, brain_cfg, evidence, stats=stats)
        if stats.get("error"):
            return
        signal = str(stats.get("persona_update_request") or "").strip()
        if signal:
            memory_brain.request_profile_update(signal)
        # 游标推进的五种不推进情形（plan §5.1，P1）：①零增量（上面 return）②stats.error
        # ③无可用 LLM（上面 return）④整理前后无任务块（empty_extraction）⑤整理 agent
        # 未产生任何动作（agent_ops==0——只回文本没动工具）。任一命中都把本批**留到下次**，
        # 不能让"喂了但没人吃"的批次被划过去。
        if stats.get("empty_extraction") or not int(stats.get("agent_ops") or 0):
            if not stats.get("empty_extraction"):
                stats["l2_no_agent_action"] = 1
            return
        if feed:
            set_watermark(project_id, memory_brain.cursor_at(feed[-1]), _INDEX_LAST_ATOM_TS)
        if not stats.get("empty_maintenance"):
            set_watermark(project_id, memory_brain._now())
    except Exception:
        pass


# ---------------------------------------------------------------- 消费端(导航 / 按需读取)

def load_scene_nav(project_id: str, topic_cap: int = 10,
                   diary_cap: int = _DIARY_NAV_WINDOW, month_cap: int = 2) -> List[Dict[str, Any]]:
    """L2 导航段(system 常驻):分族配额裁剪后的**索引条目**。

    2026-11-02 对齐原仓库 `Path + 热度 + Summary` 三行渲染
    (``MemoryCore/src/core/scene/scene-navigation.ts:113-115``):本函数只负责
    **分族配额**,渲染由消费方(``instructions._memory_stable_block``)完成。

    - 主题块:heat 降序,取前 ``topic_cap``;
    - 日记:日期倒序,取前 ``diary_cap``(heat 恒 0 不参与排序,渲染成日期);
    - 月记:日期倒序,取前 ``month_cap``。
    总条数 ≤ topic_cap + diary_cap + month_cap = 19(主题 10 + 日记 7 + 月记 2)。
    全文按需 read_scene;`read_scene` 空参列清单走 ``list_scene_blocks``(不裁剪)。
    """
    try:
        # H-4：日记/月记全局一份（按日期），不随 project 变化；项目索引只投影任务块
        entries = [e for e in read_scene_index(project_id)
                   if not str(e.get("filename") or "").startswith(_DIARY_PREFIX)]
        months: List[Dict[str, Any]] = []
        dailies: List[Dict[str, Any]] = []
        others: List[Dict[str, Any]] = list(entries)
        if diary_cap > 0 or month_cap > 0:      # 生产路径两配额为 0，避免每轮读盘
            try:
                for p in diary_dir().glob("diary-*.md"):
                    try:
                        meta, _ = parse_scene_block(p.read_text(encoding="utf-8"))
                    except Exception:
                        continue
                    e = {"filename": p.name, "summary": meta["summary"], "heat": 0,
                         "created": meta["created"], "updated": meta["updated"]}
                    (months if p.name.startswith("diary-month-") else dailies).append(e)
            except Exception:
                pass
        others.sort(key=lambda e: int(e.get("heat") or 0), reverse=True)
        dailies.sort(key=lambda e: str(e.get("filename") or ""), reverse=True)
        months.sort(key=lambda e: str(e.get("filename") or ""), reverse=True)
        return (others[:max(0, topic_cap)] + months[:max(0, month_cap)]
                + dailies[:max(0, diary_cap)])
    except Exception:
        return []


def read_scene_block(project_id: str, name: str) -> str:
    """L2 按需读取端(read_scene 工具用):单块全文,不截断(TAM read 不截断)。

    尺寸纪律由写侧 ``_BLOCK_MAX_CHARS`` 守(4000),读侧不再悄悄砍——
    历史上截断会让超限块"只看得见前半",整理 agent 无从精简、模型也只读到半条规则。

    H-4：日记/月记先读全局叙事目录（一天一篇）；旧项目目录只作迁移期历史回退。
    """
    try:
        safe = normalize_scene_filename(name)
        if safe.startswith(_DIARY_PREFIX):
            p = diary_dir() / safe
            if p.is_file():
                return p.read_text(encoding="utf-8").strip()
        p = _resolve_block(scene_blocks_dir(project_id), name)
        if not p.exists():
            return ""
        return p.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


def list_scene_blocks(project_id: str) -> List[Dict[str, Any]]:
    """read_scene 空参时的可用块清单(即索引,不裁剪)。"""
    return load_scene_nav(project_id, topic_cap=10**6, diary_cap=10**6, month_cap=10**6)


def write_diary_block(project_id: str, date_str: str, text: str, *,
                      topics: Optional[List[str]] = None,
                      context_keys: Optional[List[str]] = None,
                      peers: Optional[List[str]] = None,
                      time_from: str = "", time_to: str = "",
                      atmosphere: str = "",
                      we_participants: Optional[List[str]] = None) -> bool:
    """写日记块 ``diary-<日期>.md``（内核巩固期直写；plan persona-social-memory M2）。

    H-4（2026-11-06）：落点是**全局叙事目录** ``rp.global_diary()``，``project_id``
    只作调用兼容、不再决定路径——助手的一天跨项目，按日期全局唯一一篇。

    - 当日一篇、幂等（已存在则跳过返回 False）；META.summary = 正文首行（导航地图用）；
    - 日记不占任务块 max_blocks 容量、维护 agent 不可读写（只读资产）；
    - 空正文拒绝（白卷不落盘）；
    - 可选键（plan §13.2.2 / §16.3）：``topics`` / ``context_keys`` / ``peers`` /
      ``time_from`` / ``time_to`` / ``atmosphere``（P6-1 可回味词面）/
      ``we``（P6-2 共同经历参与人，取值来自配置）追加在四键之后；
      ``parse_scene_block`` 只认原四键，新键自动忽略，五字段投影不受影响。
      本函数**不碰叙事索引**（upsert 由巩固期调用方负责，§13.10.4：写块与索引分工，
      索引失败静默不回滚写块）。
    """
    try:
        text = (text or "").strip()
        if not text:
            return False
        date_str = str(date_str or "").strip()
        if not date_str or any(c in date_str for c in "/\\"):
            return False
        scene_dir = diary_dir()          # H-4：跨项目统一，一天只此一篇
        scene_dir.mkdir(parents=True, exist_ok=True)
        p = scene_dir / f"{_DIARY_PREFIX}{date_str}.md"
        if p.exists():
            return False
        now = _now()
        # summary 取首个句末标点结尾的句子(≤60 字),不再硬截 60 字断句
        # (2026-11-02:此前 `text[:60]` 会停在半句话,导航里出现"…后来不知怎么冒出")
        flat = text.replace("\n", " ").strip()
        ends = [i for i in (flat.find(ch) for ch in "。！？!?") if i != -1]
        end = min(ends) + 1 if ends else 60
        summary = flat[:min(end, 60)]
        meta = (f"{_META_START}\ncreated: {now}\nupdated: {now}\n"
                f"summary: {summary}\nheat: 0\n")
        # 可选键（§13.2.2 / §16.3）：值为空/缺省不写行——老解析只认四键，行越少越干净
        if topics:
            meta += f"topics: {json.dumps(_clean_list(topics), ensure_ascii=False)}\n"
        if context_keys:
            meta += f"context_keys: {json.dumps(_clean_list(context_keys), ensure_ascii=False)}\n"
        if peers:
            meta += f"peers: {json.dumps(_clean_list(peers), ensure_ascii=False)}\n"
        if time_from:
            meta += f"time_from: {str(time_from)[:32]}\n"
        if time_to:
            meta += f"time_to: {str(time_to)[:32]}\n"
        if atmosphere:
            meta += f"atmosphere: {str(atmosphere).strip()[:32]}\n"
        we = _clean_list(we_participants)
        if we:
            meta += f"we: {json.dumps(we, ensure_ascii=False)}\n"
        meta += f"{_META_END}\n"
        p.write_text(meta + text + "\n", encoding="utf-8")
        return True
    except Exception:
        return False


def _clean_list(items: Optional[List[str]]) -> List[str]:
    """META 可选键的字符串数组净化（去空、去重保序、上限 12 项防 META 膨胀）。"""
    out: List[str] = []
    for x in (items or []):
        s = str(x or "").strip()
        if s and s not in out:
            out.append(s)
        if len(out) >= 12:
            break
    return out
