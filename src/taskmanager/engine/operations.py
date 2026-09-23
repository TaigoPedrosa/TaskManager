import logging
import sqlite3
from datetime import UTC, datetime
from typing import Any

from taskmanager.core.enums import (
    LedgerCommand,
    NodeKind,
    NodeStatus,
    RelationType,
    TransferMode,
    VerificationType,
)
from taskmanager.core.models import (
    LedgerEvent,
    Node,
    NodeRelation,
    NodeSection,
    NodeVerification,
)
from taskmanager.db.ledger_repo import LedgerRepository
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.engine.graph import GraphEngine
from taskmanager.engine.runtime import ExecutionCoordinator
from taskmanager.engine.verification import VerificationEngine, VerificationResult

# The section a project bootstraps once and every `tm guide` overlay hangs off; `section set`
# points a user here when they try to write to it before it exists.
GUIDE_NODE = "guide"

# A task's forward progress through one lease cycle, reused for a lease-sweep rollback.
_SWEEP_BACK: dict[NodeStatus, NodeStatus] = {
    NodeStatus.IMPLEMENTING: NodeStatus.NOT_STARTED,
    NodeStatus.REVIEWING: NodeStatus.WAITING_REVIEW,
    NodeStatus.FIXING: NodeStatus.WAITING_FIXES,
    NodeStatus.MERGING: NodeStatus.WAITING_MERGE,
}


