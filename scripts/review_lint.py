"""OmniAgent 内核红线 lint。

目的：自动检测内核是否违反「通用 agent 不写死领域」的三条红线，
避免每次加功能都靠人工 review 是否又写死了屏幕/工具/领域假设。

红线定义：
  R1 屏幕字段泄露：内核文件里把 ocr_text / ui_tree / active_window
      当作数据访问（字典 key / .get / 属性调用），而非注释说明。
  R2 具体工具名分支：内核文件里对具体设备工具名做 if 分支
      （内核元工具 verify/plan/task_done/record/collect_list 豁免）。
  R3 text_of 非抽象：devices/base.py 里 text_of / verify_done 仍有默认实现体
      （必须用 @abstractmethod，内核不预设感知读法/完成判定）。

约束域（M4 起「自动发现」，新增文件默认受约束，不用再手工登记）：
  KERNEL_DIRS   omni_core/**/*.py        -> 强制 R1 + R2
  PLUGIN_EXEMPT omni_core/tools/**       -> 豁免（L1 能力插件层，本就是能力实现）
  CAPA_EXEMPT   —— 无（SoM/VLM 实现已随 §5.4 迁到 omni_core/tools/vision_runtime.py）
  DEVICE_DIRS   devices/**/*.py          -> 豁免 R1/R2（设备驱动）；base.py 额外强制 R3

用法：
  python scripts/review_lint.py            # 扫描内核目录，打印违规
  python scripts/review_lint.py --strict   # 任何违规即非零退出（CI 用）
退出码：0=无违规，1=有违规，2=参数/IO 错误。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KERNEL_DIRS = ["omni_core"]
DEVICE_DIRS = ["devices"]

# L1 能力层豁免（相对仓库根的目录前缀 / 具体文件）
PLUGIN_EXEMPT_DIRS = ["omni_core/tools"]   # 含 vision_runtime.py（SoM/VLM 实现，§5.4）
PLUGIN_EXEMPT_FILES: list[str] = []


def _posix(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


# R1：作为"数据访问"的屏幕字段模式（排除纯注释/文档字符串里的提及）
SCREEN_FIELD_RE = re.compile(
    r'(\[\s*["\'](ocr_text|ui_tree|active_window)["\']\s*\]'   # dict["ocr_text"]
    r'|\.get\(\s*["\'](ocr_text|ui_tree|active_window)["\']'   # .get("ocr_text"
    r'|current_(ocr_text|ui_tree|active_window)\('             # world.current_ocr_text()
    r'|["\'](ocr_text|ui_tree|active_window)["\']\s*:)'         # dict literal key
)

# R2：内核里对具体工具名做分支（豁免内核元工具）。
TOOL_BRANCH_RE = re.compile(r'(?:if|elif)\b[^\n]*?\.?name\s*==\s*["\']([\w]+)["\']')

# R3：text_of / verify_done 是否仍是"有默认实现体"（出现 @abstractmethod 即合规）
ABSTRACT_RE = re.compile(r"@abstractmethod")

KERNEL_META_TOOLS = {"verify", "plan", "task_done", "record", "collect_list", "escalate"}


def _read(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except Exception as e:  # noqa: BLE001
        print(f"[ERR] 无法读取 {path}: {e}", file=sys.stderr)
        return []


def _strip_inline_comment(line: str) -> str:
    """去掉行内 # 注释，降低误报（允许注释里解释'为何不再读 ocr'）。"""
    in_str = None
    for i, ch in enumerate(line):
        if in_str:
            if ch == in_str:
                in_str = None
        elif ch in ("'", '"'):
            in_str = ch
        elif ch == "#":
            return line[:i]
    return line


