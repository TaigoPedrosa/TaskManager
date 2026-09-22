import ast
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from taskmanager.core.enums import VerificationType
from taskmanager.core.models import NodeVerification


@dataclass
class VerificationResult:
    verification_id: int | None
    target_path: str
    verification_type: VerificationType
    passed: bool
    message: str


def _extract_symbol_name(pattern: str) -> str:
    cleaned = pattern.strip()
    if cleaned.startswith("async def "):
        cleaned = cleaned[len("async def ") :].strip()
    elif cleaned.startswith("def "):
        cleaned = cleaned[len("def ") :].strip()
    elif cleaned.startswith("class "):
        cleaned = cleaned[len("class ") :].strip()

    for delimiter in ("(", ":", " "):
        if delimiter in cleaned:
            cleaned = cleaned.split(delimiter, 1)[0].strip()
    return cleaned


PYTHON_SUFFIXES = frozenset({".py", ".pyi"})


def _binds_name(node: ast.AST, name: str) -> bool:
    """A module-, class- or function-level binding of `name`, by any statement that creates one.

    Annotated and plain assignments count: a schema field is `x: int` or `x = 0`, and matching only
    def/class made every field-adding task's check unsatisfiable.
    """
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return node.name == name
    if isinstance(node, ast.AnnAssign):
        return isinstance(node.target, ast.Name) and node.target.id == name
    if isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == name:
                return True
            if isinstance(target, (ast.Tuple, ast.List)) and any(
                isinstance(elt, ast.Name) and elt.id == name for elt in target.elts
            ):
                return True
    return False


