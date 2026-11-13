#!/usr/bin/env python3
"""验证向量臂是否真在起作用（只读，不改任何状态）。

用法::

    python scripts/vector_check.py

判据不是「服务活着」，而是**语义增益**：同一个查询分别关/开向量跑
``search_l0``，看向量能否救回字面搜不到的内容；再用一个负样本确认
``cos_floor`` 不会把无关内容凭空塞满。

链路：``embedder.ready()`` → ``service_url`` / ``encode`` 维度；
增益：正样本应有新增召回，负样本新增应为 0。
"""
from __future__ import annotations

import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
os.chdir(REPO)

from omni_core import memory_brain as mb  # noqa: E402
from omni_core.local import embedder as e  # noqa: E402

#: (查询, 说明)。前三条字面不命中但语义相关，最后一条是负样本。
QUERIES = [
    ("逼我学新歌", "语义相关、字面不命中"),
    ("拿吃的东西威胁我", "对应「上餐桌」，字面不命中"),
    ("有人表白了", "对应「我喜欢你」，字面不命中"),
    ("量子场论重整化群方程", "负样本：库里不该有"),
]


def main() -> int:
    print("=== 1. 服务链 ===")
    try:
        v = e.encode(["链路自检"])
        dim = len(v[0]) if v else 0
        print(f"  ready = {e.ready()} | url = {e.service_url()} | dim = {dim}")
        if not e.ready() or not dim:
            print("  => 服务未就绪，向量臂不会生效（检索会降级 FTS-only）")
            return 1
    except Exception as ex:  # noqa: BLE001
        print(f"  FAIL: {type(ex).__name__}: {ex}")
        return 1

    orig = mb._vec_enabled
    results = []
    try:
        for q, note in QUERIES:
            mb._vec_enabled = lambda: False
            fts = mb.search_l0(q, limit=5, days=365) or []
            mb._vec_enabled = orig
            mix = mb.search_l0(q, limit=5, days=365) or []
            fids = {r["id"] for r in fts}
            results.append((q, note, fts, mix,
                            [r for r in mix if r["id"] not in fids]))
    finally:
        mb._vec_enabled = orig

    print("\n=== 2. 语义增益（同一查询，关/开向量） ===")
    for q, note, fts, mix, added in results:
        print(f"\n[{q}]  {note}")
        print(f"  FTS-only {len(fts)} 条 | FTS+向量 {len(mix)} 条 | 向量新增 {len(added)} 条")
        for r in added[:3]:
            print(f"    + {str(r['text'])[:56]}")

    gain = sum(1 for _q, _n, _f, _m, a in results[:-1] if a)
    neg = len(results[-1][4])
    print("\n=== 3. 判定 ===")
    print(f"  正样本有增益的查询: {gain}/{len(results) - 1}")
    print(f"  负样本向量新增: {neg} 条（应为 0，验证 cos_floor 不过度召回）")
    ok = gain >= 1 and neg == 0
    print("  => 向量臂生效" if ok else "  => 需要检查：服务/配置/索引")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
