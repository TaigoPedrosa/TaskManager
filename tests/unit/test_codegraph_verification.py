import json
import os
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from taskmanager.cli.main import app
from taskmanager.core.enums import VerificationType
from taskmanager.core.models import NodeVerification
from taskmanager.db.node_repo import NodeRepository
from taskmanager.di.container import create_container
from taskmanager.engine.verification import CODEGRAPH_INSTALL, VerificationEngine

runner = CliRunner()

# Stands in for the codegraph CLI: `init` marks a directory indexed, and `query` prints the
# indexed tree's lines that contain the search, so a check can only match what the ref holds.
STUB = r"""#!/bin/sh
printf '%s\n' "$*" >> "$CODEGRAPH_LOG"
cmd=$1
shift
if [ "$cmd" = init ]; then
  for arg; do dir=$arg; done
  if [ -n "$CODEGRAPH_INIT_FAILS" ]; then echo "init broke" >&2; exit 1; fi
  mkdir -p "$dir/.codegraph"
  if [ -n "$CODEGRAPH_INIT_RACES" ]; then
    name=$(basename "$dir"); sha=${name#.}; sha=${sha%%-*}
    cp -R "$dir/." "$(dirname "$dir")/$sha" && touch "$(dirname "$dir")/$sha/won"
  fi
  exit 0
fi
while [ $# -gt 0 ]; do
  case $1 in
    -p) dir=$2; shift 2 ;;
    --) search=$2; shift 2 ;;
    *) shift ;;
  esac
done
if [ -n "$CODEGRAPH_QUERY_FAILS" ]; then echo "query broke" >&2; exit 2; fi
[ -d "$dir/.codegraph" ] || { echo "not initialized in $dir" >&2; exit 1; }
grep -rh --exclude-dir=.codegraph -e "$search" "$dir"
exit 0
"""


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _commit(repo: Path, rel_path: str, content: str) -> None:
    (repo / rel_path).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel_path).write_text(content, encoding="utf-8")
    _git(repo, "add", rel_path)
    _git(repo, "commit", "-q", "-m", f"add {rel_path}")


def _make_repo(repo: Path) -> Path:
    """A checkout with a codegraph index, `src/app.py` defining `on_main` pushed to a bare
    `origin`."""
    origin = repo.parent / f"{repo.name}-origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], capture_output=True, check=True)
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "remote", "add", "origin", str(origin))
    _commit(repo, "src/app.py", "def on_main():\n    pass\n")
    _git(repo, "push", "-q", "-u", "origin", "main")
    (repo / ".codegraph").mkdir()
    (repo / ".codegraph" / "codegraph.db").touch()
    with (repo / ".git" / "info" / "exclude").open("a", encoding="utf-8") as exclude:
        exclude.write(".codegraph/\n")
    return repo


