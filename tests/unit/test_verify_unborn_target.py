"""`tm verify run` with no `--ref` reads where the chain lands; a target the first landing has not
created yet reads as the repository's default branch."""

from pathlib import Path

import pytest
from lifecycle_estate import add, commit, git, make_estate, on_branch, push_main

from taskmanager.core.enums import NodeKind, VerificationType
from taskmanager.core.models import NodeVerification
from taskmanager.engine.claims import Claims
from taskmanager.engine.config import ProjectConfig, RepoConfig

TARGET = "feature/notify"


def estate(tmp_path: Path, remote: str | None = "origin") -> Claims:
    claims = make_estate(tmp_path, config=ProjectConfig(repos={"api": RepoConfig(remote=remote)}))
    add(claims, "S", NodeKind.SPEC)
    add(claims, "T1", parent="S")
    claims.ops.update_node("S", frontmatter_set={"land_on": TARGET})
    for check in (
        NodeVerification(
            node_id="T1", verification_type=VerificationType.FILE_EXISTS, target_path="api/f.py"
        ),
        NodeVerification(
            node_id="T1",
            verification_type=VerificationType.TEST_COMMAND,
            target_path="ref",
            expected_pattern='echo "$TM_VERIFY_REF"',
        ),
    ):
        claims.nodes.add_verification(check)
    return claims


def messages(claims: Claims) -> tuple[bool, list[str]]:
    passed, results = claims.ops.run_verifications("T1")
    return passed, [r.message for r in results]


def test_a_target_not_on_the_remote_yet_reads_the_default_branch(tmp_path: Path) -> None:
    claims = estate(tmp_path)
    push_main(claims.root / "api", "f.py", "x\n")

    passed, found = messages(claims)

    assert passed, found
    assert found == ["File exists (git ref origin/main in api)", "origin/main"]


def test_a_target_not_on_the_remote_yet_still_fails_for_the_missing_deliverable(
    tmp_path: Path,
) -> None:
    claims = estate(tmp_path)

    passed, found = messages(claims)

    assert not passed
    assert found[0] == "File api/f.py does not exist (git ref origin/main in api)"
    assert "fetch" not in found[0]


def test_a_target_on_the_remote_is_read_as_itself(tmp_path: Path) -> None:
    claims = estate(tmp_path)
    api = claims.root / "api"
    on_branch(api, TARGET, "f.py", "x\n")
    git(api, "push", "-q", "origin", f"{TARGET}:{TARGET}")
    git(api, "update-ref", "-d", f"refs/remotes/origin/{TARGET}")

    passed, found = messages(claims)

    assert passed, found
    assert found == [
        f"File exists (git ref origin/{TARGET} in api)",
        f"origin/{TARGET}",
    ]


def test_a_target_not_created_yet_in_a_repo_with_no_remote_reads_the_local_default_branch(
    tmp_path: Path,
) -> None:
    claims = estate(tmp_path, remote=None)
    api = claims.root / "api"
    git(api, "remote", "remove", "origin")
    commit(api, "f.py", "x\n", "f")

    passed, found = messages(claims)

    assert passed, found
    assert found == ["File exists (git ref main in api)", "main"]


@pytest.mark.parametrize("remote", ["origin", None])
def test_a_target_equal_to_the_default_branch_is_read_as_itself(
    tmp_path: Path, remote: str | None
) -> None:
    claims = estate(tmp_path, remote=remote)
    claims.ops.update_node("S", frontmatter_set={"land_on": "main"})
    api = claims.root / "api"
    if remote is None:
        git(api, "remote", "remove", "origin")
        commit(api, "f.py", "x\n", "f")
    else:
        push_main(api, "f.py", "x\n")

    passed, found = messages(claims)

    assert passed, found
    assert found[1] == (f"{remote}/main" if remote else "main")


