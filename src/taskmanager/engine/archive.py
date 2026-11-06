"""Which finished specs the web leaves out of its counts, and the move of a finished spec with its
whole subtree out of the hot databases into `archive.db`.

The web reads the ledger and the clock on every snapshot rather than storing anything, so a spec
crosses into its archive without any write; `archive_spec` is the move that does write."""

import os
import sqlite3
from collections.abc import Callable, Iterable, Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, NamedTuple

from taskmanager.core.enums import NodeKind
from taskmanager.core.models import Node
from taskmanager.core.status import DecisionStatus, Status
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.ledger_repo import LedgerRepository
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.schema import (
    LEDGER_SCHEMA_SQL,
    NODE_REV_TRIGGERS_SQL,
    STATE_SCHEMA_SQL,
    STATE_SCHEMA_VERSION,
)
from taskmanager.db.utils import lease_alive, parse_db_datetime
from taskmanager.engine.claims import LIVE_JOBS
from taskmanager.engine.config import ConfigStore
from taskmanager.engine.operations import OperationError

FINISHED: Final = frozenset({Status.COMPLETED, Status.ABANDONED, Status.SUPERSEDED})
# A table is copied after the one it references. `node_relations` follows, with the rows whose two
# ends are in the subtree; a row with one end outside it waits in the archive's `external_relations`,
# since a foreign key keeps `node_relations` to nodes the archive holds.
_SUBTREE_TABLES: Final = (
    ("nodes", "id"),
    ("node_sections", "node_id"),
    ("node_verifications", "node_id"),
    ("node_conditions", "node_id"),
    ("node_findings", "node_id"),
    ("node_checks", "node_id"),
    ("node_notes", "node_id"),
    ("jobs", "node_id"),
)
_AUTO_ID_TABLES: Final = frozenset({"node_verifications"})
_IN_SUBTREE: Final = "(SELECT id FROM temp.subtree)"
_CROSSING: Final = f"(source_id IN {_IN_SUBTREE}) <> (target_id IN {_IN_SUBTREE})"
_BOTH_HOT: Final = (
    "source_id IN (SELECT id FROM main.nodes) AND target_id IN (SELECT id FROM main.nodes)"
)


class ArchiveSchedule(NamedTuple):
    # The finished specs past their keep time.
    due: list[str]
    # When the next finished spec still in view comes due.
    next_boundary: datetime | None


def completion_times(
    nodes: Iterable[Node],
    ledger: LedgerRepository,
    statuses: frozenset[Status] = frozenset({Status.COMPLETED}),
) -> dict[str, datetime]:
    """When each node in one of `statuses` got there: the ledger's last move to one of them, or,
    for a node the ledger holds no such move for (finished before the ledger recorded
    transitions, or restored from an export that carried no completion time), its row's last
    update."""
    completed = {node.id: node for node in nodes if node.status in statuses}
    recorded = ledger.completed_at(completed, [status.value for status in statuses])
    return {
        node_id: recorded.get(node_id) or parse_db_datetime(node.updated_at)
        for node_id, node in completed.items()
    }


def archive_schedule(
    specs: Iterable[Node], ledger: LedgerRepository, now: datetime, hours: int
) -> ArchiveSchedule:
    """Which of `specs` are finished for longer than `hours`, and when the rest come due."""
    if hours <= 0:
        return ArchiveSchedule([], None)
    keep_for = timedelta(hours=hours)
    due: list[str] = []
    boundaries: list[datetime] = []
    for spec_id, finished_at in sorted(completion_times(specs, ledger, FINISHED).items()):
        if finished_at + keep_for <= now:
            due.append(spec_id)
        else:
            boundaries.append(finished_at + keep_for)
    return ArchiveSchedule(due, min(boundaries, default=None))


_EXTERNAL_RELATIONS_SQL: Final = """
CREATE TABLE IF NOT EXISTS external_relations (
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (source_id, target_id, relation_type)
);
"""


ARCHIVE_FILE: Final = "archive.db"


def archive_path(root: Path) -> Path:
    return root / ".taskmanager" / ARCHIVE_FILE


