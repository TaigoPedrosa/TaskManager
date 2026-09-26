"""Scoring reads only the snapshot already built: no per-task query, whatever the estate's size.

Statement counts, never wall time: `sqlite3.Connection.set_trace_callback` on the connections
`DatabaseManager` hands out counts each `execute()`, at 100 and 1,000 nodes.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from estate import seed

from taskmanager.db.cache_repo import CacheRepository
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.job_repo import JobRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.engine.heuristics import RecommendationEngine, score_every_task
from taskmanager.engine.snapshot import SnapshotBuilder

# One spec + `plans` plans + `plans * 8` tasks: 11 plans seed 100 nodes, 111 seed 1,000.
_ESTATE_SIZES = {100: 11, 1000: 111}


@contextmanager
def count_statements(db: DatabaseManager) -> Iterator[list[int]]:
    """Every statement run on `db`'s state and cache connections from here until the block
    exits, as a one-item list so the caller can read it after the `with` closes."""
    count = [0]

    def trace(_: str) -> None:
        count[0] += 1

    with db.get_state_connection() as conn:
        conn.set_trace_callback(trace)
    with db.get_cache_connection() as conn:
        conn.set_trace_callback(trace)
    try:
        yield count
    finally:
        with db.get_state_connection() as conn:
            conn.set_trace_callback(None)
        with db.get_cache_connection() as conn:
            conn.set_trace_callback(None)


def _score_every_task_statements(root: Path, plans: int) -> int:
    node_repo = seed(root, plans)
    db = node_repo.db
    builder = SnapshotBuilder(node_repo, RuntimeRepository(db), JobRepository(db))
    snapshot = builder.build()
    with count_statements(db) as count:
        score_every_task(snapshot)
    return count[0]


def _get_next_tasks_statements(root: Path, plans: int) -> int:
    node_repo = seed(root, plans)
    db = node_repo.db
    builder = SnapshotBuilder(node_repo, RuntimeRepository(db), JobRepository(db))
    engine = RecommendationEngine(node_repo, RuntimeRepository(db), builder, CacheRepository(db))
    with count_statements(db) as count:
        engine.get_next_tasks(limit=10)
    return count[0]


def test_score_every_task_issues_no_sql_statement(tmp_path: Path) -> None:
    assert _score_every_task_statements(tmp_path, _ESTATE_SIZES[100]) == 0


def test_score_every_task_issues_no_sql_statement_at_1000_nodes(tmp_path: Path) -> None:
    assert _score_every_task_statements(tmp_path, _ESTATE_SIZES[1000]) == 0


def test_get_next_tasks_costs_the_same_at_100_and_1000_nodes(tmp_path: Path) -> None:
    counts = {
        size: _get_next_tasks_statements(tmp_path / str(size), plans)
        for size, plans in _ESTATE_SIZES.items()
    }
    assert counts[100] == counts[1000]
