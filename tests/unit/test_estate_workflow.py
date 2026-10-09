import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from taskmanager.cli.main import app
from taskmanager.core.enums import NodeKind, VerificationType
from taskmanager.core.models import Node, NodeVerification
from taskmanager.core.status import Status
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.di.container import create_container
from taskmanager.engine.operations import Operations
from taskmanager.renderers.importers import BulkImporter

runner = CliRunner()


def ops_with_code_to_land(root: Path, monkeypatch: pytest.MonkeyPatch) -> Operations:
    """Operations on the estate at `root` that read every container as having code to land:
    these estates have no repositories for the git check to read."""
    ops = create_container(root).get(Operations)
    monkeypatch.setattr(ops, "nothing_to_land", lambda _container: False)
    return ops


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
    subprocess.run(["git", "init", "-q", str(tmp_path / "web")], check=True)
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
    # A sibling that stays READY, so deferring "a" below doesn't leave the plan with no counted
    # child: `task defer` never re-derives the plan's own stored status the way `tm import`
    # does, and an all-set-aside plan would round-trip differently.
    runner.invoke(app, ["task", "add", "c", "--plan", "S1-P1", "--slug", "c", "-C", str(source)])
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
    runner.invoke(app, ["task", "defer", "S1-P1-a", "--note", "later", "-C", str(source)])
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
    # Deferring a task that others depend on strands those dependents behind a decision
    # ("drop the edge, defer, or abandon?"), so b now also waits on that decision.
    assert [d["id"] for d in restored["depends_on"]] == ["S1-P1-a", "decision-D1"]
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


