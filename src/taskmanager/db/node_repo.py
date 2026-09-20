import json
from typing import Any

from taskmanager.core.enums import (
    NodeKind,
    NodeStatus,
    RelationType,
    TransferMode,
    VerificationType,
)
from taskmanager.core.models import (
    Node,
    NodeRelation,
    NodeSection,
    NodeVerification,
)
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.utils import parse_db_datetime, to_db_timestamp


class NodeRepository:
    def __init__(self, db_mgr: DatabaseManager) -> None:
        self.db = db_mgr

    def save_node(self, node: Node) -> None:
        with self.db.get_spec_connection() as conn:
            conn.execute(
                """
                INSERT INTO nodes (
                    id, kind, title, status, priority, ordinal, target_repo,
                    acceptable_models, frontmatter_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    kind=excluded.kind,
                    title=excluded.title,
                    status=excluded.status,
                    priority=excluded.priority,
                    ordinal=excluded.ordinal,
                    target_repo=excluded.target_repo,
                    acceptable_models=excluded.acceptable_models,
                    frontmatter_json=excluded.frontmatter_json,
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
            conn.commit()

    def get_node(self, node_id: str) -> Node | None:
        with self.db.get_spec_connection() as conn:
            row = conn.execute(
                """
                SELECT id, kind, title, status, priority, ordinal, target_repo,
                       acceptable_models, frontmatter_json, created_at, updated_at
                FROM nodes WHERE id = ?
                """,
                (node_id,),
            ).fetchone()
            if not row:
                return None
            return self._row_to_node(row)

    def list_nodes(
        self, kind: NodeKind | None = None, status: NodeStatus | None = None
    ) -> list[Node]:
        query = (
            "SELECT id, kind, title, status, priority, ordinal, target_repo, "
            "acceptable_models, frontmatter_json, created_at, updated_at "
            "FROM nodes WHERE 1=1"
        )
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
            conn.commit()

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
            conn.commit()

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

    def get_dependencies(self, node_id: str) -> list[str]:
        with self.db.get_spec_connection() as conn:
            rows = conn.execute(
                "SELECT target_id FROM node_relations WHERE source_id = ? AND relation_type = ? ORDER BY rowid ASC",
                (node_id, RelationType.DEPENDS_ON.value),
            ).fetchall()
            return [r[0] for r in rows]

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
            conn.commit()

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
                    """,
                    (
                        ver.node_id,
                        vtype,
                        ver.target_path,
                        ver.expected_pattern,
                        ver.codegraph_query_json,
                    ),
                )
                ver.id = cursor.lastrowid
            conn.commit()

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
        return Node(
            id=row[0],
            kind=NodeKind(row[1]),
            title=row[2],
            status=NodeStatus(row[3]),
            priority=row[4],
            ordinal=row[5],
            target_repo=row[6],
            acceptable_models=json.loads(row[7]),
            frontmatter=json.loads(row[8]),
            created_at=parse_db_datetime(row[9]),
            updated_at=parse_db_datetime(row[10]),
        )

    def update_ordinal(self, node_id: str, ordinal: int) -> None:
        with self.db.get_spec_connection() as conn:
            conn.execute(
                "UPDATE nodes SET ordinal = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (ordinal, node_id),
            )
            conn.commit()
