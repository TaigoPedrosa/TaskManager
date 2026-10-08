import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
PRIVATE = re.compile(r"(?i:socialsrc)|/Users/")
# Sample command lines a test renders, where an absolute path is the content under test.
ABSOLUTE_PATH_FIXTURES = {"tests/web/decisions_read.test.mjs"}
# What running the suite in an exported tree writes beside the exported files.
RUN_OUTPUT = {".venv", "__pycache__", ".pytest_cache"}
GATES = [
    "uv run ruff check .",
    "uv run ruff format --check .",
    "uv run mypy",
    "uv run pytest",
    "node --test tests/",
]


def is_checkout() -> bool:
    return (REPO / ".git").exists()


def tracked_files() -> list[str]:
    if not is_checkout():
        return [
            path.relative_to(REPO).as_posix()
            for path in REPO.rglob("*")
            if path.is_file() and RUN_OUTPUT.isdisjoint(path.relative_to(REPO).parts)
        ]
    listing = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-z"], capture_output=True, text=True, check=True
    )
    return [name for name in listing.stdout.split("\0") if (REPO / name).is_file()]


def test_no_tracked_file_names_a_private_repository_or_home_directory() -> None:
    skipped = ABSOLUTE_PATH_FIXTURES | {Path(__file__).relative_to(REPO).as_posix()}
    hits = [
        f"{name}:{number}: {line.strip()}"
        for name in sorted(set(tracked_files()) - skipped)
        for number, line in enumerate(
            (REPO / name).read_bytes().decode("utf-8", errors="replace").splitlines(), start=1
        )
        if PRIVATE.search(line)
    ]
    assert hits == []


def test_the_internal_planning_notes_are_not_published() -> None:
    assert not (REPO / "docs/superpowers").exists()


def test_the_changelog_has_an_entry_for_every_release_tag() -> None:
    if not is_checkout():
        pytest.skip("an exported tree carries no git tags")
    tags = subprocess.run(
        ["git", "-C", str(REPO), "tag", "-l", "v*"], capture_output=True, text=True, check=True
    ).stdout.split()
    changelog = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    assert [tag for tag in tags if f"## [{tag.removeprefix('v')}]" not in changelog] == []


def test_the_changelog_has_an_entry_for_the_packaged_version() -> None:
    version = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    assert f"## [{version}]" in (REPO / "CHANGELOG.md").read_text(encoding="utf-8")


def test_ci_runs_every_gate() -> None:
    workflow = yaml.safe_load((REPO / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    runs = [step.get("run", "") for job in workflow["jobs"].values() for step in job["steps"]]
    assert [gate for gate in GATES if not any(gate in run for run in runs)] == []


def test_local_tool_output_is_git_ignored_and_docs_images_are_not(tmp_path: Path) -> None:
    shutil.copy(REPO / ".gitignore", tmp_path / ".gitignore")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    ignored = [
        ".playwright-cli/page.yml",
        ".playwright-mcp/page.png",
        ".taskmanager-bkp/state.db",
        "app_375.png",
    ]
    check = subprocess.run(
        ["git", "-C", str(tmp_path), "-c", "core.excludesFile=/dev/null", "check-ignore"]
        + [*ignored, "docs/tm-web.png"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert (check.returncode, check.stdout.split()) == (0, ignored)
