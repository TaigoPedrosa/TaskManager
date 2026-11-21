"""An estate 0.3.23 wrote migrates and then claims by tier, through the real CLI."""

import json
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import yaml
from typer.testing import CliRunner, Result

from taskmanager.cli.main import app

runner = CliRunner()

SONNET = "claude-sonnet-5-5"
OPUS = "claude-opus-5-5"
HAIKU = "claude-haiku-4-5-20251001"
FABLE = "claude-fable-5-1"


def tm(root: Path, *args: str) -> Result:
    return runner.invoke(app, [*args, "-C", str(root)])


def ok(root: Path, *args: str) -> Result:
    res = tm(root, *args)
    assert res.exit_code == 0, res.output
    return res


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def import_spec(root: Path, spec: str, tasks: list[dict[str, Any]]) -> None:
    doc = {
        "spec": {"id": spec, "title": spec},
        "plans": [{"id": f"{spec}-P", "title": "P", "tasks": tasks}],
    }
    file = root / f"{spec}.json"
    file.write_text(json.dumps(doc), encoding="utf-8")
    ok(root, "import", "--file", str(file))


def set_user_version(db_file: Path, version: int) -> None:
    conn = sqlite3.connect(db_file)
    try:
        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()
    finally:
        conn.close()


def stored_models(db_file: Path) -> dict[str, tuple[list[str], list[str] | None]]:
    conn = sqlite3.connect(db_file)
    try:
        return {
            node_id: (json.loads(models), json.loads(frontmatter).get("review_models"))
            for node_id, models, frontmatter in conn.execute(
                "SELECT id, acceptable_models, frontmatter_json FROM nodes WHERE kind = 'task'"
            )
        }
    finally:
        conn.close()


def core_repo(root: Path) -> None:
    root.mkdir(exist_ok=True)
    origin = root / "core.git"
    git(root, "init", "--bare", "-b", "main", str(origin))
    git(root, "clone", str(origin), str(root / "core"))
    work = root / "core"
    git(work, "config", "user.email", "ci@example.com")
    git(work, "config", "user.name", "CI")
    git(work, "commit", "--allow-empty", "-m", "init")
    git(work, "push", "origin", "HEAD:main")
    git(work, "fetch", "origin")


def estate_0_3_23(root: Path) -> None:
    core_repo(root)
    ok(root, "init")
    ok(root, "config", "set", "models.default", SONNET)
    ok(root, "config", "set", "repos.core.gates.main.command", "true")
    import_spec(
        root,
        "S1",
        [
            {
                "id": "S1-P-a",
                "title": "a",
                "acceptable_models": [SONNET],
                "frontmatter": {"review_models": [OPUS]},
            },
            {"id": "S1-P-b", "title": "b", "acceptable_models": [HAIKU, FABLE]},
            {"id": "S1-P-c", "title": "c", "acceptable_models": ["gpt-6.1-sol"]},
        ],
    )
    ok(root, "task", "update", "S1-P-a", "--repo", "core")
    import_spec(root, "OLD", [{"id": "OLD-P-a", "title": "a", "acceptable_models": [SONNET]}])
    ok(root, "task", "abandon", "OLD-P-a", "--note", "finished")
    ok(root, "spec", "archive", "OLD")
    for db_file in (root / ".taskmanager" / "state.db", root / ".taskmanager" / "archive.db"):
        set_user_version(db_file, 9)


def test_a_0_3_23_estate_migrates_into_tiers_and_claims_by_tier(tmp_path: Path) -> None:
    estate_0_3_23(tmp_path)
    state_db = tmp_path / ".taskmanager" / "state.db"
    archive_db = tmp_path / ".taskmanager" / "archive.db"
    config = tmp_path / ".taskmanager" / "config.yaml"
    assert stored_models(state_db) == {
        "S1-P-a": ([SONNET], [OPUS]),
        "S1-P-b": ([HAIKU, FABLE], None),
        "S1-P-c": (["gpt-6.1-sol"], None),
    }
    assert stored_models(archive_db) == {"OLD-P-a": ([SONNET], None)}
    assert yaml.safe_load(config.read_text(encoding="utf-8"))["models"]["default"] == SONNET

    before = tm(tmp_path, "task", "list")
    assert before.exit_code == 1
    assert "run `tm db migrate`" in str(before.exception)

    ok(tmp_path, "db", "migrate")

    ok(tmp_path, "task", "list")
    assert stored_models(state_db) == {
        "S1-P-a": (["standard"], ["strong"]),
        "S1-P-b": (["light", "strong"], None),
        "S1-P-c": (["gpt-6.1-sol"], None),
    }
    assert stored_models(archive_db) == {"OLD-P-a": (["standard"], None)}
    assert yaml.safe_load(config.read_text(encoding="utf-8"))["models"]["default"] == "standard"

    claim = ok(
        tmp_path,
        "task",
        "start",
        "S1-P-a",
        "--agent",
        "agent-a",
        "--session",
        "sess-1",
        "--worktree-dir",
        str(tmp_path / "wt"),
        "--harness",
        "claude-code",
        "--json",
    )
    step = json.loads(claim.stdout)
    assert (step["action"], step["model"], step["route"]) == ("implement", "sonnet", "standard")


