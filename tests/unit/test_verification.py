import subprocess
from pathlib import Path

import pytest

from taskmanager.core.enums import VerificationType
from taskmanager.core.models import NodeVerification
from taskmanager.engine.verification import (
    RETIRED_VERIFICATIONS,
    VerificationEngine,
    retired_verification,
)


def test_file_exists_verification(tmp_path: Path) -> None:
    test_file = tmp_path / "module.py"
    test_file.write_text("x = 1\n")

    ver = NodeVerification(
        node_id="AUTH-T01",
        verification_type=VerificationType.FILE_EXISTS,
        target_path="module.py",
    )
    engine = VerificationEngine(tmp_path)
    res = engine.verify_assertion(ver)

    assert res.passed is True
    assert res.message == "File exists"


def test_missing_file_verification(tmp_path: Path) -> None:
    ver = NodeVerification(
        node_id="AUTH-T01",
        verification_type=VerificationType.FILE_EXISTS,
        target_path="nonexistent.py",
    )
    engine = VerificationEngine(tmp_path)
    res = engine.verify_assertion(ver)

    assert res.passed is False
    assert "File nonexistent.py does not exist" in res.message


def test_file_absent_verification(tmp_path: Path) -> None:
    engine = VerificationEngine(tmp_path)

    ver = NodeVerification(
        node_id="AUTH-T01",
        verification_type=VerificationType.FILE_ABSENT,
        target_path="deleted.py",
    )
    res = engine.verify_assertion(ver)
    assert res.passed is True
    assert res.message == "File absent"

    existing = tmp_path / "deleted.py"
    existing.write_text("temporary")
    res_failed = engine.verify_assertion(ver)
    assert res_failed.passed is False
    assert "File deleted.py still exists" in res_failed.message


def test_test_command_verification(tmp_path: Path) -> None:
    engine = VerificationEngine(tmp_path)

    ver_success = NodeVerification(
        node_id="AUTH-T01",
        verification_type=VerificationType.TEST_COMMAND,
        target_path="",
        expected_pattern="python3 -c 'exit(0)'",
    )
    res_success = engine.verify_assertion(ver_success)
    assert res_success.passed is True

    ver_fail = NodeVerification(
        node_id="AUTH-T01",
        verification_type=VerificationType.TEST_COMMAND,
        target_path="",
        expected_pattern="python3 -c 'exit(1)'",
    )
    res_fail = engine.verify_assertion(ver_fail)
    assert res_fail.passed is False


def test_verify_all(tmp_path: Path) -> None:
    (tmp_path / "exists.py").write_text("x = 1\n")
    engine = VerificationEngine(tmp_path)
    verifications = [
        NodeVerification(
            node_id="T1",
            verification_type=VerificationType.FILE_EXISTS,
            target_path="exists.py",
        ),
        NodeVerification(
            node_id="T1",
            verification_type=VerificationType.FILE_EXISTS,
            target_path="missing.py",
        ),
    ]
    results = engine.verify_all(verifications)
    assert len(results) == 2
    assert results[0].passed is True
    assert results[1].passed is False


# -- git-ref resolution -----------------------------------------------------------------------


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )


def _commit(repo: Path, rel_path: str, content: str) -> None:
    target = repo / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _git(repo, "add", rel_path)
    _git(repo, "commit", "-q", "-m", f"add {rel_path}")


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """`<tmp_path>/myrepo`, with a bare `origin` and `committed.py` pushed to `origin/main`."""
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", str(origin)], capture_output=True, text=True, check=True
    )

    repo = tmp_path / "myrepo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "remote", "add", "origin", str(origin))
    _commit(repo, "committed.py", "def committed_symbol():\n    pass\n")
    _git(repo, "push", "-q", "-u", "origin", "main")
    return repo


def test_file_exists_reads_origin_main_not_the_working_tree(git_repo: Path, tmp_path: Path) -> None:
    (git_repo / "committed.py").unlink()  # gone on disk; still on origin/main

    engine = VerificationEngine(tmp_path)
    ver = NodeVerification(
        node_id="T1", verification_type=VerificationType.FILE_EXISTS, target_path="committed.py"
    )
    result = engine.verify_assertion(ver, target_repo="myrepo")
    assert result.passed is True
    assert "origin/main" in result.message


def test_file_exists_working_tree_only_fails_against_origin_main(
    git_repo: Path, tmp_path: Path
) -> None:
    (git_repo / "uncommitted.py").write_text("x = 1\n", encoding="utf-8")

    engine = VerificationEngine(tmp_path)
    ver = NodeVerification(
        node_id="T1", verification_type=VerificationType.FILE_EXISTS, target_path="uncommitted.py"
    )
    result = engine.verify_assertion(ver, target_repo="myrepo")
    assert result.passed is False


def test_ref_option_sees_a_branch_only_file(git_repo: Path, tmp_path: Path) -> None:
    _git(git_repo, "checkout", "-q", "-b", "feature")
    _commit(git_repo, "feature_only.py", "def feature_symbol():\n    pass\n")

    engine = VerificationEngine(tmp_path)
    ver = NodeVerification(
        node_id="T1",
        verification_type=VerificationType.FILE_EXISTS,
        target_path="feature_only.py",
    )
    on_main = engine.verify_assertion(ver, target_repo="myrepo")
    assert on_main.passed is False

    on_feature = engine.verify_assertion(ver, target_repo="myrepo", ref="feature")
    assert on_feature.passed is True
    assert "feature" in on_feature.message


