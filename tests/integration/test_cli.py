import json
from pathlib import Path

from typer.testing import CliRunner

from taskmanager.cli.main import app

runner = CliRunner()


def test_cli_lifecycle_spec_plan_task_render_next(tmp_path: Path) -> None:
    # 1. init
    res = runner.invoke(app, ["init", "--path", str(tmp_path)])
    assert res.exit_code == 0
    assert "Initialized .taskmanager" in res.stdout
    assert (tmp_path / ".taskmanager" / "spec.db").exists()
    assert (tmp_path / ".taskmanager" / "runtime.db").exists()
    assert (tmp_path / ".taskmanager" / "ledger.db").exists()

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
            "--require-review",
            "--path",
            str(tmp_path),
        ],
    )
    assert res.exit_code == 0
    assert "review gate" in res.stdout.lower()

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


def test_cli_execution_leases_and_runtime(tmp_path: Path) -> None:
    runner.invoke(app, ["init", "--path", str(tmp_path)])
    runner.invoke(app, ["spec", "add", "Core Spec", "--slug", "CORE", "--path", str(tmp_path)])
    runner.invoke(
        app, ["plan", "add", "Core Plan", "--spec", "CORE", "--slug", "P1", "--path", str(tmp_path)]
    )
    runner.invoke(
        app,
        ["task", "add", "Task Run", "--plan", "CORE-P1", "--slug", "RUN1", "--path", str(tmp_path)],
    )

    # run start
    res = runner.invoke(
        app,
        [
            "run",
            "start",
            "CORE-P1-RUN1",
            "--agent",
            "agent-alpha",
            "--session",
            "sess-123",
            "--path",
            str(tmp_path),
        ],
    )
    assert res.exit_code == 0
    assert "Started task CORE-P1-RUN1" in res.stdout

    # run heartbeat
    res = runner.invoke(
        app,
        ["run", "heartbeat", "CORE-P1-RUN1", "--path", str(tmp_path)],
    )
    assert res.exit_code == 0
    assert "Heartbeat recorded" in res.stdout

    # run list
    res = runner.invoke(app, ["run", "list", "--path", str(tmp_path)])
    assert res.exit_code == 0
    assert "CORE-P1-RUN1" in res.stdout
    assert "agent-alpha" in res.stdout

    # run stop
    res = runner.invoke(
        app,
        ["run", "stop", "CORE-P1-RUN1", "--status", "WAITING_REVIEW", "--path", str(tmp_path)],
    )
    assert res.exit_code == 0
    assert "Stopped task CORE-P1-RUN1" in res.stdout

    # run sweep
    res = runner.invoke(app, ["run", "sweep", "--path", str(tmp_path)])
    assert res.exit_code == 0


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


def test_cli_task_depends_gated_edge(tmp_path: Path) -> None:
    runner.invoke(app, ["init", "--path", str(tmp_path)])
    runner.invoke(app, ["spec", "add", "Gate Spec", "--slug", "GAT", "--path", str(tmp_path)])
    runner.invoke(
        app, ["plan", "add", "Gate Plan", "--spec", "GAT", "--slug", "P1", "--path", str(tmp_path)]
    )
    runner.invoke(
        app,
        ["task", "add", "Implement", "--plan", "GAT-P1", "--slug", "IMPL", "--path", str(tmp_path)],
    )
    runner.invoke(
        app,
        ["task", "add", "Review", "--plan", "GAT-P1", "--slug", "REV", "--path", str(tmp_path)],
    )

    res = runner.invoke(
        app,
        [
            "task",
            "depends",
            "GAT-P1-REV",
            "--add",
            "GAT-P1-IMPL:WAITING_REVIEW",
            "--path",
            str(tmp_path),
        ],
    )
    assert res.exit_code == 0
    assert "GAT-P1-IMPL:WAITING_REVIEW" in res.stdout

    doc = json.loads(
        runner.invoke(app, ["task", "get", "GAT-P1-REV", "--json", "--path", str(tmp_path)]).stdout
    )
    assert doc["state"] == "BLOCKED"
    assert doc["depends_on"] == [
        {"id": "GAT-P1-IMPL", "status": "NOT_STARTED", "gate": "WAITING_REVIEW"}
    ]

    runner.invoke(
        app,
        [
            "run",
            "start",
            "GAT-P1-IMPL",
            "--agent",
            "agent-a",
            "--session",
            "sess-a",
            "--path",
            str(tmp_path),
        ],
    )
    runner.invoke(
        app,
        [
            "run",
            "stop",
            "GAT-P1-IMPL",
            "--status",
            "WAITING_REVIEW",
            "--path",
            str(tmp_path),
        ],
    )

    doc = json.loads(
        runner.invoke(app, ["task", "get", "GAT-P1-REV", "--json", "--path", str(tmp_path)]).stdout
    )
    assert doc["state"] == "READY"

    res = runner.invoke(
        app,
        [
            "task",
            "depends",
            "GAT-P1-REV",
            "--add",
            "GAT-P1-IMPL:NOT_A_STATUS",
            "--path",
            str(tmp_path),
        ],
    )
    assert res.exit_code != 0


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


def test_cli_run_release_drops_the_lease_without_changing_status(tmp_path: Path) -> None:
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
            "run",
            "start",
            "S1-P1-t1",
            "--agent",
            "agent-a",
            "--session",
            "sess-a",
            "--path",
            str(tmp_path),
        ],
    )

    res = runner.invoke(app, ["run", "release", "S1-P1-t1", "--path", str(tmp_path)])
    assert res.exit_code == 0
    assert "Released lease for S1-P1-t1" in res.stdout

    res = runner.invoke(app, ["run", "list", "--path", str(tmp_path)])
    assert "S1-P1-t1" not in res.stdout

    doc = json.loads(
        runner.invoke(app, ["task", "get", "S1-P1-t1", "--json", "--path", str(tmp_path)]).stdout
    )
    assert doc["status"] == "IMPLEMENTING"