def check_r1_screen_field(lines: list[str], path: Path, findings: list[str]) -> None:
    rel = _posix(path)
    for n, raw in enumerate(lines, 1):
        code = _strip_inline_comment(raw)
        if not code.strip():
            continue
        m = SCREEN_FIELD_RE.search(code)
        if m:
            # 豁免：抽象方法 docstring 里的接口契约说明（含"返回"/"dict("等说明词，
            # 非代码访问）。
            if any(k in code for k in ("返回", "dict(", "（如", "示例")):
                continue
            findings.append(
                f"[R1] {rel}:{n} 内核直接访问屏幕字段 {m.group(0)!r} "
                f"（应改为 backend.text_of/verify_done 抽象，不预设字段）"
            )


def check_r2_tool_branch(lines: list[str], path: Path, findings: list[str]) -> None:
    rel = _posix(path)
    for n, raw in enumerate(lines, 1):
        code = _strip_inline_comment(raw)
        for m in TOOL_BRANCH_RE.finditer(code):
            name = m.group(1)
            if name in KERNEL_META_TOOLS:
                continue
            findings.append(
                f"[R2] {rel}:{n} 内核对具体工具名 {name!r} 分支 "
                f"（工具应在 agent 外层插件层按名平级派发，内核零分支）"
            )


def check_r3_abstract_backend(lines: list[str], path: Path, findings: list[str]) -> None:
    """仅设备抽象基类：text_of / verify_done 必须是 @abstractmethod。"""
    rel = _posix(path)
    text = "\n".join(lines)
    for method in ("text_of", "verify_done"):
        m = re.search(rf"def\s+{method}\s*\(", text)
        if not m:
            continue
        segment = text[: m.start()]
        last_def = segment.rfind("def ")
        before = segment[last_def:] if last_def != -1 else segment
        if not ABSTRACT_RE.search(before):
            findings.append(
                f"[R3] {rel} `{method}` 仍有默认实现体 "
                f"（应改为 @abstractmethod，内核不预设感知读法/完成判定）"
            )


def discover_files(dirs: list[str]) -> list[Path]:
    files: list[Path] = []
    for d in dirs:
        base = ROOT / d
        if not base.is_dir():
            print(f"[SKIP] 目录不存在: {d}", file=sys.stderr)
            continue
        files.extend(sorted(base.rglob("*.py")))
    return [f for f in files if "__pycache__" not in f.parts]


def is_exempt(path: Path) -> bool:
    rel = _posix(path)
    if rel in PLUGIN_EXEMPT_FILES:
        return True
    return any(rel.startswith(d.rstrip("/") + "/") for d in PLUGIN_EXEMPT_DIRS)


def main() -> int:
    ap = argparse.ArgumentParser(description="OmniAgent 内核红线 lint")
    ap.add_argument("--strict", action="store_true", help="有违规即非零退出（CI 用）")
    args = ap.parse_args()

    findings: list[str] = []

    kernel_files = [f for f in discover_files(KERNEL_DIRS) if not is_exempt(f)]
    if not kernel_files:
        print(f"[ERR] 未发现内核文件: {KERNEL_DIRS}", file=sys.stderr)
        return 2
    for p in kernel_files:
        lines = _read(p)
        check_r1_screen_field(lines, p, findings)
        check_r2_tool_branch(lines, p, findings)

    # 设备驱动层：豁免 R1/R2（本就是设备实现），但对抽象基类强制 R3
    for p in discover_files(DEVICE_DIRS):
        if p.name == "base.py":
            check_r3_abstract_backend(_read(p), p, findings)

    scanned = ", ".join(f"{len(kernel_files)} 内核文件" for _ in [0])
    if not findings:
        print(f"OK: 内核未检测到红线违规（已扫描 {scanned} + devices/）。")
        return 0

    print(f"\n发现 {len(findings)} 处红线风险：\n")
    for f in findings:
        print("  " + f)
    print(
        "\n说明：R1=屏幕字段泄露 / R2=具体工具名分支 / R3=text_of非抽象。"
        "L1 能力层（omni_core/tools/**）与设备层（devices/**）豁免 R1/R2。"
    )
    return 1 if args.strict else 0


if __name__ == "__main__":
    raise SystemExit(main())
