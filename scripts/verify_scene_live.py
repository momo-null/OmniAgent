"""L2 场景执行器 + 收尾意图门 真机验证(真实端点,取 main 槽位当前配置模型)。

覆盖单测测不到的四件事:
  S1 场景维护真实 agent 循环:真实多轮 scene_read/scene_write、META 落盘、索引同步;
  S2 容量红色预警真实合并:压到 max_blocks=2,看模型是否按纪律 MERGE + 软删;
  S3 意图门真实判定:用本次问题的**历史真实文本**当夹具(DSML 退化样本 vs 正常收尾报告),
     验证 LLM 语义判断能否区分"未派发调用"与"收尾陈述";
  S4 端到端 run_task:真实 PowerShell 只读任务,核对注入链(system 稳定段/lead-in)、
     意图门链路与任务结果。

隔离纪律:全部使用 zz-live-* 一次性项目,不触碰用户 default 项目的记忆数据;
api_key 只读不打印。结果落 temp/verify_scene_live_result.json。

用法: python scripts/verify_scene_live.py [--skip-e2e]
"""
import json
import os
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
RESULT = os.path.join(ROOT, "temp", "verify_scene_live_result.json")

LIVE_PROJECTS = ["zz-live-scene", "zz-live-cap", "zz-live-e2e"]
_created_tasks: list = []


def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _mask(cfg):
    return {k: ("***" if "key" in k.lower() else v) for k, v in (cfg or {}).items()}


def _resolve_brain_cfg() -> dict:
    """与生产同源:端点 = models.json main 槽位;引擎参数 = config.yaml brain 非端点键。"""
    import config
    from omni_core.brain import router as model_router

    brain_src = (config.load_config() or {}).get("brain") or {}
    cfg = {"reasoning_mode": brain_src.get("reasoning_mode", "native"),
           "maxInputTokens": brain_src.get("maxInputTokens", 0)}
    selected = model_router.resolve_slot("main")
    if not selected or not selected.get("base_url"):
        raise RuntimeError("models.json main 槽位未配置端点")
    cfg.update(selected)
    return cfg


def _cleanup_projects():
    from omni_core.local import runtime_paths as rp
    for pid in LIVE_PROJECTS:
        for path in (rp.project_dir(pid),):
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)
    for tid in _created_tasks:
        td = rp.task_dir(tid)
        if td.exists():
            shutil.rmtree(td, ignore_errors=True)


# ---------------------------------------------------------------- S1 场景维护
BLOCK_EXISTING = (
    "-----META-START-----\n"
    "created: 2026-10-05T10:00:00+00:00\n"
    "updated: 2026-10-06T18:20:00+00:00\n"
    "summary: Windows C盘扫描的可靠操作方式与环境事实\n"
    "heat: 3\n"
    "-----META-END-----\n\n"
    "## 可靠操作方式\n"
    "- 递归统计目录占用用 PowerShell: Get-ChildItem -Recurse 比 cmd dir 快\n"
    "- 大目录扫描必须设 timeout_sec,曾因 get_top_dirs.ps1 无超时卡 180s+\n"
    "\n## 怪癖与坑\n"
    "- Windows 路径单反斜杠在 JSON 参数里可直接用\n"
)

EVIDENCE = (
    "[会话要点]\n"
    "[user] 分析d盘文件占用,找出最大的那一批文件\n"
    "[assistant] 用 Get-PSDrive D 查得 D 盘总容量 345.21GB,已用 299.2GB,剩余 46.01GB。\n"
    "[assistant] 用 PowerShell 递归统计一级目录占用:D:\\Program Files (x86) 占 203.75GB,\n"
    "D:\\AI 占 40.09GB;扫描脚本 scan_d.ps1 用 $ErrorActionPreference = 'SilentlyContinue'\n"
    "跳过无权限目录,300s 超时内完成;发现 $root 路径拼接必须用反引号转义,否则 PowerShell\n"
    "把 D:\\ 当转义序列;单个文件最大的是 pagefile.sys 与某 4GB 级压缩包。\n"
    "[世界状态]\n"
    "# 最近动作\n"
    "- shell_exec(Get-PSDrive D ...) -> ok, returncode 0\n"
    "- write_file(scan_d.ps1) -> ok; shell_exec(powershell -File scan_d.ps1) -> ok\n"
)


