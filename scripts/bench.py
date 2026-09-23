#!/usr/bin/env python
"""本地执行模型吞吐基准（文本 / 视觉）。

用法：
  python scripts/bench.py                         # 默认测 4B-vl 文本
  python scripts/bench.py --image temp/m0_test_scene.png
  python scripts/bench.py --gguf D:/AI/Models/qwen3_5_9B/...gguf --runs 6

要点：
  - thinking 默认关闭（执行层要直接答案，避免 reasoning_content 挤空 content）
  - 首次请求含 CUDA graph 捕获开销，作为 warmup 不计入统计
  - 视觉吞吐 = 生成 tok/s；图像预填充为一次性开销（真实截图更长）
"""
import argparse
import base64
import json
import os
import sys
import time
import urllib.request

# 让脚本可从仓库任意位置运行
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from model_hub.manager import ModelManager  # noqa: E402

DEFAULT_GGUF = r"D:\AI\Models\qwen3_5_4B\Qwen_Qwen3.5-4B-Q4_K_M.gguf"
RUN_NAME = "bench"


def chat(port: int, messages: list, max_tokens: int) -> tuple:
    """发一次请求，返回 (耗时秒, completion_tokens)。"""
    body = json.dumps({
        "model": "local",
        "messages": messages,
        "temperature": 0.1,
        "max_tokens": max_tokens,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/chat/completions",
        data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=180) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    dt = time.time() - t0
    comp = data.get("usage", {}).get("completion_tokens", 0)
    return dt, comp


def bench_channel(port: int, messages: list, max_tokens: int, runs: int, label: str):
    print(f"\n=== {label} ===")
    samples = []
    for i in range(runs + 1):
        dt, comp = chat(port, messages, max_tokens)
        if i == 0:
            print(f"  [warmup] {dt:.2f}s gen={comp}tok (skip)")
            continue
        tps = comp / dt
        samples.append(tps)
        print(f"  run{i}: {dt:.2f}s | gen={comp}tok | {tps:.1f} tok/s")
    if samples:
        avg = sum(samples) / len(samples)
        print(f"  => {label} avg {avg:.1f} tok/s  (n={len(samples)})")
    return avg if samples else 0.0


def main():
    ap = argparse.ArgumentParser(description="本地模型吞吐基准")
    ap.add_argument("--gguf", default=DEFAULT_GGUF, help="GGUF 主模型路径")
    ap.add_argument("--image", default=None, help="视觉基准用的图片（不传则只测文本）")
    ap.add_argument("--runs", type=int, default=5, help="统计次数（不含 warmup）")
    ap.add_argument("--max-tokens", type=int, default=256, help="每次生成上限")
    ap.add_argument("--prompt", default="请详细介绍一下量子计算的基本原理，并说明它与经典计算的主要区别，写满约两百字。",
                    help="文本基准的 prompt")
    args = ap.parse_args()

    if not os.path.exists(args.gguf):
        print(f"[ERROR] GGUF 不存在: {args.gguf}")
        sys.exit(1)

    mgr = ModelManager()
    res = mgr.start_model_path(args.gguf, name=RUN_NAME, wait_ready=True)
    if res.get("status") != "ready":
        print(f"[ERROR] 模型启动失败: {res}")
        sys.exit(1)
    port = res["port"]
    print(f"模型就绪: name={RUN_NAME} pid={res['pid']} port={port}")

    # 文本
    bench_channel(port, [{"role": "user", "content": args.prompt}],
                  args.max_tokens, args.runs, "TEXT")

    # 视觉
    if args.image:
        if not os.path.exists(args.image):
            print(f"[WARN] 图片不存在，跳过 VL: {args.image}")
        else:
            b64 = base64.b64encode(open(args.image, "rb").read()).decode("utf-8")
            content = [
                {"type": "text", "text": "请详细描述这张图片里有什么物体、颜色、形状和空间位置，尽量展开写满。"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
            ]
            bench_channel(port, [{"role": "user", "content": content}],
                          args.max_tokens, args.runs, "VL")

    mgr.stop_model(RUN_NAME)
    print("\n[done] 已停止模型")


if __name__ == "__main__":
    main()
