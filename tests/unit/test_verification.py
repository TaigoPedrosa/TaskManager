from pathlib import Path
from unittest.mock import patch

from taskmanager.core.enums import VerificationType
from taskmanager.core.models import NodeVerification
from taskmanager.engine.verification import VerificationEngine


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


def test_ast_symbol_verification_function(tmp_path: Path) -> None:
    source_file = tmp_path / "auth.py"
    source_file.write_text("def verify_jwt(token: str) -> bool:\n    return True\n")

    ver = NodeVerification(
        node_id="AUTH-T01",
        verification_type=VerificationType.SYMBOL_SIGNATURE,
        target_path="auth.py",
        expected_pattern="def verify_jwt(token: str) -> bool",
    )

    engine = VerificationEngine(tmp_path)
    result = engine.verify_assertion(ver)
    assert result.passed is True
    assert "Symbol verify_jwt found" in result.message


def test_ast_symbol_verification_async_function(tmp_path: Path) -> None:
    source_file = tmp_path / "async_service.py"
    source_file.write_text("async def fetch_user(user_id: str) -> dict:\n    return {}\n")

    ver = NodeVerification(
        node_id="AUTH-T01",
        verification_type=VerificationType.SYMBOL_SIGNATURE,
        target_path="async_service.py",
        expected_pattern="async def fetch_user(user_id: str) -> dict",
    )

    engine = VerificationEngine(tmp_path)
    result = engine.verify_assertion(ver)
    assert result.passed is True
    assert "Symbol fetch_user found" in result.message


def test_ast_symbol_verification_class(tmp_path: Path) -> None:
    source_file = tmp_path / "models.py"
    source_file.write_text("class TokenVerifier:\n    pass\n")

    ver = NodeVerification(
        node_id="AUTH-T01",
        verification_type=VerificationType.SYMBOL_SIGNATURE,
        target_path="models.py",
        expected_pattern="class TokenVerifier:",
    )

    engine = VerificationEngine(tmp_path)
    result = engine.verify_assertion(ver)
    assert result.passed is True
    assert "Symbol TokenVerifier found" in result.message


def test_ast_symbol_missing_and_syntax_error(tmp_path: Path) -> None:
    source_file = tmp_path / "broken.py"
    source_file.write_text("def valid_function(): pass\n")

    engine = VerificationEngine(tmp_path)

    ver_missing = NodeVerification(
        node_id="AUTH-T01",
        verification_type=VerificationType.SYMBOL_SIGNATURE,
        target_path="broken.py",
        expected_pattern="def missing_function()",
    )
    result_missing = engine.verify_assertion(ver_missing)
    assert result_missing.passed is False
    assert "Symbol missing_function not found" in result_missing.message

    source_file.write_text("def broken_syntax(:\n")
    result_syntax = engine.verify_assertion(ver_missing)
    assert result_syntax.passed is False
    assert "Syntax error in broken.py" in result_syntax.message

    ver_no_pattern = NodeVerification(
        node_id="AUTH-T01",
        verification_type=VerificationType.SYMBOL_SIGNATURE,
        target_path="broken.py",
        expected_pattern=None,
    )
    source_file.write_text("x = 1\n")
    result_no_pattern = engine.verify_assertion(ver_no_pattern)
    assert result_no_pattern.passed is False
    assert "No expected symbol pattern" in result_no_pattern.message


def test_ast_symbol_missing_source_file(tmp_path: Path) -> None:
    engine = VerificationEngine(tmp_path)
    ver = NodeVerification(
        node_id="AUTH-T01",
        verification_type=VerificationType.SYMBOL_SIGNATURE,
        target_path="does_not_exist.py",
        expected_pattern="def missing()",
    )
    result = engine.verify_assertion(ver)
    assert result.passed is False
    assert "File does_not_exist.py missing" in result.message


def test_codegraph_query_missing_cli(tmp_path: Path) -> None:
    engine = VerificationEngine(tmp_path)
    ver = NodeVerification(
        node_id="AUTH-T01",
        verification_type=VerificationType.CODEGRAPH_QUERY,
        target_path="auth.py",
        codegraph_query_json='{"find": "calls"}',
    )
    with patch("shutil.which", return_value=None):
        result = engine.verify_assertion(ver)
        assert result.passed is True
        assert "codegraph CLI not installed; skipped" in result.message


