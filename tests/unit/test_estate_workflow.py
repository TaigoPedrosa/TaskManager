import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from taskmanager.cli.main import app
from taskmanager.core.enums import NodeKind, NodeStatus, VerificationType
from taskmanager.core.models import Node, NodeVerification
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.engine.git import GitManager
from taskmanager.engine.graph import GraphEngine
from taskmanager.engine.runtime import ExecutionCoordinator
from taskmanager.renderers.importers import BulkImporter

runner = CliRunner()


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-b", "main")
    git(path, "config", "user.email", "ci@example.com")
    git(path, "config", "user.name", "CI")
    git(path, "commit", "--allow-empty", "-m", "init")


@pytest.fixture
def db(tmp_path: Path) -> DatabaseManager:
    manager = DatabaseManager(tmp_path / "db")
    manager.init_all()
    return manager


def task(node_repo: NodeRepository, task_id: str, **kw: object) -> Node:
    node = Node(id=task_id, kind=NodeKind.TASK, title=task_id, **kw)  # type: ignore[arg-type]
    node_repo.save_node(node)
    return node


def coordinator(db: DatabaseManager, git_root: Path | None = None) -> ExecutionCoordinator:
    node_repo, runtime_repo = NodeRepository(db), RuntimeRepository(db)
    return ExecutionCoordinator(
        node_repo=node_repo,
        runtime_repo=runtime_repo,
        graph_engine=GraphEngine(node_repo=node_repo, runtime_repo=runtime_repo),
        git_mgr=GitManager(git_root) if git_root else None,
    )


def test_declared_files_are_paths_from_frontmatter_and_path_verifications(
    db: DatabaseManager,
) -> None:
    repo = NodeRepository(db)
    task(repo, "T-1", frontmatter={"declared_files": ["web/a.tsx", "web/b.tsx"]})
    repo.add_verification(
        NodeVerification(
            node_id="T-1", verification_type=VerificationType.FILE_EXISTS, target_path="web/c.tsx"
        )
    )
    repo.add_verification(
        NodeVerification(
            node_id="T-1",
            verification_type=VerificationType.TEST_COMMAND,
            target_path="label, not a path",
            expected_pattern="true",
        )
    )
    assert repo.declared_files("T-1") == ["web/c.tsx", "web/a.tsx", "web/b.tsx"]


def test_a_lease_locks_frontmatter_files_and_a_second_claim_is_refused(db: DatabaseManager) -> None:
    repo = NodeRepository(db)
    task(repo, "T-1", frontmatter={"declared_files": ["web/shared.tsx"]})
    task(repo, "T-2", frontmatter={"declared_files": ["web/shared.tsx"]})
    coord = coordinator(db)
    coord.start_task("T-1", "a", "s")
    with pytest.raises(ValueError, match="file collision"):
        coord.start_task("T-2", "b", "s")
    coord.stop_task("T-1", NodeStatus.COMPLETED)
    coord.start_task("T-2", "b", "s")


def test_the_lease_ttl_is_the_one_asked_for(db: DatabaseManager) -> None:
    repo = NodeRepository(db)
    task(repo, "T-1")
    lease = coordinator(db).start_task("T-1", "a", "s", ttl_seconds=1800)
    assert lease.ttl_seconds == 1800


def test_a_repeated_verification_is_stored_once(db: DatabaseManager) -> None:
    repo = NodeRepository(db)
    task(repo, "T-1")
    for _ in range(3):
        repo.add_verification(
            NodeVerification(
                node_id="T-1", verification_type=VerificationType.FILE_EXISTS, target_path="x"
            )
        )
        repo.add_verification(
            NodeVerification(
                node_id="T-1",
                verification_type=VerificationType.TEST_COMMAND,
                target_path="x",
                expected_pattern="true",
            )
        )
    assert len(repo.get_verifications("T-1")) == 2


def test_an_import_naming_an_unknown_dependency_writes_nothing(db: DatabaseManager) -> None:
    repo = NodeRepository(db)
    importer = BulkImporter(repo)
    doc = {
        "plans": [
            {
                "id": "P",
                "title": "P",
                "tasks": [{"id": "P-1", "title": "a", "depends_on": ["nope"]}],
            }
        ]
    }
    with pytest.raises(ValueError, match="nothing written"):
        importer.import_dict(doc)
    assert repo.get_node("P") is None
    assert repo.get_node("P-1") is None


