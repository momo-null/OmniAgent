"""M4b.1 checkpoint 补验证：用必定成功的两层任务验证 checkpoint 写入。

策略：plan 后强制清空子任务 done_when → worker verify 无条件信任 → subtask 成功 → checkpoint。
"""
import json
import sys
import time
import os
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from omni_core.local.tool_loop import ToolLoop, TaskSpec
from omni_core.local.world_model import WorldModel
from omni_core.local.runtime_paths import project_world_model, project_trajectory

# 构造两层 ToolLoop
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
        "api_key": "not-needed-local",
        "capabilities": {"vision": True},
        "request": {"temperature": 0.3, "max_tokens": 2048},
    },
    escalation_cfg={
        "inner_step_max": 5,
        "wallclock_sec": 120,
        "verify_fail_max": 3,
        "no_confidence_hard": True,
        "worker_history_keep": 3,
    },
)

spec = TaskSpec(
    objective="截一张屏幕截图然后立刻完成任务",
    done_when="",
    max_steps=20,
    app="default",
)

# 手动编排两层流程（复用 plan/run_subtask/reflect，但强制空 done_when）
loop._reset_run_counters()
world = WorldModel(app=spec.app)
world.run_id = uuid.uuid4().hex[:12]
world.objective = spec.objective

plan = loop._plan(spec, world)
subtasks = plan.get("subtasks") or []
if not subtasks:
    subtasks = [{"desc": spec.objective, "done_when": ""}]

# ★ 强制清空所有 done_when：部分应用 OCR 为空，任何文字条件都无法通过 verify
for st in subtasks:
    st["done_when"] = ""

print(f"[plan] {len(subtasks)} subtasks: {[s['desc'] for s in subtasks]}")
t0 = time.time()

completed = 0
for i, st in enumerate(subtasks):
    inner_spec = TaskSpec(
        objective=st.get("desc", ""),
        done_when=st.get("done_when", ""),
        expected=None,  # 不触发 OCR 硬校验
        max_steps=5,
    )
    res = loop._run_subtask(inner_spec, parent_world=world)
    print(f"\n[subtask {i+1}] success={res['success']} escalated={res['escalated']} reason={res['reason']}")
    if res.get("success"):
        completed += 1
        world.checkpoint(st)

world.save()
elapsed = time.time() - t0
print(f"\n[总结果] {completed}/{len(subtasks)} 完成  elapsed={elapsed:.1f}s")

# --- 验证产出 ---
wm_dir = project_world_model(spec.app)
ckpt_dir = wm_dir / "checkpoints"
md = wm_dir / "current.md"

print(f"\n{'='*40}")
print(f"current.md: {'✅ 存在' if md.exists() else '❌ 不存在'} ({md.stat().st_size if md.exists() else 0} 字节)")
if md.exists():
    for l in md.read_text(encoding="utf-8").split("\n")[:15]:
        print(f"  {l}")

ckpts = sorted(ckpt_dir.glob("*/*.json")) if ckpt_dir.exists() else []
print(f"\ncheckpoints: {'✅ ' + str(len(ckpts)) + ' 个' if ckpts else '❌ 无'}")
for c in ckpts:
    d = json.loads(c.read_text(encoding="utf-8"))
    print(f"  - {c.relative_to(wm_dir)}  subgoal={d.get('subgoal',{}).get('desc','?')}")

traj_dir = project_trajectory(spec.app)
jsonls = sorted(traj_dir.glob("*.jsonl")) if traj_dir.exists() else []
reports = sorted((traj_dir / "reports").glob("*.json")) if (traj_dir / "reports").exists() else []
print(f"\ntrajectory: {len(jsonls)} JSONL  {len(reports)} reports")
if reports:
    d = json.loads(reports[0].read_text(encoding="utf-8"))
    for k in ["success","brain_calls","decision_steps","action_count"]:
        print(f"  {k}: {d.get(k)}")
