import json
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from taskmanager import __version__
from taskmanager.cli.main import app
from taskmanager.core.enums import (
    EmbeddingProviderType,
    NodeKind,
    RelationType,
    SearchMode,
)
from taskmanager.core.models import Node, NodeRelation, NodeSection
from taskmanager.core.status import Outcome, Status
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.node_repo import NodeRepository
from taskmanager.engine.config import EmbeddingsConfig
from taskmanager.engine.search import (
    MockEmbeddingProvider,
    SearchEngine,
    SearchError,
    chunks,
    fts_query,
)

runner = CliRunner()
DIMS = 8


class CountingProvider:
    def __init__(self, dimensions: int = DIMS) -> None:
        self.inner = MockEmbeddingProvider(dimensions)
        self.texts: list[str] = []

    def get_embedding(self, text: str) -> list[float]:
        self.texts.append(text)
        return self.inner.get_embedding(text)


@dataclass
class Kit:
    engine: SearchEngine
    repo: NodeRepository
    provider: CountingProvider
    db: DatabaseManager

    def node(self, node_id: str, title: str, kind: NodeKind = NodeKind.TASK, **kw: object) -> None:
        self.repo.save_node(Node(id=node_id, kind=kind, title=title, **kw))  # type: ignore[arg-type]

    def section(self, node_id: str, key: str, content: str, ordinal: int = 0) -> None:
        self.repo.save_section(
            NodeSection(
                node_id=node_id, section_key=key, ordinal=ordinal, header=key, content=content
            )
        )

    def contains(self, parent: str, child: str) -> None:
        self.repo.add_relation(
            NodeRelation(source_id=parent, target_id=child, relation_type=RelationType.CONTAINS)
        )

    def vectors(self) -> list[tuple[str, str, str]]:
        with self.db.get_state_connection() as conn:
            return conn.execute(
                "SELECT node_id, target_type, section_key FROM vec_nodes ORDER BY rowid"
            ).fetchall()


def make_kit(tmp_path: Path, dims: int = DIMS) -> Kit:
    db = DatabaseManager(tmp_path / ".taskmanager")
    db.init_all(vector_dimensions=dims)
    provider = CountingProvider(dims)
    settings = EmbeddingsConfig(provider=EmbeddingProviderType.MOCK, dimensions=dims)
    return Kit(SearchEngine(db, provider, settings), NodeRepository(db), provider, db)


@pytest.fixture
def kit(tmp_path: Path) -> Kit:
    return make_kit(tmp_path)


def test_fts_ranks_a_title_match_above_a_body_only_match(kit: Kit) -> None:
    kit.node("T-body", "Cleanup")
    kit.section("T-body", "notes", "rotate tokens, rotate keys, rotate secrets")
    kit.node("T-title", "Rotate the signing keys")
    hits = kit.engine.fts("rotate")
    assert [h.id for h in hits] == ["T-title", "T-body"]
    assert hits[0].score > hits[1].score
    assert "**rotate**" in hits[1].snippet


@pytest.mark.parametrize(
    "query",
    [
        "",
        "   ",
        "\t\n",
        "AND",
        "OR",
        "NOT",
        "NEAR",
        "NEAR(a b)",
        "a AND",
        "OR a",
        "a OR OR b",
        "*",
        "a*",
        '"',
        '""',
        '"unbalanced',
        'say "hi" "there',
        "(",
        ")",
        "(a",
        "-",
        "-a",
        "a-b",
        "a:",
        ":",
        "title:a",
        "^a",
        "'",
        "a'b",
        "{a}",
        "\\",
        "%",
    ],
)
def test_the_users_text_is_data_and_never_a_syntax_error(kit: Kit, query: str) -> None:
    kit.node("T-1", "a b and or near auth")
    kit.engine.fts(query)
    kit.engine.run(query, SearchMode.FTS)


