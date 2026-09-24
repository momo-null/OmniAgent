"""MVP CLI 入口：跑通「大脑(demo-model) ↔ 本地执行器」闭环。

用法：
  set OMNI_BRAIN_API_KEY=tp-xxx
  python scripts/run_mvp.py --objective "打开计算器并计算(123+456)的结果" \
        --done_when "屏幕上显示579" --expected 579 --max_steps 20

成功判据：进程退出码 0 且 expected 命中 OCR 文本。
附带验证：把 --expected 改成错误值（如 000），应触发 task_done(卡住) 而非假成功。

注意：本脚本会真实操作你的电脑（开程序、敲键）。运行时请勿触碰鼠标键盘。
"""
import os
import sys
import json
import argparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import yaml
from omni_core.local.loop import ToolLoop, TaskSpec


def load_brain_cfg(config_path: str) -> dict:
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if "brain" not in cfg:
        raise RuntimeError(f"{config_path} 缺少 brain: 配置段")
    return cfg["brain"]


def main():
    p = argparse.ArgumentParser(description="OmniAgent MVP：大脑↔本地执行器闭环")
    p.add_argument("--objective", required=True, help="任务目标")
    p.add_argument("--done_when", default="", help="完成条件描述")
    p.add_argument("--expected", default=None, help="本地硬校验值（出现在 OCR 文本即成功），如 579")
    p.add_argument("--max_steps", type=int, default=20, help="最大步数兜底")
    p.add_argument("--config", default=os.path.join(ROOT, "config.yaml"), help="配置文件路径")
    p.add_argument("--quiet", action="store_true", help="关闭循环日志")
    args = p.parse_args()

    brain_cfg = load_brain_cfg(args.config)
    if not (brain_cfg.get("api_key") or os.environ.get(brain_cfg.get("api_key_env", "OMNI_BRAIN_API_KEY"))):
        sys.stderr.write(
            f"[错误] 未设置大脑 api key：可在 config 写 brain.api_key，"
            f"或设置环境变量 {brain_cfg.get('api_key_env')}。\n"
        )
        sys.exit(2)

    spec = TaskSpec(
        objective=args.objective,
        done_when=args.done_when,
        expected=args.expected,
        max_steps=args.max_steps,
    )

    print(f"[mvp] objective={args.objective!r} expected={args.expected!r} max_steps={args.max_steps}")
    loop = ToolLoop(brain_cfg, verbose=not args.quiet)
    result = loop.run_task(spec)
    print("\n[mvp] RESULT:", json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result["success"] else 1)


if __name__ == "__main__":
    main()