def exported_with_claude_ids(tmp_path: Path) -> Path:
    """An export holding Claude ids in a live spec's model lists, in an archived one's and in
    `models.*`, every task in `core`."""
    source = tmp_path / "source"
    core_repo(source)
    ok(source, "init")
    ok(source, "config", "set", "repos.core.gates.main.command", "true")
    ok(source, "config", "set", "models.default", SONNET)
    ok(source, "config", "set", "models.strong", f"[{OPUS}, {FABLE}]")
    import_spec(
        source,
        "S1",
        [
            {
                "id": "S1-P-a",
                "title": "a",
                "acceptable_models": [SONNET, OPUS],
                "frontmatter": {"review_models": [OPUS]},
            },
            {"id": "S1-P-b", "title": "b", "acceptable_models": [OPUS]},
            {"id": "S1-P-c", "title": "c", "acceptable_models": [FABLE]},
            {"id": "S1-P-d", "title": "d", "acceptable_models": ["gpt-6.1-sol"]},
        ],
    )
    for task in ("a", "b", "c", "d"):
        ok(source, "task", "update", f"S1-P-{task}", "--repo", "core")
    import_spec(source, "OLD", [{"id": "OLD-P-a", "title": "a", "acceptable_models": [OPUS]}])
    ok(source, "task", "abandon", "OLD-P-a", "--note", "finished")
    ok(source, "spec", "archive", "OLD")
    out = tmp_path / "out"
    ok(source, "export", str(out))
    return out


def restored(tmp_path: Path, export: Path) -> Path:
    fresh = tmp_path / "fresh"
    core_repo(fresh)
    ok(fresh, "restore", str(export))
    return fresh


def test_a_format_5_export_restores_its_claude_ids_as_tiers_that_max_strong_counts(
    tmp_path: Path,
) -> None:
    export = exported_with_claude_ids(tmp_path)
    (export / "_format.json").write_text(
        json.dumps({"format": "tm-lifecycle", "version": 5}), encoding="utf-8"
    )

    fresh = restored(tmp_path, export)

    assert stored_models(fresh / ".taskmanager" / "state.db") == {
        "S1-P-a": (["standard", "strong"], ["strong"]),
        "S1-P-b": (["strong"], None),
        "S1-P-c": (["strong"], None),
        "S1-P-d": (["gpt-6.1-sol"], None),
    }
    assert stored_models(fresh / ".taskmanager" / "archive.db") == {"OLD-P-a": (["strong"], None)}
    models = yaml.safe_load((fresh / ".taskmanager" / "config.yaml").read_text(encoding="utf-8"))
    assert models["models"] == {"default": "standard", "strong": ["strong"]}
    found = ok(
        fresh,
        *("wave", "discover", "--harness", "claude-code", "--lines"),
        *("--session", "s1", "--slots", "4", "--max-strong", "1"),
    ).stdout.splitlines()
    chosen = {line.split()[1]: line.split()[3] for line in found if line.startswith("N ")}
    strong = [node for node in ("S1-P-b", "S1-P-c") if node in chosen]
    assert len(strong) == 1
    held = "S1-P-c" if strong == ["S1-P-b"] else "S1-P-b"
    assert chosen == {"S1-P-a": "sonnet", strong[0]: "opus", "S1-P-d": "gpt-6.1-sol"}
    assert f"H {held}: no free strong-model slot" in found


def test_a_current_export_restores_a_claude_id_as_the_pin_it_is(tmp_path: Path) -> None:
    fresh = restored(tmp_path, exported_with_claude_ids(tmp_path))

    assert stored_models(fresh / ".taskmanager" / "state.db")["S1-P-b"] == ([OPUS], None)
    assert stored_models(fresh / ".taskmanager" / "archive.db") == {"OLD-P-a": ([OPUS], None)}
    config = yaml.safe_load((fresh / ".taskmanager" / "config.yaml").read_text(encoding="utf-8"))
    assert config["models"] == {"default": SONNET, "strong": [OPUS, FABLE]}


def test_a_format_5_export_leaves_the_pins_the_destination_archive_already_holds(
    tmp_path: Path,
) -> None:
    export = exported_with_claude_ids(tmp_path)
    (export / "_format.json").write_text(
        json.dumps({"format": "tm-lifecycle", "version": 5}), encoding="utf-8"
    )
    fresh = tmp_path / "fresh"
    core_repo(fresh)
    ok(fresh, "init")
    import_spec(fresh, "MINE", [{"id": "MINE-P-a", "title": "a", "acceptable_models": [OPUS]}])
    ok(fresh, "task", "abandon", "MINE-P-a", "--note", "finished")
    ok(fresh, "spec", "archive", "MINE")

    ok(fresh, "restore", str(export))

    assert stored_models(fresh / ".taskmanager" / "archive.db") == {
        "MINE-P-a": ([OPUS], None),
        "OLD-P-a": (["strong"], None),
    }