def test_the_words_of_a_query_are_or_ed_and_a_quoted_phrase_stays_one_phrase() -> None:
    assert fts_query('rotate "signing keys" -x') == '"rotate" OR "signing keys" OR "-x"'
    assert fts_query('say "hi') == '"say" OR """hi"'
    assert fts_query('   "" ') == ""


def test_a_quoted_phrase_matches_only_the_words_in_that_order(kit: Kit) -> None:
    kit.node("T-in-order", "Rotate signing keys")
    kit.node("T-reversed", "Keys are signing, rotate them")
    assert {h.id for h in kit.engine.fts("rotate keys")} == {"T-in-order", "T-reversed"}
    assert [h.id for h in kit.engine.fts('"signing keys"')] == ["T-in-order"]


def test_a_word_that_is_an_fts_operator_still_matches_as_a_word(kit: Kit) -> None:
    kit.node("T-1", "Rate limit and quota")
    assert [h.id for h in kit.engine.fts("and")] == ["T-1"]


def test_filters_apply_before_the_limit(kit: Kit) -> None:
    kit.node("S1", "deploy spec", NodeKind.SPEC)
    kit.node("S1-P1", "deploy plan one", NodeKind.PLAN)
    kit.node("S1-P2", "plan two", NodeKind.PLAN)
    for n in range(4):
        kit.node(f"S1-P1-t{n}", "deploy deploy deploy", status=Status.READY)
        kit.contains("S1-P1", f"S1-P1-t{n}")
    kit.node("S1-P2-late", "deploy once", status=Status.REVIEWED, outcome=Outcome.APPROVE)
    kit.contains("S1-P2", "S1-P2-late")

    def ids(**kw: object) -> list[str]:
        return [h.id for h in kit.engine.fts("deploy", limit=1, **kw)]  # type: ignore[arg-type]

    assert ids() != ["S1-P2-late"]
    assert ids(plan="S1-P2") == ["S1-P2-late"]
    assert ids(status=Status.REVIEWED) == ["S1-P2-late"]
    assert ids(kinds=[NodeKind.SPEC]) == ["S1"]
    assert ids(kinds=[NodeKind.PLAN]) == ["S1-P1"]
    assert kit.engine.fts("deploy", plan="S1-P2", limit=1)[0].plan == "S1-P2"


def test_the_vector_search_filters_before_the_limit_too(kit: Kit) -> None:
    kit.node("S1-P1", "one", NodeKind.PLAN)
    kit.node("S1-P2", "two", NodeKind.PLAN)
    for n in range(3):
        kit.node(f"near-{n}", "x")
        kit.contains("S1-P1", f"near-{n}")
        kit.engine.index_node(f"near-{n}", [1.0] + [0.0] * (DIMS - 1))
    kit.node("far", "y", status=Status.COMPLETED)
    kit.contains("S1-P2", "far")
    kit.engine.index_node("far", [0.0, 1.0] + [0.0] * (DIMS - 2))
    query = [1.0] + [0.0] * (DIMS - 1)
    assert kit.engine.search("", query, plan="S1-P2", limit=1)[0]["node_id"] == "far"
    assert kit.engine.search("", query, statuses=["COMPLETED"], limit=1)[0]["node_id"] == "far"
    assert kit.engine.search("", query, limit=1)[0]["node_id"].startswith("near-")


def test_hybrid_fuses_both_rankings_by_reciprocal_rank(kit: Kit) -> None:
    for node_id, title in [
        ("T-a", "alpha login"),
        ("T-b", "beta login"),
        ("T-c", "gamma"),
        ("T-d", "login alpha"),
        ("T-e", "login"),
    ]:
        kit.node(node_id, title)
    kit.engine.index()

    assert [h.id for h in kit.engine.fts("login")] == ["T-e", "T-a", "T-b", "T-d"]
    assert [h.id for h in kit.engine.semantic("login")] == ["T-e", "T-b", "T-d", "T-c", "T-a"]

    hits = kit.engine.hybrid("login")
    assert [h.id for h in hits] == ["T-e", "T-b", "T-a", "T-d", "T-c"]
    assert [h.score for h in hits] == [
        pytest.approx(1 / 61 + 1 / 61, abs=1e-6),
        pytest.approx(1 / 63 + 1 / 62, abs=1e-6),
        pytest.approx(1 / 62 + 1 / 65, abs=1e-6),
        pytest.approx(1 / 64 + 1 / 63, abs=1e-6),
        pytest.approx(1 / 64, abs=1e-6),
    ]
    assert [h.id for h in kit.engine.hybrid("login", limit=2)] == ["T-e", "T-b"]


