"""任务产物收编桥离线测试（零内核桥：沙箱产物按任务写入的相对路径归位）。"""
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


def test_relative_paths_mirror_into_project(tmp_path):
    proj, art = _make(tmp_path, artifacts={
        "corpus/hot.md": "# 热点",
        "corpus/formats.md": "# 形态",
        "corpus/lingo.md": "# 黑话",
    })
    placed = collect("bilibili", "t1", projects_root=tmp_path / "projects",
                     artifacts_root=art)
    assert len(placed) == 3
    assert (proj / "corpus" / "hot.md").read_text(encoding="utf-8") == "# 热点"
    assert (proj / "corpus" / "lingo.md").exists()


def test_diary_subdir_kept(tmp_path):
    proj, art = _make(tmp_path, artifacts={"diary/2026-11-08-觉醒.md": "今天逛了B站"})
    collect("bilibili", "t1", projects_root=tmp_path / "projects", artifacts_root=art)
    assert (proj / "diary" / "2026-11-08-觉醒.md").exists()
    assert not (proj / "corpus" / "2026-11-08-觉醒.md").exists()


def test_new_subdir_created(tmp_path):
    """knowledge/、days/ 这类任务新建的子目录照原路径创建，不再要求预先存在。"""
    proj, art = _make(tmp_path, project="life", artifacts={
        "knowledge/2026-11-09.jsonl": '{"a":1}',
        "days/2026-11-09.md": "# 一天",
    })
    collect("life", "t1", projects_root=tmp_path / "projects", artifacts_root=art)
    assert (tmp_path / "projects" / "life" / "knowledge" / "2026-11-09.jsonl").exists()
    assert (tmp_path / "projects" / "life" / "days" / "2026-11-09.md").exists()


def test_all_file_types_collected(tmp_path):
    """不再按后缀筛选：任务写什么就归位什么（落位由任务决定）。"""
    proj, art = _make(tmp_path, artifacts={"corpus/notes.txt": "非 md 也收"})
    collect("bilibili", "t1", projects_root=tmp_path / "projects", artifacts_root=art)
    assert (proj / "corpus" / "notes.txt").read_text(encoding="utf-8") == "非 md 也收"


def test_no_overwrite_when_same_content(tmp_path):
    proj, art = _make(tmp_path, artifacts={"corpus/hot.md": "# 同内容"})
    collect("bilibili", "t1", projects_root=tmp_path / "projects", artifacts_root=art)
    before = (proj / "corpus" / "hot.md").stat().st_mtime_ns
    collect("bilibili", "t1", projects_root=tmp_path / "projects", artifacts_root=art)
    assert (proj / "corpus" / "hot.md").stat().st_mtime_ns == before  # 内容没变不重复写


def test_missing_dirs_noop(tmp_path):
    # 项目/产物区都不存在 → 静默无操作
    assert collect("nope", "t_nope", projects_root=tmp_path / "p",
                   artifacts_root=tmp_path / "a") == []
