import struct
from pathlib import Path
from typing import Any

import httpx
import pytest

from taskmanager.core.enums import NodeKind
from taskmanager.core.models import Node
from taskmanager.core.status import Status
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.node_repo import NodeRepository
from taskmanager.engine.search import (
    MockEmbeddingProvider,
    OpenAIEmbeddingProvider,
    SearchEngine,
)


def test_vector_serialization() -> None:
    provider = MockEmbeddingProvider(dimensions=4)
    db = DatabaseManager(Path("/tmp"))
    engine = SearchEngine(db, provider)

    vector = [1.0, 2.5, -3.0, 0.125]
    serialized = engine._serialize_vec(vector)
    assert isinstance(serialized, bytes)
    assert len(serialized) == 4 * 4

    unpacked = struct.unpack("4f", serialized)
    for expected, actual in zip(vector, unpacked, strict=True):
        assert pytest.approx(actual, rel=1e-5) == expected


def test_mock_embedding_provider_deterministic() -> None:
    provider = MockEmbeddingProvider(dimensions=8)
    vec1 = provider.get_embedding("hello")
    vec2 = provider.get_embedding("hello")
    vec3 = provider.get_embedding("world")

    assert len(vec1) == 8
    assert vec1 == vec2
    assert vec1 != vec3


def test_sqlite_vec_knn_matching_with_mock_provider(tmp_path: Path) -> None:
    db = DatabaseManager(tmp_path)
    db.init_all(vector_dimensions=4)
    repo = NodeRepository(db)

    repo.save_node(Node(id="AUTH-T01", kind=NodeKind.TASK, title="JWT Auth", status=Status.READY))
    repo.save_node(
        Node(
            id="DATA-T01",
            kind=NodeKind.TASK,
            title="Postgres Migration",
            status=Status.READY,
        )
    )

    provider = MockEmbeddingProvider(dimensions=4)
    engine = SearchEngine(db, provider)

    engine.index_node("AUTH-T01", [1.0, 0.0, 0.0, 0.0])
    engine.index_node("DATA-T01", [0.0, 1.0, 0.0, 0.0])

    results = engine.search(query="JWT", query_vector=[0.9, 0.1, 0.0, 0.0], limit=2)
    assert len(results) == 2
    assert results[0]["node_id"] == "AUTH-T01"
    assert results[0]["title"] == "JWT Auth"
    assert results[0]["status"] == Status.READY.value
    assert results[0]["distance"] < results[1]["distance"]

    text_results = engine.search(query="JWT", limit=2)
    assert len(text_results) > 0
    assert "node_id" in text_results[0]
    assert "distance" in text_results[0]


def test_index_node_upsert_replaces_vector(tmp_path: Path) -> None:
    db = DatabaseManager(tmp_path)
    db.init_all(vector_dimensions=4)
    repo = NodeRepository(db)

    repo.save_node(
        Node(id="TASK-01", kind=NodeKind.TASK, title="Initial Task", status=Status.READY)
    )

    provider = MockEmbeddingProvider(dimensions=4)
    engine = SearchEngine(db, provider)

    engine.index_node("TASK-01", [1.0, 0.0, 0.0, 0.0], target_type="title", section_key="details")
    results = engine.search(query="", query_vector=[1.0, 0.0, 0.0, 0.0], limit=1)
    assert len(results) == 1
    assert results[0]["section_key"] == "details"
    assert pytest.approx(results[0]["distance"], abs=1e-4) == 0.0

    engine.index_node("TASK-01", [0.0, 1.0, 0.0, 0.0], target_type="title", section_key=None)
    results_after = engine.search(query="", query_vector=[0.0, 1.0, 0.0, 0.0], limit=1)
    assert len(results_after) == 1
    assert results_after[0]["section_key"] is None
    assert pytest.approx(results_after[0]["distance"], abs=1e-4) == 0.0


def test_vector_search_kind_and_status_filtering(tmp_path: Path) -> None:
    db = DatabaseManager(tmp_path)
    db.init_all(vector_dimensions=4)
    repo = NodeRepository(db)

    nodes = [
        Node(id="TASK-OPEN", kind=NodeKind.TASK, title="Task Open", status=Status.READY),
        Node(id="TASK-DONE", kind=NodeKind.TASK, title="Task Done", status=Status.COMPLETED),
        Node(id="PLAN-OPEN", kind=NodeKind.PLAN, title="Plan Open", status=Status.IMPLEMENTING),
        Node(id="PLAN-DONE", kind=NodeKind.PLAN, title="Plan Done", status=Status.COMPLETED),
    ]
    for n in nodes:
        repo.save_node(n)

    provider = MockEmbeddingProvider(dimensions=4)
    engine = SearchEngine(db, provider)

    target_vec = [1.0, 0.0, 0.0, 0.0]
    engine.index_node("TASK-OPEN", [0.95, 0.05, 0.0, 0.0])
    engine.index_node("TASK-DONE", [0.99, 0.01, 0.0, 0.0])
    engine.index_node("PLAN-OPEN", [0.90, 0.10, 0.0, 0.0])
    engine.index_node("PLAN-DONE", [0.85, 0.15, 0.0, 0.0])

    task_results = engine.search(query="", query_vector=target_vec, kinds=["task"], limit=5)
    assert len(task_results) == 2
    assert {r["node_id"] for r in task_results} == {"TASK-OPEN", "TASK-DONE"}
    assert all(r["kind"] == "task" for r in task_results)

    done_results = engine.search(query="", query_vector=target_vec, statuses=["COMPLETED"], limit=5)
    assert len(done_results) == 2
    assert {r["node_id"] for r in done_results} == {"TASK-DONE", "PLAN-DONE"}
    assert all(r["status"] == "COMPLETED" for r in done_results)

    combined_results = engine.search(
        query="",
        query_vector=target_vec,
        kinds=["plan"],
        statuses=["COMPLETED"],
        limit=5,
    )
    assert len(combined_results) == 1
    assert combined_results[0]["node_id"] == "PLAN-DONE"


def test_openai_embedding_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TASKMANAGER_OPENAI_BASE_URL", "https://custom.openai.test/v1")
    monkeypatch.setenv("TASKMANAGER_OPENAI_API_KEY", "env-test-key")
    monkeypatch.setenv("TASKMANAGER_OPENAI_MODEL", "text-embedding-test")

    provider = OpenAIEmbeddingProvider()
    assert provider.base_url == "https://custom.openai.test/v1"
    assert provider.api_key == "env-test-key"
    assert provider.model == "text-embedding-test"

    expected_vector = [0.1, 0.2, 0.3, 0.4]

    def mock_post(self: httpx.Client, url: str, **kwargs: Any) -> httpx.Response:
        assert url == "https://custom.openai.test/v1/embeddings"
        assert kwargs["headers"]["Authorization"] == "Bearer env-test-key"
        assert kwargs["json"]["model"] == "text-embedding-test"
        assert kwargs["json"]["input"] == "test query"
        return httpx.Response(
            status_code=200,
            json={"data": [{"embedding": expected_vector}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx.Client, "post", mock_post)
    actual_vector = provider.get_embedding("test query")
    assert actual_vector == expected_vector