def s1_scene_maintenance(brain_cfg: dict) -> dict:
    from omni_core.local import scene_executor
    from omni_core.local.task_store import TaskStore

    out: dict = {}
    _created_tasks.append(TaskStore.create(objective="live-s1", project_id="zz-live-scene")["task_id"])
    _write_block("zz-live-scene", "windows-环境事实.md", BLOCK_EXISTING)

    stats: dict = {}
    dbg = []
    t0 = time.time()
    scene_executor.maintain_scenes("zz-live-scene", brain_cfg, EVIDENCE, stats=stats,
                                   on_debug=lambda k, p: dbg.append(
                                       {"kind": k, "p": str(p)[:220]}))
    out["elapsed_s"] = round(time.time() - t0, 1)
    out["stats"] = {k: (str(v)[:220] if isinstance(v, str) else v)
                    for k, v in stats.items() if k != "error"} | (
        {"error": stats["error"]} if "error" in stats else {})
    out["llm_debug"] = dbg
    out["reply_preview"] = str(stats.get("reply_preview") or "")

    entries = scene_executor.read_scene_index("zz-live-scene")
    out["blocks"] = [{"filename": e["filename"], "summary": e["summary"][:60],
                      "heat": e["heat"]} for e in entries]
    out["has_error"] = "error" in stats
    out["agent_did_work"] = bool(entries) and stats.get("empty_maintenance") is False
    # 期望:模型把 D 盘的新知识整合进块(更新 windows-环境事实 或新建 d盘 块)
    out["pass"] = (not out["has_error"]) and out["agent_did_work"]
    for line in _dump_blocks("zz-live-scene"):
        log("    " + line)
    return out


def _write_block(pid, name, content):
    from omni_core.local import scene_executor
    d = scene_executor.scene_blocks_dir(pid)
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(content, encoding="utf-8")


def _dump_blocks(pid) -> list:
    from omni_core.local import scene_executor
    d = scene_executor.scene_blocks_dir(pid)
    lines = []
    for p in sorted(d.glob("*.md")) if d.exists() else []:
        raw = p.read_text(encoding="utf-8")
        meta, _ = scene_executor.parse_scene_block(raw)
        lines.append(f"[block] {p.name} heat={meta['heat']} summary={meta['summary'][:50]} "
                     f"chars={len(raw)}")
    return lines


# ---------------------------------------------------------------- S2 容量红色预警
def _block(pid, name, summary, heat, fact):
    _write_block(pid, name,
                 "-----META-START-----\n"
                 f"created: 2026-10-01T00:00:00+00:00\nupdated: 2026-10-06T00:00:00+00:00\n"
                 f"summary: {summary}\nheat: {heat}\n"
                 "-----META-END-----\n\n"
                 f"## 环境事实\n- {fact}\n")


