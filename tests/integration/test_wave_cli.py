import json
from pathlib import Path

from typer.testing import CliRunner

from taskmanager.cli.main import app
from taskmanager.engine.discovery import djb2

runner = CliRunner()


def test_wave_discover_prints_payload_then_a_matching_check_line(tmp_path: Path) -> None:
    runner.invoke(app, ["init", "--path", str(tmp_path)])
    runner.invoke(app, ["spec", "add", "Auth Spec", "--slug", "AUTH", "--path", str(tmp_path)])
    runner.invoke(
        app,
        ["plan", "add", "User Plan", "--spec", "AUTH", "--slug", "USER", "--path", str(tmp_path)],
    )
    runner.invoke(
        app,
        [
            "task",
            "add",
            "Add login",
            "--plan",
            "AUTH-USER",
            "--slug",
            "T1",
            "--path",
            str(tmp_path),
        ],
    )
    # Claimable as `implement` needs a target repository: that's what routes its worktree.
    runner.invoke(
        app, ["task", "update", "AUTH-USER-T1", "--repo", "core", "--path", str(tmp_path)]
    )

    res = runner.invoke(
        app,
        [
            "wave",
            "discover",
            "--spec",
            "AUTH",
            "--session",
            "sess-1",
            "--slots",
            "5",
            "--max-strong",
            "2",
            "--path",
            str(tmp_path),
        ],
    )

    assert res.exit_code == 0, res.stdout
    lines = res.stdout.rstrip("\n").split("\n")
    assert len(lines) == 2
    payload, check = lines
    match = check.removeprefix("__CHECK n=")
    n_str, h_str = match.split(" h=")

    data = json.loads(payload)
    assert [c["id"] for c in data["chosen"]] == ["AUTH-USER-T1"]
    assert int(n_str) == len(data["chosen"])
    assert int(h_str) == djb2(payload)


def test_wave_discover_lines_prints_the_batch_without_json(tmp_path: Path) -> None:
    runner.invoke(app, ["init", "--path", str(tmp_path)])
    runner.invoke(app, ["spec", "add", "Auth Spec", "--slug", "AUTH", "--path", str(tmp_path)])
    runner.invoke(
        app,
        ["plan", "add", "User Plan", "--spec", "AUTH", "--slug", "USER", "--path", str(tmp_path)],
    )
    runner.invoke(
        app,
        [
            "task",
            "add",
            "Add login",
            "--plan",
            "AUTH-USER",
            "--slug",
            "T1",
            "--path",
            str(tmp_path),
        ],
    )
    runner.invoke(
        app, ["task", "update", "AUTH-USER-T1", "--repo", "core", "--path", str(tmp_path)]
    )
    args = ["wave", "discover", "--spec", "AUTH", "--session", "sess-1", "--slots", "5"]
    args += ["--max-strong", "2", "--path", str(tmp_path)]

    as_json = json.loads(runner.invoke(app, args).stdout.split("\n")[0])
    res = runner.invoke(app, [*args, "--lines"])

    assert res.exit_code == 0, res.stdout
    assert '"' not in res.stdout and "{" not in res.stdout
    chosen = as_json["chosen"][0]
    requires = ",".join(chosen["requires"]) or "-"
    assert res.stdout.splitlines() == [
        f"N AUTH-USER-T1 {chosen['action']} {chosen['model']} {chosen['kind']} core {requires}",
        *[f"H {held}" for held in as_json["held"]],
        f"W {as_json['waiting_for_slot']}",
    ]
