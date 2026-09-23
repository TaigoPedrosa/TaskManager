from datetime import UTC, datetime

from taskmanager.core.enums import NodeKind, NodeStatus, RelationType, VirtualStatus
from taskmanager.core.models import Lease, Node, NodeRelation
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository

# A task's forward progress through one lease cycle. SUPERSEDED, ABANDONED and DEFERRED are
# side-exits, not positions on this line, and are handled separately by `gate_satisfied`.
LIFECYCLE_ORDER: dict[NodeStatus, int] = {
    NodeStatus.NOT_STARTED: 0,
    NodeStatus.IMPLEMENTING: 1,
    NodeStatus.WAITING_REVIEW: 2,
    NodeStatus.REVIEWING: 3,
    NodeStatus.WAITING_FIXES: 4,
    NodeStatus.FIXING: 5,
    NodeStatus.WAITING_MERGE: 6,
    NodeStatus.MERGING: 7,
    NodeStatus.COMPLETED: 8,
}

# A child in one of these statuses cannot reach completion, so a rollup leaves it out of both
# the count and the denominator (§3.2a) -- it counts again the moment its status changes back.
_SET_ASIDE = {NodeStatus.SUPERSEDED, NodeStatus.ABANDONED, NodeStatus.DEFERRED}
_BLOCKED_STATES = {
    VirtualStatus.BLOCKED,
    VirtualStatus.BLOCKED_BY_LEASE,
    VirtualStatus.AWAITING_DECISION,
}


def gate_satisfied(status: NodeStatus, gate: NodeStatus) -> bool:
    """Whether a dependency's current status clears a `depends_on` edge's gate: at or past
    `gate` in the lifecycle order. SUPERSEDED clears any gate, same as it always cleared the
    implicit COMPLETED gate every bare edge carries; ABANDONED and DEFERRED clear none."""
    if status == NodeStatus.SUPERSEDED:
        return True
    status_order = LIFECYCLE_ORDER.get(status)
    if status_order is None:
        return False
    return status_order >= LIFECYCLE_ORDER.get(gate, LIFECYCLE_ORDER[NodeStatus.COMPLETED])