def test_importing_the_same_document_twice_changes_nothing(db: DatabaseManager) -> None:
    repo = NodeRepository(db)
    importer = BulkImporter(repo)
    doc = {
        "plans": [
            {
                "id": "P",
                "title": "P",
                "tasks": [
                    {
                        "id": "P-1",
                        "title": "a",
                        "verifications": [{"type": "file_exists", "target_path": "f"}],
                    }
                ],
            }
        ]
    }
    importer.import_dict(doc)
    importer.import_dict(doc)
    assert len(repo.get_verifications("P-1")) == 1


def test_a_worktree_is_cut_in_the_tasks_own_repository_from_origin_main_without_upstream(
    db: DatabaseManager, tmp_path: Path
) -> None:
    root = tmp_path / "estate"
    init_repo(root)
    init_repo(root / "web")
    git(root / "web", "update-ref", "refs/remotes/origin/main", "HEAD")
    repo = NodeRepository(db)
    task(repo, "T-1", target_repo="web")
    coord = coordinator(db, root)

    lease = coord.start_task("T-1", "a", "s", create_worktree=True, worktree_base=tmp_path / "wt")

    worktree = Path(lease.worktree_path or "")
    assert worktree == tmp_path / "wt" / "web-T-1"
    assert git(worktree, "rev-parse", "--abbrev-ref", "HEAD") == "tm/T-1"
    upstream = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "tm/T-1@{upstream}"],
        cwd=root / "web",
        capture_output=True,
        text=True,
        check=False,
    )
    assert upstream.returncode != 0
    assert "tm/T-1" in git(root / "web", "branch", "--list", "tm/T-1")
    assert "tm/T-1" not in git(root, "branch", "--list")

    coord.stop_task("T-1", NodeStatus.WAITING_REVIEW, remove_worktree=True)
    assert not worktree.exists()


def test_a_worktree_needs_a_git_repository_for_the_task(
    db: DatabaseManager, tmp_path: Path
) -> None:
    root = tmp_path / "estate"
    init_repo(root)
    repo = NodeRepository(db)
    task(repo, "T-1", target_repo="web")
    with pytest.raises(ValueError, match="not a git repository"):
        coordinator(db, root).start_task(
            "T-1", "a", "s", create_worktree=True, worktree_base=tmp_path / "wt"
        )


def test_the_root_is_found_from_a_subdirectory_and_from_a_worktree_outside_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "estate"
    root.mkdir()
    assert runner.invoke(app, ["init", "-C", str(root)]).exit_code == 0
    init_repo(root / "web")
    git(root / "web", "update-ref", "refs/remotes/origin/main", "HEAD")
    outside = tmp_path / "elsewhere" / "wt"
    git(root / "web", "worktree", "add", "--no-track", "-b", "b", str(outside), "origin/main")
    monkeypatch.delenv("TM_ROOT", raising=False)

    monkeypatch.chdir(root / "web")
    assert runner.invoke(app, ["spec", "list"]).exit_code == 0
    monkeypatch.chdir(outside)
    res = runner.invoke(app, ["spec", "list"])
    assert res.exit_code == 0, res.output


def test_a_command_outside_any_root_is_an_error_and_creates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lonely = tmp_path / "lonely"
    lonely.mkdir()
    monkeypatch.chdir(lonely)
    monkeypatch.delenv("TM_ROOT", raising=False)
    res = runner.invoke(app, ["next"])
    assert res.exit_code != 0
    assert not (lonely / ".taskmanager").exists()
    missing = tmp_path / "typo"
    assert runner.invoke(app, ["next", "-C", str(missing)]).exit_code != 0
    assert not missing.exists()


def test_tm_root_selects_the_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "estate"
    root.mkdir()
    runner.invoke(app, ["init", "-C", str(root)])
    runner.invoke(app, ["spec", "add", "S", "--slug", "S1", "-C", str(root)])
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.chdir(other)
    monkeypatch.setenv("TM_ROOT", str(root))
    res = runner.invoke(app, ["spec", "list"])
    assert res.exit_code == 0 and "S1" in res.output


