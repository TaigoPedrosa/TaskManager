"""A claim's route through the real CLI: strong counting, the ledger row, the web body and the
harness profile."""

import json
import subprocess
from pathlib import Path
from typing import Any

import yaml
from click.testing import Result
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from taskmanager.cli.main import app
from taskmanager.web.app import create_app

runner = CliRunner()


def tm(root: Path, *args: str) -> Result:
    return runner.invoke(app, [*args, "-C", str(root)])


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def ok(root: Path, *args: str) -> Result:
    res = tm(root, *args)
    assert res.exit_code == 0, res.output
    return res


def estate(root: Path, *, strong: tuple[str, ...] = ()) -> None:
    """Tasks `S1-P1-a` and `S1-P1-b` in the repository `core`; those in `strong` route to the
    strong tier."""
    origin = root / "core.git"
    git(root, "init", "--bare", "-b", "main", str(origin))
    git(root, "clone", str(origin), str(root / "core"))
    work = root / "core"
    git(work, "config", "user.email", "ci@example.com")
    git(work, "config", "user.name", "CI")
    git(work, "commit", "--allow-empty", "-m", "init")
    git(work, "push", "origin", "HEAD:main")
    git(work, "fetch", "origin")
    ok(root, "init")
    ok(root, "spec", "add", "S", "--slug", "S1", "--no-review", "--no-fix")
    ok(root, "plan", "add", "P", "--spec", "S1", "--slug", "P1")
    for slug in ("a", "b"):
        ok(root, "task", "add", slug, "--plan", "S1-P1", "--slug", slug)
        ok(root, "task", "update", f"S1-P1-{slug}", "--repo", "core")
        if slug in strong:
            ok(root, "task", "update", f"S1-P1-{slug}", "--models", "strong")
    ok(root, "config", "set", "repos.core.gates.main.command", "true")


def discover(root: Path, session: str) -> list[str]:
    res = ok(
        root,
        *("wave", "discover", "--harness", "claude-code", "--lines"),
        *("--session", session, "--slots", "4", "--max-strong", "1"),
    )
    return res.stdout.splitlines()


def claim(root: Path, *flags: str) -> dict[str, Any]:
    res = ok(root, "task", "start", "S1-P1-a", "--agent", "a1", "--session", "s1", *flags)
    step: dict[str, Any] = yaml.safe_load(res.stdout)
    return step


def test_a_strong_claim_keeps_counting_against_max_strong_in_a_later_discovery(
    tmp_path: Path,
) -> None:
    estate(tmp_path, strong=("a", "b"))

    first = discover(tmp_path, "s1")

    chosen = [line for line in first if line.startswith("N ")]
    assert len(chosen) == 1 and chosen[0].split()[3] == "opus"
    held = [line for line in first if line.startswith("H ")]
    other = "S1-P1-b" if "S1-P1-a" in chosen[0] else "S1-P1-a"
    assert held == [f"H {other}: no free strong-model slot"]

    ok(tmp_path, "task", "start", chosen[0].split()[1], "--agent", "a1", "--session", "s1")
    second = discover(tmp_path, "s1")

    assert [line for line in second if line.startswith("N ")] == []
    assert f"H {other}: no free strong-model slot" in second


def test_a_claude_code_claim_records_its_route_and_model_and_the_web_steps_show_them(
    tmp_path: Path,
) -> None:
    estate(tmp_path)

    step = claim(tmp_path, "--harness", "claude-code")

    assert (step["route"], step["model"]) == ("standard", "sonnet")
    steps = TestClient(create_app(tmp_path)).get("/api/nodes/S1-P1-a").json()["steps"]
    assert [(s["action"], s["route"], s["model"]) for s in steps] == [
        ("implement", "standard", "sonnet")
    ]
    events = json.loads(ok(tmp_path, "task", "get", "S1-P1-a", "--json").stdout)
    assert events["lease"]["route"] == "standard"
    assert "model" not in events["lease"]


def test_a_codex_claim_has_no_model_without_a_profile_and_the_profile_string_with_one(
    tmp_path: Path,
) -> None:
    estate(tmp_path)

    bare = claim(tmp_path, "--harness", "codex")

    assert bare["model"] is None
    assert bare["model_note"] == "harness default: harnesses.codex.models.standard is unset"

    ok(tmp_path, "task", "release", "S1-P1-a", "--agent", "a1", "--token", bare["token"])
    ok(tmp_path, "config", "set", "harnesses.codex.models.standard", "gpt-6.1-sol")
    mapped = claim(tmp_path, "--harness", "codex")

    assert (mapped["model"], mapped["route"]) == ("gpt-6.1-sol", "standard")
    assert "model_note" not in mapped