def test_indexing_skips_unchanged_text_and_re_embeds_only_the_changed_section(kit: Kit) -> None:
    kit.node("T-1", "Title")
    kit.section("T-1", "a", "first body", 0)
    kit.section("T-1", "b", "second body", 1)

    first = kit.engine.index()
    assert (first.embedded, first.unchanged) == (3, 0)
    assert len(kit.provider.texts) == 3

    again = kit.engine.index()
    assert (again.embedded, again.unchanged) == (0, 3)
    assert len(kit.provider.texts) == 3

    kit.section("T-1", "b", "second body, edited", 1)
    changed = kit.engine.index()
    assert (changed.embedded, changed.unchanged) == (1, 2)
    assert kit.provider.texts[3:] == ["b\nsecond body, edited"]
    assert kit.engine.status()["stale_nodes"] == 0


def test_a_node_with_two_sections_keeps_two_vectors(kit: Kit) -> None:
    kit.node("T-1", "Title")
    kit.engine.index_node("T-1", [1.0] + [0.0] * (DIMS - 1), "section", "overview")
    kit.engine.index_node("T-1", [0.0, 1.0] + [0.0] * (DIMS - 2), "section", "design")
    assert kit.vectors() == [("T-1", "section", "overview"), ("T-1", "section", "design")]

    kit.engine.index_node("T-1", [0.0, 0.0, 1.0] + [0.0] * (DIMS - 3), "section", "design")
    assert kit.vectors() == [("T-1", "section", "overview"), ("T-1", "section", "design")]


def test_a_section_is_embedded_and_kept_as_one_vector_per_chunk(kit: Kit) -> None:
    paragraphs = [f"paragraph {n} " + "word " * 100 for n in range(8)]
    kit.node("T-1", "Title")
    kit.section("T-1", "long", "\n\n".join(paragraphs))
    kit.engine.index()
    held = [v for v in kit.vectors() if v[2] == "long"]
    assert len(held) == len(kit.provider.texts) - 1 > 1
    assert kit.engine.semantic("paragraph 3")[0].id == "T-1"
    assert kit.engine.status()["chunks"] == len(kit.vectors())


def test_chunks_break_on_paragraph_boundaries_near_the_size() -> None:
    text = "\n\n".join(["x" * 600] * 5)
    packed = chunks(text, 1500)
    assert packed == ["\n\n".join(["x" * 600] * 2)] * 2 + ["x" * 600]
    assert chunks("y" * 3200, 1500) == ["y" * 1500, "y" * 1500, "y" * 200]
    assert chunks("  \n\n ") == []


def test_indexing_prunes_what_no_longer_exists(kit: Kit) -> None:
    kit.node("T-1", "Title")
    kit.section("T-1", "a", "body")
    kit.engine.index()
    with kit.db.get_state_connection() as conn:
        conn.execute("DELETE FROM node_sections WHERE node_id = 'T-1'")
        conn.commit()
    report = kit.engine.index()
    assert report.removed == 1
    assert kit.vectors() == [("T-1", "title", "")]


def test_kind_limits_what_is_indexed(kit: Kit) -> None:
    kit.node("S1", "spec", NodeKind.SPEC)
    kit.node("T-1", "task")
    assert kit.engine.index([NodeKind.SPEC]).embedded == 1
    assert kit.vectors() == [("S1", "title", "")]


