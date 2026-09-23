from dataclasses import dataclass

from taskmanager.core.enums import (
    NodeKind,
    NodeStatus,
    RecommendationStrategy,
    VirtualStatus,
)
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.engine.graph import GraphEngine


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


def _score_task(
    task_id: str,
    task_priority: int,
    parent_plan_id: str | None,
    plan_children: dict[str, set[str]],
    node_repo: NodeRepository,
    weights: tuple[float, float, float, float],
) -> tuple[float, int]:
    """A task's dispatch score and its unblocking count, independent of readiness.

    Shared by `get_next_tasks` (READY tasks only, top-N) and `score_every_task`
    (every task, for the visualizer's score filter) so the formula lives once.
    """
    w_prio, w_unlock, w_close, w_adv = weights
    parent_plan = node_repo.get_node(parent_plan_id) if parent_plan_id else None
    plan_priority = parent_plan.priority if parent_plan else 50

    s_prio = (task_priority * 0.7) + (plan_priority * 0.3)
    blocked_downstream = node_repo.get_blocked_by(task_id)
    s_unlock = min(len(blocked_downstream) * 25.0, 100.0)

    s_close = 0.0
    s_adv = 0.0
    if parent_plan_id:
        siblings = list(plan_children[parent_plan_id])
        if siblings:
            completed_count = sum(
                1
                for s in siblings
                if (node := node_repo.get_node(s))
                and node.status in (NodeStatus.COMPLETED, NodeStatus.SUPERSEDED)
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
    node_repo: NodeRepository,
    strategy: RecommendationStrategy | str = RecommendationStrategy.BALANCED,
) -> dict[str, float]:
    """Every task's dispatch score, regardless of readiness -- for browsing/filtering,
    not for picking a batch (that stays `get_next_tasks`, which also prunes by
    readiness, model and file collisions)."""
    weights = _STRATEGY_WEIGHTS[_resolve_strategy(strategy)]
    plans = node_repo.list_nodes(kind=NodeKind.PLAN)
    plan_children = {p.id: set(node_repo.get_children(p.id)) for p in plans}
    parent_of: dict[str, str] = {child_id: p.id for p in plans for child_id in plan_children[p.id]}

    scores: dict[str, float] = {}
    for task in node_repo.list_nodes(kind=NodeKind.TASK):
        score, _ = _score_task(
            task.id, task.priority, parent_of.get(task.id), plan_children, node_repo, weights
        )
        scores[task.id] = score
    return scores


class RecommendationEngine:
    def __init__(
        self,
        node_repo: NodeRepository,
        runtime_repo: RuntimeRepository,
        graph_engine: GraphEngine,
    ) -> None:
        self.node_repo = node_repo
        self.runtime_repo = runtime_repo
        self.graph = graph_engine
        self.graph_engine = graph_engine

    def get_next_tasks(
        self,
        plan_id: str | None = None,
        model_filter: str | None = None,
        strategy: RecommendationStrategy | str = RecommendationStrategy.BALANCED,
        limit: int = 5,
    ) -> list[ScoredTask]:
        weights = _STRATEGY_WEIGHTS[_resolve_strategy(strategy)]

        all_tasks = self.node_repo.list_nodes(kind=NodeKind.TASK)
        plans = self.node_repo.list_nodes(kind=NodeKind.PLAN)
        plan_children = {p.id: set(self.node_repo.get_children(p.id)) for p in plans}

        scored: list[ScoredTask] = []

        for task in all_tasks:
            if self.graph.resolve_task_state(task.id) != VirtualStatus.READY:
                continue

            if (
                model_filter
                and task.acceptable_models
                and model_filter not in task.acceptable_models
            ):
                continue

            # A file-lock conflict already reads as BLOCKED_BY_LEASE, not READY, so the
            # `!= VirtualStatus.READY` check above already excludes it; declared_files is
            # still needed below, for the batch's own same-file collision guard.
            declared_files = self.node_repo.declared_files(task.id)

            parent_plan_id: str | None = None
            for p in plans:
                if task.id in plan_children[p.id]:
                    parent_plan_id = p.id
                    break

            if plan_id is not None and parent_plan_id != plan_id:
                continue

            total_score, unblocking_count = _score_task(
                task.id, task.priority, parent_plan_id, plan_children, self.node_repo, weights
            )

            scored.append(
                ScoredTask(
                    task_id=task.id,
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
        # A batch is started together, so no two of its tasks may claim one file.
        chosen: list[ScoredTask] = []
        taken: set[str] = set()
        for candidate in scored:
            if taken.intersection(candidate.declared_files):
                continue
            chosen.append(candidate)
            taken.update(candidate.declared_files)
            if len(chosen) >= limit:
                break
        return chosen