class OperationError(ValueError):
    """A refusal a user can act on; its message is shown verbatim by the CLI and the web."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


class Operations:
    def __init__(
        self,
        node_repo: NodeRepository,
        runtime_repo: RuntimeRepository,
        graph: GraphEngine,
        coordinator: ExecutionCoordinator,
        ledger_repo: LedgerRepository,
        verification_engine: VerificationEngine,
        actor: str = "cli",
    ) -> None:
        self.node_repo = node_repo
        self.runtime_repo = runtime_repo
        self.graph = graph
        self.coordinator = coordinator
        self.ledger_repo = ledger_repo
        self.verification_engine = verification_engine
        self.actor = actor

    def with_actor(self, actor: str) -> Operations:
        return Operations(
            self.node_repo,
            self.runtime_repo,
            self.graph,
            self.coordinator,
            self.ledger_repo,
            self.verification_engine,
            actor=actor,
        )

    def _ledger(
        self,
        command: LedgerCommand | str,
        target_id: str | None = None,
        payload: dict[str, Any] | None = None,
        diff: dict[str, Any] | None = None,
    ) -> None:
        try:
            self.ledger_repo.append(
                LedgerEvent(
                    actor_id=self.actor,
                    command=command,
                    target_id=target_id,
                    payload=payload or {},
                    diff=diff or {},
                )
            )
        except (sqlite3.Error, OSError) as exc:
            logging.getLogger(__name__).debug("Failed to append ledger event: %s", exc)

    # -- spec / plan / task creation -------------------------------------------------------

    def add_spec(
        self, title: str, slug: str | None = None, priority: int = 50, order: int = 0
    ) -> str:
        if slug:
            spec_id = slug
        else:
            existing = {n.id for n in self.node_repo.list_nodes(kind=NodeKind.SPEC)}
            counter = 1
            while f"S{counter}" in existing:
                counter += 1
            spec_id = f"S{counter}"

        node = Node(id=spec_id, kind=NodeKind.SPEC, title=title, priority=priority, ordinal=order)
        self.node_repo.save_node(node)
        self._ledger(
            LedgerCommand.SPEC_ADD,
            target_id=spec_id,
            payload={"title": title, "priority": priority, "ordinal": order},
        )
        return spec_id

    def add_plan(
        self,
        title: str,
        spec: str,
        slug: str | None = None,
        priority: int = 50,
        order: int = 0,
        require_review: bool = False,
    ) -> tuple[str, str | None]:
        if slug:
            plan_id = f"{spec}-{slug}"
        else:
            children = set(self.node_repo.get_children(spec))
            counter = 1
            while f"{spec}-P{counter}" in children:
                counter += 1
            plan_id = f"{spec}-P{counter}"

        plan_node = Node(
            id=plan_id, kind=NodeKind.PLAN, title=title, priority=priority, ordinal=order
        )
        self.node_repo.save_node(plan_node)
        self.node_repo.add_relation(
            NodeRelation(source_id=spec, target_id=plan_id, relation_type=RelationType.CONTAINS)
        )

        if require_review:
            gate_id = self.graph.inject_plan_review_gate(plan_id)
            self._ledger(
                LedgerCommand.PLAN_REVIEW_GATE,
                target_id=plan_id,
                payload={"title": title, "spec": spec, "review_gate": gate_id, "ordinal": order},
            )
            return plan_id, gate_id

        self._ledger(
            LedgerCommand.PLAN_ADD,
            target_id=plan_id,
            payload={"title": title, "spec": spec, "ordinal": order},
        )
        return plan_id, None

    def add_task(
        self,
        title: str,
        plan: str,
        slug: str | None = None,
        priority: int = 50,
        order: int = 0,
        depends_on: list[str] | None = None,
        models: list[str] | None = None,
    ) -> str:
        if slug:
            task_id = f"{plan}-{slug}"
        else:
            children = set(self.node_repo.get_children(plan))
            counter = 1
            while f"{plan}-T{counter}" in children:
                counter += 1
            task_id = f"{plan}-T{counter}"

        task_node = Node(
            id=task_id,
            kind=NodeKind.TASK,
            title=title,
            priority=priority,
            ordinal=order,
            acceptable_models=models or [],
        )
        self.node_repo.save_node(task_node)
        self.node_repo.add_relation(
            NodeRelation(source_id=plan, target_id=task_id, relation_type=RelationType.CONTAINS)
        )

        for dep in depends_on or []:
            self.node_repo.add_relation(
                NodeRelation(
                    source_id=task_id, target_id=dep, relation_type=RelationType.DEPENDS_ON
                )
            )

        self._ledger(
            LedgerCommand.TASK_ADD, target_id=task_id, payload={"title": title, "plan": plan}
        )
        return task_id

    # -- node update / dependencies / supersede / move --------------------------------------

    def update_node(
        self,
        node_id: str,
        title: str | None = None,
        priority: int | None = None,
        models: list[str] | None = None,
        repo: str | None = None,
        frontmatter_set: dict[str, Any] | None = None,
        frontmatter_unset: list[str] | None = None,
    ) -> dict[str, Any]:
        node = self.node_repo.get_node(node_id)
        if node is None:
            raise OperationError(f"task '{node_id}' not found", 404)
        changed: dict[str, Any] = {}
        if title is not None:
            node.title = title
            changed["title"] = title
        if priority is not None:
            if not 1 <= priority <= 100:
                raise OperationError("priority is 1-100", 400)
            node.priority = priority
            changed["priority"] = priority
        if models is not None:
            node.acceptable_models = models
            changed["acceptable_models"] = node.acceptable_models
        if repo is not None:
            node.target_repo = repo
            changed["target_repo"] = repo
        for key, value in (frontmatter_set or {}).items():
            node.frontmatter[key] = value
            changed[f"frontmatter.{key}"] = value
        for key in frontmatter_unset or []:
            node.frontmatter.pop(key, None)
            changed[f"frontmatter.{key}"] = None
        if not changed:
            raise OperationError("nothing to update", 400)
        node.updated_at = datetime.now(tz=UTC)
        self.node_repo.save_node(node)
        self._ledger(LedgerCommand.TASK_UPDATE, target_id=node_id, payload=changed)
        return changed

    def set_dependencies(
        self,
        task_id: str,
        add: list[tuple[str, NodeStatus | None]],
        remove: list[str],
    ) -> list[tuple[str, NodeStatus]]:
        if self.node_repo.get_node(task_id) is None:
            raise OperationError(f"Task '{task_id}' not found", 404)
        current = set(self.node_repo.get_dependencies(task_id))
        problems: list[str] = []
        for dep, _gate in add:
            if self.node_repo.get_node(dep) is None:
                problems.append(f"'{dep}' does not exist")
            elif dep not in current and self.graph.would_cause_cycle(task_id, dep):
                problems.append(f"'{dep}' would make a cycle")
        for dep in remove:
            if dep not in current:
                problems.append(f"'{dep}' is not a dependency")
        if problems:
            raise OperationError(f"Nothing changed: {'; '.join(problems)}", 409)
        for dep, gate in add:
            self.node_repo.add_relation(
                NodeRelation(
                    source_id=task_id,
                    target_id=dep,
                    relation_type=RelationType.DEPENDS_ON,
                    metadata={"gate": gate.value} if gate is not None else {},
                )
            )
        for dep in remove:
            self.node_repo.remove_relation(task_id, dep, RelationType.DEPENDS_ON)
        self._ledger(
            LedgerCommand.TASK_DEPENDS,
            target_id=task_id,
            payload={"add": [d for d, _ in add], "remove": remove},
        )
        return self.node_repo.get_dependency_edges(task_id)

    def supersede(
        self, old_id: str, new_id: str, transfer_blocks: str = TransferMode.ALL.value
    ) -> None:
        old_node = self.node_repo.get_node(old_id)
        if not old_node:
            raise OperationError(f"Task '{old_id}' not found", 404)
        if new_id == old_id or self.node_repo.get_node(new_id) is None:
            raise OperationError(f"Replacement task '{new_id}' not found; nothing was changed", 400)

        # A replaced task is not being worked on: its lease and file locks go with it.
        self.runtime_repo.release_lease(old_id)
        old_node.status = NodeStatus.SUPERSEDED
        self.node_repo.save_node(old_node)

        self.node_repo.add_relation(
            NodeRelation(source_id=new_id, target_id=old_id, relation_type=RelationType.SUPERSEDES)
        )

        tb_val = transfer_blocks.strip().lower()
        if tb_val == TransferMode.ALL.value:
            self.node_repo.transfer_blocks(old_id, new_id, TransferMode.ALL)
        elif tb_val == TransferMode.NONE.value:
            self.node_repo.transfer_blocks(old_id, new_id, TransferMode.NONE)
        else:
            custom_ids = [x.strip() for x in transfer_blocks.split(",") if x.strip()]
            self.node_repo.transfer_blocks(
                old_id, new_id, TransferMode.CUSTOM, custom_ids=custom_ids
            )

        self._ledger(
            LedgerCommand.TASK_SUPERSEDE,
            target_id=old_id,
            payload={"superseded_by": new_id, "transfer_blocks": transfer_blocks},
        )

    def move_task(self, task_id: str, plan_id: str) -> None:
        if self.node_repo.get_node(task_id) is None:
            raise OperationError(f"Task '{task_id}' not found", 404)
        if self.node_repo.get_node(plan_id) is None:
            raise OperationError(f"Plan '{plan_id}' not found", 404)
        with self.node_repo.db.get_spec_connection() as conn:
            row = conn.execute(
                "SELECT source_id FROM node_relations WHERE target_id = ? AND relation_type = ?",
                (task_id, RelationType.CONTAINS.value),
            ).fetchone()
        old_plan = row[0] if row else None
        if old_plan is not None:
            self.node_repo.remove_relation(old_plan, task_id, RelationType.CONTAINS)
        self.node_repo.add_relation(
            NodeRelation(source_id=plan_id, target_id=task_id, relation_type=RelationType.CONTAINS)
        )
        self._ledger(
            LedgerCommand.TASK_MOVE, target_id=task_id, payload={"from": old_plan, "to": plan_id}
        )

    # -- status / leases ----------------------------------------------------------------------

    def set_status(
        self,
        task_id: str,
        status: NodeStatus = NodeStatus.WAITING_REVIEW,
        remove_worktree: bool = False,
    ) -> None:
        self.coordinator.stop_task(
            task_id=task_id, new_status=status, remove_worktree=remove_worktree
        )
        self._ledger(
            LedgerCommand.TASK_STOP,
            target_id=task_id,
            payload={"status": status.value, "remove_worktree": remove_worktree},
        )

    def release_lease(self, task_id: str) -> None:
        self.runtime_repo.release_lease(task_id)
        self._ledger(LedgerCommand.LEASE_RELEASE, target_id=task_id)

    def sweep_leases(self) -> list[str]:
        swept = self.runtime_repo.sweep_expired_leases()
        if swept:
            # An abandoned claim returns the task to the state before it, or nobody could claim it.
            for task_id in swept:
                node = self.node_repo.get_node(task_id)
                if node is not None and node.status in _SWEEP_BACK:
                    node.status = _SWEEP_BACK[node.status]
                    self.node_repo.save_node(node)
            self._ledger(LedgerCommand.LEASE_SWEEP, payload={"swept_tasks": swept})
        return swept

    # -- sections -------------------------------------------------------------------------

    def set_section(
        self, node_id: str, section_key: str, content: str, header: str | None = None
    ) -> None:
        if self.node_repo.get_node(node_id) is None:
            hint = (
                " Create it once with `tm spec add 'Project guide' --slug guide`."
                if node_id == GUIDE_NODE
                else ""
            )
            raise OperationError(f"No node '{node_id}' to hold the section.{hint}", 404)

        sec_header = header or f"## {section_key.capitalize()}"
        existing_secs = self.node_repo.get_all_sections(node_id)
        existing = next((s for s in existing_secs if s.section_key == section_key), None)
        ordinal = existing.ordinal if existing else len(existing_secs) + 1

        self.node_repo.save_section(
            NodeSection(
                node_id=node_id,
                section_key=section_key,
                ordinal=ordinal,
                header=sec_header,
                content=content,
            )
        )
        self._ledger(LedgerCommand.SECTION_SET, target_id=f"{node_id}:{section_key}")

    def remove_section(self, node_id: str, section_key: str) -> None:
        if not self.node_repo.remove_section(node_id, section_key):
            raise OperationError(f"No section '{section_key}' on node '{node_id}'", 404)
        self._ledger(LedgerCommand.SECTION_REMOVE, target_id=f"{node_id}:{section_key}")

    # -- verifications ----------------------------------------------------------------------

    def add_verification(
        self,
        task_id: str,
        verification_type: VerificationType,
        target: str,
        pattern: str | None = None,
    ) -> NodeVerification:
        ver = NodeVerification(
            node_id=task_id,
            verification_type=verification_type,
            target_path=target,
            expected_pattern=pattern,
        )
        self.node_repo.add_verification(ver)
        self._ledger(
            LedgerCommand.VERIFICATION_ADD,
            target_id=task_id,
            payload={"type": verification_type.value, "target": target},
        )
        return ver

    def remove_verification(self, task_id: str, verification_id: int) -> None:
        if not self.node_repo.remove_verification(task_id, verification_id):
            raise OperationError(f"Task '{task_id}' has no verification {verification_id}", 404)
        self._ledger(
            "verify remove", target_id=task_id, payload={"verification_id": verification_id}
        )

    def run_verifications(self, task_id: str | None) -> tuple[bool, list[VerificationResult]]:
        if task_id:
            vers = self.node_repo.get_verifications(task_id)
        else:
            vers = []
            for t in self.node_repo.list_nodes(kind=NodeKind.TASK):
                vers.extend(self.node_repo.get_verifications(t.id))

        if not vers:
            raise OperationError(
                "No verifications to run: an empty check set proves nothing. Add one with "
                "`tm verify add`, or attest the task.",
                400,
            )

        results = self.verification_engine.verify_all(vers)
        all_passed = all(r.passed for r in results)
        self._ledger(
            LedgerCommand.VERIFICATION_RUN,
            target_id=task_id,
            payload={"passed": all_passed, "count": len(results)},
        )
        return all_passed, results