def test_a_new_dimension_needs_a_rebuild_and_says_so(tmp_path: Path) -> None:
    kit = make_kit(tmp_path)
    kit.node("T-1", "alpha")
    kit.engine.index()

    wider = SearchEngine(
        kit.db,
        MockEmbeddingProvider(16),
        EmbeddingsConfig(provider=EmbeddingProviderType.MOCK, dimensions=16),
    )
    for call in (wider.index, lambda: wider.semantic("alpha"), lambda: wider.hybrid("alpha")):
        with pytest.raises(SearchError, match="tm index --rebuild"):
            call()

    assert wider.index(rebuild=True).embedded == 1
    assert wider.status()["dimensions"] == 16
    assert wider.semantic("alpha")[0].id == "T-1"


def test_a_changed_provider_or_model_needs_a_rebuild(kit: Kit) -> None:
    kit.node("T-1", "alpha")
    kit.engine.index()
    other = SearchEngine(
        kit.db,
        kit.provider,
        EmbeddingsConfig(provider=EmbeddingProviderType.MOCK, dimensions=DIMS, model="other"),
    )
    with pytest.raises(SearchError, match="mock/mock/8.*other.*tm index --rebuild"):
        other.semantic("alpha")


def test_semantic_search_without_a_provider_or_an_index_says_what_to_do(kit: Kit) -> None:
    kit.node("T-1", "alpha")
    bare = SearchEngine(kit.db, None, EmbeddingsConfig())
    for mode in (SearchMode.SEMANTIC, SearchMode.HYBRID):
        with pytest.raises(SearchError, match="no embedding provider configured"):
            bare.run("alpha", mode)
        with pytest.raises(SearchError, match="vector index is empty"):
            kit.engine.run("alpha", mode)
    with pytest.raises(SearchError, match="no embedding provider configured"):
        bare.index()


def test_auto_is_hybrid_only_with_a_provider_and_an_index(kit: Kit) -> None:
    kit.node("T-1", "alpha")
    assert kit.engine.run("alpha")[1] is SearchMode.FTS
    kit.engine.index()
    hits, used = kit.engine.run("alpha")
    assert used is SearchMode.HYBRID and hits[0].id == "T-1"
    bare = SearchEngine(kit.db, None, EmbeddingsConfig())
    assert bare.run("alpha")[1] is SearchMode.FTS


def test_a_semantic_hit_shows_the_start_of_the_section_it_matched(kit: Kit) -> None:
    kit.node("T-1", "Title")
    kit.section("T-1", "notes", "the  quick\nbrown fox")
    kit.engine.index()
    hit = kit.engine.run("notes\nthe  quick\nbrown fox", SearchMode.SEMANTIC)[0][0]
    assert (hit.section, hit.snippet) == ("notes", "the quick brown fox")


def test_a_search_in_a_stale_index_still_answers_and_status_counts_it(kit: Kit) -> None:
    kit.node("T-1", "alpha")
    kit.engine.index()
    kit.node("T-1", "alpha changed")
    kit.node("T-2", "new")
    assert kit.engine.status()["stale_nodes"] == 2
    assert kit.engine.semantic("alpha")[0].id == "T-1"


def _cli(root: Path, *args: str) -> tuple[int, str]:
    # Everything after `--` is a query word, so -C has to come before it.
    cut = args.index("--") if "--" in args else len(args)
    res = runner.invoke(app, [*args[:cut], "-C", str(root), *args[cut:]])
    return res.exit_code, res.output


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in ("TASKMANAGER_OPENAI_API_KEY", "TASKMANAGER_OPENAI_BASE_URL", "TM_ROOT"):
        monkeypatch.delenv(name, raising=False)
    _cli(tmp_path, "init")
    _cli(tmp_path, "spec", "add", "Auth spec", "--slug", "S1")
    _cli(tmp_path, "plan", "add", "Login plan", "--spec", "S1", "--slug", "P1")
    _cli(tmp_path, "task", "add", "Rotate signing keys", "--plan", "S1-P1", "--slug", "keys")
    _cli(tmp_path, "task", "add", "Write docs", "--plan", "S1-P1", "--slug", "docs")
    _cli(tmp_path, "section", "set", "S1-P1-docs:body", "explain how keys rotate")
    return tmp_path


