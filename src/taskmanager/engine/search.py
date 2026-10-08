import hashlib
import os
import re
import sqlite3
import struct
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, replace
from typing import Any, Final, Protocol

import httpx

from taskmanager import __version__
from taskmanager.core.enums import (
    EmbeddingProviderType,
    NodeKind,
    SearchMode,
    SearchTargetType,
)
from taskmanager.core.status import DecisionStatus, Status
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.schema import vec_nodes_sql
from taskmanager.engine.config import DEFAULT_KEY_ENV, EmbeddingsConfig

RRF_K: Final = 60
CHUNK_CHARS: Final = 1500
INDEXED_KINDS: Final = (NodeKind.TASK.value, NodeKind.PLAN.value, NodeKind.SPEC.value)
DEFAULT_MODELS: Final = {
    EmbeddingProviderType.NONE: "",
    EmbeddingProviderType.MOCK: "mock",
    EmbeddingProviderType.OPENAI: "text-embedding-3-small",
    EmbeddingProviderType.LOCAL: "all-MiniLM-L6-v2",
}
NO_PROVIDER: Final = (
    "no embedding provider configured: `tm config set embeddings.provider <local|openai>`"
)
REBUILD: Final = "run `tm index --rebuild`"
REPOSITORY: Final = "git+https://github.com/TaigoPedrosa/TaskManager"

# The plan a node belongs to: itself for a plan, its parent plan for a task.
_PLAN_SQL: Final = """CASE n.kind WHEN 'plan' THEN n.id WHEN 'task' THEN (
    SELECT MIN(r.source_id) FROM node_relations r
    WHERE r.target_id = n.id AND r.relation_type = 'contains') END"""


class SearchError(Exception):
    pass


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
        self.api_key = api_key if api_key is not None else os.getenv(DEFAULT_KEY_ENV, "")
        self.model = model or os.getenv("TASKMANAGER_OPENAI_MODEL") or "text-embedding-3-small"

    def get_embedding(self, text: str) -> list[float]:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        payload = {"input": text, "model": self.model}
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.post(f"{self.base_url}/embeddings", json=payload, headers=headers)
                resp.raise_for_status()
                embedding: list[float] = resp.json()["data"][0]["embedding"]
        except (httpx.HTTPError, ValueError, LookupError, TypeError) as exc:
            raise SearchError(f"embedding request to {self.base_url} failed: {exc}") from exc
        return embedding


class LocalEmbeddingProvider:
    def __init__(self, model: str) -> None:
        self.model = model
        self._encoder: Any = None

    def get_embedding(self, text: str) -> list[float]:
        if self._encoder is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                raise SearchError(
                    "sentence-transformers is not installed: uv tool install --reinstall "
                    f"'taskmanager[local-embeddings] @ {REPOSITORY}@v{__version__}'"
                ) from exc
            try:
                self._encoder = SentenceTransformer(self.model)
            except (OSError, ValueError) as exc:
                raise SearchError(f"cannot load local model {self.model}: {exc}") from exc
        return [float(x) for x in self._encoder.encode(text)]


def build_provider(cfg: EmbeddingsConfig) -> EmbeddingProvider | None:
    model = cfg.model or DEFAULT_MODELS[cfg.provider]
    match cfg.provider:
        case EmbeddingProviderType.MOCK:
            return MockEmbeddingProvider(cfg.dimensions)
        case EmbeddingProviderType.OPENAI:
            return OpenAIEmbeddingProvider(cfg.base_url, os.environ.get(cfg.api_key_env, ""), model)
        case EmbeddingProviderType.LOCAL:
            return LocalEmbeddingProvider(model)
        case _:
            return None


@dataclass(frozen=True)
class Hit:
    id: str
    kind: str
    title: str
    status: str
    plan: str | None
    score: float
    snippet: str
    section: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class IndexReport:
    embedded: int
    unchanged: int
    removed: int


