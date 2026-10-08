import re
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Literal

from taskmanager.core.display import Facts, display_status, phase
from taskmanager.core.enums import CONTAINERS, NodeKind, RelationType, VerificationType
from taskmanager.core.lifecycle import Cycle, next_action
from taskmanager.core.models import LedgerEvent, Node
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
from taskmanager.db.cache_repo import CacheRepository, _command_hash
from taskmanager.db.graph_reader import GraphData, read_graph
from taskmanager.db.job_repo import JobRepository
from taskmanager.db.node_repo import (
    NodeRepository,
    declared_files_of,
    is_locked_path,
    locked_key,
)
from taskmanager.db.runtime_repo import RuntimeRepository, lease_alive
from taskmanager.engine.chains import satisfied
from taskmanager.engine.config import ConfigStore, ProjectConfig
from taskmanager.engine.stepgraph import SnapNode, Snapshot, migration_holders

if TYPE_CHECKING:
    from taskmanager.engine.operations import Operations

# A job stopped for an agent still owns its node's worktree and lease, as a running one does.
_LIVE_JOB = frozenset({JobState.RUNNING, JobState.NEEDS_AGENT})
_LOCKING = frozenset({Action.IMPLEMENT, Action.FIX})
_NOT_STARTED = frozenset({Status.READY}) | EXITS
# `tm verify run --ref` exports TM_VERIFY_REF, so an `origin/main` fallback inside this
# expansion follows the ref being verified; only a bare `origin/main` pins a check to main.
_VERIFY_REF_EXPANSION = re.compile(r"\$\{TM_VERIFY_REF:-[^}]*\}")
ORIGIN_MAIN: Final = "origin/main"


def writes_migration(files: list[str]) -> bool:
    return any("migrations/versions/" in f for f in files)


def names_origin_main(command: str) -> bool:
    return ORIGIN_MAIN in _VERIFY_REF_EXPANSION.sub("", command)


def sensitive_areas(node: Node) -> tuple[str, ...]:
    """What `node`'s `sensitive:` frontmatter key names: one area, or a list of them."""
    value = node.frontmatter.get("sensitive")
    if value is None:
        return ()
    return tuple(str(area) for area in (value if isinstance(value, list) else [value]))


def stored_status(node: Node) -> Status | DecisionStatus:
    return node.status


def is_sensitive(node: Node, files: list[str]) -> bool:
    """`node` alone, `files` being its declared files: a node writing a migration is sensitive
    without the key."""
    return bool(sensitive_areas(node)) or writes_migration(files)


def cycle_of(node: Node, sensitive: bool) -> Cycle:
    """`node`'s cycle. `sensitive` holds when `node` or any node under it is: a container's
    landing brings every descendant's code, a superseded one's included, so its fix may touch
    any of it."""
    if not isinstance(node.status, Status):
        raise ValueError(f"decision {node.id!r} has no cycle")  # noqa: TRY004
    return Cycle(
        status=node.status,
        container=node.kind in CONTAINERS,
        review=node.review,
        fix=node.fix,
        sensitive=sensitive,
        outcome=node.outcome,
        fix_for=node.fix_for,
        claimed_from=node.claimed_from,
        review_cycles=node.review_cycles,
        merge_attempts=node.merge_attempts,
        step_failures=node.step_failures,
    )


