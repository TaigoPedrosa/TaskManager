import shutil
from pathlib import Path

import pytest
from lifecycle_estate import (
    add,
    attach_landing,
    commit,
    git,
    make_estate,
    stored,
)
from typer.testing import CliRunner

from taskmanager.cli.main import app as cli_app
from taskmanager.core.enums import NodeKind, VerificationType
from taskmanager.core.models import NodeVerification
from taskmanager.core.status import Action, JobState, Status
from taskmanager.engine import gates
from taskmanager.engine.claims import Claims
from taskmanager.engine.config import ConfigStore, Gate, ProjectConfig, RepoConfig
from taskmanager.engine.operations import OperationError
from taskmanager.engine.setup import remote_head
from taskmanager.engine.stepgraph import SnapNode, Snapshot
from taskmanager.engine.validation import validate

TRUE = Gate(command="true", timeout=60)


class NoBranches:
    def branch_exists(self, node_id: str) -> bool:
        return False

    def base_matches(self, node_id: str, new_target: str, new_top: str) -> bool:
        return True


WRAPPER = '#!/bin/sh\nprintf "%s\\n" "$*" >> "{log}"\nexec "{git}" "$@"\n'


def estate(tmp_path: Path, remote: str | None) -> Claims:
    """An estate holding T1, a task with review off, in `api` with `remote` configured."""
    repos = {"api": RepoConfig(remote=remote, gates={"main": TRUE})}
    claims = make_estate(tmp_path, config=ProjectConfig(repos=repos))
    add(claims, "T1", review=False)
    return claims


def without_remotes(api: Path) -> None:
    git(api, "remote", "remove", "origin")
    assert git(api, "remote") == ""
    assert git(api, "for-each-ref", "refs/remotes") == ""


