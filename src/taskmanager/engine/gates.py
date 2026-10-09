"""Gate commands, the failing set a JUnit report or a failing_pattern names, whether a green run ran any test, and the
attribution of a red tip."""

import argparse
import glob
import hashlib
import os
import re
import shlex
import signal
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Literal

from taskmanager.core.models import GateRun
from taskmanager.db.cache_repo import CacheRepository
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.node_repo import NodeRepository
from taskmanager.engine import git as gitops
from taskmanager.engine.config import DEFAULT_REMOTE, ConfigStore

# The baseline cache stores this type; re-exported so callers share the one definition.
__all__ = ["GateRun"]

RED_TARGET = "red-target"
TAIL_CHARS = 4000
TIMEOUT_EXIT = 124

Attribution = Literal["push", "own_defect", "red_target", "unattributed"]

_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def render(template: str, **values: str) -> str:
    """Each `{name}` given a value becomes it, shell-quoted, since the values are paths and ids
    spliced into a command line. Every other brace is the shell's:
    `${HOME}`, `awk '{print $1}'`, `find . -exec ls {} +`."""
    return _PLACEHOLDER.sub(
        lambda m: shlex.quote(values[m[1]]) if m[1] in values else m[0], template
    )


def template_hash(template: str) -> str:
    """The baseline cache key: the template, so every node shares one baseline per sha."""
    return hashlib.sha256(template.encode()).hexdigest()[:16]


def _reports(cwd: Path, junit_glob: str | None) -> list[Path]:
    if not junit_glob:
        return []
    return sorted(Path(p) for p in glob.glob(str(cwd / junit_glob), recursive=True))


def _failing(reports: list[Path]) -> frozenset[str]:
    failing: set[str] = set()
    for report in reports:
        for case in ET.parse(report).getroot().iter("testcase"):
            if case.find("failure") is not None or case.find("error") is not None:
                failing.add(f"{case.get('classname', '')}::{case.get('name', '')}")
    return frozenset(failing)


def _matched(output: str, failing_pattern: str | None) -> frozenset[str]:
    if not failing_pattern:
        return frozenset()
    return frozenset(m[1] for m in re.finditer(failing_pattern, output, re.MULTILINE) if m[1])


def _count(reports: list[Path]) -> int:
    return sum(1 for report in reports for _ in ET.parse(report).getroot().iter("testcase"))


def _no_tests(
    cwd: Path, output: str, reports: list[Path], junit_glob: str | None, tests_ran: str | None
) -> str | None:
    if junit_glob:
        if not reports:
            return f"no test report matches {junit_glob}"
        names = ", ".join(str(report.relative_to(cwd)) for report in reports)
        try:
            if _count(reports) == 0:
                return f"test report {names} holds 0 tests"
        except ET.ParseError:
            return f"test report {names} could not be read"
    if tests_ran:
        found = re.search(tests_ran, output)
        if found is None or not found[1].isdigit():
            return f"the gate's output never matched tests_ran {tests_ran!r} with a count"
        if int(found[1]) == 0:
            return f"the gate ran 0 tests (tests_ran {tests_ran!r})"
    return None


