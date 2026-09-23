#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""从 config.yaml 的 llm_runtime.models 迁移到 .meta.json 侧注文件

model-meta-refactor (2026-07-11) 配套脚本：把旧的「注册表」模型参数
逐个写入各自 GGUF 文件同目录的 ``<stem>.meta.json``。

特性：
- 非破坏性：仅在 GGUF 同目录新增/更新 .meta.json，不动 GGUF 与 config。
- mmproj_path 转换为相对路径（相对于 .meta.json 所在目录）。
- 默认只迁移，不修改 config.yaml；加 --remove 才删除 models 段（并打印备份提示）。

用法：
    python scripts/migrate_models_from_config.py
    python scripts/migrate_models_from_config.py --config path/to/config.yaml
    python scripts/migrate_models_from_config.py --remove   # 迁移后删除 config 的 models 段
"""
import argparse
import json
import os
import sys

import yaml

# 允许以模块方式运行（scripts/ 在仓库根下）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from model_hub.meta import ModelMetaData  # noqa: E402


def find_config(explicit: str = None) -> str:
    if explicit:
        return explicit
    here = os.path.dirname(os.path.abspath(__file__))
    cand = os.path.join(os.path.dirname(here), "config.yaml")
    if not os.path.exists(cand):
        raise FileNotFoundError(f"未找到 config.yaml: {cand}")
    return cand


def migrate(config_path: str) -> dict:
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}

    models = (config.get("llm_runtime", {}) or {}).get("models", {}) or {}
    if not models:
        print("[warn] config.yaml 中没有 llm_runtime.models，无需迁移。")
        return {"success": 0, "failed": 0, "skipped": 0}

    success, failed, skipped = 0, 0, 0
    for name, cfg in models.items():
        gguf_path = cfg.get("gguf_path")
        if not gguf_path:
            print(f"[skip] {name}: 缺少 gguf_path")
            skipped += 1
            continue
        if not os.path.exists(gguf_path):
            print(f"[fail] {name}: GGUF 不存在 {gguf_path}")
            failed += 1
            continue

        # mmproj 绝对路径 -> 相对路径
        mmproj = cfg.get("mmproj_path")
        if mmproj:
            try:
                mmproj = os.path.relpath(mmproj, os.path.dirname(gguf_path))
            except ValueError:
                pass  # 跨盘等异常，保留原值

        updates = {
            "name": name,
            "description": cfg.get("description"),
            "tags": cfg.get("tags"),
            "mmproj_path": mmproj,
            "ctx_size": cfg.get("ctx_size"),
            "gpu_layers": cfg.get("gpu_layers"),
            "threads": cfg.get("threads"),
            "port": cfg.get("port"),
            "reasoning_budget": cfg.get("reasoning_budget"),
        }
        meta_path = ModelMetaData.save_to(gguf_path, updates)
        print(f"[ok]   {name} -> {meta_path}")
        success += 1

    print(f"\n迁移完成：成功 {success}，失败 {failed}，跳过 {skipped}")
    return {"success": success, "failed": failed, "skipped": skipped}


def remove_models_block(config_path: str) -> None:
    """删除 config.yaml 的 llm_runtime.models 段（就地修改，先备份）"""
    backup = config_path + ".bak"
    with open(config_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    with open(backup, "w", encoding="utf-8") as f:
        f.writelines(lines)
    print(f"[info] 已备份 config.yaml -> {backup}")

    text = "".join(lines)
    # 简单可靠的删除：定位 'models:' 在 llm_runtime 下、缩进 2 空格，删到下一个 2 空格顶层键
    import re

    pattern = re.compile(
        r"(\n[ \t]*#.*\n)*[ \t]*models:\n(?:[ \t]+.*\n)*?([ \t]*api:\n)",
        re.MULTILINE,
    )
    new_text, n = pattern.subn(r"\2", text)
    if n == 0:
        print("[warn] 未能自动定位 models 段，未修改 config.yaml（请手动删除）。")
        return
    with open(config_path, "w", encoding="utf-8") as f:
        f.write(new_text)
    print("[ok]   已从 config.yaml 删除 llm_runtime.models 段。")


def main():
    parser = argparse.ArgumentParser(description="迁移 config 注册表到 .meta.json")
    parser.add_argument("--config", default=None, help="config.yaml 路径")
    parser.add_argument(
        "--remove",
        action="store_true",
        help="迁移完成后从 config.yaml 删除 models 段（会自动备份）",
    )
    args = parser.parse_args()

    config_path = find_config(args.config)
    print(f"使用 config: {config_path}")
    result = migrate(config_path)
    if args.remove and result["success"] > 0:
        remove_models_block(config_path)


if __name__ == "__main__":
    main()
