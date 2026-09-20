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
        strat = (
            RecommendationStrategy(strategy.lower().replace("_", "-"))
            if isinstance(strategy, str)
            else strategy
        )
        if strat == RecommendationStrategy.UNBLOCK_FIRST:
            w_prio, w_unlock, w_close, w_adv = 0.20, 0.60, 0.10, 0.10
        elif strat == RecommendationStrategy.FINISH_PLANS:
            w_prio, w_unlock, w_close, w_adv = 0.10, 0.10, 0.50, 0.30
        elif strat == RecommendationStrategy.PRIORITY_STRICT:
            w_prio, w_unlock, w_close, w_adv = 1.0, 0.0, 0.0, 0.0
        else:
            w_prio, w_unlock, w_close, w_adv = 0.35, 0.30, 0.20, 0.15

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

            declared_files = self.node_repo.declared_files(task.id)

            if declared_files and self.runtime_repo.get_conflicting_tasks(declared_files):
                continue

            parent_plan_id: str | None = None
            for p in plans:
                if task.id in plan_children[p.id]:
                    parent_plan_id = p.id
                    break

            if plan_id is not None and parent_plan_id != plan_id:
                continue

            parent_plan = self.node_repo.get_node(parent_plan_id) if parent_plan_id else None
            plan_priority = parent_plan.priority if parent_plan else 50

            s_prio = (task.priority * 0.7) + (plan_priority * 0.3)
            blocked_downstream = self.node_repo.get_blocked_by(task.id)
            s_unlock = min(len(blocked_downstream) * 25.0, 100.0)

            s_close = 0.0
            s_adv = 0.0
            if parent_plan_id:
                siblings = list(plan_children[parent_plan_id])
                if siblings:
                    completed_count = sum(
                        1
                        for s in siblings
                        if (node := self.node_repo.get_node(s))
                        and node.status in (NodeStatus.COMPLETED, NodeStatus.SUPERSEDED)
                    )
                    total_siblings = len(siblings)
                    s_close = (completed_count / total_siblings) * 100.0
                    if completed_count == total_siblings - 1:
                        s_adv = 100.0

            total_score = round(
                w_prio * s_prio + w_unlock * s_unlock + w_close * s_close + w_adv * s_adv,
                2,
            )

            scored.append(
                ScoredTask(
                    task_id=task.id,
                    title=task.title,
                    plan_id=parent_plan_id,
                    score=total_score,
                    priority=task.priority,
                    acceptable_models=task.acceptable_models,
                    unblocking_count=len(blocked_downstream),
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