def cycle_in(snapshot: Snapshot, node: Node) -> Cycle:
    """`node`'s cycle, its declared files and its subtree read from `snapshot`'s one bulk read."""
    files = declared_files_of(node, snapshot.graph_data().verifications.get(node.id, []))
    under = (snapshot.nodes[d] for d in snapshot.descendants(node.id))
    return cycle_of(
        node, is_sensitive(node, files) or any(n.sensitive or n.writes_migration for n in under)
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


def roll_up_ancestors(
    ops: Operations, node_id: str, *, include_self: bool = False
) -> list[tuple[str, Status]]:
    """Re-derive each ancestor container's status after a change under it (and `node_id`'s own
    with `include_self`), in the caller's transaction, and return every container that moved
    with its new status. The only rollup: a container reaching IMPLEMENTED with nothing left to
    land is landed already, one reaching DEFERRED or ABANDONED strands its dependents, and each
    move is ledgered."""
    # decisions imports this module, so importing it back at load time would be circular.
    from taskmanager.engine.decisions import open_stranded_decision, stranded_dependents

    node_repo = ops.node_repo
    moved: list[tuple[str, Status]] = []
    seen: set[str] = set()
    parents = [node_id] if include_self else node_repo.get_parent_ids(node_id)
    while parents and parents[0] not in seen:
        parent = node_repo.get_node(parents[0])
        if parent is None or parent.kind not in CONTAINERS:
            break
        seen.add(parent.id)
        children = [node_repo.get_node(c) for c in node_repo.get_children(parent.id)]
        statuses = [
            s for c in children if c is not None and isinstance(s := stored_status(c), Status)
        ]
        current = ops.snapshots.cycle(parent)
        derived = rollup(current.status, statuses)
        if derived == Status.IMPLEMENTED and ops.nothing_to_land(parent.id):
            # Its code is on its target already, as a landing would have left it: a reviewed
            # container still owes its one review there.
            derived = Status.LANDED if current.review else Status.COMPLETED
        if derived != current.status:
            node_repo.save_node(apply_cycle(parent, replace(current, status=derived)))
            moved.append((parent.id, derived))
            ops.ledger_repo.append(
                LedgerEvent(
                    actor_id=ops.actor,
                    command="rollup",
                    target_id=parent.id,
                    payload={"from": current.status, "to": derived},
                )
            )
            if derived in (Status.DEFERRED, Status.ABANDONED) and (
                dependents := stranded_dependents(ops, parent.id)
            ):
                open_stranded_decision(ops, parent.id, derived, dependents)
        parents = node_repo.get_parent_ids(parent.id)
    return moved


def _top(snapshot: Snapshot, node_id: str, branches: ProjectConfig, repo: str | None = None) -> str:
    """`Operations.landing_branch`, read from the snapshot."""
    root, seen = node_id, {node_id}
    while (parent := snapshot.nodes[root].parent) in snapshot.nodes and parent not in seen:
        root = parent
        seen.add(root)
    land_on = snapshot.nodes[root].land_on
    if land_on:
        return land_on
    if repo is None:
        repo = snapshot.nodes[node_id].repo
    if repo is None:
        repos = (snapshot.nodes[d].repo for d in snapshot.counted_descendants(node_id))
        repo = min(filter(None, repos), default=None)
    return branches.default_branch(repo)


def with_tops(snapshot: Snapshot, branches: ProjectConfig, repo: str | None = None) -> Snapshot:
    """`snapshot` with each node's `top` as `Operations.landing_branch(node, repo)` reads it
    under `branches`."""
    tops = {
        node_id: replace(n, top=_top(snapshot, node_id, branches, repo))
        for node_id, n in snapshot.nodes.items()
    }
    return Snapshot(nodes=tops, edges=snapshot.edges, data=snapshot.data)


class SnapshotBuilder:
    """The whole graph as the pure rules read it, and the per-node facts display derives from.

    `build()` reads `state.db` once, through `graph_reader.read_graph`; every other method here
    but `cycle` reads only that one read's result, carried on the `Snapshot` it returned. `cycle`
    serves a single live node with no snapshot built, so it reads that node's subtree itself.
    """

    def __init__(
        self, node_repo: NodeRepository, runtime_repo: RuntimeRepository, job_repo: JobRepository
    ) -> None:
        self.node_repo = node_repo
        self.runtime_repo = runtime_repo
        self.job_repo = job_repo

    def build(self) -> Snapshot:
        data = read_graph(self.node_repo.db)
        parents: dict[str, str] = {}
        for source, target in data.relations[RelationType.CONTAINS]:
            parents.setdefault(target, source)
        nodes = {
            node_id: self._snap(node, parents.get(node_id), data)
            for node_id, node in data.nodes.items()
        }
        edges = data.relations[RelationType.DEPENDS_ON]
        # A container's repository is only known once its descendants are in the tree.
        draft = Snapshot(nodes=nodes, edges=edges, data=data)
        return with_tops(draft, ConfigStore(self.node_repo.db.taskmanager_dir.parent).branches())

    def cycle(self, node: Node) -> Cycle:
        return cycle_of(node, self._sensitive(node))

    def _sensitive(self, node: Node) -> bool:
        """`cycle_in`'s rule, read node by node down `node`'s own subtree, never the whole graph.
        `seen` stops a corrupt CONTAINS cycle, which would otherwise hang the claim reading it."""
        pending, seen = [node], {node.id}
        while pending:
            current = pending.pop()
            files = declared_files_of(current, self.node_repo.get_verifications(current.id))
            if is_sensitive(current, files):
                return True
            for child_id in self.node_repo.get_children(current.id):
                if child_id not in seen and (child := self.node_repo.get_node(child_id)):
                    seen.add(child_id)
                    pending.append(child)
        return False

    @staticmethod
    def lock_set(node_id: str, snapshot: Snapshot) -> list[str]:
        """The files a claim of this node locks: its declared files, or for a container that
        declares none, the union of its descendants' -- each keyed to its own node's
        target_repo, so the same repo-relative path in two repositories never collides.

        A `staticmethod`, over the snapshot alone: `engine.selection` calls this directly, so the
        set a claim locks and the set discovery's `blocked_reason` checks are the one
        implementation, never two copies to keep in step."""
        data = snapshot.graph_data()
        node = snapshot.nodes[node_id]
        own = SnapshotBuilder._declared_files(node_id, data)
        if own or node.kind not in CONTAINERS:
            return [locked_key(node.repo, f) for f in own if is_locked_path(f)]
        keys = [
            locked_key(snapshot.nodes[d].repo, f)
            for d in snapshot.counted_descendants(node_id)
            for f in SnapshotBuilder._declared_files(d, data)
            if is_locked_path(f)
        ]
        return list(dict.fromkeys(keys))

    @staticmethod
    def conflicts(files: list[str], snapshot: Snapshot) -> dict[str, str]:
        """Every one of `files` a live lease other than its own currently holds, exactly as
        `RuntimeRepository.get_conflicting_tasks` reads it -- from the snapshot's one bulk read,
        not a query per call."""
        data = snapshot.graph_data()
        wanted = set(files)
        found: dict[str, str] = {}
        for lock in data.file_locks:
            if lock.file_path not in wanted:
                continue
            lease = data.leases.get(lock.task_id)
            if lease is not None and lease_alive(
                lease.ttl_seconds, lease.last_heartbeat, data.built_at
            ):
                found[lock.file_path] = f"Task: {lock.task_id}, Agent: {lease.agent_id}"
        return found

    def facts(self, node_id: str, snapshot: Snapshot) -> Facts:
        """Everything display derivation needs beyond the node's own cycle. `unmet_condition`
        is left False: conditions run through the ConditionRunner, whose result a caller folds
        in with `dataclasses.replace`."""
        data = snapshot.graph_data()
        node = data.nodes.get(node_id)
        if node is None:
            raise KeyError(node_id)
        jobs = data.jobs.get(node_id, [])
        work, decisions = waits_on(snapshot, node)
        locking = next_action(cycle_in(snapshot, node)) in _LOCKING
        return Facts(
            lease=self._lease(node_id, data),
            job_needs_agent=any(
                j.kind == JobKind.LAND and j.state == JobState.NEEDS_AGENT for j in jobs
            ),
            open_decision=bool(decisions),
            unsatisfied_edge=bool(work),
            sync_pending=any(j.kind == JobKind.SYNC for j in jobs),
            files_locked=locking
            and bool(self.conflicts(self.lock_set(node_id, snapshot), snapshot)),
            descendant_started=any(
                snapshot.status(d) not in _NOT_STARTED for d in snapshot.descendants(node_id)
            ),
        )

    @staticmethod
    def _declared_files(node_id: str, data: GraphData) -> list[str]:
        return declared_files_of(data.nodes.get(node_id), data.verifications.get(node_id, []))

    @staticmethod
    def _lease(node_id: str, data: GraphData) -> Literal["live", "expired", "none"]:
        lease = data.leases.get(node_id)
        if lease is None:
            return "none"
        return (
            "live"
            if lease_alive(lease.ttl_seconds, lease.last_heartbeat, data.built_at)
            else "expired"
        )

    @staticmethod
    def _busy(node_id: str, data: GraphData) -> bool:
        lease = data.leases.get(node_id)
        if lease is not None and lease_alive(
            lease.ttl_seconds, lease.last_heartbeat, data.built_at
        ):
            return True
        return any(j.state in _LIVE_JOB for j in data.jobs.get(node_id, ()))

    def _snap(self, node: Node, parent: str | None, data: GraphData) -> SnapNode:
        commands = [
            v.expected_pattern or v.target_path
            for v in data.verifications.get(node.id, [])
            if v.verification_type == VerificationType.TEST_COMMAND
        ]
        return SnapNode(
            id=node.id,
            kind=node.kind,
            parent=parent,
            merge=node.merge,
            status=stored_status(node),
            claimed_from=node.claimed_from,
            review=node.review,
            fix=node.fix,
            repo=node.target_repo,
            writes_migration=writes_migration(self._declared_files(node.id, data)),
            sensitive=sensitive_areas(node),
            busy=self._busy(node.id, data),
            literal_origin_main=any(names_origin_main(c) for c in commands),
            land_on=None if (land_on := node.frontmatter.get("land_on")) is None else str(land_on),
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
        self._conditions = cache.all_conditions(max_age) if cache is not None else {}

    def _unmet(self, node: Node) -> bool:
        if self.cache is None:
            return False
        data = self.snapshot.graph_data()
        stages = {ConditionStage.CLAIM}
        if next_action(cycle_in(self.snapshot, node)) == Action.MERGE:
            stages.add(ConditionStage.LANDING)
        for condition in data.conditions.get(node.id, []):
            if condition.stage not in stages:
                continue
            cached = self._conditions.get((node.id, condition.idx))
            if cached is None:
                continue
            command_hash, code = cached
            if command_hash == _command_hash(condition.command) and code != 0:
                return True
        return False

    def display(self, node: Node) -> str:
        status = stored_status(node)
        if not isinstance(status, Status):
            return status.value
        facts = replace(
            self.builder.facts(node.id, self.snapshot), unmet_condition=self._unmet(node)
        )
        return display_status(cycle_in(self.snapshot, node), facts).value


def chain_holder(snapshot: Snapshot, node: Node) -> str | None:
    """The migration writer an implement of `node` waits behind in its repository's chain.
    Discovery holds it there as it would behind an unlanded dependency, so it reads the same."""
    if (
        node.kind == NodeKind.DECISION
        or not snapshot.nodes[node.id].writes_migration
        or next_action(cycle_in(snapshot, node)) != Action.IMPLEMENT
    ):
        return None
    return migration_holders(snapshot, node.target_repo or "").get(node.id)


def waits_on(snapshot: Snapshot, node: Node) -> tuple[list[str], list[str]]:
    """The work `node` waits to see landed and the open decisions it waits on, its containers'
    edges and its migration chain included: every reason its display blocks on, by name."""
    if node.id not in snapshot.nodes or node.kind == NodeKind.DECISION:
        return [], []
    edges = [d for d in snapshot.inherited_edges(node.id) if d in snapshot.nodes]
    decisions = [d for d in edges if snapshot.nodes[d].kind == NodeKind.DECISION]
    work = [d for d in edges if d not in decisions and not satisfied(snapshot, node.id, d)]
    holder = chain_holder(snapshot, node)
    if holder is not None and holder not in work:
        work.append(holder)
    return work, [d for d in decisions if snapshot.status(d) == DecisionStatus.OPEN]


def phase_of(node: Node) -> str | None:
    status = stored_status(node)
    return phase(status).value if isinstance(status, Status) else None
