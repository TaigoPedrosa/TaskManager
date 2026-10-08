"""Where a chain lands at the top: its spec's `land_on`, else its repository's `default_branch`."""

from pathlib import Path

import pytest
from lifecycle_estate import add, branch_at, git, make_estate, on_branch, push_main, stored
from typer.testing import CliRunner

from taskmanager.cli.main import app as cli_app
from taskmanager.core.enums import NodeKind
from taskmanager.core.status import Action, Merge, Status
from taskmanager.engine.claims import Claims
from taskmanager.engine.config import ProjectConfig, RepoConfig
from taskmanager.engine.operations import OperationError


def tm(claims: Claims, *args: str) -> tuple[int, str]:
    res = CliRunner().invoke(cli_app, [*args, "-C", str(claims.root)])
    return res.exit_code, res.output


def push_branch(repo: Path, branch: str, base: str = "origin/main") -> str:
    sha = on_branch(repo, branch, f"{branch.replace('/', '-')}.txt", branch, base=base)
    git(repo, "push", "-q", "origin", f"{branch}:{branch}")
    git(repo, "fetch", "-q", "origin")
    return sha


def estate(tmp_path: Path, default: str | None = None) -> Claims:
    """S > P > (T, C): T lands where its spec lands, C on P's branch."""
    config = ProjectConfig(repos={"api": RepoConfig(default_branch=default)}) if default else None
    claims = make_estate(tmp_path, config=config)
    add(claims, "S", NodeKind.SPEC)
    add(claims, "P", NodeKind.PLAN, parent="S")
    add(claims, "T", parent="P", merge=Merge.SPEC)
    add(claims, "C", parent="P", merge=Merge.PARENT)
    return claims


@pytest.mark.parametrize(
    ("land_on", "default", "top"),
    [
        (None, None, "main"),
        ("release/2", None, "release/2"),
        (None, "develop", "develop"),
        ("release/2", "develop", "release/2"),
    ],
    ids=["neither", "land-on", "default-branch", "land-on-over-default-branch"],
)
def test_landing_branch_reads_the_spec_s_land_on_else_the_repository_s_default_branch(
    tmp_path: Path, land_on: str | None, default: str | None, top: str
) -> None:
    claims = estate(tmp_path, default)
    if land_on is not None:
        claims.ops.update_node("S", frontmatter_set={"land_on": land_on})
    ops = claims.ops

    assert [ops.landing_branch(n) for n in ("S", "P", "T", "C")] == [top] * 4
    assert (ops.target_of("T"), ops.target_ref("T")) == (top, f"origin/{top}")
    assert (ops.target_of("C"), ops.target_ref("C")) == ("tm/P", "tm/P")
    assert (ops.target_of("P"), ops.target_of("S")) == (top, top)
    snap = claims.snapshots.build()
    assert [snap.nodes[n].top for n in ("S", "P", "T", "C")] == [top] * 4


def test_landing_branch_of_a_node_with_no_spec_is_its_repository_s_default_branch(
    tmp_path: Path,
) -> None:
    claims = make_estate(
        tmp_path, config=ProjectConfig(repos={"api": RepoConfig(default_branch="trunk")})
    )
    add(claims, "LONE")
    add(claims, "ELSEWHERE", repo="web")

    assert claims.ops.landing_branch("LONE") == "trunk"
    assert claims.ops.landing_branch("ELSEWHERE") == "main"
    assert claims.ops.landing_branch("ELSEWHERE", "api") == "trunk"


@pytest.mark.parametrize("default", [None, "develop"])
def test_a_claim_cuts_from_and_names_the_default_branch_while_the_target_is_not_on_origin(
    tmp_path: Path, default: str | None
) -> None:
    claims = estate(tmp_path, default)
    api = claims.root / "api"
    if default is not None:
        push_branch(api, default)
    claims.ops.update_node("S", frontmatter_set={"land_on": "release/2"})

    result = claims.start("T", "agent-1", "s1")

    assert (result.base, result.bases) == (default or "main", {"api": default or "main"})
    assert result.worktree is not None
    expected = git(api, "rev-parse", f"origin/{default or 'main'}")
    assert git(Path(result.worktree), "rev-parse", "HEAD") == expected