def s2_capacity_merge(brain_cfg: dict) -> dict:
    from omni_core.local import scene_executor

    pid = "zz-live-cap"
    out: dict = {}
    _write_block(pid, "环境-构建.md", "-----META-START-----\ncreated: 2026-10-01T00:00:00+00:00\n"
                 "updated: 2026-10-06T00:00:00+00:00\nsummary: 构建工具链环境\nheat: 9\n"
                 "-----META-END-----\n\n## 环境事实\n- 依赖版本影响构建兼容\n")
    _write_block(pid, "网络-代理.md", "-----META-START-----\ncreated: 2026-10-01T00:00:00+00:00\n"
                 "updated: 2026-10-06T00:00:00+00:00\nsummary: 本地代理端口与规则\nheat: 5\n"
                 "-----META-END-----\n\n## 环境事实\n- 127.0.0.1:7890 为系统代理\n")
    _write_block(pid, "杂项-下载.md", "-----META-START-----\ncreated: 2026-10-01T00:00:00+00:00\n"
                 "updated: 2026-10-06T00:00:00+00:00\nsummary: 下载目录习惯\nheat: 1\n"
                 "-----META-END-----\n\n## 环境事实\n- 下载默认落在 D:\\download\n")
    calls = {"chat": 0}

    real_client = scene_executor.LLMClient

    class _Counting(real_client):
        def __init__(self, cfg, *a, **k):
            super().__init__(cfg, *a, **k)
            self._log = calls

        def chat(self, messages, tools=None, tool_choice="auto"):
            self._log["chat"] += 1
            return super().chat(messages, tools, tool_choice)

    scene_executor.LLMClient = _Counting
    # 容量压到 2:3 块 → 红色预警,模型必须 MERGE(脚本内补丁,不落盘)
    import config
    original = config.get_config

    def patched_get_config(k, d=None):
        if k == "knowledge.scene.max_blocks":
            return 2
        return original(k, d)

    config.get_config = patched_get_config
    try:
        stats: dict = {}
        scene_executor.maintain_scenes(pid, brain_cfg, "新证据: 无(仅容量整理)", stats=stats,
                                       on_debug=lambda k, p: calls.setdefault(
                                           "dbg", []).append(str(p)[:200]))
        out["stats"] = {k: (str(v)[:220] if isinstance(v, str) else v)
                        for k, v in stats.items() if k != "error"} | (
            {"error": stats["error"]} if "error" in stats else {})
    finally:
        config.get_config = original
        scene_executor.LLMClient = real_client

    entries = scene_executor.read_scene_index(pid)
    out["chat_rounds"] = calls["chat"]
    out["blocks_after"] = [e["filename"] for e in entries]
    out["block_count"] = len(entries)
    out["pass"] = (not out["stats"].get("error")) and len(entries) <= 2
    for line in _dump_blocks(pid):
        log("    " + line)
    return out


# ---------------------------------------------------------------- S3 意图门真实判定
DSML_B = (
    "D盘总使用约299GB，可用约46GB。我需要分析各个目录的占用大小。让我用PowerShell逐目录计算大小。\n"
    "先看一级目录各自的占用情况：\n\n"
    "<｜DSML｜tool_calls>\n"
    "<｜DSML｜invoke name=\"write_file\">\n"
    "<｜DSML｜parameter name=\"path\" string=\"true\">scan_d.ps1</｜DSML｜parameter>\n"
    "<｜DSML｜parameter name=\"content\" string=\"true\">$ErrorActionPreference = 'SilentlyContinue'\n"
    "$root = 'D:\\'\n"
    "$dirs = Get-ChildItem -Path $root -Directory -Force -ErrorAction SilentlyContinue\n"
    "$result = foreach ($d in $dirs) {\n"
    "  $size = (Get-ChildItem -Path $d.FullName -Recurse -File -Force -ErrorAction SilentlyContinue |\n"
    "           Measure-Object -Property Length -Sum).Sum\n"
    "  [PSCustomObject]@{ Dir = $d.Name; SizeGB = [math]::Round($size/1GB, 2) }\n"
    "}\n"
    "$result | Sort-Object SizeGB -Descending | Format-Table -AutoSize\n"
    "</｜DSML｜parameter>\n"
    "</｜DSML｜invoke>\n"
    "</｜DSML｜tool_calls>\n"
)