@pytest.mark.parametrize("configured", [None, "origin"], ids=["remote-null", "origin-missing"])
def test_with_no_remote_a_task_is_read_on_its_own_branch_once_it_has_one(
    tmp_path: Path, configured: str | None
) -> None:
    claims = estate(tmp_path, remote=configured)
    api = claims.root / "api"
    git(api, "remote", "remove", "origin")
    on_branch(api, "tm/T1", "f.py", "x\n", base="main")

    passed, found = messages(claims)

    assert passed, found
    assert found == ["File exists (git ref tm/T1 in api)", "tm/T1"]


def test_with_a_remote_a_task_is_read_on_its_own_branch_until_it_lands(tmp_path: Path) -> None:
    claims = estate(tmp_path)
    api = claims.root / "api"
    on_branch(api, "tm/T1", "f.py", "x\n")

    assert messages(claims) == (True, ["File exists (git ref tm/T1 in api)", "tm/T1"])

    git(api, "push", "-q", "origin", f"tm/T1:{TARGET}")
    git(api, "fetch", "-q", "origin")

    assert messages(claims) == (
        True,
        [f"File exists (git ref origin/{TARGET} in api)", f"origin/{TARGET}"],
    )


def test_a_landing_ref_that_does_not_resolve_is_named_with_the_own_branch_tried(
    tmp_path: Path,
) -> None:
    claims = make_estate(
        tmp_path, config=ProjectConfig(repos={"api": RepoConfig(default_branch="trunk")})
    )
    add(claims, "T1")
    claims.nodes.add_verification(
        NodeVerification(
            node_id="T1",
            verification_type=VerificationType.TEST_COMMAND,
            target_path="ref",
            expected_pattern='git -C api rev-parse --verify --quiet "$TM_VERIFY_REF"',
        )
    )

    passed, found = messages(claims)

    assert not passed
    assert found == [
        (
            "Test command failed with exit code 1; no ref to read T1 at in api: neither tm/T1 "
            "nor origin/trunk resolves, so name one with --ref"
        )
    ]
    _, asked = claims.ops.run_verifications("T1", ref="nowhere")
    assert [r.message for r in asked] == ["Test command failed with exit code 1"]

    on_branch(claims.root / "api", "tm/T1", "f.py", "x\n")

    assert messages(claims) == (True, [git(claims.root / "api", "rev-parse", "tm/T1")])


def test_a_ref_asked_for_is_read_whatever_the_remote(tmp_path: Path) -> None:
    claims = estate(tmp_path)
    on_branch(claims.root / "api", "tm/T1", "f.py", "x\n")

    passed, results = claims.ops.run_verifications("T1", ref="tm/T1")

    assert passed
    assert [r.message for r in results] == ["File exists (git ref tm/T1 in api)", "tm/T1"]


def test_with_no_remote_a_plan_s_command_reads_the_plan_s_own_branch(tmp_path: Path) -> None:
    claims = estate(tmp_path, remote=None)
    api = claims.root / "api"
    git(api, "remote", "remove", "origin")
    add(claims, "P", NodeKind.PLAN, parent="S")
    claims.nodes.add_verification(
        NodeVerification(
            node_id="P",
            verification_type=VerificationType.TEST_COMMAND,
            target_path="ref",
            expected_pattern='echo "$TM_VERIFY_REF"',
        )
    )
    add(claims, "T2", parent="P")
    on_branch(api, "tm/P", "g.py", "x\n", base="main")

    passed, results = claims.ops.run_verifications("P")

    assert passed
    assert [r.message for r in results] == ["tm/P"]


def test_a_plan_with_no_repository_yet_still_runs_its_commands(tmp_path: Path) -> None:
    claims = estate(tmp_path, remote=None)
    add(claims, "P", NodeKind.PLAN, parent="S")
    for command in ("true", "false"):
        claims.nodes.add_verification(
            NodeVerification(
                node_id="P",
                verification_type=VerificationType.TEST_COMMAND,
                target_path=command,
                expected_pattern=command,
            )
        )

    passed, results = claims.ops.run_verifications("P")

    assert (passed, [r.message for r in results]) == (
        False,
        [
            "Test command passed",
            (
                "Test command failed with exit code 1; no ref to read P at in .: neither tm/P nor "
                "origin/main resolves, so name one with --ref"
            ),
        ],
    )
