"""The worked example in `tm guide plan` is executable documentation: it is imported here by the
real CLI, so a guide that drifts from the importer reddens rather than misleading its reader."""

import json
import os
import re
import subprocess
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml
from typer.testing import CliRunner

from taskmanager.cli.main import app

runner = CliRunner()

EXAMPLE_BLOCK = re.compile(r"<!-- tm:example -->\s*```yaml\n(.*?)```", re.DOTALL)

EXPECTED_TASKS = {
    "NOTIFY-EMAIL-SENDER",
    "NOTIFY-EMAIL-TEMPLATES",
    "NOTIFY-EMAIL-API",
}


def _example_document() -> str:
    guide = files("taskmanager").joinpath("guides/plan.md").read_text(encoding="utf-8")
    match = EXAMPLE_BLOCK.search(guide)
    assert match is not None, "plan.md carries no `<!-- tm:example -->` yaml block"
    body = match.group(1)
    assert body.strip(), "the marked example block is empty"
    return body


def _run(*args: str) -> str:
    result = runner.invoke(app, list(args))
    assert result.exit_code == 0, f"{args} exited {result.exit_code}:\n{result.output}"
    return result.output


def _imported_root(tmp_path: Path) -> Path:
    document = tmp_path / "plan.yaml"
    document.write_text(_example_document(), encoding="utf-8")
    _run("init", "-C", str(tmp_path))
    subprocess.run(["git", "init", "-q", str(tmp_path / "backend")], check=True)
    _run("config", "set", "repos.backend.gates.main.command", "true", "-C", str(tmp_path))
    _run("import", "--format", "yaml", "-f", str(document), "-C", str(tmp_path))
    return tmp_path


def _yaml(*args: str) -> Any:
    return yaml.safe_load(_run(*args))


def test_the_example_document_is_the_shape_the_importer_reads() -> None:
    doc = yaml.safe_load(_example_document())
    assert set(doc) == {"spec", "plans"}
    tasks = [t for plan in doc["plans"] for t in plan["tasks"]]
    assert {t["id"] for t in tasks} == EXPECTED_TASKS


def test_the_example_imports_the_documented_nodes(tmp_path: Path) -> None:
    root = _imported_root(tmp_path)

    specs = _yaml("spec", "list", "--yaml", "-C", str(root))
    plans = _yaml("plan", "list", "--yaml", "-C", str(root))
    tasks = _yaml("task", "list", "--yaml", "-C", str(root))

    assert [s["id"] for s in specs] == ["NOTIFY"]
    assert [p["id"] for p in plans] == ["NOTIFY-EMAIL"]
    assert {t["id"] for t in tasks} == EXPECTED_TASKS


def test_the_example_imports_the_documented_dependency_edges(tmp_path: Path) -> None:
    root = _imported_root(tmp_path)

    edges = {
        task_id: {
            d["id"] for d in _yaml("task", "get", task_id, "--yaml", "-C", str(root))["depends_on"]
        }
        for task_id in sorted(EXPECTED_TASKS)
    }

    assert edges == {
        "NOTIFY-EMAIL-SENDER": set(),
        "NOTIFY-EMAIL-TEMPLATES": set(),
        "NOTIFY-EMAIL-API": {"NOTIFY-EMAIL-SENDER", "NOTIFY-EMAIL-TEMPLATES"},
    }


def test_the_example_imports_the_documented_flags(tmp_path: Path) -> None:
    root = _imported_root(tmp_path)

    def flags(node_id: str) -> tuple[object, object, object]:
        doc = _yaml("task", "get", node_id, "--yaml", "-C", str(root))
        return doc["review"], doc["fix"], doc["merge"]

    assert flags("NOTIFY-EMAIL") == (True, True, "spec")
    # The plan's one review of its landed branch covers every task under it.
    assert flags("NOTIFY-EMAIL-SENDER") == (False, False, "parent")
    assert flags("NOTIFY-EMAIL-TEMPLATES") == (False, False, "parent")
    assert flags("NOTIFY-EMAIL-API") == (False, False, "parent")


def test_every_example_node_lands_on_the_spec_s_land_on(tmp_path: Path) -> None:
    land_on = yaml.safe_load(_example_document())["spec"]["frontmatter"]["land_on"]
    guide = files("taskmanager").joinpath("guides/plan.md").read_text(encoding="utf-8")
    assert f"the plan lands on the spec's `land_on`, `{land_on}`," in guide
    root = _imported_root(tmp_path)

    lands_on = {
        node_id: _yaml("task", "get", node_id, "--yaml", "-C", str(root))["lands_on"]
        for node_id in ("NOTIFY", "NOTIFY-EMAIL", *sorted(EXPECTED_TASKS))
    }

    assert lands_on == dict.fromkeys(lands_on, land_on)


def test_discovery_offers_the_tasks_the_guide_says_it_offers_first(tmp_path: Path) -> None:
    root = _imported_root(tmp_path)

    output = _run(
        "wave",
        "discover",
        "--session",
        "guide-example",
        "--slots",
        "5",
        "--max-strong",
        "5",
        "-C",
        str(root),
    )

    payload = json.loads(output.splitlines()[0])
    assert sorted((node["id"], node["action"]) for node in payload["chosen"]) == [
        ("NOTIFY-EMAIL-SENDER", "implement"),
        ("NOTIFY-EMAIL-TEMPLATES", "implement"),
    ]