def test_search_prints_text_by_default_and_names_the_mode_on_the_last_line(project: Path) -> None:
    code, out = _cli(project, "search", "rotate", "keys")
    lines = out.strip().splitlines()
    assert code == 0
    assert lines[0].startswith("S1-P1-keys  task  READY plan S1-P1")
    assert lines[-1] == "2 results, mode: fts"

    _cli(project, "config", "set", "embeddings.provider", "mock")
    _cli(project, "config", "set", "embeddings.dimensions", "16")
    assert _cli(project, "index")[0] == 0
    code, out = _cli(project, "search", "rotate", "keys")
    assert out.strip().splitlines()[-1].endswith("mode: hybrid")
    assert "mode: fts" in _cli(project, "search", "--mode", "fts", "keys")[1]


def test_search_json_carries_every_field_and_filters_narrow_it(project: Path) -> None:
    code, out = _cli(project, "search", "keys", "--json", "--kind", "task", "--plan", "S1-P1")
    doc = json.loads(out)
    assert code == 0 and doc["mode"] == "fts"
    assert {h["id"] for h in doc["results"]} == {"S1-P1-keys", "S1-P1-docs"}
    assert set(doc["results"][0]) == {
        "id",
        "kind",
        "title",
        "status",
        "plan",
        "score",
        "snippet",
        "section",
    }
    none = json.loads(_cli(project, "search", "keys", "--json", "--status", "COMPLETED")[1])
    assert none["results"] == []
    assert "mode: fts" in _cli(project, "search", "keys", "--yaml")[1]
    assert _cli(project, "search", "keys", "--limit", "1", "--json")[0] == 0


@pytest.mark.parametrize("query", ["-", '"', "AND", "a:", "(", ""])
def test_search_of_hostile_text_exits_zero(project: Path, query: str) -> None:
    code, out = _cli(project, "search", "--mode", "fts", "--", query)
    assert code == 0 and "Traceback" not in out and out.strip().endswith("mode: fts")


def test_semantic_search_and_index_without_a_provider_exit_1_with_the_hint(project: Path) -> None:
    for args in (("search", "--mode", "semantic", "keys"), ("index",)):
        code, out = _cli(project, *args)
        assert code == 1 and "Traceback" not in out
        assert (
            out.strip()
            == "no embedding provider configured: `tm config set embeddings.provider <local|openai>`"
        )


def test_index_status_reports_provider_size_and_staleness(project: Path) -> None:
    _cli(project, "config", "set", "embeddings.provider", "mock")
    _cli(project, "config", "set", "embeddings.dimensions", "16")
    _cli(project, "index")
    _cli(project, "section", "set", "S1-P1-docs:body", "changed text")
    code, out = _cli(project, "index", "--status")
    assert code == 0
    assert "provider: mock" in out and "dimensions: 16" in out
    assert "nodes_indexed: 4" in out and "chunks: 5" in out and "stale_nodes: 1" in out
    code, out = _cli(project, "index")
    assert "Indexed 1 items: 4 unchanged, 0 removed" in out


def test_index_prints_progress_counts_not_one_line_per_chunk(project: Path) -> None:
    _cli(project, "config", "set", "embeddings.provider", "mock")
    _cli(project, "config", "set", "embeddings.dimensions", "16")
    out = _cli(project, "index")[1].strip().splitlines()
    assert out == ["embedded 5/5", "Indexed 5 items: 0 unchanged, 0 removed"]


