import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from taskmanager.cli.main import app
from taskmanager.core.enums import VerificationType
from taskmanager.core.models import NodeVerification
from taskmanager.engine import doctor
from taskmanager.engine.config import ConfigError, ConfigStore
from taskmanager.engine.verification import VerificationEngine

runner = CliRunner()

# `init` marks the exported tree indexed and `query` answers for any search.
STUB = """#!/bin/sh
if [ "$1" = init ]; then for arg; do dir=$arg; done; mkdir -p "$dir/.codegraph"; fi
echo '[]'
"""
NOT_AN_OBJECT = "has codegraph_query_json '[\"kind\"]', which is not a JSON object of query flags"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _tool(bin_dir: Path, script: str) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    path = bin_dir / "codegraph"
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A tm root whose target repo `app` has a codegraph index, with a stub codegraph on PATH."""
    _tool(tmp_path / "bin", STUB)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:{os.environ['PATH']}")
    repo = tmp_path / "root" / "app"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / ".codegraph").mkdir()
    (repo / ".codegraph" / "codegraph.db").touch()
    return tmp_path / "root"


def _commits(root: Path, count: int) -> list[str]:
    repo = root / "app"
    shas = []
    for i in range(count):
        (repo / f"f{i}.txt").write_text(f"{i}\n", encoding="utf-8")
        _git(repo, "add", f"f{i}.txt")
        _git(repo, "commit", "-q", "-m", f"f{i}")
        shas.append(_git(repo, "rev-parse", "HEAD"))
    return shas


def _verify(root: Path, sha: str) -> None:
    check = NodeVerification(
        node_id="T1",
        verification_type=VerificationType.CODEGRAPH_QUERY,
        target_path="f",
        expected_pattern=r"\[\]",
    )
    result = VerificationEngine(root).verify_assertion(check, target_repo="app", ref=sha)
    assert result.passed is True, result.message


def _cached(root: Path) -> set[str]:
    cache = root / ".taskmanager" / "cache" / "codegraph"
    return {p.name for p in cache.iterdir() if p.is_dir()}


def test_verifying_more_commits_than_the_cache_holds_keeps_the_most_recent(root: Path) -> None:
    ConfigStore(root).set("codegraph.cache_commits", "2")
    first, second, third = _commits(root, 3)

    for sha in (first, second, third):
        _verify(root, sha)

    assert _cached(root) == {second, third}


def test_reading_a_cached_commit_keeps_it_over_one_read_before_it(root: Path) -> None:
    ConfigStore(root).set("codegraph.cache_commits", "2")
    first, second, third = _commits(root, 3)

    for sha in (first, second, first, third):
        _verify(root, sha)

    assert _cached(root) == {first, third}


def test_the_cache_holds_three_commits_by_default(root: Path) -> None:
    shas = _commits(root, 4)

    for sha in shas:
        _verify(root, sha)

    assert _cached(root) == set(shas[1:])


def test_a_cache_of_no_commits_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="codegraph.cache_commits"):
        ConfigStore(tmp_path).set("codegraph.cache_commits", "0")


# -- query flags on `tm verify add` ------------------------------------------------------------


@pytest.fixture
def estate(tmp_path: Path) -> Path:
    """An estate holding task P-1 with no verifications."""
    path = tmp_path / "estate"
    path.mkdir()
    assert runner.invoke(app, ["init", "-C", str(path)]).exit_code == 0
    assert _import(path, []).exit_code == 0
    return path


def _import(estate: Path, checks: list[dict[str, str]]) -> Result:
    doc = estate.parent / "doc.json"
    task = {"id": "P-1", "title": "a", "verifications": checks}
    doc.write_text(
        json.dumps({"plans": [{"id": "P", "title": "P", "tasks": [task]}]}), encoding="utf-8"
    )
    return runner.invoke(app, ["import", "-f", str(doc), "-C", str(estate)])


def _listed(estate: Path) -> list[dict[str, str]]:
    listed = runner.invoke(app, ["verify", "list", "P-1", "--json", "-C", str(estate)])
    assert listed.exit_code == 0, listed.output
    rows: list[dict[str, str]] = json.loads(listed.stdout)
    return rows


def _add(estate: Path, query_json: str) -> list[str]:
    return [
        *("verify", "add", "P-1", "--type", "codegraph_query", "--target", "x"),
        *("--pattern", "x", "--query-json", query_json, "-C", str(estate)),
    ]


def test_verify_add_refuses_query_flags_that_are_not_an_object_as_import_does(
    estate: Path,
) -> None:
    check = {"type": "codegraph_query", "target_path": "x", "expected_pattern": "x"}
    imported = _import(estate, [{**check, "codegraph_query_json": '["kind"]'}])
    added = runner.invoke(app, _add(estate, '["kind"]'))

    assert imported.exit_code == 1
    assert f"P-1: codegraph_query 'x' {NOT_AN_OBJECT}" in " ".join(imported.output.split())
    assert added.exit_code == 1
    assert f"P-1: codegraph_query 'x' {NOT_AN_OBJECT}" in " ".join(added.output.split())
    assert _listed(estate) == []


def test_verify_add_refuses_query_flags_that_are_not_json(estate: Path) -> None:
    added = runner.invoke(app, _add(estate, '{"kind"'))

    assert added.exit_code == 1
    assert "which is not a JSON object of query flags" in " ".join(added.output.split())
    assert _listed(estate) == []


def test_verify_add_stores_query_flags_that_list_reads_back(estate: Path) -> None:
    flags = '{"kind": "function", "limit": 20}'

    added = runner.invoke(app, _add(estate, flags))

    assert added.exit_code == 0, added.output
    assert [r["codegraph_query_json"] for r in _listed(estate)] == [flags]


def test_query_flags_survive_an_export_and_a_restore(estate: Path, tmp_path: Path) -> None:
    flags = '{"kind": "function"}'
    assert runner.invoke(app, _add(estate, flags)).exit_code == 0
    exported = tmp_path / "export"
    assert runner.invoke(app, ["export", str(exported), "-C", str(estate)]).exit_code == 0
    restored = tmp_path / "restored"

    res = runner.invoke(app, ["restore", str(exported), "-C", str(restored)])

    assert res.exit_code == 0, res.output
    assert [r["codegraph_query_json"] for r in _listed(restored)] == [flags]


# -- the suite's own codegraph -----------------------------------------------------------------


def test_a_codegraph_the_test_did_not_put_on_path_is_never_found_or_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with tempfile.TemporaryDirectory() as outside:
        calls = Path(outside) / "calls.log"
        _tool(Path(outside) / "bin", f'#!/bin/sh\necho "$@" >> "{calls}"\necho 1.6.0\n')
        monkeypatch.setenv("PATH", f"{Path(outside) / 'bin'}:{os.environ['PATH']}")

        facts = {f.name: f for f in doctor.facts(tmp_path)}

        assert shutil.which("codegraph") is None
        assert facts["codegraph"].ok is False
        assert not calls.exists()


def test_a_codegraph_the_test_put_on_path_is_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _tool(tmp_path / "bin", "#!/bin/sh\necho 1.6.0\n")
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:{os.environ['PATH']}")

    assert shutil.which("codegraph") == str(stub)
    assert {f.name: f for f in doctor.facts(tmp_path)}["codegraph"].found == "1.6.0"
