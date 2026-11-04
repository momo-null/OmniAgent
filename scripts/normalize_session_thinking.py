"""一次性归一：修复会话记录里被 O(n²) 膨胀的 thinking step 文本。

背景（2026-11-04）：`gateway/dispatch.py` 曾把 emitter 的「完整快照」当增量做
`content += text`，导致同一 block 的思考文本变成 s1+s2+…+sn（逐段加长的前缀拼接），
单条最长 3.2MB，`qq` 会话 jsonl 里 thinking 占 93% 体积。

还原思路（按优先级）：
1. **轨迹回填**（首选，无损）：同 task 的 trajectory 里 `kind=think` 记录存的是
   正确原文（由 `emitter._emit` 写出，SDK 侧正确增量累加），膨胀串一定以它结尾；
   取「最长的匹配后缀」即为原文。
2. **尾部双份检测**：`emitter._emit` 会在回合末再发一次完整快照，所以膨胀串常以
   P+P 结尾 → 用滚动哈希找最大的 k 使 `S[n-2k:n-k] == S[n-k:]`，取后半为原文。
3. 都不成立 → 跳过该条（只在报告里列出，绝不瞎猜写回）。

安全性：
- 默认 **dry-run**，只打印报告；加 ``--write`` 才落盘；
- 落盘前把整份 jsonl 备份为 ``<name>.bak-YYYYmmdd-HHMMSS``；
- 原子写（临时文件 + replace），只改 `extra.steps[].content` 一个字段。

用法：
    python scripts/normalize_session_thinking.py                # 只报告
    python scripts/normalize_session_thinking.py --write        # 备份后写回
    python scripts/normalize_session_thinking.py --project qq --min-chars 4000
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from omni_core.local import runtime_paths as RP  # noqa: E402

#: 数据根（``projects/`` + ``tasks/`` 的父目录）。默认取 runtime_paths 的 ~/.omniagent；
#: 桌面壳把 HOME 指到别处时（如 D:\\appdata\\omniagent）用 ``--home`` 显式指定。
_HOME: Optional[Path] = None


def _projects_root() -> Path:
    return (_HOME or RP.global_omni()) / "projects"


def _task_dir(task_id: str) -> Path:
    return (_HOME or RP.global_omni()) / "tasks" / task_id

#: 判定「疑似膨胀」：膨胀串 = s1+s2+…+sn（逐段加长的前缀），开头必然呈现
#: 「前一小块立刻重复」——`This|This`、`用户|用户`、`群里|群里`。正常思考不会这样开头。
_DOUBLING_MAX_BLOCK = 10

_BASE = 911382323
_MOD = (1 << 61) - 1


def _looks_inflated(text: str, min_chars: int) -> bool:
    if len(text) <= min_chars:
        return False
    for p in range(1, _DOUBLING_MAX_BLOCK + 1):
        if len(text) >= 2 * p and text[:p] == text[p:2 * p]:
            return True
    return False


def _traj_reasonings(task_id: str, cache: Dict[str, List[str]]) -> List[str]:
    """该 task 轨迹里所有「真实思考原文」（kind=think 且非 JSON 元记录）。"""
    if task_id in cache:
        return cache[task_id]
    out: List[str] = []
    try:
        d = _task_dir(task_id)
    except Exception:
        cache[task_id] = out
        return out
    if d.is_dir():
        for f in sorted(d.glob("*.jsonl")):
            try:
                with f.open(encoding="utf-8", errors="ignore") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except Exception:
                            continue
                        if rec.get("kind") != "think":
                            continue
                        c = rec.get("content")
                        if not isinstance(c, str) or not c or c.lstrip().startswith("{"):
                            continue
                        out.append(c)
            except Exception:
                continue
    cache[task_id] = out
    return out


def _restore_from_traj(text: str, reasonings: List[str]) -> Optional[str]:
    """膨胀串以原文结尾；取最长匹配后缀。要求原文显著更短（防误判正常长思考）。"""
    best: Optional[str] = None
    for r in reasonings:
        if len(r) < len(text) and text.endswith(r) and len(text) >= int(len(r) * 1.5):
            if best is None or len(r) > len(best):
                best = r
    return best


def _restore_by_double_tail(text: str) -> Optional[str]:
    """找最大的 k 使末尾两段相等（emitter 回合末重复快照），返回后半段。"""
    n = len(text)
    if n < 4:
        return None
    h = [0] * (n + 1)
    p = [1] * (n + 1)
    for i, ch in enumerate(text):
        h[i + 1] = (h[i] * _BASE + ord(ch)) % _MOD
        p[i + 1] = (p[i] * _BASE) % _MOD

    def sub_hash(lo: int, hi: int) -> int:
        return (h[hi] - h[lo] * p[hi - lo]) % _MOD

    for k in range(n // 2, 0, -1):
        if sub_hash(n - 2 * k, n - k) != sub_hash(n - k, n):
            continue
        if text[n - 2 * k:n - k] == text[n - k:]:        # 哈希命中后再精确比对
            return text[n - k:]
    return None


def _process_file(path: Path, min_chars: int, write: bool,
                  cache: Dict[str, List[str]]) -> Tuple[int, int, int, List[str]]:
    raw = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    lines: List[str] = []
    fixed = 0
    saved = 0
    skipped: List[str] = []
    for line in raw:
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except Exception:
            lines.append(line)
            continue
        mutated = False
        steps = (rec.get("extra") or {}).get("steps") or []
        for s in steps:
            if not isinstance(s, dict) or s.get("type") != "thinking":
                continue
            old = s.get("content") or ""
            if not _looks_inflated(old, min_chars):
                continue
            new = _restore_from_traj(old, _traj_reasonings(str(rec.get("task_id") or ""), cache))
            how = "traj"
            if not new:
                new = _restore_by_double_tail(old)
                how = "tail2"
            if not new or len(new) >= len(old):
                skipped.append(f"{str(rec.get('ts'))[:19]} len={len(old)} task={rec.get('task_id')}")
                continue
            s["content"] = new
            fixed += 1
            saved += len(old.encode("utf-8")) - len(new.encode("utf-8"))
            mutated = True
            print(f"    [{how}] {str(rec.get('ts'))[:19]} {len(old)} -> {len(new)}")
        lines.append(json.dumps(rec, ensure_ascii=False) if mutated else line)

    if write and fixed:
        # 并发保护：网关可能正在往同一份 jsonl 追加（一轮 run 结束就写）。
        # 最近 2 分钟内有写入就跳过，避免用旧快照覆盖掉刚落盘的新记录。
        if time.time() - path.stat().st_mtime < 120:
            print(f"  [跳过] {path.name} 2 分钟内有写入（会话可能正在跑），稍后再执行")
            return 0, 0, len(raw), skipped
        bak = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, bak)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        tmp.replace(path)
        print(f"  已写回（备份 {bak.name}）")
    return fixed, saved, len(raw), skipped


def main() -> int:
    ap = argparse.ArgumentParser(description="归一会话记录里膨胀的 thinking 文本")
    ap.add_argument("--write", action="store_true", help="真正写回（默认只报告）")
    ap.add_argument("--project", default="", help="只处理某个 project_id")
    ap.add_argument("--home", default="", help="数据根（含 projects/ 与 tasks/），默认 ~/.omniagent")
    ap.add_argument("--min-chars", type=int, default=4000, help="超过该长度才检查（默认 4000）")
    args = ap.parse_args()

    global _HOME
    if args.home:
        _HOME = Path(args.home)
        print(f"数据根：{_HOME}")

    root = _projects_root()
    if not root.is_dir():
        print(f"projects 目录不存在：{root}")
        return 1
    files = sorted(p for p in root.glob("*/*.jsonl") if not p.name.endswith(".bak"))
    if args.project:
        files = [p for p in files if p.parent.name == args.project]

    cache: Dict[str, List[str]] = {}
    total_fixed = total_saved = 0
    all_skipped: List[str] = []
    for f in files:
        before = f.stat().st_size
        fixed, saved, nlines, skipped = _process_file(f, args.min_chars, args.write, cache)
        all_skipped.extend(f"{f.parent.name}/{f.name} {x}" for x in skipped)
        if fixed:
            total_fixed += fixed
            total_saved += saved
            print(f"  {f.parent.name}/{f.name}: 记录 {nlines} 条，修复 {fixed} 处，"
                  f"省 {saved/1048576:.2f} MB（{before/1048576:.2f} MB）")
        elif skipped:
            print(f"  {f.parent.name}/{f.name}: 命中 {len(skipped)} 处但无法还原（已跳过）")

    print(f"\n合计：修复 {total_fixed} 处，节省 {total_saved/1048576:.2f} MB"
          f"{'（已写回）' if args.write else '（dry-run，未写盘）'}")
    if all_skipped:
        print(f"跳过 {len(all_skipped)} 处：")
        for x in all_skipped[:20]:
            print("  " + x)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