def test_failed_fetch_fails_closed(git_repo: Path, tmp_path: Path) -> None:
    _git(git_repo, "remote", "set-url", "origin", str(tmp_path / "does-not-exist.git"))

    engine = VerificationEngine(tmp_path)
    ver = NodeVerification(
        node_id="T1", verification_type=VerificationType.FILE_EXISTS, target_path="committed.py"
    )
    result = engine.verify_assertion(ver, target_repo="myrepo")
    assert result.passed is False
    assert "fetch" in result.message


def test_file_absent_inverts_against_origin_main(git_repo: Path, tmp_path: Path) -> None:
    engine = VerificationEngine(tmp_path)

    ver_gone = NodeVerification(
        node_id="T1", verification_type=VerificationType.FILE_ABSENT, target_path="gone.py"
    )
    assert engine.verify_assertion(ver_gone, target_repo="myrepo").passed is True

    ver_present = NodeVerification(
        node_id="T1", verification_type=VerificationType.FILE_ABSENT, target_path="committed.py"
    )
    result = engine.verify_assertion(ver_present, target_repo="myrepo")
    assert result.passed is False
    assert "still exists" in result.message


def test_file_absent_fails_closed_on_an_unresolved_ref(git_repo: Path, tmp_path: Path) -> None:
    engine = VerificationEngine(tmp_path)
    ver = NodeVerification(
        node_id="T1", verification_type=VerificationType.FILE_ABSENT, target_path="committed.py"
    )
    result = engine.verify_assertion(ver, target_repo="myrepo", ref="no-such-ref")
    assert result.passed is False
    assert "fails closed" in result.message


def test_repo_prefixed_path_is_stripped(git_repo: Path, tmp_path: Path) -> None:
    engine = VerificationEngine(tmp_path)
    ver = NodeVerification(
        node_id="T1",
        verification_type=VerificationType.FILE_EXISTS,
        target_path="myrepo/committed.py",
    )
    result = engine.verify_assertion(ver, target_repo="myrepo")
    assert result.passed is True


def test_test_command_reads_the_ref_from_tm_verify_ref(git_repo: Path, tmp_path: Path) -> None:
    _git(git_repo, "checkout", "-q", "-b", "feature")
    _commit(git_repo, "feature_only.py", "x = 1\n")

    engine = VerificationEngine(tmp_path)
    ver = NodeVerification(
        node_id="T1",
        verification_type=VerificationType.TEST_COMMAND,
        target_path="",
        expected_pattern='git -C myrepo cat-file -e "$TM_VERIFY_REF:feature_only.py"',
    )
    assert engine.verify_assertion(ver, target_repo="myrepo", ref="main").passed is False
    assert engine.verify_assertion(ver, target_repo="myrepo", ref="feature").passed is True


def test_test_command_with_a_ref_still_inherits_the_parent_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TM_VERIFY_INHERIT_PROBE", "seen")
    engine = VerificationEngine(tmp_path)
    ver = NodeVerification(
        node_id="T1",
        verification_type=VerificationType.TEST_COMMAND,
        target_path="",
        expected_pattern='test "$TM_VERIFY_INHERIT_PROBE" = "seen"',
    )
    result = engine.verify_assertion(ver, target_repo="myrepo", ref="feature")
    assert result.passed is True


def test_test_command_without_a_ref_sets_no_tm_verify_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TM_VERIFY_REF", raising=False)
    engine = VerificationEngine(tmp_path)
    ver = NodeVerification(
        node_id="T1",
        verification_type=VerificationType.TEST_COMMAND,
        target_path="",
        expected_pattern='test -z "${TM_VERIFY_REF+set}"',
    )
    assert engine.verify_assertion(ver, target_repo="myrepo").passed is True
    assert engine.verify_assertion(ver, target_repo="myrepo", ref="feature").passed is False


def test_no_target_repo_keeps_the_working_tree_fallback(git_repo: Path, tmp_path: Path) -> None:
    (tmp_path / "root_only.py").write_text("x = 1\n", encoding="utf-8")

    engine = VerificationEngine(tmp_path)
    ver = NodeVerification(
        node_id="T1", verification_type=VerificationType.FILE_EXISTS, target_path="root_only.py"
    )
    result = engine.verify_assertion(ver)
    assert result.passed is True
    assert result.message == "File exists"


@pytest.mark.parametrize("v_type", sorted(RETIRED_VERIFICATIONS))
def test_a_retired_type_fails_naming_the_test_command_idiom(
    git_repo: Path, tmp_path: Path, v_type: VerificationType
) -> None:
    (git_repo / "committed.py").write_text("def committed_symbol(): pass\n")
    engine = VerificationEngine(tmp_path)
    ver = NodeVerification(
        node_id="T1",
        verification_type=v_type,
        target_path="myrepo/committed.py",
        expected_pattern="committed_symbol",
    )

    at_ref = engine.verify_assertion(ver, target_repo="myrepo")
    in_tree = engine.verify_assertion(ver)

    for result in (at_ref, in_tree):
        assert result.passed is False
        assert result.message == retired_verification(v_type)
    assert '"${TM_VERIFY_REF:-origin/main}"' in at_ref.message
