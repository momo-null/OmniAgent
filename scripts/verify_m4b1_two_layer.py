"""M4b.1 真机两层闭环验证：测试 world_model 持久化 + 子目标 checkpoint。

环境要求：
- MuMu 模拟器 127.0.0.1:7555 在线
- 本地 4B-vl server 127.0.0.1:8085 在线
- 在线大脑 demo-model 可调用

产出：
- .omniagent/world_model/<app>/current.md（持久 world state）
- .omniagent/world_model/<app>/checkpoints/<run_id>/<subgoal>.json（子目标 checkpoint）
- .omniagent/trajectory/<app>/*.jsonl（轨迹）
- .omniagent/trajectory/<app>/reports/<run_id>.json（telemetry）
"""
import json
import sys
import os
import time
from pathlib import Path

# 仓库根加入 path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from omni_core.local.tool_loop import ToolLoop, TaskSpec
from omni_core.local.runtime_paths import project_world_model, project_trajectory


def main():
    print("=" * 60)
    print("M4b.1 真机两层闭环验证")
    print("=" * 60)

    # 构造 ToolLoop（两层模式：在线 brain 规划 + 本地 4B 执行）
    loop = ToolLoop(
        brain_cfg={
            "model": "demo-model",
            "base_url": "https://api.example.com/v1",
            "api_key": os.environ.get("OMNI_BRAIN_API_KEY", ""),
            "capabilities": {"vision": False},
            "request": {"temperature": 0.3, "max_tokens": 2048},
        },
        verbose=True,
        executor_cfg={
            "enabled": True,
            "model": "qwen3.5-4b-vl",
            "base_url": "http://127.0.0.1:8085",
            "api_key": "not-needed-local",  # 本地 llama-server 无需真实 key，LLMClient 要求非空
            "capabilities": {"vision": True},
            "request": {"temperature": 0.3, "max_tokens": 2048},
        },
        escalation_cfg={
            "inner_step_max": 5,       # 缩短为 5 步，加速验证
            "wallclock_sec": 90,        # 缩短超时
            "verify_fail_max": 2,       # 2 次失败即升级
            "no_confidence_hard": True,
            "worker_history_keep": 3,
        },
    )

    # 修复版验证：大脑自主选择感知工具（observe / ocr_screenshot / vision_describe）
    # done_when="" 让 verify 无条件信任大脑——先验证工具选择与闭环，后续再加 OCR 条件
    spec = TaskSpec(
        objective="观察当前屏幕画面，截一张图，识别画面中的文字，然后报告完成了",
        done_when="",
        max_steps=20,
        app="default",
    )

    print(f"\n[任务] {spec.objective}")
    print(f"[app] {spec.app}")
    t0 = time.time()

    try:
        result = loop.run_task(spec)
    except Exception as e:
        print(f"\n[异常] {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        result = {"success": False, "reason": str(e), "steps": 0}

    elapsed = time.time() - t0
    print(f"\n{'=' * 60}")
    print(f"[结果] success={result.get('success')}  reason={result.get('reason')}  steps={result.get('steps')}")
    print(f"[耗时] {elapsed:.1f}s")
    if result.get("metrics"):
        print(f"[指标] {json.dumps(result['metrics'], ensure_ascii=False)}")

    # --- 验证 world_model 持久化产出 ---
    print(f"\n{'=' * 60}")
    print("world_model 持久化验证")
    print("=" * 60)

    wm_dir = project_world_model(spec.app)
    md_file = wm_dir / "current.md"
    print(f"\n1. current.md: {md_file}")
    if md_file.exists():
        print(f"   大小: {md_file.stat().st_size} 字节")
        print("--- 内容前 30 行 ---")
        lines = md_file.read_text(encoding="utf-8").split("\n")[:30]
        for l in lines:
            print(f"   {l}")
    else:
        print("   ❌ 不存在（world_model.save() 未被调用或失败）")

    ckpt_dir = wm_dir / "checkpoints"
    print(f"\n2. checkpoints: {ckpt_dir}")
    if ckpt_dir.exists():
        ckpts = sorted(ckpt_dir.glob("*/*.json"))
        print(f"   文件数: {len(ckpts)}")
        for c in ckpts:
            print(f"   - {c.relative_to(wm_dir)}")
            try:
                data = json.loads(c.read_text(encoding="utf-8"))
                print(f"     subgoal: {data.get('subgoal', {}).get('desc', '?')}")
                print(f"     facts: {len(data.get('world_snapshot', {}).get('facts', []))}")
            except Exception as e:
                print(f"     解析失败: {e}")
    else:
        print("   ❌ 不存在（无子任务完成或 checkpoint 未触发）")

    # --- 验证 trajectory 产出 ---
    print(f"\n3. trajectory:")
    traj_dir = project_trajectory(spec.app)
    jsonl_files = sorted(traj_dir.glob("*.jsonl")) if traj_dir.exists() else []
    print(f"   JSONL 文件: {len(jsonl_files)}")
    for jf in jsonl_files:
        print(f"   - {jf.relative_to(traj_dir)} ({jf.stat().st_size} 字节, "
              f"{sum(1 for _ in jf.read_text(encoding='utf-8').splitlines())} 行)")

    report_dir = traj_dir / "reports"
    reports = sorted(report_dir.glob("*.json")) if report_dir.exists() else []
    print(f"   report 文件: {len(reports)}")
    for r in reports:
        print(f"   - {r.relative_to(traj_dir)}")
        try:
            data = json.loads(r.read_text(encoding="utf-8"))
            for k in ["success", "brain_calls", "decision_steps", "action_count"]:
                print(f"     {k}: {data.get(k)}")
        except Exception as e:
            print(f"     解析失败: {e}")

    print(f"\n{'=' * 60}")
    print("验证完成")


if __name__ == "__main__":
    main()
