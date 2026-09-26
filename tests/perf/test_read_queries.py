"""A bulk read of the graph costs a fixed number of statements, whatever the estate's size.

Statement counts, never wall time: `sqlite3.Connection.set_trace_callback` on the connections
`DatabaseManager` hands out counts each `execute()`, at 100 and 1,000 nodes.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from estate import seed

from taskmanager.db.cache_repo import CacheRepository
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.job_repo import JobRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.engine.claims import Claims
from taskmanager.engine.discovery import discover
from taskmanager.engine.snapshot import DisplayView, SnapshotBuilder

# One spec + `plans` plans + `plans * 8` tasks: 11 plans seed 100 nodes, 111 seed 1,000.
_ESTATE_SIZES = {100: 11, 1000: 111}
_MAX_DISPLAY_STATEMENTS = 16


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


def _display_statements(root: Path, plans: int) -> int:
    node_repo = seed(root, plans)
    db = node_repo.db
    builder = SnapshotBuilder(node_repo, RuntimeRepository(db), JobRepository(db))
    with count_statements(db) as count:
        view = DisplayView(builder, CacheRepository(db), max_age=0)
        assert view.snapshot.data is not None
        for node in view.snapshot.data.nodes.values():
            view.display(node)
    return count[0]


def _discover_statements(root: Path, plans: int) -> int:
    seed(root, plans)
    claims = Claims.open(root)
    with count_statements(claims.nodes.db) as count:
        discover(claims, specs=None, session="s1", slots=10, max_strong=10)
    return count[0]


@pytest.mark.parametrize("size", sorted(_ESTATE_SIZES))
def test_a_display_view_and_every_nodes_display_stay_within_a_fixed_statement_budget(
    tmp_path: Path, size: int
) -> None:
    statements = _display_statements(tmp_path / str(size), _ESTATE_SIZES[size])
    assert statements <= _MAX_DISPLAY_STATEMENTS


def test_a_display_view_and_every_nodes_display_cost_the_same_at_100_and_1000_nodes(
    tmp_path: Path,
) -> None:
    counts = {
        size: _display_statements(tmp_path / str(size), plans)
        for size, plans in _ESTATE_SIZES.items()
    }
    assert counts[100] == counts[1000]


def test_wave_discover_costs_the_same_number_of_statements_at_100_and_1000_nodes(
    tmp_path: Path,
) -> None:
    counts = {
        size: _discover_statements(tmp_path / str(size), plans)
        for size, plans in _ESTATE_SIZES.items()
    }
    assert counts[100] == counts[1000]