def run_gate(
    command: str,
    cwd: Path,
    timeout: int,
    junit_glob: str | None,
    tests_ran: str | None = None,
    failing_pattern: str | None = None,
) -> GateRun:
    # A report an earlier run left in this worktree would be read as this run's.
    for stale in _reports(cwd, junit_glob):
        stale.unlink()
    proc = subprocess.Popen(
        command,
        shell=True,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        output, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # The gate's own children would outlive a kill of the shell alone and hold its pipe.
        os.killpg(proc.pid, signal.SIGKILL)
        output, _ = proc.communicate()
        return GateRun(TIMEOUT_EXIT, None, f"{output}\ntimed out after {timeout}s"[-TAIL_CHARS:])
    reports = _reports(cwd, junit_glob)
    failing: frozenset[str] | None
    try:
        failing = _failing(reports) if reports else None
    except ET.ParseError:
        failing = None
    matched = _matched(output, failing_pattern)
    if matched:
        failing = (failing or frozenset()) | matched
    if proc.returncode != 0 and not failing:
        # A red run whose report and pattern name no failure failed where neither looks (a
        # build, a crash), so its set says nothing about which tests broke.
        failing = None
    no_tests = (
        _no_tests(cwd, output, reports, junit_glob, tests_ran) if proc.returncode == 0 else None
    )
    return GateRun(proc.returncode, failing, output[-TAIL_CHARS:], no_tests)


def attribute(tip: GateRun, base: GateRun) -> Attribution:
    if tip.exit_code == 0:
        return "push"
    if base.exit_code == 0:
        return "own_defect"
    if tip.failing is None or base.failing is None:
        return "unattributed"
    if tip.failing - base.failing:
        return "own_defect"
    if tip.failing < base.failing:
        return "push"
    return "red_target"


def red_target_cleared(
    cache: CacheRepository,
    repo_dir: Path,
    repo: str,
    sha: str,
    template_hash: str,
    target: str,
    *,
    remote: bool,
    remote_name: str | None = DEFAULT_REMOTE,
) -> bool:
    """What a landing parked on a red target waits on: the target (a top branch read on
    `remote_name`, or locally in a repository with none, when `remote`; else a local container
    branch) moved past `sha`, and the baseline at the new sha, if one ran, no longer fails the
    parked set. An unreadable target is not cleared."""
    current = (
        gitops.ls_remote(repo_dir, remote_name, f"refs/heads/{target}")[0]
        if remote
        else gitops.rev_parse(repo_dir, f"refs/heads/{target}")
    )
    if not current or current == sha:
        return False
    parked = cache.get_baseline(repo, sha, template_hash)
    later = cache.get_baseline(repo, current, template_hash)
    if parked is None or parked.failing is None or later is None or later.failing is None:
        return True
    if later.exit_code == 0:
        return True
    return not parked.failing <= later.failing


def clear_red_targets(nodes: NodeRepository, node_id: str) -> None:
    """Ends a parked landing's wait on its red target: the node's next landing parks again if
    the target is still red."""
    for cond in nodes.get_conditions(node_id):
        if cond.needs.startswith(RED_TARGET):
            nodes.remove_condition(node_id, cond.idx)


def red_target_command(
    root: Path, repo: str, sha: str, template_hash: str, target: str, *, remote: bool
) -> str:
    """The condition command tm stores for a parked landing: tm itself evaluating the rule."""
    parts = [sys.executable, "-m", "taskmanager.engine.gates", RED_TARGET, "--root", str(root)]
    parts += ["--repo", repo, "--sha", sha, "--template-hash", template_hash, "--target", target]
    parts += ["--remote" if remote else "--local"]
    return " ".join(shlex.quote(part) for part in parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m taskmanager.engine.gates")
    commands = parser.add_subparsers(dest="command", required=True)
    red = commands.add_parser(RED_TARGET, help="exit 0 once a parked red target has cleared")
    red.add_argument("--root", type=Path, required=True)
    red.add_argument("--repo", required=True)
    red.add_argument("--sha", required=True)
    red.add_argument("--template-hash", required=True)
    red.add_argument("--target", default="main")
    read = red.add_mutually_exclusive_group()
    read.add_argument(
        "--remote", action="store_true", help="read the target on the repository's remote"
    )
    read.add_argument("--local", action="store_true", help="read the target in the clone")
    args = parser.parse_args(argv)
    # A condition stored by a tm that named neither flag read only `main`, on its remote.
    remote = args.remote or (not args.local and args.target == "main")
    cache = CacheRepository(DatabaseManager(args.root / ".taskmanager"))
    cleared = red_target_cleared(
        cache,
        args.root / args.repo,
        args.repo,
        args.sha,
        args.template_hash,
        args.target,
        remote=remote,
        remote_name=ConfigStore(args.root).branches().remote(args.repo),
    )
    return 0 if cleared else 1


if __name__ == "__main__":
    sys.exit(main())
