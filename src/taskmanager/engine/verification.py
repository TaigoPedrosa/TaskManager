import ast
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from taskmanager.core.enums import VerificationType
from taskmanager.core.models import NodeVerification
from taskmanager.engine.config import DEFAULT_BRANCH, ConfigStore

CODEGRAPH_INSTALL: Final = "npm install -g @colbymchenry/codegraph"


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


def _resolve_ref(repo_root: Path, ref: str | None, branch: str) -> tuple[str, str | None]:
    """The ref to read, and a fetch-failure message when the default ref could not be refreshed.

    A caller-supplied `ref` is read as-is, with no fetch. Otherwise the default is
    `origin/<branch>` after fetching it; a failed fetch is reported and never falls back to the
    working tree.
    """
    if ref is not None:
        return ref, None
    fetched = _git(repo_root, "fetch", "-q", "origin", branch)
    if fetched.returncode != 0:
        return f"origin/{branch}", fetched.stderr.strip() or f"exit code {fetched.returncode}"
    return f"origin/{branch}", None


def codegraph_regex(pattern: str | None) -> re.Pattern[str]:
    """A codegraph_query's `expected_pattern`, compiled; a ValueError says why it cannot judge."""
    if not pattern:
        raise ValueError(
            "has no expected_pattern, the regex its `codegraph query --json` output must match"
        )
    try:
        return re.compile(pattern)
    except re.error as e:
        raise ValueError(f"has an expected_pattern that is not a regex: {e}") from None


def codegraph_flags(query_json: str | None) -> list[str]:
    """A codegraph_query's `codegraph_query_json` as command-line flags; a ValueError says why
    it is not a JSON object of them."""
    try:
        flags = json.loads(query_json or "{}")
        return [arg for key, value in flags.items() for arg in (f"--{key}", str(value))]
    except ValueError, AttributeError:
        raise ValueError(
            f"has codegraph_query_json {query_json!r}, which is not a JSON object of query flags"
        ) from None


def _index_tree(repo_root: Path, sha: str, tree: Path) -> str | None:
    """Export `sha` into `tree` and index it there, since codegraph reads a directory and never a
    git ref. The failure, or None once `tree` holds the index."""
    build = Path(tempfile.mkdtemp(prefix=f".{sha}-", dir=tree.parent))
    try:
        archive = subprocess.run(
            ["git", "-C", str(repo_root), "archive", "--format=tar", sha],
            capture_output=True,
            check=True,
        )
        # ponytail: the whole archive sits in memory; stream it through a pipe when a tree
        # outgrows RAM.
        with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
            tar.extractall(build, filter="data")
        init = subprocess.run(
            ["codegraph", "init", "-y", str(build)], capture_output=True, text=True, check=False
        )
        if init.returncode != 0:
            detail = init.stderr.strip() or init.stdout.strip()
            return f"`codegraph init` failed with exit code {init.returncode}: {detail}"
        # A run that indexed the same sha first left an identical tree there.
        with contextlib.suppress(OSError):
            build.rename(tree)
        return None
    finally:
        shutil.rmtree(build, ignore_errors=True)


def _evict(cache: Path, keep: int) -> None:
    """Deletes every tree but the `keep` most recently queried."""
    # ponytail: no lock, so a run can evict a tree another run is about to query; a lock file
    # in the cache when parallel verifications at many commits trip on it.
    trees = sorted(
        (p for p in cache.iterdir() if p.is_dir() and not p.name.startswith(".")),
        key=lambda p: p.stat().st_mtime_ns,
        reverse=True,
    )
    for stale in trees[keep:]:
        shutil.rmtree(stale, ignore_errors=True)


