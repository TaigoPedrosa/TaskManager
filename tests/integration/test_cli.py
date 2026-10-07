import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from taskmanager.cli.main import app
from taskmanager.di.container import create_container
from taskmanager.engine.operations import Operations

runner = CliRunner()


def test_cli_lifecycle_spec_plan_task_render_next(tmp_path: Path) -> None:
    # 1. init
    res = runner.invoke(app, ["init", "--path", str(tmp_path)])
    assert res.exit_code == 0
    assert "Initialized .taskmanager" in res.stdout
    assert (tmp_path / ".taskmanager" / "spec.db").exists()
    assert (tmp_path / ".taskmanager" / "runtime.db").exists()
    assert (tmp_path / ".taskmanager" / "ledger.db").exists()
    assert (tmp_path / ".taskmanager" / "audit.db").exists()

    # 2. spec add, list, get
    res = runner.invoke(
        app,
        ["spec", "add", "Auth Spec", "--slug", "AUTH", "--priority", "80", "--path", str(tmp_path)],
    )
    assert res.exit_code == 0
    assert "Added spec AUTH" in res.stdout

    res = runner.invoke(app, ["spec", "list", "--path", str(tmp_path)])
    assert res.exit_code == 0
    assert "AUTH" in res.stdout
    assert "Auth Spec" in res.stdout

    res = runner.invoke(app, ["spec", "get", "AUTH", "--path", str(tmp_path)])
    assert res.exit_code == 0
    assert "Auth Spec" in res.stdout

    # 3. plan add, list, get, and require-review
    res = runner.invoke(
        app,
        [
            "plan",
            "add",
            "User Plan",
            "--spec",
            "AUTH",
            "--slug",
            "USER",
            "--priority",
            "75",
            "--path",
            str(tmp_path),
        ],
    )
    assert res.exit_code == 0
    assert "Added plan AUTH-USER" in res.stdout

    res = runner.invoke(app, ["plan", "list", "--spec", "AUTH", "--path", str(tmp_path)])
    assert res.exit_code == 0
    assert "AUTH-USER" in res.stdout

    res = runner.invoke(app, ["plan", "get", "AUTH-USER", "--path", str(tmp_path)])
    assert res.exit_code == 0
    assert "User Plan" in res.stdout

    res = runner.invoke(
        app,
        [
            "plan",
            "add",
            "Review Plan",
            "--spec",
            "AUTH",
            "--slug",
            "REVPLAN",
            "--review",
            "--fix",
            "--path",
            str(tmp_path),
        ],
    )
    assert res.exit_code == 0
    assert "Added plan AUTH-REVPLAN" in res.stdout

    # 4. task add, list, get
    res = runner.invoke(
        app,
        [
            "task",
            "add",
            "Login Endpoint",
            "--plan",
            "AUTH-USER",
            "--slug",
            "LOGIN",
            "--priority",
            "90",
            "--models",
            "sonnet,haiku",
            "--path",
            str(tmp_path),
        ],
    )
    assert res.exit_code == 0
    assert "Added task AUTH-USER-LOGIN" in res.stdout

    res = runner.invoke(app, ["task", "list", "--plan", "AUTH-USER", "--path", str(tmp_path)])
    assert res.exit_code == 0
    assert "AUTH-USER-LOGIN" in res.stdout

    res = runner.invoke(app, ["task", "get", "AUTH-USER-LOGIN", "--path", str(tmp_path)])
    assert res.exit_code == 0
    assert "Login Endpoint" in res.stdout
    assert "sonnet" in res.stdout

    # 5. section set and get with qualified path
    res = runner.invoke(
        app,
        [
            "section",
            "set",
            "AUTH-USER-LOGIN:steps",
            "--content",
            "- [ ] Implement JWT verification",
            "--path",
            str(tmp_path),
        ],
    )
    assert res.exit_code == 0
    assert "Saved section AUTH-USER-LOGIN:steps" in res.stdout

    res = runner.invoke(
        app,
        ["section", "get", "AUTH-USER-LOGIN:steps", "--path", str(tmp_path)],
    )
    assert res.exit_code == 0
    assert "Implement JWT verification" in res.stdout

    # 6. render views: summary, subagent, full
    res = runner.invoke(
        app,
        ["render", "AUTH-USER-LOGIN", "--view", "summary", "--path", str(tmp_path)],
    )
    assert res.exit_code == 0
    assert "id: AUTH-USER-LOGIN" in res.stdout

    res = runner.invoke(
        app,
        ["render", "AUTH-USER-LOGIN", "--view", "subagent", "--path", str(tmp_path)],
    )
    assert res.exit_code == 0
    assert "Implement JWT verification" in res.stdout

    res = runner.invoke(
        app,
        ["render", "AUTH-USER-LOGIN", "--view", "full", "--path", str(tmp_path)],
    )
    assert res.exit_code == 0
    assert "id: AUTH-USER-LOGIN" in res.stdout
    assert "Login Endpoint" in res.stdout

    # 7. next tasks recommendations (table and json)
    res = runner.invoke(app, ["next", "--path", str(tmp_path)])
    assert res.exit_code == 0
    assert "AUTH-USER-LOGIN" in res.stdout

    res = runner.invoke(app, ["next", "--json", "--path", str(tmp_path)])
    assert res.exit_code == 0
    data = json.loads(res.stdout)
    assert any(t["task_id"] == "AUTH-USER-LOGIN" for t in data)


