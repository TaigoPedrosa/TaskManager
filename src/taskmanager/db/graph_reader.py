from dataclasses import dataclass, field
from datetime import UTC, datetime

from taskmanager.core.enums import RelationType, VerificationType
from taskmanager.core.models import (
    Condition,
    FileLock,
    Job,
    Lease,
    Node,
    NodeVerification,
)
from taskmanager.core.status import ConditionStage, JobState
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.job_repo import _COLUMNS as _JOB_COLUMNS
from taskmanager.db.job_repo import _row_to_job
from taskmanager.db.node_repo import _NODE_COLUMNS, NodeRepository
from taskmanager.db.runtime_repo import _LEASE_COLUMNS, _row_to_lease

_LIVE_JOB_STATES = (JobState.RUNNING.value, JobState.NEEDS_AGENT.value)


@dataclass(frozen=True)
class GraphData:
    """Every node, relation, verification, condition, lease, file lock and live job -- one read
    of `state.db`, grouped the way every reader already asks for it, so `SnapshotBuilder` and
    its callers need no query per node."""

    nodes: dict[str, Node]
    # SQLite's own count of changes to a node or to a section, verification, condition or
    # relation naming it; not on `Node` itself, since nothing that builds one writes it.
    revs: dict[str, int]
    relations: dict[RelationType, list[tuple[str, str]]]
    verifications: dict[str, list[NodeVerification]]
    conditions: dict[str, list[Condition]]
    leases: dict[str, Lease]
    file_locks: list[FileLock]
    jobs: dict[str, list[Job]]
    # One instant for the whole snapshot: every lease's liveness is judged against it, not
    # against the time each reader happens to ask.
    built_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))


def read_graph(db: DatabaseManager) -> GraphData:
    built_at = datetime.now(tz=UTC)
    with db.get_state_connection() as conn:
        # A caller already inside a transaction (`validated_write`) shares its consistent view;
        # otherwise this read opens its own, so the eight selects below see one snapshot.
        opened = not db.in_transaction and not conn.in_transaction
        if opened:
            conn.execute("BEGIN")
        try:
            # Same order as `NodeRepository.list_nodes()`: a topological tie-break (migration
            # ordering) reads this dict's insertion order, so it must match exactly.
            node_rows = conn.execute(
                f"SELECT {_NODE_COLUMNS} FROM nodes ORDER BY ordinal ASC, priority DESC, id ASC"
            ).fetchall()
            nodes = {row[0]: NodeRepository._row_to_node(row) for row in node_rows}

            revs = dict(conn.execute("SELECT id, rev FROM nodes").fetchall())

            relations: dict[RelationType, list[tuple[str, str]]] = {t: [] for t in RelationType}
            for source, target, rel_type in conn.execute(
                "SELECT source_id, target_id, relation_type FROM node_relations ORDER BY rowid ASC"
            ).fetchall():
                relations[RelationType(rel_type)].append((source, target))

            verifications: dict[str, list[NodeVerification]] = {}
            for r in conn.execute(
                """
                SELECT id, node_id, verification_type, target_path,
                       expected_pattern, codegraph_query_json
                FROM node_verifications
                ORDER BY id ASC
                """
            ).fetchall():
                verifications.setdefault(r[1], []).append(
                    NodeVerification(
                        id=r[0],
                        node_id=r[1],
                        verification_type=VerificationType(r[2]),
                        target_path=r[3],
                        expected_pattern=r[4],
                        codegraph_query_json=r[5],
                    )
                )

            conditions: dict[str, list[Condition]] = {}
            for r in conn.execute(
                "SELECT node_id, idx, needs, command, stage FROM node_conditions "
                "ORDER BY node_id ASC, idx ASC"
            ).fetchall():
                conditions.setdefault(r[0], []).append(
                    Condition(
                        node_id=r[0],
                        idx=r[1],
                        needs=r[2],
                        command=r[3],
                        stage=ConditionStage(r[4]),
                    )
                )

            leases = {
                row[0]: _row_to_lease(row)
                for row in conn.execute(f"SELECT {_LEASE_COLUMNS} FROM leases").fetchall()
            }

            file_locks = [
                FileLock(file_path=r[0], task_id=r[1], lock_type=r[2])
                for r in conn.execute(
                    "SELECT file_path, task_id, lock_type FROM file_locks ORDER BY file_path"
                ).fetchall()
            ]

            jobs: dict[str, list[Job]] = {}
            for r in conn.execute(
                f"SELECT {_JOB_COLUMNS} FROM jobs WHERE state IN (?, ?) ORDER BY rowid ASC",
                _LIVE_JOB_STATES,
            ).fetchall():
                job = _row_to_job(r)
                jobs.setdefault(job.node_id, []).append(job)
        finally:
            if opened:
                conn.execute("COMMIT")
    return GraphData(
        nodes=nodes,
        revs=revs,
        relations=relations,
        verifications=verifications,
        conditions=conditions,
        leases=leases,
        file_locks=file_locks,
        jobs=jobs,
        built_at=built_at,
    )