def test_decision_and_attachment_export_restore_round_trip(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    runner.invoke(app, ["init", "-C", str(source)])
    runner.invoke(app, ["spec", "add", "S", "--slug", "S1", "-C", str(source)])
    runner.invoke(app, ["plan", "add", "P", "--spec", "S1", "--slug", "P1", "-C", str(source)])
    runner.invoke(app, ["task", "add", "T", "--plan", "S1-P1", "--slug", "t1", "-C", str(source)])
    runner.invoke(
        app,
        [
            "decision",
            "add",
            "Which way?",
            "--slug",
            "way",
            "--option",
            "a|Do X",
            "--recommend",
            "a",
            "--blocks",
            "S1-P1-t1",
            "-C",
            str(source),
        ],
    )
    asset_src = source / "shot.png"
    asset_src.write_bytes(b"png-content")
    runner.invoke(app, ["attach", "S1-P1-t1", str(asset_src), "-C", str(source)])

    e1 = tmp_path / "e1"
    assert runner.invoke(app, ["export", str(e1), "-C", str(source)]).exit_code == 0
    assert (e1 / "_decisions.json").exists()
    assert list((e1 / "assets").iterdir())

    fresh = tmp_path / "fresh"
    fresh.mkdir()
    res = runner.invoke(app, ["restore", str(e1), "-C", str(fresh)])
    assert res.exit_code == 0, res.output
    assert (fresh / ".taskmanager" / "assets").is_dir()
    assert sorted(p.name for p in (fresh / ".taskmanager" / "assets").iterdir()) == sorted(
        p.name for p in (e1 / "assets").iterdir()
    )

    task = json.loads(
        runner.invoke(app, ["task", "get", "S1-P1-t1", "--json", "-C", str(fresh)]).stdout
    )
    assert task["awaiting_decisions"] == ["decision-way"]

    e2 = tmp_path / "e2"
    assert runner.invoke(app, ["export", str(e2), "-C", str(fresh)]).exit_code == 0
    for f in sorted(e1.glob("*.json")):
        assert f.read_bytes() == (e2 / f.name).read_bytes(), f.name
    for f in sorted((e1 / "assets").iterdir()):
        assert f.read_bytes() == (e2 / "assets" / f.name).read_bytes(), f.name


def test_a_decision_does_not_crash_task_list_next_or_plan_rollups(tmp_path: Path) -> None:
    runner.invoke(app, ["init", "-C", str(tmp_path)])
    runner.invoke(app, ["spec", "add", "S", "--slug", "S1", "-C", str(tmp_path)])
    runner.invoke(app, ["plan", "add", "P", "--spec", "S1", "--slug", "P1", "-C", str(tmp_path)])
    runner.invoke(app, ["task", "add", "T", "--plan", "S1-P1", "--slug", "t1", "-C", str(tmp_path)])
    runner.invoke(app, ["decision", "add", "Q", "--slug", "q1", "-C", str(tmp_path)])

    for args in (
        ["task", "list", "--json", "-C", str(tmp_path)],
        ["next", "--json", "-C", str(tmp_path)],
        ["plan", "list", "--json", "-C", str(tmp_path)],
        ["spec", "list", "--json", "-C", str(tmp_path)],
    ):
        res = runner.invoke(app, args)
        assert res.exit_code == 0, (args, res.output)
        assert "decision-q1" not in res.output


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
    assert {r["id"]: r["state"] for r in rows} == {
        "S1-P1-a": "READY",
        "S1-P1-b": "BLOCKED_BY_TASK",
    }


def test_task_get_json_names_blockers_and_the_lease(tmp_path: Path) -> None:
    from taskmanager.core.models import Lease
    from taskmanager.core.status import Action, Status

    _seed_estate(tmp_path)
    root = str(tmp_path)
    doc = json.loads(runner.invoke(app, ["task", "get", "S1-P1-b", "--json", "-C", root]).stdout)
    assert doc["blocked_by"] == ["S1-P1-a"]
    assert doc["depends_on"] == [{"id": "S1-P1-a", "status": "READY"}]
    assert doc["lease"] is None
    db = DatabaseManager(tmp_path / ".taskmanager")
    node_repo = NodeRepository(db)
    node = node_repo.get_node("S1-P1-a")
    assert node is not None
    node.status, node.claimed_from = Status.IMPLEMENTING, Status.READY
    lease = Lease(
        task_id="S1-P1-a",
        agent_id="x",
        session_id="y",
        branch_name="tm/S1-P1-a",
        action=Action.IMPLEMENT,
        ttl_seconds=3600,
    )
    assert RuntimeRepository(db).claim(lease, [], node)
    leased = json.loads(runner.invoke(app, ["task", "get", "S1-P1-a", "--json", "-C", root]).stdout)
    assert leased["state"] == "IMPLEMENTING" and leased["lease"]["agent_id"] == "x"
    assert leased["lease"]["action"] == "implement"
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


def test_supersede_refuses_a_missing_replacement_and_a_node_under_a_live_lease(
    tmp_path: Path,
) -> None:
    from taskmanager.core.models import Lease
    from taskmanager.core.status import Action, Status

    _seed_estate(tmp_path)
    root = str(tmp_path)
    bad = runner.invoke(app, ["task", "supersede", "S1-P1-a", "NOTHING", "-C", root])
    assert bad.exit_code == 1 and "nothing was changed" in bad.output

    db = DatabaseManager(tmp_path / ".taskmanager")
    node_repo, runtime_repo = NodeRepository(db), RuntimeRepository(db)
    node = node_repo.get_node("S1-P1-a")
    assert node is not None
    node.status, node.claimed_from = Status.IMPLEMENTING, Status.READY
    lease = Lease(
        task_id="S1-P1-a",
        agent_id="x",
        session_id="y",
        branch_name="tm/S1-P1-a",
        action=Action.IMPLEMENT,
        ttl_seconds=3600,
    )
    assert runtime_repo.claim(lease, [], node)
    held = runner.invoke(app, ["task", "supersede", "S1-P1-a", "S1-P1-b", "-C", root])
    assert held.exit_code == 1
    still = json.loads(runner.invoke(app, ["task", "get", "S1-P1-a", "--json", "-C", root]).stdout)
    assert still["status"] == "IMPLEMENTING" and still["lease"] is not None


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


def test_plan_list_reports_the_state_its_tasks_add_up_to(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from taskmanager.core.status import Status
    from taskmanager.engine.snapshot import roll_up_ancestors

    _seed_estate(tmp_path)
    rows = json.loads(runner.invoke(app, ["plan", "list", "--json", "-C", str(tmp_path)]).stdout)
    assert rows[0]["status"] == "READY" and rows[0]["state"] == "READY"
    node_repo = NodeRepository(DatabaseManager(tmp_path / ".taskmanager"))
    for task_id in ("S1-P1-a", "S1-P1-b"):
        node = node_repo.get_node(task_id)
        assert node is not None
        node.status = Status.COMPLETED
        node_repo.save_node(node)
    roll_up_ancestors(ops_with_code_to_land(tmp_path, monkeypatch), "S1-P1-a")
    rows = json.loads(runner.invoke(app, ["plan", "list", "--json", "-C", str(tmp_path)]).stdout)
    assert rows[0]["status"] == "IMPLEMENTED" and rows[0]["state"] == "WAITING_MERGE"


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
    from taskmanager.core.status import Outcome, Status

    repo = NodeRepository(db)
    importer = BulkImporter(repo)
    importer.import_dict(
        {
            "plans": [
                {"id": "P", "title": "P", "tasks": [{"id": "P-1", "title": "a", "priority": 70}]}
            ]
        }
    )
    node = repo.get_node("P-1")
    assert node is not None
    node.status, node.outcome = Status.REVIEWED, Outcome.APPROVE
    node.acceptable_models = ["claude-sonnet-5"]
    node.frontmatter = {"declared_files": ["web/a"]}
    repo.save_node(node)

    importer.import_dict(
        {"plans": [{"id": "P", "title": "P", "tasks": [{"id": "P-1", "title": "a renamed"}]}]}
    )
    kept = repo.get_node("P-1")
    assert kept is not None
    assert kept.title == "a renamed"
    assert (kept.status, kept.outcome, kept.priority) == (Status.REVIEWED, Outcome.APPROVE, 70)
    assert kept.acceptable_models == ["claude-sonnet-5"]
    assert kept.frontmatter == {"declared_files": ["web/a"]}

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
    assert stated is not None and stated.status == Status.COMPLETED and stated.priority == 20


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


def test_an_import_with_a_key_nothing_reads_is_refused_before_anything_is_written(
    db: DatabaseManager,
) -> None:
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
                        "deferral": "text that would have vanished",
                        "verifications": [{"type": "file_exists", "target_path": "f", "typo": 1}],
                    }
                ],
            }
        ],
        "oops": 1,
    }
    with pytest.raises(ValueError, match="unknown keys") as excinfo:
        importer.import_dict(doc)
    message = str(excinfo.value)
    assert (
        "task P-1: deferral" in message
        and "verification: typo" in message
        and "document: oops" in message
    )
    assert repo.get_node("P") is None