@pytest.fixture
def log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "codegraph"
    stub.write_text(STUB, encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    calls = tmp_path / "codegraph.log"
    monkeypatch.setenv("CODEGRAPH_LOG", str(calls))
    for flag in ("CODEGRAPH_INIT_FAILS", "CODEGRAPH_INIT_RACES", "CODEGRAPH_QUERY_FAILS"):
        monkeypatch.delenv(flag, raising=False)
    return calls


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A tm root whose target repo `app` is a git checkout with a codegraph index."""
    _make_repo(tmp_path / "root" / "app")
    return tmp_path / "root"


def _calls(log: Path, command: str) -> list[str]:
    lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return [line for line in lines if line.split(" ", 1)[0] == command]


def _check(
    pattern: str | None = r"def on_main\(\)", search: str = "def on_", flags: str | None = None
) -> NodeVerification:
    return NodeVerification(
        node_id="T1",
        verification_type=VerificationType.CODEGRAPH_QUERY,
        target_path=search,
        expected_pattern=pattern,
        codegraph_query_json=flags,
    )


def _cache(root: Path) -> Path:
    return root / ".taskmanager" / "cache" / "codegraph"


def test_codegraph_query_whose_output_matches_the_pattern_passes(root: Path, log: Path) -> None:
    result = VerificationEngine(root).verify_assertion(_check(), target_repo="app")
    assert result.passed is True, result.message
    assert "origin/main" in result.message


def test_codegraph_query_whose_output_does_not_match_the_pattern_fails(
    root: Path, log: Path
) -> None:
    result = VerificationEngine(root).verify_assertion(
        _check(pattern=r"def on_feature\(\)"), target_repo="app"
    )
    assert result.passed is False
    assert "does not match" in result.message


def test_codegraph_query_reads_the_ref_and_never_the_working_tree(root: Path, log: Path) -> None:
    repo = root / "app"
    _git(repo, "checkout", "-q", "-b", "feature")
    _commit(repo, "src/feature.py", "def on_feature():\n    pass\n")
    (repo / "src" / "disk.py").write_text("def on_disk():\n    pass\n", encoding="utf-8")
    engine = VerificationEngine(root)

    on_feature = _check(pattern=r"def on_feature\(\)")
    assert engine.verify_assertion(on_feature, target_repo="app", ref="feature").passed is True
    assert engine.verify_assertion(on_feature, target_repo="app").passed is False
    on_disk = _check(pattern=r"def on_disk\(\)")
    assert engine.verify_assertion(on_disk, target_repo="app", ref="feature").passed is False


def test_codegraph_query_flags_reach_the_command_line(root: Path, log: Path) -> None:
    check = _check(flags='{"kind": "function", "limit": 20}')
    assert VerificationEngine(root).verify_assertion(check, target_repo="app").passed is True

    tree = _cache(root) / _git(root / "app", "rev-parse", "origin/main")
    assert _calls(log, "query") == [f"query --kind function --limit 20 --json -p {tree} -- def on_"]


@pytest.mark.parametrize("flags", ['{"kind"', '["kind", "function"]'])
def test_codegraph_query_flags_that_are_not_a_json_object_fail(
    root: Path, log: Path, flags: str
) -> None:
    result = VerificationEngine(root).verify_assertion(_check(flags=flags), target_repo="app")
    assert result.passed is False
    assert "not a JSON object" in result.message
    assert not log.exists()


@pytest.mark.parametrize(
    ("pattern", "problem"),
    [(None, "has no expected_pattern"), ("", "has no expected_pattern"), ("(", "not a regex")],
)
def test_codegraph_query_without_a_usable_pattern_fails_before_running(
    root: Path, log: Path, pattern: str | None, problem: str
) -> None:
    result = VerificationEngine(root).verify_assertion(_check(pattern=pattern), target_repo="app")
    assert result.passed is False
    assert problem in result.message
    assert not log.exists()


def test_codegraph_query_without_the_cli_fails_naming_the_install_command(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    result = VerificationEngine(root).verify_assertion(_check(), target_repo="app")
    assert result.passed is False
    assert CODEGRAPH_INSTALL in result.message


def test_codegraph_query_in_a_repo_without_an_index_fails_naming_codegraph_init(
    root: Path, log: Path
) -> None:
    (root / "app" / ".codegraph" / "codegraph.db").unlink()
    (root / "app" / ".codegraph" / ".gitignore").write_text("*\n!.gitignore\n", encoding="utf-8")
    result = VerificationEngine(root).verify_assertion(_check(), target_repo="app")
    assert result.passed is False
    assert "`codegraph init`" in result.message
    assert not log.exists()


def test_codegraph_query_reuses_the_index_built_for_a_sha(root: Path, log: Path) -> None:
    engine = VerificationEngine(root)
    assert engine.verify_assertion(_check(), target_repo="app").passed is True
    assert engine.verify_assertion(_check(), target_repo="app").passed is True
    assert len(_calls(log, "init")) == 1
    assert len(_calls(log, "query")) == 2


def test_codegraph_query_whose_index_build_fails_fails_and_leaves_no_tree(
    root: Path, log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEGRAPH_INIT_FAILS", "1")
    result = VerificationEngine(root).verify_assertion(_check(), target_repo="app")
    assert result.passed is False
    assert "`codegraph init` failed" in result.message
    assert "init broke" in result.message
    assert sorted(p.name for p in _cache(root).iterdir()) == [".gitignore"]
    assert _calls(log, "query") == []


def test_codegraph_query_keeps_the_tree_a_concurrent_run_indexed_first(
    root: Path, log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEGRAPH_INIT_RACES", "1")
    result = VerificationEngine(root).verify_assertion(_check(), target_repo="app")
    assert result.passed is True, result.message
    sha = _git(root / "app", "rev-parse", "origin/main")
    assert (_cache(root) / sha / "won").exists()
    assert sorted(p.name for p in _cache(root).iterdir()) == [".gitignore", sha]


def test_codegraph_query_that_exits_non_zero_fails_with_its_error(
    root: Path, log: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEGRAPH_QUERY_FAILS", "1")
    result = VerificationEngine(root).verify_assertion(_check(), target_repo="app")
    assert result.passed is False
    assert "query broke" in result.message


def test_codegraph_query_at_a_ref_that_does_not_resolve_fails(root: Path, log: Path) -> None:
    result = VerificationEngine(root).verify_assertion(
        _check(), target_repo="app", ref="no-such-ref"
    )
    assert result.passed is False
    assert "does not resolve" in result.message
    assert not log.exists()


def test_codegraph_query_whose_fetch_fails_fails_closed(
    root: Path, log: Path, tmp_path: Path
) -> None:
    _git(root / "app", "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    result = VerificationEngine(root).verify_assertion(_check(), target_repo="app")
    assert result.passed is False
    assert "fetch" in result.message
    assert not log.exists()


def test_codegraph_query_with_no_target_repo_reads_the_tm_root_and_leaves_git_clean(
    tmp_path: Path, log: Path
) -> None:
    root = _make_repo(tmp_path / "solo")
    result = VerificationEngine(root).verify_assertion(_check())
    assert result.passed is True, result.message
    assert "in ." in result.message
    assert _git(root, "status", "--porcelain") == ""


# -- the write rule ----------------------------------------------------------------------------


def _import(root: Path, tmp_path: Path, checks: list[dict[str, str]]) -> str:
    subprocess.run(["git", "init", "-q", str(root / "app")], check=True)
    doc = tmp_path / "doc.json"
    task = {"id": "P-1", "title": "a", "target_repo": "app", "verifications": checks}
    doc.write_text(
        json.dumps({"plans": [{"id": "P", "title": "P", "tasks": [task]}]}), encoding="utf-8"
    )
    result = runner.invoke(app, ["import", "-f", str(doc), "-C", str(root)])
    return "" if result.exit_code == 0 else result.output


def _listed(root: Path) -> list[dict[str, str]]:
    listed = runner.invoke(app, ["verify", "list", "P-1", "--json", "-C", str(root)])
    assert listed.exit_code == 0, listed.output
    rows: list[dict[str, str]] = json.loads(listed.stdout)
    return rows


@pytest.mark.parametrize(
    ("pattern", "problem"),
    [({}, "has no expected_pattern"), ({"expected_pattern": "("}, "not a regex")],
)
def test_import_refuses_a_codegraph_query_without_a_usable_pattern(
    tmp_path: Path, pattern: dict[str, str], problem: str
) -> None:
    root = tmp_path / "estate"
    root.mkdir()
    assert runner.invoke(app, ["init", "-C", str(root)]).exit_code == 0
    refused = _import(root, tmp_path, [{"type": "codegraph_query", "target_path": "x", **pattern}])
    assert problem in " ".join(refused.split())
    assert runner.invoke(app, ["verify", "list", "P-1", "-C", str(root)]).exit_code == 1

    usable = {"type": "codegraph_query", "target_path": "x", "expected_pattern": "x"}
    assert _import(root, tmp_path, [usable]) == ""
    assert [r["type"] for r in _listed(root)] == ["codegraph_query"]


def test_verify_add_refuses_a_codegraph_query_without_a_pattern(tmp_path: Path) -> None:
    root = tmp_path / "estate"
    root.mkdir()
    assert runner.invoke(app, ["init", "-C", str(root)]).exit_code == 0
    assert _import(root, tmp_path, []) == ""

    add = ["verify", "add", "P-1", "--type", "codegraph_query", "--target", "x", "-C", str(root)]
    refused = runner.invoke(app, add)
    assert refused.exit_code == 1
    assert "has no expected_pattern" in " ".join(refused.output.split())
    assert _listed(root) == []

    assert runner.invoke(app, [*add, "--pattern", "x"]).exit_code == 0
    assert [r["expected_pattern"] for r in _listed(root)] == ["x"]


def test_a_codegraph_query_stored_without_a_pattern_never_blocks_a_later_write(
    tmp_path: Path,
) -> None:
    root = tmp_path / "estate"
    root.mkdir()
    assert runner.invoke(app, ["init", "-C", str(root)]).exit_code == 0
    assert _import(root, tmp_path, []) == ""
    create_container(root).get(NodeRepository).add_verification(
        NodeVerification(
            node_id="P-1", verification_type=VerificationType.CODEGRAPH_QUERY, target_path="x"
        )
    )

    added = runner.invoke(
        app, ["verify", "add", "P-1", "--type", "file_exists", "--target", "f", "-C", str(root)]
    )
    assert added.exit_code == 0, added.output
