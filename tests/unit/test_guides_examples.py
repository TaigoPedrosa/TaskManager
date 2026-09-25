"""The worked example in `tm guide plan` is executable documentation: it is imported here by the
real CLI, so a guide that drifts from the importer reddens rather than misleading its reader."""

import json
import re
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

    assert flags("NOTIFY-EMAIL") == (True, True, "main")
    assert flags("NOTIFY-EMAIL-SENDER") == (True, True, "parent")
    assert flags("NOTIFY-EMAIL-TEMPLATES") == (True, False, "parent")
    assert flags("NOTIFY-EMAIL-API") == (True, True, "parent")


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
