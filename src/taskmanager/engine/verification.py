import ast
import os
import shutil
import subprocess
from collections.abc import Mapping
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

_PATH_VERIFICATION_TYPES = frozenset(
    {
        VerificationType.FILE_EXISTS,
        VerificationType.FILE_ABSENT,
        VerificationType.SYMBOL_SIGNATURE,
        VerificationType.AST_EXPORT,
    }
)


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


def _symbol_signature_result(
    ver: NodeVerification, content: str, mode_suffix: str
) -> VerificationResult:
    try:
        tree = ast.parse(content)
    except SyntaxError as e:
        return VerificationResult(
            verification_id=ver.id,
            target_path=ver.target_path,
            verification_type=ver.verification_type,
            passed=False,
            message=f"Syntax error in {ver.target_path}: {e}{mode_suffix}",
        )

    if not ver.expected_pattern:
        return VerificationResult(
            verification_id=ver.id,
            target_path=ver.target_path,
            verification_type=ver.verification_type,
            passed=False,
            message=f"No expected symbol pattern specified{mode_suffix}",
        )

    symbol_name = _extract_symbol_name(ver.expected_pattern)
    found = any(_binds_name(node, symbol_name) for node in ast.walk(tree))
    if found:
        return VerificationResult(
            verification_id=ver.id,
            target_path=ver.target_path,
            verification_type=ver.verification_type,
            passed=True,
            message=f"Symbol {symbol_name} found{mode_suffix}",
        )
    return VerificationResult(
        verification_id=ver.id,
        target_path=ver.target_path,
        verification_type=ver.verification_type,
        passed=False,
        message=f"Symbol {symbol_name} not found in {ver.target_path}{mode_suffix}",
    )


def _ast_export_result(ver: NodeVerification, content: str, mode_suffix: str) -> VerificationResult:
    try:
        tree = ast.parse(content)
    except SyntaxError as e:
        return VerificationResult(
            verification_id=ver.id,
            target_path=ver.target_path,
            verification_type=ver.verification_type,
            passed=False,
            message=f"Syntax error in {ver.target_path}: {e}{mode_suffix}",
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
            message=f"Export {expected_name} found{mode_suffix}",
        )
    return VerificationResult(
        verification_id=ver.id,
        target_path=ver.target_path,
        verification_type=ver.verification_type,
        passed=False,
        message=f"Export {expected_name} not found in {ver.target_path}{mode_suffix}",
    )


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _relative_to_repo(target_path: str, target_repo: str) -> str:
    prefix = f"{target_repo}/"
    return target_path.removeprefix(prefix)


def _resolve_ref(repo_root: Path, ref: str | None) -> tuple[str, str | None]:
    """The ref to read, and a fetch-failure message when the default ref could not be refreshed.

    A caller-supplied `ref` is read as-is, with no fetch. Otherwise the default is `origin/main`
    after fetching it; a failed fetch is reported and never falls back to the working tree.
    """
    if ref is not None:
        return ref, None
    fetched = _git(repo_root, "fetch", "-q", "origin", "main")
    if fetched.returncode != 0:
        return "origin/main", fetched.stderr.strip() or f"exit code {fetched.returncode}"
    return "origin/main", None