class GraphEngine:
    def __init__(self, node_repo: NodeRepository, runtime_repo: RuntimeRepository) -> None:
        self.node_repo = node_repo
        self.runtime_repo = runtime_repo

    def _is_lease_active(self, lease: Lease) -> bool:
        last_hb = lease.last_heartbeat
        if last_hb.tzinfo is None:
            last_hb = last_hb.astimezone(UTC)
        now = datetime.now(tz=UTC)
        return (now - last_hb).total_seconds() <= lease.ttl_seconds

    def _is_task_in_flight(self, task_id: str) -> bool:
        lease = self.runtime_repo.get_lease(task_id)
        return lease is not None and self._is_lease_active(lease)

    def _has_open_decision(self, deps: list[tuple[str, NodeStatus]]) -> bool:
        # A decision's own status is Open=NOT_STARTED, Answered=COMPLETED,
        # Withdrawn=ABANDONED -- met by either terminal state, never by the edge's gate.
        for dep_id, _gate in deps:
            dep_node = self.node_repo.get_node(dep_id)
            if (
                dep_node is not None
                and dep_node.kind == NodeKind.DECISION
                and dep_node.status not in (NodeStatus.COMPLETED, NodeStatus.ABANDONED)
            ):
                return True
        return False

    def resolve_task_state(self, task_id: str) -> VirtualStatus | NodeStatus:
        node = self.node_repo.get_node(task_id)
        if node is None:
            raise ValueError(f"Task '{task_id}' not found")

        lease = self.runtime_repo.get_lease(task_id)
        if lease is not None and self._is_lease_active(lease):
            return VirtualStatus.IN_FLIGHT

        deps = self.node_repo.get_dependency_edges(task_id)

        # An open decision reads as AWAITING_DECISION whatever the stored status -- a task
        # already claimed past NOT_STARTED (WAITING_REVIEW, WAITING_FIXES, WAITING_MERGE) still
        # has nothing to do while the owner hasn't ruled, and falls back to the stored status
        # the moment the decision is answered or withdrawn.
        if node.status != NodeStatus.NOT_STARTED:
            return VirtualStatus.AWAITING_DECISION if self._has_open_decision(deps) else node.status

        for dep_id, gate in deps:
            dep_node = self.node_repo.get_node(dep_id)
            if dep_node is not None and dep_node.kind == NodeKind.DECISION:
                continue
            if dep_node is None or not gate_satisfied(dep_node.status, gate):
                return VirtualStatus.BLOCKED
        if self._has_open_decision(deps):
            return VirtualStatus.AWAITING_DECISION

        declared_files = self.node_repo.declared_files(task_id)
        if declared_files and self.runtime_repo.get_conflicting_tasks(declared_files):
            return VirtualStatus.BLOCKED_BY_LEASE

        return VirtualStatus.READY

    def _rollup(
        self,
        counted_states: list[NodeStatus | VirtualStatus],
        set_aside_statuses: list[NodeStatus],
        all_not_started: bool,
    ) -> NodeStatus | VirtualStatus:
        """§3.2a, shared by the plan-over-tasks and spec-over-plans rollups: `counted_states` is
        each non-set-aside child's resolved state, `set_aside_statuses` is each set-aside
        child's own stored status, and `all_not_started` says whether every counted child is
        still at its stored NOT_STARTED with nothing in flight under it."""
        if not counted_states:
            if NodeStatus.DEFERRED in set_aside_statuses:
                return NodeStatus.DEFERRED
            if NodeStatus.ABANDONED in set_aside_statuses:
                return NodeStatus.ABANDONED
            return NodeStatus.COMPLETED  # every counted-out child is SUPERSEDED

        if all(s == NodeStatus.COMPLETED for s in counted_states):
            return NodeStatus.COMPLETED
        # A plan is only ever reported BLOCKED, never BLOCKED_BY_LEASE -- that distinction is a
        # per-task claimability signal, and collapsing it into one rollup value keeps a plan's
        # status meaning "nothing under it can proceed right now" either way.
        if all(s in _BLOCKED_STATES for s in counted_states):
            return VirtualStatus.BLOCKED
        if all_not_started:
            return VirtualStatus.READY
        return NodeStatus.IMPLEMENTING

    def resolve_plan_status(self, plan_id: str) -> NodeStatus | VirtualStatus:
        plan_node = self.node_repo.get_node(plan_id)
        if plan_node is None:
            raise ValueError(f"Plan '{plan_id}' not found")

        children = self.node_repo.get_children(plan_id)
        if not children:
            matching_tasks = [
                n.id
                for n in self.node_repo.list_nodes(kind=NodeKind.TASK)
                if n.id.startswith(f"{plan_id}-") and not n.id.endswith("-REV")
            ]
            if matching_tasks:
                children = matching_tasks

        if not children:
            return plan_node.status

        child_nodes = [self.node_repo.get_node(cid) for cid in children]
        counted = [cn for cn in child_nodes if cn is not None and cn.status not in _SET_ASIDE]
        set_aside_statuses = [
            cn.status for cn in child_nodes if cn is not None and cn.status in _SET_ASIDE
        ]

        return self._rollup(
            [self.resolve_task_state(cn.id) for cn in counted],
            set_aside_statuses,
            all(
                cn.status == NodeStatus.NOT_STARTED and not self._is_task_in_flight(cn.id)
                for cn in counted
            ),
        )

    def resolve_spec_status(self, spec_id: str) -> NodeStatus | VirtualStatus:
        """A spec's status the same way a plan's is: rolled up from its children, here plans
        rather than tasks. Reuses `resolve_plan_status`'s already-coarse result per child."""
        spec_node = self.node_repo.get_node(spec_id)
        if spec_node is None:
            raise ValueError(f"Spec '{spec_id}' not found")

        children = self.node_repo.get_children(spec_id)
        plan_nodes = [
            child
            for cid in children
            if (child := self.node_repo.get_node(cid)) is not None and child.kind == NodeKind.PLAN
        ]
        if not plan_nodes:
            return spec_node.status

        # A plan's own stored `status` sits at NOT_STARTED forever in normal use -- its rollup
        # is what "set aside" has to mean for a plan nested under a spec (a plan whose only
        # children are all abandoned rolls up to ABANDONED itself, with nothing on the plan
        # node's own row to show for it), unlike a task, whose stored status is transitioned
        # directly and already is its meaningful state.
        plan_states = [self.resolve_plan_status(pn.id) for pn in plan_nodes]
        counted_states = [state for state in plan_states if state not in _SET_ASIDE]
        set_aside_statuses = [
            state for state in plan_states if isinstance(state, NodeStatus) and state in _SET_ASIDE
        ]

        return self._rollup(
            counted_states,
            set_aside_statuses,
            all(s in (NodeStatus.NOT_STARTED, VirtualStatus.READY) for s in counted_states),
        )

    def would_cause_cycle(self, source_id: str, target_id: str) -> bool:
        if source_id == target_id:
            return True

        visited: set[str] = set()
        queue: list[str] = [target_id]

        while queue:
            curr = queue.pop(0)
            if curr == source_id:
                return True
            if curr in visited:
                continue
            visited.add(curr)

            for dep_id in self.node_repo.get_dependencies(curr):
                if dep_id == source_id:
                    return True
                if dep_id not in visited:
                    queue.append(dep_id)

        return False

    def inject_plan_review_gate(self, plan_id: str) -> str:
        gate_id = f"{plan_id}-REV"
        plan_node = self.node_repo.get_node(plan_id)
        target_repo = plan_node.target_repo if plan_node else None

        gate_node = Node(
            id=gate_id,
            kind=NodeKind.REVIEW_GATE,
            title=f"Review Gate: {plan_id}",
            status=NodeStatus.NOT_STARTED,
            target_repo=target_repo,
        )
        self.node_repo.save_node(gate_node)

        child_ids = self.node_repo.get_children(plan_id)
        task_ids: list[str] = []
        for cid in child_ids:
            if cid == gate_id:
                continue
            child = self.node_repo.get_node(cid)
            if child is None or child.kind == NodeKind.TASK:
                task_ids.append(cid)

        if not task_ids:
            matching_tasks = [
                n.id
                for n in self.node_repo.list_nodes(kind=NodeKind.TASK)
                if n.id.startswith(f"{plan_id}-") and n.id != gate_id
            ]
            task_ids.extend(matching_tasks)

        for tid in task_ids:
            self.node_repo.add_relation(
                NodeRelation(
                    source_id=gate_id,
                    target_id=tid,
                    relation_type=RelationType.DEPENDS_ON,
                )
            )

        return gate_id
