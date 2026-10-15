import subprocess
from pathlib import Path

import pytest
from lifecycle_estate import commit, git, make_repo

from taskmanager.engine import git as gitops


def repository(tmp_path: Path) -> Path:
    root = tmp_path / "estate"
    root.mkdir()
    return make_repo(root, "api")


def test_changed_files_returns_non_ascii_and_spaced_names_unquoted(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    git(repo, "checkout", "-q", "-b", "work")
    commit(repo, "db/migrations/café.sql", "x", "add")
    commit(repo, "docs/a b.md", "x", "add")

    assert gitops.changed_files(repo, "origin/main", "work") == [
        "db/migrations/café.sql",
        "docs/a b.md",
    ]


def test_changed_files_with_no_difference_returns_nothing(tmp_path: Path) -> None:
    repo = repository(tmp_path)

    assert gitops.changed_files(repo, "origin/main", "origin/main") == []


def test_merge_no_ff_with_a_conflict_returns_false_and_leaves_the_merge_in_progress(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    git(repo, "checkout", "-q", "-b", "other", "origin/main")
    commit(repo, "f.txt", "other\n", "other")
    git(repo, "checkout", "-q", "-b", "mine", "origin/main")
    commit(repo, "f.txt", "mine\n", "mine")

    assert gitops.merge_no_ff(repo, "other", "merge") is False
    assert gitops.rev_parse(repo, "MERGE_HEAD")


def test_merge_no_ff_clean_returns_true(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    git(repo, "checkout", "-q", "-b", "other", "origin/main")
    commit(repo, "g.txt", "other\n", "other")
    git(repo, "checkout", "-q", "-b", "mine", "origin/main")
    commit(repo, "f.txt", "mine\n", "mine")

    assert gitops.merge_no_ff(repo, "other", "merge") is True


def test_merge_no_ff_of_unrelated_histories_raises_instead_of_reading_as_a_conflict(
    tmp_path: Path,
) -> None:
    repo = repository(tmp_path)
    git(repo, "checkout", "-q", "--orphan", "orphan")
    commit(repo, "o.txt", "o\n", "orphan")
    git(repo, "checkout", "-q", "-b", "mine", "origin/main")

    with pytest.raises(subprocess.CalledProcessError):
        gitops.merge_no_ff(repo, "orphan", "merge")
