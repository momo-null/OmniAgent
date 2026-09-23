"""M3b 真机两层闭环验证：在 MuMu 模拟器活屏上跑通「在线大脑拆分 → 本地 4B 执行」两层编排。

环境约束（沙箱）：
- 8085 本地 4B-vl 在线（llama.cpp server，不校验 key）；
- 无在线 GLM key，故本验证用本地 4B **同时充当** brain + executor（两层逻辑走通，模型相同）；
- 真在线大脑（demo-model）需用户设 OMNI_BRAIN_API_KEY 后，把 brain_cfg.base_url 改指向在线端点再跑。

两层链路：
  brain.plan(objective) -> {subtasks[], overall_done_when}   （低频，仅一次）
  for each subtask:
      executor（本地 4B）跑内层 ReAct（同一套 tool 插件层/Vision/Execution）
      成功 -> 回报；触发升级条件 -> escalate 回在线大脑反思

结果落 temp/verify_two_layer_result.json + temp/verify_two_layer_progress.log。

用法（MuMu 已在跑、8085 已起）：
  .venv/Scripts/python.exe scripts/verify_two_layer_live.py
"""
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
RESULT = os.path.join(ROOT, "temp", "verify_two_layer_result.json")
PROGRESS = os.path.join(ROOT, "temp", "verify_two_layer_progress.log")


def log(msg: str):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(PROGRESS, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def main():
    os.makedirs(os.path.dirname(RESULT), exist_ok=True)
    open(PROGRESS, "w", encoding="utf-8").close()
    out: dict = {}

    import yaml
    with open(os.path.join(ROOT, "config.yaml"), "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    vision_cfg = dict(cfg.get("runtime", {}).get("vision", {}))

    # ---------- 复用已运行的 4B-vl（8085） ----------
    log("step1: 探测已运行的 4B-vl（不重复启动）...")
    from omni_core.tools.vision_runtime import VisionRuntime

    base_url = VisionRuntime._probe_local_vlm()
    if not base_url:
        raise RuntimeError("未探测到 4B-vl server（8080-8120）。请先启动 llama-server。")
    log(f"  复用 VLM: {base_url}")

    # ---------- 连模拟器 + 截图（活屏） ----------
    log("step2: 连接 MuMu 模拟器并截图（活屏）...")
    from devices import ExecutionModule

    exec_mod = ExecutionModule()  # runtime.backend=emulator
    shot = exec_mod.screenshot()
    log(f"  screenshot -> {json.dumps(shot, ensure_ascii=False)}")
    if not shot.get("ok"):
        raise RuntimeError(f"截图失败: {shot}")
    out["screenshot"] = shot.get("path")

    # ---------- 本地 4B 同时充当 brain + executor（沙箱无在线 key） ----------
    log("step3: 以本地 4B 同时充当 在线大脑 + 本地执行器（两层逻辑验证）...")
    os.environ["OMNI_BRAIN_API_KEY"] = os.environ.get("OMNI_BRAIN_API_KEY", "local")
    os.environ["OMNI_EXECUTOR_API_KEY"] = os.environ.get("OMNI_EXECUTOR_API_KEY", "local")

    brain_cfg = {
        "provider": "openai-compatible",
        "base_url": base_url,
        "model": "qwen3.5-4b-vl",
        "api_key_env": "OMNI_BRAIN_API_KEY",
        "capabilities": {"vision": True},
        "request": {"temperature": 0.3, "max_tokens": 2048},
    }
    # executor_cfg 直接传（绕过 config.runtime.executor.enabled），确保两层启用
    executor_cfg = {
        "provider": "openai-compatible",
        "enabled": True,
        "base_url": base_url,
        "model": "qwen3.5-4b-vl",
        "api_key_env": "OMNI_EXECUTOR_API_KEY",
        "capabilities": {"vision": True},
        "request": {"temperature": 0.3, "max_tokens": 2048},
    }

    from omni_core.local.tool_loop import ToolLoop, TaskSpec

    loop = ToolLoop(brain_cfg, executor_cfg=executor_cfg, verbose=True, max_history=12)
    out["tool_count"] = len(loop.registry.schemas)
    out["tool_names"] = [s["function"]["name"] for s in loop.registry.schemas]
    out["two_layer_enabled"] = loop.executor is not None
    log(f"  registry 工具数={out['tool_count']}; 两层已启用={out['two_layer_enabled']}")
    log(f"  工具: {out['tool_names']}")

    # ---------- 真机两层闭环 ----------
    objective = (
        "你正在操作当前实时设备界面。请自主完成一个只读偏安全的感知汇报任务：\n"
        "1) 调用 som_marks 标出屏幕上看起来可交互的区域（获得带编号的标记）；\n"
        "2) 调用 vision_describe 描述当前界面整体状态（处于哪个界面、有哪些关键元素）；\n"
        "3) 基于以上观察，判断当前界面是否存在「安全且不会离开当前界面」的可点元素"
        "（例如已激活的标签、可关闭的小提示）；若有，可轻点一次验证并立即重新观察确认界面无变化，"
        "但不要点击任何会进入新界面、触发关键流程或产生不可逆操作的按钮；\n"
        "4) 完成后调用 task_done 汇报你观察到的结果。\n"
        "默认请自己依次做完；只有当其中某几步互不依赖且各自耗时较长时，"
        "才用 dispatch 把它们派给子 agent 并行执行。"
    )
    spec = TaskSpec(objective=objective, done_when="", max_steps=8)
    log(f"step4: run_task 启动（统一入口，目标: {objective[:30]}...）")

    try:
        result = loop.run_task(spec)
        out["two_layer_result"] = result
        out["error"] = None
        log(f"  完成: success={result.get('success')} steps={result.get('steps')} "
            f"escalated={result.get('escalated')} reason={result.get('reason')}")
    except Exception as e:
        import traceback
        out["error"] = f"{type(e).__name__}: {e}"
        out["traceback"] = traceback.format_exc()
        log(f"  异常: {out['error']}")

    with open(RESULT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    log("DONE -> " + RESULT)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        log("ERROR:\n" + tb)
        with open(RESULT, "w", encoding="utf-8") as f:
            json.dump({"error": str(e), "traceback": tb}, f, ensure_ascii=False, indent=2)
        sys.exit(1)
