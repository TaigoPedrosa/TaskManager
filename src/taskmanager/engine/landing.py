"""Landing jobs: tm merges, gates, pushes and verifies a node, as a detached process.

A landing can outlast the command runner's limit, so `tm task start` only records the job and
spawns `python -m taskmanager.engine.landing run <job> --root <root>`. An agent is handed the job
only when it stops at `needs_agent`.
"""

import argparse
import logging
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from taskmanager.core.enums import NodeKind
from taskmanager.core.models import Condition, Job
from taskmanager.core.status import Action, ConditionStage, JobKind, JobState, Outcome, Status
from taskmanager.db.cache_repo import CacheRepository
from taskmanager.db.job_repo import JobRepository
from taskmanager.engine import gates
from taskmanager.engine import git as gitops
from taskmanager.engine.claims import LIVE_JOBS, Claims, SyncPair
from taskmanager.engine.config import Gate, ProjectConfig
from taskmanager.engine.gates import GateRun
from taskmanager.engine.git import GitManager
from taskmanager.engine.operations import OperationError

HEARTBEAT_SECONDS = 30
PUSH_TRIES = 3
PUSH_PAUSE_SECONDS = 5
PUSH_ERROR_LINES = 5
LOCK_WAIT_SECONDS = 120
# ponytail: one fixed limit for every after_land hook; a repos.<repo> key if a hook needs longer.
AFTER_LAND_TIMEOUT_SECONDS = 600
LIVE = frozenset({JobState.RUNNING, JobState.NEEDS_AGENT})
_logger = logging.getLogger(__name__)

# Where a resumed job picks up, by the reason it stopped. A resolved conflict is gated like any
# merge; an `error` starts over, since nothing after it can be trusted.
RESUME_AT = {
    "conflict": "gate",
    "unattributed": "gate",
    "no gate": "gate",
    "no tests": "gate",
    "red": "gate",
    "push_failed": "push",
    "branch_locked": "push",
    "error": "start",
    "no branch": "start",
}


