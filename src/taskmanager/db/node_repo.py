import json
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from taskmanager.core.enums import (
    NodeKind,
    NodeStatus,
    RelationType,
    TransferMode,
    VerificationType,
)
from taskmanager.core.models import (
    Condition,
    Node,
    NodeRelation,
    NodeSection,
    NodeVerification,
)
from taskmanager.core.status import ConditionStage, DecisionStatus, Status
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.utils import parse_db_datetime, to_db_timestamp

_NODE_COLUMNS = (
    "id, kind, title, status, priority, ordinal, target_repo, acceptable_models, "
    "frontmatter_json, claimed_from, review, fix, merge, outcome, verdict, fix_for, "
    "review_cycles, merge_attempts, step_failures, branch, requires, land_order, "
    "created_at, updated_at"
)


def _status(raw: str) -> NodeStatus | Status | DecisionStatus:
    # A value both vocabularies share reads as NodeStatus, so modules not yet on the new
    # vocabulary keep matching it; each name only the new vocabulary has reads as its own.
    for vocabulary in (NodeStatus, Status):
        try:
            return vocabulary(raw)
        except ValueError:
            continue
    return DecisionStatus(raw)


class NodeRepository:
    def __init__(self, db_mgr: DatabaseManager) -> None:
        self.db = db_mgr

    @contextmanager
    def transaction(self) -> Generator[None]:
        """A run of several writes below lands as one commit or none: `save_node`,
        `save_section`, `add_relation` and the rest keep committing on their own outside this
        scope, but inside it they share the one commit (or rollback) `spec_transaction()` does
        at the end."""
        with self.db.spec_transaction():
            yield

    def save_node(self, node: Node) -> None:
        with self.db.get_spec_connection() as conn:
            conn.execute(
                f"""
                INSERT INTO nodes ({_NODE_COLUMNS})
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    kind=excluded.kind,
                    title=excluded.title,
                    status=excluded.status,
                    priority=excluded.priority,
                    ordinal=excluded.ordinal,
                    target_repo=excluded.target_repo,
                    acceptable_models=excluded.acceptable_models,
                    frontmatter_json=excluded.frontmatter_json,
                    claimed_from=excluded.claimed_from,
                    review=excluded.review,
                    fix=excluded.fix,
                    merge=excluded.merge,
                    outcome=excluded.outcome,
                    verdict=excluded.verdict,
                    fix_for=excluded.fix_for,
                    review_cycles=excluded.review_cycles,
                    merge_attempts=excluded.merge_attempts,
                    step_failures=excluded.step_failures,
                    branch=excluded.branch,
                    requires=excluded.requires,
                    land_order=excluded.land_order,
                    updated_at=excluded.updated_at;
                """,
                (
                    node.id,
                    node.kind.value,
                    node.title,
                    node.status.value,
                    node.priority,
                    node.ordinal,
                    node.target_repo,
                    json.dumps(node.acceptable_models),
                    json.dumps(node.frontmatter),
                    node.claimed_from.value if node.claimed_from else None,
                    int(node.review),
                    int(node.fix),
                    node.merge.value,
                    node.outcome.value if node.outcome else None,
                    node.verdict,
                    node.fix_for.value if node.fix_for else None,
                    node.review_cycles,
                    node.merge_attempts,
                    node.step_failures,
                    node.branch,
                    json.dumps(node.requires),
                    json.dumps(node.land_order),
                    to_db_timestamp(node.created_at),
                    to_db_timestamp(node.updated_at),
                ),
            )
            cursor = conn.execute(
                """
                UPDATE nodes_fts
                SET title = ?, frontmatter_text = ?
                WHERE node_id = ?;
                """,
                (node.title, json.dumps(node.frontmatter), node.id),
            )
            if cursor.rowcount == 0:
                rowid_row = conn.execute(
                    "SELECT rowid FROM nodes WHERE id = ?", (node.id,)
                ).fetchone()
                rowid = rowid_row[0] if rowid_row else None
                content_row = conn.execute(
                    """
                    SELECT COALESCE(GROUP_CONCAT(content, ' '), '')
                    FROM (SELECT content FROM node_sections WHERE node_id = ? ORDER BY ordinal ASC)
                    """,
                    (node.id,),
                ).fetchone()
                content_text = content_row[0] if content_row else ""
                conn.execute(
                    """
                    INSERT INTO nodes_fts (rowid, node_id, title, frontmatter_text, content_text)
                    VALUES (?, ?, ?, ?, ?);
                    """,
                    (rowid, node.id, node.title, json.dumps(node.frontmatter), content_text),
                )
            self.db.spec_commit(conn)

    def get_node(self, node_id: str) -> Node | None:
        with self.db.get_spec_connection() as conn:
            row = conn.execute(
                f"SELECT {_NODE_COLUMNS} FROM nodes WHERE id = ?", (node_id,)
            ).fetchone()
            if not row:
                return None
            return self._row_to_node(row)

    def list_nodes(
        self, kind: NodeKind | None = None, status: NodeStatus | None = None
    ) -> list[Node]:
        query = f"SELECT {_NODE_COLUMNS} FROM nodes WHERE 1=1"
        params: list[str] = []
        if kind is not None:
            query += " AND kind = ?"
            params.append(kind.value if hasattr(kind, "value") else str(kind))
        if status is not None:
            query += " AND status = ?"
            params.append(status.value if hasattr(status, "value") else str(status))
        query += " ORDER BY ordinal ASC, priority DESC, id ASC"
        with self.db.get_spec_connection() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
            return [self._row_to_node(r) for r in rows]

    def save_section(self, section: NodeSection) -> None:
        with self.db.get_spec_connection() as conn:
            conn.execute(
                """
                INSERT INTO node_sections (node_id, section_key, ordinal, header, content)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(node_id, section_key) DO UPDATE SET
                    ordinal=excluded.ordinal,
                    header=excluded.header,
                    content=excluded.content;
                """,
                (
                    section.node_id,
                    section.section_key,
                    section.ordinal,
                    section.header,
                    section.content,
                ),
            )
            cursor = conn.execute(
                """
                UPDATE nodes_fts SET content_text = (
                    SELECT COALESCE(GROUP_CONCAT(content, ' '), '')
                    FROM (SELECT content FROM node_sections WHERE node_id = ? ORDER BY ordinal ASC)
                ) WHERE node_id = ?;
                """,
                (section.node_id, section.node_id),
            )
            if cursor.rowcount == 0:
                node_row = conn.execute(
                    "SELECT rowid, title, frontmatter_json FROM nodes WHERE id = ?",
                    (section.node_id,),
                ).fetchone()
                if node_row:
                    rowid, title, fm = node_row
                    content_row = conn.execute(
                        """
                        SELECT COALESCE(GROUP_CONCAT(content, ' '), '')
                        FROM (SELECT content FROM node_sections WHERE node_id = ? ORDER BY ordinal ASC)
                        """,
                        (section.node_id,),
                    ).fetchone()
                    content_text = content_row[0] if content_row else ""
                    conn.execute(
                        """
                        INSERT INTO nodes_fts (rowid, node_id, title, frontmatter_text, content_text)
                        VALUES (?, ?, ?, ?, ?);
                        """,
                        (rowid, section.node_id, title, fm, content_text),
                    )
            self.db.spec_commit(conn)

    def get_section(self, node_id: str, section_key: str) -> NodeSection | None:
        with self.db.get_spec_connection() as conn:
            row = conn.execute(
                """
                SELECT node_id, section_key, ordinal, header, content
                FROM node_sections
                WHERE node_id = ? AND section_key = ?
                """,
                (node_id, section_key),
            ).fetchone()
            if not row:
                return None
            return NodeSection(
                node_id=row[0],
                section_key=row[1],
                ordinal=row[2],
                header=row[3],
                content=row[4],
            )

    def remove_section(self, node_id: str, section_key: str) -> bool:
        with self.db.get_spec_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM node_sections WHERE node_id = ? AND section_key = ?",
                (node_id, section_key),
            )
            if cursor.rowcount:
                conn.execute(
                    """
                    UPDATE nodes_fts SET content_text = (
                        SELECT COALESCE(GROUP_CONCAT(content, ' '), '')
                        FROM (SELECT content FROM node_sections WHERE node_id = ? ORDER BY ordinal ASC)
                    ) WHERE node_id = ?;
                    """,
                    (node_id, node_id),
                )
            self.db.spec_commit(conn)
            return cursor.rowcount > 0

    def get_all_sections(self, node_id: str) -> list[NodeSection]:
        with self.db.get_spec_connection() as conn:
            rows = conn.execute(
                """
                SELECT node_id, section_key, ordinal, header, content
                FROM node_sections
                WHERE node_id = ?
                ORDER BY ordinal ASC
                """,
                (node_id,),
            ).fetchall()
            return [
                NodeSection(
                    node_id=r[0],
                    section_key=r[1],
                    ordinal=r[2],
                    header=r[3],
                    content=r[4],
                )
                for r in rows
            ]

    def add_relation(self, relation: NodeRelation) -> None:
        rel_type = (
            relation.relation_type.value
            if hasattr(relation.relation_type, "value")
            else str(relation.relation_type)
        )
        with self.db.get_spec_connection() as conn:
            conn.execute(
                """
                INSERT INTO node_relations (source_id, target_id, relation_type, metadata_json)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(source_id, target_id, relation_type) DO UPDATE SET
                    metadata_json=excluded.metadata_json;
                """,
                (
                    relation.source_id,
                    relation.target_id,
                    rel_type,
                    json.dumps(relation.metadata),
                ),
            )
            self.db.spec_commit(conn)

    def get_children(self, parent_id: str) -> list[str]:
        with self.db.get_spec_connection() as conn:
            rows = conn.execute(
                """
                SELECT r.target_id
                FROM node_relations r
                JOIN nodes n ON r.target_id = n.id
                WHERE r.source_id = ? AND r.relation_type = ?
                ORDER BY n.ordinal ASC, n.priority DESC, n.id ASC
                """,
                (parent_id, RelationType.CONTAINS.value),
            ).fetchall()
            return [r[0] for r in rows]

    def get_parent_ids(self, node_id: str) -> list[str]:
        with self.db.get_spec_connection() as conn:
            rows = conn.execute(
                """
                SELECT r.source_id
                FROM node_relations r
                WHERE r.target_id = ? AND r.relation_type = ?
                ORDER BY r.rowid ASC
                """,
                (node_id, RelationType.CONTAINS.value),
            ).fetchall()
            return [r[0] for r in rows]

    def get_ancestor_of_kind(self, node_id: str, kind: NodeKind) -> str | None:
        """The nearest CONTAINS ancestor of the given kind, walking through any number of
        intermediate parents -- `add_plan` never checks that its `--spec` argument is a spec, so
        a plan can nest under another plan and the spec is further up than the direct parent.
        None once the chain runs out or (a corrupt CONTAINS cycle) repeats a node."""
        visited: set[str] = set()
        current = node_id
        while True:
            parents = self.get_parent_ids(current)
            if not parents:
                return None
            parent = parents[0]
            if parent in visited:
                return None
            visited.add(parent)
            node = self.get_node(parent)
            if node is not None and node.kind == kind:
                return parent
            current = parent

    def remove_relation(self, source_id: str, target_id: str, relation_type: RelationType) -> None:
        with self.db.get_spec_connection() as conn:
            conn.execute(
                "DELETE FROM node_relations WHERE source_id = ? AND target_id = ? "
                "AND relation_type = ?",
                (source_id, target_id, relation_type.value),
            )
            self.db.spec_commit(conn)

    def get_dependencies(self, node_id: str) -> list[str]:
        with self.db.get_spec_connection() as conn:
            rows = conn.execute(
                "SELECT target_id FROM node_relations WHERE source_id = ? AND relation_type = ? ORDER BY rowid ASC",
                (node_id, RelationType.DEPENDS_ON.value),
            ).fetchall()
            return [r[0] for r in rows]

    def get_dependency_edges(self, node_id: str) -> list[tuple[str, NodeStatus]]:
        """Each dependency with the status its target must reach to satisfy it — `COMPLETED`
        for a bare edge (today's behavior, unchanged), or the edge's own `gate` metadata."""
        with self.db.get_spec_connection() as conn:
            rows = conn.execute(
                "SELECT target_id, metadata_json FROM node_relations "
                "WHERE source_id = ? AND relation_type = ? ORDER BY rowid ASC",
                (node_id, RelationType.DEPENDS_ON.value),
            ).fetchall()
            edges: list[tuple[str, NodeStatus]] = []
            for target_id, metadata_json in rows:
                metadata = json.loads(metadata_json) if metadata_json else {}
                gate_raw = metadata.get("gate")
                gate = NodeStatus(gate_raw) if gate_raw else NodeStatus.COMPLETED
                edges.append((target_id, gate))
            return edges

    def get_blocked_by(self, node_id: str) -> list[str]:
        with self.db.get_spec_connection() as conn:
            rows = conn.execute(
                "SELECT source_id FROM node_relations WHERE target_id = ? AND relation_type = ? ORDER BY rowid ASC",
                (node_id, RelationType.DEPENDS_ON.value),
            ).fetchall()
            return [r[0] for r in rows]

    def transfer_blocks(
        self,
        old_id: str,
        new_id: str,
        transfer_mode: TransferMode | str,
        custom_ids: list[str] | None = None,
    ) -> None:
        mode = (
            TransferMode(transfer_mode.lower()) if isinstance(transfer_mode, str) else transfer_mode
        )
        dep_val = RelationType.DEPENDS_ON.value
        with self.db.get_spec_connection() as conn:
            if mode == TransferMode.ALL:
                conn.execute(
                    "UPDATE OR IGNORE node_relations SET target_id = ? WHERE target_id = ? AND relation_type = ?",
                    (new_id, old_id, dep_val),
                )
                conn.execute(
                    "DELETE FROM node_relations WHERE target_id = ? AND relation_type = ?",
                    (old_id, dep_val),
                )
            elif mode == TransferMode.NONE:
                pass
            elif mode == TransferMode.CUSTOM and custom_ids:
                placeholders = ",".join("?" for _ in custom_ids)
                conn.execute(
                    f"UPDATE OR IGNORE node_relations SET target_id = ? WHERE target_id = ? AND relation_type = ? AND source_id IN ({placeholders})",
                    (new_id, old_id, dep_val, *custom_ids),
                )
                conn.execute(
                    f"DELETE FROM node_relations WHERE target_id = ? AND relation_type = ? AND source_id IN ({placeholders})",
                    (old_id, dep_val, *custom_ids),
                )
            self.db.spec_commit(conn)

    def add_verification(self, ver: NodeVerification) -> None:
        vtype = (
            ver.verification_type.value
            if hasattr(ver.verification_type, "value")
            else str(ver.verification_type)
        )
        with self.db.get_spec_connection() as conn:
            if ver.id is not None:
                conn.execute(
                    """
                    INSERT INTO node_verifications (
                        id, node_id, verification_type, target_path,
                        expected_pattern, codegraph_query_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        node_id=excluded.node_id,
                        verification_type=excluded.verification_type,
                        target_path=excluded.target_path,
                        expected_pattern=excluded.expected_pattern,
                        codegraph_query_json=excluded.codegraph_query_json;
                    """,
                    (
                        ver.id,
                        ver.node_id,
                        vtype,
                        ver.target_path,
                        ver.expected_pattern,
                        ver.codegraph_query_json,
                    ),
                )
            else:
                cursor = conn.execute(
                    """
                    INSERT INTO node_verifications (
                        node_id, verification_type, target_path,
                        expected_pattern, codegraph_query_json
                    )
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        ver.node_id,
                        vtype,
                        ver.target_path,
                        ver.expected_pattern,
                        ver.codegraph_query_json,
                    ),
                )
                ver.id = cursor.lastrowid if cursor.rowcount else None
            self.db.spec_commit(conn)

    def declared_files(self, task_id: str) -> list[str]:
        """The paths a task claims, which is what its lease locks and what `next` filters on.

        A `test_command` or `codegraph_query` puts a label or a query in `target_path`, not a
        path, so only the path-bearing verification types count.
        """
        pathy = {
            VerificationType.FILE_EXISTS,
            VerificationType.FILE_ABSENT,
            VerificationType.SYMBOL_SIGNATURE,
            VerificationType.AST_EXPORT,
        }
        files = [
            v.target_path
            for v in self.get_verifications(task_id)
            if v.verification_type in pathy and v.target_path
        ]
        node = self.get_node(task_id)
        declared = (
            (node.frontmatter.get("declared_files") or node.frontmatter.get("files"))
            if node
            else None
        )
        if isinstance(declared, list):
            files.extend(str(f) for f in declared)
        elif isinstance(declared, str):
            files.append(declared)
        return list(dict.fromkeys(files))

    def remove_verification(self, node_id: str, verification_id: int) -> bool:
        with self.db.get_spec_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM node_verifications WHERE node_id = ? AND id = ?",
                (node_id, verification_id),
            )
            self.db.spec_commit(conn)
            return cursor.rowcount > 0

    def clear_verifications(self, node_id: str) -> None:
        with self.db.get_spec_connection() as conn:
            conn.execute("DELETE FROM node_verifications WHERE node_id = ?", (node_id,))
            self.db.spec_commit(conn)

    def get_verifications(self, node_id: str) -> list[NodeVerification]:
        with self.db.get_spec_connection() as conn:
            rows = conn.execute(
                """
                SELECT id, node_id, verification_type, target_path,
                       expected_pattern, codegraph_query_json
                FROM node_verifications
                WHERE node_id = ?
                ORDER BY id ASC
                """,
                (node_id,),
            ).fetchall()
            return [
                NodeVerification(
                    id=r[0],
                    node_id=r[1],
                    verification_type=VerificationType(r[2]),
                    target_path=r[3],
                    expected_pattern=r[4],
                    codegraph_query_json=r[5],
                )
                for r in rows
            ]

    @staticmethod
    def _row_to_node(row: tuple[Any, ...]) -> Node:
        return Node.model_validate(
            {
                "id": row[0],
                "kind": NodeKind(row[1]),
                "title": row[2],
                "status": _status(row[3]),
                "priority": row[4],
                "ordinal": row[5],
                "target_repo": row[6],
                "acceptable_models": json.loads(row[7]),
                "frontmatter": json.loads(row[8]),
                "claimed_from": row[9],
                "review": bool(row[10]),
                "fix": bool(row[11]),
                "merge": row[12],
                "outcome": row[13],
                "verdict": row[14],
                "fix_for": row[15],
                "review_cycles": row[16],
                "merge_attempts": row[17],
                "step_failures": row[18],
                "branch": row[19],
                "requires": json.loads(row[20]),
                "land_order": json.loads(row[21]),
                "created_at": parse_db_datetime(row[22]),
                "updated_at": parse_db_datetime(row[23]),
            }
        )

    def get_conditions(self, node_id: str) -> list[Condition]:
        with self.db.get_state_connection() as conn:
            rows = conn.execute(
                "SELECT node_id, idx, needs, command, stage FROM node_conditions "
                "WHERE node_id = ? ORDER BY idx ASC",
                (node_id,),
            ).fetchall()
        return [
            Condition(node_id=r[0], idx=r[1], needs=r[2], command=r[3], stage=ConditionStage(r[4]))
            for r in rows
        ]

    def add_condition(self, condition: Condition) -> Condition:
        with self.db.get_state_connection() as conn:
            (idx,) = conn.execute(
                "SELECT COALESCE(MAX(idx), 0) + 1 FROM node_conditions WHERE node_id = ?",
                (condition.node_id,),
            ).fetchone()
            conn.execute(
                "INSERT INTO node_conditions (node_id, idx, needs, command, stage) "
                "VALUES (?, ?, ?, ?, ?)",
                (condition.node_id, idx, condition.needs, condition.command, condition.stage.value),
            )
            self.db.spec_commit(conn)
        return condition.model_copy(update={"idx": idx})

    def remove_condition(self, node_id: str, idx: int) -> bool:
        with self.db.get_state_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM node_conditions WHERE node_id = ? AND idx = ?", (node_id, idx)
            )
            self.db.spec_commit(conn)
            return cursor.rowcount > 0

    def update_ordinal(self, node_id: str, ordinal: int) -> None:
        with self.db.get_spec_connection() as conn:
            conn.execute(
                "UPDATE nodes SET ordinal = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (ordinal, node_id),
            )
            self.db.spec_commit(conn)