class VerificationEngine:
    def __init__(self, target_root: Path) -> None:
        self.root = Path(target_root)

    def verify_assertion(
        self,
        ver: NodeVerification,
        target_repo: str | None = None,
        ref: str | None = None,
    ) -> VerificationResult:
        if target_repo and ver.verification_type in _PATH_VERIFICATION_TYPES:
            return self._verify_at_ref(ver, target_repo, ref)
        return self._verify_in_tree(ver, ref)

    def _verify_at_ref(
        self, ver: NodeVerification, target_repo: str, ref: str | None
    ) -> VerificationResult:
        repo_root = self.root / target_repo
        effective_ref, fetch_error = _resolve_ref(repo_root, ref)
        mode_suffix = f" (git ref {effective_ref} in {target_repo})"

        if fetch_error is not None:
            return VerificationResult(
                verification_id=ver.id,
                target_path=ver.target_path,
                verification_type=ver.verification_type,
                passed=False,
                message=(
                    f"git fetch origin main in {target_repo} failed, refusing to fall back to "
                    f"the working tree: {fetch_error}"
                ),
            )

        rel_path = _relative_to_repo(ver.target_path, target_repo)
        exists = _git(repo_root, "cat-file", "-e", f"{effective_ref}:{rel_path}").returncode == 0

        if ver.verification_type == VerificationType.FILE_EXISTS:
            if exists:
                return VerificationResult(
                    ver.id,
                    ver.target_path,
                    ver.verification_type,
                    True,
                    f"File exists{mode_suffix}",
                )
            return VerificationResult(
                ver.id,
                ver.target_path,
                ver.verification_type,
                False,
                f"File {ver.target_path} does not exist{mode_suffix}",
            )

        if ver.verification_type == VerificationType.FILE_ABSENT:
            ref_resolves = (
                _git(
                    repo_root, "rev-parse", "--verify", "--quiet", f"{effective_ref}^{{commit}}"
                ).returncode
                == 0
            )
            if not ref_resolves:
                return VerificationResult(
                    ver.id,
                    ver.target_path,
                    ver.verification_type,
                    False,
                    f"Ref {effective_ref} does not resolve in {target_repo}; "
                    f"file_absent fails closed{mode_suffix}",
                )
            if not exists:
                return VerificationResult(
                    ver.id,
                    ver.target_path,
                    ver.verification_type,
                    True,
                    f"File absent{mode_suffix}",
                )
            return VerificationResult(
                ver.id,
                ver.target_path,
                ver.verification_type,
                False,
                f"File {ver.target_path} still exists{mode_suffix}",
            )

        if ver.verification_type == VerificationType.SYMBOL_SIGNATURE and (
            Path(rel_path).suffix not in PYTHON_SUFFIXES
        ):
            return VerificationResult(
                verification_id=ver.id,
                target_path=ver.target_path,
                verification_type=ver.verification_type,
                passed=False,
                message=(
                    f"symbol_signature parses Python; {ver.target_path} is "
                    f"'{Path(rel_path).suffix or 'extensionless'}'. Use a test_command."
                ),
            )

        if not exists:
            return VerificationResult(
                ver.id,
                ver.target_path,
                ver.verification_type,
                False,
                f"File {ver.target_path} missing{mode_suffix}",
            )

        shown = _git(repo_root, "show", f"{effective_ref}:{rel_path}")
        if shown.returncode != 0:
            return VerificationResult(
                ver.id,
                ver.target_path,
                ver.verification_type,
                False,
                f"Failed to read {ver.target_path}{mode_suffix}: {shown.stderr.strip()}",
            )

        if ver.verification_type == VerificationType.SYMBOL_SIGNATURE:
            return _symbol_signature_result(ver, shown.stdout, mode_suffix)
        return _ast_export_result(ver, shown.stdout, mode_suffix)

    def _verify_in_tree(self, ver: NodeVerification, ref: str | None = None) -> VerificationResult:
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
            except OSError as e:
                return VerificationResult(
                    verification_id=ver.id,
                    target_path=ver.target_path,
                    verification_type=ver.verification_type,
                    passed=False,
                    message=f"Failed to read {ver.target_path}: {e}",
                )
            return _symbol_signature_result(ver, content, mode_suffix="")

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
            except OSError as e:
                return VerificationResult(
                    verification_id=ver.id,
                    target_path=ver.target_path,
                    verification_type=ver.verification_type,
                    passed=False,
                    message=f"Failed to read {ver.target_path}: {e}",
                )
            return _ast_export_result(ver, content, mode_suffix="")

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
            # A stored command string has no placeholder to rewrite, so the ref reaches it
            # through the environment; unset when no ref was asked for.
            env = None if ref is None else {**os.environ, "TM_VERIFY_REF": ref}
            res = subprocess.run(
                command,
                cwd=self.root,
                env=env,
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

    def verify_all(
        self,
        verifications: list[NodeVerification],
        repo_for_node: Mapping[str, str | None] | None = None,
        ref: str | None = None,
    ) -> list[VerificationResult]:
        repos = repo_for_node or {}
        return [
            self.verify_assertion(v, target_repo=repos.get(v.node_id), ref=ref)
            for v in verifications
        ]