def test_a_specs_state_rolls_up_from_its_plans_the_way_a_plans_does_from_its_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from taskmanager.core.status import Status
    from taskmanager.engine.snapshot import roll_up_ancestors

    root = str(tmp_path)
    runner.invoke(app, ["init", "-C", root])
    runner.invoke(app, ["spec", "add", "S", "--slug", "S1", "-C", root])
    runner.invoke(app, ["plan", "add", "P1", "--spec", "S1", "--slug", "P1", "-C", root])
    runner.invoke(app, ["plan", "add", "P2", "--spec", "S1", "--slug", "P2", "-C", root])
    runner.invoke(app, ["task", "add", "a", "--plan", "S1-P1", "--slug", "a", "-C", root])
    runner.invoke(app, ["task", "add", "b", "--plan", "S1-P2", "--slug", "b", "-C", root])
    node_repo = NodeRepository(DatabaseManager(tmp_path / ".taskmanager"))
    ops = ops_with_code_to_land(tmp_path, monkeypatch)

    def spec_row() -> dict[str, str]:
        rows = json.loads(runner.invoke(app, ["spec", "list", "--json", "-C", root]).stdout)
        return next(r for r in rows if r["id"] == "S1")

    def set_status(node_id: str, status: Status) -> None:
        node = node_repo.get_node(node_id)
        assert node is not None
        node.status = status
        node_repo.save_node(node)

    assert (spec_row()["status"], spec_row()["state"]) == ("READY", "READY")
    set_status("S1-P1-a", Status.IMPLEMENTED)
    assert (spec_row()["status"], spec_row()["state"]) == ("READY", "IMPLEMENTING")
    for task_id in ("S1-P1-a", "S1-P2-b"):
        set_status(task_id, Status.COMPLETED)
        roll_up_ancestors(ops, task_id)
    assert (spec_row()["status"], spec_row()["state"]) == ("READY", "IMPLEMENTING")
    for plan_id in ("S1-P1", "S1-P2"):
        set_status(plan_id, Status.COMPLETED)
    roll_up_ancestors(ops, "S1-P1")
    assert (spec_row()["status"], spec_row()["state"]) == ("IMPLEMENTED", "WAITING_MERGE")
    get_out = runner.invoke(app, ["spec", "get", "S1", "-C", root]).stdout
    assert "Status: IMPLEMENTED" in get_out and "State: WAITING_MERGE" in get_out


