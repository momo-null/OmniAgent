"""M2.5 自主 harness：给 agent 一个真实子目标，观察它是否自主编排出
mark -> 点击 -> 校验 -> 重试 闭环。

前置（需用户环境就绪）：
  - MuMu 模拟器在跑（config.runtime.backend=emulator，ADB 已连）
  - 本地 4B-vl llama-server 在跑（runtime.vision.enabled=true）
  - 环境变量 OMNI_BRAIN_API_KEY 已设（大脑 demo-model 的 key）

用法：
  python scripts/run_agent_autonomous.py "进入主界面并点开『出击』"
  python scripts/run_agent_autonomous.py "观察当前界面并描述你看到了什么" "" 12
"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from omni_core.local.loop import ToolLoop, TaskSpec


def main():
    objective = sys.argv[1] if len(sys.argv) > 1 else "观察当前界面，描述你看到了什么"
    done_when = sys.argv[2] if len(sys.argv) > 2 else ""
    max_steps = int(os.environ.get("OMNI_MAX_STEPS", "20"))

    cfg = config.load_config() or {}
    brain_cfg = cfg.get("brain", {})
    if not (brain_cfg.get("api_key") or os.environ.get(brain_cfg.get("api_key_env", "OMNI_BRAIN_API_KEY"), "")):
        print("[harness] 警告：未检测到大脑 api key（config.brain.api_key 或环境变量），brain.chat 会失败")

    loop = ToolLoop(brain_cfg, verbose=True)
    spec = TaskSpec(objective=objective, done_when=done_when, max_steps=max_steps)
    print(f"[harness] 目标：{objective}")
    result = loop.run_task(spec)
    print("[harness] 结果：", json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