def _ensure_archive(root: Path) -> None:
    path = archive_path(root)
    if path.exists():
        return
    # Built beside the target and renamed, so a reader never opens a half-made file.
    staged = path.with_name(f"{path.name}.tmp")
    staged.unlink(missing_ok=True)
    conn = sqlite3.connect(staged)
    try:
        conn.executescript(
            STATE_SCHEMA_SQL + NODE_REV_TRIGGERS_SQL + LEDGER_SCHEMA_SQL + _EXTERNAL_RELATIONS_SQL
        )
        conn.execute(f"PRAGMA user_version = {STATE_SCHEMA_VERSION}")
        conn.commit()
    finally:
        conn.close()
    os.replace(staged, path)


def archived_repo(root: Path) -> NodeRepository:
    """The archived nodes, read-only. ponytail: `tm db migrate` leaves archive.db at the schema it
    was made with; migrate it there when state.db's schema next changes."""
    _ensure_archive(root)
    db = DatabaseManager(root / ".taskmanager")
    db.state_db = db.ledger_db = archive_path(root)
    with db.get_state_connection() as conn:
        conn.execute("PRAGMA query_only = ON")
    return NodeRepository(db)


@contextmanager
def _subtree(db: DatabaseManager, spec_id: str, source: str) -> Iterator[sqlite3.Connection]:
    """The state connection with archive.db and audit.db attached and `temp.subtree` holding the
    ids of `spec_id`'s subtree as `source` ("main" or "archive") stores them: the spec, everything
    it contains, and each decision that blocks only nodes among them."""
    with db.get_state_connection() as conn:
        for name, path in (("archive", archive_path(db.dir.parent)), ("audit", db.ledger_db)):
            conn.execute(f"ATTACH DATABASE ? AS {name}", (str(path),))
        try:
            conn.execute("CREATE TEMP TABLE subtree (id TEXT PRIMARY KEY)")
            conn.execute(
                f"""
                WITH RECURSIVE tree(id) AS (
                    SELECT id FROM {source}.nodes WHERE id = ?
                    UNION
                    SELECT r.target_id FROM {source}.node_relations r
                    JOIN tree ON r.source_id = tree.id WHERE r.relation_type = 'contains'
                )
                INSERT INTO temp.subtree SELECT id FROM tree
                """,
                (spec_id,),
            )
            blocked_only_here = conn.execute(
                f"""
                SELECT DISTINCT r.target_id FROM {source}.node_relations r
                JOIN {source}.nodes d ON d.id = r.target_id AND d.kind = 'decision'
                WHERE r.relation_type = 'depends_on' AND r.source_id IN {_IN_SUBTREE}
                AND NOT EXISTS (
                    SELECT 1 FROM {source}.node_relations o
                    WHERE o.target_id = r.target_id AND o.relation_type = 'depends_on'
                    AND o.source_id NOT IN {_IN_SUBTREE}
                )
                """
            ).fetchall()
            conn.executemany("INSERT OR IGNORE INTO temp.subtree VALUES (?)", blocked_only_here)
            conn.commit()
            yield conn
        finally:
            conn.execute("DROP TABLE IF EXISTS temp.subtree")
            conn.execute("DETACH DATABASE archive")
            conn.execute("DETACH DATABASE audit")


def _stored(path: Path, spec_id: str) -> tuple[str, str] | None:
    """The kind and creation time of `spec_id` in the database at `path`, None when absent."""
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
        row = conn.execute("SELECT kind, created_at FROM nodes WHERE id = ?", (spec_id,)).fetchone()
    return None if row is None else (row[0], row[1])