def test_a_provider_failure_is_one_line_and_exit_1(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(self: httpx.Client, url: str, **kwargs: object) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx.Client, "post", refuse)
    _cli(project, "config", "set", "embeddings.provider", "openai")
    _cli(project, "config", "set", "embeddings.base_url", "http://embeddings.invalid/v1")
    code, out = _cli(project, "index")
    assert code == 1 and "Traceback" not in out
    assert len(out.strip().splitlines()) == 1
    assert "http://embeddings.invalid/v1" in out and "connection refused" in out


def test_the_openai_provider_reads_the_key_from_the_variable_the_config_names(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[dict[str, object]] = []

    def answer(self: httpx.Client, url: str, **kwargs: object) -> httpx.Response:
        seen.append({"url": url, **kwargs})
        return httpx.Response(
            200, json={"data": [{"embedding": [0.5] * 4}]}, request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(httpx.Client, "post", answer)
    monkeypatch.setenv("MY_EMBED_KEY", "sekret-value")
    for key, value in [
        ("embeddings.provider", "openai"),
        ("embeddings.api_key_env", "MY_EMBED_KEY"),
        ("embeddings.base_url", "http://local.test/v1"),
        ("embeddings.model", "nomic"),
        ("embeddings.dimensions", "4"),
    ]:
        _cli(project, "config", "set", key, value)
    code, out = _cli(project, "index")
    assert code == 0, out
    assert seen[0]["url"] == "http://local.test/v1/embeddings"
    assert seen[0]["headers"] == {"Authorization": "Bearer sekret-value"}
    assert seen[0]["json"] == {"input": "Auth spec", "model": "nomic"}
    assert "sekret-value" not in (project / ".taskmanager" / "config.yaml").read_text()
    assert "sekret-value" not in out


def test_a_provider_returning_the_wrong_size_names_the_setting(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def answer(self: httpx.Client, url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(
            200, json={"data": [{"embedding": [0.5] * 3}]}, request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(httpx.Client, "post", answer)
    _cli(project, "config", "set", "embeddings.provider", "openai")
    _cli(project, "config", "set", "embeddings.dimensions", "4")
    code, out = _cli(project, "index")
    assert code == 1 and "returned 3 dimensions" in out and "embeddings.dimensions" in out


def test_a_missing_sentence_transformers_prints_the_install_hint(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)
    _cli(project, "config", "set", "embeddings.provider", "local")
    code, out = _cli(project, "index")
    assert code == 1 and "Traceback" not in out
    assert (
        "sentence-transformers is not installed: uv tool install --reinstall "
        f"'taskmanager[local-embeddings] @ git+https://github.com/TaigoPedrosa/TaskManager@v{__version__}'"
    ) in out
    assert _cli(project, "index", "--status")[0] == 0


def test_an_old_vector_table_with_one_row_per_node_asks_for_a_rebuild(kit: Kit) -> None:
    with kit.db.get_state_connection() as conn:
        conn.execute("DROP TABLE vec_nodes")
        conn.execute(
            f"CREATE VIRTUAL TABLE vec_nodes USING vec0(node_id TEXT PRIMARY KEY, "
            f"target_type TEXT, section_key TEXT, embedding FLOAT[{DIMS}] DISTANCE_METRIC=cosine)"
        )
        conn.commit()
    kit.node("T-1", "alpha")
    kit.engine.index_node("T-1", [1.0] + [0.0] * (DIMS - 1))
    with pytest.raises(SearchError, match="one vector per node.*tm index --rebuild"):
        kit.engine.index()
    assert kit.engine.index(rebuild=True).embedded == 1


def test_search_refuses_a_status_that_is_not_one(project: Path) -> None:
    res = runner.invoke(app, ["search", "keys", "--status", "ready", "-C", str(project)])
    assert res.exit_code == 2 and "READY" in res.output
    code, out = _cli(project, "search", "keys", "--json", "--status", "READY")
    assert code == 0 and {h["id"] for h in json.loads(out)["results"]} == {
        "S1-P1-keys",
        "S1-P1-docs",
    }
