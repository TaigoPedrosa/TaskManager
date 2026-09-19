import json
from typing import Any

from taskmanager.db.node_repo import NodeRepository


class MarkdownRenderer:
    def __init__(self, node_repo: NodeRepository) -> None:
        self.node_repo = node_repo

    def render(self, node_id: str, view: str = "full") -> str:
        node = self.node_repo.get_node(node_id)
        if not node:
            raise ValueError(f"Node {node_id} not found")

        sections = self.node_repo.get_all_sections(node_id)

        frontmatter_dict: dict[str, Any] = {
            "id": node.id,
            "kind": node.kind.value if hasattr(node.kind, "value") else str(node.kind),
            "title": node.title,
            "status": node.status.value if hasattr(node.status, "value") else str(node.status),
            "priority": node.priority,
        }
        if node.target_repo is not None:
            frontmatter_dict["target_repo"] = node.target_repo
        frontmatter_dict["acceptable_models"] = node.acceptable_models
        frontmatter_dict.update(node.frontmatter)

        yaml_lines = ["---"]
        for k, v in frontmatter_dict.items():
            if isinstance(v, bool):
                yaml_lines.append(f"{k}: {'true' if v else 'false'}")
            elif isinstance(v, (list, dict)):
                yaml_lines.append(f"{k}: {json.dumps(v)}")
            elif v is None:
                yaml_lines.append(f"{k}: null")
            else:
                yaml_lines.append(f"{k}: {v}")
        yaml_lines.append("---")
        frontmatter_text = "\n".join(yaml_lines)

        if view == "summary":
            overview_sec = self.node_repo.get_section(node_id, "overview")
            body = overview_sec.content if overview_sec else f"# {node.title}"
            return f"{frontmatter_text}\n\n{body}\n"

        if view == "subagent":
            out = [frontmatter_text]

            parent_ids = self._get_parent_ids(node_id)
            for pid in parent_ids:
                pnode = self.node_repo.get_node(pid)
                if pnode:
                    p_secs = self.node_repo.get_all_sections(pid)
                    for psec in p_secs:
                        if psec.section_key in ("context", "overview"):
                            out.append(f"## Parent Context ({pnode.title})\n\n{psec.content}")

            out.append(f"# Task Brief: {node.title}")
            for sec in sections:
                if sec.header:
                    out.append(f"{sec.header}\n\n{sec.content}")
                else:
                    out.append(sec.content)

            verifications = self.node_repo.get_verifications(node_id)
            if verifications:
                v_lines = ["### Verifications"]
                for v in verifications:
                    pat_str = f" (pattern: {v.expected_pattern})" if v.expected_pattern else ""
                    v_lines.append(f"- {v.verification_type.value}: `{v.target_path}`{pat_str}")
                out.append("\n".join(v_lines))

            return "\n\n".join(out) + "\n"

        if view == "full":
            out = [frontmatter_text, f"# {node.title}"]
            for sec in sections:
                if sec.header:
                    out.append(f"{sec.header}\n\n{sec.content}")
                else:
                    out.append(sec.content)
            return "\n\n".join(out) + "\n"

        raise ValueError(f"Unknown view '{view}'. Supported views: summary, subagent, full")

    def _get_parent_ids(self, node_id: str) -> list[str]:
        with self.node_repo.db.get_spec_connection() as conn:
            rows = conn.execute(
                "SELECT source_id FROM node_relations WHERE target_id = ? AND relation_type = 'contains' ORDER BY rowid ASC",
                (node_id,),
            ).fetchall()
            return [r[0] for r in rows]