REPORT_A = (
    "D 盘分析完成。基于对 D:\\ 的实际递归统计,结论如下。\n\n"
    "## D 盘文件占用分析报告\n\n"
    "**总体情况**:D 盘总容量 345.21 GB,已用 299.2 GB,剩余 46.01 GB。\n\n"
    "### 一级目录占用排行\n"
    "| 目录 | 大小 |\n|---|---|\n"
    "| D:\\Program Files (x86) | 203.75 GB |\n"
    "| D:\\AI | 40.09 GB |\n"
    "| D:\\download | 21.4 GB |\n\n"
    "### 最大的单文件 Top3\n"
    "1. pagefile.sys(系统托管,不建议删)\n"
    "2. D:\\AI\\models\\x-70b.q4.gguf(41.9 GB)\n"
    "3. D:\\download\\setup-cache.iso(6.1 GB)\n\n"
    "结论:空间主要被 Program Files (x86) 与 AI 模型文件占据;"
    "如需释放空间,优先清理 setup-cache.iso 与模型缓存。"
)


def s3_intent_judge(brain_cfg: dict) -> dict:
    from omni_core.local.loop import ToolLoop
    from omni_core.local import llm_judge

    loop = ToolLoop(brain_cfg, verbose=False)
    judge = loop._plain_finish_judge(None)
    out: dict = {}
    raw = {}

    def spy(cfg, system, user, timeout=10.0):
        # 注意:用闭包里的真函数,不能走 llm_judge.chat_json(此时已是 spy 自己→递归)
        r = real_chat_json(cfg, system, user, timeout=timeout)
        raw[user[:40]] = r
        return r

    for name, text, expect_hit in (
        ("B-历史DSML退化样本(应判调用意图)", DSML_B, True),
        ("A-真实收尾报告(应放行)", REPORT_A, False),
        ("A2-短句收尾(应放行)", "已完成,扫描了 3 个目录,最大的文件是 D:/AI/x.zip(4.2GB)。", False),
    ):
        t0 = time.time()
        real_chat_json = llm_judge.chat_json
        llm_judge.chat_json = spy
        try:
            verdict = judge(text)
        finally:
            llm_judge.chat_json = real_chat_json
        hit = verdict is not None
        model_reply = raw.get(text[:40])
        out[name] = {"hit": hit, "expect_hit": expect_hit, "elapsed_s": round(time.time() - t0, 1),
                     "model_reply": json.dumps(model_reply, ensure_ascii=False)[:200] if model_reply is not None else None,
                     "pass": hit == expect_hit}
        log(f"    {name}: hit={hit} reply={out[name]['model_reply']}")
    out["pass"] = all(v["pass"] for v in out.values())
    return out


# ---------------------------------------------------------------- S4 端到端
def s4_end_to_end(brain_cfg: dict) -> dict:
    import json as _json
    from omni_core.local import runtime_paths as rp
    from omni_core.local.loop import ToolLoop
    from omni_core.local.task_store import TaskStore

    out: dict = {}
    tid = TaskStore.create(objective="live-e2e", project_id="zz-live-e2e")["task_id"]
    _created_tasks.append(tid)
    _write_block("zz-live-e2e", "windows-环境事实.md", BLOCK_EXISTING)

    loop = ToolLoop(brain_cfg, verbose=False)
    objective = ("用 PowerShell 查询 D 盘的总容量与剩余空间,把数字汇报给我。"
                 "只读操作:不要写文件、不要改系统。")
    from omni_core.local.loop.parts import TaskSpec
    spec = TaskSpec(objective=objective, done_when="", max_steps=8, task_id=tid,
                    project_id="zz-live-e2e")
    t0 = time.time()
    result = loop.run_task(spec)
    out["elapsed_s"] = round(time.time() - t0, 1)
    out["result"] = {k: result.get(k) for k in ("success", "reason", "steps", "escalated")}

    task_dir = rp.task_trajectory(tid).parent  # 真实轨迹 = tasks/<tid>/<日期>_<run>.jsonl
    placements = []
    intent = []
    import glob as _glob
    for f in _glob.glob(str(task_dir / "*.jsonl")):
        for line in open(f, encoding="utf-8", errors="replace"):
            try:
                rec = json.loads(line)
            except Exception:
                continue
            inner = rec.get("content") or ""
            try:
                payload = json.loads(inner) if isinstance(inner, str) and inner.startswith("{") else None
            except Exception:
                payload = None
            if isinstance(payload, dict):
                if payload.get("kind") == "memory_injected":
                    placements.append(payload.get("placement"))
                if payload.get("kind") == "finish_intent_check":
                    intent.append(payload.get("verdict"))
    out["memory_injected_placements"] = placements
    out["finish_intent_check"] = intent
    out["lead_in_note"] = ("lead_in 仅在 L1 池子≥20 条时出现,scratch 项目为空属正确零注入")
    out["pass"] = bool(result.get("success")) and "system" in placements
    return out


