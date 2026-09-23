"""M2 真机验证：截图 MuMu 模拟器界面，用本地 4B-vl 判断读到了什么。

用法（沙箱外）：
  .venv/Scripts/python.exe scripts/verify_vision_live.py

流程：
  1. model_hub 启动/复用 4B-vl llama-server（idempotent）
  2. EmulatorBackend 连 MuMu(127.0.0.1:7555)，screenshot
  3. VisionRuntime.vision_describe -> 本地 VLM 文本
  4. 结果 + 截图路径写入 temp/verify_vision_result.json
"""
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
RESULT = os.path.join(ROOT, "temp", "verify_vision_result.json")
PROGRESS = os.path.join(ROOT, "temp", "verify_vision_progress.log")


def log(msg: str):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(PROGRESS, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def main():
    os.makedirs(os.path.dirname(RESULT), exist_ok=True)
    open(PROGRESS, "w", encoding="utf-8").close()

    import yaml
    with open(os.path.join(ROOT, "config.yaml"), "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    vision_cfg = dict(cfg.get("runtime", {}).get("vision", {}))

    # 1) 优先探测已运行的 4B-vl（避免重复起一个撑爆 8GB 显存）；没有再启动
    log("step1: 探测/启动 4B-vl llama-server ...")
    from omni_core.tools.vision_runtime import VisionRuntime

    existing = VisionRuntime._probe_local_vlm()
    if existing:
        base_url = existing
        log(f"  探测到已有 VLM: {base_url}（复用，不重复启动）")
    else:
        from model_hub.manager import ModelManager

        mgr = ModelManager()
        gguf = "D:/AI/Models/qwen3_5_4B/Qwen_Qwen3.5-4B-Q4_K_M.gguf"
        start = mgr.start_model(
            name="qwen3.5-4B-vl",
            gguf_path=gguf,
            wait_ready=True,
            timeout=180,
            profile="default",
        )
        log(f"  model start -> {json.dumps(start, ensure_ascii=False)}")
        if start.get("status") not in ("ready", "already_running"):
            raise RuntimeError(f"4B-vl 启动失败: {start.get('status')}")
        base_url = f"http://127.0.0.1:{start['port']}"
    # 把解析到的地址显式注入，保证 VisionRuntime 命中正确实例
    vision_cfg["base_url"] = base_url

    # 2) 连模拟器 + 截图
    log("step2: 连接 MuMu 模拟器并截图 ...")
    from devices import ExecutionModule

    exec_mod = ExecutionModule()  # config runtime.backend=emulator
    shot = exec_mod.screenshot()
    log(f"  screenshot -> {json.dumps(shot, ensure_ascii=False)}")
    if not shot.get("ok"):
        raise RuntimeError(f"截图失败: {shot}")

    # 3) 本地 VLM 判断
    log("step3: 本地 4B-vl 读屏判断 ...")
    vr = VisionRuntime(exec_mod, cfg=vision_cfg)
    prompt = (
        "这是当前设备界面的截图。请描述你实际看到的画面：\n"
        "1) 当前处于哪个界面（如：主界面/列表/详情/加载中等）？\n"
        "2) 画面里有哪些关键文字、按钮、图标或 UI 元素？\n"
        "3) 整体画面氛围（明亮/暗黑、是否有特效等）。\n"
        "如果看到加载中、登录或弹窗也请说明。用中文简洁分点回答。"
    )
    desc = vr.vision_describe(prompt, max_tokens=512)
    log(f"  vision_describe -> ok={desc.get('ok')}, len={len(desc.get('description',''))}")

    # 4) SoM 标注（展示结构化 mark 数量，验证 M2 视觉工具链）
    log("step4: SoM 结构化标注 ...")
    som = vr.som_ground()
    log(f"  som_ground -> ok={som.get('ok')}, marks={som.get('element_count')}")

    result = {
        "screenshot": shot.get("path"),
        "vision_ok": desc.get("ok"),
        "description": desc.get("description", ""),
        "vision_error": desc.get("error"),
        "som_ok": som.get("ok"),
        "som_marked_image": som.get("marked_image"),
        "som_element_count": som.get("element_count"),
        "som_marks": som.get("marks", [])[:30],
    }
    with open(RESULT, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
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