def logged_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every git command run from here on, one line of arguments each."""
    real = shutil.which("git")
    assert real is not None
    bin_dir, log = tmp_path / "bin", tmp_path / "git.log"
    bin_dir.mkdir()
    wrapper = bin_dir / "git"
    wrapper.write_text(WRAPPER.format(log=log, git=real))
    wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{Path(real).parent}")
    return log


def subcommands(log: Path) -> list[list[str]]:
    """Each logged command from its subcommand on, past `-C <dir>` and `-c <key=value>`."""
    found = []
    for line in log.read_text().splitlines():
        args = line.split()
        while args and args[0] in ("-C", "-c"):
            args = args[2:]
        found.append(args)
    return found


def verified(claims: Claims, ref: str) -> None:
    """T1 checks its file and that `TM_VERIFY_REF` is `ref`."""
    claims.nodes.add_verification(
        NodeVerification(
            node_id="T1",
            verification_type=VerificationType.FILE_EXISTS,
            target_path="api/feature.py",
        )
    )
    claims.nodes.add_verification(
        NodeVerification(
            node_id="T1",
            verification_type=VerificationType.TEST_COMMAND,
            target_path="verify-ref",
            expected_pattern=f'test "$TM_VERIFY_REF" = {ref}',
        )
    )


def implement_and_land(claims: Claims) -> JobState:
    claimed = claims.start("T1", "builder", "s1")
    assert claimed.action == Action.IMPLEMENT and claimed.worktree is not None, claimed.reason
    commit(Path(claimed.worktree), "feature.py", "x = 1\n", "feature")
    claims.complete("T1", token=claimed.token)
    landing = attach_landing(claims)
    merge = claims.start("T1", "merger", "s1")
    assert merge.action == Action.MERGE and merge.job is not None, merge.reason
    return landing.run(merge.job)


def test_a_repo_with_no_remote_claims_lands_and_verifies_on_its_local_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claims = estate(tmp_path, None)
    api = claims.root / "api"
    without_remotes(api)
    verified(claims, "main")
    log = logged_git(tmp_path, monkeypatch)

    assert implement_and_land(claims) == JobState.SUCCEEDED

    assert stored(claims, "T1").status == Status.COMPLETED
    assert git(api, "log", "-1", "--format=%s", "main") == "merge(T1): land tm/T1 on main"
    assert git(api, "show", "main:feature.py") == "x = 1"
    passed, results = claims.ops.run_verifications("T1")
    assert passed, [r.message for r in results]
    assert "(git ref main in api)" in results[0].message
    ran = subcommands(log)
    assert [c for c in ran if c[0] in ("fetch", "pull", "remote")] == []
    assert [c for c in ran if c[0] in ("push", "ls-remote") and "." not in c] == []
    assert any(c[0] == "push" for c in ran)
    assert git(api, "remote") == ""
    assert git(api, "for-each-ref", "refs/remotes") == ""


def test_a_no_remote_landing_moves_a_clean_checkout_of_its_target_with_its_files(
    tmp_path: Path,
) -> None:
    claims = estate(tmp_path, None)
    api = claims.root / "api"
    without_remotes(api)
    git(api, "checkout", "-q", "main")

    assert implement_and_land(claims) == JobState.SUCCEEDED

    assert (api / "feature.py").read_text() == "x = 1\n"
    assert git(api, "status", "--porcelain") == ""


def test_a_repo_whose_remote_is_upstream_lands_by_pushing_there_and_never_touches_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    claims = estate(tmp_path, "upstream")
    api = claims.root / "api"
    upstream = tmp_path / "origins" / "api-upstream.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(upstream))
    git(api, "remote", "add", "upstream", str(upstream))
    git(api, "push", "-q", "upstream", "HEAD:main")
    git(api, "fetch", "-q", "upstream")
    origin_main = git(api, "ls-remote", "origin", "refs/heads/main")
    verified(claims, "upstream/main")
    log = logged_git(tmp_path, monkeypatch)

    state = implement_and_land(claims)

    assert state == JobState.SUCCEEDED
    assert git(upstream, "log", "-1", "--format=%s", "main") == "merge(T1): land tm/T1 on main"
    assert git(api, "ls-remote", "origin", "refs/heads/main") == origin_main
    ran = subcommands(log)
    assert ["push", "-q", "upstream", "HEAD:refs/heads/main"] in ran
    assert [c for c in ran if "origin" in " ".join(c)] == [
        ["ls-remote", "origin", "refs/heads/main"]
    ]


def test_a_configured_remote_the_repo_does_not_have_is_refused_naming_the_key(
    tmp_path: Path,
) -> None:
    claims = estate(tmp_path, "nowhere")

    with pytest.raises(OperationError, match=r"repos\.api\.remote names 'nowhere'") as refused:
        claims.start("T1", "builder", "s1")

    assert "remote -v" in str(refused.value)


def test_a_verification_reading_a_configured_remote_the_repo_lacks_fails_naming_the_key(
    tmp_path: Path,
) -> None:
    claims = estate(tmp_path, "nowhere")
    verified(claims, "nowhere/main")

    passed, results = claims.ops.run_verifications("T1")

    assert not passed
    assert "repos.api.remote names 'nowhere'" in results[0].message


def test_the_remote_key_takes_null_for_a_repo_with_no_remote(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path)

    store.set("repos.api.remote", "null")
    assert store.branches().remote("api") is None
    store.set("repos.api.remote", "upstream")
    assert store.branches().remote("api") == "upstream"
    assert ProjectConfig().remote("api") == "origin"


def test_a_remote_name_git_refuses_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not a remote name git accepts"):
        ConfigStore(tmp_path).set("repos.api.remote", "a..b")


@pytest.mark.parametrize(("remote", "refused"), [("origin", False), (None, True)])
def test_a_literal_origin_main_check_on_main_is_refused_only_where_origin_is_not_the_remote(
    remote: str | None, refused: bool
) -> None:
    config = ProjectConfig(repos={"api": RepoConfig(remote=remote)})
    node = SnapNode("T", NodeKind.TASK, repo="api", literal_origin_main=True)
    after = Snapshot({"T": node}, [], config=config)
    found = [r.rule for r in validate(Snapshot({}, []), after, {"T"}, NoBranches())]
    assert found == ([5] if refused else [])


def test_the_default_branch_of_a_repo_with_no_remote_is_the_one_checked_out(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "r"
    git(tmp_path, "init", "-q", "-b", "trunk", str(repo))

    assert remote_head(repo, None) == "trunk"


def test_a_parked_red_target_in_a_repo_with_no_remote_clears_once_its_local_branch_moves(
    tmp_path: Path,
) -> None:
    claims = estate(tmp_path, None)
    api = claims.root / "api"
    without_remotes(api)
    sha = git(api, "rev-parse", "main")
    args = ["red-target", "--root", str(claims.root), "--repo", "api", "--sha", sha]
    args += ["--template-hash", "h", "--target", "main", "--remote"]

    assert gates.main(args) == 1
    git(api, "commit", "-q", "--allow-empty", "-m", "moved")
    assert gates.main(args) == 0


def test_a_default_branch_move_reads_where_a_no_remote_repo_cut_its_branch_locally(
    tmp_path: Path,
) -> None:
    """The clone keeps a stale `origin/main`; with no remote configured, the cut is read on the
    local `main`, which `develop` lacks a commit of."""
    claims = estate(tmp_path, None)
    api = claims.root / "api"
    git(api, "branch", "-q", "develop", "main")
    commit(api, "local.txt", "local\n", "local main moves on")
    git(api, "branch", "-q", "--no-track", "tm/T1", "main")

    result = CliRunner().invoke(
        cli_app, ["config", "set", "repos.api.default_branch", "develop", "-C", str(claims.root)]
    )

    assert result.exit_code == 1
    assert "T1: its branch exists and was not cut from develop" in result.output