def test_task_update_changes_only_what_it_is_given(tmp_path: Path) -> None:
    runner.invoke(app, ["init", "-C", str(tmp_path)])
    runner.invoke(app, ["spec", "add", "S", "--slug", "S1", "-C", str(tmp_path)])
    runner.invoke(app, ["plan", "add", "P", "--spec", "S1", "--slug", "P1", "-C", str(tmp_path)])
    runner.invoke(app, ["task", "add", "T", "--plan", "S1-P1", "--slug", "t1", "-C", str(tmp_path)])
    res = runner.invoke(
        app,
        [
            "task",
            "update",
            "S1-P1-t1",
            "--models",
            "claude-sonnet-5,gemini-3.8-flash-high",
            "--repo",
            "web",
            "-C",
            str(tmp_path),
        ],
    )
    assert res.exit_code == 0, res.output
    node = NodeRepository(DatabaseManager(tmp_path / ".taskmanager")).get_node("S1-P1-t1")
    assert node is not None
    assert node.acceptable_models == ["claude-sonnet-5", "gemini-3.8-flash-high"]
    assert node.target_repo == "web"
    assert node.title == "T"
    assert runner.invoke(app, ["task", "update", "S1-P1-t1", "-C", str(tmp_path)]).exit_code != 0


def test_two_exports_of_one_state_are_byte_identical(tmp_path: Path) -> None:
    runner.invoke(app, ["init", "-C", str(tmp_path)])
    runner.invoke(app, ["spec", "add", "S", "--slug", "S1", "-C", str(tmp_path)])
    runner.invoke(app, ["plan", "add", "P", "--spec", "S1", "--slug", "P1", "-C", str(tmp_path)])
    runner.invoke(app, ["task", "add", "T", "--plan", "S1-P1", "--slug", "t1", "-C", str(tmp_path)])
    runner.invoke(app, ["section", "set", "S1-P1-t1:body", "text", "-C", str(tmp_path)])
    for name in ("one", "two"):
        assert (
            runner.invoke(app, ["export", str(tmp_path / name), "-C", str(tmp_path)]).exit_code == 0
        )
    a = (tmp_path / "one" / "S1-P1.json").read_bytes()
    assert a == (tmp_path / "two" / "S1-P1.json").read_bytes()
    doc = json.loads(a)
    assert doc["spec"]["id"] == "S1"
    plan = doc["plans"][0]
    assert [t["id"] for t in plan["tasks"]] == ["S1-P1-t1"]
    assert plan["tasks"][0]["sections"][0]["content"] == "text"
    assert (tmp_path / "one" / "_spec-S1.json").exists()


