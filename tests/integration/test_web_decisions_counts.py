"""Integration tests for `/api/decisions`' `counts`."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from taskmanager.core.enums import NodeKind
from taskmanager.core.models import Node
from taskmanager.core.status import DecisionStatus
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.node_repo import NodeRepository
from taskmanager.web.app import create_app


@pytest.fixture
def api(tmp_path: Path) -> tuple[TestClient, NodeRepository]:
    db_mgr = DatabaseManager(tmp_path / ".taskmanager")
    db_mgr.init_all()
    node_repo = NodeRepository(db_mgr)
    for i in range(2):
        node_repo.save_node(Node(id=f"decision-open-{i}", kind=NodeKind.DECISION, title="Open"))
    node_repo.save_node(
        Node(
            id="decision-answered-1",
            kind=NodeKind.DECISION,
            title="Answered",
            status=DecisionStatus.ANSWERED,
        )
    )
    for i in range(3):
        node_repo.save_node(
            Node(
                id=f"decision-withdrawn-{i}",
                kind=NodeKind.DECISION,
                title="Withdrawn",
                status=DecisionStatus.WITHDRAWN,
            )
        )
    app = create_app(tmp_path)
    return TestClient(app), node_repo


def test_counts_hold_every_status_with_no_filter(
    api: tuple[TestClient, NodeRepository],
) -> None:
    client, _node_repo = api
    res = client.get("/api/decisions")
    assert res.status_code == 200
    assert res.json()["counts"] == {"open": 2, "answered": 1, "withdrawn": 3}


def test_counts_hold_every_status_under_a_status_filter(
    api: tuple[TestClient, NodeRepository],
) -> None:
    client, _node_repo = api
    res = client.get("/api/decisions", params={"status": "withdrawn"})
    assert res.status_code == 200
    body = res.json()
    assert len(body["items"]) == 3
    assert body["counts"] == {"open": 2, "answered": 1, "withdrawn": 3}


def test_counts_hold_every_status_across_a_page_boundary(
    api: tuple[TestClient, NodeRepository],
) -> None:
    client, _node_repo = api
    res = client.get("/api/decisions", params={"status": "withdrawn", "limit": "1"})
    assert res.status_code == 200
    body = res.json()
    assert len(body["items"]) == 1
    assert body["next"] is not None
    assert body["counts"] == {"open": 2, "answered": 1, "withdrawn": 3}
