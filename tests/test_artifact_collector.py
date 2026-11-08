"""任务产物收编桥离线测试（零内核桥：沙箱产物 → 项目 corpus/diary 归位）。"""
from gateway.artifact_collector import collect


def _make(tmp_path, project="bilibili", artifacts=None):
    proj = tmp_path / "projects" / project
    (proj / "corpus").mkdir(parents=True)
    art = tmp_path / "tasks" / "t1" / "artifacts"
    art.mkdir(parents=True)
    for name, content in (artifacts or {}).items():
        p = art / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return proj, art


def test_corpus_files_land_in_project_corpus(tmp_path):
    proj, art = _make(tmp_path, artifacts={
        "hot.md": "# 热点", "formats.md": "# 形态", "lingo.md": "# 黑话",
        "notes.txt": "不收非 md",
    })
    placed = collect("bilibili", "t1", projects_root=tmp_path / "projects",
                     artifacts_root=art)
    assert len(placed) == 3
    assert (proj / "corpus" / "hot.md").read_text(encoding="utf-8") == "# 热点"
    assert (proj / "corpus" / "lingo.md").exists()
    assert not (proj / "corpus" / "notes.txt").exists()  # 非 md 不收


def test_date_prefixed_goes_to_diary(tmp_path):
    proj, art = _make(tmp_path, artifacts={"2026-11-08-觉醒.md": "今天逛了B站"})
    collect("bilibili", "t1", projects_root=tmp_path / "projects", artifacts_root=art)
    assert (proj / "diary" / "2026-11-08-觉醒.md").exists()
    assert not (proj / "corpus" / "2026-11-08-觉醒.md").exists()


def test_subdir_mirrors_only_existing(tmp_path):
    proj, art = _make(tmp_path, artifacts={
        "corpus/hot.md": "# 更新版",   # corpus/ 已存在 → 收
        "random/new.md": "不建新顶层",  # random/ 不存在 → 不收
    })
    collect("bilibili", "t1", projects_root=tmp_path / "projects", artifacts_root=art)
    assert (proj / "corpus" / "hot.md").read_text(encoding="utf-8") == "# 更新版"
    assert not (proj / "random").exists()


def test_no_overwrite_when_same_content(tmp_path):
    proj, art = _make(tmp_path, artifacts={"hot.md": "# 同内容"})
    collect("bilibili", "t1", projects_root=tmp_path / "projects", artifacts_root=art)
    before = (proj / "corpus" / "hot.md").stat().st_mtime
    collect("bilibili", "t1", projects_root=tmp_path / "projects", artifacts_root=art)
    assert (proj / "corpus" / "hot.md").stat().st_mtime == before  # 内容没变不重复写


def test_missing_dirs_noop(tmp_path):
    # 项目/产物区都不存在 → 静默无操作
    assert collect("nope", "t_nope", projects_root=tmp_path / "p",
                   artifacts_root=tmp_path / "a") == []