def test_task_list_and_next_take_a_spec_filter(tmp_path: Path) -> None:
    runner.invoke(app, ["init", "--path", str(tmp_path)])
    runner.invoke(app, ["spec", "add", "Spec A", "--slug", "SPECA", "--path", str(tmp_path)])
    runner.invoke(app, ["spec", "add", "Spec B", "--slug", "SPECB", "--path", str(tmp_path)])
    runner.invoke(
        app,
        ["plan", "add", "A Plan", "--spec", "SPECA", "--slug", "PLANA", "--path", str(tmp_path)],
    )
    runner.invoke(
        app,
        ["plan", "add", "B Plan", "--spec", "SPECB", "--slug", "PLANB", "--path", str(tmp_path)],
    )
    runner.invoke(
        app,
        ["task", "add", "A Task", "--plan", "SPECA-PLANA", "--slug", "T1", "--path", str(tmp_path)],
    )
    runner.invoke(
        app,
        ["task", "add", "B Task", "--plan", "SPECB-PLANB", "--slug", "T1", "--path", str(tmp_path)],
    )

    res = runner.invoke(app, ["task", "list", "--spec", "SPECA", "--path", str(tmp_path)])
    assert res.exit_code == 0
    assert "SPECA-PLANA-T1" in res.stdout
    assert "SPECB-PLANB-T1" not in res.stdout

    res = runner.invoke(app, ["task", "list", "--spec", "SPECA", "--json", "--path", str(tmp_path)])
    assert res.exit_code == 0
    data = json.loads(res.stdout)
    assert [t["id"] for t in data] == ["SPECA-PLANA-T1"]

    res = runner.invoke(app, ["next", "--spec", "SPECB", "--json", "--path", str(tmp_path)])
    assert res.exit_code == 0
    data = json.loads(res.stdout)
    assert [t["task_id"] for t in data] == ["SPECB-PLANB-T1"]

    res = runner.invoke(app, ["task", "get", "SPECA-PLANA-T1", "--yaml", "--path", str(tmp_path)])
    assert res.exit_code == 0
    assert "spec_id: SPECA" in res.stdout


def test_spec_filter_walks_a_plan_nested_under_another_plan(tmp_path: Path) -> None:
    """`plan add --spec <id>` never checks that `<id>` is a spec, so a plan nested under another
    plan (its own `--spec` pointed at a plan id) is real data, not a fixture contrivance."""
    runner.invoke(app, ["init", "--path", str(tmp_path)])
    runner.invoke(app, ["spec", "add", "Spec A", "--slug", "SPECA", "--path", str(tmp_path)])
    runner.invoke(
        app,
        ["plan", "add", "Outer", "--spec", "SPECA", "--slug", "OUTER", "--path", str(tmp_path)],
    )
    runner.invoke(
        app,
        [
            "plan",
            "add",
            "Inner",
            "--spec",
            "SPECA-OUTER",
            "--slug",
            "INNER",
            "--path",
            str(tmp_path),
        ],
    )
    runner.invoke(
        app,
        [
            "task",
            "add",
            "Nested Task",
            "--plan",
            "SPECA-OUTER-INNER",
            "--slug",
            "T1",
            "--path",
            str(tmp_path),
        ],
    )

    res = runner.invoke(
        app, ["task", "get", "SPECA-OUTER-INNER-T1", "--yaml", "--path", str(tmp_path)]
    )
    assert res.exit_code == 0
    assert "spec_id: SPECA" in res.stdout

    res = runner.invoke(app, ["next", "--spec", "SPECA", "--json", "--path", str(tmp_path)])
    assert res.exit_code == 0
    data = json.loads(res.stdout)
    assert [t["task_id"] for t in data] == ["SPECA-OUTER-INNER-T1"]

    res = runner.invoke(app, ["task", "list", "--spec", "none", "--json", "--path", str(tmp_path)])
    assert res.exit_code == 0
    assert json.loads(res.stdout) == []


