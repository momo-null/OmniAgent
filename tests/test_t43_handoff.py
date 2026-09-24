"""T4.3（U5a/U5b）— 子父任务交接面增强。

覆盖：
1. 子任务关键产物 / 完成条件校验结果 / 失败点位正常透传展示。
2. 存在历史子任务结果时自动追加世界状态摘要（截断 600 字符）。
3. 无历史子任务结果时零追加，默认行为无回归。
"""
import types

import pytest

from omni_core.local.loop import (
    _ARTIFACT_ITEM_CHARS, _WORLD_SUMMARY_CHARS, _failure_point, _format_prev_results,
    _subtask_artifacts,
)


def _world(notes=None, facts=None):
    return types.SimpleNamespace(notes=list(notes or []), facts=list(facts or []))


# --- U5a：字段抽取 -----------------------------------------------------------
def test_subtask_artifacts_collects_notes_then_facts():
    w = _world(notes=["备注1", "备注2", "备注3", "备注4"], facts=["事实1", "事实2"])
    arts = _subtask_artifacts(w)
    assert "备注4" in arts and "备注3" in arts      # 备注优先
    assert any("事实" in a for a in arts)
    assert len(arts) <= 6


def test_subtask_artifacts_dedupes_and_tolerates_empty():
    w = _world(notes=["同一条", "同一条"], facts=[])
    assert _subtask_artifacts(w) == ["同一条"]
    assert _subtask_artifacts(None) == []


def test_failure_point_is_first_line_of_reason():
    res = {"success": False, "reason": "首行失败原因\n第二行细节"}
    assert _failure_point(res) == "首行失败原因"
    # 成功无失败点位
    assert _failure_point({"success": True, "reason": "x"}) == ""
    assert _failure_point({"success": False, "reason": ""}) == ""


def test_failure_point_truncates_long_line():
    res = {"success": False, "reason": "长" * 500}
    assert len(_failure_point(res)) == _ARTIFACT_ITEM_CHARS


# --- U5b：主任务展示 ---------------------------------------------------------
def test_prev_results_show_artifacts_and_verification():
    prev = [{
        "desc": "抓取数据", "success": True, "reason": "完成",
        "done_when_hit": True,
        "artifacts": ["产物一", "产物二", "产物三", "产物四"],   # 最多展示 3 条
    }]
    text = _format_prev_results(prev, world_summary="")
    assert "抓取数据: 成功" in text
    assert "完成条件校验: 通过" in text
    assert "产物一" in text and "产物二" in text and "产物三" in text
    assert "产物四" not in text, "单子任务最多展示 3 条产物"


def test_prev_results_truncate_single_artifact():
    long_artifact = "A" * 500
    text = _format_prev_results(
        [{"desc": "d", "success": True, "reason": "", "artifacts": [long_artifact]}], "")
    assert "A" * _ARTIFACT_ITEM_CHARS in text
    assert "A" * (_ARTIFACT_ITEM_CHARS + 1) not in text
    assert text.rstrip().endswith("...")


def test_prev_results_show_failure_point():
    text = _format_prev_results(
        [{"desc": "d", "success": False, "reason": "超时\n细节",
          "failure_point": "超时"}], "")
    assert "失败点位: 超时" in text


def test_world_summary_appended_and_truncated():
    summary = "S" * 1000
    text = _format_prev_results([{"desc": "d", "success": True, "reason": ""}],
                                world_summary=summary)
    assert "【当前世界状态摘要】" in text
    assert "S" * _WORLD_SUMMARY_CHARS in text
    assert "S" * (_WORLD_SUMMARY_CHARS + 1) not in text


def test_no_prev_results_means_no_append():
    assert _format_prev_results([], world_summary="即便有摘要") == ""
    assert _format_prev_results(None, world_summary="x") == ""


# --- 全链路：_run_subtask 补齐三键 -------------------------------------------
def test_run_subtask_adds_handoff_fields(monkeypatch):
    """子任务返回字典应补齐 done_when_hit / artifacts / failure_point 三键。"""
    from omni_core.local.loop import TaskSpec, ToolLoop

    loop = ToolLoop({"model": "m", "base_url": "http://127.0.0.1:9", "api_key": "k"}, verbose=False)
    monkeypatch.setattr(loop, "_run_via_sdk", lambda *a, **k: {
        "success": False, "reason": "首行原因\n第二行细节",
        "steps": 1, "escalated": False,
    })
    spec = TaskSpec(objective="子任务目标", done_when="DONE", expected="DONE",
                    task_id="t43-task")
    res = loop._run_subtask(spec)

    assert res["failure_point"] == "首行原因", res
    assert isinstance(res["artifacts"], list)
    assert "done_when_hit" in res
    # 产物/失败点位可被主任务展示层消费
    text = _format_prev_results([{**res, "desc": spec.objective}], "世界摘要")
    assert "失败点位: 首行原因" in text
    assert "世界摘要" in text