def _blockers(conn: sqlite3.Connection) -> list[str]:
    blockers = [
        f"{node_id} is {status}, not finished"
        for node_id, status in conn.execute(
            f"""
            SELECT id, status FROM main.nodes WHERE id IN {_IN_SUBTREE}
            AND IIF(kind = 'decision', status = ?, status NOT IN (?, ?, ?)) ORDER BY id
            """,
            (DecisionStatus.OPEN.value, *sorted(s.value for s in FINISHED)),
        )
    ]
    now = datetime.now(tz=UTC)
    blockers += [
        f"{node_id} holds a live lease"
        for node_id, ttl, beat in conn.execute(
            f"SELECT task_id, ttl_seconds, last_heartbeat FROM main.leases "
            f"WHERE task_id IN {_IN_SUBTREE} ORDER BY task_id"
        )
        if lease_alive(ttl, parse_db_datetime(beat), now)
    ]
    live = ", ".join("?" for _ in LIVE_JOBS)
    blockers += [
        f"{node_id} has a {state} job"
        for node_id, state in conn.execute(
            f"SELECT node_id, state FROM main.jobs WHERE node_id IN {_IN_SUBTREE} "
            f"AND state IN ({live}) ORDER BY node_id",
            sorted(s.value for s in LIVE_JOBS),
        )
    ]
    # An edge onto a COMPLETED node reads as satisfied once the node is gone.
    blockers += [
        f"{source} depends on {target}, which is {status}"
        for source, target, status in conn.execute(
            f"""
            SELECT r.source_id, r.target_id, n.status FROM main.node_relations r
            JOIN main.nodes n ON n.id = r.target_id
            WHERE r.relation_type = 'depends_on' AND r.target_id IN {_IN_SUBTREE}
            AND r.source_id NOT IN {_IN_SUBTREE} AND n.status <> ?
            ORDER BY r.source_id, r.target_id
            """,
            (Status.COMPLETED.value,),
        )
    ]
    blockers += [
        f"{source} supersedes {target}"
        for source, target in conn.execute(
            f"""
            SELECT source_id, target_id FROM main.node_relations
            WHERE relation_type = 'supersedes' AND target_id IN {_IN_SUBTREE}
            AND source_id NOT IN {_IN_SUBTREE} ORDER BY source_id, target_id
            """
        )
    ]
    # A decision with dependents outside the subtree is not in it, so these are its edges out.
    blockers += [
        f"decision {decision} is open and also blocks nodes outside the spec"
        for (decision,) in conn.execute(
            f"""
            SELECT DISTINCT r.target_id FROM main.node_relations r
            JOIN main.nodes d ON d.id = r.target_id
            WHERE r.relation_type = 'depends_on' AND r.source_id IN {_IN_SUBTREE}
            AND r.target_id NOT IN {_IN_SUBTREE} AND d.kind = 'decision' AND d.status = ?
            ORDER BY r.target_id
            """,
            (DecisionStatus.OPEN.value,),
        )
    ]
    return blockers


def archived_owner(root: Path, node_id: str) -> str | None:
    """The top node of the archived tree that holds `node_id` (the spec it belongs to), None when
    archive.db does not hold it."""
    path = archive_path(root)
    if not path.exists() or _stored(path, node_id) is None:
        return None
    seen = {node_id}
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as conn:
        while parent := conn.execute(
            "SELECT source_id FROM node_relations WHERE target_id = ? "
            "AND relation_type IN ('contains', 'depends_on') ORDER BY relation_type LIMIT 1",
            (node_id,),
        ).fetchone():
            if parent[0] in seen:
                break
            node_id = parent[0]
            seen.add(node_id)
    return node_id


def archive_blockers(root: Path, spec_id: str) -> list[str]:
    """Why `spec_id` cannot be archived now, one line each; empty when it can."""
    _ensure_archive(root)
    db = DatabaseManager(root / ".taskmanager")
    if _stored(db.state_db, spec_id) is None:
        return [f"spec '{spec_id}' is not in the hot databases"]
    with _subtree(db, spec_id, "main") as conn:
        return _blockers(conn)


def _replace_into(conn: sqlite3.Connection, src: str, dst: str, table: str, where: str) -> None:
    columns = ", ".join(row[1] for row in conn.execute(f"PRAGMA {dst}.table_info({table})"))
    conn.execute(
        f"INSERT OR REPLACE INTO {dst}.{table} ({columns}) "
        f"SELECT {columns} FROM {src}.{table} WHERE {where}"
    )


def _append_into(conn: sqlite3.Connection, src: str, dst: str, table: str, where: str) -> None:
    """Rows of a table keyed by AUTOINCREMENT, which numbers each store on its own: the id stays
    behind, and a row whose other columns the destination holds already is not copied twice."""
    columns = [row[1] for row in conn.execute(f"PRAGMA {dst}.table_info({table})")]
    columns.remove("id")
    held = " AND ".join(f"d.{c} IS s.{c}" for c in columns)
    names = ", ".join(columns)
    conn.execute(
        f"INSERT INTO {dst}.{table} ({names}) "
        f"SELECT {names} FROM {src}.{table} s WHERE ({where}) "
        f"AND NOT EXISTS (SELECT 1 FROM {dst}.{table} d WHERE {held}) ORDER BY s.id"
    )


