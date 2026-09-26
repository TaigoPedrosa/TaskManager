from dataclasses import dataclass

from taskmanager.core.enums import NodeKind, RecommendationStrategy
from taskmanager.core.models import Node
from taskmanager.core.status import DisplayStatus, Status
from taskmanager.db.cache_repo import CacheRepository
from taskmanager.db.node_repo import NodeRepository, declared_files_of, locked_key
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.engine.snapshot import DisplayView, SnapshotBuilder
from taskmanager.engine.stepgraph import Snapshot


@dataclass
class ScoredTask:
    task_id: str
    title: str
    plan_id: str | None
    score: float
    priority: int
    acceptable_models: list[str]
    unblocking_count: int
    declared_files: list[str]


_STRATEGY_WEIGHTS: dict[RecommendationStrategy, tuple[float, float, float, float]] = {
    RecommendationStrategy.UNBLOCK_FIRST: (0.20, 0.60, 0.10, 0.10),
    RecommendationStrategy.FINISH_PLANS: (0.10, 0.10, 0.50, 0.30),
    RecommendationStrategy.PRIORITY_STRICT: (1.0, 0.0, 0.0, 0.0),
    RecommendationStrategy.BALANCED: (0.35, 0.30, 0.20, 0.15),
}


def _resolve_strategy(strategy: RecommendationStrategy | str) -> RecommendationStrategy:
    return (
        RecommendationStrategy(strategy.lower().replace("_", "-"))
        if isinstance(strategy, str)
        else strategy
    )


def _plan_children(nodes: dict[str, Node], snapshot: Snapshot) -> dict[str, set[str]]:
    return {
        node_id: set(snapshot.children(node_id))
        for node_id, node in nodes.items()
        if node.kind == NodeKind.PLAN
    }


def _blocked_by(snapshot: Snapshot) -> dict[str, list[str]]:
    blocked: dict[str, list[str]] = {}
    for dependent, dependency in snapshot.edges:
        blocked.setdefault(dependency, []).append(dependent)
    return blocked


def _ancestor_of_kind(
    snapshot: Snapshot, nodes: dict[str, Node], node_id: str, kind: NodeKind
) -> str | None:
    visited: set[str] = set()
    current = node_id
    while True:
        parent = snapshot.parent(current)
        if parent is None or parent in visited:
            return None
        visited.add(parent)
        if (node := nodes.get(parent)) is not None and node.kind == kind:
            return parent
        current = parent


def _score_task(
    task_id: str,
    task_priority: int,
    parent_plan_id: str | None,
    plan_children: dict[str, set[str]],
    nodes: dict[str, Node],
    blocked_by: dict[str, list[str]],
    weights: tuple[float, float, float, float],
) -> tuple[float, int]:
    """A task's dispatch score and its unblocking count, independent of readiness.

    Shared by `get_next_tasks` (READY tasks only, top-N) and `score_every_task`
    (every task, for the visualizer's score filter) so the formula lives once.
    """
    w_prio, w_unlock, w_close, w_adv = weights
    plan_priority = nodes[parent_plan_id].priority if parent_plan_id else 50

    s_prio = (task_priority * 0.7) + (plan_priority * 0.3)
    blocked_downstream = blocked_by.get(task_id, [])
    s_unlock = min(len(blocked_downstream) * 25.0, 100.0)

    s_close = 0.0
    s_adv = 0.0
    if parent_plan_id:
        siblings = plan_children[parent_plan_id]
        if siblings:
            completed_count = sum(
                1
                for s in siblings
                if (node := nodes.get(s)) and node.status in (Status.COMPLETED, Status.SUPERSEDED)
            )
            total_siblings = len(siblings)
            s_close = (completed_count / total_siblings) * 100.0
            if completed_count == total_siblings - 1:
                s_adv = 100.0

    total_score = round(
        w_prio * s_prio + w_unlock * s_unlock + w_close * s_close + w_adv * s_adv, 2
    )
    return total_score, len(blocked_downstream)