class VerificationEngine:
    def __init__(self, target_root: Path) -> None:
        self.root = Path(target_root)

    def verify_assertion(
        self,
        ver: NodeVerification,
        target_repo: str | None = None,
        ref: str | None = None,
        branch: str | None = None,
    ) -> VerificationResult:
        """With no `ref`, a path check or a codegraph query reads `origin/<branch>` (the default
        branch when none is named), and a test command is handed `origin/<branch>` only when
        `branch` is named."""
        if ver.verification_type == VerificationType.CODEGRAPH_QUERY:
            return self._codegraph_at_ref(ver, target_repo or ".", ref, branch or DEFAULT_BRANCH)
        if target_repo and ver.verification_type in _PATH_VERIFICATION_TYPES:
            return self._verify_at_ref(ver, target_repo, ref, branch or DEFAULT_BRANCH)
        if ref is None and branch is not None:
            ref = f"origin/{branch}"
        return self._verify_in_tree(ver, ref)

    def _verify_at_ref(
        self, ver: NodeVerification, target_repo: str, ref: str | None, branch: str
    ) -> VerificationResult:
        repo_root = self.root / target_repo
        effective_ref, fetch_error = _resolve_ref(repo_root, ref, branch)
        mode_suffix = f" (git ref {effective_ref} in {target_repo})"

        if fetch_error is not None:
            return VerificationResult(
                verification_id=ver.id,
                target_path=ver.target_path,
                verification_type=ver.verification_type,
                passed=False,
                message=(
                    f"git fetch origin {branch} in {target_repo} failed, refusing to fall back "
                    f"to the working tree: {fetch_error}"
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

    def _codegraph_at_ref(
        self, ver: NodeVerification, target_repo: str, ref: str | None, branch: str
    ) -> VerificationResult:
        def result(passed: bool, message: str) -> VerificationResult:
            return VerificationResult(
                ver.id, ver.target_path, ver.verification_type, passed, message
            )

        try:
            regex = codegraph_regex(ver.expected_pattern)
            flags = codegraph_flags(ver.codegraph_query_json)
        except ValueError as e:
            return result(False, f"codegraph_query {ver.target_path!r} {e}")
        if shutil.which("codegraph") is None:
            return result(False, f"codegraph is not on PATH; install it: `{CODEGRAPH_INSTALL}`")
        repo_root = self.root / target_repo
        if not (repo_root / ".codegraph").is_dir():
            return result(
                False, f"{target_repo} has no codegraph index; run `codegraph init` in {repo_root}"
            )

        effective_ref, fetch_error = _resolve_ref(repo_root, ref, branch)
        if fetch_error is not None:
            return result(
                False,
                f"git fetch origin {branch} in {target_repo} failed, refusing to fall back to "
                f"the working tree: {fetch_error}",
            )
        mode_suffix = f" (git ref {effective_ref} in {target_repo})"
        sha = _git(
            repo_root, "rev-parse", "--verify", "--quiet", f"{effective_ref}^{{commit}}"
        ).stdout.strip()
        if not sha:
            return result(False, f"Ref {effective_ref} does not resolve in {target_repo}")

        cache = self.root / ".taskmanager" / "cache" / "codegraph"
        tree = cache / sha
        if not tree.is_dir():
            cache.mkdir(parents=True, exist_ok=True)
            # Keeps the exported trees out of git and out of the repository's own index.
            (cache / ".gitignore").write_text("*\n", encoding="utf-8")
            failure = _index_tree(repo_root, sha, tree)
            if failure is not None:
                return result(False, failure + mode_suffix)
        # A tree's mtime is when its commit was last queried, the order eviction reads. The
        # clock is read here because a filesystem's own touch can give two trees one tick.
        now = time.time_ns()
        os.utime(tree, ns=(now, now))
        _evict(cache, ConfigStore(self.root).resolve("codegraph.cache_commits").value)

        query = subprocess.run(
            ["codegraph", "query", *flags, "--json", "-p", str(tree), "--", ver.target_path],
            capture_output=True,
            text=True,
            check=False,
        )
        if query.returncode != 0:
            detail = query.stderr.strip() or query.stdout.strip()
            return result(
                False,
                f"codegraph query failed with exit code {query.returncode}{mode_suffix}: {detail}",
            )
        if regex.search(query.stdout):
            return result(True, f"codegraph query matched /{regex.pattern}/{mode_suffix}")
        return result(
            False,
            f"codegraph query {ver.target_path!r} output does not match "
            f"/{regex.pattern}/{mode_suffix}",
        )

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
            # through the environment; unset when there is none to hand it.
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
        branch_for_node: Mapping[str, str] | None = None,
    ) -> list[VerificationResult]:
        repos, branches = repo_for_node or {}, branch_for_node or {}
        return [
            self.verify_assertion(
                v, target_repo=repos.get(v.node_id), ref=ref, branch=branches.get(v.node_id)
            )
            for v in verifications
        ]
