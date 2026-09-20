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

    def import_dict(self, data: dict[str, Any]) -> None:
        nodes: list[Node] = []
        sections: list[NodeSection] = []
        relations: list[NodeRelation] = []
        verifications: list[NodeVerification] = []

        spec_data = data.get("spec")
        if spec_data:
            spec_node = self._parse_node(
                spec_data, NodeKind.SPEC, self.node_repo.get_node(spec_data["id"])
            )
            nodes.append(spec_node)
            sections.extend(self._parse_sections(spec_node.id, spec_data.get("sections")))
            for dep in spec_data.get("depends_on", []):
                relations.append(
                    NodeRelation(
                        source_id=spec_node.id,
                        target_id=dep,
                        relation_type=RelationType.DEPENDS_ON,
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
                relations.append(
                    NodeRelation(
                        source_id=plan_node.id,
                        target_id=dep,
                        relation_type=RelationType.DEPENDS_ON,
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
                    relations.append(
                        NodeRelation(
                            source_id=task_node.id,
                            target_id=dep,
                            relation_type=RelationType.DEPENDS_ON,
                        )
                    )
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
                relations.append(
                    NodeRelation(
                        source_id=task_node.id,
                        target_id=dep,
                        relation_type=RelationType.DEPENDS_ON,
                    )
                )
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
        for ver in verifications:
            self.node_repo.add_verification(ver)

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
