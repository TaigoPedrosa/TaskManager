import os
import struct
from typing import Any, Protocol

import httpx

from taskmanager.core.enums import NodeKind, NodeStatus, SearchTargetType
from taskmanager.db.connection import DatabaseManager


class EmbeddingProvider(Protocol):
    def get_embedding(self, text: str) -> list[float]: ...


class MockEmbeddingProvider:
    def __init__(self, dimensions: int = 384) -> None:
        self.dimensions = dimensions

    def get_embedding(self, text: str) -> list[float]:
        vec = [0.0] * self.dimensions
        for i, char in enumerate(text[: self.dimensions]):
            vec[i] = (ord(char) % 10) / 10.0
        return vec


class OpenAIEmbeddingProvider:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
    ) -> None:
        self.base_url = (
            base_url or os.getenv("TASKMANAGER_OPENAI_BASE_URL") or "https://api.openai.com/v1"
        ).rstrip("/")
        self.api_key = api_key or os.getenv("TASKMANAGER_OPENAI_API_KEY") or ""
        self.model = model or os.getenv("TASKMANAGER_OPENAI_MODEL") or "text-embedding-3-small"

    def get_embedding(self, text: str) -> list[float]:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        payload = {"input": text, "model": self.model}
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(f"{self.base_url}/embeddings", json=payload, headers=headers)
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            embedding: list[float] = data["data"][0]["embedding"]
            return embedding


class SearchEngine:
    def __init__(self, db_mgr: DatabaseManager, embedding_provider: Any) -> None:
        self.db = db_mgr
        self.provider = embedding_provider

    def _serialize_vec(self, vector: list[float]) -> bytes:
        return struct.pack(f"{len(vector)}f", *vector)

    def index_node(
        self,
        node_id: str,
        vector: list[float],
        target_type: SearchTargetType | str = SearchTargetType.TITLE,
        section_key: str | None = None,
    ) -> None:
        target_type_str = target_type.value if hasattr(target_type, "value") else str(target_type)
        raw_bytes = self._serialize_vec(vector)
        with self.db.get_spec_connection() as conn:
            conn.execute("DELETE FROM vec_nodes WHERE node_id = ?", (node_id,))
            conn.execute(
                """
                INSERT INTO vec_nodes (node_id, target_type, section_key, embedding)
                VALUES (?, ?, ?, ?)
                """,
                (node_id, target_type_str, section_key or "", raw_bytes),
            )
            conn.commit()

    def search(
        self,
        query: str,
        query_vector: list[float] | None = None,
        kinds: list[NodeKind | str] | None = None,
        statuses: list[NodeStatus | str] | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        vec = query_vector if query_vector is not None else self.provider.get_embedding(query)
        raw_bytes = self._serialize_vec(vec)

        k_candidates = max(limit * 5, 20) if (kinds or statuses) else limit

        query_sql = """
        SELECT
            n.id,
            n.kind,
            n.title,
            n.status,
            v.target_type,
            v.section_key,
            v.distance
        FROM vec_nodes v
        JOIN nodes n ON n.id = v.node_id
        WHERE v.embedding MATCH ?
          AND k = ?
        """
        params: list[Any] = [raw_bytes, k_candidates]

        if kinds:
            placeholders = ",".join("?" for _ in kinds)
            query_sql += f" AND n.kind IN ({placeholders})"
            params.extend([k.value if hasattr(k, "value") else str(k) for k in kinds])

        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            query_sql += f" AND n.status IN ({placeholders})"
            params.extend([s.value if hasattr(s, "value") else str(s) for s in statuses])

        query_sql += " ORDER BY v.distance ASC LIMIT ?"
        params.append(limit)

        with self.db.get_spec_connection() as conn:
            rows = conn.execute(query_sql, tuple(params)).fetchall()
            return [
                {
                    "node_id": r[0],
                    "kind": r[1],
                    "title": r[2],
                    "status": r[3],
                    "target_type": r[4],
                    "section_key": r[5] if r[5] else None,
                    "distance": float(r[6]),
                }
                for r in rows
            ]
