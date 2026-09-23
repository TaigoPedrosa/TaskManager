from typing import Any

from taskmanager.core.enums import NodeKind, NodeStatus, RelationType, VerificationType
from taskmanager.core.models import (
    Node,
    NodeRelation,
    NodeSection,
    NodeVerification,
)
from taskmanager.db.node_repo import NodeRepository


class BulkImporter:
    def __init__(self, node_repo: NodeRepository) -> None:
        self.node_repo = node_repo

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
        }
    )
    DOCUMENT_KEYS = frozenset({"spec", "plans", "tasks", "decisions"})
    VERIFICATION_KEYS = frozenset(
        {"type", "verification_type", "target_path", "expected_pattern", "codegraph_query_json"}
    )

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
        verifications: list[NodeVerification] = []
        replace_verifications: set[str] = set()

        spec_data = data.get("spec")
        if spec_data:
            spec_node = self._parse_node(
                spec_data, NodeKind.SPEC, self.node_repo.get_node(spec_data["id"])
            )
            nodes.append(spec_node)
            sections.extend(self._parse_sections(spec_node.id, spec_data.get("sections")))
            for dep in spec_data.get("depends_on", []):
                dep_id, metadata = self._parse_dep(dep)
                relations.append(
                    NodeRelation(
                        source_id=spec_node.id,
                        target_id=dep_id,
                        relation_type=RelationType.DEPENDS_ON,
                        metadata=metadata,
                    )
                )

        for p_idx, plan_data in enumerate(data.get("plans", []), start=1):
            if "ordinal" not in plan_data:
                plan_data["ordinal"] = p_idx
            plan_node = self._parse_node(
                plan_data, NodeKind.PLAN, self.node_repo.get_node(plan_data["id"])
            )
            nodes.append(plan_node)
            sections.extend(self._parse_sections(plan_node.id, plan_data.get("sections")))
            if spec_data:
                relations.append(
                    NodeRelation(
                        source_id=spec_data["id"],
                        target_id=plan_node.id,
                        relation_type=RelationType.CONTAINS,
                    )
                )
            for dep in plan_data.get("depends_on", []):
                dep_id, metadata = self._parse_dep(dep)
                relations.append(
                    NodeRelation(
                        source_id=plan_node.id,
                        target_id=dep_id,
                        relation_type=RelationType.DEPENDS_ON,
                        metadata=metadata,
                    )
                )

            for t_idx, task_data in enumerate(plan_data.get("tasks", []), start=1):
                if "ordinal" not in task_data:
                    task_data["ordinal"] = t_idx
                task_node = self._parse_node(
                    task_data, NodeKind.TASK, self.node_repo.get_node(task_data["id"])
                )
                nodes.append(task_node)
                sections.extend(self._parse_sections(task_node.id, task_data.get("sections")))
                relations.append(
                    NodeRelation(
                        source_id=plan_node.id,
                        target_id=task_node.id,
                        relation_type=RelationType.CONTAINS,
                    )
                )
                for dep in task_data.get("depends_on", []):
                    dep_id, metadata = self._parse_dep(dep)
                    relations.append(
                        NodeRelation(
                            source_id=task_node.id,
                            target_id=dep_id,
                            relation_type=RelationType.DEPENDS_ON,
                            metadata=metadata,
                        )
                    )
                if "verifications" in task_data:
                    replace_verifications.add(task_node.id)
                for ver_data in task_data.get("verifications", []):
                    v_type_val = ver_data.get("verification_type") or ver_data.get("type")
                    v_type = (
                        VerificationType(v_type_val) if isinstance(v_type_val, str) else v_type_val
                    )
                    verifications.append(
                        NodeVerification(
                            node_id=task_node.id,
                            verification_type=v_type,
                            target_path=ver_data["target_path"],
                            expected_pattern=ver_data.get("expected_pattern"),
                            codegraph_query_json=ver_data.get("codegraph_query_json"),
                        )
                    )

        for task_data in data.get("tasks", []):
            task_node = self._parse_node(
                task_data, NodeKind.TASK, self.node_repo.get_node(task_data["id"])
            )
            nodes.append(task_node)
            sections.extend(self._parse_sections(task_node.id, task_data.get("sections")))
            if spec_data:
                relations.append(
                    NodeRelation(
                        source_id=spec_data["id"],
                        target_id=task_node.id,
                        relation_type=RelationType.CONTAINS,
                    )
                )
            for dep in task_data.get("depends_on", []):
                dep_id, metadata = self._parse_dep(dep)
                relations.append(
                    NodeRelation(
                        source_id=task_node.id,
                        target_id=dep_id,
                        relation_type=RelationType.DEPENDS_ON,
                        metadata=metadata,
                    )
                )
            if "verifications" in task_data:
                replace_verifications.add(task_node.id)
            for ver_data in task_data.get("verifications", []):
                v_type_val = ver_data.get("verification_type") or ver_data.get("type")
                v_type = VerificationType(v_type_val) if isinstance(v_type_val, str) else v_type_val
                verifications.append(
                    NodeVerification(
                        node_id=task_node.id,
                        verification_type=v_type,
                        target_path=ver_data["target_path"],
                        expected_pattern=ver_data.get("expected_pattern"),
                        codegraph_query_json=ver_data.get("codegraph_query_json"),
                    )
                )

        for dec_data in data.get("decisions", []):
            dec_node = self._parse_node(
                dec_data, NodeKind.DECISION, self.node_repo.get_node(dec_data["id"])
            )
            nodes.append(dec_node)
            sections.extend(self._parse_sections(dec_node.id, dec_data.get("sections")))
            for dep in dec_data.get("depends_on", []):
                dep_id, metadata = self._parse_dep(dep)
                relations.append(
                    NodeRelation(
                        source_id=dec_node.id,
                        target_id=dep_id,
                        relation_type=RelationType.DEPENDS_ON,
                        metadata=metadata,
                    )
                )

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
            raise ValueError(f"import refused, nothing written: unknown ids {unknown}")

        for node in nodes:
            self.node_repo.save_node(node)
        for section in sections:
            self.node_repo.save_section(section)
        for rel in relations:
            self.node_repo.add_relation(rel)
        # A document that states a task's checks replaces the set it had.
        for node_id in replace_verifications:
            self.node_repo.clear_verifications(node_id)
        for ver in verifications:
            self.node_repo.add_verification(ver)

    @staticmethod
    def _parse_dep(dep: Any) -> tuple[str, dict[str, Any]]:
        """A `depends_on` entry: a bare id (COMPLETED gate, as always), or `{"id", "gate"}`."""
        if isinstance(dep, dict):
            return dep["id"], ({"gate": dep["gate"]} if dep.get("gate") else {})
        return dep, {}

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

        status_val = pick("status", NodeStatus.NOT_STARTED)
        return Node(
            id=data["id"],
            kind=NodeKind(data.get("kind", default_kind)),
            title=data["title"],
            status=NodeStatus(status_val) if isinstance(status_val, str) else status_val,
            priority=pick("priority", 50),
            ordinal=data.get("ordinal", existing.ordinal if existing is not None else 0),
            target_repo=pick("target_repo", None),
            acceptable_models=pick("acceptable_models", []),
            frontmatter=pick("frontmatter", {}),
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