def test_render_recursive_walks_spec_to_plans_to_tasks_in_order(tmp_path: Path) -> None:
    root = str(tmp_path)
    runner.invoke(app, ["init", "-C", root])
    runner.invoke(app, ["spec", "add", "S", "--slug", "S1", "-C", root])
    runner.invoke(app, ["plan", "add", "P", "--spec", "S1", "--slug", "P1", "-C", root])
    runner.invoke(app, ["task", "add", "a", "--plan", "S1-P1", "--slug", "a", "-C", root])
    runner.invoke(app, ["task", "add", "b", "--plan", "S1-P1", "--slug", "b", "-C", root])
    runner.invoke(app, ["section", "set", "S1-P1-a:objective", "objective of a", "-C", root])

    flat = runner.invoke(app, ["render", "S1", "-C", root])
    assert flat.exit_code == 0
    assert "S1-P1-a" not in flat.stdout  # a flat render of the spec does not descend

    deep = runner.invoke(app, ["render", "S1", "--recursive", "-C", root])
    assert deep.exit_code == 0
    assert (
        deep.stdout.index("id: S1")
        < deep.stdout.index("id: S1-P1")
        < deep.stdout.index("id: S1-P1-a")
        < deep.stdout.index("id: S1-P1-b")
    )
    assert "objective of a" in deep.stdout
    assert deep.stdout.count("\n\n---\n\n") == 3  # three joins for four rendered nodes


def test_render_recursive_on_a_leaf_task_is_just_its_own_render(tmp_path: Path) -> None:
    root = str(tmp_path)
    _seed_estate(tmp_path)
    leaf = runner.invoke(app, ["render", "S1-P1-a", "-C", root])
    deep = runner.invoke(app, ["render", "S1-P1-a", "--recursive", "-C", root])
    assert leaf.stdout == deep.stdout


def test_render_recursive_refuses_a_section_path(tmp_path: Path) -> None:
    root = str(tmp_path)
    _seed_estate(tmp_path)
    res = runner.invoke(app, ["render", "S1-P1-a:objective", "--recursive", "-C", root])
    assert res.exit_code == 1 and "section" in res.output


def test_render_recursive_survives_a_relation_cycle(tmp_path: Path) -> None:
    from taskmanager.core.enums import RelationType
    from taskmanager.core.models import NodeRelation
    from taskmanager.db.connection import DatabaseManager
    from taskmanager.db.node_repo import NodeRepository

    root = str(tmp_path)
    runner.invoke(app, ["init", "-C", root])
    runner.invoke(app, ["spec", "add", "S", "--slug", "S1", "-C", root])
    runner.invoke(app, ["plan", "add", "P", "--spec", "S1", "--slug", "P1", "-C", root])
    repo = NodeRepository(DatabaseManager(Path(root) / ".taskmanager"))
    repo.add_relation(
        NodeRelation(source_id="S1-P1", target_id="S1", relation_type=RelationType.CONTAINS)
    )
    res = runner.invoke(app, ["render", "S1", "--recursive", "-C", root])
    assert res.exit_code == 0 and "relation cycle" in res.output


def test_render_takes_several_ids_and_joins_them_in_the_order_given(tmp_path: Path) -> None:
    root = str(tmp_path)
    _seed_estate(tmp_path)
    one = runner.invoke(app, ["render", "S1-P1-b", "-C", root])
    two = runner.invoke(app, ["render", "S1-P1-a", "-C", root])
    both = runner.invoke(app, ["render", "S1-P1-b", "S1-P1-a", "-C", root])
    assert both.exit_code == 0
    assert both.stdout == one.stdout[:-1] + "\n\n---\n\n" + two.stdout
    assert both.stdout.index("id: S1-P1-b") < both.stdout.index("id: S1-P1-a")


def test_render_with_several_ids_refuses_at_the_first_unknown_one(tmp_path: Path) -> None:
    root = str(tmp_path)
    _seed_estate(tmp_path)
    res = runner.invoke(app, ["render", "S1-P1-a", "NOPE", "-C", root])
    assert res.exit_code == 1 and "Traceback" not in res.output


def test_task_list_render_renders_every_listed_task_instead_of_a_table(tmp_path: Path) -> None:
    root = str(tmp_path)
    _seed_estate(tmp_path)
    rendered = runner.invoke(app, ["task", "list", "--render", "summary", "-C", root])
    assert rendered.exit_code == 0
    assert "id: S1-P1-a" in rendered.stdout and "id: S1-P1-b" in rendered.stdout
    assert "Tasks" not in rendered.stdout  # not the table title
    assert rendered.stdout.count("\n\n---\n\n") == 1  # two tasks, one join

    scoped = runner.invoke(
        app,
        [
            "task",
            "list",
            "--plan",
            "S1-P1",
            "--render",
            "full",
            "-C",
            root,
        ],
    )
    assert scoped.exit_code == 0
    assert "id: S1-P1-a" in scoped.stdout and "id: S1-P1-b" in scoped.stdout
