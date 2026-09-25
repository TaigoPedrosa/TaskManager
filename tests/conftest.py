"""Hermetic terminal and estate for the suite.

`rich` reads the colour variables when its console is first built, so they are settled here, before
anything imports it: a caller's `FORCE_COLOR` would otherwise split `Imported Plan 1` with escape
codes and turn every plain-text assertion on rich output red.
"""

import os

import pytest

os.environ.pop("FORCE_COLOR", None)
os.environ["NO_COLOR"] = "1"
os.environ["COLUMNS"] = "200"


@pytest.fixture(autouse=True)
def _no_estate_outside_the_test(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tm call that names no root must never open the estate of the checkout the suite runs
    from: from a worktree, the root lookup follows git's common dir to the primary checkout and
    its live `.taskmanager`. Each test starts pinned to an empty directory and standing in it,
    with git's repository search stopped at the temp tree, so a call that forgets `-C` fails
    with "no .taskmanager" instead even when `--basetemp` sits inside a repository."""
    empty = tmp_path_factory.mktemp("no-estate")
    monkeypatch.setenv("TM_ROOT", str(empty))
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path_factory.getbasetemp().resolve()))
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(empty)
