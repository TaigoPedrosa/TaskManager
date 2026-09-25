import re
from dataclasses import replace
from datetime import UTC, datetime
from typing import Literal

from taskmanager.core.display import Facts, display_status, phase
from taskmanager.core.enums import NodeKind, RelationType, VerificationType
from taskmanager.core.lifecycle import Cycle, next_action
from taskmanager.core.models import Node
from taskmanager.core.rollup import rollup
from taskmanager.core.status import (
    EXITS,
    Action,
    ConditionStage,
    DecisionStatus,
    JobKind,
    JobState,
    Status,
)
from taskmanager.db.cache_repo import CacheRepository
from taskmanager.db.job_repo import JobRepository
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository, lease_alive
from taskmanager.engine.chains import satisfied
from taskmanager.engine.stepgraph import SnapNode, Snapshot

CONTAINERS = frozenset({NodeKind.PLAN, NodeKind.SPEC})

# A job stopped for an agent still owns its node's worktree and lease, as a running one does.
_LIVE_JOB = frozenset({JobState.RUNNING, JobState.NEEDS_AGENT})
_LOCKING = frozenset({Action.IMPLEMENT, Action.FIX})
_NOT_STARTED = frozenset({Status.READY}) | EXITS
# `tm verify run --ref` exports TM_VERIFY_REF, so an `origin/main` fallback inside this
# expansion follows the ref being verified; only a bare `origin/main` pins a check to main.
_VERIFY_REF_EXPANSION = re.compile(r"\$\{TM_VERIFY_REF:-[^}]*\}")


def writes_migration(files: list[str]) -> bool:
    return any("migrations/versions/" in f for f in files)


def names_origin_main(command: str) -> bool:
    return "origin/main" in _VERIFY_REF_EXPANSION.sub("", command)


def stored_status(node: Node) -> Status | DecisionStatus:
    return node.status


def cycle_of(node: Node) -> Cycle:
    if not isinstance(node.status, Status):
        raise ValueError(f"decision {node.id!r} has no cycle")  # noqa: TRY004
    return Cycle(
        status=node.status,
        container=node.kind in CONTAINERS,
        review=node.review,
        fix=node.fix,
        outcome=node.outcome,
        fix_for=node.fix_for,
        claimed_from=node.claimed_from,
        review_cycles=node.review_cycles,
        merge_attempts=node.merge_attempts,
        step_failures=node.step_failures,
    )


def apply_cycle(node: Node, c: Cycle) -> Node:
    """`node` carrying `c`'s stored fields, for a writer to save."""
    return node.model_copy(
        update={
            "status": c.status,
            "claimed_from": c.claimed_from,
            "outcome": c.outcome,
            "fix_for": c.fix_for,
            "review_cycles": c.review_cycles,
            "merge_attempts": c.merge_attempts,
            "step_failures": c.step_failures,
            "updated_at": datetime.now(tz=UTC),
        }
    )


def node_busy(
    runtime_repo: RuntimeRepository, job_repo: JobRepository | None, node_id: str
) -> bool:
    """A live lease or an unfinished job: a step is running, and nothing may move the node."""
    lease = runtime_repo.get_lease(node_id)
    if lease is not None and lease_alive(
        lease.ttl_seconds, lease.last_heartbeat, datetime.now(tz=UTC)
    ):
        return True
    return job_repo is not None and any(j.state in _LIVE_JOB for j in job_repo.for_node(node_id))


def roll_up_ancestors(node_repo: NodeRepository, node_id: str) -> list[tuple[str, Status]]:
    """Re-derive each ancestor container's status after a change under it, in the caller's
    transaction, and return every container that moved with its new status. The completion of a
    container whose branches match their base is the landing's to decide, not this."""
    moved: list[tuple[str, Status]] = []
    seen = {node_id}
    parents = node_repo.get_parent_ids(node_id)
    while parents and parents[0] not in seen:
        parent = node_repo.get_node(parents[0])
        if parent is None or parent.kind not in CONTAINERS:
            break
        seen.add(parent.id)
        children = [node_repo.get_node(c) for c in node_repo.get_children(parent.id)]
        statuses = [
            s for c in children if c is not None and isinstance(s := stored_status(c), Status)
        ]
        current = cycle_of(parent)
        derived = rollup(current.status, statuses)
        if derived != current.status:
            node_repo.save_node(apply_cycle(parent, replace(current, status=derived)))
            moved.append((parent.id, derived))
        parents = node_repo.get_parent_ids(parent.id)
    return moved


