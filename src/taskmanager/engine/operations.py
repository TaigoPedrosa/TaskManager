import hashlib
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
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
from taskmanager.engine.assets import (
    AssetError,
    AttachmentSource,
    is_project_relative,
    store_asset,
)
from taskmanager.engine.decisions import (
    DecisionAnswer,
    DecisionData,
    DecisionOption,
    read_decision,
    write_decision,
)
from taskmanager.engine.graph import GraphEngine
from taskmanager.engine.runtime import ExecutionCoordinator
from taskmanager.engine.verification import VerificationEngine, VerificationResult

# The section a project bootstraps once and every `tm guide` overlay hangs off; `section set`
# points a user here when they try to write to it before it exists.
GUIDE_NODE = "guide"

# A task's forward progress through one lease cycle, reused for a lease-sweep rollback.
_SWEEP_BACK: dict[str, NodeStatus] = {
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

    @staticmethod
    def _validate_priority(priority: int) -> None:
        if not 1 <= priority <= 100:
            raise OperationError("priority is 1-100", 400)

    def add_spec(
        self, title: str, slug: str | None = None, priority: int = 50, order: int = 0
    ) -> str:
        self._validate_priority(priority)
        if slug:
            spec_id = slug
            if self.node_repo.get_node(spec_id) is not None:
                raise OperationError(f"'{spec_id}' already exists", 409)
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
        self._validate_priority(priority)
        if self.node_repo.get_node(spec) is None:
            raise OperationError(f"spec '{spec}' not found", 404)
        if slug:
            plan_id = f"{spec}-{slug}"
            if self.node_repo.get_node(plan_id) is not None:
                raise OperationError(f"'{plan_id}' already exists", 409)
        else:
            children = set(self.node_repo.get_children(spec))
            counter = 1
            while f"{spec}-P{counter}" in children:
                counter += 1
            plan_id = f"{spec}-P{counter}"

        plan_node = Node(
            id=plan_id, kind=NodeKind.PLAN, title=title, priority=priority, ordinal=order
        )
        with self.node_repo.transaction():
            self.node_repo.save_node(plan_node)
            self.node_repo.add_relation(
                NodeRelation(source_id=spec, target_id=plan_id, relation_type=RelationType.CONTAINS)
            )
            if require_review:
                gate_id = self.graph.inject_plan_review_gate(plan_id)

        if require_review:
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
        self._validate_priority(priority)
        if self.node_repo.get_node(plan) is None:
            raise OperationError(f"plan '{plan}' not found", 404)
        missing_deps = [d for d in (depends_on or []) if self.node_repo.get_node(d) is None]
        if missing_deps:
            raise OperationError(f"dependency not found: {', '.join(missing_deps)}", 404)
        if slug:
            task_id = f"{plan}-{slug}"
            if self.node_repo.get_node(task_id) is not None:
                raise OperationError(f"'{task_id}' already exists", 409)
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
        with self.node_repo.transaction():
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
        with self.node_repo.transaction():
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

        # A replaced task is not being worked on: its lease and file locks go with it. The
        # lease lives in a separate database from the node writes below, so it is released
        # ahead of, and independent of, the transaction guarding those.
        self.runtime_repo.release_lease(old_id)
        old_node.status = NodeStatus.SUPERSEDED

        with self.node_repo.transaction():
            self.node_repo.save_node(old_node)
            self.node_repo.add_relation(
                NodeRelation(
                    source_id=new_id, target_id=old_id, relation_type=RelationType.SUPERSEDES
                )
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
        task_node = self.node_repo.get_node(task_id)
        if task_node is None:
            raise OperationError(f"Task '{task_id}' not found", 404)
        if task_node.kind != NodeKind.TASK:
            raise OperationError(f"'{task_id}' is not a task", 400)
        plan_node = self.node_repo.get_node(plan_id)
        if plan_node is None:
            raise OperationError(f"Plan '{plan_id}' not found", 404)
        if plan_node.kind != NodeKind.PLAN:
            raise OperationError(f"'{plan_id}' is not a plan", 400)
        with self.node_repo.transaction():
            with self.node_repo.db.get_spec_connection() as conn:
                row = conn.execute(
                    "SELECT source_id FROM node_relations "
                    "WHERE target_id = ? AND relation_type = ?",
                    (task_id, RelationType.CONTAINS.value),
                ).fetchone()
            old_plan = row[0] if row else None
            if old_plan is not None:
                self.node_repo.remove_relation(old_plan, task_id, RelationType.CONTAINS)
            self.node_repo.add_relation(
                NodeRelation(
                    source_id=plan_id, target_id=task_id, relation_type=RelationType.CONTAINS
                )
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
        section: tuple[str, str, str | None] | None = None,
    ) -> None:
        node = self.node_repo.get_node(task_id)
        if node is not None and node.kind == NodeKind.DECISION:
            raise OperationError(
                f"'{task_id}' is a decision; use `tm decision answer/withdraw/reopen`", 409
            )
        payload: dict[str, Any] = {"status": status.value, "remove_worktree": remove_worktree}
        # Both writes share one `node_repo` transaction: `stop_task`'s status change and the
        # section write either both land or neither does, so a ruling can never land without
        # its status even when the status write itself succeeds and the section write is what
        # fails.
        with self.node_repo.transaction():
            self.coordinator.stop_task(
                task_id=task_id, new_status=status, remove_worktree=remove_worktree
            )
            if section is not None:
                key, content, header = section
                self._write_section(task_id, key, content, header)
                payload["section"] = key
        self._ledger(LedgerCommand.TASK_STOP, target_id=task_id, payload=payload)

    def release_lease(self, task_id: str) -> None:
        if self.node_repo.get_node(task_id) is None:
            raise OperationError(f"node '{task_id}' not found", 404)
        self.runtime_repo.release_lease(task_id)
        self._ledger(LedgerCommand.LEASE_RELEASE, target_id=task_id)

    def sweep_leases(self) -> list[str]:
        swept = self.runtime_repo.sweep_expired_leases()
        if swept:
            # An abandoned claim returns the task to the state before it, or nobody could claim it.
            with self.node_repo.transaction():
                for task_id in swept:
                    node = self.node_repo.get_node(task_id)
                    if node is not None and node.status in _SWEEP_BACK:
                        node.status = _SWEEP_BACK[node.status]
                        self.node_repo.save_node(node)
            self._ledger(LedgerCommand.LEASE_SWEEP, payload={"swept_tasks": swept})
        return swept

    # -- sections -------------------------------------------------------------------------

    def _write_section(
        self, node_id: str, section_key: str, content: str, header: str | None
    ) -> None:
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
        self._write_section(node_id, section_key, content, header)
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
        if self.node_repo.get_node(task_id) is None:
            raise OperationError(f"task '{task_id}' not found", 404)
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

    def run_verifications(
        self, task_id: str | None, ref: str | None = None
    ) -> tuple[bool, list[VerificationResult]]:
        if ref is not None and not task_id:
            raise OperationError("--ref checks one task's repo; pass a task id", 400)

        repo_for_node: dict[str, str | None] = {}
        if task_id:
            node = self.node_repo.get_node(task_id)
            if node is None:
                raise OperationError(f"task '{task_id}' not found", 404)
            repo_for_node[task_id] = node.target_repo
            vers = self.node_repo.get_verifications(task_id)
        else:
            vers = []
            for t in self.node_repo.list_nodes(kind=NodeKind.TASK):
                repo_for_node[t.id] = t.target_repo
                vers.extend(self.node_repo.get_verifications(t.id))

        if not vers:
            raise OperationError(
                "No verifications to run: an empty check set proves nothing. Add one with "
                "`tm verify add`, or attest the task.",
                400,
            )

        results = self.verification_engine.verify_all(vers, repo_for_node, ref)
        all_passed = all(r.passed for r in results)
        self._ledger(
            LedgerCommand.VERIFICATION_RUN,
            target_id=task_id,
            payload={"passed": all_passed, "count": len(results)},
        )
        return all_passed, results

    # -- decisions ------------------------------------------------------------------------------

    @staticmethod
    def _parse_option(raw: str) -> DecisionOption:
        parts = raw.split("|")
        key = parts[0].strip() if parts else ""
        label = parts[1].strip() if len(parts) > 1 else ""
        if not key or not label:
            raise OperationError(f"--option takes 'key|Label|description', got '{raw}'", 400)
        description = parts[2].strip() if len(parts) > 2 else ""
        return DecisionOption(key=key, label=label, description=description)

    def _get_decision(self, decision_id: str) -> Node:
        node = self.node_repo.get_node(decision_id)
        if node is None or node.kind != NodeKind.DECISION:
            raise OperationError(f"decision '{decision_id}' not found", 404)
        return node

    def add_decision(
        self,
        question: str,
        slug: str | None = None,
        priority: int = 50,
        context: str | None = None,
        options: list[str] | None = None,
        recommend: str | None = None,
        allow_custom: bool = True,
        raised_by: str | None = None,
        blocks: list[str] | None = None,
    ) -> str:
        if slug:
            decision_id = f"decision-{slug}"
            if self.node_repo.get_node(decision_id) is not None:
                raise OperationError(f"'{decision_id}' already exists", 409)
        else:
            existing = {n.id for n in self.node_repo.list_nodes(kind=NodeKind.DECISION)}
            counter = 1
            while f"decision-D{counter}" in existing:
                counter += 1
            decision_id = f"decision-D{counter}"

        parsed_options = [self._parse_option(o) for o in options or []]
        keys = [o.key for o in parsed_options]
        if len(keys) != len(set(keys)):
            raise OperationError("option keys must be unique", 400)
        if recommend is not None and recommend not in keys:
            raise OperationError(f"'{recommend}' is not one of the option keys", 400)
        for opt in parsed_options:
            opt.recommended = opt.key == recommend

        blocked_tasks = blocks or []
        for task_id in blocked_tasks:
            blocked_node = self.node_repo.get_node(task_id)
            if blocked_node is None:
                raise OperationError(f"task '{task_id}' not found", 404)
            if blocked_node.kind != NodeKind.TASK:
                raise OperationError(f"'{task_id}' is not a task", 400)
            # The edge is task_id -> decision_id (DEPENDS_ON); would_cause_cycle walks from the
            # target back toward the source, so it sees the decision-to-be as already existing
            # were it not brand new -- checked anyway, since a `blocks` list can name a decision
            # that already depends on `task_id` through some other chain.
            if self.graph.would_cause_cycle(task_id, decision_id):
                raise OperationError(f"'{task_id}' -> '{decision_id}' would make a cycle", 409)
        if raised_by is not None and self.node_repo.get_node(raised_by) is None:
            raise OperationError(f"'{raised_by}' not found", 404)

        data = DecisionData(options=parsed_options, allow_custom=allow_custom, raised_by=raised_by)
        node = Node(
            id=decision_id,
            kind=NodeKind.DECISION,
            title=question,
            priority=priority,
            frontmatter={"decision": data.model_dump(mode="json")},
        )
        with self.node_repo.transaction():
            self.node_repo.save_node(node)
            if context:
                self.node_repo.save_section(
                    NodeSection(
                        node_id=decision_id,
                        section_key="context",
                        ordinal=1,
                        header="## Context",
                        content=context,
                    )
                )
            for task_id in blocked_tasks:
                self.node_repo.add_relation(
                    NodeRelation(
                        source_id=task_id,
                        target_id=decision_id,
                        relation_type=RelationType.DEPENDS_ON,
                    )
                )
        self._ledger(
            LedgerCommand.DECISION_ADD, target_id=decision_id, payload={"question": question}
        )
        return decision_id

    def answer_decision(
        self,
        decision_id: str,
        option: str | None = None,
        text: str = "",
        rationale: str = "",
        by: str = "cli",
    ) -> None:
        node = self._get_decision(decision_id)
        if node.status != NodeStatus.NOT_STARTED:
            raise OperationError(f"decision '{decision_id}' is not open; reopen it first", 409)
        data = read_decision(node)
        if option is not None:
            if option not in {o.key for o in data.options}:
                raise OperationError(f"'{option}' is not an option of '{decision_id}'", 400)
        elif not data.allow_custom:
            raise OperationError(f"decision '{decision_id}' does not allow a custom answer", 400)
        elif not text:
            raise OperationError("give --option or --custom", 400)

        data.answer = DecisionAnswer(
            option=option,
            text=text,
            rationale=rationale,
            answered_by=by,
            answered_at=datetime.now(tz=UTC),
        )
        write_decision(node, data)
        node.status = NodeStatus.COMPLETED
        node.updated_at = datetime.now(tz=UTC)
        with self.node_repo.transaction():
            self.node_repo.save_node(node)
        self._ledger(
            LedgerCommand.DECISION_ANSWER, target_id=decision_id, payload={"option": option}
        )

    def reopen_decision(self, decision_id: str) -> None:
        node = self._get_decision(decision_id)
        if node.status == NodeStatus.NOT_STARTED:
            raise OperationError(f"decision '{decision_id}' is already open", 409)
        data = read_decision(node)
        data.answer = None
        data.withdrawn_reason = ""
        write_decision(node, data)
        node.status = NodeStatus.NOT_STARTED
        node.updated_at = datetime.now(tz=UTC)
        self.node_repo.save_node(node)
        self._ledger(LedgerCommand.DECISION_REOPEN, target_id=decision_id)

    def withdraw_decision(self, decision_id: str, reason: str = "") -> None:
        node = self._get_decision(decision_id)
        data = read_decision(node)
        data.withdrawn_reason = reason
        write_decision(node, data)
        node.status = NodeStatus.ABANDONED
        node.updated_at = datetime.now(tz=UTC)
        self.node_repo.save_node(node)
        self._ledger(
            LedgerCommand.DECISION_WITHDRAW, target_id=decision_id, payload={"reason": reason}
        )

    def link_decision(
        self, decision_id: str, add: list[str] | None = None, remove: list[str] | None = None
    ) -> None:
        self._get_decision(decision_id)
        add_ids = add or []
        remove_ids = remove or []
        for task_id in add_ids:
            node = self.node_repo.get_node(task_id)
            if node is None:
                raise OperationError(f"task '{task_id}' not found", 404)
            if node.kind != NodeKind.TASK:
                raise OperationError(f"'{task_id}' is not a task", 400)
            if self.graph.would_cause_cycle(task_id, decision_id):
                raise OperationError(f"'{task_id}' -> '{decision_id}' would make a cycle", 409)
        for task_id in remove_ids:
            if decision_id not in self.node_repo.get_dependencies(task_id):
                raise OperationError(f"'{task_id}' does not wait on '{decision_id}'", 409)
        with self.node_repo.transaction():
            for task_id in add_ids:
                self.node_repo.add_relation(
                    NodeRelation(
                        source_id=task_id,
                        target_id=decision_id,
                        relation_type=RelationType.DEPENDS_ON,
                    )
                )
            for task_id in remove_ids:
                self.node_repo.remove_relation(task_id, decision_id, RelationType.DEPENDS_ON)
        self._ledger(
            LedgerCommand.DECISION_LINK,
            target_id=decision_id,
            payload={"add": add_ids, "remove": remove_ids},
        )

    # -- attachments ------------------------------------------------------------------------

    def _assets_dir(self) -> Path:
        return self.node_repo.db.taskmanager_dir / "assets"

    def _project_root(self) -> Path:
        return self.node_repo.db.taskmanager_dir.parent

    def attach(
        self,
        node_id: str,
        file_path: Path,
        caption: str = "",
        source: str | None = None,
        replace: str | None = None,
    ) -> dict[str, Any]:
        node = self.node_repo.get_node(node_id)
        if node is None:
            raise OperationError(f"node '{node_id}' not found", 404)
        if not file_path.is_file():
            raise OperationError(f"'{file_path}' does not exist", 400)

        attachments = list(node.frontmatter.get("attachments") or [])
        replace_idx: int | None = None
        if replace is not None:
            replace_idx = next(
                (i for i, a in enumerate(attachments) if a.get("asset") == replace), None
            )
            if replace_idx is None:
                raise OperationError(f"no attachment '{replace}' on '{node_id}'", 404)

        try:
            asset_name, mime = store_asset(self._assets_dir(), file_path)
        except AssetError as exc:
            raise OperationError(str(exc), 400) from exc

        prior = attachments[replace_idx] if replace_idx is not None else {}
        effective_caption = caption or prior.get("caption", "")
        effective_source = source or (prior.get("source") or {}).get("uri")
        if effective_source is None:
            try:
                effective_source = str(file_path.resolve().relative_to(self._project_root()))
            except ValueError:
                effective_source = None

        source_sha: str | None = None
        source_state = "unverifiable"
        if effective_source is not None and is_project_relative(effective_source):
            src_path = self._project_root() / effective_source
            if src_path.is_file():
                source_sha = hashlib.sha256(src_path.read_bytes()).hexdigest()
                source_state = "fresh"
            else:
                source_state = "missing"

        entry = {
            "asset": asset_name,
            "name": file_path.name,
            "caption": effective_caption,
            "mime": mime,
            "source": AttachmentSource(
                uri=effective_source,
                sha256=source_sha,
                captured_at=datetime.now(tz=UTC),
                state=source_state,  # type: ignore[arg-type]
            ).model_dump(mode="json"),
        }
        if replace_idx is not None:
            attachments[replace_idx] = entry
        else:
            attachments.append(entry)
        node.frontmatter["attachments"] = attachments
        node.updated_at = datetime.now(tz=UTC)
        self.node_repo.save_node(node)
        self._ledger(LedgerCommand.ATTACH, target_id=node_id, payload={"asset": asset_name})
        return entry

    def _asset_referenced(self, asset: str) -> bool:
        return any(
            a.get("asset") == asset
            for n in self.node_repo.list_nodes()
            for a in n.frontmatter.get("attachments") or []
        )

    def detach(self, node_id: str, asset: str) -> None:
        node = self.node_repo.get_node(node_id)
        if node is None:
            raise OperationError(f"node '{node_id}' not found", 404)
        attachments = list(node.frontmatter.get("attachments") or [])
        idx = next((i for i, a in enumerate(attachments) if a.get("asset") == asset), None)
        if idx is None:
            raise OperationError(f"no attachment '{asset}' on '{node_id}'", 404)
        del attachments[idx]
        node.frontmatter["attachments"] = attachments
        node.updated_at = datetime.now(tz=UTC)
        self.node_repo.save_node(node)
        self._ledger(LedgerCommand.DETACH, target_id=node_id, payload={"asset": asset})
        # `save_node` above already persisted `node_id`'s attachments with the entry removed,
        # so checking every node (this one included) is correct: a second entry on `node_id`
        # itself pointing at the same content-addressed asset is exactly the case a per-node
        # exclusion used to miss, deleting the file while that second entry still referenced it.
        if not self._asset_referenced(asset):
            (self._assets_dir() / asset).unlink(missing_ok=True)

    def list_attachments(self, node_id: str, check: bool = False) -> list[dict[str, Any]]:
        node = self.node_repo.get_node(node_id)
        if node is None:
            raise OperationError(f"node '{node_id}' not found", 404)
        attachments = list(node.frontmatter.get("attachments") or [])
        if not check:
            return attachments

        changed = False
        for entry in attachments:
            source = dict(entry.get("source") or {})
            uri = source.get("uri")
            if not uri or not is_project_relative(uri):
                continue
            path = self._project_root() / uri
            if not path.is_file():
                state = "missing"
            else:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                state = "fresh" if digest == source.get("sha256") else "stale"
            source["state"] = state
            source["checked_at"] = datetime.now(tz=UTC).isoformat()
            entry["source"] = source
            changed = True

        if changed:
            node.frontmatter["attachments"] = attachments
            node.updated_at = datetime.now(tz=UTC)
            self.node_repo.save_node(node)
            self._ledger(LedgerCommand.ATTACHMENT_CHECK, target_id=node_id)
        return attachments