def test_cli_task_supersede(tmp_path: Path) -> None:
    runner.invoke(app, ["init", "--path", str(tmp_path)])
    runner.invoke(app, ["spec", "add", "Spec A", "--slug", "SPA", "--path", str(tmp_path)])
    runner.invoke(
        app, ["plan", "add", "Plan A", "--spec", "SPA", "--slug", "PLA", "--path", str(tmp_path)]
    )
    runner.invoke(
        app,
        ["task", "add", "Old Task", "--plan", "SPA-PLA", "--slug", "OLD", "--path", str(tmp_path)],
    )
    runner.invoke(
        app,
        ["task", "add", "New Task", "--plan", "SPA-PLA", "--slug", "NEW", "--path", str(tmp_path)],
    )

    res = runner.invoke(
        app,
        ["task", "supersede", "SPA-PLA-OLD", "SPA-PLA-NEW", "--path", str(tmp_path)],
    )
    assert res.exit_code == 0
    assert "superseded by" in res.stdout

    res = runner.invoke(app, ["task", "get", "SPA-PLA-OLD", "--path", str(tmp_path)])
    assert res.exit_code == 0
    assert "SUPERSEDED" in res.stdout


def test_cli_task_depends_takes_bare_ids_and_refuses_a_status_gate(tmp_path: Path) -> None:
    runner.invoke(app, ["init", "-C", str(tmp_path)])
    runner.invoke(app, ["spec", "add", "Gate Spec", "--slug", "GAT", "-C", str(tmp_path)])
    runner.invoke(
        app, ["plan", "add", "Gate Plan", "--spec", "GAT", "--slug", "P1", "-C", str(tmp_path)]
    )
    for slug in ("IMPL", "REV"):
        runner.invoke(
            app, ["task", "add", slug, "--plan", "GAT-P1", "--slug", slug, "-C", str(tmp_path)]
        )

    gated = runner.invoke(
        app,
        ["task", "depends", "GAT-P1-REV", "--add", "GAT-P1-IMPL:REVIEWED", "-C", str(tmp_path)],
    )
    assert gated.exit_code != 0
    assert "bare id" in gated.output

    res = runner.invoke(
        app, ["task", "depends", "GAT-P1-REV", "--add", "GAT-P1-IMPL", "-C", str(tmp_path)]
    )
    assert res.exit_code == 0, res.output
    assert "GAT-P1-REV depends on: GAT-P1-IMPL" in res.stdout


def test_cli_verification_and_audit(tmp_path: Path) -> None:
    runner.invoke(app, ["init", "--path", str(tmp_path)])
    runner.invoke(app, ["spec", "add", "Spec V", "--slug", "SPV", "--path", str(tmp_path)])
    runner.invoke(
        app, ["plan", "add", "Plan V", "--spec", "SPV", "--slug", "PLV", "--path", str(tmp_path)]
    )
    runner.invoke(
        app, ["task", "add", "Task V", "--plan", "SPV-PLV", "--slug", "TV", "--path", str(tmp_path)]
    )

    # create a file for file_exists verification
    target_file = tmp_path / "output.txt"
    target_file.write_text("ok", encoding="utf-8")

    res = runner.invoke(
        app,
        [
            "verify",
            "add",
            "SPV-PLV-TV",
            "--type",
            "file_exists",
            "--target",
            "output.txt",
            "--path",
            str(tmp_path),
        ],
    )
    assert res.exit_code == 0
    assert "Added file_exists verification" in res.stdout

    res = runner.invoke(
        app,
        ["verify", "run", "SPV-PLV-TV", "--path", str(tmp_path)],
    )
    assert res.exit_code == 0
    assert "PASSED" in res.stdout

    # test audit list
    res = runner.invoke(app, ["audit", "list", "--path", str(tmp_path)])
    assert res.exit_code == 0
    assert "init" in res.stdout or "spec add" in res.stdout