def test_codegraph_query_present_cli(tmp_path: Path) -> None:
    engine = VerificationEngine(tmp_path)
    ver = NodeVerification(
        node_id="AUTH-T01",
        verification_type=VerificationType.CODEGRAPH_QUERY,
        target_path="auth.py",
        codegraph_query_json='{"find": "calls"}',
    )
    with (
        patch("shutil.which", return_value="/usr/local/bin/codegraph"),
        patch("subprocess.run") as mock_run,
    ):
        mock_run.return_value.returncode = 0
        mock_run.return_value.stdout = '{"matches": 2}'
        mock_run.return_value.stderr = ""
        result = engine.verify_assertion(ver)
        assert result.passed is True
        assert '{"matches": 2}' in result.message

        mock_run.return_value.returncode = 1
        mock_run.return_value.stdout = ""
        mock_run.return_value.stderr = "Error parsing query"
        result_failed = engine.verify_assertion(ver)
        assert result_failed.passed is False
        assert "Error parsing query" in result_failed.message


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


def test_ast_export_verification(tmp_path: Path) -> None:
    source_file = tmp_path / "exports.py"
    source_file.write_text('__all__ = ["PublicService"]\nclass PublicService: pass\n')

    engine = VerificationEngine(tmp_path)
    ver = NodeVerification(
        node_id="AUTH-T01",
        verification_type=VerificationType.AST_EXPORT,
        target_path="exports.py",
        expected_pattern="PublicService",
    )
    result = engine.verify_assertion(ver)
    assert result.passed is True
    assert "Export PublicService found" in result.message

    ver_missing = NodeVerification(
        node_id="AUTH-T01",
        verification_type=VerificationType.AST_EXPORT,
        target_path="exports.py",
        expected_pattern="PrivateService",
    )
    result_missing = engine.verify_assertion(ver_missing)
    assert result_missing.passed is False


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


def test_ast_symbol_verification_annotated_field(tmp_path: Path) -> None:
    source_file = tmp_path / "schemas.py"
    source_file.write_text(
        "class OpsTenantDetailOut(BaseModel):\n    campaigns_in_use: int\n"
    )

    ver = NodeVerification(
        node_id="AUTH-T01",
        verification_type=VerificationType.SYMBOL_SIGNATURE,
        target_path="schemas.py",
        expected_pattern="campaigns_in_use: int",
    )

    engine = VerificationEngine(tmp_path)
    result = engine.verify_assertion(ver)
    assert result.passed is True
    assert "Symbol campaigns_in_use found" in result.message


def test_ast_symbol_verification_plain_assignment(tmp_path: Path) -> None:
    source_file = tmp_path / "settings.py"
    source_file.write_text("OPS_ORIGIN = 'https://ops.example'\n")

    ver = NodeVerification(
        node_id="AUTH-T01",
        verification_type=VerificationType.SYMBOL_SIGNATURE,
        target_path="settings.py",
        expected_pattern="OPS_ORIGIN",
    )

    engine = VerificationEngine(tmp_path)
    result = engine.verify_assertion(ver)
    assert result.passed is True


def test_ast_symbol_verification_absent_field_still_fails(tmp_path: Path) -> None:
    source_file = tmp_path / "schemas.py"
    source_file.write_text("class OpsTenantDetailOut(BaseModel):\n    slug: str\n")

    ver = NodeVerification(
        node_id="AUTH-T01",
        verification_type=VerificationType.SYMBOL_SIGNATURE,
        target_path="schemas.py",
        expected_pattern="campaigns_in_use: int",
    )

    engine = VerificationEngine(tmp_path)
    result = engine.verify_assertion(ver)
    assert result.passed is False


def test_ast_symbol_verification_refuses_a_non_python_target(tmp_path: Path) -> None:
    source_file = tmp_path / "themeChoice.ts"
    source_file.write_text("export const themeChoice = 'dark';\n")

    ver = NodeVerification(
        node_id="AUTH-T01",
        verification_type=VerificationType.SYMBOL_SIGNATURE,
        target_path="themeChoice.ts",
        expected_pattern="themeChoice",
    )

    engine = VerificationEngine(tmp_path)
    result = engine.verify_assertion(ver)
    assert result.passed is False
    assert "parses Python" in result.message
    assert "test_command" in result.message
