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
