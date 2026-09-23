import json
from pathlib import Path

from typer.testing import CliRunner

from taskmanager.cli.main import app
from taskmanager.engine.wave import djb2

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
