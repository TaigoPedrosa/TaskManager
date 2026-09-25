from enum import StrEnum
from typing import Any, cast

from taskmanager.core.enums import NodeKind, RelationType, VerificationType
from taskmanager.core.models import (
    Condition,
    Node,
    NodeRelation,
    NodeSection,
    NodeVerification,
)
from taskmanager.core.status import ConditionStage, DecisionStatus, Merge, Outcome, Status
from taskmanager.db.job_repo import JobRepository
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.engine.conditions import is_executable
from taskmanager.engine.operations import validated_write
from taskmanager.engine.snapshot import SnapshotBuilder, roll_up_ancestors

REFUSED = "import refused, nothing written: "


def _optional[E: StrEnum](kind: type[E], value: Any) -> E | None:
    return None if value is None else kind(value)


class BulkImporter:
    def __init__(self, node_repo: NodeRepository) -> None:
        self.node_repo = node_repo
        self.snapshots = SnapshotBuilder(
            node_repo, RuntimeRepository(node_repo.db), JobRepository(node_repo.db)
        )

    NODE_KEYS = frozenset(
        {
            "id",
            "kind",
            "title",
            "status",
            "priority",
            "ordinal",
            "target_repo",
            "acceptable_models",
            "frontmatter",
            "sections",
            "depends_on",
            "verifications",
            "tasks",
            "review",
            "fix",
            "merge",
            "requires",
            "conditions",
            "land_order",
            "branch",
            "outcome",
            "verdict",
            "fix_for",
            "claimed_from",
            "review_cycles",
            "merge_attempts",
            "step_failures",
        }
    )
    DOCUMENT_KEYS = frozenset({"spec", "plans", "tasks", "decisions"})
    VERIFICATION_KEYS = frozenset(
        {"type", "verification_type", "target_path", "expected_pattern", "codegraph_query_json"}
    )
    CONDITION_KEYS = frozenset({"needs", "command", "stage"})

    def _refuse_unknown_keys(self, data: dict[str, Any]) -> None:
        """A key nothing reads would be dropped silently, and its content with it."""
        problems: list[str] = []

        def check(where: str, doc: dict[str, Any], allowed: frozenset[str]) -> None:
            extra = sorted(set(doc) - allowed)
            if extra:
                problems.append(f"{where}: {', '.join(extra)}")

        check("document", data, self.DOCUMENT_KEYS)
        nodes: list[tuple[str, dict[str, Any]]] = []
        if isinstance(data.get("spec"), dict):
            nodes.append(("spec", data["spec"]))
        for plan in data.get("plans") or []:
            nodes.append((f"plan {plan.get('id')}", plan))
            nodes.extend((f"task {t.get('id')}", t) for t in plan.get("tasks") or [])
        nodes.extend((f"task {t.get('id')}", t) for t in data.get("tasks") or [])
        nodes.extend((f"decision {d.get('id')}", d) for d in data.get("decisions") or [])
        for where, node in nodes:
            check(where, node, self.NODE_KEYS)
            for v in node.get("verifications") or []:
                check(f"{where} verification", v, self.VERIFICATION_KEYS)
            for c in node.get("conditions") or []:
                check(f"{where} condition", c, self.CONDITION_KEYS)
        if problems:
            raise ValueError(
                "import refused, nothing written: unknown keys ("
                + "; ".join(problems)
                + "). Section text belongs under `sections:`, other data under `frontmatter:`."
            )

    def import_dict(self, data: dict[str, Any]) -> None:
        self._refuse_unknown_keys(data)
        nodes: list[Node] = []
        sections: list[NodeSection] = []
        relations: list[NodeRelation] = []
        verifications: dict[str, list[NodeVerification]] = {}
        conditions: dict[str, list[Condition]] = {}

        def take(raw: dict[str, Any], kind: NodeKind, parent: str | None) -> None:
            node = self._parse_node(raw, kind, self.node_repo.get_node(raw["id"]))
            nodes.append(node)
            sections.extend(self._parse_sections(node.id, raw.get("sections")))
            if parent is not None:
                relations.append(
                    NodeRelation(
                        source_id=parent, target_id=node.id, relation_type=RelationType.CONTAINS
                    )
                )
            relations.extend(
                NodeRelation(
                    source_id=node.id,
                    target_id=self._parse_dep(dep),
                    relation_type=RelationType.DEPENDS_ON,
                )
                for dep in raw.get("depends_on", [])
            )
            if "verifications" in raw:
                verifications[node.id] = [
                    self._parse_verification(node.id, v) for v in raw["verifications"]
                ]
            if "conditions" in raw:
                conditions[node.id] = [self._parse_condition(node.id, c) for c in raw["conditions"]]

        spec_data = data.get("spec")
        spec_id = spec_data["id"] if spec_data else None
        if spec_data:
            take(spec_data, NodeKind.SPEC, None)
        for p_idx, plan_data in enumerate(data.get("plans", []), start=1):
            plan_data.setdefault("ordinal", p_idx)
            take(plan_data, NodeKind.PLAN, spec_id)
            for t_idx, task_data in enumerate(plan_data.get("tasks", []), start=1):
                task_data.setdefault("ordinal", t_idx)
                take(task_data, NodeKind.TASK, plan_data["id"])
        for task_data in data.get("tasks", []):
            take(task_data, NodeKind.TASK, spec_id)
        for dec_data in data.get("decisions", []):
            take(dec_data, NodeKind.DECISION, None)

        known = {n.id for n in nodes}
        unknown = sorted(
            {
                i
                for r in relations
                for i in (r.source_id, r.target_id)
                if i not in known and self.node_repo.get_node(i) is None
            }
        )
        if unknown:
            raise ValueError(f"{REFUSED}unknown ids {unknown}")

        with validated_write(self.node_repo, self.snapshots, known, prefix=REFUSED):
            for node in nodes:
                self.node_repo.save_node(node)
            for section in sections:
                self.node_repo.save_section(section)
            for rel in relations:
                self.node_repo.add_relation(rel)
            # A document that states a node's checks or conditions replaces the set it had.
            for node_id, vers in verifications.items():
                self.node_repo.clear_verifications(node_id)
                for ver in vers:
                    self.node_repo.add_verification(ver)
            for node_id, conds in conditions.items():
                for old in self.node_repo.get_conditions(node_id):
                    self.node_repo.remove_condition(node_id, old.idx)
                for cond in conds:
                    self.node_repo.add_condition(cond)
            for node in nodes:
                roll_up_ancestors(self.node_repo, node.id)

    @staticmethod
    def _parse_dep(dep: Any) -> str:
        # ValueError, not TypeError: every refusal in this module is caught as one and reported
        # as "nothing written" by the CLI and by callers that catch ValueError.
        if isinstance(dep, dict):
            raise ValueError(  # noqa: TRY004
                f"{REFUSED}a dependency is a bare id, not {dep!r}: an edge no longer carries a "
                "gate, it waits for the dependency's code to land where this node builds"
            )
        return str(dep)

    @staticmethod
    def _parse_verification(node_id: str, raw: dict[str, Any]) -> NodeVerification:
        v_type = cast("str", raw.get("verification_type") or raw.get("type"))
        return NodeVerification(
            node_id=node_id,
            verification_type=VerificationType(v_type),
            target_path=raw["target_path"],
            expected_pattern=raw.get("expected_pattern"),
            codegraph_query_json=raw.get("codegraph_query_json"),
        )

    @staticmethod
    def _parse_condition(node_id: str, raw: dict[str, Any]) -> Condition:
        needs = str(raw.get("needs") or "")
        command = str(raw.get("command") or "")
        if not command.strip() or not is_executable(command):
            raise ValueError(
                f"{REFUSED}condition {needs!r} on {node_id!r} has no command that exits 0 once "
                "it holds; a wait nobody can check is a decision"
            )
        return Condition(
            node_id=node_id,
            idx=0,
            needs=needs,
            command=command,
            stage=ConditionStage(raw.get("stage", ConditionStage.CLAIM.value)),
        )

    @staticmethod
    def _parse_status(node_id: str, kind: NodeKind, value: Any) -> Status | DecisionStatus:
        allowed: type[Status | DecisionStatus] = (
            DecisionStatus if kind == NodeKind.DECISION else Status
        )
        try:
            return allowed(value)
        except ValueError:
            names = ", ".join(s.value for s in allowed)
            raise ValueError(
                f"{REFUSED}node {node_id!r} has status {value!r}, which a {kind.value} cannot "
                f"hold; one of: {names}"
            ) from None

    @staticmethod
    def _parse_node(
        data: dict[str, Any], default_kind: NodeKind, existing: Node | None = None
    ) -> Node:
        """A key the document omits keeps the value the node already has, so importing a document
        again never resets the progress recorded since; a key it states wins."""

        def pick(key: str, default: Any) -> Any:
            if key in data:
                return data[key]
            return getattr(existing, key) if existing is not None else default

        node_id = data["id"]
        kind = NodeKind(data.get("kind", default_kind))
        title = pick("title", None)
        if title is None:
            raise ValueError(
                f"{REFUSED}node {node_id!r} has no title and none exists to fall back to"
            )
        status: Status | DecisionStatus
        if "status" in data:
            status = BulkImporter._parse_status(node_id, kind, data["status"])
        elif existing is not None:
            status = existing.status
        else:
            status = DecisionStatus.OPEN if kind == NodeKind.DECISION else Status.READY
        review = bool(pick("review", kind == NodeKind.TASK))
        fix = bool(pick("fix", kind == NodeKind.TASK))
        if fix and not review:
            raise ValueError(
                f"{REFUSED}node {node_id!r} sets fix without review: a rejection is fixed by "
                "the node that was reviewed; set review or drop fix"
            )
        return Node(
            id=node_id,
            kind=kind,
            title=title,
            status=status,
            priority=pick("priority", 50),
            ordinal=data.get("ordinal", existing.ordinal if existing is not None else 0),
            target_repo=pick("target_repo", None),
            acceptable_models=pick("acceptable_models", []),
            frontmatter=pick("frontmatter", {}),
            review=review,
            fix=fix,
            merge=Merge(pick("merge", Merge.MAIN)),
            requires=list(pick("requires", [])),
            land_order=list(pick("land_order", [])),
            branch=pick("branch", None),
            outcome=_optional(Outcome, pick("outcome", None)),
            verdict=pick("verdict", None),
            fix_for=_optional(Outcome, pick("fix_for", None)),
            claimed_from=_optional(Status, pick("claimed_from", None)),
            review_cycles=int(pick("review_cycles", 0)),
            merge_attempts=int(pick("merge_attempts", 0)),
            step_failures=int(pick("step_failures", 0)),
        )

    @staticmethod
    def _parse_sections(node_id: str, sections_data: Any) -> list[NodeSection]:
        parsed: list[NodeSection] = []
        if not sections_data:
            return parsed

        if isinstance(sections_data, list):
            for idx, item in enumerate(sections_data, start=1):
                if isinstance(item, NodeSection):
                    parsed.append(item)
                elif isinstance(item, dict):
                    sec_key = str(item.get("section_key") or item.get("key") or f"section_{idx}")
                    header = str(item.get("header") or f"## {sec_key.capitalize()}")
                    content = str(item.get("content", ""))
                    ordinal = int(item.get("ordinal", idx))
                    parsed.append(
                        NodeSection(
                            node_id=node_id,
                            section_key=sec_key,
                            ordinal=ordinal,
                            header=header,
                            content=content,
                        )
                    )
        elif isinstance(sections_data, dict):
            for idx, (key, val) in enumerate(sections_data.items(), start=1):
                if isinstance(val, dict):
                    sec_key = str(val.get("section_key") or val.get("key") or key)
                    header = str(val.get("header") or f"## {sec_key.capitalize()}")
                    content = str(val.get("content", ""))
                    ordinal = int(val.get("ordinal", idx))
                else:
                    sec_key = str(key)
                    header = f"## {sec_key.capitalize()}"
                    content = str(val)
                    ordinal = idx
                parsed.append(
                    NodeSection(
                        node_id=node_id,
                        section_key=sec_key,
                        ordinal=ordinal,
                        header=header,
                        content=content,
                    )
                )

        return parsed