def test_an_export_restores_into_a_fresh_root_and_exports_identically(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    runner.invoke(app, ["init", "-C", str(source)])
    runner.invoke(app, ["spec", "add", "S", "--slug", "S1", "-C", str(source)])
    for plan in ("P1", "P2"):
        runner.invoke(app, ["plan", "add", plan, "--spec", "S1", "--slug", plan, "-C", str(source)])
    runner.invoke(app, ["task", "add", "a", "--plan", "S1-P1", "--slug", "a", "-C", str(source)])
    # A dependency across plans: restoring one plan at a time would refuse it.
    runner.invoke(
        app,
        [
            "task",
            "add",
            "b",
            "--plan",
            "S1-P2",
            "--slug",
            "b",
            "--depends-on",
            "S1-P1-a",
            "-C",
            str(source),
        ],
    )
    runner.invoke(app, ["section", "set", "S1-P1-a:body", "line one\nline two", "-C", str(source)])
    runner.invoke(
        app,
        [
            "verify",
            "add",
            "S1-P1-a",
            "--type",
            "test_command",
            "--target",
            "x",
            "--pattern",
            "true",
            "-C",
            str(source),
        ],
    )
    runner.invoke(
        app, ["task", "update", "S1-P1-a", "--set", 'declared_files=["web/a"]', "-C", str(source)]
    )
    runner.invoke(app, ["run", "stop", "S1-P1-a", "--status", "DEFERRED", "-C", str(source)])
    runner.invoke(app, ["section", "set", "S1:overview", "the spec text", "-C", str(source)])
    runner.invoke(app, ["config", "set", "lease_ttl", "600", "-C", str(source)])
    runner.invoke(app, ["config", "set", "embeddings.provider", "mock", "-C", str(source)])
    assert runner.invoke(app, ["export", str(tmp_path / "e1"), "-C", str(source)]).exit_code == 0

    fresh = tmp_path / "fresh"
    fresh.mkdir()
    res = runner.invoke(app, ["restore", str(tmp_path / "e1"), "-C", str(fresh)])
    assert res.exit_code == 0, res.output
    assert "Restored 2 plans and 1 specs" in res.output
    assert runner.invoke(app, ["export", str(tmp_path / "e2"), "-C", str(fresh)]).exit_code == 0
    for f in sorted((tmp_path / "e1").glob("*.json")):
        assert f.read_bytes() == (tmp_path / "e2" / f.name).read_bytes(), f.name
    restored = json.loads(
        runner.invoke(app, ["task", "get", "S1-P2-b", "--json", "-C", str(fresh)]).stdout
    )
    assert [d["id"] for d in restored["depends_on"]] == ["S1-P1-a"]
    assert json.loads((tmp_path / "e1" / "_config.json").read_text()) == {
        "embeddings": {"provider": "mock"},
        "lease_ttl": 600,
    }
    assert (fresh / ".taskmanager" / "config.yaml").read_bytes() == (
        source / ".taskmanager" / "config.yaml"
    ).read_bytes()


def test_a_directory_without_a_config_file_restores_with_defaults(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    runner.invoke(app, ["init", "-C", str(source)])
    runner.invoke(app, ["spec", "add", "S", "--slug", "S1", "-C", str(source)])
    assert runner.invoke(app, ["export", str(tmp_path / "e1"), "-C", str(source)]).exit_code == 0
    assert not (tmp_path / "e1" / "_config.json").exists()

    fresh = tmp_path / "fresh"
    fresh.mkdir()
    assert runner.invoke(app, ["restore", str(tmp_path / "e1"), "-C", str(fresh)]).exit_code == 0
    assert not (fresh / ".taskmanager" / "config.yaml").exists()
    listed = json.loads(runner.invoke(app, ["config", "list", "--json", "-C", str(fresh)]).stdout)
    assert {row["source"] for row in listed} == {"default"}


def _seed_estate(tmp_path: Path) -> None:
    runner.invoke(app, ["init", "-C", str(tmp_path)])
    runner.invoke(app, ["spec", "add", "S", "--slug", "S1", "-C", str(tmp_path)])
    runner.invoke(app, ["plan", "add", "P", "--spec", "S1", "--slug", "P1", "-C", str(tmp_path)])
    for slug in ("a", "b"):
        runner.invoke(
            app,
            ["task", "add", f"T {slug}", "--plan", "S1-P1", "--slug", slug, "-C", str(tmp_path)]
            + (["--depends-on", "S1-P1-a"] if slug == "b" else []),
        )
    runner.invoke(
        app, ["section", "set", "S1-P1-a:body", "line one\nline two", "-C", str(tmp_path)]
    )


def test_reads_are_json_or_yaml_and_the_two_agree(tmp_path: Path) -> None:
    import yaml

    _seed_estate(tmp_path)
    for args in (["task", "list"], ["plan", "list"], ["spec", "list"]):
        as_json = runner.invoke(app, [*args, "--json", "-C", str(tmp_path)])
        as_yaml = runner.invoke(app, [*args, "--yaml", "-C", str(tmp_path)])
        assert as_json.exit_code == 0 and as_yaml.exit_code == 0
        assert json.loads(as_json.stdout) == yaml.safe_load(as_yaml.stdout)
        assert len(as_yaml.stdout) < len(as_json.stdout)
    rows = json.loads(runner.invoke(app, ["task", "list", "--json", "-C", str(tmp_path)]).stdout)
    assert {r["id"]: r["state"] for r in rows} == {"S1-P1-a": "READY", "S1-P1-b": "BLOCKED"}


def test_task_get_json_names_blockers_and_the_lease(tmp_path: Path) -> None:
    _seed_estate(tmp_path)
    doc = json.loads(
        runner.invoke(app, ["task", "get", "S1-P1-b", "--json", "-C", str(tmp_path)]).stdout
    )
    assert doc["blocked_by"] == ["S1-P1-a"]
    assert doc["depends_on"] == [{"id": "S1-P1-a", "status": "NOT_STARTED"}]
    assert doc["lease"] is None
    runner.invoke(
        app, ["run", "start", "S1-P1-a", "--agent", "x", "--session", "y", "-C", str(tmp_path)]
    )
    leased = json.loads(
        runner.invoke(app, ["task", "get", "S1-P1-a", "--json", "-C", str(tmp_path)]).stdout
    )
    assert leased["state"] == "IN_FLIGHT" and leased["lease"]["agent_id"] == "x"
    assert "body" in leased["sections"]


def test_an_unknown_task_exits_nonzero_for_every_output_form(tmp_path: Path) -> None:
    _seed_estate(tmp_path)
    for flag in ("--json", "--yaml"):
        res = runner.invoke(app, ["task", "get", "nope", flag, "-C", str(tmp_path)])
        assert res.exit_code == 1 and "not found" in res.output


def test_yaml_keeps_multiline_text_readable(capsys: pytest.CaptureFixture[str]) -> None:
    import yaml

    from taskmanager.cli.main import _emit

    doc = {"text": "line one\nline two", "n": 3, "none": None}
    _emit(doc, as_yaml=True)
    out = capsys.readouterr().out
    assert out.splitlines()[0].endswith("|-") and "\\n" not in out
    assert yaml.safe_load(out) == doc


def test_each_stage_of_the_lifecycle_is_claimed_by_its_own_lease(db: DatabaseManager) -> None:
    repo = NodeRepository(db)
    task(repo, "T-1", frontmatter={"declared_files": ["web/a.tsx"]})
    task(repo, "T-2", frontmatter={"declared_files": ["web/a.tsx"]})
    coord = coordinator(db)

    coord.start_task("T-1", "impl", "s")
    assert repo.get_node("T-1").status == NodeStatus.IMPLEMENTING  # type: ignore[union-attr]
    coord.stop_task("T-1", NodeStatus.WAITING_REVIEW)

    coord.start_task("T-1", "rev", "s")
    assert repo.get_node("T-1").status == NodeStatus.REVIEWING  # type: ignore[union-attr]
    # A review locks nothing, so another task on the same file starts while it runs.
    coord.start_task("T-2", "other", "s")
    coord.stop_task("T-2", NodeStatus.NOT_STARTED)
    coord.stop_task("T-1", NodeStatus.WAITING_FIXES)

    coord.start_task("T-1", "fix", "s")
    assert repo.get_node("T-1").status == NodeStatus.FIXING  # type: ignore[union-attr]
    with pytest.raises(ValueError, match="file collision"):
        coord.start_task("T-2", "other", "s")
    coord.stop_task("T-1", NodeStatus.WAITING_MERGE)

    with pytest.raises(ValueError, match="not ready to start"):
        coord.start_task("T-1", "late", "s")


def test_a_fix_round_reuses_the_branch_and_worktree_its_first_round_cut(
    db: DatabaseManager, tmp_path: Path
) -> None:
    root = tmp_path / "estate"
    init_repo(root)
    init_repo(root / "web")
    git(root / "web", "update-ref", "refs/remotes/origin/main", "HEAD")
    repo = NodeRepository(db)
    task(repo, "T-1", target_repo="web")
    coord = coordinator(db, root)
    first = coord.start_task("T-1", "a", "s", create_worktree=True, worktree_base=tmp_path / "wt")
    coord.stop_task("T-1", NodeStatus.WAITING_FIXES)
    again = coord.start_task("T-1", "b", "s", create_worktree=True, worktree_base=tmp_path / "wt")
    assert again.worktree_path == first.worktree_path
    coord.stop_task("T-1", NodeStatus.WAITING_FIXES, remove_worktree=True)
    # Removed, the branch survives, so a later round cuts the worktree again from it.
    third = coord.start_task("T-1", "c", "s", create_worktree=True, worktree_base=tmp_path / "wt")
    assert Path(third.worktree_path or "").exists()


def test_guide_serves_the_builtin_text_then_the_project_addendum(tmp_path: Path) -> None:
    runner.invoke(app, ["init", "-C", str(tmp_path)])
    listed = runner.invoke(app, ["guide", "-C", str(tmp_path)])
    assert listed.exit_code == 0 and "overview:" in listed.stdout
    plain = runner.invoke(app, ["guide", "overview", "-C", str(tmp_path)])
    assert plain.exit_code == 0 and "How TaskManager works" in plain.stdout
    assert "project addendum" not in listed.stdout

    runner.invoke(app, ["spec", "add", "Project guide", "--slug", "guide", "-C", str(tmp_path)])
    runner.invoke(
        app, ["section", "set", "guide:overview", "Local rule: ask first.", "-C", str(tmp_path)]
    )
    both = runner.invoke(app, ["guide", "overview", "-C", str(tmp_path)])
    assert both.stdout.index("How TaskManager works") < both.stdout.index("Local rule: ask first.")
    assert "(+ project addendum)" in runner.invoke(app, ["guide", "-C", str(tmp_path)]).stdout
    only = runner.invoke(app, ["guide", "overview", "--project", "-C", str(tmp_path)])
    assert only.stdout.strip() == "Local rule: ask first."
    assert runner.invoke(app, ["guide", "nope", "-C", str(tmp_path)]).exit_code != 0


def test_guide_lists_a_project_only_topic(tmp_path: Path) -> None:
    runner.invoke(app, ["init", "-C", str(tmp_path)])
    runner.invoke(app, ["spec", "add", "Project guide", "--slug", "guide", "-C", str(tmp_path)])
    runner.invoke(
        app, ["section", "set", "guide:frontend", "Use the design system.", "-C", str(tmp_path)]
    )
    listed = runner.invoke(app, ["guide", "-C", str(tmp_path)]).stdout
    assert "frontend: (project topic)" in listed
    assert (
        runner.invoke(app, ["guide", "frontend", "-C", str(tmp_path)]).stdout.strip()
        == "Use the design system."
    )


def test_completing_a_task_removes_the_worktree_it_no_longer_holds_a_lease_on(
    db: DatabaseManager, tmp_path: Path
) -> None:
    root = tmp_path / "estate"
    init_repo(root)
    init_repo(root / "web")
    git(root / "web", "update-ref", "refs/remotes/origin/main", "HEAD")
    repo = NodeRepository(db)
    task(repo, "T-1", target_repo="web")
    coord = coordinator(db, root)
    lease = coord.start_task("T-1", "a", "s", create_worktree=True, worktree_base=tmp_path / "wt")
    coord.stop_task("T-1", NodeStatus.WAITING_MERGE)
    worktree = Path(lease.worktree_path or "")
    assert worktree.exists()
    coord.stop_task("T-1", NodeStatus.COMPLETED, remove_worktree=True)
    assert not worktree.exists()


def test_a_swept_lease_returns_the_task_to_the_state_before_its_claim(tmp_path: Path) -> None:
    _seed_estate(tmp_path)
    runner.invoke(
        app,
        [
            "run",
            "start",
            "S1-P1-a",
            "--agent",
            "x",
            "--session",
            "y",
            "--ttl",
            "1",
            "-C",
            str(tmp_path),
        ],
    )
    import time

    time.sleep(1.2)
    out = runner.invoke(app, ["run", "sweep", "-C", str(tmp_path)])
    assert "S1-P1-a" in out.output
    doc = json.loads(
        runner.invoke(app, ["task", "get", "S1-P1-a", "--json", "-C", str(tmp_path)]).stdout
    )
    assert doc["status"] == "NOT_STARTED" and doc["state"] == "READY" and doc["lease"] is None


def test_an_empty_check_set_and_an_unknown_render_are_refusals(tmp_path: Path) -> None:
    _seed_estate(tmp_path)
    empty = runner.invoke(app, ["verify", "run", "S1-P1-a", "-C", str(tmp_path)])
    assert empty.exit_code == 2 and "proves nothing" in empty.output
    unknown = runner.invoke(app, ["render", "NOPE", "-C", str(tmp_path)])
    assert unknown.exit_code == 1 and "Traceback" not in unknown.output


def test_root_prints_the_project_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runner.invoke(app, ["init", "-C", str(tmp_path)])
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    monkeypatch.delenv("TM_ROOT", raising=False)
    assert runner.invoke(app, ["root"]).stdout.strip() == str(tmp_path.resolve())


def test_supersede_refuses_a_missing_replacement_and_releases_the_old_lease(tmp_path: Path) -> None:
    _seed_estate(tmp_path)
    runner.invoke(
        app, ["run", "start", "S1-P1-a", "--agent", "x", "--session", "y", "-C", str(tmp_path)]
    )
    bad = runner.invoke(app, ["task", "supersede", "S1-P1-a", "NOTHING", "-C", str(tmp_path)])
    assert bad.exit_code == 1 and "nothing was changed" in bad.output
    still = json.loads(
        runner.invoke(app, ["task", "get", "S1-P1-a", "--json", "-C", str(tmp_path)]).stdout
    )
    assert still["status"] == "IMPLEMENTING" and still["lease"] is not None
    ok = runner.invoke(app, ["task", "supersede", "S1-P1-a", "S1-P1-b", "-C", str(tmp_path)])
    assert ok.exit_code == 0
    done = json.loads(
        runner.invoke(app, ["task", "get", "S1-P1-a", "--json", "-C", str(tmp_path)]).stdout
    )
    assert done["status"] == "SUPERSEDED" and done["lease"] is None


def test_a_section_needs_its_node_and_a_frontmatter_key_can_be_set(tmp_path: Path) -> None:
    _seed_estate(tmp_path)
    missing = runner.invoke(app, ["section", "set", "guide:implement", "x", "-C", str(tmp_path)])
    assert (
        missing.exit_code == 1
        and "tm spec add" in missing.output
        and "Traceback" not in missing.output
    )
    res = runner.invoke(
        app,
        [
            "task",
            "update",
            "S1-P1-a",
            "--set",
            'declared_files=["web/a.tsx"]',
            "--set",
            "note=hello",
            "-C",
            str(tmp_path),
        ],
    )
    assert res.exit_code == 0, res.output
    doc = json.loads(
        runner.invoke(app, ["task", "get", "S1-P1-a", "--json", "-C", str(tmp_path)]).stdout
    )
    assert doc["declared_files"] == ["web/a.tsx"] and doc["frontmatter"]["note"] == "hello"


def test_next_returns_a_batch_whose_tasks_share_no_file(tmp_path: Path) -> None:
    runner.invoke(app, ["init", "-C", str(tmp_path)])
    doc = {
        "plans": [
            {
                "id": "P",
                "title": "P",
                "tasks": [
                    {"id": f"P-{n}", "title": n, "frontmatter": {"declared_files": [f]}}
                    for n, f in (("a", "x"), ("b", "x"), ("c", "y"))
                ],
            }
        ]
    }
    f = tmp_path / "doc.json"
    f.write_text(json.dumps(doc))
    runner.invoke(app, ["import", "-f", str(f), "-C", str(tmp_path)])
    rows = json.loads(runner.invoke(app, ["next", "-n", "5", "--json", "-C", str(tmp_path)]).stdout)
    files = [r["declared_files"][0] for r in rows]
    assert sorted(files) == ["x", "y"]


def test_plan_list_reports_the_state_its_tasks_add_up_to(tmp_path: Path) -> None:
    _seed_estate(tmp_path)
    rows = json.loads(runner.invoke(app, ["plan", "list", "--json", "-C", str(tmp_path)]).stdout)
    assert rows[0]["status"] == "NOT_STARTED" and rows[0]["state"] == "NOT_STARTED"
    for t in ("S1-P1-a", "S1-P1-b"):
        runner.invoke(app, ["run", "stop", t, "--status", "COMPLETED", "-C", str(tmp_path)])
    rows = json.loads(runner.invoke(app, ["plan", "list", "--json", "-C", str(tmp_path)]).stdout)
    assert rows[0]["status"] == "NOT_STARTED" and rows[0]["state"] == "COMPLETED"


def test_task_depends_adds_removes_and_refuses_a_cycle_or_an_unknown_id(tmp_path: Path) -> None:
    _seed_estate(tmp_path)
    root = str(tmp_path)
    # b already depends on a: a depending on b would close a cycle.
    cycle = runner.invoke(app, ["task", "depends", "S1-P1-a", "--add", "S1-P1-b", "-C", root])
    assert cycle.exit_code == 1 and "cycle" in cycle.output
    unknown = runner.invoke(
        app, ["task", "depends", "S1-P1-b", "--add", "S1-P1-a,NOPE", "-C", root]
    )
    assert unknown.exit_code == 1 and "NOPE" in unknown.output
    same = json.loads(runner.invoke(app, ["task", "get", "S1-P1-b", "--json", "-C", root]).stdout)
    assert [d["id"] for d in same["depends_on"]] == ["S1-P1-a"]  # nothing partial was written
    runner.invoke(app, ["task", "add", "c", "--plan", "S1-P1", "--slug", "c", "-C", root])
    assert (
        runner.invoke(
            app, ["task", "depends", "S1-P1-c", "--add", "S1-P1-a,S1-P1-b", "-C", root]
        ).exit_code
        == 0
    )
    assert (
        runner.invoke(app, ["task", "depends", "S1-P1-c", "--add", "S1-P1-a", "-C", root]).exit_code
        == 0
    )
    assert (
        runner.invoke(
            app, ["task", "depends", "S1-P1-c", "--remove", "S1-P1-b", "-C", root]
        ).exit_code
        == 0
    )
    got = json.loads(runner.invoke(app, ["task", "get", "S1-P1-c", "--json", "-C", root]).stdout)
    assert [d["id"] for d in got["depends_on"]] == ["S1-P1-a"]
    assert (
        runner.invoke(
            app, ["task", "depends", "S1-P1-c", "--remove", "S1-P1-b", "-C", root]
        ).exit_code
        == 1
    )


def test_brackets_in_task_text_reach_the_reader_untouched(tmp_path: Path) -> None:
    _seed_estate(tmp_path)
    root = str(tmp_path)
    text = "route /teams/[/{level_id}] and the rule [report-path] and a [bold]tag[/bold]"
    runner.invoke(app, ["section", "set", "S1-P1-a:objective", text, "-C", root])
    runner.invoke(
        app, ["task", "update", "S1-P1-a", "--title", "keep [x] and [/y] in a title", "-C", root]
    )
    for args in (
        ["render", "S1-P1-a", "--view", "subagent"],
        ["section", "get", "S1-P1-a:objective"],
    ):
        out = runner.invoke(app, [*args, "-C", root])
        assert out.exit_code == 0, out.output
        assert (
            "[/{level_id}]" in out.output
            and "[report-path]" in out.output
            and "[bold]tag[/bold]" in out.output
        )
    for args in (["task", "list"], ["task", "get", "S1-P1-a"]):
        out = runner.invoke(app, [*args, "-C", root])
        assert out.exit_code == 0 and "[x]" in out.output and "[/y]" in out.output


def test_reimporting_a_document_keeps_the_progress_it_does_not_state(db: DatabaseManager) -> None:
    repo = NodeRepository(db)
    importer = BulkImporter(repo)
    doc = {
        "plans": [{"id": "P", "title": "P", "tasks": [{"id": "P-1", "title": "a", "priority": 70}]}]
    }
    importer.import_dict(doc)
    coord = coordinator(db)
    coord.start_task("P-1", "x", "s")
    coord.stop_task("P-1", NodeStatus.WAITING_MERGE)
    node = repo.get_node("P-1")
    assert node is not None
    node.acceptable_models = ["claude-sonnet-5"]
    node.frontmatter = {"declared_files": ["web/a"]}
    repo.save_node(node)

    importer.import_dict(
        {"plans": [{"id": "P", "title": "P", "tasks": [{"id": "P-1", "title": "a renamed"}]}]}
    )
    kept = repo.get_node("P-1")
    assert kept is not None
    assert kept.title == "a renamed"
    assert kept.status == NodeStatus.WAITING_MERGE and kept.priority == 70
    assert kept.acceptable_models == ["claude-sonnet-5"] and kept.frontmatter == {
        "declared_files": ["web/a"]
    }

    importer.import_dict(
        {
            "plans": [
                {
                    "id": "P",
                    "title": "P",
                    "tasks": [{"id": "P-1", "title": "a", "status": "COMPLETED", "priority": 20}],
                }
            ]
        }
    )
    stated = repo.get_node("P-1")
    assert stated is not None and stated.status == NodeStatus.COMPLETED and stated.priority == 20


def test_a_document_that_states_checks_replaces_them_and_verify_can_list_and_remove(
    tmp_path: Path,
) -> None:
    root = str(tmp_path)
    runner.invoke(app, ["init", "-C", root])

    def doc(pattern: str) -> Path:
        f = tmp_path / "d.json"
        check = {"type": "test_command", "target_path": "x", "expected_pattern": pattern}
        f.write_text(
            json.dumps(
                {
                    "plans": [
                        {
                            "id": "P",
                            "title": "P",
                            "tasks": [{"id": "P-1", "title": "a", "verifications": [check]}],
                        }
                    ]
                }
            )
        )
        return f

    runner.invoke(app, ["import", "-f", str(doc("false")), "-C", root])
    runner.invoke(app, ["import", "-f", str(doc("true")), "-C", root])
    rows = json.loads(runner.invoke(app, ["verify", "list", "P-1", "--json", "-C", root]).stdout)
    assert [r["expected_pattern"] for r in rows] == ["true"]  # the stale check did not survive

    runner.invoke(
        app, ["verify", "add", "P-1", "--type", "file_exists", "--target", "f", "-C", root]
    )
    rows = json.loads(runner.invoke(app, ["verify", "list", "P-1", "--json", "-C", root]).stdout)
    assert len(rows) == 2
    assert (
        runner.invoke(app, ["verify", "remove", "P-1", str(rows[1]["id"]), "-C", root]).exit_code
        == 0
    )
    assert (
        len(json.loads(runner.invoke(app, ["verify", "list", "P-1", "--json", "-C", root]).stdout))
        == 1
    )
    assert runner.invoke(app, ["verify", "remove", "P-1", "999", "-C", root]).exit_code == 1
    assert runner.invoke(app, ["verify", "list", "NOPE", "-C", root]).exit_code == 1