def _copy(conn: sqlite3.Connection, src: tuple[str, str], dst: tuple[str, str]) -> None:
    """The subtree's rows from `src` to `dst`, each a (state, ledger) pair of schema names. A
    replace keyed by the rows' own ids, so running it again leaves each row once."""
    for table, key in _SUBTREE_TABLES:
        copy = _append_into if table in _AUTO_ID_TABLES else _replace_into
        copy(conn, src[0], dst[0], table, f"{key} IN {_IN_SUBTREE}")
    ends = f"source_id IN {_IN_SUBTREE} AND target_id IN {_IN_SUBTREE}"
    _replace_into(conn, src[0], dst[0], "node_relations", ends)
    if dst[0] == "archive":
        conn.execute(
            f"INSERT OR REPLACE INTO archive.external_relations "
            f"SELECT source_id, target_id, relation_type, metadata_json "
            f"FROM main.node_relations WHERE {_CROSSING}"
        )
    else:
        # Only an edge whose other end is hot again; the rest wait for that end to come back.
        columns = "source_id, target_id, relation_type, metadata_json"
        conn.execute(
            f"INSERT OR REPLACE INTO main.node_relations ({columns}) "
            f"SELECT {columns} FROM archive.external_relations WHERE {_CROSSING} AND {_BOTH_HOT}"
        )
    _append_into(conn, src[1], dst[1], "ledger_events", f"target_id IN {_IN_SUBTREE}")


def _drop_state(conn: sqlite3.Connection, schema: str) -> None:
    if schema == "main":
        for table, key in (("nodes_fts", "node_id"), ("index_state", "node_id")):
            conn.execute(f"DELETE FROM main.{table} WHERE {key} IN {_IN_SUBTREE}")
        has_vectors = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'vec_nodes'")
        if has_vectors.fetchone():
            conn.execute(f"DELETE FROM main.vec_nodes WHERE node_id IN {_IN_SUBTREE}")
        # A lapsed lease holds nothing, and its file locks go with it.
        conn.execute(f"DELETE FROM main.leases WHERE task_id IN {_IN_SUBTREE}")
    else:
        conn.execute(f"DELETE FROM archive.external_relations WHERE {_CROSSING} AND {_BOTH_HOT}")
    # Every table below `nodes` cascades from it.
    conn.execute(f"DELETE FROM {schema}.nodes WHERE id IN {_IN_SUBTREE}")


def _reindex(conn: sqlite3.Connection) -> None:
    """Full-text rows for the restored nodes, as `save_node` keeps them; their embeddings come
    back with the next index run."""
    conn.execute(f"DELETE FROM main.nodes_fts WHERE node_id IN {_IN_SUBTREE}")
    conn.execute(
        f"""
        INSERT INTO main.nodes_fts (rowid, node_id, title, frontmatter_text, content_text)
        SELECT n.rowid, n.id, n.title, n.frontmatter_json, COALESCE((
            SELECT GROUP_CONCAT(content, ' ') FROM (
                SELECT content FROM main.node_sections WHERE node_id = n.id ORDER BY ordinal
            )
        ), '')
        FROM main.nodes n WHERE n.id IN {_IN_SUBTREE}
        """
    )


def _move(
    db: DatabaseManager,
    conn: sqlite3.Connection,
    spec_id: str,
    src: tuple[str, str],
    dst: tuple[str, str],
) -> None:
    """Copy, then delete the source's ledger rows, then its state rows. Each step commits alone
    and the spec stays in the source until the last one, so a run that stops anywhere is finished
    by running the move again."""
    held = conn.execute(f"SELECT created_at FROM {dst[0]}.nodes WHERE id = ?", (spec_id,))
    there = held.fetchone()
    here = conn.execute(f"SELECT created_at FROM {src[0]}.nodes WHERE id = ?", (spec_id,))
    if there is not None and there != here.fetchone():
        raise OperationError(
            f"'{spec_id}' names a different spec in the destination: nothing was moved", 409
        )
    with db.spec_transaction():
        _copy(conn, src, dst)
        if dst[0] == "main":
            _reindex(conn)
    with db.spec_transaction():
        conn.execute(f"DELETE FROM {src[1]}.ledger_events WHERE target_id IN {_IN_SUBTREE}")
    with db.spec_transaction():
        _drop_state(conn, src[0])


_HOT: Final = ("main", "audit")
_COLD: Final = ("archive", "archive")


def copy_to_archive(root: Path, spec_id: str) -> None:
    """The first half of `archive_spec`: the subtree's rows copied, nothing deleted."""
    _ensure_archive(root)
    db = DatabaseManager(root / ".taskmanager")
    with _subtree(db, spec_id, "main") as conn, db.spec_transaction():
        _copy(conn, _HOT, _COLD)


