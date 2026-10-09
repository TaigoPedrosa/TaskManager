"""The claims engine: the only writer of a node's position in the dispatch cycle.

Every verb reads the stored cycle, asks the pure lifecycle rules for the next one, and writes it
with its lease change and the parents' rollup in one state.db transaction.
"""

import hashlib
import json
import logging
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from subprocess import CalledProcessError
from typing import Any, Final, Protocol, cast

from taskmanager.core import lifecycle
from taskmanager.core.enums import CONTAINERS, NodeKind, RelationType
from taskmanager.core.lifecycle import Caps, Cycle, LifecycleError
from taskmanager.core.models import (
    Condition,
    FileLock,
    Job,
    Lease,
    LeaseAction,
    LedgerEvent,
    Node,
    NodeRelation,
)
from taskmanager.core.status import (
    EXITS,
    IN_STEP,
    Action,
    ConditionStage,
    DecisionStatus,
    Event,
    JobKind,
    JobState,
    Outcome,
    Status,
)
from taskmanager.db.cache_repo import CacheRepository
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.graph_reader import read_graph
from taskmanager.db.job_repo import JobRepository
from taskmanager.db.node_repo import declared_files_of
from taskmanager.di.container import create_container
from taskmanager.engine import git as gitops
from taskmanager.engine import selection
from taskmanager.engine.chains import TOP, meeting, sync_pairs
from taskmanager.engine.conditions import ConditionRunner, is_executable
from taskmanager.engine.config import DEFAULT_BRANCH, ConfigStore, ProjectConfig
from taskmanager.engine.decisions import (
    open_failed_decision,
    open_stranded_decision,
    stranded_dependents,
)
from taskmanager.engine.doctor import CODEGRAPH_INSTALL, codegraph_index
from taskmanager.engine.gates import RED_TARGET, clear_red_targets
from taskmanager.engine.git import GitManager
from taskmanager.engine.operations import OperationError, Operations
from taskmanager.engine.routing import model_for
from taskmanager.engine.snapshot import (
    SnapshotBuilder,
    roll_up_ancestors,
    stored_status,
    writes_migration,
)
from taskmanager.engine.stepgraph import Snapshot
from taskmanager.engine.validation import Refusal, validate

LIVE_JOBS = frozenset({JobState.RUNNING, JobState.NEEDS_AGENT})
# (source, base, carrier): see Claims._sync_pairs.
SyncPair = tuple[str, str, str]
_log = logging.getLogger(__name__)

_STALLED = "max_step_failures steps in a row ended without progress"
_FAILED_BECAUSE: dict[Event, str] = {
    Event.REJECT: "its review rejected it with no fix round left",
    Event.OWN_DEFECT: "its landing failed on its own defect with no merge fix left",
    Event.RELEASE: _STALLED,
    Event.EXPIRED: _STALLED,
}

# Seconds a claim waits on one codegraph call before it reports codegraph unavailable.
CODEGRAPH_TIMEOUT: Final = 120
# `codegraph node --symbols-only` lists one symbol per line as "- `name` (kind) ...".
_SYMBOL_LINE = re.compile(r"^- `([^`]+)` \((\w+)\)", re.MULTILINE)
# Its first line ends "used by N files: a.py, b.py", with "+N more" past the eighth, or with
# _NO_USERS.
_USED_BY = re.compile(r"used by \d+ files?: (.+)$")
_NO_USERS: Final = "no other indexed file depends on it"


class CodegraphUnavailable(Exception):
    pass


