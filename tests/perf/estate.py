"""An estate of any size, seeded straight through `NodeRepository`: no import, no validation, so
a 1,000-node estate seeds in seconds. Every test that needs scale builds one here.
"""

import random
from pathlib import Path

from taskmanager.core.enums import NodeKind, RelationType
from taskmanager.core.models import Node, NodeRelation, NodeSection
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.node_repo import NodeRepository

_SECTION_KEYS = ("objective", "acceptance", "body", "context")


def seed(
    root: Path,
    plans: int,
    tasks_per_plan: int = 8,
    edges_per_task: int = 2,
    section_bytes: int = 2400,
    seed: int = 1,
) -> NodeRepository:
    """One spec, `plans` plans of `tasks_per_plan` tasks each, every task with dependency edges
    to earlier tasks and four sections."""
    rng = random.Random(seed)
    db = DatabaseManager(root / ".taskmanager")
    db.init_all()
    node_repo = NodeRepository(db)

    spec_id = "SPEC"
    node_repo.save_node(Node(id=spec_id, kind=NodeKind.SPEC, title="Perf spec"))

    body = "x" * section_bytes
    earlier: list[str] = []
    for p in range(plans):
        plan_id = f"P{p}"
        node_repo.save_node(Node(id=plan_id, kind=NodeKind.PLAN, title=f"Plan {p}"))
        node_repo.add_relation(
            NodeRelation(source_id=spec_id, target_id=plan_id, relation_type=RelationType.CONTAINS)
        )
        for t in range(tasks_per_plan):
            task_id = f"{plan_id}-T{t}"
            node_repo.save_node(
                Node(
                    id=task_id,
                    kind=NodeKind.TASK,
                    title=task_id,
                    target_repo=".",
                    frontmatter={"declared_files": [f"src/{task_id}.py"]},
                )
            )
            node_repo.add_relation(
                NodeRelation(
                    source_id=plan_id, target_id=task_id, relation_type=RelationType.CONTAINS
                )
            )
            for i, key in enumerate(_SECTION_KEYS):
                node_repo.save_section(
                    NodeSection(
                        node_id=task_id,
                        section_key=key,
                        ordinal=i,
                        header=f"## {key.title()}",
                        content=body,
                    )
                )
            for dep in rng.sample(earlier, k=min(edges_per_task, len(earlier))):
                node_repo.add_relation(
                    NodeRelation(
                        source_id=task_id, target_id=dep, relation_type=RelationType.DEPENDS_ON
                    )
                )
            earlier.append(task_id)
    return node_repo