def test_cli_verify_run_reads_a_branch_ref_before_merge(tmp_path: Path) -> None:
    runner.invoke(app, ["init", "--path", str(tmp_path)])
    runner.invoke(app, ["spec", "add", "Spec R", "--slug", "SPR", "--path", str(tmp_path)])
    runner.invoke(
        app, ["plan", "add", "Plan R", "--spec", "SPR", "--slug", "PLR", "--path", str(tmp_path)]
    )
    runner.invoke(
        app, ["task", "add", "Task R", "--plan", "SPR-PLR", "--slug", "TR", "--path", str(tmp_path)]
    )
    runner.invoke(
        app, ["task", "update", "SPR-PLR-TR", "--repo", "myrepo", "--path", str(tmp_path)]
    )

    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", str(origin)], capture_output=True, text=True, check=True
    )
    repo = tmp_path / "myrepo"
    repo.mkdir()
    subprocess.run(
        ["git", "-C", str(repo), "init", "-q", "-b", "main"],
        capture_output=True,
        text=True,
        check=True,
    )
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", str(origin)], check=True)
    (repo / "README.md").write_text("root\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "init"], check=True)
    subprocess.run(["git", "-C", str(repo), "push", "-q", "-u", "origin", "main"], check=True)
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "tm/SPR-PLR-TR"], check=True)
    (repo / "delivered.py").write_text("def deliver():\n    pass\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "delivered.py"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "deliver"], check=True)

    runner.invoke(
        app,
        [
            "verify",
            "add",
            "SPR-PLR-TR",
            "--type",
            "file_exists",
            "--target",
            "delivered.py",
            "--path",
            str(tmp_path),
        ],
    )

    unmerged = runner.invoke(app, ["verify", "run", "SPR-PLR-TR", "--path", str(tmp_path)])
    assert unmerged.exit_code == 1
    assert "FAILED" in unmerged.stdout

    on_branch = runner.invoke(
        app,
        [
            "verify",
            "run",
            "SPR-PLR-TR",
            "--ref",
            "tm/SPR-PLR-TR",
            "--path",
            str(tmp_path),
        ],
    )
    assert on_branch.exit_code == 0
    assert "PASSED" in on_branch.stdout


def test_cli_import_hierarchy(tmp_path: Path) -> None:
    runner.invoke(app, ["init", "--path", str(tmp_path)])

    hierarchy = {
        "spec": {"id": "IMP-SPEC", "title": "Imported Spec", "priority": 70},
        "plans": [
            {
                "id": "IMP-SPEC-PLAN1",
                "title": "Imported Plan 1",
                "priority": 60,
                "tasks": [
                    {
                        "id": "IMP-SPEC-PLAN1-T1",
                        "title": "Imported Task 1",
                        "priority": 85,
                        "acceptable_models": ["sonnet"],
                        "sections": [
                            {
                                "section_key": "steps",
                                "header": "## Steps",
                                "content": "- [ ] Step A\n- [ ] Step B",
                            }
                        ],
                    }
                ],
            }
        ],
    }
    json_path = tmp_path / "hierarchy.json"
    json_path.write_text(json.dumps(hierarchy), encoding="utf-8")

    res = runner.invoke(
        app,
        ["import", "--format", "json", "--file", str(json_path), "--path", str(tmp_path)],
    )
    assert res.exit_code == 0
    assert "Successfully imported data" in res.stdout

    # verify imported elements
    res = runner.invoke(app, ["spec", "get", "IMP-SPEC", "--path", str(tmp_path)])
    assert res.exit_code == 0
    assert "Imported Spec" in res.stdout

    res = runner.invoke(app, ["plan", "get", "IMP-SPEC-PLAN1", "--path", str(tmp_path)])
    assert res.exit_code == 0
    assert "Imported Plan 1" in res.stdout

    res = runner.invoke(app, ["task", "get", "IMP-SPEC-PLAN1-T1", "--path", str(tmp_path)])
    assert res.exit_code == 0
    assert "Imported Task 1" in res.stdout

    res = runner.invoke(
        app,
        ["section", "get", "IMP-SPEC-PLAN1-T1:steps", "--path", str(tmp_path)],
    )
    assert res.exit_code == 0
    assert "Step A" in res.stdout


def test_cli_install_command(tmp_path: Path) -> None:
    res_help = runner.invoke(app, ["install", "--help"])
    assert res_help.exit_code == 0
    assert "Install TaskManager globally" in res_help.stdout

    res_status = runner.invoke(app, ["install", "--status", "--path", str(tmp_path)])
    assert res_status.exit_code == 0


def test_cli_task_update_unset_removes_a_frontmatter_key(tmp_path: Path) -> None:
    runner.invoke(app, ["init", "--path", str(tmp_path)])
    runner.invoke(app, ["spec", "add", "S", "--slug", "S1", "--path", str(tmp_path)])
    runner.invoke(
        app, ["plan", "add", "P", "--spec", "S1", "--slug", "P1", "--path", str(tmp_path)]
    )
    runner.invoke(
        app, ["task", "add", "T", "--plan", "S1-P1", "--slug", "t1", "--path", str(tmp_path)]
    )
    runner.invoke(
        app,
        ["task", "update", "S1-P1-t1", "--set", 'declared_files=["a"]', "--path", str(tmp_path)],
    )

    res = runner.invoke(
        app,
        ["task", "update", "S1-P1-t1", "--unset", "declared_files", "--path", str(tmp_path)],
    )
    assert res.exit_code == 0
    assert "Updated S1-P1-t1" in res.stdout

    doc = json.loads(
        runner.invoke(app, ["task", "get", "S1-P1-t1", "--json", "--path", str(tmp_path)]).stdout
    )
    assert "declared_files" not in doc["frontmatter"]


def test_cli_task_move_reparents_to_another_plan(tmp_path: Path) -> None:
    runner.invoke(app, ["init", "--path", str(tmp_path)])
    runner.invoke(app, ["spec", "add", "S", "--slug", "S1", "--path", str(tmp_path)])
    runner.invoke(
        app, ["plan", "add", "P1", "--spec", "S1", "--slug", "P1", "--path", str(tmp_path)]
    )
    runner.invoke(
        app, ["plan", "add", "P2", "--spec", "S1", "--slug", "P2", "--path", str(tmp_path)]
    )
    runner.invoke(
        app, ["task", "add", "T", "--plan", "S1-P1", "--slug", "t1", "--path", str(tmp_path)]
    )

    res = runner.invoke(
        app, ["task", "move", "S1-P1-t1", "--plan", "S1-P2", "--path", str(tmp_path)]
    )
    assert res.exit_code == 0
    assert "Moved S1-P1-t1 to S1-P2" in res.stdout

    res = runner.invoke(app, ["plan", "get", "S1-P1", "--path", str(tmp_path)])
    assert "S1-P1-t1" not in res.stdout
    res = runner.invoke(app, ["plan", "get", "S1-P2", "--path", str(tmp_path)])
    assert "S1-P1-t1" in res.stdout

    res = runner.invoke(
        app, ["task", "move", "S1-P1-t1", "--plan", "NOPE", "--path", str(tmp_path)]
    )
    assert res.exit_code == 1
    assert "not found" in res.stdout


def test_cli_section_remove_deletes_a_section(tmp_path: Path) -> None:
    runner.invoke(app, ["init", "--path", str(tmp_path)])
    runner.invoke(app, ["spec", "add", "S", "--slug", "S1", "--path", str(tmp_path)])
    runner.invoke(
        app, ["plan", "add", "P", "--spec", "S1", "--slug", "P1", "--path", str(tmp_path)]
    )
    runner.invoke(
        app, ["task", "add", "T", "--plan", "S1-P1", "--slug", "t1", "--path", str(tmp_path)]
    )
    runner.invoke(app, ["section", "set", "S1-P1-t1:steps", "content", "--path", str(tmp_path)])

    res = runner.invoke(app, ["section", "remove", "S1-P1-t1:steps", "--path", str(tmp_path)])
    assert res.exit_code == 0
    assert "Removed section S1-P1-t1:steps" in res.stdout

    res = runner.invoke(app, ["section", "get", "S1-P1-t1:steps", "--path", str(tmp_path)])
    assert res.exit_code == 1

    res = runner.invoke(app, ["section", "remove", "S1-P1-t1:steps", "--path", str(tmp_path)])
    assert res.exit_code == 1
    assert "No section" in res.stdout


def test_cli_section_get_then_set_round_trips_content_byte_identical(tmp_path: Path) -> None:
    """`tm section get` puts its header on stderr so a caller capturing only stdout and
    feeding it straight into `set -f` never folds the header into the content."""
    runner.invoke(app, ["init", "--path", str(tmp_path)])
    runner.invoke(app, ["spec", "add", "S", "--slug", "S1", "--path", str(tmp_path)])
    runner.invoke(
        app, ["plan", "add", "P", "--spec", "S1", "--slug", "P1", "--path", str(tmp_path)]
    )
    runner.invoke(
        app, ["task", "add", "T", "--plan", "S1-P1", "--slug", "t1", "--path", str(tmp_path)]
    )
    runner.invoke(
        app,
        [
            "section",
            "set",
            "S1-P1-t1:report",
            "--content",
            "line one\nline two",
            "--header",
            "## Report",
            "--path",
            str(tmp_path),
        ],
    )

    got = runner.invoke(app, ["section", "get", "S1-P1-t1:report", "--path", str(tmp_path)])
    assert got.exit_code == 0
    assert got.stdout == "line one\nline two"
    assert got.stderr == "## Report\n"

    reread_file = tmp_path / "reread.txt"
    for _ in range(3):
        reread_file.write_text(got.stdout, encoding="utf-8")
        runner.invoke(
            app,
            [
                "section",
                "set",
                "S1-P1-t1:report",
                "--file",
                str(reread_file),
                "--path",
                str(tmp_path),
            ],
        )
        got = runner.invoke(app, ["section", "get", "S1-P1-t1:report", "--path", str(tmp_path)])
        assert got.exit_code == 0
        assert got.stdout == "line one\nline two"


def test_cli_section_get_piped_into_set_from_stdin_leaves_a_custom_header_and_content_unchanged(
    tmp_path: Path,
) -> None:
    root = ["--path", str(tmp_path)]
    for args in (
        ["init"],
        ["spec", "add", "S", "--slug", "S1"],
        ["plan", "add", "P", "--spec", "S1", "--slug", "P1"],
        ["task", "add", "T", "--plan", "S1-P1", "--slug", "t1"],
        ["section", "set", "S1-P1-t1:figma", "--content", "frame — decisão\n", "-h", "## Frames"],
    ):
        assert runner.invoke(app, [*args, *root]).exit_code == 0, args
    before = runner.invoke(app, ["section", "get", "S1-P1-t1:figma", *root])

    piped = runner.invoke(
        app, ["section", "set", "S1-P1-t1:figma", "--file", "-", *root], input=before.stdout
    )
    after = runner.invoke(app, ["section", "get", "S1-P1-t1:figma", *root])

    assert piped.exit_code == 0, piped.output
    assert (after.stdout, after.stderr) == ("frame — decisão\n", "## Frames\n")
    assert (after.stdout, after.stderr) == (before.stdout, before.stderr)


def test_spec_and_plan_list_and_get_show_the_display_beside_the_stored_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from taskmanager.core.status import Status
    from taskmanager.db.connection import DatabaseManager
    from taskmanager.db.node_repo import NodeRepository
    from taskmanager.engine.snapshot import roll_up_ancestors

    runner.invoke(app, ["init", "--path", str(tmp_path)])
    runner.invoke(app, ["spec", "add", "S", "--slug", "S1", "--path", str(tmp_path)])
    runner.invoke(
        app, ["plan", "add", "P", "--spec", "S1", "--slug", "P1", "--path", str(tmp_path)]
    )
    runner.invoke(
        app, ["task", "add", "T", "--plan", "S1-P1", "--slug", "t1", "--path", str(tmp_path)]
    )
    node_repo = NodeRepository(DatabaseManager(tmp_path / ".taskmanager"))
    task = node_repo.get_node("S1-P1-t1")
    assert task is not None
    task.status = Status.COMPLETED
    node_repo.save_node(task)
    ops = create_container(tmp_path).get(Operations)
    # No repository here for the git check to read: the plan's code counts as still to land.
    monkeypatch.setattr(ops, "nothing_to_land", lambda _container: False)
    roll_up_ancestors(ops, "S1-P1-t1")

    res = runner.invoke(app, ["plan", "list", "--spec", "S1", "--path", str(tmp_path)])
    assert res.exit_code == 0 and "WAITING_MERGE" in res.stdout
    res = runner.invoke(app, ["plan", "get", "S1-P1", "--path", str(tmp_path)])
    assert "Status: IMPLEMENTED" in res.stdout and "State: WAITING_MERGE" in res.stdout
    res = runner.invoke(app, ["spec", "list", "--path", str(tmp_path)])
    assert res.exit_code == 0 and "IMPLEMENTING" in res.stdout
    res = runner.invoke(app, ["spec", "get", "S1", "--path", str(tmp_path)])
    assert "Status: READY" in res.stdout and "State: IMPLEMENTING" in res.stdout


def test_plan_list_filters_by_stored_parent_not_id_prefix(tmp_path: Path) -> None:
    from taskmanager.core.enums import RelationType
    from taskmanager.core.models import NodeRelation
    from taskmanager.db.connection import DatabaseManager
    from taskmanager.db.node_repo import NodeRepository

    runner.invoke(app, ["init", "--path", str(tmp_path)])
    runner.invoke(app, ["spec", "add", "S", "--slug", "S1", "--path", str(tmp_path)])
    runner.invoke(app, ["spec", "add", "S", "--slug", "S2", "--path", str(tmp_path)])
    runner.invoke(
        app, ["plan", "add", "Orphan", "--spec", "S1", "--slug", "ORPHAN", "--path", str(tmp_path)]
    )
    runner.invoke(
        app, ["plan", "add", "Foster", "--spec", "S2", "--slug", "FOSTER", "--path", str(tmp_path)]
    )

    db = DatabaseManager(tmp_path / ".taskmanager")
    node_repo = NodeRepository(db)
    # S1-ORPHAN shares S1's id prefix but S1 no longer contains it.
    node_repo.remove_relation("S1", "S1-ORPHAN", RelationType.CONTAINS)
    # S2-FOSTER is a real child of S1 despite an id prefixed for S2.
    node_repo.add_relation(
        NodeRelation(source_id="S1", target_id="S2-FOSTER", relation_type=RelationType.CONTAINS)
    )

    res = runner.invoke(app, ["plan", "list", "--spec", "S1", "--path", str(tmp_path)])
    assert res.exit_code == 0
    assert "S2-FOSTER" in res.stdout
    assert "S1-ORPHAN" not in res.stdout


def test_task_list_filters_by_stored_parent_not_id_prefix(tmp_path: Path) -> None:
    from taskmanager.core.enums import RelationType
    from taskmanager.core.models import NodeRelation
    from taskmanager.db.connection import DatabaseManager
    from taskmanager.db.node_repo import NodeRepository

    runner.invoke(app, ["init", "--path", str(tmp_path)])
    runner.invoke(app, ["spec", "add", "S", "--slug", "S1", "--path", str(tmp_path)])
    runner.invoke(
        app, ["plan", "add", "P1", "--spec", "S1", "--slug", "P1", "--path", str(tmp_path)]
    )
    runner.invoke(
        app, ["plan", "add", "P2", "--spec", "S1", "--slug", "P2", "--path", str(tmp_path)]
    )
    runner.invoke(
        app,
        ["task", "add", "Orphan", "--plan", "S1-P1", "--slug", "ORPHAN", "--path", str(tmp_path)],
    )
    runner.invoke(
        app,
        ["task", "add", "Foster", "--plan", "S1-P2", "--slug", "FOSTER", "--path", str(tmp_path)],
    )

    db = DatabaseManager(tmp_path / ".taskmanager")
    node_repo = NodeRepository(db)
    # S1-P1-ORPHAN shares S1-P1's id prefix but S1-P1 no longer contains it.
    node_repo.remove_relation("S1-P1", "S1-P1-ORPHAN", RelationType.CONTAINS)
    # S1-P2-FOSTER is a real child of S1-P1 despite an id prefixed for S1-P2.
    node_repo.add_relation(
        NodeRelation(
            source_id="S1-P1", target_id="S1-P2-FOSTER", relation_type=RelationType.CONTAINS
        )
    )

    res = runner.invoke(app, ["task", "list", "--plan", "S1-P1", "--path", str(tmp_path)])
    assert res.exit_code == 0
    assert "S1-P2-FOSTER" in res.stdout
    assert "S1-P1-ORPHAN" not in res.stdout
