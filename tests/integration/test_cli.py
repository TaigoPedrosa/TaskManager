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
