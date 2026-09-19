from typing import Any

from taskmanager.core.enums import NodeKind, NodeStatus, RelationType
from taskmanager.core.models import Node, NodeRelation, NodeSection
from taskmanager.db.node_repo import NodeRepository


class BulkImporter:
    def __init__(self, node_repo: NodeRepository) -> None:
        self.node_repo = node_repo

    def import_dict(self, data: dict[str, Any]) -> None:
        nodes: list[Node] = []
        sections: list[NodeSection] = []
        relations: list[NodeRelation] = []

        spec_data = data.get("spec")
        if spec_data:
            spec_node = self._parse_node(spec_data, NodeKind.SPEC)
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

        for plan_data in data.get("plans", []):
            plan_node = self._parse_node(plan_data, NodeKind.PLAN)
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

            for task_data in plan_data.get("tasks", []):
                task_node = self._parse_node(task_data, NodeKind.TASK)
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

        for task_data in data.get("tasks", []):
            task_node = self._parse_node(task_data, NodeKind.TASK)
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

        for node in nodes:
            self.node_repo.save_node(node)
        for section in sections:
            self.node_repo.save_section(section)
        for rel in relations:
            self.node_repo.add_relation(rel)

    @staticmethod
    def _parse_node(data: dict[str, Any], default_kind: NodeKind) -> Node:
        kind = NodeKind(data.get("kind", default_kind))
        status_val = data.get("status", NodeStatus.NOT_STARTED)
        status = NodeStatus(status_val) if isinstance(status_val, str) else status_val
        return Node(
            id=data["id"],
            kind=kind,
            title=data["title"],
            status=status,
            priority=data.get("priority", 50),
            target_repo=data.get("target_repo"),
            acceptable_models=data.get("acceptable_models", []),
            frontmatter=data.get("frontmatter", {}),
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