# ponytail: one query per node for its files, lease and jobs; bulk reads once a graph is large
# enough for it to show.
class SnapshotBuilder:
    """The whole graph as the pure rules read it, and the per-node facts display derives from."""

    def __init__(
        self, node_repo: NodeRepository, runtime_repo: RuntimeRepository, job_repo: JobRepository
    ) -> None:
        self.node_repo = node_repo
        self.runtime_repo = runtime_repo
        self.job_repo = job_repo

    def build(self) -> Snapshot:
        parents: dict[str, str] = {}
        for source, target in self.node_repo.relations(RelationType.CONTAINS):
            parents.setdefault(target, source)
        nodes = {n.id: self._snap(n, parents.get(n.id)) for n in self.node_repo.list_nodes()}
        return Snapshot(nodes=nodes, edges=self.node_repo.relations(RelationType.DEPENDS_ON))

    def cycle(self, node: Node) -> Cycle:
        return cycle_of(node)

    def lock_set(self, node_id: str, snapshot: Snapshot) -> list[str]:
        """The files a claim of this node locks: its declared files, or for a container that
        declares none, the union of its descendants'."""
        own = self.node_repo.declared_files(node_id)
        if own or snapshot.nodes[node_id].kind not in CONTAINERS:
            return own
        files = [f for d in snapshot.descendants(node_id) for f in self.node_repo.declared_files(d)]
        return list(dict.fromkeys(files))

    def facts(self, node_id: str, snapshot: Snapshot) -> Facts:
        """Everything display derivation needs beyond the node's own cycle. `unmet_condition`
        is left False: conditions run through the ConditionRunner, whose result a caller folds
        in with `dataclasses.replace`."""
        node = self.node_repo.get_node(node_id)
        if node is None:
            raise KeyError(node_id)
        jobs = self.job_repo.for_node(node_id)
        deps = snapshot.inherited_edges(node_id)
        decisions = [d for d in deps if snapshot.nodes[d].kind == NodeKind.DECISION]
        locking = next_action(cycle_of(node)) in _LOCKING
        return Facts(
            lease=self._lease(node_id, datetime.now(tz=UTC)),
            job_needs_agent=any(
                j.kind == JobKind.LAND and j.state == JobState.NEEDS_AGENT for j in jobs
            ),
            open_decision=any(snapshot.status(d) == DecisionStatus.OPEN for d in decisions),
            unsatisfied_edge=any(
                not satisfied(snapshot, node_id, d) for d in deps if d not in decisions
            ),
            sync_pending=any(j.kind == JobKind.SYNC and j.state in _LIVE_JOB for j in jobs),
            files_locked=locking
            and bool(self.runtime_repo.get_conflicting_tasks(self.lock_set(node_id, snapshot))),
            descendant_started=any(
                snapshot.status(d) not in _NOT_STARTED for d in snapshot.descendants(node_id)
            ),
        )

    def _lease(self, node_id: str, now: datetime) -> Literal["live", "expired", "none"]:
        lease = self.runtime_repo.get_lease(node_id)
        if lease is None:
            return "none"
        return "live" if lease_alive(lease.ttl_seconds, lease.last_heartbeat, now) else "expired"

    def _snap(self, node: Node, parent: str | None) -> SnapNode:
        commands = [
            v.expected_pattern or v.target_path
            for v in self.node_repo.get_verifications(node.id)
            if v.verification_type == VerificationType.TEST_COMMAND
        ]
        busy = node_busy(self.runtime_repo, self.job_repo, node.id)
        return SnapNode(
            id=node.id,
            kind=node.kind,
            parent=parent,
            merge=node.merge,
            status=stored_status(node),
            review=node.review,
            fix=node.fix,
            repo=node.target_repo,
            writes_migration=writes_migration(self.node_repo.declared_files(node.id)),
            busy=busy,
            literal_origin_main=any(names_origin_main(c) for c in commands),
        )


class DisplayView:
    """One snapshot, and the cached condition results, that every display in one read shares.

    Conditions are read from the cache only: a reader must never wait on a condition's command.
    """

    def __init__(
        self, builder: SnapshotBuilder, cache: CacheRepository | None = None, max_age: int = 0
    ) -> None:
        self.builder = builder
        self.snapshot = builder.build()
        self.cache = cache
        self.max_age = max_age

    def _unmet(self, node: Node) -> bool:
        if self.cache is None:
            return False
        stages = {ConditionStage.CLAIM}
        if next_action(self.builder.cycle(node)) == Action.MERGE:
            stages.add(ConditionStage.LANDING)
        for condition in self.builder.node_repo.get_conditions(node.id):
            if condition.stage not in stages:
                continue
            code = self.cache.get_condition(node.id, condition.idx, condition.command, self.max_age)
            if code is not None and code != 0:
                return True
        return False

    def display(self, node: Node) -> str:
        status = stored_status(node)
        if not isinstance(status, Status):
            return status.value
        facts = replace(
            self.builder.facts(node.id, self.snapshot), unmet_condition=self._unmet(node)
        )
        return display_status(self.builder.cycle(node), facts).value


def phase_of(node: Node) -> str | None:
    status = stored_status(node)
    return phase(status).value if isinstance(status, Status) else None