class Landing:
    def __init__(
        self,
        root: Path,
        config: ProjectConfig,
        claims: Claims,
        cache: CacheRepository,
        jobs: JobRepository,
        detach: bool = True,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.root = root
        self.config = config
        self.claims = claims
        self.cache = cache
        self.jobs = jobs
        self.detach = detach
        self.sleep = sleep
        claims.landing = self

    @classmethod
    def open(cls, root: Path, actor: str | None = None) -> Landing:
        claims = Claims.open(root, actor=actor)
        return cls(root, claims.config, claims, CacheRepository(claims.nodes.db), claims.jobs)

    def start_land(self, node_id: str) -> str:
        """Only a merge claim lands a node: anything else would push code no review passed, with
        no lease holding the node while it lands."""
        node = self.claims.node(node_id)
        lease = self.claims.runtime.get_lease(node_id)
        if (
            node.status != Status.MERGING
            or lease is None
            or lease.action != Action.MERGE
            or not self.claims.live(lease)
        ):
            raise OperationError(
                f"{node_id} is {node.status} with no live merge lease: only a merge claim "
                f"lands it, through `tm task start {node_id}`",
                409,
            )
        if any(
            j.kind == JobKind.LAND and j.state in LIVE_JOBS for j in self.jobs.for_node(node_id)
        ):
            raise OperationError(f"{node_id} is already landing", 409)
        repos = self.claims.repos_of(node_id)
        job = self._new_job(
            JobKind.LAND, node_id, repos[0], self.claims.target_of(node_id, repos[0]), {}
        )
        self._launch(job)
        return job.id

    def start_sync(self, node_id: str, pairs: list[SyncPair]) -> str:
        units = self.claims.sync_units(pairs)
        if not units:
            raise OperationError(f"{node_id} needs no sync", 409)
        if not self.claims.hold_for_sync(node_id):
            raise OperationError(f"{node_id} is held: its sync was not started", 409)
        source, target, repo = units[0]
        job = self._new_job(
            JobKind.SYNC,
            node_id,
            repo,
            target,
            {"units": [list(unit) for unit in units], "done": 0, "source": source},
        )
        self._spawn(job)
        return job.id

    def run(self, job_id: str) -> JobState:
        job = self._job(job_id)
        if job.state != JobState.RUNNING:
            return job.state
        job.pid = os.getpid()
        if not self.jobs.update(job):
            return self._state(job)
        with self._beating(job.node_id):
            try:
                return self._sync(job) if job.kind == JobKind.SYNC else self._land(job)
            except SystemExit, KeyboardInterrupt:
                # Stopped from outside (an expired or released lease): nothing else removes the
                # worktree this job left, and no later job reuses its path.
                self._discard(job, self._worktree_path(job))
                raise
            except Exception as exc:
                # Whatever stops a step stops the job for an agent: an exception let through
                # would leave it `running` with no process left to end it. The traceback goes to
                # the job's log.
                _logger.exception("job %s stopped on an error", job.id)
                detail = f"{type(exc).__name__}: {exc}\n{getattr(exc, 'stderr', '') or ''}"
                return self._needs_agent(job, "error", error=detail.strip())

    def resume(
        self,
        job_id: str,
        own_defect: str | None = None,
        push: bool = False,
        agent: str | None = None,
        token: str | None = None,
    ) -> JobState:
        job = self._job(job_id)
        if job.state != JobState.NEEDS_AGENT:
            raise OperationError(
                f"job {job_id} is {job.state}; only a job waiting for an agent resumes", 409
            )
        lease = self.claims.runtime.get_lease(job.node_id)
        if lease is None or lease.ttl_seconds is None:
            raise OperationError(
                f"job {job_id} is not handed to an agent: take it with "
                f"`tm task start {job.node_id}`",
                409,
            )
        self.claims.own(job.node_id, lease, agent, token)
        reason = str(job.result.get("reason"))
        if own_defect is not None:
            if job.kind == JobKind.SYNC:
                raise OperationError(
                    "a sync has no own defect: resolve it and resume, or release the node", 400
                )
            return self._own_defect(job, own_defect)
        if push and reason != "unattributed":
            raise OperationError("--push answers an unattributed red only", 400)
        if (
            reason == "conflict"
            and job.worktree
            and not gitops.settled(Path(job.worktree), self._merging(job))
        ):
            raise OperationError(
                f"the merge in {job.worktree} is not committed: resolve every path and commit, "
                "then resume",
                409,
            )
        step = "push" if push else RESUME_AT.get(reason, "start")
        # Removed only after the compare-and-swap below is won: a resume that loses it must not
        # delete the worktree the winner is building in.
        stale = job.worktree if step == "start" else None
        if stale:
            job.worktree = None
        job.step = step
        job.state = JobState.RUNNING
        job.result.pop("push_tries", None)
        job.result["resumed"] = int(job.result.get("resumed", 0)) + 1
        if not self.jobs.set_state(job, {JobState.NEEDS_AGENT}):
            raise OperationError(f"job {job_id} moved while it was being resumed", 409)
        if stale:
            self._discard(job, Path(stale))
        self.claims.heartbeat(job.node_id)
        if self.detach:
            return self._spawn(job)
        return self.run(job.id)

    def _land(self, job: Job) -> JobState:
        steps = {
            "start": self._build,
            "gate": self._gate,
            "push": self._push,
            "verify": self._verify,
        }
        while True:
            assert job.step is not None, "a landing job always has a step"
            nxt = steps[job.step](job)
            if isinstance(nxt, JobState):
                return nxt
            job.step = nxt
            job.heartbeat = datetime.now(tz=UTC)
            if not self.jobs.update(job):
                return self._state(job)

    def _build(self, job: Job) -> str | JobState:
        repo_dir, branch = self._dir(job), self.claims.branch_of(job.node_id)
        if self._at_top(job):
            top = self.claims.fetched_top(job.node_id, job.repo)
            target, creating = self.claims.ops.top_ref(job.repo, top), top != job.target
        else:
            target, creating = self.claims.target_ref(job.node_id, job.repo), False
        exists = bool(gitops.rev_parse(repo_dir, f"refs/heads/{branch}"))
        squash = None
        if not exists and not self.claims.is_container(self.claims.node(job.node_id)):
            # A task always has a branch by now; landing nothing would complete it untested.
            return self._needs_agent(
                job, "no branch", detail=f"{branch} does not exist in {job.repo}"
            )
        # A target not created yet is merged from its default branch and created by the push,
        # even when the branch adds nothing to it.
        if not exists or (
            not creating
            and (
                gitops.is_ancestor(repo_dir, branch, target)
                or gitops.diff_quiet(repo_dir, target, branch)
                or (squash := self._squashed(job, branch, target)) is not None
            )
        ):
            # A landing killed after its push, a repository with nothing to land, and a partial
            # container landing retried all resume here.
            job.result["already_landed"] = True
            if squash is not None:
                job.result["squash"] = squash
            return "verify"
        unmet = self.claims.conditions.unmet(job.node_id, ConditionStage.LANDING)
        self._drop_cleared_red_targets(job.node_id, unmet)
        if unmet:
            return self._blocked(job, "landing waits on " + "; ".join(c.needs for c in unmet))
        base = gitops.rev_parse(repo_dir, target)
        worktree = self._worktree_path(job)
        gitops.add_detached_worktree(repo_dir, worktree, base)
        job.worktree = str(worktree)
        job.result["base_sha"] = base
        if not gitops.merge_no_ff(worktree, branch, self._subject(job)):
            return self._needs_agent(job, "conflict")
        return "gate"

    def _gate(self, job: Job) -> str | JobState:
        worktree = Path(self._require_worktree(job))
        gate: Gate | None
        if self._at_top(job):
            gate = self._gate_config(job.repo, "main")
            if gate is None:
                return self._needs_agent(
                    job,
                    "no gate",
                    detail=f"repos.{job.repo}.gates.main is not configured (set it with `tm "
                    f'config set repos.{job.repo}.gates.main.command "<command>"`, then `tm job '
                    f"resume {job.id}`): a repository with no main gate cannot land on "
                    f"{job.target}",
                )
        else:
            # A node's own verification is red on its target by construction, so it has no
            # baseline: red is an own defect.
            passed, report, timed_out = self.claims.verify(
                job.node_id, gitops.rev_parse(worktree, "HEAD"), job.repo
            )
            job.result["own_verify"] = report
            if not passed and not self._advisory(job.node_id):
                if timed_out:
                    return self._needs_agent(job, "verify timeout", report=report)
                return self._own_defect(job, f"own verifications red at the merge:\n{report}")
            gate = self._gate_config(job.repo, "parent")
            if gate is None:
                return "push"
        tip = self._run_gate(gate, job, worktree)
        if tip.no_tests:
            return self._no_tests(job, tip)
        if tip.exit_code == 0:
            return "push"
        base = self._baseline(job, gate)
        verdict = gates.attribute(tip, base)
        if verdict == "push":
            return "push"
        if verdict == "own_defect":
            added = sorted((tip.failing or frozenset()) - (base.failing or frozenset()))
            why = f"adding {', '.join(added)}" if added else f"green on {job.target}"
            return self._own_defect(job, f"gate red at the tip, {why}:\n{tip.tail}")
        if verdict == "red_target":
            return self._park_red_target(job, gate, base)
        return self._needs_agent(job, "unattributed", tip=tip.tail, base=base.tail)

    def _push(self, job: Job) -> str | JobState:
        worktree = Path(self._require_worktree(job))
        if not self._at_top(job):
            return self._move_branch(job, worktree, "verify")
        repo_dir, remote = self._dir(job), self.claims.ops.remote(job.repo)
        moved = gitops.tracking(remote, job.target)
        while (tries := int(job.result.get("push_tries", 0))) < PUSH_TRIES:
            if tries:
                # A remote that did not answer usually does a few seconds later; trying again at
                # once spends every try on the same blip.
                self.sleep(PUSH_PAUSE_SECONDS * tries)
            head, run = gitops.ls_remote(repo_dir, remote, f"refs/heads/{job.target}")
            if head and head != job.result["base_sha"]:
                # The full gate runs at the tip that is pushed, so a moved target is merged in
                # and gated again.
                gitops.fetch(repo_dir, remote, job.target)
                subject = f"merge({job.node_id}): {moved} into its landing"
                # Recorded before the merge: a conflict parks the job here, and the resumed gate
                # attributes a red tip against the target it now contains.
                job.result["base_sha"] = gitops.rev_parse(repo_dir, moved)
                self._unsquash(job, worktree)
                if not gitops.merge_no_ff(worktree, moved, subject):
                    return self._needs_agent(job, "conflict")
                return "gate"
            # A remote that answered with no such branch is one this push creates.
            if head or run.returncode == 0:
                if not self._running(job):
                    return self._state(job)
                fold = self._fold_base(job)
                if (stopped := self._squash(job, worktree, fold)) is not None:
                    return stopped
                lease = self._keep_backup(job) if fold else None
                run = gitops.push(worktree, remote, job.target, lease)
                if run.returncode == 0:
                    self._after_land(job, worktree)
                    return "verify"
            job.result["push_tries"] = tries + 1
            job.result.setdefault("push_errors", []).append(_git_failure(run))
        return self._needs_agent(job, "push_failed")

    def _after_land(self, job: Job, worktree: Path) -> None:
        """The repository's `after_land` hook, for a container's push only: the push already
        happened, so a red hook is recorded and the landing goes on."""
        repo_config = self.config.repos.get(job.repo)
        hook = repo_config.after_land if repo_config is not None else None
        if hook is None or not self.claims.is_container(self.claims.node(job.node_id)):
            return
        command = gates.render(
            hook,
            target=job.target,
            branch=self.claims.branch_of(job.node_id),
            node=job.node_id,
            repo=job.repo,
        )
        run = gates.run_gate(command, worktree, AFTER_LAND_TIMEOUT_SECONDS, None)
        self.claims.note(
            job.node_id,
            "merge",
            f"{job.repo}: after_land `{command}` exited {run.exit_code}\n{run.tail}".rstrip(),
        )

    def _move_branch(self, job: Job, worktree: Path, done: str) -> str | JobState:
        """Moves a local container branch to the worktree's HEAD by compare-and-swap, under the
        branch's lock; a branch that moved meanwhile is merged in and gated again."""
        tries = int(job.result.get("push_tries", 0))
        if tries >= PUSH_TRIES:
            return self._needs_agent(job, "push_failed")
        if not self._lock(job):
            return self._needs_agent(job, "branch_locked")
        ref = f"refs/heads/{job.target}"
        repo_dir = self._dir(job)
        try:
            if not self._running(job):
                return self._state(job)
            if (stopped := self._squash(job, worktree)) is not None:
                return stopped
            head = gitops.rev_parse(worktree, "HEAD")
            if gitops.update_ref_cas(repo_dir, ref, head, str(job.result["base_sha"])):
                return done
            moved = gitops.rev_parse(repo_dir, ref)
        finally:
            self.jobs.release_branch(job.repo, job.target, job.id)
        job.result["push_tries"] = tries + 1
        job.result["base_sha"] = moved
        self._unsquash(job, worktree)
        if not gitops.merge_no_ff(worktree, moved, f"merge({job.node_id}): {job.target} moved"):
            return self._needs_agent(job, "conflict")
        return "gate"

    def _fold_base(self, job: Job) -> str | None:
        """The first landing's base when this spec landing may replace the commits its earlier
        landings put on the target, so the target keeps one commit of the spec; None otherwise,
        with the reason in the `:merge` note."""
        if job.kind != JobKind.LAND or self.claims.node(job.node_id).kind != NodeKind.SPEC:
            return None
        earlier = self.jobs.succeeded_land_jobs(job.node_id, job.repo)
        if not earlier:
            return None
        base, why = self._fold_refusal(job, earlier)
        if why is None:
            return base
        kept = self._drop_fold_backup(job)
        if job.result.get("fold_skipped") != why:
            job.result["fold_skipped"] = why
            tail = f"; {kept} holds the previous head" if kept else ""
            self.claims.note(job.node_id, "merge", f"{job.repo}: fold skipped, {why}{tail}")
        return None

    def _drop_fold_backup(self, job: Job) -> str | None:
        """Deletes the backup branch of a fold that did not go ahead, while it still points at
        the head it was cut at; the name of one that stays, else None."""
        name = str(job.result.pop("fold_backup", None) or "")
        at = str(job.result.pop("fold_backup_at", None) or "")
        if not name or (at and gitops.delete_branch(self._dir(job), name, at)):
            return None
        return name

    def _fold_refusal(self, job: Job, earlier: list[Job]) -> tuple[str, str | None]:
        """The first landing's base, and why the range above it cannot be rewritten (None when
        it can). Rewriting is safe only while nobody else can hold a commit of the range: the
        owner's branches and the fetch remote are both read."""
        repo_dir, target = self._dir(job), job.target
        base = str(earlier[0].result.get("base_sha") or "")
        if self.claims.ops.remote(job.repo) is not None:
            return base, "push is on, so the target is shared"
        span = gitops.rev_list(repo_dir, f"{base}..{target}") if base else []
        squashes = {str(j.result["squash"]) for j in earlier if j.result.get("squash")}
        if not span or not gitops.is_ancestor(repo_dir, base, target):
            return base, f"{target} no longer sits on the first landing's base"
        if not set(span) <= squashes:
            return base, f"commits that are not this spec's sit on {target}"
        remote = self.config.fetch_remote(job.repo)
        if remote:
            pushed, run = gitops.ls_remote(repo_dir, remote, f"refs/heads/{target}")
            if run.returncode != 0:
                return base, f"{remote} could not be read"
            if pushed and not (
                gitops.fetch(repo_dir, remote, target) and gitops.rev_parse(repo_dir, pushed)
            ):
                return base, f"{remote}/{target} could not be fetched"
            if pushed and any(gitops.is_ancestor(repo_dir, sha, pushed) for sha in span):
                return base, f"{remote}/{target} holds the range"
        own = self.claims.branch_of(job.node_id)
        for sha in span:
            for branch in gitops.contains(repo_dir, sha):
                if branch not in (target, own) and not branch.startswith((f"{own}@", "backup/")):
                    return base, f"branch {branch} holds {sha[:12]}"
        return base, None

    def _keep_backup(self, job: Job) -> str:
        """Sets the target's head aside as `backup/<node>` (`-r<n>` when taken) before the fold
        replaces it, and returns the lease that makes the replacing push fail if it moved."""
        head, repo_dir = str(job.result["base_sha"]), self._dir(job)
        name = str(job.result.get("fold_backup") or "")
        if not name or gitops.rev_parse(repo_dir, f"refs/heads/{name}") != head:
            name, n = f"backup/{job.node_id}", 1
            while not gitops.ensure_branch(repo_dir, name, head):
                n += 1
                name = f"backup/{job.node_id}-r{n}"
            job.result["fold_backup"], job.result["fold_backup_at"] = name, head
            self.jobs.update(job)
        return f"refs/heads/{job.target}:{head}"

    def _squash(self, job: Job, worktree: Path, parent: str | None = None) -> JobState | None:
        """Replaces a container's gated merge at HEAD by one commit that holds its tree on the
        target's head, so the target gains one commit and the container's branch keeps the
        granular history. None when the worktree is ready to move the target."""
        if job.kind != JobKind.LAND or not self.claims.is_container(self.claims.node(job.node_id)):
            return None
        self._unsquash(job, worktree)
        repo_dir = self._dir(job)
        merged, tree = gitops.rev_parse(worktree, "HEAD"), gitops.tree_of(worktree, "HEAD")
        sha = gitops.commit_tree(
            repo_dir,
            tree,
            parent or str(job.result["base_sha"]),
            self.claims.squash_subject(job.node_id, job.repo),
        )
        gitops.reset_soft(worktree, sha)
        if gitops.tree_of(worktree, "HEAD") != tree:
            return self._needs_agent(job, "error")
        job.result.update(
            squash=sha,
            merged=merged,
            tip=gitops.rev_parse(repo_dir, self.claims.branch_of(job.node_id)),
        )
        # Written before the target moves: a landing killed after its move is recognised by it.
        return None if self.jobs.update(job) else self._state(job)

    @staticmethod
    def _unsquash(job: Job, worktree: Path) -> None:
        """Puts HEAD back on the gated merge, which a merge of a moved target and a resolved
        conflict both build on; the trees are equal, so the index stays as it is."""
        if (merged := job.result.pop("merged", None)) is not None:
            gitops.reset_soft(worktree, str(merged))

    def _squashed(self, job: Job, branch: str, target: str) -> str | None:
        """A container's branch is never an ancestor of its target once squashed: an earlier
        landing job of this branch's current tip that put its squash on the target proves it,
        and the squash is returned."""
        repo_dir = self._dir(job)
        tip = gitops.rev_parse(repo_dir, branch)
        return next(
            (
                str(j.result["squash"])
                for j in reversed(self.jobs.for_node(job.node_id))
                if j.kind == JobKind.LAND
                and j.repo == job.repo
                and j.result.get("squash")
                and j.result.get("tip") == tip
                and gitops.is_ancestor(repo_dir, str(j.result["squash"]), target)
            ),
            None,
        )

    def _verify(self, job: Job) -> JobState:
        passed, report, timed_out = self.claims.verify(job.node_id, self._target_ref(job), job.repo)
        if not passed and not self._advisory(job.node_id):
            if timed_out:
                return self._needs_agent(job, "verify timeout", report=report)
            # The code is on the target but the node's assertion does not hold there.
            return self._own_defect(
                job, f"verifications red on {job.target} after landing:\n{report}"
            )
        self._remove_worktree(job)
        return self._succeed(job, report)

    def _container_lines(self, job: Job) -> str:
        """The `:merge` lines a container's landing records: the `backup:` branch when a child
        landed on it, and the `squash:` commit once it is on the target, which export, restore
        and the reviews read back. A squash already recorded is not written twice."""
        lines = ""
        node_id = job.node_id
        if any(
            self.claims.ops.landing_parent(child) == node_id
            for child in self.claims.nodes.get_children(node_id)
        ):
            lines += f"\nbackup: {self.claims.branch_of(node_id)}"
        squash = str(job.result.get("squash") or "")
        if (
            squash
            and gitops.is_ancestor(self._dir(job), squash, self._target_ref(job))
            and self.claims.recorded_squashes(node_id).get(job.repo) != squash
        ):
            lines += f"\nsquash: {job.repo} {squash}"
        return lines

    def _succeed(self, job: Job, report: str) -> JobState:
        summary = f"{job.repo}: landed on {job.target}\n{report}"
        if self.claims.is_container(self.claims.node(job.node_id)):
            summary += self._container_lines(job)
        if job.result.get("fold_backup"):
            summary += (
                f"\nfolded into one commit, the previous head kept at {job.result['fold_backup']}"
            )
        repos = self.claims.repos_of(job.node_id)
        later = repos[repos.index(job.repo) + 1 :] if job.repo in repos else []
        # The job's SUCCEEDED and the node's result commit together: a reader polling the job
        # never sees it succeeded on a node that has not moved, and a crash leaves neither.
        with self.claims.nodes.transaction():
            if not self._end(job, JobState.SUCCEEDED, verify=report):
                return self._state(job)
            if not later:
                self.claims.landed(job.node_id, summary)
                return JobState.SUCCEEDED
            # One landing job per repository, in order, under the same lease; a pushed target
            # is never rolled back, so a later repository's failure leaves this one landed.
            following = self._new_job(
                JobKind.LAND,
                job.node_id,
                later[0],
                self.claims.target_of(job.node_id, later[0]),
                {},
            )
            job.result["next"] = following.id
            self.jobs.set_state(job, {JobState.SUCCEEDED})
            self.claims.note(job.node_id, "merge", summary)
        # The next repository runs as its own job, so whatever stops it is ended against it,
        # not against this one, which has already succeeded.
        if not self.detach:
            return self.run(following.id)
        self._spawn(following)
        return JobState.SUCCEEDED

    def _sync(self, job: Job) -> JobState:
        units = job.result["units"]
        while int(job.result["done"]) < len(units):
            source, target, repo = units[int(job.result["done"])]
            job.repo, job.target, job.result["source"] = repo, target, source
            stopped = self._sync_unit(job)
            if stopped is not None:
                return stopped
            job.result["done"] = int(job.result["done"]) + 1
            job.result.pop("push_tries", None)
            job.step = "start"
            if not self.jobs.update(job):
                return self._state(job)
        if not self._end(job, JobState.SUCCEEDED):
            return self._state(job)
        self.claims.sync_done(job.node_id)
        return JobState.SUCCEEDED

    def _sync_unit(self, job: Job) -> JobState | None:
        """Merges the source into one container branch in one repository, gates it with the
        repository's `parent` gate when one is configured, and moves the branch by
        compare-and-swap. None when the unit is done."""
        repo_dir, source = self._dir(job), str(job.result["source"])
        while True:
            if job.step == "start":
                if gitops.is_ancestor(repo_dir, source, job.target):
                    return None
                base = gitops.rev_parse(repo_dir, f"refs/heads/{job.target}")
                worktree = self._worktree_path(job)
                gitops.add_detached_worktree(repo_dir, worktree, base)
                job.worktree = str(worktree)
                job.result["base_sha"] = base
                subject = f"merge({job.node_id}): sync {source} into {job.target}"
                if not gitops.merge_no_ff(worktree, source, subject):
                    return self._needs_agent(job, "conflict")
                job.step = "gate"
            elif job.step == "gate":
                gate = self._gate_config(job.repo, "parent")
                if gate is not None:
                    worktree = Path(self._require_worktree(job))
                    run = self._run_gate(gate, job, worktree)
                    if run.no_tests:
                        return self._no_tests(job, run)
                    if run.exit_code != 0:
                        return self._needs_agent(job, "red", tip=run.tail)
                job.step = "push"
            else:
                moved = self._move_branch(job, Path(self._require_worktree(job)), "done")
                if isinstance(moved, JobState):
                    return moved
                if moved == "done":
                    self._remove_worktree(job)
                    return None
                job.step = moved
            job.heartbeat = datetime.now(tz=UTC)
            if not self.jobs.update(job):
                return self._state(job)

    def _end(self, job: Job, state: JobState, **result: Any) -> bool:
        """False when the job was expired under us (its lease swept or released): the node is no
        longer this job's to move."""
        job.state = state
        job.result.update(result)
        job.heartbeat = datetime.now(tz=UTC)
        return self.jobs.set_state(job, LIVE)

    def _state(self, job: Job) -> JobState:
        current = self.jobs.get(job.id)
        return current.state if current is not None else job.state

    def _running(self, job: Job) -> bool:
        """Read just before a write nothing rolls back (a push, a branch swap): a job released
        or swept meanwhile must not land."""
        return self._state(job) == JobState.RUNNING

    def _needs_agent(self, job: Job, reason: str, **detail: str) -> JobState:
        if not self._end(job, JobState.NEEDS_AGENT, reason=reason, **detail):
            return self._state(job)
        self.claims.park(job.node_id)
        if job.result.get("resumed"):
            # An agent took this job over and it stopped again unresolved; counting that is what
            # bounds a stop no agent can resolve.
            self.claims.count_unresolved(job.node_id)
            return self._state(job)
        return JobState.NEEDS_AGENT

    def _own_defect(self, job: Job, finding: str) -> JobState:
        self._remove_worktree(job)
        if not self._end(job, JobState.OWN_DEFECT, finding=finding):
            return self._state(job)
        self.claims.landing_failed(job.node_id, f"{job.repo}: {finding}")
        return JobState.OWN_DEFECT

    def _blocked(self, job: Job, why: str, **extra: Any) -> JobState:
        self._remove_worktree(job)
        if not self._end(job, JobState.CONDITION_UNMET, why=why, **extra):
            return self._state(job)
        self.claims.landing_blocked(job.node_id, f"{job.repo}: {why}")
        return JobState.CONDITION_UNMET

    def _park_red_target(self, job: Job, gate: Gate, base: GateRun) -> JobState:
        sha = str(job.result["base_sha"])
        failing = sorted(base.failing or frozenset())
        remote = self._at_top(job)
        red = job.target
        if remote and not gitops.rev_parse(self._dir(job), self.claims.ops.top_ref(job.repo, red)):
            # A target not created yet was merged from its default branch: that is what is red.
            red = self.claims.ops.default_branch(job.repo)
        gates.clear_red_targets(self.claims.nodes, job.node_id)
        self.claims.nodes.add_condition(
            Condition(
                node_id=job.node_id,
                idx=0,
                needs=f"{gates.RED_TARGET}: {job.repo} {red} at {sha[:12]} fails the "
                f"{len(failing)} test(s) this landing fails",
                command=gates.red_target_command(
                    self.root, job.repo, sha, gates.template_hash(gate), red, remote=remote
                ),
                stage=ConditionStage.LANDING,
            )
        )
        mark = {
            "repo": job.repo,
            "target": red,
            "sha": sha,
            "failing": failing,
            "since": datetime.now(tz=UTC).isoformat(),
        }
        return self._blocked(
            job, f"{red} is red at {sha[:12]} with the same failures", red_target=mark
        )

    def _drop_cleared_red_targets(self, node_id: str, unmet: list[Condition]) -> None:
        still = {c.idx for c in unmet}
        for cond in self.claims.nodes.get_conditions(node_id):
            if cond.needs.startswith(gates.RED_TARGET) and cond.idx not in still:
                self.claims.nodes.remove_condition(node_id, cond.idx)

    def _new_job(
        self, kind: JobKind, node_id: str, repo: str, target: str, result: dict[str, Any]
    ) -> Job:
        return self.jobs.create(
            Job(
                kind=kind,
                node_id=node_id,
                repo=repo,
                target=target,
                state=JobState.RUNNING,
                step="start",
                result=result,
            )
        )

    def _launch(self, job: Job) -> None:
        if not self.detach:
            return
        log = self.root / ".taskmanager" / "jobs" / f"{job.id}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("ab") as out:
            # The child records its own pid: a write from here could overwrite its first step.
            subprocess.Popen(
                [sys.executable, "-m", "taskmanager.engine.landing", "run", job.id]
                + ["--root", str(self.root)],
                cwd=self.root,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

    def _spawn(self, job: Job) -> JobState:
        """Launches `job`'s process; a launch that fails stops the job for an agent, so no job
        is left running with no process to end it."""
        try:
            self._launch(job)
        except OSError as exc:
            return self._needs_agent(job, "error", error=f"could not launch: {exc}")
        return JobState.RUNNING

    def _job(self, job_id: str) -> Job:
        job = self.jobs.get(job_id)
        if job is None:
            raise OperationError(f"job '{job_id}' not found", 404)
        return job

    @contextmanager
    def _beating(self, node_id: str) -> Iterator[None]:
        stop = threading.Event()

        def beat() -> None:
            while not stop.wait(HEARTBEAT_SECONDS):
                try:
                    self.claims.heartbeat(node_id)
                except Exception:
                    # One missed beat (a locked database) must not end them all: the lease would
                    # lapse mid-gate.
                    _logger.exception("heartbeat for %s failed", node_id)

        thread = threading.Thread(target=beat, daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join()

    def _lock(self, job: Job) -> bool:
        deadline = time.monotonic() + LOCK_WAIT_SECONDS
        while not self.jobs.acquire_branch(job.repo, job.target, job.id):
            if time.monotonic() > deadline:
                return False
            time.sleep(1)
        return True

    def _baseline(self, job: Job, gate: Gate) -> GateRun:
        """The gate at the untouched target sha, run once per (repo, sha, gate): a landing
        that finds another's run under way waits for its result instead of running its own."""
        sha = str(job.result["base_sha"])
        key = gates.template_hash(gate)
        lock = f"baseline/{sha}/{key}"
        deadline = time.monotonic() + gate.timeout + LOCK_WAIT_SECONDS
        while not self.jobs.acquire_branch(job.repo, lock, job.id):
            cached = self.cache.get_baseline(job.repo, sha, key)
            if cached is not None:
                return cached
            if time.monotonic() > deadline:
                break
            time.sleep(1)
        try:
            cached = self.cache.get_baseline(job.repo, sha, key)
            if cached is not None:
                return cached
            worktree = self._worktree_path(job, "base")
            self._discard(job, worktree)
            gitops.add_detached_worktree(self._dir(job), worktree, sha)
            try:
                run = self._run_gate(gate, job, worktree)
            finally:
                self._discard(job, worktree)
            # A timeout or a crash outside the tests names no failing set; cached, it would answer
            # every later landing on this sha as unattributed until the target moves.
            if run.exit_code == 0 or run.failing is not None:
                self.cache.put_baseline(job.repo, sha, key, run)
            return run
        finally:
            self.jobs.release_branch(job.repo, lock, job.id)

    def _advisory(self, node_id: str) -> bool:
        """A rejection nobody below fixes lands where the parent's review sees it, so its own
        verifications are reported, not enforced."""
        node = self.claims.node(node_id)
        return node.review and not node.fix and node.outcome == Outcome.REJECT

    def _merging(self, job: Job) -> str:
        if job.kind == JobKind.SYNC:
            return str(job.result["source"])
        return self.claims.branch_of(job.node_id)

    def _target_ref(self, job: Job) -> str:
        if self._at_top(job):
            gitops.fetch(self._dir(job), self.claims.ops.remote(job.repo), job.target)
        return self.claims.target_ref(job.node_id, job.repo)

    def _at_top(self, job: Job) -> bool:
        """The landing's target is its chain's top branch, which is pushed to the repository's
        remote, or to the repository itself when it has none; a container branch is local to the
        shared clone and moved in place."""
        return self.claims.ops.landing_parent(job.node_id) is None

    def _gate_config(self, repo: str, which: Literal["main", "parent"]) -> Gate | None:
        repo_config = self.config.repos.get(repo)
        return repo_config.gates.get(which) if repo_config is not None else None

    def _run_gate(self, gate: Gate, job: Job, worktree: Path) -> GateRun:
        return gates.run_gate(
            self._render(gate, job, worktree),
            worktree,
            gate.timeout,
            gate.junit,
            gate.tests_ran,
            gate.failing_pattern,
        )

    def _no_tests(self, job: Job, run: GateRun) -> JobState:
        return self._needs_agent(
            job,
            "no tests",
            detail=f"the test gate exited 0 but {run.no_tests}: a gate that ran no tests is not "
            f"green (fix the gate's command, report or tests_ran in repos.{job.repo}.gates, then "
            f"`tm job resume {job.id}`)",
            tip=run.tail,
        )

    def _render(self, gate: Gate, job: Job, worktree: Path) -> str:
        return gates.render(
            gate.command, worktree=str(worktree), node=job.node_id, repo=job.repo, target=job.target
        )

    def _subject(self, job: Job) -> str:
        return f"merge({job.node_id}): land {self.claims.branch_of(job.node_id)} on {job.target}"

    def _dir(self, job: Job) -> Path:
        return self.root / job.repo

    def _worktree_path(self, job: Job, suffix: str = "") -> Path:
        name = f"{job.id}-{suffix}" if suffix else job.id
        return self.root / self.config.worktree_dir / "land" / name

    def _require_worktree(self, job: Job) -> str:
        if not job.worktree:
            raise OperationError(f"job {job.id} has no worktree at step {job.step}", 409)
        return job.worktree

    def _remove_worktree(self, job: Job) -> None:
        if job.worktree:
            self._discard(job, Path(job.worktree))
        job.worktree = None

    def _discard(self, job: Job, path: Path) -> None:
        """Removes one of this job's worktrees and its registration, whatever state a crash or a
        hand deletion left it in: git refuses to add a path it still lists, or to remove a
        directory it no longer owns."""
        repo_dir = self._dir(job)
        if path.exists():
            try:
                GitManager(repo_dir).remove_worktree(path, force=True)
            except subprocess.CalledProcessError:
                shutil.rmtree(path, ignore_errors=True)
        gitops.prune_worktrees(repo_dir)


def _git_failure(run: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    return {
        "command": shlex.join(run.args),
        "exit_code": run.returncode,
        "stderr": "\n".join(run.stderr.splitlines()[:PUSH_ERROR_LINES]),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m taskmanager.engine.landing")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run a landing or sync job to its end or next stop")
    run.add_argument("job_id")
    run.add_argument("--root", type=Path, required=True)
    args = parser.parse_args(argv)
    # `finally` blocks run only if the SIGTERM of an expired lease ends the process as an exit.
    signal.signal(signal.SIGTERM, lambda signum, _frame: sys.exit(128 + signum))
    print(Landing.open(args.root.resolve()).run(args.job_id))
    return 0


if __name__ == "__main__":
    sys.exit(main())