def archive_spec(root: Path, spec_id: str) -> None:
    _ensure_archive(root)
    db = DatabaseManager(root / ".taskmanager")
    hot = _stored(db.state_db, spec_id)
    if hot is None:
        if _stored(archive_path(root), spec_id) is None:
            raise OperationError(f"spec '{spec_id}' not found", 404)
        return
    if hot[0] != NodeKind.SPEC.value:
        raise OperationError(f"'{spec_id}' is a {hot[0]}: only a spec is archived", 400)
    with _subtree(db, spec_id, "main") as conn:
        if blockers := _blockers(conn):
            raise OperationError(f"'{spec_id}' cannot be archived: {'; '.join(blockers)}", 409)
        _move(db, conn, spec_id, _HOT, _COLD)


def unarchive_spec(root: Path, spec_id: str) -> None:
    _ensure_archive(root)
    db = DatabaseManager(root / ".taskmanager")
    if _stored(archive_path(root), spec_id) is None:
        if _stored(db.state_db, spec_id) is None:
            raise OperationError(f"spec '{spec_id}' not found", 404)
        return
    with _subtree(db, spec_id, "archive") as conn:
        _move(db, conn, spec_id, _COLD, _HOT)


def refuse_clashing_archive(root: Path, incoming: Path) -> None:
    """Refuses before any write when a node in the archive at `incoming` shares its id with a
    different node here, in the hot databases or the archive."""
    _ensure_archive(root)
    db = DatabaseManager(root / ".taskmanager")
    with closing(sqlite3.connect(db.state_db)) as conn:
        conn.execute("ATTACH DATABASE ? AS archive", (str(archive_path(root)),))
        conn.execute("ATTACH DATABASE ? AS incoming", (str(incoming),))
        for store in ("main", "archive"):
            clash = conn.execute(
                f"SELECT i.id FROM incoming.nodes i JOIN {store}.nodes d ON d.id = i.id "
                "WHERE d.kind <> i.kind OR d.created_at <> i.created_at ORDER BY i.id LIMIT 1"
            ).fetchone()
            if clash is not None:
                raise OperationError(
                    f"'{clash[0]}' names a different node in the destination: nothing was restored",
                    409,
                )


def merge_archive(root: Path, incoming: Path) -> None:
    """Adds every spec of the archive at `incoming` to this one; what it holds already stays."""
    _ensure_archive(root)
    with closing(sqlite3.connect(archive_path(root))) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("ATTACH DATABASE ? AS incoming", (str(incoming),))
        conn.execute("CREATE TEMP TABLE subtree AS SELECT id FROM incoming.nodes")
        with conn:
            for table, key in _SUBTREE_TABLES:
                copy = _append_into if table in _AUTO_ID_TABLES else _replace_into
                copy(conn, "incoming", "main", table, f"{key} IN {_IN_SUBTREE}")
            ends = f"source_id IN {_IN_SUBTREE} AND target_id IN {_IN_SUBTREE}"
            _replace_into(conn, "incoming", "main", "node_relations", ends)
            conn.execute(
                "INSERT OR REPLACE INTO main.external_relations "
                "SELECT * FROM incoming.external_relations"
            )
            _append_into(conn, "incoming", "main", "ledger_events", f"target_id IN {_IN_SUBTREE}")


def archive_due(
    root: Path, now: datetime, on_failure: Callable[[str, Exception], None] | None = None
) -> list[str]:
    """Archives each finished spec that has been so for `archive.automatically_after_hours` and
    has no blocker, leaves the rest to a later call, and returns the ids it archived. A spec that
    cannot be moved goes to `on_failure` and the others carry on: this runs beside commands that
    have their own work to do."""
    hours = ConfigStore(root).project().archive.automatically_after_hours
    db = DatabaseManager(root / ".taskmanager")
    specs = NodeRepository(db).list_nodes(kind=NodeKind.SPEC)
    archived = []
    for spec_id in archive_schedule(specs, LedgerRepository(db), now, hours).due:
        try:
            if not archive_blockers(root, spec_id):
                archive_spec(root, spec_id)
                archived.append(spec_id)
        except Exception as exc:  # noqa: BLE001
            if on_failure is not None:
                on_failure(spec_id, exc)
    return archived
