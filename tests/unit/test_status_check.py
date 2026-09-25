"""A stored status the node's kind cannot hold is refused by SQLite and by the model."""

import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from taskmanager.core.enums import NodeKind
from taskmanager.core.models import Node
from taskmanager.core.status import DecisionStatus, Status
from taskmanager.db.connection import DatabaseManager


@pytest.fixture
def db(tmp_path: Path) -> DatabaseManager:
    manager = DatabaseManager(tmp_path / ".taskmanager")
    manager.init_all()
    return manager


def insert(db: DatabaseManager, kind: str, status: str) -> None:
    with db.get_state_connection() as conn:
        conn.execute(
            "INSERT INTO nodes (id, kind, title, status) VALUES (?, ?, ?, ?)",
            (f"{kind}-{status}", kind, "x", status),
        )
        conn.commit()


@pytest.mark.parametrize(
    ("kind", "status"),
    [
        ("task", "NOT_STARTED"),
        ("task", "WAITING_REVIEW"),
        ("plan", "IN_FLIGHT"),
        ("spec", "BLOCKED"),
        ("task", "OPEN"),
        ("decision", "READY"),
        ("decision", "NOT_STARTED"),
        ("decision", "COMPLETED"),
    ],
)
def test_the_status_check_refuses_a_status_the_kind_cannot_hold(
    db: DatabaseManager, kind: str, status: str
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        insert(db, kind, status)


@pytest.mark.parametrize(
    ("kind", "status"),
    [
        ("task", "READY"),
        ("plan", "IMPLEMENTED"),
        ("spec", "COMPLETED"),
        ("task", "SUPERSEDED"),
        ("decision", "OPEN"),
        ("decision", "WITHDRAWN"),
    ],
)
def test_the_status_check_accepts_the_statuses_of_the_kind(
    db: DatabaseManager, kind: str, status: str
) -> None:
    insert(db, kind, status)


def test_a_node_defaults_to_ready_and_a_decision_to_open() -> None:
    assert Node(id="T", kind=NodeKind.TASK, title="t").status == Status.READY
    assert Node(id="D", kind=NodeKind.DECISION, title="d").status == DecisionStatus.OPEN


@pytest.mark.parametrize(
    ("kind", "status"),
    [
        (NodeKind.TASK, DecisionStatus.OPEN),
        (NodeKind.DECISION, Status.READY),
        (NodeKind.PLAN, "NOT_STARTED"),
    ],
)
def test_the_model_refuses_a_status_its_kind_cannot_hold(kind: NodeKind, status: object) -> None:
    with pytest.raises(ValidationError):
        Node(id="X", kind=kind, title="x", status=status)  # type: ignore[arg-type]
