"""The page is served at every view path the router writes, and nothing else is shadowed."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from taskmanager.db.connection import DatabaseManager
from taskmanager.web.app import create_app


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    DatabaseManager(tmp_path / ".taskmanager").init_all()
    return TestClient(create_app(tmp_path))


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/document",
        "/graph",
        "/waves",
        "/decisions",
        "/document/S1-P1-T1",
        "/graph/S1",
        "/waves/S1-P1",
        "/decisions/decision-D1",
    ],
)
def test_each_view_path_serves_the_page(client: TestClient, path: str) -> None:
    res = client.get(path)
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/html")
    assert 'id="view-doc-btn"' in res.text


def test_the_api_is_not_shadowed(client: TestClient) -> None:
    res = client.get("/api/statuses")
    assert res.status_code == 200
    assert res.headers["content-type"] == "application/json"
    assert isinstance(res.json()["statuses"], list)


@pytest.mark.parametrize("path", ["/nope", "/nope/X", "/document/a/b", "/api/nope"])
def test_any_other_path_is_404(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 404
