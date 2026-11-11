"""主人身份归一 + 认领 + 主人卡背景保留 + L3 自我画像写点（2026-11-11 三分法）。

覆盖四块：
- ``local/identity.py``：MASTER_KEY 合成 key、多标识名单归一、Web 恒主人；
- 认领链（memory_brain）：restamp 回填 / 旧卡收编 / 宽窗重建 / list_speakers 候选；
- ``memory_brain.maintain_relations`` 主人分支：按合成 key 判定、稳定背景区
  LLM 丢失时代码补写回（背景保留是硬要求，不只靠 prompt）；
- ``memory_brain.maintain_profile``：写 self_profile.md（她的自我画像）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from omni_core.local import identity


# ---------------------------------------------------------------- identity 归一

def test_master_key_is_synthetic():
    """合成 key 恒定：不依赖配置、不含真实标识。"""
    assert identity.MASTER_KEY == "master"
    assert identity.master_speaker() == "主人(master)"
    assert identity.is_master_key("master") and not identity.is_master_key("10001")


def test_unconfigured_web_still_master(monkeypatch):
    """名单为空：Web 恒主人（master_speaker 不依赖配置），通道零归一。"""
    monkeypatch.setattr(identity, "master_uids", lambda: [])
    assert identity.master_speaker() == "主人(master)"
    assert identity.normalize_master("某人(10001)") == "某人(10001)"
    assert identity.normalize_master("", "10001") == ""


def test_multi_uid_all_map_to_one_key(monkeypatch):
    """多标识名单：任一命中（裸 uid 或成形 speaker）都归一到同一合成 key。"""
    monkeypatch.setattr(identity, "master_uids", lambda: ["88888", "77777"])
    assert identity.normalize_master("", "88888") == "主人(master)"
    assert identity.normalize_master("蓝色大肥鱼的主人(77777)", "") == "主人(master)"
    assert identity.normalize_master("doro(30001)", "") == "doro(30001)"
    assert identity.normalize_master("", "30001") == ""


def test_master_form_passthrough(monkeypatch):
    """已是规范形态原样通过（幂等）。"""
    monkeypatch.setattr(identity, "master_uids", lambda: ["88888"])
    assert identity.normalize_master("主人(master)") == "主人(master)"


def test_resolve_peer_uids_claim_normalization(monkeypatch):
    """读侧归一：已认领的旧身份（裸 uid / 成形 speaker）都对应到主人 key。"""
    monkeypatch.setattr(identity, "master_uids", lambda: ["88888", "77777"])
    assert identity.resolve_peer_uids("88888") == ["master"]
    assert identity.resolve_peer_uids("蓝色大肥鱼的主人(77777)") == ["master"]
    assert identity.resolve_peer_uids("master") == ["master"]
    assert identity.resolve_peer_uids("主人(master)") == ["master"]
    assert identity.resolve_peer_uids("doro(30001)") == ["30001"]   # 未认领原样
    assert identity.resolve_peer_uids("") == []


def test_resolve_peer_uids_unconfigured(monkeypatch):
    """名单为空：除 master key 本体外一律原样（零认领部署语义不变）。"""
    monkeypatch.setattr(identity, "master_uids", lambda: [])
    assert identity.resolve_peer_uids("10001") == ["10001"]
    assert identity.resolve_peer_uids("doro(10001)") == ["10001"]
    assert identity.resolve_peer_uids("master") == ["master"]


def test_speaker_uid_parse():
    assert identity.speaker_uid("名字(12345)") == "12345"
    assert identity.speaker_uid("不含括号") == ""
    assert identity.speaker_uid("尾括号缺失(12345") == ""


def test_master_uids_list_and_string(monkeypatch):
    """配置兼容列表与字符串；脏值（空白/非字符串）丢弃。"""
    import config as _cfg
    real = _cfg.get_config
    monkeypatch.setattr(_cfg, "get_config",
                        lambda path, default=None:
                        [" 1 ", "", 2] if path == "knowledge.relations.master_uids"
                        else real(path, default))
    assert identity.master_uids() == ["1", "2"]
    monkeypatch.setattr(_cfg, "get_config",
                        lambda path, default=None:
                        "9" if path == "knowledge.relations.master_uids"
                        else real(path, default))
    assert identity.master_uids() == ["9"]


# ---------------------------------------------------------------- 关系卡维护

@pytest.fixture()
def rel_env(tmp_path, monkeypatch):
    """关系卡维护的隔离环境：relations 目录 + 记忆开关。"""
    from omni_core import memory_brain
    from omni_core.local import runtime_paths as rp
    rel_dir = tmp_path / "relations"
    rel_dir.mkdir()
    monkeypatch.setattr(rp, "global_relations", lambda: rel_dir)
    monkeypatch.setattr(memory_brain, "enabled", lambda: True)
    return rel_dir


def _fake_llm(monkeypatch, payload):
    from omni_core.local import llm_judge
    captured = {"calls": []}

    def chat_json(brain_cfg, system, prompt, timeout=60.0):
        captured["calls"].append({"system": system, "prompt": prompt})
        return payload

    monkeypatch.setattr(llm_judge, "chat_json", chat_json)
    return captured


def _seed_peer_lines(monkeypatch, peers):
    from omni_core import memory_brain
    monkeypatch.setattr(memory_brain, "_peer_lines", lambda limit_days=7: peers)


def test_master_card_by_synthetic_key(rel_env, monkeypatch):
    """主人分支按合成 key 判定（与配置名单解耦）：素材带现有背景、LLM 丢背景代码补写回。"""
    from omni_core import memory_brain

    card_path = rel_env / "master.md"
    card_path.write_text(
        "-----META-START-----\nname: 主人\nuid: master\nupdated: x\n-----META-END-----\n"
        "【稳定背景】\n- 测试机 Windows，工作盘 D 盘。\n- OmniAgent 在 D:/AI/OmniAgent。\n"
        "【最近交手】旧内容\n【他对我的态度】旧\n【我对他的态度】旧\n", encoding="utf-8")

    captured = _fake_llm(monkeypatch, {"card": "【最近交手】聊了缓存命中率。\n"
                                           "【他对我的态度】直接\n【我对他的态度】信赖",
                                       "topics": ["缓存", "开发"]})
    _seed_peer_lines(monkeypatch, {"master": {"name": "主人", "lines": ["看下今天的缓存数据",
                                                                           "行，先这样",
                                                                           "明天继续"]}})
    monkeypatch.setattr(memory_brain, "upsert_narrative_index", lambda *a, **k: None)

    assert memory_brain.maintain_relations({"model": "x"}) == 1
    call = captured["calls"][0]
    assert "测试机 Windows" in call["prompt"]
    assert "主人说过的话" in call["prompt"]
    body = card_path.read_text(encoding="utf-8")
    assert "【稳定背景】" in body and "测试机 Windows" in body  # 代码补写回
    assert "聊了缓存命中率" in body


def test_plain_card_uses_plain_template(rel_env, monkeypatch):
    """普通人卡走原模板、素材不带背景指令。"""
    from omni_core import memory_brain

    captured = _fake_llm(monkeypatch, {"card": "最近交手：x\n他对我的态度：x\n我对我的态度：x",
                                       "topics": []})
    _seed_peer_lines(monkeypatch, {
        "master": {"name": "主人", "lines": ["a", "b", "c"]},
        "10001": {"name": "doro", "lines": ["x", "y", "z"]},
    })
    monkeypatch.setattr(memory_brain, "upsert_narrative_index", lambda *a, **k: None)

    assert memory_brain.maintain_relations({"model": "x"}) == 2
    plain = [c for c in captured["calls"] if "稳定背景" not in c["system"]]
    assert plain and all("对方说过的话" in c["prompt"] for c in plain)


def test_maintain_master_card_wide_window(rel_env, monkeypatch):
    """认领后的宽窗重建：只重写主人卡，不碰别人的卡。"""
    from omni_core import memory_brain
    seen = {}

    def fake_peer_lines(limit_days=7):
        seen["limit_days"] = limit_days
        return {"master": {"name": "主人", "lines": ["a", "b", "c"]},
                "10001": {"name": "doro", "lines": ["x", "y", "z"]}}

    monkeypatch.setattr(memory_brain, "_peer_lines", fake_peer_lines)
    _fake_llm(monkeypatch, {"card": "【稳定背景】\n- A\n【最近交手】x\n"
                                    "【他对我的态度】x\n【我对他的态度】x", "topics": []})
    monkeypatch.setattr(memory_brain, "upsert_narrative_index", lambda *a, **k: None)

    assert memory_brain.maintain_master_card({"model": "x"}, limit_days=90) is True
    assert seen["limit_days"] == 90
    assert (rel_env / "master.md").exists()
    assert not (rel_env / "10001.md").exists()  # 没碰别人的卡


# ---------------------------------------------------------------- 认领链

class _Conn:
    """极简 sqlite 替身：fetchall 返回预置行；UPDATE 记录到 updates。"""

    def __init__(self, rows):
        self._rows = rows
        self.updates = []

    def execute(self, sql, params=()):
        class _C:
            rowcount = 1

            def fetchall(self_inner):
                return self._rows
        if sql.startswith("UPDATE"):
            self.updates.append((sql, params))
        return _C()

    def commit(self):
        pass

    def close(self):
        pass


def test_restamp_backfills_history(monkeypatch):
    """回填：该 uid 的历史 user 行重盖章 主人(master)（精确后缀匹配，不误伤同尾号）。"""
    from omni_core import memory_brain

    conn = _Conn([("蓝色大肥鱼的主人(88888)",), ("别人(38888)",), ("主人(master)",)])
    monkeypatch.setattr(memory_brain, "open_brain", lambda: conn)
    n = memory_brain.restamp_speaker_as_master("88888")
    assert n == 1  # 只有 88888 那个 speaker 被改（38888 不误伤、主人行本就规范）
    assert conn.updates and conn.updates[0][1][0] == "主人(master)"
    assert conn.updates[0][1][1] == "蓝色大肥鱼的主人(88888)"


def test_retire_relation_card(rel_env, monkeypatch):
    """注销：删正文 + 撤索引；主人卡拒绝（身份锚不许从这里删）。"""
    from omni_core import memory_brain

    plain = rel_env / "10001.md"
    plain.write_text("x", encoding="utf-8")
    deleted_index = []
    monkeypatch.setattr(memory_brain, "delete_narrative_index",
                        lambda uid, kind: deleted_index.append((uid, kind)) or True)
    assert memory_brain.retire_relation_card("10001") is True
    assert not plain.exists() and deleted_index == [("10001", "relation")]
    assert memory_brain.retire_relation_card("master") is False  # 主人卡拒绝


def test_list_speakers_claim_candidates(monkeypatch):
    """认领候选：q 空 = 活跃门槛(≥3) + 排除已认领；q 非空 = 全量前缀搜索（不限门槛）。"""
    from omni_core import memory_brain

    monkeypatch.setattr(memory_brain, "open_brain", lambda: _Conn([
        ("群友甲(70001001)", 61, "2026-11-11"),
        ("doro(30001)", 2, "2026-11-11"),
        ("路人(40109)", 5, "2026-11-10"),
        ("主人(master)", 9, "2026-11-11"),
    ]))
    items = memory_brain.list_speakers(exclude_uids={"master"})
    uids = [x["uid"] for x in items]
    assert "70001001" in uids and "40109" in uids
    assert "30001" not in uids  # 2 条 < 门槛 3
    assert "master" not in uids  # 已认领排除
    items = memory_brain.list_speakers(q="doro", exclude_uids={"master"})
    assert [x["uid"] for x in items] == ["30001"]


def test_search_narrative_claimed_peer_hits_master_card(rel_env, monkeypatch):
    """认领后按旧身份搜叙事 → 主人卡获得对象加权（uid 级匹配，昵称无关）。"""
    from omni_core import memory_brain

    # 两张索引行：主人卡（peers=规范标签）与普通人卡，日期相同（近因同权）
    rows = [(
        "master", "relation", "", "2026-11-11", "2026-11-11", "", "",
        "与 主人(master) 的往来印象卡；近段 18 条往来；话题：穿搭",
        '["穿搭", "日常"]', "[]", '["主人(master)"]', "",
    ), (
        "10001", "relation", "", "2026-11-11", "2026-11-11", "", "",
        "与 doro(10001) 的往来印象卡；话题：闲聊",
        '["闲聊"]', "[]", '["doro(10001)"]', "",
    )]
    monkeypatch.setattr(memory_brain, "open_brain", lambda: _Conn(rows))
    monkeypatch.setattr(identity, "master_uids", lambda: ["88888"])

    hit = memory_brain.search_narrative(kind="relation", peer="88888")
    docs = {d["doc_id"]: d["score"] for d in hit}
    assert "master" in docs
    # 同近因下，认领对象的卡必须排在普通卡前（peer 加权生效）
    assert docs["master"] > docs.get("10001", 0.0)

    # 未认领身份不享受主人卡加权
    hit2 = memory_brain.search_narrative(kind="relation", peer="10001")
    docs2 = {d["doc_id"]: d["score"] for d in hit2}
    assert docs2["master"] < docs2["10001"] or "master" not in docs2 or         docs2["master"] <= docs2["10001"]


# ---------------------------------------------------------------- L3 自我画像写点

def test_maintain_profile_writes_self_profile(tmp_path, monkeypatch):
    """L3 重写落 self_profile.md（不再有用户画像文件）。"""
    from omni_core import memory_brain
    from omni_core.local import runtime_paths as rp

    sp = tmp_path / "self_profile.md"
    monkeypatch.setattr(rp, "self_profile", lambda: sp)

    class _ProfileConn:
        def execute(self, *a, **k):
            class _C:
                rowcount = 1

                def fetchall(self):
                    return [("她最近迷上百元级平价lo裙",)] * 25

                def fetchone(self):
                    return ("",)
            return _C()

        def commit(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(memory_brain, "open_brain", lambda: _ProfileConn())
    monkeypatch.setattr(memory_brain, "_meta_get", lambda conn, key: "")
    monkeypatch.setattr(memory_brain, "_meta_set", lambda conn, k, v: None)

    from omni_core.local import llm_judge
    monkeypatch.setattr(llm_judge, "chat_json",
                        lambda cfg, system, prompt, timeout=60.0:
                        {"profile": "【消费偏好】喜欢平价lo裙。"})

    assert memory_brain.maintain_profile({"model": "x"}) is True
    assert sp.exists()
    assert "平价lo裙" in sp.read_text(encoding="utf-8")