def score_every_task(
    snapshot: Snapshot,
    strategy: RecommendationStrategy | str = RecommendationStrategy.BALANCED,
) -> dict[str, float]:
    """Every task's dispatch score, regardless of readiness -- for browsing/filtering,
    not for picking a batch (that stays `get_next_tasks`, which also prunes by
    readiness, model and file collisions)."""
    weights = _STRATEGY_WEIGHTS[_resolve_strategy(strategy)]
    nodes = snapshot.graph_data().nodes
    plan_children = _plan_children(nodes, snapshot)
    parent_of = {c: p for p, children in plan_children.items() for c in children}
    blocked_by = _blocked_by(snapshot)

    scores: dict[str, float] = {}
    for task_id, task in nodes.items():
        if task.kind != NodeKind.TASK:
            continue
        score, _ = _score_task(
            task_id,
            task.priority,
            parent_of.get(task_id),
            plan_children,
            nodes,
            blocked_by,
            weights,
        )
        scores[task_id] = score
    return scores


class RecommendationEngine:
    def __init__(
        self,
        node_repo: NodeRepository,
        runtime_repo: RuntimeRepository,
        snapshots: SnapshotBuilder,
        cache: CacheRepository | None = None,
        condition_ttl: int = 0,
    ) -> None:
        self.node_repo = node_repo
        self.runtime_repo = runtime_repo
        self.snapshots = snapshots
        self.cache = cache
        self.condition_ttl = condition_ttl

    def get_next_tasks(
        self,
        plan_id: str | None = None,
        spec_id: str | None = None,
        model_filter: str | None = None,
        strategy: RecommendationStrategy | str = RecommendationStrategy.BALANCED,
        limit: int = 5,
    ) -> list[ScoredTask]:
        weights = _STRATEGY_WEIGHTS[_resolve_strategy(strategy)]

        view = DisplayView(self.snapshots, self.cache, self.condition_ttl)
        snapshot = view.snapshot
        data = snapshot.graph_data()
        nodes = data.nodes
        plan_children = _plan_children(nodes, snapshot)
        parent_of = {c: p for p, children in plan_children.items() for c in children}
        blocked_by = _blocked_by(snapshot)
        # "none" reads as the sentinel for "no spec", so a task with no plan (parent_plan_id
        # None) matches it the same way a plan with no spec ancestor does: .get(None) is None.
        wanted_spec = None if spec_id in (None, "none") else spec_id
        plan_spec: dict[str | None, str | None] = (
            {p: _ancestor_of_kind(snapshot, nodes, p, NodeKind.SPEC) for p in plan_children}
            if spec_id is not None
            else {}
        )

        scored: list[ScoredTask] = []

        for task_id, task in nodes.items():
            if task.kind != NodeKind.TASK:
                continue

            if view.display(task) != DisplayStatus.READY.value:
                continue

            if (
                model_filter
                and task.acceptable_models
                and model_filter not in task.acceptable_models
            ):
                continue

            # A file-lock conflict already reads as BLOCKED_BY_LEASE, not READY, so the check
            # above already excludes it; declared_files is still needed below, for the batch's
            # own same-file collision guard.
            declared_files = declared_files_of(task, data.verifications.get(task_id, []))

            parent_plan_id = parent_of.get(task_id)

            if plan_id is not None and parent_plan_id != plan_id:
                continue

            if spec_id is not None and plan_spec.get(parent_plan_id) != wanted_spec:
                continue

            total_score, unblocking_count = _score_task(
                task_id, task.priority, parent_plan_id, plan_children, nodes, blocked_by, weights
            )

            scored.append(
                ScoredTask(
                    task_id=task_id,
                    title=task.title,
                    plan_id=parent_plan_id,
                    score=total_score,
                    priority=task.priority,
                    acceptable_models=task.acceptable_models,
                    unblocking_count=unblocking_count,
                    declared_files=declared_files,
                )
            )

        scored.sort(key=lambda x: x.score, reverse=True)
        # A batch is started together, so no two of its tasks may claim one file -- keyed to
        # each task's own target_repo, so the same repo-relative path in two repositories
        # never collides.
        chosen: list[ScoredTask] = []
        taken: set[str] = set()
        for candidate in scored:
            repo = nodes[candidate.task_id].target_repo
            keys = [locked_key(repo, f) for f in candidate.declared_files]
            if taken.intersection(keys):
                continue
            chosen.append(candidate)
            taken.update(keys)
            if len(chosen) >= limit:
                break
        return chosen
