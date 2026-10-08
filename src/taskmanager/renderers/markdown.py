import json
from typing import Any

from taskmanager.core.enums import CONTAINERS, NodeKind, RelationType, RenderView
from taskmanager.core.models import Node
from taskmanager.core.status import DecisionStatus, Outcome, Status
from taskmanager.db.node_repo import NodeRepository
from taskmanager.engine.decisions import read_decision
from taskmanager.engine.snapshot import display_view, stored_status


class MarkdownRenderer:
    def __init__(self, node_repo: NodeRepository) -> None:
        self.node_repo = node_repo

    def render(self, node_id: str, view: RenderView | str = RenderView.FULL) -> str:
        node = self.node_repo.get_node(node_id)
        if not node:
            raise ValueError(f"Node {node_id} not found")

        try:
            v = RenderView(view) if isinstance(view, str) else view
        except ValueError:
            raise ValueError(
                f"Unknown view '{view}'. Supported views: {', '.join(s.value for s in RenderView)}"
            ) from None
        sections = self.node_repo.get_all_sections(node_id)

        if node.kind == NodeKind.DECISION:
            return self._render_decision(node, sections)

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
        for k, v_item in frontmatter_dict.items():
            if isinstance(v_item, bool):
                yaml_lines.append(f"{k}: {'true' if v_item else 'false'}")
            elif isinstance(v_item, (list, dict)):
                yaml_lines.append(f"{k}: {json.dumps(v_item)}")
            elif v_item is None:
                yaml_lines.append(f"{k}: null")
            else:
                yaml_lines.append(f"{k}: {v_item}")
        yaml_lines.append("---")
        frontmatter_text = "\n".join(yaml_lines)

        superseded = self._superseded(node)

        if v == RenderView.SUMMARY:
            overview_sec = self.node_repo.get_section(node_id, "overview")
            body = [overview_sec.content] if overview_sec else []
            title = [] if overview_sec else [f"# {node.title}"]
            return "\n\n".join([frontmatter_text, *title, *superseded, *body]) + "\n"

        if v == RenderView.SUBAGENT:
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
            out.extend(superseded)
            for sec in sections:
                if sec.header:
                    out.append(f"{sec.header}\n\n{sec.content}")
                else:
                    out.append(sec.content)

            verifications = self.node_repo.get_verifications(node_id)
            if verifications:
                v_lines = ["### Verifications"]
                for ver in verifications:
                    pat_str = f" (pattern: {ver.expected_pattern})" if ver.expected_pattern else ""
                    v_lines.append(f"- {ver.verification_type.value}: `{ver.target_path}`{pat_str}")
                out.append("\n".join(v_lines))

            rejected = self._rejected_children(node)
            if rejected:
                out.append(
                    "\n".join(
                        [
                            "### Children whose review rejected",
                            (
                                "Each landed on this branch with its findings unfixed; they "
                                "are this container's to fix:"
                            ),
                            *rejected,
                        ]
                    )
                )

            return "\n\n".join(out) + "\n"

        if v == RenderView.FULL:
            out = [frontmatter_text, f"# {node.title}", *superseded]
            for sec in sections:
                if sec.header:
                    out.append(f"{sec.header}\n\n{sec.content}")
                else:
                    out.append(sec.content)
            return "\n\n".join(out) + "\n"

        raise ValueError(f"Unknown view '{view}'. Supported views: summary, subagent, full")

    def render_recursive(
        self, node_id: str, view: RenderView | str = RenderView.FULL, _seen: set[str] | None = None
    ) -> str:
        """The node's own render, then each child's, depth-first, in `get_children`'s order.

        A spec recurses through its plans into their tasks; a task has no children and returns
        just its own render. `_seen` guards a relation cycle the schema does not otherwise forbid.
        """
        seen = _seen if _seen is not None else set()
        if node_id in seen:
            return f"<!-- {node_id}: already rendered above, relation cycle -->"
        seen.add(node_id)

        parts = [self.render(node_id, view)]
        for child_id in self.node_repo.get_children(node_id):
            parts.append(self.render_recursive(child_id, view, seen))
        return "\n\n---\n\n".join(parts)

    def _superseded(self, node: Node) -> list[str]:
        # Ahead of every section, so a superseded node's old deferral reads as history.
        if node.status != Status.SUPERSEDED:
            return []
        by = display_view(self.node_repo).superseded_by(node.id)
        return [f"Superseded by {by['id']} ({by['status']})"] if by else []

    def _rejected_children(self, node: Any) -> list[str]:
        # Read from each child's stored outcome on every render, never copied into a section.
        if node.kind not in CONTAINERS:
            return []
        lines: list[str] = []
        for child_id in self.node_repo.get_children(node.id):
            child = self.node_repo.get_node(child_id)
            if child is not None and child.outcome == Outcome.REJECT:
                lines.append(f"- `{child.id}` ({child.title}): `tm section get {child.id}:review`")
        return lines

    def _render_decision(self, node: Any, sections: list[Any]) -> str:
        data = read_decision(node)
        parts = [f"# Decision: {node.title}", f"Status: {node.status.value}"]
        for sec in sections:
            header = sec.header or f"## {sec.section_key.capitalize()}"
            parts.append(f"{header}\n\n{sec.content}")
        if data.options:
            opt_lines = ["## Options"]
            for opt in data.options:
                mark = " **(recommended)**" if opt.recommended else ""
                desc = f" -- {opt.description}" if opt.description else ""
                opt_lines.append(f"- `{opt.key}`: {opt.label}{mark}{desc}")
            parts.append("\n".join(opt_lines))
        if not data.allow_custom:
            parts.append("## Custom answers\n\nNot allowed; pick one of the options above.")
        if data.answer is not None:
            answer = data.answer
            lines = ["## Answer", f"Chosen: {answer.option or '(custom)'}"]
            if answer.text:
                lines.append(answer.text)
            if answer.rationale:
                lines.append(f"Rationale: {answer.rationale}")
            lines.append(f"Answered by {answer.answered_by} at {answer.answered_at.isoformat()}")
            parts.append("\n".join(lines))
        elif stored_status(node) == DecisionStatus.WITHDRAWN:
            lines = ["## Withdrawn"]
            if data.withdrawn_reason:
                lines.append(data.withdrawn_reason)
            if data.withdrawn_by and data.withdrawn_at:
                lines.append(f"Withdrawn by {data.withdrawn_by} at {data.withdrawn_at.isoformat()}")
            parts.append("\n\n".join(lines))
        return "\n\n".join(parts) + "\n"

    def _get_parent_ids(self, node_id: str) -> list[str]:
        with self.node_repo.db.get_state_connection() as conn:
            rows = conn.execute(
                "SELECT source_id FROM node_relations WHERE target_id = ? AND relation_type = ? ORDER BY rowid ASC",
                (node_id, RelationType.CONTAINS.value),
            ).fetchall()
            return [r[0] for r in rows]
