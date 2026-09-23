"""K3 有效性裁决 · V2 抽检。

抽 ≥3 个 ``memory/rollouts/<tid>.md``，复核：
- facts 可溯源（头部含 trajectory 引用）
- lessons 归因合理（失败/高重试带 reason）
- 格式合规（含 ## facts / ## lessons / ## user_corrections 段落）

不通过 → 回 K0/K2 修蒸馏，不否定记忆价值本身（设计 §6）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from omni_core.local import runtime_paths as P  # noqa: E402
from omni_core.local.curator import _parse_rollout_sections  # noqa: E402


def _rollout_header(text: str) -> Dict[str, Any]:
    res = {"trajectory": "", "success": None}
    try:
        for ln in text.splitlines():
            s = ln.strip()
            if s.startswith("- task_id:"):
                tm = __import__("re").search(r"/ trajectory:\s*(\S+)", s)
                if tm:
                    res["trajectory"] = tm.group(1).strip()
                sm = __import__("re").search(r"/ success:\s*(true|false)", s, __import__("re").IGNORECASE)
                if sm:
                    res["success"] = sm.group(1).strip().lower() == "true"
                break
    except Exception:
        pass
    return res


def review_one(task_id: str, text: str) -> Dict[str, Any]:
    issues: List[str] = []
    hdr = _rollout_header(text)
    facts, lessons = _parse_rollout_sections(text)

    # 1. 可溯源：成功事实需有 trajectory 引用
    if hdr["success"] and not hdr["trajectory"]:
        issues.append("facts 不可溯源（缺 trajectory 引用）")
    # 2. lessons 归因合理：失败 lessons 应带 reason
    for l in lessons:
        if l.startswith("[失败]") or l.startswith("[高重试"):
            if "——" not in l:
                issues.append(f"lesson 缺归因: {l[:40]}")
    # 3. 格式合规：必须有三段
    for seg in ("## facts", "## lessons", "## user_corrections"):
        if seg not in text:
            issues.append(f"缺段落 {seg}")

    return {"task_id": task_id, "ok": len(issues) == 0, "issues": issues,
            "facts_n": len(facts), "lessons_n": len(lessons),
            "success": hdr["success"]}


def main() -> int:
    ap = argparse.ArgumentParser(description="K3 V2 rollout 抽检")
    ap.add_argument("--limit", type=int, default=3, help="抽检条数（默认 3）")
    args = ap.parse_args()

    d = P.memory_rollouts()
    if not d.exists():
        print(json.dumps({"ok": False, "reason": "无 rollouts 目录"}, ensure_ascii=False, indent=2))
        return 1

    files = sorted(d.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)[: args.limit]
    results = [review_one(p.stem, p.read_text(encoding="utf-8")) for p in files]
    all_ok = all(r["ok"] for r in results)
    out = {"ok": all_ok, "reviewed": len(results), "results": results}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