def test_a_review_claimed_before_the_target_is_on_origin_names_the_default_branch_as_base(
    tmp_path: Path,
) -> None:
    claims = estate(tmp_path)
    claims.ops.update_node("S", frontmatter_set={"land_on": "release/2"})
    add(claims, "R", parent="P", status=Status.IMPLEMENTED)
    branch_at(claims.root / "api", "tm/R")

    result = claims.start("R", "reviewer", "s1")

    assert (result.action, result.branch, result.base) == (Action.REVIEW, "tm/R", "main")


@pytest.mark.parametrize(("node", "base"), [("T", "release/2"), ("C", "tm/P")])
def test_a_claim_cuts_from_the_target_once_it_is_on_origin(
    tmp_path: Path, node: str, base: str
) -> None:
    claims = estate(tmp_path)
    api = claims.root / "api"
    release = push_branch(api, "release/2")
    claims.ops.update_node("S", frontmatter_set={"land_on": "release/2"})

    result = claims.start(node, "agent-1", "s1")

    assert result.base == base
    assert result.worktree is not None
    assert git(Path(result.worktree), "rev-parse", "HEAD") == release
    if node == "C":
        assert git(api, "rev-parse", "tm/P") == release


@pytest.mark.parametrize("node", ["P", "T"])
def test_task_update_refuses_land_on_anywhere_but_a_spec(tmp_path: Path, node: str) -> None:
    claims = estate(tmp_path)

    code, output = tm(claims, "task", "update", node, "--set", "land_on=release/2")

    assert code == 1
    assert f"{node}: land_on is set on a spec only" in output
    assert "land_on" not in stored(claims, node).frontmatter


@pytest.mark.parametrize("branch", ["bad name", "a..b", "HEAD", "-x"])
def test_task_update_refuses_a_land_on_git_rejects_as_a_branch_name(
    tmp_path: Path, branch: str
) -> None:
    claims = estate(tmp_path)

    code, output = tm(claims, "task", "update", "S", "--set", f"land_on={branch}")

    assert code == 1
    assert f"S: land_on '{branch}' is not a branch name git accepts" in output
    assert "land_on" not in stored(claims, "S").frontmatter


def test_task_update_takes_a_valid_land_on_on_a_spec(tmp_path: Path) -> None:
    claims = estate(tmp_path)

    assert tm(claims, "task", "update", "S", "--set", "land_on=release/2")[0] == 0

    assert stored(claims, "S").frontmatter["land_on"] == "release/2"


def test_changing_land_on_under_a_cut_branch_is_refused_until_it_is_reopened_on_a_new_branch(
    tmp_path: Path,
) -> None:
    claims = estate(tmp_path)
    api = claims.root / "api"
    push_branch(api, "release/2")
    push_main(api, "moved.txt", "main moved on")
    branch_at(api, "tm/T")

    code, output = tm(claims, "task", "update", "S", "--set", "land_on=release/2")

    assert code == 1
    assert "T: its branch exists and was not cut from release/2" in output
    assert "reopen it with --new-branch before changing where it lands" in output
    assert "land_on" not in stored(claims, "S").frontmatter

    assert tm(claims, "task", "defer", "T", "--note", "retarget")[0] == 0
    assert tm(claims, "task", "reopen", "T", "--note", "retarget", "--new-branch")[0] == 0
    assert tm(claims, "task", "update", "S", "--set", "land_on=release/2")[0] == 0
    assert stored(claims, "T").status == Status.READY
    assert stored(claims, "S").frontmatter["land_on"] == "release/2"


def test_changing_land_on_to_a_branch_not_on_origin_keeps_the_cut_and_is_accepted(
    tmp_path: Path,
) -> None:
    claims = estate(tmp_path)
    branch_at(claims.root / "api", "tm/T")

    claims.ops.update_node("S", frontmatter_set={"land_on": "release/9"})

    assert stored(claims, "S").frontmatter["land_on"] == "release/9"


def test_a_refused_land_on_change_raises_a_conflict(tmp_path: Path) -> None:
    claims = estate(tmp_path)
    api = claims.root / "api"
    push_branch(api, "release/2")
    push_main(api, "moved.txt", "main moved on")
    branch_at(api, "tm/T")

    with pytest.raises(OperationError) as exc:
        claims.ops.update_node("S", frontmatter_set={"land_on": "release/2"})

    assert exc.value.status_code == 409
