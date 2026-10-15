"""Commands refuse or print what they were given instead of writing blanks or garbling output."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from taskmanager.cli.main import _parse_import, app
from taskmanager.core.enums import ImportFormat
from taskmanager.di.container import create_container
from taskmanager.engine.operations import Operations

runner = CliRunner()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    assert runner.invoke(app, ["init", "-C", str(tmp_path)]).exit_code == 0
    ops = create_container(tmp_path).get(Operations)
    ops.add_task("t", ops.add_plan("P", ops.add_spec("S", slug="S1"), slug="P1"), slug="a")
    return tmp_path


def test_section_set_without_content_refuses_and_keeps_the_section(root: Path) -> None:
    assert runner.invoke(app, ["section", "set", "S1-P1-a:steps", "keep", "-C", str(root)])

    res = runner.invoke(app, ["section", "set", "S1-P1-a:steps", "-C", str(root)])

    assert res.exit_code == 1, res.output
    got = runner.invoke(app, ["section", "get", "S1-P1-a:steps", "-C", str(root)])
    assert got.stdout == "keep"


def test_section_set_with_an_explicit_empty_content_still_clears_it(root: Path) -> None:
    runner.invoke(app, ["section", "set", "S1-P1-a:steps", "keep", "-C", str(root)])

    res = runner.invoke(app, ["section", "set", "S1-P1-a:steps", "--content", "", "-C", str(root)])

    assert res.exit_code == 0, res.output
    assert runner.invoke(app, ["section", "get", "S1-P1-a:steps", "-C", str(root)]).stdout == ""


def test_section_get_without_a_key_prints_a_markup_looking_id_verbatim(root: Path) -> None:
    res = runner.invoke(app, ["section", "get", "[x]", "-C", str(root)])

    assert res.exit_code == 0, res.output
    assert "'[x]'" in res.output


def test_verify_run_with_json_and_no_verifications_keeps_stdout_empty(root: Path) -> None:
    res = runner.invoke(app, ["verify", "run", "S1-P1-a", "--json", "-C", str(root)])

    assert res.exit_code == 2, res.output
    assert res.stdout == ""


def test_markdown_import_reads_the_frontmatter_and_ignores_a_dashed_value() -> None:
    content = "---\nspec:\n  id: S\n  title: a ---\n---\n"

    assert _parse_import(content, ImportFormat.MARKDOWN) == {"spec": {"id": "S", "title": "a ---"}}


def test_markdown_import_with_a_body_refuses_instead_of_dropping_it() -> None:
    with pytest.raises(ValueError, match="only the frontmatter"):
        _parse_import("---\nspec: {id: S, title: t}\n---\n\nthe spec text\n", ImportFormat.MARKDOWN)


@pytest.mark.parametrize(
    "content", ["spec: {}\n", "---\nspec: {}\n", ""], ids=["none", "open", "empty"]
)
def test_markdown_import_without_a_closed_frontmatter_block_refuses(content: str) -> None:
    with pytest.raises(ValueError, match="frontmatter block"):
        _parse_import(content, ImportFormat.MARKDOWN)


def test_markdown_import_through_the_command_refuses_a_body(root: Path, tmp_path: Path) -> None:
    doc = tmp_path / "s.md"
    doc.write_text("---\nspec: {id: S2, title: t}\n---\ntext\n", encoding="utf-8")

    res = runner.invoke(app, ["import", "-f", str(doc), "-C", str(root)])

    assert res.exit_code == 1, res.output
    listed = json.loads(runner.invoke(app, ["spec", "list", "--json", "-C", str(root)]).stdout)
    assert "S2" not in json.dumps(listed)