def test_re_importing_the_untouched_example_changes_nothing(tmp_path: Path) -> None:
    root = _imported_root(tmp_path)

    def snapshot(name: str) -> dict[str, str]:
        out = root / name
        _run("export", str(out), "-C", str(root))
        written = sorted(out.glob("*.json"))
        assert written, "export wrote no files"
        return {p.name: p.read_text(encoding="utf-8") for p in written}

    before = snapshot("export-before")
    _run("import", "--format", "yaml", "-f", str(root / "plan.yaml"), "-C", str(root))
    after = snapshot("export-after")

    assert before == after


def _example_checks(label_suffix: str) -> list[tuple[str, str]]:
    doc = yaml.safe_load(_example_document())
    return [
        (task["target_repo"], v["expected_pattern"])
        for plan in doc["plans"]
        for task in plan["tasks"]
        for v in task.get("verifications", [])
        if v["type"] == "test_command" and v["target_path"].endswith(label_suffix)
    ]


def _example_test_commands() -> list[tuple[str, str, str]]:
    return [(repo, command, command.split()[-2]) for repo, command in _example_checks("-suite")]


def _git(repo: Path, *args: str) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


def _commit_tests(repo: Path, paths: list[str], body: str, branch: str) -> None:
    for rel in paths:
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(body, encoding="utf-8")
    _git(repo, "checkout", "-q", "-B", branch)
    _git(repo, "add", *paths)
    _git(repo, "commit", "-q", "-m", branch)


def test_every_example_test_command_runs_the_ref_it_is_given_in_its_target_repo(
    tmp_path: Path,
) -> None:
    """A landing runs these from the tm root with the target in TM_VERIFY_REF; one that runs the
    root's own files, or the repository's working tree, reads neither the target nor the ref. Run
    outside tm, with no ref exported, the command reads origin/main."""
    commands = _example_test_commands()
    assert len(commands) == 3
    repo = tmp_path / "backend"
    repo.mkdir()
    _git(repo, "init", "-q")
    paths = [path for _, _, path in commands]
    _commit_tests(repo, paths, "def test_holds():\n    pass\n", "green")
    _commit_tests(repo, paths, "def test_holds():\n    assert False\n", "red")

    unset = {k: v for k, v in os.environ.items() if k != "TM_VERIFY_REF"}
    for target_repo, command, _ in commands:
        assert target_repo == "backend"
        exits = {}
        for ref in ("green", "red"):
            env = {**unset, "TM_VERIFY_REF": ref}
            done = subprocess.run(
                command, shell=True, cwd=tmp_path, env=env, capture_output=True, check=False
            )
            exits[ref] = done.returncode
            _git(repo, "update-ref", "refs/remotes/origin/main", ref)
            done = subprocess.run(
                command, shell=True, cwd=tmp_path, env=unset, capture_output=True, check=False
            )
            exits[f"origin/main at {ref}"] = done.returncode
        green = exits["green"] == 0 and exits["origin/main at green"] == 0
        red = exits["red"] != 0 and exits["origin/main at red"] != 0
        assert green and red, (command, exits)


def test_every_example_test_command_is_a_suite_or_a_definition_grep() -> None:
    doc = yaml.safe_load(_example_document())
    labels = [
        v["target_path"]
        for plan in doc["plans"]
        for task in plan["tasks"]
        for v in task.get("verifications", [])
        if v["type"] == "test_command"
    ]
    assert sorted(labels) == ["api-suite", "sender-defined", "sender-suite", "templates-suite"]


def _run_at(command: str, cwd: Path, ref: str | None) -> int:
    env = {k: v for k, v in os.environ.items() if k != "TM_VERIFY_REF"}
    if ref is not None:
        env["TM_VERIFY_REF"] = ref
    return subprocess.run(
        command, shell=True, cwd=cwd, env=env, capture_output=True, check=False
    ).returncode


def test_the_example_s_definition_check_greps_the_ref_it_is_given(tmp_path: Path) -> None:
    """The check passes only on a ref whose file defines `send`, whatever the working tree holds,
    and with no ref exported reads origin/main."""
    [(target_repo, command)] = _example_checks("-defined")
    repo = tmp_path / target_repo
    repo.mkdir()
    _git(repo, "init", "-q")
    path = "src/notify/email/sender.py"
    _commit_tests(repo, [path], "def send(message):\n    pass\n", "defined")
    _commit_tests(repo, [path], "def deliver(message):\n    pass\n", "renamed")
    _git(repo, "checkout", "-q", "defined")

    assert _run_at(command, tmp_path, "defined") == 0
    assert _run_at(command, tmp_path, "renamed") != 0
    _git(repo, "update-ref", "refs/remotes/origin/main", "renamed")
    assert _run_at(command, tmp_path, None) != 0
    _git(repo, "update-ref", "refs/remotes/origin/main", "defined")
    assert _run_at(command, tmp_path, None) == 0