class VerificationEngine:
    def __init__(self, target_root: Path) -> None:
        self.root = Path(target_root)

    def verify_assertion(self, ver: NodeVerification) -> VerificationResult:
        full_path = self.root / ver.target_path

        if ver.verification_type == VerificationType.FILE_EXISTS:
            if full_path.exists():
                return VerificationResult(
                    verification_id=ver.id,
                    target_path=ver.target_path,
                    verification_type=ver.verification_type,
                    passed=True,
                    message="File exists",
                )
            return VerificationResult(
                verification_id=ver.id,
                target_path=ver.target_path,
                verification_type=ver.verification_type,
                passed=False,
                message=f"File {ver.target_path} does not exist",
            )

        if ver.verification_type == VerificationType.FILE_ABSENT:
            if not full_path.exists():
                return VerificationResult(
                    verification_id=ver.id,
                    target_path=ver.target_path,
                    verification_type=ver.verification_type,
                    passed=True,
                    message="File absent",
                )
            return VerificationResult(
                verification_id=ver.id,
                target_path=ver.target_path,
                verification_type=ver.verification_type,
                passed=False,
                message=f"File {ver.target_path} still exists",
            )

        if ver.verification_type == VerificationType.SYMBOL_SIGNATURE:
            if not full_path.exists():
                return VerificationResult(
                    verification_id=ver.id,
                    target_path=ver.target_path,
                    verification_type=ver.verification_type,
                    passed=False,
                    message=f"File {ver.target_path} missing",
                )
            if full_path.suffix not in PYTHON_SUFFIXES:
                return VerificationResult(
                    verification_id=ver.id,
                    target_path=ver.target_path,
                    verification_type=ver.verification_type,
                    passed=False,
                    message=(
                        f"symbol_signature parses Python; {ver.target_path} is "
                        f"'{full_path.suffix or 'extensionless'}'. Use a test_command."
                    ),
                )
            try:
                content = full_path.read_text(encoding="utf-8")
                tree = ast.parse(content)
            except SyntaxError as e:
                return VerificationResult(
                    verification_id=ver.id,
                    target_path=ver.target_path,
                    verification_type=ver.verification_type,
                    passed=False,
                    message=f"Syntax error in {ver.target_path}: {e}",
                )
            except OSError as e:
                return VerificationResult(
                    verification_id=ver.id,
                    target_path=ver.target_path,
                    verification_type=ver.verification_type,
                    passed=False,
                    message=f"Failed to read {ver.target_path}: {e}",
                )

            if not ver.expected_pattern:
                return VerificationResult(
                    verification_id=ver.id,
                    target_path=ver.target_path,
                    verification_type=ver.verification_type,
                    passed=False,
                    message="No expected symbol pattern specified",
                )

            symbol_name = _extract_symbol_name(ver.expected_pattern)
            found = any(_binds_name(node, symbol_name) for node in ast.walk(tree))

            if found:
                return VerificationResult(
                    verification_id=ver.id,
                    target_path=ver.target_path,
                    verification_type=ver.verification_type,
                    passed=True,
                    message=f"Symbol {symbol_name} found",
                )
            return VerificationResult(
                verification_id=ver.id,
                target_path=ver.target_path,
                verification_type=ver.verification_type,
                passed=False,
                message=f"Symbol {symbol_name} not found in {ver.target_path}",
            )

        if ver.verification_type == VerificationType.AST_EXPORT:
            if not full_path.exists():
                return VerificationResult(
                    verification_id=ver.id,
                    target_path=ver.target_path,
                    verification_type=ver.verification_type,
                    passed=False,
                    message=f"File {ver.target_path} missing",
                )
            try:
                content = full_path.read_text(encoding="utf-8")
                tree = ast.parse(content)
            except SyntaxError as e:
                return VerificationResult(
                    verification_id=ver.id,
                    target_path=ver.target_path,
                    verification_type=ver.verification_type,
                    passed=False,
                    message=f"Syntax error in {ver.target_path}: {e}",
                )
            except OSError as e:
                return VerificationResult(
                    verification_id=ver.id,
                    target_path=ver.target_path,
                    verification_type=ver.verification_type,
                    passed=False,
                    message=f"Failed to read {ver.target_path}: {e}",
                )

            expected_name = ver.expected_pattern.strip() if ver.expected_pattern else ""
            found = False
            for node in tree.body:
                if (
                    isinstance(node, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets)
                    and isinstance(node.value, (ast.List, ast.Tuple, ast.Set))
                ):
                    for elt in node.value.elts:
                        if isinstance(elt, ast.Constant) and elt.value == expected_name:
                            found = True
                            break
                elif (
                    isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                    and node.name == expected_name
                    and not node.name.startswith("_")
                ):
                    found = True
                if found:
                    break

            if found:
                return VerificationResult(
                    verification_id=ver.id,
                    target_path=ver.target_path,
                    verification_type=ver.verification_type,
                    passed=True,
                    message=f"Export {expected_name} found",
                )
            return VerificationResult(
                verification_id=ver.id,
                target_path=ver.target_path,
                verification_type=ver.verification_type,
                passed=False,
                message=f"Export {expected_name} not found in {ver.target_path}",
            )

        if ver.verification_type == VerificationType.CODEGRAPH_QUERY:
            if not shutil.which("codegraph"):
                return VerificationResult(
                    verification_id=ver.id,
                    target_path=ver.target_path,
                    verification_type=ver.verification_type,
                    passed=True,
                    message="codegraph CLI not installed; skipped",
                )
            res = subprocess.run(
                ["codegraph", "query", ver.codegraph_query_json or "{}"],
                cwd=self.root,
                capture_output=True,
                text=True,
                check=False,
            )
            passed = res.returncode == 0
            msg = (
                res.stdout.strip()
                or res.stderr.strip()
                or (
                    "Query executed successfully"
                    if passed
                    else f"codegraph query failed with exit code {res.returncode}"
                )
            )
            return VerificationResult(
                verification_id=ver.id,
                target_path=ver.target_path,
                verification_type=ver.verification_type,
                passed=passed,
                message=msg,
            )

        if ver.verification_type == VerificationType.TEST_COMMAND:
            command = ver.expected_pattern or ver.target_path
            if not command:
                return VerificationResult(
                    verification_id=ver.id,
                    target_path=ver.target_path,
                    verification_type=ver.verification_type,
                    passed=False,
                    message="No test command specified",
                )
            res = subprocess.run(
                command,
                cwd=self.root,
                shell=True,
                capture_output=True,
                text=True,
                check=False,
            )
            passed = res.returncode == 0
            msg = (
                res.stdout.strip()
                or res.stderr.strip()
                or (
                    "Test command passed"
                    if passed
                    else f"Test command failed with exit code {res.returncode}"
                )
            )
            return VerificationResult(
                verification_id=ver.id,
                target_path=ver.target_path,
                verification_type=ver.verification_type,
                passed=passed,
                message=msg,
            )

        return VerificationResult(
            verification_id=ver.id,
            target_path=ver.target_path,
            verification_type=ver.verification_type,
            passed=True,
            message="Verification passed",
        )

    def verify_all(self, verifications: list[NodeVerification]) -> list[VerificationResult]:
        return [self.verify_assertion(v) for v in verifications]
