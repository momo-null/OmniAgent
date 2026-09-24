"""M4b.2 Skill 库：成功轨迹提炼 + N=3 晋升门。

格式：Hermes 风格 SKILL.md（Markdown + YAML frontmatter，对齐 agentskills.io）。
存储：task 级 `tasks/<task_id>/skills/<skill_name>.md`；通用 skill 落用户级 `~/.omniagent/skills/`。
子结构（substeps/metadata）用 JSON 内嵌在 frontmatter 中，避免手写 YAML 解析。

Skill 生命周期：
  task success → 从 RunRecord 提取 substeps → 创建 candidate skill
  → 后续相同 objective_pattern 成功 → success_count += 1
  → 成功 3 次（连续）→ 晋升 active
  → 中间一次失败 → success_count 归零（打断连续性）
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from omni_core.local.runtime_paths import task_skills, global_skills


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
            f"description: {self.description}\n"
            f"tags_json: {json.dumps(self.tags, ensure_ascii=False)}\n"
            f"substeps_json: {ss}\n"
            f"metadata_json: {md}\n"
            f"---"
        )

    def to_markdown(self) -> str:
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

    # --- N=3 晋升 ------------------------------------------------------------
    def record_success(self) -> bool:
        self.metadata.success_count += 1
        self.metadata.total_uses += 1
        self.metadata.last_used = datetime.now(timezone.utc).isoformat()
        self.metadata.confidence = self.metadata.success_count / max(self.metadata.total_uses, 1)
        if self.metadata.success_count >= 3 and self.metadata.status == "candidate":
            self.metadata.status = "active"
            return True
        return False

    def record_failure(self, reason: str = "") -> None:
        self.metadata.failure_count += 1
        self.metadata.total_uses += 1
        self.metadata.success_count = 0
        self.metadata.last_used = datetime.now(timezone.utc).isoformat()
        self.metadata.confidence = self.metadata.success_count / max(self.metadata.total_uses, 1)
        if reason and reason not in self.metadata.known_failures:
            self.metadata.known_failures.append(reason[:200])


# ---------------------------------------------------------------------------
# Skill 库持久化
# ---------------------------------------------------------------------------

def _safe_filename(name: str) -> str:
    return "".join(c if c.isalnum() or c in "_-" else "_" for c in name)[:60]


class SkillLibrary:
    """skill 池 = task 级 tasks/<task_id>/skills/ + 全局 ~/.omniagent/skills/。

    recall（load/list/find）合并两层、task 优先；写入按 scope 分流：
    - scope == "global"（通用 skill，如 Curator 升级产出）→ 全局
    - 默认 / scope == "task" → task 级 tasks/<task_id>/skills/
    """

    def __init__(self, task_id: str):
        self.task_id = task_id
        self._proj_dir = task_skills(task_id)
        self._proj_dir.mkdir(parents=True, exist_ok=True)

    def _dir(self) -> Path:
        return self._proj_dir

    @staticmethod
    def _global_dir() -> Path:
        d = global_skills()
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _path(self, name: str) -> Path:
        return self._dir() / f"{_safe_filename(name)}.md"

    # ---- 双层级读取（项目优先 + 全局） ----
    def _all_paths(self) -> List[Path]:
        paths: List[Path] = []
        # 全局先列（项目优先：项目同名覆盖全局，故项目后列）
        g = self._global_dir()
        paths.extend(sorted(g.glob("*.md")))
        paths.extend(sorted(self._dir().glob("*.md")))
        return paths

    def _save_to(self, skill: Skill, directory: Path) -> str:
        if not skill.metadata.created:
            skill.metadata.created = datetime.now(timezone.utc).isoformat()
        text = skill.to_frontmatter() + "\n\n" + skill.to_markdown()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{_safe_filename(skill.name)}.md"
        path.write_text(text, encoding="utf-8")
        return str(path)
    def save(self, skill: Skill) -> str:
        """按 scope 分流落盘：global → 用户级；否则 → task 级 tasks/<task_id>/skills/。"""
        scope = skill.metadata.scope or "task"
        if scope == "global":
            return self._save_to(skill, self._global_dir())
        return self._save_to(skill, self._dir())

    def save_global(self, skill: Skill) -> str:
        """显式落全局（Curator 升级产出的通用 skill 用）。"""
        return self._save_to(skill, self._global_dir())

    def load(self, name: str) -> Optional[Skill]:
        # 项目优先：先看项目级，再回退全局
        path = self._path(name)
        if path.exists():
            return self._parse(path)
        gpath = self._global_dir() / f"{_safe_filename(name)}.md"
        if gpath.exists():
            return self._parse(gpath)
        return None

    def list_all(self) -> List[Skill]:
        # 项目优先：同名时项目覆盖全局（全局后列，但需去重同名）
        seen: set = set()
        out: List[Skill] = []
        for f in self._all_paths():
            if f.name in seen:
                continue
            seen.add(f.name)
            s = self._parse(f)
            if s:
                out.append(s)
        return out

    def list_global(self) -> List[Skill]:
        """仅列全局通用 skill（~/.omniagent/skills/），不含 task 私有。

        用于 Web 面板展示：task 私有 skill 不进全局列表、不共享，只在所属 task 内被消费
        （召回经 SkillLibrary(task_id) / `load_skill` 工具可见）。
        """
        out: List[Skill] = []
        for f in sorted(self._global_dir().glob("*.md")):
            s = self._parse(f)
            if s:
                out.append(s)
        return out

    def find_by_pattern(self, objective: str) -> List[Skill]:
        obj = (objective or "").strip()
        if not obj:
            return []
        obj_cjk = _cjk_bigrams(obj)
        # 两层召回：
        # 1) 精确子串（语言无关，兼容原 objective_pattern 与英文/短关键词，如 "截图"）；
        # 2) CJK 字符 bigram 重叠（中文语序无关弱匹配；仅取汉字，滤掉 rimworld 等拉丁噪声）。
        # 按相关性排序（精确命中优先，其次共享 bigram 数），取最相关者。
        scored = []
        for s in self.list_all():
            text = " ".join([
                s.name, s.objective_pattern, s.description,
                " ".join(s.tags), (s.body or "")[:1500],
            ])
            if obj.lower() in text.lower():
                scored.append((1000, s))
                continue
            if obj_cjk:
                inter = obj_cjk & _cjk_bigrams(text)
                if len(inter) >= 2:
                    scored.append((len(inter), s))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [s for _, s in scored]

    def delete(self, name: str) -> bool:
        path = self._path(name)
        if path.exists():
            path.unlink()
            return True
        gpath = self._global_dir() / f"{_safe_filename(name)}.md"
        if gpath.exists():
            gpath.unlink()
            return True
        return False

    # --- 录制 ----------------------------------------------------------------
    @classmethod
    def from_run_record(cls, record: dict, task_id: str) -> Optional[Skill]:
        if not record.get("success"):
            return None
        sd = record.get("steps_data") or []
        if len(sd) < 2:
            return None
        substeps = [SkillSubstep(tool=s.get("tool", ""), args=s.get("args") or {})
                    for s in sd if isinstance(s, dict)]
        return Skill(
            name=_derive_skill_name(record.get("objective", "")),
            objective_pattern=record.get("objective", ""),
            task_id=task_id,
            substeps=substeps,
            metadata=SkillMetadata(success_count=1, total_uses=1, confidence=1.0,
                                   status="candidate",
                                   created=datetime.now(timezone.utc).isoformat()),
        )

    # --- 晋升判定 ------------------------------------------------------------
    def promote_or_insert(self, candidate: Skill) -> Dict[str, Any]:
        existing = self.load(candidate.name)
        if existing is None:
            for s in self.list_all():
                if s.objective_pattern == candidate.objective_pattern:
                    existing = s
                    break
        if existing:
            if len(candidate.substeps) >= len(existing.substeps):
                existing.substeps = candidate.substeps
            promoted = existing.record_success()
            self.save(existing)
            return {"action": "promoted" if promoted else "updated",
                    "skill_name": existing.name, "promoted": promoted,
                    "success_count": existing.metadata.success_count}
        self.save(candidate)
        return {"action": "created", "skill_name": candidate.name,
                "promoted": False, "success_count": 1}

    def record_failure_on_pattern(self, objective: str, reason: str = "") -> None:
        for s in self.list_all():
            if s.objective_pattern and s.objective_pattern.lower() in objective.lower():
                s.record_failure(reason)
                self.save(s)

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
                body=body or "",
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


def _cjk_bigrams(s: str) -> set:
    """仅取汉字（CJK）字符成 bigram 集合，忽略空白与拉丁/数字字符。

    用于中文弱匹配：过滤掉 rimworld / http 等拉丁噪声，仅按汉字重叠判定相关性，
    且汉字 bigram 天然语序无关（"建造基地" 与 "基地建造" 共享 建造/基地）。
    长度≤1 时退化为单字符集合。
    """
    cjk = [c for c in (s or "") if "\u4e00" <= c <= "\u9fff"]
    if len(cjk) <= 1:
        return set(cjk)
    return {"".join(cjk[i:i + 2]) for i in range(len(cjk) - 1)}


def _derive_skill_name(objective: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff_]", "_", objective.strip()[:40])
    return f"skill_{name}" if name else "skill_unnamed"
