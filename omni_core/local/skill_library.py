"""Skill 库：成功轨迹提炼 + N=3 累计晋升门。

格式：Hermes 风格 SKILL.md（Markdown + YAML frontmatter，对齐 agentskills.io）。
存储：project 级 `projects/<pid>/skills/<name>-<entry8>.md`；用户显式提升的
通用 skill 落全局 `~/.omniagent/skills/`。用户手写的纯 .md 技能（无 entry_id）保持 `<name>.md`。
子结构（substeps/metadata）用 JSON 内嵌在 frontmatter 中，避免手写 YAML 解析。

Skill 身份（归一化白名单，方向经实测校准）：
  ``entry_id = sha1(归一化动作序列)[:16]`` —— 同一操作序列跨 task 认得自己。
  归一化：text/content/query/input/command/url → `<text>`；path → `<path>`；
  坐标（x/y/x1/y1/x2/y2）保留键名去值；**其余字符串值默认 → `<text>`**
  （实测依据：task_done(reason=…) 每次措辞不同，保守保留会让累计晋升永远命不中）；
  结构保留键（resource_id/view_id/包名/按键类）与非字符串标量留原值。

Skill 生命周期：
  task success → 从 RunRecord 提取 substeps → 创建 candidate skill
  → 后续 entry_id 相同的成功 → success_count += 1（**累计**语义；失败不清零——
    失败只累加 failure_count，不与晋升构成双罚）
  → 累计成功 3 次 → 晋升 active

技能产出形态：
  **知识型技能的机械转录是硬禁令**（把执行轨迹转录成"学到的知识"、以 objective[:40]
  写死命名均禁止）。技能来源 = ①人工维护；②``skill.auto_distill`` 开关接入的
  **结构化宏提取**（步骤 = 轨迹原始 args，身份 = 归一哈希；LLM 仅出
  ``routine/description`` 标签 + ``validate_skill_summary`` 防幻觉校验）；
  ``promote_or_insert`` 供两条来源共用。
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from omni_core.local.runtime_paths import (
    DEFAULT_PROJECT_ID,
    global_skills,
    project_skills,
    task_dir,
)


# ---------------------------------------------------------------------------
# Skill 数据结构
# ---------------------------------------------------------------------------

@dataclass
class SkillSubstep:
    tool: str
    args: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SkillMetadata:
    success_count: int = 0
    failure_count: int = 0
    total_uses: int = 0
    confidence: float = 0.0
    status: str = "candidate"
    last_used: str = ""
    created: str = ""
    scope: str = "project"          # project | global（升级 skill 跨项目复利）
    environment: Dict[str, Any] = field(default_factory=dict)
    known_failures: List[str] = field(default_factory=list)


@dataclass
class Skill:
    name: str = ""
    objective_pattern: str = ""
    task_id: str = ""
    entry_id: str = ""                   # 技能身份：sha1(归一化动作序列)[:16]；手写技能为空
    description: str = ""
    tags: List[str] = field(default_factory=list)
    substeps: List[SkillSubstep] = field(default_factory=list)
    metadata: SkillMetadata = field(default_factory=SkillMetadata)
    body: str = ""
    disable_model_invocation: bool = False  # frontmatter 字段：True 时不可被模型直接调用

    def to_frontmatter(self) -> str:
        """YAML frontmatter（子结构用 JSON 内嵌）。"""
        ss = json.dumps(
            [{"tool": s.tool, "args": s.args} for s in self.substeps],
            ensure_ascii=False, separators=(",", ":"),
        )
        md = json.dumps({
            "success_count": self.metadata.success_count,
            "failure_count": self.metadata.failure_count,
            "total_uses": self.metadata.total_uses,
            "confidence": self.metadata.confidence,
            "status": self.metadata.status,
            "last_used": self.metadata.last_used,
            "created": self.metadata.created,
            "scope": self.metadata.scope,
            "known_failures": self.metadata.known_failures,
        }, ensure_ascii=False, separators=(",", ":"))
        return (
            f"---\n"
            f"name: {self.name}\n"
            f"objective_pattern: {self.objective_pattern}\n"
            f"task_id: {self.task_id}\n"
            f"entry_id: {self.entry_id}\n"
            f"description: {self.description}\n"
            f"disable_model_invocation: {str(bool(self.disable_model_invocation)).lower()}\n"
            f"tags_json: {json.dumps(self.tags, ensure_ascii=False)}\n"
            f"substeps_json: {ss}\n"
            f"metadata_json: {md}\n"
            f"---"
        )

    def to_markdown(self) -> str:
        """结构化机器档案渲染（无正文时的落盘兜底 / 人类可读视图）。"""
        body = [
            f"# Skill: {self.name}",
            f"## Objective",
            f"`{self.objective_pattern}`",
            "## Substeps",
        ]
        for i, s in enumerate(self.substeps, 1):
            a = json.dumps(s.args, ensure_ascii=False) if s.args else "{}"
            body.append(f"{i}. `{s.tool}({a})`")
        body += [
            f"## Status: {self.metadata.status}",
            f"- Success: {self.metadata.success_count}",
            f"- Failures: {self.metadata.failure_count}",
            f"- Confidence: {self.metadata.confidence:.2f}",
        ]
        return "\n".join(body)

    def _body_markdown(self) -> str:
        """落盘正文：有 body（playbook，引导型文字）→ body 即正文；
        无正文（机器档案）→ 结构化渲染。保证 body 落盘-读回逐字节一致。"""
        return self.body.strip() if (self.body or "").strip() else self.to_markdown()

    # --- N=3 累计晋升 --------------------------------------------------------
    def record_success(self) -> bool:
        """累计成功 +1（失败不清零）；累计满 3 且仍为 candidate → active。"""
        self.metadata.success_count += 1
        self.metadata.total_uses += 1
        self.metadata.last_used = datetime.now(timezone.utc).isoformat()
        self.metadata.confidence = self.metadata.success_count / max(self.metadata.total_uses, 1)
        if self.metadata.success_count >= 3 and self.metadata.status == "candidate":
            self.metadata.status = "active"
            return True
        return False

    def record_failure(self, reason: str = "") -> None:
        """失败记数（**不清零 success_count**：晋升看累计成功，失败只累加，不构成双罚）。"""
        self.metadata.failure_count += 1
        self.metadata.total_uses += 1
        self.metadata.last_used = datetime.now(timezone.utc).isoformat()
        self.metadata.confidence = self.metadata.success_count / max(self.metadata.total_uses, 1)
        if reason and reason not in self.metadata.known_failures:
            self.metadata.known_failures.append(reason[:200])

    def record_use(self) -> None:
        """命中簿记（命中即记 total_uses——否则命中率不可观测，效用淘汰没有输入）。

        只记使用、不改成败计数（成败由 record_success / record_failure 记）。
        """
        self.metadata.total_uses += 1
        self.metadata.last_used = datetime.now(timezone.utc).isoformat()
        self.metadata.confidence = self.metadata.success_count / max(self.metadata.total_uses, 1)


# ---------------------------------------------------------------------------
# Skill 库持久化
# ---------------------------------------------------------------------------

def _safe_filename(name: str) -> str:
    return "".join(c if c.isalnum() or c in "_-" else "_" for c in name)[:60]


# ---------------------------------------------------------------------------
# 技能身份：归一化 + entry_id
# ---------------------------------------------------------------------------
#: 结构保留键（值进身份——匹配的强信号）；其余字符串值一律视为易变自由文本
_STRUCTURE_KEEP_KEYS = frozenset({
    "resource_id", "resource-id", "view_id", "package", "package_name",
    "key", "keycode", "code", "action",
})
_NORMALIZE_TEXT_KEYS = frozenset({"text", "content", "query", "input", "command", "url"})
_NORMALIZE_PATH_KEYS = frozenset({"path"})
_COORD_KEY_RE = re.compile(r"^x\d*$|^y\d*$")


def _normalize_args(args: Dict[str, Any]) -> Dict[str, Any]:
    """按白名单归一化单步参数（易变值出身份、结构性值留原值）。

    - text/content/query/input/command/url → `<text>`；path → `<path>`；
    - 坐标（x/y/x1/y1/x2/y2）保留键名、去值；
    - **其余字符串值 → `<text>`**（默认易变。实测依据：`task_done(reason=…)` 每次
      措辞不同，若保守保留原值，跨 run 累计晋升永远命不中——恰是本白名单要修的病）；
    - 结构保留键（resource_id/view_id/包名/按键类）与非字符串标量保留原值。
    """
    out: Dict[str, Any] = {}
    for k, v in (args or {}).items():
        key = str(k)
        if key in _NORMALIZE_TEXT_KEYS:
            out[key] = "<text>"
        elif key in _NORMALIZE_PATH_KEYS:
            out[key] = "<path>"
        elif _COORD_KEY_RE.match(key):
            out[key] = ""
        elif isinstance(v, str) and key not in _STRUCTURE_KEEP_KEYS:
            out[key] = "<text>"
        else:
            out[key] = v
    return out


def compute_entry_id(substeps: List[SkillSubstep]) -> str:
    """entry_id = sha1(归一化动作序列)[:16]——同一套路的稳定身份。"""
    seq = [{"tool": s.tool, "args": _normalize_args(s.args)} for s in (substeps or [])]
    raw = json.dumps(seq, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 结构化宏提取（skill.auto_distill 开关的生产者）
# ---------------------------------------------------------------------------
#: 提取排除的循环元工具 / 技能自指工具（终止与检索不是可复用动作；回放自身会递归）
_AUTO_EXCLUDE_TOOLS = frozenset({
    "task_done", "verify", "escalate", "record", "plan",
    "load_skill", "search_skill", "replay_skill",
})


def _latest_success_trajectory(task_id: str) -> Optional[Path]:
    """取该任务**最近一次成功 run** 的轨迹文件（*.run.json 按 end_ts 取最新）。

    无任务目录 / 无成功 run / 轨迹文件缺失 → None（调用方零产出）。
    """
    d = task_dir(task_id)
    if not d.is_dir():
        return None
    best: Optional[tuple] = None
    for p in d.glob("*.run.json"):
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not rec.get("success"):
            continue
        tf = str(rec.get("trajectory_file") or "")
        if not tf or not Path(tf).is_file():
            continue
        ts = str(rec.get("end_ts") or "")
        if best is None or ts >= best[0]:
            best = (ts, Path(tf))
    return best[1] if best else None


def substeps_from_trajectory(task_id: str, max_steps: int = 32) -> List[SkillSubstep]:
    """从任务最近一次成功 run 的轨迹提取已执行动作序列（原始 args）。

    双轨纪律：返回的 substeps 保留**原始 args**（回放执行所需）；
    技能身份由 ``compute_entry_id``（归一形态）另行计算，归一结果不落盘为步骤内容。
    过滤：think 行 / 无工具名步 / 元工具 / 结果 ``ok=False`` 的失败步（失败步不进宏）。
    无轨迹 / 无成功 run → 空表。

    Args:
        task_id: 任务 id。
        max_steps: 序列截头上限（控技能体积）。
    """
    path = _latest_success_trajectory(task_id)
    if path is None:
        return []
    out: List[SkillSubstep] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    for line in lines:
        if len(out) >= max_steps:
            break
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("kind") == "think":
            continue
        action = rec.get("action") or {}
        tool = str((action or {}).get("tool") or "").strip()
        if not tool or tool in _AUTO_EXCLUDE_TOOLS:
            continue
        result = rec.get("result")
        if isinstance(result, dict) and result.get("ok") is False:
            continue
        args = (action or {}).get("args")
        out.append(SkillSubstep(tool=tool, args=dict(args) if isinstance(args, dict) else {}))
    return out


class SkillLibrary:
    """skill 池 = project 级 projects/<pid>/skills/ + 全局 ~/.omniagent/skills/。

    知识分层 v2：skill 归 **project**（跨 task 复用），global 只放手动的「设为全局」。
    recall（load/list/find）合并两层、project 优先；写入按 scope 分流：
    - scope == "global" → 全局
    - 默认 → project 级 projects/<pid>/skills/

    project 归属解析：显式传参 > task.json 的 project_id > ``default``（缺省项目）。
    """

    def __init__(self, task_id: str, project_id: str = ""):
        self.task_id = task_id
        self.project_id = project_id or self._resolve_project(task_id)
        self._proj_dir = project_skills(self.project_id)
        self._proj_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _resolve_project(task_id: str) -> str:
        if task_id:
            try:
                from omni_core.local.task_store import TaskStore
                pid = TaskStore.project_of(task_id)
                if pid:
                    return pid
            except Exception:
                pass
        return DEFAULT_PROJECT_ID

    def _dir(self) -> Path:
        return self._proj_dir

    @staticmethod
    def _global_dir() -> Path:
        d = global_skills()
        d.mkdir(parents=True, exist_ok=True)
        return d

    @staticmethod
    def _filename_for(skill: Skill) -> str:
        """落盘文件名：有身份的技能带 entry8 后缀（同名变体不互相覆盖）；
        手写技能（无 entry_id）保持 `<name>.md`。"""
        base = _safe_filename(skill.name) or "skill"
        if skill.entry_id:
            return f"{base}-{skill.entry_id[:8]}.md"
        return f"{base}.md"

    # ---- 双层级读取（project 优先 + 全局） ----
    def _iter_both_dirs(self) -> List[Path]:
        """project 目录在前（同名/同 entry_id 先到先得 = project 胜出），全局在后。"""
        paths: List[Path] = []
        paths.extend(sorted(self._dir().glob("*.md")))
        paths.extend(sorted(self._global_dir().glob("*.md")))
        return paths

    def _save_to(self, skill: Skill, directory: Path) -> str:
        if not skill.entry_id and skill.substeps:
            skill.entry_id = compute_entry_id(skill.substeps)  # 单一收口：有动作序列必有身份
        if not skill.metadata.created:
            skill.metadata.created = datetime.now(timezone.utc).isoformat()
        text = skill.to_frontmatter() + "\n\n" + skill._body_markdown()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / self._filename_for(skill)
        path.write_text(text, encoding="utf-8")
        return str(path)

    def save(self, skill: Skill) -> str:
        """按 scope 分流落盘：global → 用户级；否则 → project 级 projects/<pid>/skills/。"""
        scope = skill.metadata.scope or "project"
        if scope == "global":
            return self._save_to(skill, self._global_dir())
        return self._save_to(skill, self._dir())

    def save_global(self, skill: Skill) -> str:
        """显式落全局。"""
        return self._save_to(skill, self._global_dir())

    def promote_to_global(self, name: str) -> Dict[str, Any]:
        """「设为全局」（唯一晋升路径，用户显式触发）：
        project 副本落全局并移除 project 层文件——global 在 project 内直接可见可用，
        但 project 优先，不移除副本则提升不可见。已是全局则幂等重写。"""
        skill = self.load(name)
        if skill is None:
            return {"ok": False, "error": f"未找到技能: {name}"}
        src = self.source_path(name)
        skill.metadata.scope = "global"
        out_path = self._save_to(skill, self._global_dir())
        removed = False
        if src and src.parent == self._dir() and src.exists() and src.parent != self._global_dir():
            src.unlink()
            removed = True
        return {"ok": True, "path": out_path, "removed_project_copy": removed,
                "skill_name": skill.name}

    def source_path(self, name: str) -> Optional[Path]:
        """按名定位技能文件：project 级优先，全局兜底。

        先试两种精确文件名（legacy `<name>.md` 与带 entry8 后缀），再按解析出的
        skill.name 全目录扫描（覆盖 rename / 手写文件名不一致）。
        """
        base = _safe_filename(name)
        for d in (self._dir(), self._global_dir()):
            for candidate in (d / f"{base}.md",):
                if candidate.exists():
                    return candidate
        for f in self._iter_both_dirs():
            s = self._parse(f)
            if s and s.name == name:
                return f
        return None

    def load(self, name: str) -> Optional[Skill]:
        path = self.source_path(name)
        return self._parse(path) if path else None

    def list_all(self) -> List[Skill]:
        """project 优先合并两层：同名文件 project 胜出；同 entry_id 亦 project 胜出（不重复注入）。"""
        seen_files: set = set()
        seen_entries: set = set()
        out: List[Skill] = []
        for f in self._iter_both_dirs():
            if f.name in seen_files:
                continue
            seen_files.add(f.name)
            s = self._parse(f)
            if not s:
                continue
            if s.entry_id:
                if s.entry_id in seen_entries:
                    continue
                seen_entries.add(s.entry_id)
            out.append(s)
        return out

    def list_global(self) -> List[Skill]:
        """仅列全局通用 skill（~/.omniagent/skills/），不含 project 私有。

        用于 Web 面板展示：project 私有 skill 不进全局列表、不共享，只在所属 project 内
        被消费（召回经 SkillLibrary(task_id) / `load_skill` 工具可见）。
        """
        out: List[Skill] = []
        for f in sorted(self._global_dir().glob("*.md")):
            s = self._parse(f)
            if s:
                out.append(s)
        return out

    def delete(self, name: str) -> bool:
        path = self.source_path(name)
        if path:
            path.unlink()
            return True
        return False

    # --- 缓存淘汰 --------------------------------------------------------------
    @staticmethod
    def _archive_dir(directory: Path) -> Path:
        """档案子目录：``_archive/`` 不匹配 ``*.md`` glob，天然退出目录/召回/再淘汰视野。"""
        return directory / "_archive"

    def _idle_days(self, skill: Skill, now: datetime) -> Optional[float]:
        """闲置天数锚点：active 看 last_used（缺失回退 created），candidate 看 created。"""
        anchor = (skill.metadata.last_used or skill.metadata.created
                  if skill.metadata.status == "active" else skill.metadata.created)
        if not anchor:
            return None
        try:
            then = datetime.fromisoformat(str(anchor))
        except Exception:
            return None
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        return max(0.0, (now - then).total_seconds() / 86400.0)

    def evict_stale(self, candidate_ttl_days: float = 14.0,
                    active_idle_days: float = 30.0,
                    active_min_confidence: float = 0.5) -> Dict[str, int]:
        """缓存淘汰：仿 memory A4「降级不删」——过期技能移入 ``_archive/`` 子目录。

        只处理**机器宏**（entry_id 非空）；手写技能（无 entry_id）永不自动淘汰。
        - candidate：建档 ``candidate_ttl_days`` 内没攒满 3 次成功 → 档案化
          （长尾唯一序列不无限堆积，§2.5 candidate 池治理）；
        - active：闲置 ``active_idle_days`` 且 confidence < ``active_min_confidence``
          → 档案化（低效用 + 长期未用双条件，高置信技能不因闲置被清）。

        Returns:
            {"candidates_archived": n, "actives_archived": m, "archived": 合计}
        """
        now = datetime.now(timezone.utc)
        out = {"candidates_archived": 0, "actives_archived": 0, "archived": 0}
        for directory in (self._dir(), self._global_dir()):
            archive = self._archive_dir(directory)
            for path in sorted(directory.glob("*.md")):
                s = self._parse(path)
                if not s or not s.entry_id:
                    continue  # 手写技能 / 损坏文件不碰
                idle = self._idle_days(s, now)
                if idle is None:
                    continue
                evict = False
                if s.metadata.status == "candidate":
                    evict = idle >= float(candidate_ttl_days)
                elif s.metadata.status == "active":
                    evict = (idle >= float(active_idle_days)
                             and s.metadata.confidence < float(active_min_confidence))
                if not evict:
                    continue
                try:
                    archive.mkdir(parents=True, exist_ok=True)
                    target = archive / path.name
                    if target.exists():
                        target.unlink()  # 同名档已存在（重复归档），以新档为准
                    path.rename(target)
                    if s.metadata.status == "candidate":
                        out["candidates_archived"] += 1
                    else:
                        out["actives_archived"] += 1
                    out["archived"] += 1
                except Exception:
                    continue
        return out

    # --- 晋升判定 ------------------------------------------------------------
    def promote_or_insert(self, candidate: Skill) -> Dict[str, Any]:
        """身份优先合并：entry_id（同一动作序列）→ name → objective_pattern 全等
        （legacy 兜底，命中即回填 entry_id）。累计成功满 3 → active。"""
        if not candidate.entry_id and candidate.substeps:
            candidate.entry_id = compute_entry_id(candidate.substeps)
        existing: Optional[Skill] = None
        if candidate.entry_id:
            for s in self.list_all():
                if s.entry_id and s.entry_id == candidate.entry_id:
                    existing = s
                    break
        if existing is None:
            existing = self.load(candidate.name)
        if existing is None and candidate.objective_pattern:
            for s in self.list_all():
                if s.objective_pattern and s.objective_pattern == candidate.objective_pattern:
                    existing = s
                    break
        if existing:
            if not existing.entry_id and candidate.entry_id:
                existing.entry_id = candidate.entry_id  # 旧档首次触达即回填身份
            if len(candidate.substeps) >= len(existing.substeps):
                existing.substeps = candidate.substeps
                if candidate.entry_id:
                    existing.entry_id = candidate.entry_id
            # §3.1：LLM 提炼的意图签名 / playbook 增量补齐（已有内容不覆写，保人工编辑）
            if candidate.description and not existing.description:
                existing.description = candidate.description
            if candidate.body and not existing.body:
                existing.body = candidate.body
            if candidate.tags and not existing.tags:
                existing.tags = candidate.tags
            promoted = existing.record_success()
            self.save(existing)
            return {"action": "promoted" if promoted else "updated",
                    "skill_name": existing.name, "promoted": promoted,
                    "success_count": existing.metadata.success_count}
        # created 分支同样记账首次成功（否则 N=3 晋升门实际要 4 次成功才触发——
        # 首次成功从不计数的历史 bug，2026-10-04 宏轴复活时修正）
        candidate.record_success()
        self.save(candidate)
        return {"action": "created", "skill_name": candidate.name,
                "promoted": candidate.metadata.status == "active",
                "success_count": candidate.metadata.success_count}

    # --- 内部 ----------------------------------------------------------------
    @staticmethod
    def _parse(path: Path) -> Optional[Skill]:
        try:
            text = path.read_text(encoding="utf-8")
            front, body = _split_frontmatter(text)
            data = _parse_front(front)
            # 无 name 时回退文件名（兼容纯 .md 技能，不要求固定 frontmatter）
            name = data.get("name") or path.stem
            ss = json.loads(data.get("substeps_json", "[]"))
            md = json.loads(data.get("metadata_json", "{}"))
            return Skill(
                name=name,
                objective_pattern=data.get("objective_pattern", ""),
                task_id=data.get("task_id", ""),
                entry_id=str(data.get("entry_id", "") or ""),
                description=data.get("description", ""),
                tags=_parse_tags(data.get("tags_json") or data.get("tags")),
                substeps=[SkillSubstep(tool=s.get("tool", ""), args=s.get("args") or {}) for s in ss],
                metadata=SkillMetadata(
                    success_count=int(md.get("success_count", 0) or 0),
                    failure_count=int(md.get("failure_count", 0) or 0),
                    total_uses=int(md.get("total_uses", 0) or 0),
                    confidence=float(md.get("confidence", 0) or 0),
                    status=str(md.get("status", "candidate")),
                    last_used=str(md.get("last_used", "")),
                    created=str(md.get("created", "")),
                    scope=str(md.get("scope", "project")),
                    known_failures=md.get("known_failures") or [],
                ),
                body=body.strip(),
                disable_model_invocation=_parse_disable(data.get("disable_model_invocation")),
            )
        except Exception:
            return None


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def _split_frontmatter(text: str):
    parts = text.split("---", 2)
    return (parts[1], parts[2]) if len(parts) >= 3 else ("", text)


def _parse_front(front: str) -> Dict[str, str]:
    r: Dict[str, str] = {}
    for line in front.strip().split("\n"):
        line = line.strip()
        if ":" not in line:
            continue
        k, _, v = line.partition(":")
        k = k.strip().lstrip("- ").strip()
        v = v.strip().strip('"').strip("'")
        if k:
            r[k] = v
    return r


def _parse_disable(raw) -> bool:
    """解析 frontmatter 的 disable_model_invocation 布尔字段。

    默认 False；显式 true/1/yes 为 True；false/0/no/空为 False；
    无法识别的脏值视为 True（保守：不让模型调用不确定禁用的技能）。
    """
    if raw is None:
        return False
    if isinstance(raw, bool):
        return raw
    s = str(raw).strip().lower()
    if s in ("true", "1", "yes", "y"):
        return True
    if s in ("false", "0", "no", "n", ""):
        return False
    return True


def _parse_tags(raw) -> List[str]:
    """frontmatter 的 tags 解析：支持 JSON 数组（tags_json）或逗号/空白分隔回退。"""
    if isinstance(raw, list):
        return [str(t) for t in raw]
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return [str(t) for t in parsed]
    except Exception:
        pass
    return [t.strip() for t in re.split(r"[,\s]+", str(raw).strip()) if t.strip()]


# ---------------------------------------------------------------------------
# §3.1 防幻觉校验（纯函数；将来 skill.auto_distill 开关接入 LLM 提炼时启用）
# ---------------------------------------------------------------------------

def validate_skill_summary(cand: Dict[str, Any], executed_tools: Any = ()) -> bool:
    """防幻觉校验（plan §3.1，纯函数、可离线复核）：校验不过就不产出。

    - ``routine``：非空、≤12 字（规范套路名，``name = skill_<routine>``）；
    - ``description`` / ``objective_pattern``：意图签名，非空；
    - ``playbook``：引导型文字非空且有最小长度（防「一句话空转」）；
    - 正文中以 ``tool(...)`` 调用形态引用的工具名必须 ⊆ 实际执行过的工具集
      （轨迹里没跑过的工具调用 = 幻觉，宁严勿松——校验失败只是少产出一条）。
    """
    routine = str((cand or {}).get("routine") or "").strip()
    if not routine or len(routine) > 12:
        return False
    if not str((cand or {}).get("description") or "").strip():
        return False
    if not str((cand or {}).get("objective_pattern") or "").strip():
        return False
    playbook = str((cand or {}).get("playbook") or "").strip()
    if len(playbook) < 5 or len(playbook) > 4000:
        return False
    mentioned = set(re.findall(r"([A-Za-z_]\w*)\s*\(", playbook))
    hallucinated = {m for m in mentioned if m not in set(executed_tools or ())}
    return not hallucinated


# ---------------------------------------------------------------------------
# 生产者 · 结构化宏提取（skill.auto_distill 开关）
# 原 Curator 第 9a 步（maybe_distill_skill）；2026-10-05 K 系列退役后拆出独立挂点，
# 由 ToolLoop 收尾（loop/finish.py）触发。开关默认关 → 不改变任何现有行为。
# ---------------------------------------------------------------------------

def maybe_distill_skill(task_id: str, brain_cfg: Optional[Dict[str, Any]],
                        success: bool, objective: str) -> Dict[str, Any]:
    """结构化宏提取：轨迹原始 args → LLM 短标签 → 防幻觉校验 → promote_or_insert。

    与 memory 轴零重叠（plan §1）：产物是可重放宏（``entry_id + substeps[原始 args]``），
    存 ``skills/``、经回放执行器消费；不进注入块。身份只用 entry_id 哈希（plan §2.6
    定案——缓存语义=同序列同宏；LLM 语义对齐即 hachimi 坑#2 假泛化，不启用）。

    降级（宁严勿松）：无 brain / 任务未成功 / 步数 <2 / 标签 LLM 失败 / 校验不过
    → 本轮不产出（返回 {"action": "skipped", "reason": ...}）。

    Returns:
        promote_or_insert 结果 dict（action: created/updated/promoted…）或 skipped。
    """
    if not brain_cfg:
        return {"action": "skipped", "reason": "no_brain"}
    if not success:
        return {"action": "skipped", "reason": "not_success"}
    substeps = substeps_from_trajectory(task_id)
    if len(substeps) < 2:  # executed≥2 才建候选（plan §2.2，避坑#4 配额幻觉）
        return {"action": "skipped", "reason": "too_few_steps"}
    objective = str(objective or "").strip()
    labels = _skill_label_llm(brain_cfg, objective, substeps)
    if not isinstance(labels, dict):
        return {"action": "skipped", "reason": "label_llm_failed"}
    routine = str(labels.get("routine") or "").strip()
    description = str(labels.get("description") or "").strip()
    # 防幻觉校验的 playbook 面：步骤渲染文本（tool(...) 形态可被提及校验）
    playbook = "\n".join(
        f"{i + 1}. `{s.tool}({json.dumps(s.args, ensure_ascii=False)})`"
        for i, s in enumerate(substeps))
    cand = {"routine": routine, "description": description,
            "objective_pattern": objective, "playbook": playbook}
    if not validate_skill_summary(cand, [s.tool for s in substeps]):
        return {"action": "skipped", "reason": "validate_failed"}
    skill = Skill(
        name=f"skill_{routine}",
        objective_pattern=objective,
        task_id=task_id,
        description=description,
        substeps=substeps,  # 原始 args 双轨落盘；entry_id 由 save 时归一计算
    )
    return SkillLibrary(task_id=task_id).promote_or_insert(skill)


def _skill_label_llm(brain_cfg: Dict[str, Any], objective: str,
                     substeps: List[SkillSubstep]) -> Optional[Dict[str, Any]]:
    """LLM 短标签（plan §2.1：LLM 不转录步骤，只出 routine/description）。

    失败降级返回 None（区别于「观察不到可复用套路」的空字段——后者由
    validate_skill_summary 拦下，同样不产出）。
    """
    from omni_core.local import llm_judge

    system = (
        "你是技能命名器。根据任务目标与实际执行的工具调用序列，输出：\n"
        "1. routine：技能短名（不超过 12 字，动宾短语，如「清理构建产物」）；\n"
        "2. description：一句话用途描述（不超过 40 字）。\n"
        "不得编造序列中不存在的工具名；观察不到可复用套路就把字段留空。\n"
        '只返回 JSON：{"routine": "...", "description": "..."}'
    )
    seq = "\n".join(
        f"{i + 1}. {s.tool}({json.dumps(s.args, ensure_ascii=False)[:160]})"
        for i, s in enumerate(substeps))
    user = f"任务目标：{objective}\n\n执行序列：\n{seq}"
    verdict = llm_judge.chat_json(brain_cfg, system, user, timeout=60.0)
    return verdict if isinstance(verdict, dict) else None