def _codegraph(*args: str) -> str:
    try:
        run = subprocess.run(
            ["codegraph", *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=CODEGRAPH_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CodegraphUnavailable(f"codegraph {args[0]}: {exc}") from exc
    if run.returncode != 0:
        detail = run.stderr.strip() or run.stdout.strip()
        raise CodegraphUnavailable(
            f"codegraph {args[0]} failed with exit code {run.returncode}: {detail}"
        )
    return run.stdout


def _seed_index(checkout: Path, worktree: Path) -> None:
    """codegraph answers a path from the nearest `.codegraph/` above it, so a worktree cut inside
    its checkout would answer from the checkout's tree. The index stores repository-relative
    paths, so a copy synced in the worktree answers for it, in a fraction of a fresh init."""
    if shutil.which("codegraph") is None:
        raise CodegraphUnavailable(f"codegraph is not on PATH; install it: `{CODEGRAPH_INSTALL}`")
    index, seeded = codegraph_index(checkout), codegraph_index(worktree)
    try:
        seeded.parent.mkdir(exist_ok=True)
        # SQLite's backup copies one consistent state even while a codegraph daemon writes.
        with closing(sqlite3.connect(index)) as source, closing(sqlite3.connect(seeded)) as copy:
            source.backup(copy)
    except (OSError, sqlite3.Error) as exc:
        raise CodegraphUnavailable(f"copying {index}: {exc}") from exc
    _codegraph("sync", "--quiet", str(worktree))


def _symbols(listing: str) -> list[str]:
    """The symbols a `node --symbols-only` listing names, a method under its class's name as
    `impact` resolves it."""
    names: list[str] = []
    owner = ""
    for name, kind in _SYMBOL_LINE.findall(listing):
        owner = name if kind == "class" else owner
        names.append(f"{owner}.{name}" if kind == "method" and owner else name)
    return list(dict.fromkeys(names))


def _users(listing: str) -> set[str] | None:
    """The files a `node --symbols-only` listing says depend on its file, or None when it names
    only some of them or none in a form this reads."""
    header = listing.partition("\n")[0]
    if header.endswith(_NO_USERS):
        return set()
    found = _USED_BY.search(header)
    if found is None:
        return None
    files = found.group(1).split(", ")
    return None if files[-1].startswith("+") else set(files)


def _dependents(worktree: Path, name: str) -> set[str]:
    """The files holding what uses the symbol `name` directly."""
    out = _codegraph("impact", name, "--depth", "1", "--json", "-p", str(worktree))
    try:
        found = json.loads(out)
    except ValueError:
        # A name codegraph has not indexed is answered with a line of text and exit code 0.
        return set()
    try:
        return {str(item["filePath"]) for item in found["affected"]}
    except (KeyError, TypeError) as exc:
        raise CodegraphUnavailable(f"codegraph impact {name} printed no affected files") from exc


def _reaches(worktree: Path, declared: list[str], held: dict[str, str]) -> list[str]:
    lines: list[str] = []
    with ThreadPoolExecutor() as pool:
        for path in declared:
            # `impact` on a file path answers with that file alone, so the file's dependents
            # come from its `node` listing: a file none of whose dependents is held is skipped.
            listing = _codegraph("node", "-f", path, "--symbols-only", "-p", str(worktree))
            users = _users(listing)
            if users is not None and not held.keys() & users:
                continue
            names = _symbols(listing)
            found = pool.map(partial(_dependents, worktree), names)
            for name, files in zip(names, found, strict=True):
                lines += [
                    f"{name} reaches {f} held by {held[f]}" for f in sorted(files & held.keys())
                ]
    return lines


def codegraph_lines(
    checkout: Path, worktree: Path, declared: list[str], held: dict[str, str]
) -> list[str]:
    """What a claim prints after `codegraph: ` for a worktree of `checkout`: nothing when the
    checkout has no codegraph index, and one `unavailable` line for any failure, never a raise.
    `declared` and `held` are repository-relative; `held` maps a file to the node holding it."""
    if not codegraph_index(checkout).is_file():
        return []
    lines: list[str] = []
    try:
        _seed_index(checkout, worktree)
        lines.append(f"ready {worktree}")
        lines += _reaches(worktree, declared, held)
    except CodegraphUnavailable as exc:
        lines.append(f"unavailable ({exc})")
    return lines


def _stop_process(job: Job) -> None:
    """Stops the detached process running `job`, with its git children, so a job expired under
    it cannot push or keep heartbeating a lease it no longer owns. The pid is matched against
    the job's own command line first: a pid recorded long ago may name an unrelated process."""
    if job.pid is None or job.pid == os.getpid():
        return
    try:
        command = subprocess.run(
            ["ps", "-ww", "-o", "command=", "-p", str(job.pid)],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        if f"taskmanager.engine.landing run {job.id}" in command:
            os.killpg(job.pid, signal.SIGTERM)
    except OSError as exc:
        _log.warning("could not stop job %s (pid %s): %s", job.id, job.pid, exc)


@dataclass(frozen=True)
class DecisionSpec:
    question: str
    # Each option is "key|Label|description|effect", the `tm decision add --option` syntax.
    options: list[str] = field(default_factory=list)
    recommend: str | None = None


@dataclass(frozen=True)
class Blocker:
    depends: list[str] = field(default_factory=list)
    decision: DecisionSpec | None = None
    condition: Condition | None = None


@dataclass(frozen=True)
class ClaimResult:
    action: Action
    reason: str | None = None
    model: str | None = None
    job: str | None = None
    repos: list[str] = field(default_factory=list)
    branch: str | None = None
    base: str | None = None
    # A task's worktree; for a container, the directory holding one worktree per repository.
    worktree: str | None = None
    # Repository name to the worktree the step works in there.
    worktrees: dict[str, str] = field(default_factory=dict)
    # The claim's own name, which closes its step; absent when nothing was claimed.
    token: str | None = None
    # Repository name to its `base`: the repositories of a container can land on different
    # default branches. `base` is the first repository's.
    bases: dict[str, str] = field(default_factory=dict)
    # `codegraph_lines` for each worktree the claim cut: advice only, the claim stands either way.
    codegraph: list[str] = field(default_factory=list)


class LandingJobs(Protocol):
    def start_land(self, node_id: str) -> str: ...

    def start_sync(self, node_id: str, pairs: list[SyncPair]) -> str: ...


class _UnmovedBranches:
    """Branch facts for writes that change neither `merge` nor the parent, where the
    branch-base rule cannot fire."""

    def branch_exists(self, node_id: str) -> bool:
        return False

    def base_matches(self, node_id: str, new_target: str, new_top: str) -> bool:
        return True


class Claims:
    def __init__(
        self,
        root: Path,
        config: ProjectConfig,
        ops: Operations,
        jobs: JobRepository,
        snapshots: SnapshotBuilder,
        conditions: ConditionRunner,
    ) -> None:
        self.root = root
        self.config = config
        self.ops = ops
        self.nodes = ops.node_repo
        self.runtime = ops.runtime_repo
        self.jobs = jobs
        self.snapshots = snapshots
        self.conditions = conditions
        # Set by the landing engine when it is built: it needs these claims to move nodes.
        self.landing: LandingJobs | None = None
        self.caps = Caps(
            fix_rounds_task=config.max_fix_rounds.task,
            fix_rounds_container=config.max_fix_rounds.container,
            merge_attempts=config.max_merge_attempts,
            step_failures=config.max_step_failures,
        )

    @classmethod
    def open(
        cls, root: Path, config: ProjectConfig | None = None, actor: str | None = None
    ) -> Claims:
        container = create_container(root)
        db = container.get(DatabaseManager)
        ops = container.get(Operations)
        if actor is not None:
            ops = ops.with_actor(actor)
        cfg = config or ConfigStore(root).project()
        jobs = JobRepository(db)
        cache = CacheRepository(db)
        return cls(
            root,
            cfg,
            ops,
            jobs,
            SnapshotBuilder(ops.node_repo, ops.runtime_repo, jobs),
            ConditionRunner(root, ops.node_repo, cache, cfg.condition_ttl, cfg.condition_timeout),
        )

    def node(self, node_id: str) -> Node:
        found = self.nodes.get_node(node_id)
        if found is None:
            raise OperationError(f"node '{node_id}' not found", 404)
        if found.kind == NodeKind.DECISION:
            raise OperationError(f"'{node_id}' is a decision; use `tm decision`", 400)
        return found

    @staticmethod
    def is_container(node: Node) -> bool:
        return node.kind in CONTAINERS

    def branch_of(self, node_id: str) -> str:
        return self.ops.branch_of(self.node(node_id).id)

    def target_of(self, node_id: str, repo: str | None = None) -> str:
        """The branch `node_id` lands on: its parent's branch, or its chain's top branch."""
        return self.ops.target_of(self.node(node_id).id, repo)

    def target_ref(self, node_id: str, repo: str | None = None) -> str:
        return self.ops.target_ref(self.node(node_id).id, repo)

    def repos_of(self, node_id: str) -> list[str]:
        return self.ops.repos_of(self.node(node_id).id, self.config.repo_order)

    def ttl_for(self, action: Action) -> int:
        return self.config.lease_ttl_for(action)

    @staticmethod
    def live(lease: Lease) -> bool:
        if lease.ttl_seconds is None:
            # Parked for an agent: never expires until someone takes it.
            return True
        age = (datetime.now(tz=UTC) - lease.last_heartbeat).total_seconds()
        return age <= lease.ttl_seconds

    def _locked_files(self, node: Node, action: Action | None, snap: Snapshot) -> list[str]:
        """The same union a claim locks and the display's `files_conflict` reads: one walk over
        the snapshot, through `SnapshotBuilder.lock_set`, so the two can never drift apart."""
        if action not in (Action.IMPLEMENT, Action.FIX):
            return []
        return self.snapshots.lock_set(node.id, snap)

    def blocked_reason(self, node: Node, snap: Snapshot, action: Action | None) -> str | None:
        """Why `node` cannot be claimed now, the first reason in claimability order; None when
        it can. `engine.selection` holds the graph rules, read only from `snap`'s one bulk read;
        a condition needs a command run, which only this live check can do, between the rules
        that must block ahead of it and the ones (starting with "no next action") that don't."""
        reason = selection.blocked_reason_before_condition(node, snap)
        if reason is not None:
            return reason
        reason = self._condition_reason(node, snap, action)
        if reason is not None:
            return reason
        return selection.blocked_reason_after_condition(
            node, snap, action, repo_order=self.config.repo_order
        )

    def _condition_reason(self, node: Node, snap: Snapshot, action: Action | None) -> str | None:
        data = snap.graph_data()
        stages = [
            ConditionStage.CLAIM,
            *([ConditionStage.LANDING] if action == Action.MERGE else []),
        ]
        for stage in stages:
            # A node with none of this stage's conditions is met by definition: skip the runner
            # (which would run any stale one's command) rather than call it for nothing.
            if not any(c.stage == stage for c in data.conditions.get(node.id, [])):
                continue
            unmet = self.conditions.unmet(node.id, stage)
            if unmet:
                return f"condition unmet: {unmet[0].needs}"
        return None

    def next_step(self, node: Node, snap: Snapshot) -> tuple[Action | None, str | None]:
        """The action a claim would take now, and the model it would name."""
        return selection.next_step(node, snap, self.config.models)

    def verify(self, node_id: str, ref: str, repo: str | None = None) -> tuple[bool, str]:
        """The node's verifications at `ref` (a container's: every descendant task's), limited to
        `repo` when given. An empty set passes and says so."""
        node = self.node(node_id)
        ids = (
            [d for d in self.ops.counted_descendants(node_id) if self.node(d).kind == NodeKind.TASK]
            if self.is_container(node)
            else [node_id]
        )
        repo_for: dict[str, str | None] = {i: self.node(i).target_repo for i in ids}
        checks = [
            v
            for i in ids
            if repo is None or repo_for[i] == repo
            for v in self.nodes.get_verifications(i)
        ]
        if not checks:
            return True, f"no verifications to run at {ref}: an empty set passes"
        results = self.ops.verification_engine.verify_all(checks, repo_for, ref)
        report = "\n".join(
            f"{'PASS' if r.passed else 'FAIL'} {r.verification_type} {r.target_path}: {r.message}"
            for r in results
        )
        return all(r.passed for r in results), report

    def start(
        self,
        node_id: str,
        agent: str,
        session: str,
        ttl: int | None = None,
        worktree_dir: Path | None = None,
    ) -> ClaimResult:
        self.node(node_id)
        self.sweep()
        node = self.node(node_id)
        handed = self._hand_over(node, agent, session, ttl)
        if handed is not None:
            return handed
        snap = self.snapshots.build()
        cycle = self.snapshots.cycle(node)
        action = lifecycle.next_action(cycle)
        reason = self.blocked_reason(node, snap, action) or selection.ungated_reason(
            selection.repos_of(snap, node.id, self.config.repo_order),
            selection.gated_repos(self.config),
        )
        if reason is not None or action is None:
            return ClaimResult(Action.BLOCKED, reason)
        pairs = self._sync_pairs(node.id, snap)
        units = self.sync_units(pairs) if pairs else []
        if units:
            # A dependency landed below a branch this node builds on: that branch catches up
            # before any step starts on it, never after.
            job = self._landing().start_sync(node.id, pairs)
            branches = ", ".join(dict.fromkeys(base for _, base, _ in units))
            return ClaimResult(Action.BLOCKED, f"syncing {branches}", job=job)
        return self._claim(node, cycle, action, agent, session, ttl, worktree_dir, snap)

    def _hand_over(
        self, node: Node, agent: str, session: str, ttl: int | None
    ) -> ClaimResult | None:
        """A job stopped for an agent is handed, with its lease, to whoever claims its node."""
        job = next(
            (j for j in self.jobs.for_node(node.id) if j.state == JobState.NEEDS_AGENT), None
        )
        lease = self.runtime.get_lease(node.id)
        if job is None or lease is None or lease.ttl_seconds is not None:
            return None
        action = Action.MERGE if job.kind == JobKind.LAND else Action.SYNC
        model = model_for(action, node, 0, self.config.models)
        token = uuid.uuid4().hex
        ttl = ttl or self.ttl_for(action)
        if not self.runtime.take_over(node.id, agent, session, ttl, model, token):
            return ClaimResult(Action.BLOCKED, f"job {job.id} was handed to another agent first")
        self._ledger("job handover", node.id, {"job": job.id, "agent": agent})
        if action == Action.SYNC:
            branch, base = job.target, str(job.result.get("source", job.target))
        else:
            branch, base = self.branch_of(node.id), job.target
        return ClaimResult(
            action,
            None,
            model,
            job.id,
            [job.repo],
            branch,
            base,
            job.worktree,
            {job.repo: job.worktree} if job.worktree else {},
            token,
            {job.repo: base},
        )

    def _claim(
        self,
        node: Node,
        cycle: Cycle,
        action: Action,
        agent: str,
        session: str,
        ttl: int | None,
        worktree_dir: Path | None,
        snap: Snapshot,
    ) -> ClaimResult:
        claimed = lifecycle.claim(cycle)
        after = self._with_cycle(node, claimed)
        model = model_for(action, after, lifecycle.fix_round(claimed), self.config.models)
        lease = Lease(
            task_id=node.id,
            agent_id=agent,
            session_id=session,
            branch_name=self._step_branch(after, action),
            ttl_seconds=ttl or self.ttl_for(action),
            action=cast(LeaseAction, action),
            review_hash=self._review_hash(node.id) if action == Action.REVIEW else None,
            model=model,
        )
        locks = [
            FileLock(file_path=f, task_id=node.id) for f in self._locked_files(node, action, snap)
        ]
        if not self.runtime.claim(lease, locks, after):
            return ClaimResult(Action.BLOCKED, "claimed by another session at the same instant")
        self._ledger(
            "task start", node.id, {"action": action.value, "agent": agent, "from": node.status}
        )
        try:
            begun = self._begin(after, action, model, worktree_dir)
        except (OperationError, CalledProcessError, OSError, ValueError) as exc:
            self._unclaim(node)
            detail = getattr(exc, "stderr", None) or str(exc)
            raise OperationError(f"claim of {node.id} undone: {detail}".strip(), 409) from exc
        if begun.worktree is not None:
            self.runtime.set_worktree(node.id, lease.token, begun.worktree)
        own = [lock.file_path for lock in locks]
        lines = self._codegraph(own, begun.worktrees, snap)
        return replace(begun, token=lease.token, codegraph=lines)

    def _codegraph(self, own: list[str], worktrees: dict[str, str], snap: Snapshot) -> list[str]:
        # `start` swept expired leases before building `snap`, so every lock in it is live.
        locks = snap.graph_data().file_locks
        lines: list[str] = []
        for repo, worktree in worktrees.items():
            prefix = f"{repo}:"
            # Another repository's key keeps its prefix, so it never equals a path codegraph
            # prints for this one.
            held = {lock.file_path.removeprefix(prefix): lock.task_id for lock in locks}
            lines += codegraph_lines(
                self.root / repo,
                Path(worktree),
                [key.removeprefix(prefix) for key in own if key.startswith(prefix)],
                held,
            )
        return lines

    def _step_branch(self, node: Node, action: Action) -> str:
        """The branch a step works on: the node's own, or for a review of landed code, the
        target it landed on in its first repository."""
        if action == Action.REVIEW and node.claimed_from == Status.LANDED:
            return self.target_ref(node.id, next(iter(self.repos_of(node.id)), None))
        return self.branch_of(node.id)

    @staticmethod
    def _past_landing(node: Node, action: Action) -> bool:
        """A container's review of its landed target, or the fix of what that review found: both
        work in every repository its code landed in, whether its own branch is there or not."""
        if action == Action.REVIEW:
            return node.claimed_from == Status.LANDED
        return action == Action.FIX and node.fix_for == Outcome.REJECT

    def _begin(
        self, node: Node, action: Action, model: str, worktree_dir: Path | None
    ) -> ClaimResult:
        repos = self.repos_of(node.id)
        branch = self._step_branch(node, action)
        if self.is_container(node) and not self._past_landing(node, action):
            repos = [r for r in repos if gitops.rev_parse(self.root / r, f"refs/heads/{branch}")]
        worktree: str | None = None
        worktrees: dict[str, str] = {}
        if action in (Action.IMPLEMENT, Action.FIX):
            worktree, worktrees = self._cut(node, repos, worktree_dir, fix=action == Action.FIX)
        job = self._landing().start_land(node.id) if action == Action.MERGE else None
        bases = {repo: self.base_of(node.id, repo) for repo in repos}
        return ClaimResult(
            action,
            None,
            model,
            job,
            repos,
            branch,
            bases[repos[0]] if repos else self.target_of(node.id),
            worktree,
            worktrees,
            bases=bases,
        )

    def _landing(self) -> LandingJobs:
        if self.landing is None:
            raise OperationError("no landing engine is attached to these claims", 500)
        return self.landing

    def _cut(
        self, node: Node, repos: list[str], worktree_dir: Path | None, *, fix: bool
    ) -> tuple[str, dict[str, str]]:
        """A worktree of the node's branch in each repository. A branch that already exists is
        checked out as it stands, never cut again: a fix continues its implement's commits, and a
        reopened or re-imported node resumes its branch. Write-time validation refuses a change
        of target once the branch exists, so an existing branch is cut from this node's target.
        The exception is a fix whose branch carries nothing its target lacks, as after a landing:
        that branch is retired and the fix is cut from the target, so it builds on what landed."""
        # An absolute worktree_dir replaces the root: `Path / absolute` is the absolute path.
        base_dir = self.root / (worktree_dir or self.config.worktree_dir)
        branch = self.branch_of(node.id)

        def cut(repo: str, path: Path) -> str:
            base = self._base_ref(node.id, repo)
            if fix and self._spent(self.root / repo, branch, base):
                self._retire(repo, branch)
            return str(GitManager(self.root / repo).create_worktree(branch, path, base))

        if not self.is_container(node):
            repo = repos[0]
            path = cut(repo, base_dir / (node.id if repo == "." else f"{repo}-{node.id}"))
            return path, {repo: path}
        worktrees = {repo: cut(repo, base_dir / node.id / repo) for repo in repos}
        return str(base_dir / node.id), worktrees

    @staticmethod
    def _spent(repo_dir: Path, branch: str, base: str) -> bool:
        return bool(gitops.rev_parse(repo_dir, f"refs/heads/{branch}")) and (
            gitops.is_ancestor(repo_dir, branch, base) or gitops.diff_quiet(repo_dir, base, branch)
        )

    def _base_ref(self, node_id: str, repo: str) -> str:
        """The ref `node_id`'s branch is cut from in `repo`, creating each ancestor container
        branch on the way: a node builds on its landing target, never on the top branch past a
        parent that has not landed. A top branch not on origin yet is cut from the repository's
        default branch, and the first landing on it creates it; with that one missing too, the
        claim is refused, since the landing would have nowhere to push."""
        repo_dir = self.root / repo
        parent = self.ops.landing_parent(self.node(node_id).id)
        if parent is None:
            top = self.fetched_top(node_id, repo)
            if not gitops.rev_parse(repo_dir, f"origin/{top}"):
                raise OperationError(
                    f"{repo} has no origin/{top}: tm cuts branches from origin/{top} and lands "
                    "by pushing to origin",
                    409,
                )
            return f"origin/{top}"
        parent_branch = self.branch_of(parent)
        if not gitops.rev_parse(repo_dir, f"refs/heads/{parent_branch}"):
            gitops.ensure_branch(repo_dir, parent_branch, self._base_ref(parent, repo))
        return parent_branch

    def base_of(self, node_id: str, repo: str) -> str:
        """The branch `node_id`'s branch is read against in `repo`: the container branch it
        lands on, else its top as `fetched_top` reads it, which is the default branch the
        branch was cut from until the first landing creates the top on origin."""
        parent = self.ops.landing_parent(node_id)
        return self.branch_of(parent) if parent else self.fetched_top(node_id, repo)

    def fetched_top(self, node_id: str, repo: str) -> str:
        """The branch `node_id`'s chain lands on at the top in `repo`, fetched. One not on origin
        yet reads as the repository's default branch, which the first landing on it pushes."""
        repo_dir = self.root / repo
        top, default = self.ops.landing_branch(node_id, repo), self.ops.default_branch(repo)
        gitops.fetch(repo_dir, top)
        if top != default and not gitops.rev_parse(repo_dir, f"origin/{top}"):
            gitops.fetch(repo_dir, default)
            return default
        return top

    def _unclaim(self, original: Node) -> None:
        # A landing job created before the failure would otherwise hold the node forever.
        self._expire_jobs(original.id)
        with self.nodes.transaction():
            self.nodes.save_node(original)
            self.runtime.release_lease(original.id)
        self._ledger("task start undone", original.id, {})

    def _held(
        self,
        node_id: str,
        statuses: tuple[Status, ...],
        agent: str | None,
        token: str | None,
    ) -> tuple[Node, Lease]:
        node = self.node(node_id)
        if Status(node.status) not in statuses:
            wanted = " or ".join(statuses)
            raise OperationError(f"{node_id} is {node.status}; this closes {wanted}", 409)
        lease = self.runtime.get_lease(node_id)
        if lease is None:
            raise OperationError(f"{node_id} holds no lease: it was swept or released", 409)
        self.own(node_id, lease, agent, token)
        return node, lease

    def own(self, node_id: str, lease: Lease, agent: str | None, token: str | None) -> None:
        """A named agent, or a claim's token, closes only its own live step: an agent whose lease
        expired and was claimed again must not close its successor's."""
        if agent is not None and (lease.agent_id != agent or not self.live(lease)):
            raise OperationError(
                f"{agent} holds no live lease on {node_id}; {lease.agent_id} does", 409
            )
        if token is not None and (lease.token != token or not self.live(lease)):
            raise OperationError(
                f"token {token} holds no live lease on {node_id}: another claim holds it now",
                409,
            )

    def complete(self, node_id: str, agent: str | None = None, token: str | None = None) -> Status:
        node, _ = self._held(node_id, (Status.IMPLEMENTING, Status.FIXING), agent, token)
        return self._advance(self._with_written_migrations(node), Event.COMPLETE, "task complete")

    def _with_written_migrations(self, node: Node) -> Node:
        """`node` declaring each file under its repository's `migrations` its branch changes: a
        step that writes a migration makes the node a migration writer from then on, so its fix
        is re-reviewed and it holds its repository's migration chain."""
        repo = node.target_repo
        if self.is_container(node) or repo is None:
            return node
        repo_dir = self.root / repo
        base = self.target_ref(node.id, repo)
        if not gitops.rev_parse(repo_dir, base):
            base = f"origin/{self.ops.default_branch(repo)}"
        declared = declared_files_of(node, [])
        migrations = self.config.repo(repo).migrations
        written = [
            f
            for f in gitops.changed_files(repo_dir, base, self.branch_of(node.id))
            if f not in declared and writes_migration([f], migrations)
        ]
        if not written:
            return node
        frontmatter = {**node.frontmatter, "declared_files": [*declared, *written]}
        return node.model_copy(update={"frontmatter": frontmatter})

    def review(
        self,
        node_id: str,
        approve: bool,
        verdict: str | None = None,
        agent: str | None = None,
        token: str | None = None,
    ) -> Status:
        node, lease = self._held(node_id, (Status.REVIEWING,), agent, token)
        if self._review_hash(node_id) == lease.review_hash:
            raise OperationError(
                f"{node_id}:review is unchanged since the claim: write the findings with "
                f"`tm section set {node_id}:review` first",
                409,
            )
        event = Event.APPROVE if approve else Event.REJECT
        evidence = self._section_text(node_id, "review")
        return self._advance(node, event, "task review", verdict=verdict, evidence=evidence)

    def release(
        self,
        node_id: str,
        blocked: Blocker | None = None,
        agent: str | None = None,
        token: str | None = None,
    ) -> Status:
        node = self.node(node_id)
        lease = self.runtime.get_lease(node_id)
        if lease is None:
            raise OperationError(f"{node_id} holds no lease to release", 409)
        self.own(node_id, lease, agent, token)
        if blocked is not None:
            if not (blocked.depends or blocked.decision or blocked.condition):
                raise OperationError(
                    "--blocked names what the node now waits on: --depends, --decision or "
                    "--condition",
                    400,
                )
            if blocked.condition is not None and not is_executable(blocked.condition.command):
                raise OperationError(
                    "a condition needs a command that exits 0 once it holds; a question for a "
                    "person is a --decision",
                    400,
                )
            self._check_edges(node_id, blocked.depends)
        has_job = any(j.state in LIVE_JOBS for j in self.jobs.for_node(node_id))
        if Status(node.status) not in IN_STEP and not has_job:
            raise OperationError(f"{node_id} is {node.status}, not mid-step", 409)
        self._expire_jobs(node_id)
        event = Event.RELEASE_BLOCKED if blocked is not None else Event.RELEASE
        return self._advance(node, event, "task release", blocker=blocked)

    def heartbeat(self, node_id: str) -> bool:
        return self.runtime.heartbeat(node_id)

    def sweep(self) -> list[str]:
        swept = self.runtime.sweep_expired_leases()
        for node in self.nodes.list_nodes():
            if (
                node.id not in swept
                and node.kind != NodeKind.DECISION
                and Status(node.status) in IN_STEP
                and self.runtime.get_lease(node.id) is None
            ):
                swept.append(node.id)
        for node_id in swept:
            found = self.nodes.get_node(node_id)
            if found is None or found.kind == NodeKind.DECISION:
                continue
            expired = self._expire_jobs(node_id)
            if Status(found.status) in IN_STEP or expired:
                try:
                    self._advance(found, Event.EXPIRED, "lease sweep")
                except OperationError as exc:
                    # One node the lifecycle refuses must not stop every other node's sweep.
                    _log.warning("sweep left %s where it was: %s", node_id, exc)
        self._escalate_red_targets()
        return swept

    def landed(self, node_id: str, report: str) -> Status:
        return self._advance(self.node(node_id), Event.LANDED, "job land", note=("merge", report))

    def landing_failed(self, node_id: str, finding: str) -> Status:
        return self._advance(
            self.node(node_id),
            Event.OWN_DEFECT,
            "job land",
            note=("merge", finding),
            evidence=finding,
        )

    def landing_blocked(self, node_id: str, why: str) -> Status:
        """The node goes back to where its merge was claimed from; a condition is not a failure."""
        return self._advance(
            self.node(node_id), Event.RELEASE_BLOCKED, "job land", note=("merge", why)
        )

    def park(self, node_id: str) -> None:
        """A job stopped for an agent keeps its lease with no ttl, so sweep leaves it alone."""
        self.runtime.park(node_id)

    def count_unresolved(self, node_id: str) -> Status:
        """One step failure for a job an agent took over that stopped again: the job stays
        parked for the next agent, and at the cap the node fails as a release would."""
        with self.nodes.transaction():
            # Read under the write lock: a count read before it can be stale by the time it
            # writes, and two counts below the cap would then carry the node past it unfailed.
            node = self.node(node_id)
            if node.step_failures + 1 >= self.caps.step_failures:
                self._expire_jobs(node_id)
                return self._advance(node, Event.RELEASE, "job stopped again")
            failures = node.step_failures + 1
            self.nodes.save_node(node.model_copy(update={"step_failures": failures}))
        self._ledger("job stopped again", node_id, {"step_failures": failures})
        return Status(node.status)

    def _escalate_red_targets(self) -> list[str]:
        """One decision per red target that has held landings longer than
        red_target_decision_after, blocking every landing it holds. A node mid-step cannot take
        a new edge and is linked on a later sweep; a refusal is logged, never raised, since
        every claim sweeps first."""
        # One bulk read carries both the nodes and their conditions, so this sweep costs a fixed
        # number of statements regardless of estate size: every claim runs it, and discovery
        # sweeps before every wave.
        data = read_graph(self.nodes.db)
        parked: dict[tuple[str, str, str], list[tuple[str, datetime, list[str]]]] = {}
        for node in data.nodes.values():
            if node.kind == NodeKind.DECISION:
                continue
            if not any(c.needs.startswith(RED_TARGET) for c in data.conditions.get(node.id, [])):
                continue
            marks = [
                j.result["red_target"]
                for j in self.jobs.for_node(node.id)
                if "red_target" in j.result
            ]
            if not marks:
                continue
            mark = marks[-1]
            since = datetime.fromisoformat(str(mark["since"]))
            # A mark parked before marks named their target was parked on the default branch.
            key = (str(mark["repo"]), str(mark.get("target", DEFAULT_BRANCH)), str(mark["sha"]))
            parked.setdefault(key, []).append((node.id, since, list(mark["failing"])))
        opened: list[str] = []
        now = datetime.now(tz=UTC)
        for (repo, target, sha), entries in parked.items():
            oldest = min(since for _, since, _ in entries)
            if (now - oldest).total_seconds() < self.config.red_target_decision_after:
                continue
            # Main and a container branch can be red at one sha; each needs its own fixer.
            slug = f"red-target-{repo}-{target.replace('/', '-')}-{sha[:12]}"
            held = [node_id for node_id, _, _ in entries if not self.ops.busy(node_id)]
            existing = self.nodes.get_node(f"decision-{slug}")
            try:
                if existing is None and held:
                    opened.append(
                        self.ops.add_decision(
                            f"{target} of {repo} is red at {sha[:12]} and {len(held)} "
                            f"landing(s) wait on it: who fixes {target}?",
                            slug=slug,
                            context=f"Failing on {target} and at every parked landing:\n"
                            + "\n".join(entries[0][2]),
                            options=[
                                f"fixed|{target} is fixed; the parked landings retry on their own",
                                f"investigate|Someone investigates the red {target}",
                            ],
                            blocks=held,
                        )
                    )
                elif existing is not None and existing.status == DecisionStatus.OPEN:
                    unlinked = [
                        n for n in held if existing.id not in self.nodes.get_dependencies(n)
                    ]
                    if unlinked:
                        self.ops.link_decision(existing.id, add=unlinked)
            except OperationError as exc:
                _log.warning("red %s of %s at %s not escalated: %s", target, repo, sha, exc)
        return opened

    def _expire_jobs(self, node_id: str) -> int:
        live = [j for j in self.jobs.for_node(node_id) if j.state in LIVE_JOBS]
        for job in live:
            if self.jobs.set_state(job.model_copy(update={"state": JobState.EXPIRED}), LIVE_JOBS):
                _stop_process(job)
            self.jobs.release_branch(job.repo, job.target, job.id)
        return len(live)

    def _advance(
        self,
        node: Node,
        event: Event,
        command: str,
        *,
        verdict: str | None = None,
        note: tuple[str, str] | None = None,
        blocker: Blocker | None = None,
        evidence: str = "",
    ) -> Status:
        try:
            nxt = lifecycle.advance(self.snapshots.cycle(node), event, self.caps)
        except LifecycleError as exc:
            raise OperationError(str(exc), 409) from exc
        after = self._with_cycle(node, nxt)
        if verdict is not None:
            after = after.model_copy(update={"verdict": verdict})
        with self.nodes.transaction():
            self.nodes.save_node(after)
            # Every event that moves a node ends the step its lease was for.
            self.runtime.release_lease(node.id)
            if note is not None:
                self.note(node.id, *note)
            if blocker is not None:
                self._write_blocker(node.id, blocker)
            if nxt.status == Status.FAILED and node.status != Status.FAILED:
                reason = _FAILED_BECAUSE.get(event, "its step failed")
                open_failed_decision(self.ops, node.id, reason, evidence)
            roll_up_ancestors(self.ops, node.id)
        self._ledger(
            command, node.id, {"event": event.value, "from": node.status, "to": nxt.status}
        )
        return nxt.status

    def _write_blocker(self, node_id: str, blocker: Blocker) -> None:
        for dep in blocker.depends:
            self.nodes.add_relation(
                NodeRelation(
                    source_id=node_id, target_id=dep, relation_type=RelationType.DEPENDS_ON
                )
            )
        if blocker.decision is not None:
            spec = blocker.decision
            self.ops.add_decision(
                spec.question,
                options=spec.options,
                recommend=spec.recommend,
                raised_by=node_id,
                blocks=[node_id],
            )
        if blocker.condition is not None:
            self.nodes.add_condition(blocker.condition.model_copy(update={"node_id": node_id}))

    def _check_edges(self, node_id: str, depends: list[str]) -> None:
        missing = [d for d in depends if self.nodes.get_node(d) is None]
        if missing:
            raise OperationError(f"no node {', '.join(missing)}", 404)
        before = self.snapshots.build()
        after = replace(before, edges=[*before.edges, *((node_id, d) for d in depends)])
        self._refuse(validate(before, after, {node_id}, _UnmovedBranches()))

    @staticmethod
    def _refuse(refusals: list[Refusal]) -> None:
        if refusals:
            raise OperationError("; ".join(r.message for r in refusals), 409)

    def _idle(self, node_id: str) -> None:
        lease = self.runtime.get_lease(node_id)
        if lease is not None and self.live(lease):
            raise OperationError(
                f"{node_id} is mid-step, held by {lease.agent_id}: wait for the step to end, "
                "or release it",
                409,
            )
        job = next((j for j in self.jobs.for_node(node_id) if j.state in LIVE_JOBS), None)
        if job is not None:
            raise OperationError(f"{node_id} has job {job.id} {job.state}: wait for it", 409)

    def reopen(self, node_id: str, note: str, new_branch: bool = False) -> Status:
        node = self.node(node_id)
        self._idle(node_id)
        open_decisions = [
            d
            for d in self.nodes.get_dependencies(node_id)
            if (dep := self.nodes.get_node(d)) is not None
            and stored_status(dep) == DecisionStatus.OPEN
        ]
        if open_decisions:
            raise OperationError(
                f"an open decision blocks {node_id} ({', '.join(open_decisions)}): answer it first",
                409,
            )
        counted = [s for s in self._child_statuses(node_id) if s not in EXITS]
        all_done = bool(counted) and all(s == Status.COMPLETED for s in counted)
        try:
            nxt = lifecycle.reopen(self.snapshots.cycle(node), all_done)
        except LifecycleError as exc:
            raise OperationError(str(exc), 409) from exc
        if new_branch:
            self._retire_branch(node_id)
        return self._rewrite(node, nxt, ("reopen", note), "task reopen", clear_verdict=True)

    def reset(self, node_id: str, to: Status, note: str, outcome: Outcome | None = None) -> Status:
        node = self.node(node_id)
        self._idle(node_id)
        if to in (Status.LANDED, Status.COMPLETED):
            self._prove_landed(node_id)
        try:
            nxt = lifecycle.reset(self.snapshots.cycle(node), to, outcome)
        except LifecycleError as exc:
            raise OperationError(str(exc), 400) from exc
        # A reset says where the node is, and only LANDED or COMPLETED, proven above, says its
        # code is on its target: a reset to REVIEWED or FIXED leaves it off.
        unlanded = node.model_copy(update={"on_target": False})
        return self._rewrite(unlanded, nxt, ("reset", note), "task reset")

    def defer(self, node_id: str, note: str) -> Status:
        return self._set_aside(node_id, note, lifecycle.defer, "deferral", "task defer")

    def abandon(self, node_id: str, note: str) -> Status:
        return self._set_aside(node_id, note, lifecycle.abandon, "abandonment", "task abandon")

    def _set_aside(
        self,
        node_id: str,
        note: str,
        rule: Callable[[Cycle], Cycle],
        key: str,
        command: str,
    ) -> Status:
        node = self.node(node_id)
        self._idle(node_id)
        try:
            nxt = rule(self.snapshots.cycle(node))
        except LifecycleError as exc:
            raise OperationError(str(exc), 409) from exc
        return self._rewrite(node, nxt, (key, note), command)

    def _rewrite(
        self,
        node: Node,
        nxt: Cycle,
        note: tuple[str, str],
        command: str,
        *,
        clear_verdict: bool = False,
    ) -> Status:
        after = self._with_cycle(node, nxt)
        if clear_verdict:
            after = after.model_copy(update={"verdict": None})
        with self.nodes.transaction():
            # Read inside the transaction, so a claim landing just before it is seen.
            before = self.snapshots.build()
            self.nodes.save_node(after)
            self._refuse(validate(before, self.snapshots.build(), {node.id}, _UnmovedBranches()))
            clear_red_targets(self.nodes, node.id)
            self.note(node.id, *note)
            if nxt.status in (Status.DEFERRED, Status.ABANDONED):
                self._strand(node.id, nxt.status)
            roll_up_ancestors(self.ops, node.id)
        self._ledger(command, node.id, {"from": node.status, "to": nxt.status})
        return nxt.status

    def _prove_landed(self, node_id: str) -> None:
        branch = self.branch_of(node_id)
        at_top = self.ops.landing_parent(node_id) is None
        container = self.is_container(self.node(node_id))
        repos = self.repos_of(node_id)
        if not container and not repos:
            raise OperationError(
                f"{node_id} has no target_repo: nothing proves it landed; set --repo first", 409
            )
        for repo in repos:
            repo_dir = self.root / repo
            target, ref = self.target_of(node_id, repo), self.target_ref(node_id, repo)
            if at_top:
                gitops.fetch(repo_dir, target)
            if gitops.rev_parse(repo_dir, f"refs/heads/{branch}"):
                if not gitops.is_ancestor(repo_dir, branch, ref):
                    raise OperationError(
                        f"{branch} is not on {target} in {repo}: land it first", 409
                    )
            elif not container:
                raise OperationError(f"{branch} does not exist in {repo}", 409)
            # Each repository's checks read the target that repository's code landed on.
            passed, report = self.verify(node_id, ref, repo)
            if not passed:
                raise OperationError(f"verifications red on {target} in {repo}:\n{report}", 409)

    def _retire_branch(self, node_id: str) -> None:
        """Keeps the old branch as `<branch>@<n>`, and its worktree beside the old path under the
        same suffix, so the reopened node is cut clean where its worktree always goes. Every
        cloned repository is swept: a set-aside child that later comes back would otherwise build
        on the old branch in a repository only it touched."""
        branch = self.branch_of(node_id)
        for repo in self.known_repos():
            self._retire(repo, branch)

    def _retire(self, repo: str, branch: str) -> None:
        repo_dir = self.root / repo
        if not gitops.rev_parse(repo_dir, f"refs/heads/{branch}"):
            return
        n = 1
        while gitops.rev_parse(repo_dir, f"refs/heads/{branch}@{n}"):
            n += 1
        worktree = GitManager(repo_dir).find_worktree(branch)
        try:
            if worktree is not None:
                retired = worktree.with_name(f"{worktree.name}@{n}")
                gitops.move_worktree(repo_dir, worktree, retired)
            gitops.rename_branch(repo_dir, branch, f"{branch}@{n}")
        except CalledProcessError as exc:
            raise OperationError(
                f"could not retire {branch} in {repo}: {exc.stderr or exc}".strip(), 409
            ) from exc

    def _strand(self, node_id: str, status: Status) -> None:
        dependents = stranded_dependents(self.ops, node_id)
        if dependents:
            open_stranded_decision(self.ops, node_id, status, dependents)

    def _child_statuses(self, node_id: str) -> list[Status]:
        return [
            Status(kid.status)
            for kid_id in self.nodes.get_children(node_id)
            if (kid := self.nodes.get_node(kid_id)) is not None and kid.kind != NodeKind.DECISION
        ]

    @staticmethod
    def _with_cycle(node: Node, cycle: Cycle) -> Node:
        return node.model_copy(
            update={
                "status": cycle.status,
                "claimed_from": cycle.claimed_from,
                "outcome": cycle.outcome,
                "fix_for": cycle.fix_for,
                "review_cycles": cycle.review_cycles,
                "merge_attempts": cycle.merge_attempts,
                "step_failures": cycle.step_failures,
                "updated_at": datetime.now(tz=UTC),
            }
        )

    def known_repos(self) -> list[str]:
        """Every repository the estate names that is cloned under the root."""
        names = [
            *self.config.repo_order,
            *self.config.repos,
            *(n.target_repo for n in self.nodes.list_nodes() if n.target_repo),
        ]
        return [r for r in dict.fromkeys(names) if (self.root / r / ".git").exists()]

    def _sync_pairs(self, node_id: str, snap: Snapshot) -> list[SyncPair]:
        """(source, base, carrier) per merge a claim of `node_id` may need: the carrier is the
        node whose landing put the dependency's code on the source."""
        pairs: list[SyncPair] = []
        for dep in snap.inherited_edges(node_id):
            if not isinstance(snap.status(dep), Status):
                continue
            carrier = meeting(snap, node_id, dep)
            for source, base in sync_pairs(snap, node_id, dep):
                if (source, base, carrier) not in pairs:
                    pairs.append((source, base, carrier))
        return pairs

    def sync_units(self, pairs: list[SyncPair]) -> list[tuple[str, str, str]]:
        """(source ref, base branch, repository) for each pair and repository where the base
        lacks both the carrier's landed branch and the source. Only the dependency's code
        triggers a sync, never the source merely moving on; a repository without the carrier's
        branch has nothing to sync. A carrier landed with an empty diff is on the source's tree
        but not its history, so the base holding the source is what ends its sync."""
        units: list[tuple[str, str, str]] = []
        fetched: set[tuple[str, str]] = set()
        for source, base, carrier in pairs:
            base_branch, carried = self.branch_of(base), self.branch_of(carrier)
            for repo in self.known_repos():
                repo_dir = self.root / repo
                # `base` lands on `source`, so the source is read where `base`'s landing reads it.
                source_ref = self.target_ref(base, repo)
                top = self.target_of(base, repo) if source.startswith(TOP) else None
                if top is not None and (repo, top) not in fetched:
                    gitops.fetch(repo_dir, top)
                    fetched.add((repo, top))
                present = all(
                    gitops.rev_parse(repo_dir, ref)
                    for ref in (f"refs/heads/{base_branch}", f"refs/heads/{carried}", source_ref)
                )
                unit = (source_ref, base_branch, repo)
                if (
                    present
                    and not gitops.is_ancestor(repo_dir, carried, base_branch)
                    and not gitops.is_ancestor(repo_dir, source_ref, base_branch)
                    and unit not in units
                ):
                    units.append(unit)
        return units

    def hold_for_sync(self, node_id: str) -> bool:
        """tm's own lease on the node while its sync runs: the node stays at its status and
        unclaimed, and the sync's agent, if one is needed, takes this lease over.

        The claim expects the node's own status, so a racing claim that moved the node, or
        already holds it, makes this one write nothing."""
        node = self.node(node_id)
        lease = Lease(
            task_id=node_id,
            agent_id="tm",
            session_id="tm",
            branch_name=self.branch_of(node_id),
            ttl_seconds=self.ttl_for(Action.SYNC),
            action=Action.SYNC,
        )
        return self.runtime.claim(lease, [], node, expected=Status(node.status))

    def sync_done(self, node_id: str) -> None:
        self.node(node_id)
        self.runtime.release_lease(node_id)
        self._ledger("job sync", node_id, {"state": JobState.SUCCEEDED.value})

    def _section_text(self, node_id: str, key: str) -> str:
        found = self.nodes.get_section(node_id, key)
        return found.content if found else ""

    def _review_hash(self, node_id: str) -> str:
        return hashlib.sha256(self._section_text(node_id, "review").encode()).hexdigest()

    def note(self, node_id: str, key: str, text: str) -> None:
        """Appends a timestamped entry to `node_id:key`, keeping what earlier entries said."""
        existing = self._section_text(node_id, key)
        entry = f"{datetime.now(tz=UTC):%Y-%m-%d %H:%M} {text}"
        self.ops.set_section(node_id, key, f"{existing}\n\n{entry}" if existing else entry)

    def _ledger(self, command: str, node_id: str, payload: dict[str, Any]) -> None:
        self.ops.ledger_repo.append(
            LedgerEvent(
                actor_id=self.ops.actor, command=command, target_id=node_id, payload=payload
            )
        )