def fts_query(text: str) -> str:
    """The user's words as FTS5 phrases, so operators and punctuation in them are data.

    A phrase the user quoted stays one phrase; the rest are OR-ed and ranked by bm25.
    """
    phrases = []
    for m in re.finditer(r'"([^"]*)"|(\S+)', text):
        raw = m.group(1) if m.group(1) is not None else m.group(2)
        if any(c.isalnum() for c in raw):
            phrases.append('"' + raw.replace('"', '""') + '"')
    return " OR ".join(phrases)


def chunks(text: str, size: int = CHUNK_CHARS) -> list[str]:
    """Paragraphs packed up to about `size` characters; a longer paragraph is cut hard."""
    out: list[str] = []
    current = ""
    for para in (p.strip() for p in text.split("\n\n")):
        if not para:
            continue
        while len(para) > size:
            if current:
                out.append(current)
                current = ""
            out.append(para[:size])
            para = para[size:]
        if current and len(current) + len(para) + 2 > size:
            out.append(current)
            current = ""
        current = f"{current}\n\n{para}" if current else para
    if current:
        out.append(current)
    return out


def _where(
    kinds: Iterable[NodeKind | str], status: Status | DecisionStatus | str | None, plan: str | None
) -> tuple[str, list[Any]]:
    sql, params = "", []
    kind_values = [str(k) for k in kinds]
    if kind_values:
        sql += f" AND n.kind IN ({','.join('?' for _ in kind_values)})"
        params += kind_values
    if status:
        sql += " AND n.status = ?"
        params.append(str(status))
    if plan:
        sql += (
            " AND (n.id = ? OR n.id IN (SELECT target_id FROM node_relations"
            " WHERE source_id = ? AND relation_type = 'contains'))"
        )
        params += [plan, plan]
    return sql, params


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class SearchEngine:
    def __init__(
        self,
        db_mgr: DatabaseManager,
        embedding_provider: EmbeddingProvider | None,
        settings: EmbeddingsConfig | None = None,
    ) -> None:
        self.db = db_mgr
        self.provider = embedding_provider
        self.settings = settings or EmbeddingsConfig()

    def _serialize_vec(self, vector: list[float]) -> bytes:
        return struct.pack(f"{len(vector)}f", *vector)

    def _embed(self, text: str, dimensions: int) -> list[float]:
        if self.provider is None:
            raise SearchError(NO_PROVIDER)
        vec = self.provider.get_embedding(text)
        if len(vec) != dimensions:
            raise SearchError(
                f"the provider returned {len(vec)} dimensions but the index holds {dimensions}: "
                f"set embeddings.dimensions and {REBUILD}"
            )
        return vec

    @staticmethod
    def _table_dimensions(conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'vec_nodes'").fetchone()
        if row is None:
            raise SearchError(f"there is no vector index: run `tm index`, or {REBUILD}")
        if "PRIMARY KEY" in row[0].upper():
            raise SearchError(f"the vector index holds one vector per node: {REBUILD}")
        found = re.search(r"FLOAT\[(\d+)\]", row[0], re.IGNORECASE)
        if found is None:
            raise SearchError(f"the vector index has no dimension: {REBUILD}")
        return int(found.group(1))

    def _check_index(self, conn: sqlite3.Connection) -> int:
        """The table's dimension, once it matches the configured dimension, provider and model."""
        dims = self._table_dimensions(conn)
        built = conn.execute(
            "SELECT provider, model_name, dimensions FROM embedding_metadata ORDER BY id DESC"
        ).fetchone()
        want = (self.settings.provider.value, self._model(), self.settings.dimensions)
        if dims != self.settings.dimensions or (built is not None and tuple(built) != want):
            made = "/".join(map(str, built)) if built else f"{dims} dimensions"
            raise SearchError(
                f"the index was built with {made} but the configuration says "
                f"{'/'.join(map(str, want))}: {REBUILD}"
            )
        return dims

    @staticmethod
    def _never_indexed(conn: sqlite3.Connection) -> bool:
        """Nothing was embedded yet, so there is no index whose dimension a change could break."""
        has_table = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'vec_nodes'").fetchone()
        return not conn.execute("SELECT 1 FROM embedding_metadata").fetchone() and (
            not has_table or not conn.execute("SELECT 1 FROM vec_nodes LIMIT 1").fetchone()
        )

    def _model(self) -> str:
        return self.settings.model or DEFAULT_MODELS[self.settings.provider]

    def index_node(
        self,
        node_id: str,
        vector: list[float],
        target_type: SearchTargetType | str = SearchTargetType.TITLE,
        section_key: str | None = None,
    ) -> None:
        with self.db.get_state_connection() as conn:
            self._replace(conn, node_id, str(target_type), section_key or "", [vector])
            conn.commit()

    def _replace(
        self,
        conn: sqlite3.Connection,
        node_id: str,
        target_type: str,
        section_key: str,
        vectors: list[list[float]],
    ) -> None:
        conn.execute(
            "DELETE FROM vec_nodes WHERE node_id = ? AND target_type = ? AND section_key = ?",
            (node_id, target_type, section_key),
        )
        for vector in vectors:
            conn.execute(
                "INSERT INTO vec_nodes (node_id, target_type, section_key, embedding)"
                " VALUES (?, ?, ?, ?)",
                (node_id, target_type, section_key, self._serialize_vec(vector)),
            )

    def _desired(
        self, conn: sqlite3.Connection, kinds: Iterable[NodeKind | str]
    ) -> dict[tuple[str, str, str], str]:
        """Every (node, target, section) that should be embedded, with the text to embed."""
        values = [str(k) for k in kinds] or list(INDEXED_KINDS)
        marks = ",".join("?" for _ in values)
        want: dict[tuple[str, str, str], str] = {}
        for node_id, title in conn.execute(
            f"SELECT id, title FROM nodes WHERE kind IN ({marks})", values
        ):
            want[(node_id, "title", "")] = title
        for node_id, key, header, content in conn.execute(
            "SELECT s.node_id, s.section_key, s.header, s.content FROM node_sections s"
            f" JOIN nodes n ON n.id = s.node_id WHERE n.kind IN ({marks})",
            values,
        ):
            if content.strip():
                want[(node_id, "section", key)] = f"{header}\n{content}"
        return want

    @staticmethod
    def _recorded(
        conn: sqlite3.Connection, kinds: Iterable[NodeKind | str]
    ) -> dict[tuple[str, str, str], str]:
        values = [str(k) for k in kinds] or list(INDEXED_KINDS)
        rows = conn.execute(
            "SELECT h.node_id, h.target_type, h.section_key, h.content_hash FROM index_state h"
            " LEFT JOIN nodes n ON n.id = h.node_id"
            f" WHERE n.kind IS NULL OR n.kind IN ({','.join('?' for _ in values)})",
            values,
        )
        return {(r[0], r[1], r[2]): r[3] for r in rows}

    def index(
        self,
        kinds: Iterable[NodeKind | str] = (),
        rebuild: bool = False,
        progress: Callable[[int, int], None] | None = None,
    ) -> IndexReport:
        if self.provider is None:
            raise SearchError(NO_PROVIDER)
        kinds = tuple(kinds)
        with self.db.get_state_connection() as conn:
            if rebuild or self._never_indexed(conn):
                conn.execute("DROP TABLE IF EXISTS vec_nodes")
                conn.execute(vec_nodes_sql(self.settings.dimensions))
                conn.execute("DELETE FROM index_state")
                conn.commit()
                dims = self.settings.dimensions
            else:
                dims = self._check_index(conn)
            want = self._desired(conn, kinds)
            have = self._recorded(conn, kinds)
            todo = [k for k, text in want.items() if have.get(k) != _hash(text)]
            for done, key in enumerate(sorted(todo), 1):
                vectors = [self._embed(c, dims) for c in chunks(want[key])]
                self._replace(conn, *key, vectors)
                conn.execute(
                    "INSERT OR REPLACE INTO index_state VALUES (?, ?, ?, ?)",
                    (*key, _hash(want[key])),
                )
                conn.commit()
                if progress:
                    progress(done, len(todo))
            gone = [k for k in have if k not in want]
            for key in gone:
                self._replace(conn, *key, [])
                conn.execute(
                    "DELETE FROM index_state WHERE node_id = ? AND target_type = ?"
                    " AND section_key = ?",
                    key,
                )
            conn.execute("DELETE FROM embedding_metadata")
            conn.execute(
                "INSERT INTO embedding_metadata (provider, model_name, dimensions)"
                " VALUES (?, ?, ?)",
                (self.settings.provider.value, self._model(), dims),
            )
            conn.commit()
        return IndexReport(len(todo), len(want) - len(todo), len(gone))

    def stale_nodes(self) -> set[str]:
        with self.db.get_state_connection() as conn:
            want, have = self._desired(conn, ()), self._recorded(conn, ())
        changed = [k for k, text in want.items() if have.get(k) != _hash(text)]
        return {k[0] for k in [*changed, *(k for k in have if k not in want)]}

    def status(self) -> dict[str, Any]:
        with self.db.get_state_connection() as conn:
            built = conn.execute(
                "SELECT provider, model_name, dimensions FROM embedding_metadata ORDER BY id DESC"
            ).fetchone()
            has_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'vec_nodes'"
            ).fetchone()
            status = {
                "provider": self.settings.provider.value,
                "model": self._model(),
                "dimensions": self.settings.dimensions,
                "built_with": (
                    dict(zip(("provider", "model", "dimensions"), built, strict=True))
                    if built
                    else None
                ),
                "nodes_indexed": conn.execute(
                    "SELECT COUNT(DISTINCT node_id) FROM index_state"
                ).fetchone()[0],
                "chunks": (
                    conn.execute("SELECT COUNT(*) FROM vec_nodes").fetchone()[0] if has_table else 0
                ),
            }
        return {**status, "stale_nodes": len(self.stale_nodes())}

    def search(
        self,
        query: str,
        query_vector: list[float] | None = None,
        kinds: Iterable[NodeKind | str] | None = None,
        statuses: list[Status | DecisionStatus | str] | None = None,
        limit: int = 5,
        plan: str | None = None,
    ) -> list[dict[str, Any]]:
        """Nearest nodes by their best vector; every filter applies before the limit."""
        where, params = _where(kinds or (), None, plan)
        if statuses:
            where += f" AND n.status IN ({','.join('?' for _ in statuses)})"
            params += [str(s) for s in statuses]
        with self.db.get_state_connection() as conn:
            dims = self._table_dimensions(conn)
            vec = query_vector if query_vector is not None else self._embed(query, dims)
            if len(vec) != dims:
                raise SearchError(f"the query has {len(vec)} dimensions, the index {dims}")
            rows = conn.execute(
                f"""
                SELECT n.id, n.kind, n.title, n.status, {_PLAN_SQL}, v.target_type, v.section_key,
                       MIN(vec_distance_cosine(v.embedding, ?)) AS distance
                FROM vec_nodes v JOIN nodes n ON n.id = v.node_id
                WHERE 1 = 1 {where}
                GROUP BY n.id ORDER BY distance ASC, n.id ASC LIMIT ?
                """,
                [self._serialize_vec(vec), *params, limit],
            ).fetchall()
        return [
            {
                "node_id": r[0],
                "kind": r[1],
                "title": r[2],
                "status": r[3],
                "plan": r[4],
                "target_type": r[5],
                "section_key": r[6] if r[6] else None,
                "distance": float(r[7]),
            }
            for r in rows
        ]

    def fts(
        self,
        query: str,
        kinds: Iterable[NodeKind | str] = (),
        status: Status | DecisionStatus | str | None = None,
        plan: str | None = None,
        limit: int = 10,
    ) -> list[Hit]:
        match = fts_query(query)
        if not match:
            return []
        where, params = _where(kinds, status, plan)
        with self.db.get_state_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT n.id, n.kind, n.title, n.status, {_PLAN_SQL},
                       bm25(nodes_fts, 0.0, 10.0, 2.0, 1.0) AS rank,
                       snippet(nodes_fts, -1, '**', '**', '...', 12)
                FROM nodes_fts JOIN nodes n ON n.id = nodes_fts.node_id
                WHERE nodes_fts MATCH ? {where}
                ORDER BY rank ASC, n.id ASC LIMIT ?
                """,
                [match, *params, limit],
            ).fetchall()
        return [Hit(r[0], r[1], r[2], r[3], r[4], -r[5], r[6]) for r in rows]

    def semantic(
        self,
        query: str,
        kinds: Iterable[NodeKind | str] = (),
        status: Status | DecisionStatus | str | None = None,
        plan: str | None = None,
        limit: int = 10,
    ) -> list[Hit]:
        if self.provider is None:
            raise SearchError(NO_PROVIDER)
        with self.db.get_state_connection() as conn:
            self._check_index(conn)
            empty = conn.execute("SELECT COUNT(*) FROM vec_nodes").fetchone()[0] == 0
        if empty:
            raise SearchError("the vector index is empty: run `tm index`")
        if not query.strip():
            return []
        rows = self.search(
            query, kinds=kinds, statuses=[status] if status else None, limit=limit, plan=plan
        )
        return [
            Hit(
                r["node_id"],
                r["kind"],
                r["title"],
                r["status"],
                r["plan"],
                round(1 - r["distance"], 6),
                "",
                r["section_key"],
            )
            for r in rows
        ]

    def hybrid(
        self,
        query: str,
        kinds: Iterable[NodeKind | str] = (),
        status: Status | DecisionStatus | str | None = None,
        plan: str | None = None,
        limit: int = 10,
    ) -> list[Hit]:
        pool = max(limit * 5, 50)
        by_text = self.fts(query, kinds, status, plan, pool)
        by_meaning = self.semantic(query, kinds, status, plan, pool)
        scores: dict[str, float] = defaultdict(float)
        best: dict[str, Hit] = {}
        for ranked in (by_text, by_meaning):
            for rank, hit in enumerate(ranked, 1):
                scores[hit.id] += 1 / (RRF_K + rank)
                seen = best.get(hit.id)
                best[hit.id] = (
                    hit
                    if seen is None
                    else replace(seen, snippet=seen.snippet or hit.snippet, section=hit.section)
                )
        order = sorted(scores, key=lambda node_id: (-scores[node_id], node_id))[:limit]
        return [replace(best[i], score=round(scores[i], 6)) for i in order]

    def run(
        self,
        query: str,
        mode: SearchMode = SearchMode.AUTO,
        kinds: Iterable[NodeKind | str] = (),
        status: Status | DecisionStatus | str | None = None,
        plan: str | None = None,
        limit: int = 10,
    ) -> tuple[list[Hit], SearchMode]:
        if mode is SearchMode.AUTO:
            mode = SearchMode.HYBRID if self._has_index() else SearchMode.FTS
        method = {
            SearchMode.FTS: self.fts,
            SearchMode.SEMANTIC: self.semantic,
            SearchMode.HYBRID: self.hybrid,
        }[mode]
        return self._with_snippets(method(query, kinds, status, plan, limit)), mode

    def _has_index(self) -> bool:
        if self.provider is None:
            return False
        with self.db.get_state_connection() as conn:
            try:
                self._table_dimensions(conn)
            except SearchError:
                return False
            return bool(conn.execute("SELECT 1 FROM vec_nodes LIMIT 1").fetchone())

    def _with_snippets(self, hits: list[Hit]) -> list[Hit]:
        """A semantic hit has no matched text, so it shows the start of the section it matched."""
        with self.db.get_state_connection() as conn:
            out = []
            for hit in hits:
                if not hit.snippet:
                    row = conn.execute(
                        "SELECT content FROM node_sections WHERE node_id = ? AND section_key = ?",
                        (hit.id, hit.section or ""),
                    ).fetchone()
                    text = " ".join((row[0] if row else hit.title).split())
                    hit = replace(hit, snippet=text[:160])
                out.append(hit)
            return out
