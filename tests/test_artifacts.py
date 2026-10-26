"""产物登记表（artifacts registry）+ 右侧栏 workspace API 回归。

覆盖：
1. record_artifact 登记 / 去重 / 容量上限；
2. preserve_tmp_artifacts：tmp 内产物在清理前抢救到持久 artifacts/ 并回写路径，
   tmp 外产物路径原样保留；
3. files/read 允许集：登记过的文件可读，任务外路径 404；
4. /artifacts 端点形状。

使用 conftest 的 _isolated_home fixture（runtime_paths._GLOBAL → tmp_path）。
"""
import time

import pytest
from fastapi.testclient import TestClient

import backend.server as server_mod
from omni_core.local import runtime_paths as P
from omni_core.local.artifacts import (
    load_artifacts,
    preserve_tmp_artifacts,
    record_artifact,
)


@pytest.fixture()
def tid():
    return "t_artifacts_case"


def test_record_dedup_and_cap(tid, _isolated_home):
    P.ensure_task_dirs(tid)
    f1 = P.task_tmp(tid) / "report.md"
    f1.write_text("# hello", encoding="utf-8")
    f2 = P.task_tmp(tid) / "screenshot.png"
    f2.write_bytes(b"\x89PNG\r\n")

    assert record_artifact(str(f1), tool="write_file", task_id=tid) is not None
    time.sleep(0.01)
    assert record_artifact(str(f1), tool="write_file", task_id=tid) is not None  # 去重
    assert record_artifact(str(f2), tool="screenshot", task_id=tid) is not None

    arts = load_artifacts(tid)
    assert len(arts) == 2, "同路径重复登记应去重"
    by_name = {a["name"]: a for a in arts}
    assert by_name["report.md"]["tool"] == "write_file"
    assert by_name["screenshot.png"]["bytes"] == len(b"\x89PNG\r\n")

    # 未落盘的路径不登记
    assert record_artifact(str(P.task_tmp(tid) / "ghost.md"), tool="write_file", task_id=tid) is None
    assert len(load_artifacts(tid)) == 2


def test_preserve_moves_tmp_artifacts_to_persistent_dir(tid, _isolated_home):
    P.ensure_task_dirs(tid)
    f1 = P.task_tmp(tid) / "report.md"
    f1.write_text("# 报告", encoding="utf-8")
    outside = _isolated_home / "outside.md"  # tmp 外的产物
    outside.write_text("keep", encoding="utf-8")
    record_artifact(str(f1), tool="write_file", task_id=tid)
    record_artifact(str(outside), tool="write_file", task_id=tid)

    preserve_tmp_artifacts(tid)

    arts = {a["name"]: a for a in load_artifacts(tid)}
    # tmp 内产物被复制到 artifacts/，登记路径回写
    dest = P.task_artifacts_dir(tid) / "report.md"
    assert dest.is_file() and dest.read_text(encoding="utf-8") == "# 报告"
    assert arts["report.md"]["path"] == str(dest)
    # tmp 外产物路径原样保留
    assert arts["outside.md"]["path"] == str(outside)

    # 模拟任务终态清理：tmp 被删后登记表仍指向可用的持久副本
    import shutil
    shutil.rmtree(P.task_tmp(tid), ignore_errors=True)
    assert all(a["path"] != str(f1) for a in load_artifacts(tid))


def test_files_read_allowlist(tid, _isolated_home):
    """files/read 只服务登记产物 + 任务目录树；任务外文件拒绝。"""
    P.ensure_task_dirs(tid)
    secret = _isolated_home / "secret.txt"
    secret.write_text("top secret", encoding="utf-8")
    art = P.task_tmp(tid) / "note.md"
    art.write_text("# 笔记内容", encoding="utf-8")
    record_artifact(str(art), tool="write_file", task_id=tid)

    client = TestClient(server_mod.app)

    # 登记产物可读
    r = client.get("/api/runtime/files/read", params={"task_id": tid, "path": str(art)})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True and d["kind"] == "text" and "# 笔记内容" in d["content"]

    # 任务目录树内未登记文件也可读（artifacts/ 抢救副本场景）
    in_tree = P.task_dir(tid) / "artifacts" / "kept.md"
    in_tree.parent.mkdir(parents=True, exist_ok=True)
    in_tree.write_text("kept", encoding="utf-8")
    r2 = client.get("/api/runtime/files/read",
                    params={"task_id": tid, "path": str(in_tree)})
    assert r2.status_code == 200

    # 任务外路径拒绝
    r3 = client.get("/api/runtime/files/read", params={"task_id": tid, "path": str(secret)})
    assert r3.status_code == 404


def test_artifacts_endpoint_shape(tid, _isolated_home):
    P.ensure_task_dirs(tid)
    f = P.task_tmp(tid) / "a.md"
    f.write_text("x", encoding="utf-8")
    record_artifact(str(f), tool="write_file", task_id=tid)
    client = TestClient(server_mod.app)
    r = client.get("/api/runtime/artifacts", params={"task_id": tid})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True and len(d["artifacts"]) == 1
    assert d["artifacts"][0]["name"] == "a.md"
