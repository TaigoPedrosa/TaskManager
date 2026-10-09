"""A repository's `default_branch` moves only while no branch cut from it would land elsewhere."""

from pathlib import Path

import pytest
from lifecycle_estate import add, branch_at, git, make_estate, on_branch, push_main, stored
from typer.testing import CliRunner

from taskmanager.cli.main import app as cli_app
from taskmanager.core.enums import NodeKind, VerificationType
from taskmanager.core.models import NodeVerification
from taskmanager.core.status import Merge, Status
from taskmanager.engine.claims import Claims
from taskmanager.engine.config import ConfigStore, ProjectConfig, RepoConfig

REFUSED_UNTIL_REOPENED = "reopen it with --new-branch before changing where it lands"


def tm(claims: Claims, *args: str) -> tuple[int, str]:
    res = CliRunner().invoke(cli_app, [*args, "-C", str(claims.root)])
    return res.exit_code, res.output


def default_branch(claims: Claims, repo: str = "api") -> str:
    return ConfigStore(claims.root).branches().default_branch(repo)


def fork_develop(repo: Path) -> None:
    """`develop` and `main` on origin, each one commit past where they forked."""
    on_branch(repo, "develop", "develop.txt", "develop")
    git(repo, "push", "-q", "origin", "develop:develop")
    push_main(repo, "moved.txt", "main moved on")


def estate(
    tmp_path: Path,
    default: str = "main",
    *,
    land_on: str | None = None,
    status: Status = Status.READY,
    cut: bool = True,
) -> Claims:
    """S > T, T landing where S lands, its branch cut from `default` when `cut`."""
    config = ProjectConfig(repos={"api": RepoConfig(default_branch=default)})
    claims = make_estate(tmp_path, config=config)
    add(claims, "S", NodeKind.SPEC)
    if land_on is not None:
        claims.ops.update_node("S", frontmatter_set={"land_on": land_on})
    add(claims, "T", parent="S", merge=Merge.SPEC, status=status)
    api = claims.root / "api"
    fork_develop(api)
    if cut:
        branch_at(api, "tm/T", f"origin/{default}")
    return claims


MOVES = [
    ("main", ("set", "repos", "{api: {default_branch: develop}}"), "develop"),
    ("develop", ("set", "repos", "{api: {gates: {main: {command: make}}}}"), "main"),
    ("develop", ("unset", "repos"), "main"),
]
MOVE_IDS = ["set-default-branch", "whole-repos-without-it", "unset-repos"]


@pytest.mark.parametrize(("default", "change", "moved_to"), MOVES, ids=MOVE_IDS)
def test_moving_the_default_branch_under_a_cut_branch_is_refused_naming_the_node(
    tmp_path: Path, default: str, change: tuple[str, ...], moved_to: str
) -> None:
    claims = estate(tmp_path, default)

    code, output = tm(claims, "config", *change)

    assert code == 1
    assert f"T: its branch exists and was not cut from {moved_to}" in output
    assert REFUSED_UNTIL_REOPENED in output
    assert "Traceback" not in output
    assert default_branch(claims) == default


def test_a_refused_default_branch_moves_once_the_node_is_reopened_on_a_new_branch(
    tmp_path: Path,
) -> None:
    claims = estate(tmp_path)
    # Keeps S open while T is set aside.
    add(claims, "U", parent="S")
    move = ("config", "set", "repos", "{api: {default_branch: develop}}")
    assert tm(claims, *move)[0] == 1

    assert tm(claims, "task", "defer", "T", "--note", "retarget")[0] == 0
    assert tm(claims, "task", "reopen", "T", "--note", "retarget", "--new-branch")[0] == 0

    assert tm(claims, *move)[0] == 0
    assert stored(claims, "T").status == Status.READY
    assert default_branch(claims) == "develop"


@pytest.mark.parametrize(
    ("land_on", "status", "cut"),
    [
        ("release/2", Status.READY, True),
        (None, Status.READY, False),
        (None, Status.COMPLETED, True),
        (None, Status.SUPERSEDED, True),
    ],
    ids=["spec-names-its-target", "no-branch-cut", "completed", "superseded"],
)
def test_moving_the_default_branch_is_accepted_when_nothing_cut_from_it_still_lands_there(
    tmp_path: Path, land_on: str | None, status: Status, cut: bool
) -> None:
    claims = estate(tmp_path, land_on=land_on, status=status, cut=cut)

    code, output = tm(claims, "config", "set", "repos", "{api: {default_branch: develop}}")

    assert code == 0, output
    assert default_branch(claims) == "develop"