# ---------------------------------------------------------------- main
def main():
    os.makedirs(os.path.dirname(RESULT), exist_ok=True)
    out: dict = {"started": time.strftime("%Y-%m-%d %H:%M:%S")}

    log("S0: 解析 main 槽位端点(models.json)...")
    brain_cfg = _resolve_brain_cfg()
    out["brain"] = _mask(brain_cfg)
    log(f"  model={brain_cfg.get('model')} base_url={brain_cfg.get('base_url')}")
    if not (brain_cfg.get("api_key") or os.environ.get(brain_cfg.get("api_key_env", "OMNI_BRAIN_API_KEY"))):
        raise RuntimeError("端点缺 api_key(目录与环境变量都没有),无法真机验证")

    _cleanup_projects()  # 清掉上次运行残留(含 900s 水位),保证每次全新状态

    try:
        log("S1: 场景维护——真实 agent 循环(scene_read/scene_write 多轮)...")
        out["s1_maintenance"] = s1_scene_maintenance(brain_cfg)
        log(f"  pass={out['s1_maintenance']['pass']} blocks={len(out['s1_maintenance']['blocks'])} "
            f"elapsed={out['s1_maintenance']['elapsed_s']}s")

        log("S2: 容量红色预警——max_blocks 压到 2,期望真实 MERGE+软删...")
        out["s2_capacity"] = s2_capacity_merge(brain_cfg)
        log(f"  pass={out['s2_capacity']['pass']} blocks_after={out['s2_capacity']['block_count']} "
            f"rounds={out['s2_capacity']['chat_rounds']}")

        log("S3: 意图门——历史真实文本(DSML 样本/正常报告)真实判定...")
        out["s3_intent"] = s3_intent_judge(brain_cfg)
        log(f"  pass={out['s3_intent']['pass']}")

        if "--skip-e2e" not in sys.argv:
            log("S4: 端到端 run_task(真实 PowerShell 只读任务 + 注入链)...")
            out["s4_e2e"] = s4_end_to_end(brain_cfg)
            log(f"  pass={out['s4_e2e']['pass']} placements={out['s4_e2e']['memory_injected_placements']} "
                f"intent={out['s4_e2e']['finish_intent_check']} elapsed={out['s4_e2e']['elapsed_s']}s")

        out["overall_pass"] = all(
            v.get("pass") for k, v in out.items() if k.startswith("s") and isinstance(v, dict))
        log(f"OVERALL: {'PASS' if out['overall_pass'] else 'FAIL'}")
    finally:
        if "--keep" not in sys.argv:
            _cleanup_projects()
        else:
            log("(--keep) 保留 zz-live-* 现场")
        out["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(RESULT, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        log("DONE -> " + RESULT)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        log("ERROR:\n" + traceback.format_exc())
        with open(RESULT, "w", encoding="utf-8") as f:
            json.dump({"error": str(e), "traceback": traceback.format_exc()}, f,
                      ensure_ascii=False, indent=2)
        sys.exit(1)
