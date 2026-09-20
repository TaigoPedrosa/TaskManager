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

    def resolve_task_state(self, task_id: str) -> VirtualStatus | NodeStatus:
        node = self.node_repo.get_node(task_id)
        if node is None:
            raise ValueError(f"Task '{task_id}' not found")

        lease = self.runtime_repo.get_lease(task_id)
        if lease is not None and self._is_lease_active(lease):
            return VirtualStatus.IN_FLIGHT

        if node.status != NodeStatus.NOT_STARTED:
            return node.status

        deps = self.node_repo.get_dependency_edges(task_id)
        for dep_id, gate in deps:
            dep_node = self.node_repo.get_node(dep_id)
            if dep_node is None or not gate_satisfied(dep_node.status, gate):
                return VirtualStatus.BLOCKED

        return VirtualStatus.READY

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
        child_states = [self.resolve_task_state(cid) for cid in children]

        if all(s in (NodeStatus.COMPLETED, NodeStatus.SUPERSEDED) for s in child_states):
            return NodeStatus.COMPLETED

        uncompleted = [
            s for s in child_states if s not in (NodeStatus.COMPLETED, NodeStatus.SUPERSEDED)
        ]
        if uncompleted and all(s == VirtualStatus.BLOCKED for s in uncompleted):
            return VirtualStatus.BLOCKED

        if all(
            cn is not None
            and cn.status == NodeStatus.NOT_STARTED
            and not self._is_task_in_flight(cn.id)
            for cn in child_nodes
        ):
            return NodeStatus.NOT_STARTED

        return NodeStatus.IMPLEMENTING

    def resolve_spec_status(self, spec_id: str) -> NodeStatus | VirtualStatus:
        """A spec's status the same way a plan's is: rolled up from its children, here plans
        rather than tasks. Reuses `resolve_plan_status`'s already-coarse result per child."""
        spec_node = self.node_repo.get_node(spec_id)
        if spec_node is None:
            raise ValueError(f"Spec '{spec_id}' not found")

        children = self.node_repo.get_children(spec_id)
        plan_ids = [
            cid
            for cid in children
            if (child := self.node_repo.get_node(cid)) is not None and child.kind == NodeKind.PLAN
        ]
        if not plan_ids:
            return spec_node.status

        plan_states = [self.resolve_plan_status(pid) for pid in plan_ids]

        if all(s in (NodeStatus.COMPLETED, NodeStatus.SUPERSEDED) for s in plan_states):
            return NodeStatus.COMPLETED

        uncompleted = [
            s for s in plan_states if s not in (NodeStatus.COMPLETED, NodeStatus.SUPERSEDED)
        ]
        if uncompleted and all(s == VirtualStatus.BLOCKED for s in uncompleted):
            return VirtualStatus.BLOCKED

        if all(s == NodeStatus.NOT_STARTED for s in plan_states):
            return NodeStatus.NOT_STARTED

        return NodeStatus.IMPLEMENTING

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
