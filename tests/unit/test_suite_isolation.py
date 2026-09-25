"""No test can reach the estate of the checkout the suite runs from: from a worktree, tm's root
lookup follows git's common dir to the primary checkout and its live `.taskmanager`."""

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from taskmanager.cli.main import app


def test_every_test_starts_pinned_to_an_empty_root_inside_the_temp_tree(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    base = tmp_path_factory.getbasetemp().resolve()
    pinned = Path(os.environ.get("TM_ROOT", "/")).resolve()
    assert pinned.is_relative_to(base)
    assert not (pinned / ".taskmanager").exists()
    assert Path.cwd().resolve().is_relative_to(base)


@pytest.mark.parametrize("unset_root", [False, True], ids=["pinned", "unpinned"])
def test_a_cli_call_that_names_no_root_fails_inside_the_temp_tree(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch, unset_root: bool
) -> None:
    if unset_root:
        monkeypatch.delenv("TM_ROOT")
    result = CliRunner().invoke(app, ["task", "list"])
    assert result.exit_code == 2
    base = str(tmp_path_factory.getbasetemp().resolve())
    assert f"no .taskmanager at {base}" in " ".join(result.output.split())
