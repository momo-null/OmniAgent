"""M0-M4 全功能真机验证 harness（大脑=u2，执行器=本地 4B-vl）。

覆盖里程碑：
  M0  本地 4B-vl llama-server 在线（executor 通道）
  M1  EmulatorBackend(u2/ADB) 截图 + 层级树
  M2  全工具链 + SoM + 视觉通道（vision_describe / som_marks）
  M3b 两层编排：u2 规划 + 本地 4B 执行 + escalate/verify 门控
  M4a 轨迹落盘 + telemetry + 显式 verify
  M4b.1 world-model 持久化（current.md + checkpoint）
  M4b.2 skill 库 + N=3 晋升（同 objective 连跑 3 次触发）
  M4b.3 Curator 触发式维护（任务后跑，标 excluded）

大脑模型从 config.brain 读取（当前=u2），不硬编码；换模型只改 config。

环境前置（用户本机）：
  - MuMu 模拟器在跑（ADB 127.0.0.1:7555）
  - 本地 4B-vl llama-server 在 8085（model_hub 或手动起）
  - config.brain.api_key 已设（u2 的 key）

用法（同 objective 连跑 3 次以触发 skill N=3 晋升）：
  .venv/Scripts/python.exe scripts/verify_m0_m4_live.py "观察主界面并描述可见元素" default 8
  .venv/Scripts/python.exe scripts/verify_m0_m4_live.py "观察主界面并描述可见元素" default 8
  .venv/Scripts/python.exe scripts/verify_m0_m4_live.py "观察主界面并描述可见元素" default 8
"""
import os
import sys
import json
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
RESULT = os.path.join(ROOT, "temp", "m0m4_result.json")
PROGRESS = os.path.join(ROOT, "temp", "m0m4_progress.log")


def log(msg: str):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(PROGRESS, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def main():
    os.makedirs(os.path.dirname(RESULT), exist_ok=True)
    open(PROGRESS, "w", encoding="utf-8").close()

    objective = sys.argv[1] if len(sys.argv) > 1 else "观察当前界面，描述你看到了什么"
    app = sys.argv[2] if len(sys.argv) > 2 else "default"
    max_steps = int(sys.argv[3]) if len(sys.argv) > 3 else "12"

    import yaml
    from omni_core.local.tool_loop import ToolLoop, TaskSpec
    from omni_core.local.world_model import WorldModel
    from omni_core.local.skill_library import SkillLibrary

    cfg = yaml.safe_load(open(os.path.join(ROOT, "config.yaml"), encoding="utf-8"))
    brain_cfg = cfg["brain"]
    executor_cfg = cfg["runtime"].get("executor", {})
    # 本地 llama-server 一般不要求校验 api_key；若 config 未设且环境变量也未设，兜底 dummy 避免 executor 被跳过
    if executor_cfg and executor_cfg.get("enabled") and not executor_cfg.get("api_key"):
        env_key = executor_cfg.get("api_key_env", "OMNI_EXECUTOR_API_KEY")
        if not os.environ.get(env_key):
            executor_cfg = dict(executor_cfg)
            executor_cfg["api_key"] = "dummy"
            log("executor api_key 兜底为 dummy（llama-server OpenAI 兼容端点通常不校验）")
    log(f"大脑: {brain_cfg['base_url']} / {brain_cfg['model']}")
    log(f"执行器: {executor_cfg.get('base_url')} / {executor_cfg.get('model')} (enabled={executor_cfg.get('enabled')})")
    log(f"app={app}  objective={objective[:40]}...")

    # ---------- 两层 ToolLoop（大脑=u2，执行器=4B） ----------
    loop = ToolLoop(brain_cfg, executor_cfg=executor_cfg, verbose=True)
    log(f"registry 工具数={len(loop.registry.schemas)} 两层已启用={loop.executor is not None}")

    spec = TaskSpec(objective=objective, done_when="", max_steps=max_steps, app=app)
    log("run_task 启动...")
    result = loop.run_task(spec)
    log(f"完成: success={result.get('success')} steps={result.get('steps')} "
        f"escalated={result.get('escalated')} reason={result.get('reason')}")

    # ---------- M4 产物巡检 ----------
    report = {"result": result}

    # M4a 轨迹
    try:
        from omni_core.local.runtime_paths import project_trajectory
        traj_dir = str(project_trajectory(app))
    except Exception:
        traj_dir = os.path.join(ROOT, cfg["runtime"]["trajectory"].get("dir") or ".omniagent/trajectory", app)
    if os.path.isdir(traj_dir):
        jsls = [f for f in os.listdir(traj_dir) if f.endswith(".jsonl")]
        runjs = [f for f in os.listdir(traj_dir) if f.endswith(".run.json")]
        report["trajectory"] = {"dir": traj_dir, "jsonl": len(jsls), "run_json": len(runjs)}
        log(f"M4a 轨迹: {len(jsls)} jsonl + {len(runjs)} run.json -> {traj_dir}")

    # M4b.1 world-model
    wm = WorldModel(app=app)
    if wm.load():
        report["world_model"] = {
            "facts": wm.facts,
            "checkpoints": len(WorldModel.list_checkpoints(app)),
        }
        log(f"M4b.1 world-model: {len(wm.facts)} facts, "
            f"{len(WorldModel.list_checkpoints(app))} checkpoints")
    else:
        log("M4b.1 world-model: current.md 未生成")

    # M4b.2 skill 库（含晋升状态）
    try:
        sl = SkillLibrary(app=app)
        skills = sl.list_all()
        report["skills"] = [
            {"name": s.metadata.name, "status": s.metadata.status,
             "success_count": s.metadata.success_count}
            for s in skills
        ]
        n_active = sum(1 for s in skills if s.metadata.status == "active")
        n_candidate = sum(1 for s in skills if s.metadata.status == "candidate")
        log(f"M4b.2 skill 库: {len(skills)} 个 (active={n_active}, candidate={n_candidate})")
    except Exception as e:
        report["skills_error"] = str(e)
        log(f"M4b.2 skill 库读取失败: {e}")

    # M4b.3 Curator excluded
    excluded_dir = os.path.join(traj_dir, "excluded") if os.path.isdir(traj_dir) else None
    if excluded_dir and os.path.isdir(excluded_dir):
        ex = [f for f in os.listdir(excluded_dir) if f.endswith(".json")]
        report["curator_excluded"] = len(ex)
        log(f"M4b.3 Curator: {len(ex)} 条 excluded")

    with open(RESULT, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
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