def multi_repo_estate(tmp_path: Path) -> Claims:
    """S > P > (A in api, W in web), both landing on P; P's branch cut in web alone, where
    `develop` and `main` have forked."""
    claims = make_estate(tmp_path, repos=("api", "web"), config=ProjectConfig())
    add(claims, "S", NodeKind.SPEC)
    add(claims, "P", NodeKind.PLAN, parent="S")
    add(claims, "A", parent="P", merge=Merge.PARENT)
    add(claims, "W", parent="P", repo="web", merge=Merge.PARENT)
    web = claims.root / "web"
    fork_develop(web)
    branch_at(web, "tm/P")
    return claims


def test_moving_a_repository_s_default_branch_refuses_a_container_cut_there(
    tmp_path: Path,
) -> None:
    claims = multi_repo_estate(tmp_path)

    code, output = tm(claims, "config", "set", "repos", "{web: {default_branch: develop}}")

    assert code == 1
    assert "P: its branch exists and was not cut from develop" in output
    assert default_branch(claims, "web") == "main"


def test_moving_a_repository_s_default_branch_ignores_a_branch_cut_in_another_one(
    tmp_path: Path,
) -> None:
    claims = multi_repo_estate(tmp_path)

    code, output = tm(claims, "config", "set", "repos", "{api: {default_branch: develop}}")

    assert code == 0, output
    assert default_branch(claims, "api") == "develop"


def test_moving_a_repository_s_default_branch_ignores_a_node_with_no_repository(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path)
    add(claims, "N", repo=None)
    root = claims.root
    git(root, "init", "-q", "-b", "main")
    who = ("-c", "user.name=tm", "-c", "user.email=tm@example.com", "-c", "commit.gpgsign=false")
    git(root, *who, "commit", "-q", "--allow-empty", "-m", "init")
    branch_at(root, "tm/N", "main")

    code, output = tm(claims, "config", "set", "repos", "{api: {default_branch: develop}}")

    assert code == 0, output
    assert default_branch(claims) == "develop"


def test_a_config_write_reaches_its_guard_only_when_a_default_branch_moves(
    tmp_path: Path,
) -> None:
    store_root = tmp_path / "root"
    (store_root / ".taskmanager").mkdir(parents=True)
    seen: list[tuple[ProjectConfig, ProjectConfig]] = []
    store = ConfigStore(store_root, guard=lambda before, after: seen.append((before, after)))

    store.set("repos", "{api: {gates: {main: {command: make}}}}")
    store.set("lease_ttl", "600")
    assert seen == []

    store.set("repos", "{api: {default_branch: develop}}")
    assert [(b.default_branch("api"), a.default_branch("api")) for b, a in seen] == [
        ("main", "develop")
    ]


def crossing_estate(tmp_path: Path, dep: Status, owner: Status) -> Claims:
    """W in web depends on A in api, both landing on main, with no branch cut."""
    claims = make_estate(tmp_path, repos=("api", "web"), config=ProjectConfig())
    add(claims, "A", status=dep)
    add(claims, "W", repo="web", status=owner, depends=("A",))
    return claims


@pytest.mark.parametrize(
    ("dep", "owner", "refused"),
    [
        (Status.READY, Status.READY, True),
        (Status.COMPLETED, Status.READY, False),
        (Status.READY, Status.COMPLETED, False),
    ],
    ids=["both-still-land", "dependency-landed", "dependent-landed"],
)
def test_moving_a_default_branch_refuses_splitting_a_dependency_still_to_land_across_targets(
    tmp_path: Path, dep: Status, owner: Status, refused: bool
) -> None:
    claims = crossing_estate(tmp_path, dep, owner)

    code, output = tm(claims, "config", "set", "repos", "{api: {default_branch: trunk}}")

    crossing = "W: depends on A, which lands on trunk, but W lands on main"
    assert (code, crossing in output) == ((1, True) if refused else (0, False)), output
    assert default_branch(claims) == ("main" if refused else "trunk")


@pytest.mark.parametrize(
    ("status", "land_on", "refused"),
    [
        (Status.READY, None, True),
        (Status.COMPLETED, None, False),
        (Status.READY, "release/2", False),
    ],
    ids=["moved-off-main", "landed", "target-unmoved"],
)
def test_moving_a_default_branch_refuses_moving_a_check_naming_origin_main_off_main(
    tmp_path: Path, status: Status, land_on: str | None, refused: bool
) -> None:
    claims = make_estate(tmp_path)
    add(claims, "S", NodeKind.SPEC)
    if land_on is not None:
        claims.ops.update_node("S", frontmatter_set={"land_on": land_on})
    add(claims, "T", parent="S", status=status)
    claims.nodes.add_verification(
        NodeVerification(
            node_id="T",
            verification_type=VerificationType.TEST_COMMAND,
            target_path="",
            expected_pattern="git diff --quiet origin/main",
        )
    )

    code, output = tm(claims, "config", "set", "repos", "{api: {default_branch: trunk}}")

    rule = "T: lands on its target trunk but a test_command names origin/main"
    assert (code, rule in output) == ((1, True) if refused else (0, False)), output
    assert default_branch(claims) == ("main" if refused else "trunk")
