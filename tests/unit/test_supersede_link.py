import json
import signal
from collections.abc import Iterator
from pathlib import Path
from types import FrameType

import pytest
import yaml
from typer.testing import CliRunner

from taskmanager.cli.main import app
from taskmanager.core.enums import RelationType
from taskmanager.core.models import NodeRelation, NodeSection
from taskmanager.core.status import Status
from taskmanager.db.cache_repo import CacheRepository
from taskmanager.db.job_repo import JobRepository
from taskmanager.db.node_repo import NodeRepository
from taskmanager.di.container import create_container
from taskmanager.engine.operations import Operations
from taskmanager.engine.snapshot import DisplayView, display_view
from taskmanager.web.bodies import BodyRepos, build_bodies
from taskmanager.web.rows import build_rows, statuses

runner = CliRunner()
OLD, NEW, NEXT = "S-P-old", "S-P-new", "S-P-next"


def tm(*args: str) -> str:
    res = runner.invoke(app, list(args))
    assert res.exit_code == 0, res.output
    return res.output


def get(root: Path, node_id: str) -> dict[str, object]:
    doc: dict[str, object] = yaml.safe_load(tm("task", "get", node_id, "--yaml", "-C", str(root)))
    return doc


def set_status(root: Path, node_id: str, status: Status) -> None:
    repo = create_container(root).get(NodeRepository)
    node = repo.get_node(node_id)
    assert node is not None
    repo.save_node(node.model_copy(update={"status": status}))


@pytest.fixture
def root(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    tm("init", "-C", str(source))
    ops = create_container(source).get(Operations)
    plan = ops.add_plan("plan", ops.add_spec("spec", slug="S"), slug="P")
    for slug in ("old", "new", "next"):
        ops.add_task(slug, plan, slug=slug)
    ops.node_repo.save_section(
        NodeSection(
            node_id=OLD, section_key="deferral", ordinal=1, header="## Deferral", content="later"
        )
    )
    ops.supersede(OLD, NEW)
    return source


@pytest.fixture
def no_hang() -> Iterator[None]:
    def expire(signum: int, frame: FrameType | None) -> None:
        raise TimeoutError("following the supersede chain did not end")

    previous = signal.signal(signal.SIGALRM, expire)
    signal.alarm(10)
    yield
    signal.alarm(0)
    signal.signal(signal.SIGALRM, previous)


def test_task_get_names_the_replacement_and_its_status(root: Path) -> None:
    assert get(root, OLD)["superseded_by"] == {"id": NEW, "status": "READY"}
    fields = tm("task", "get", OLD, "--json", "--fields", "superseded_by", "-C", str(root))
    assert json.loads(fields) == {"superseded_by": {"id": NEW, "status": "READY"}}
    assert f"Superseded by: {NEW} (READY)" in tm("task", "get", OLD, "-C", str(root))


def test_the_replacement_status_follows_it_to_completed(root: Path) -> None:
    set_status(root, NEW, Status.COMPLETED)

    assert get(root, OLD)["superseded_by"] == {"id": NEW, "status": "COMPLETED"}


def test_a_node_nothing_replaced_has_no_replacement(root: Path) -> None:
    assert get(root, NEW)["superseded_by"] is None
    assert "Superseded by" not in tm("task", "get", NEW, "-C", str(root))


def test_a_supersedes_link_on_a_node_not_superseded_names_nothing(root: Path) -> None:
    repo = create_container(root).get(NodeRepository)
    repo.add_relation(
        NodeRelation(source_id=NEXT, target_id=NEW, relation_type=RelationType.SUPERSEDES)
    )

    assert get(root, NEW)["superseded_by"] is None
    assert get(root, OLD)["superseded_by"] == {"id": NEW, "status": "READY"}


@pytest.mark.parametrize(
    ("view", "title"), [("full", "# old"), ("summary", "# old"), ("subagent", "# Task Brief: old")]
)
def test_every_render_names_the_replacement_under_the_title_before_any_section(
    root: Path, view: str, title: str
) -> None:
    out = tm("render", OLD, "--view", view, "-C", str(root))

    assert f"{title}\n\nSuperseded by {NEW} (READY)\n" in out
    if view != "summary":
        assert out.index("Superseded by") < out.index("## Deferral")


def test_a_render_of_a_node_nothing_replaced_has_no_superseded_line(root: Path) -> None:
    assert "Superseded by" not in tm("render", NEW, "-C", str(root))


def test_a_render_of_a_node_not_superseded_builds_no_display_view(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(_: NodeRepository) -> DisplayView:
        raise AssertionError("a whole-graph read for a node that is not superseded")

    monkeypatch.setattr("taskmanager.renderers.markdown.display_view", refuse)

    tm("render", NEW, "-C", str(root))


def test_a_chain_reports_its_last_node(root: Path) -> None:
    create_container(root).get(Operations).supersede(NEW, NEXT)
    set_status(root, NEXT, Status.COMPLETED)

    assert get(root, OLD)["superseded_by"] == {"id": NEXT, "status": "COMPLETED"}
    assert get(root, NEW)["superseded_by"] == {"id": NEXT, "status": "COMPLETED"}
    assert f"Superseded by {NEXT} (COMPLETED)" in tm("render", OLD, "-C", str(root))


@pytest.mark.usefixtures("no_hang")
def test_a_cycle_stops_at_the_last_node_before_it_repeats(root: Path) -> None:
    repo = create_container(root).get(NodeRepository)
    repo.add_relation(
        NodeRelation(source_id=OLD, target_id=NEW, relation_type=RelationType.SUPERSEDES)
    )
    set_status(root, NEW, Status.SUPERSEDED)

    assert get(root, OLD)["superseded_by"] == {"id": NEW, "status": "SUPERSEDED"}
    assert get(root, NEW)["superseded_by"] == {"id": OLD, "status": "SUPERSEDED"}


def test_export_then_restore_into_a_fresh_root_keeps_the_link(root: Path, tmp_path: Path) -> None:
    exported, fresh = tmp_path / "export", tmp_path / "fresh"
    fresh.mkdir()
    tm("export", str(exported), "-C", str(root))
    tm("restore", str(exported), "-C", str(fresh))

    restored = create_container(fresh).get(NodeRepository)
    assert restored.relations(RelationType.SUPERSEDES) == [(NEW, OLD)]
    assert get(fresh, OLD)["superseded_by"] == {"id": NEW, "status": "READY"}


def view(root: Path) -> DisplayView:
    return display_view(create_container(root).get(NodeRepository))


def plan_counts(root: Path) -> dict[str, int]:
    [spec] = [e for e in statuses(build_rows(view(root))) if e["spec"] == "S"]
    [plan] = [p for p in spec["plans"] if p["plan"] == "S-P"]
    counts: dict[str, int] = plan["counts"]
    return counts


def test_the_row_and_the_body_name_the_replacement(root: Path) -> None:
    current = view(root)
    repo = create_container(root).get(NodeRepository)
    repos = BodyRepos(repo, JobRepository(repo.db), CacheRepository(repo.db), 0, root)
    rows = build_rows(current)
    bodies = build_bodies(current, [OLD, NEW], repos=repos)

    assert rows[OLD]["superseded_by"] == {"id": NEW, "status": "READY"}
    assert rows[NEW]["superseded_by"] is None
    assert bodies[OLD]["node"]["superseded_by"] == {"id": NEW, "status": "READY"}
    assert bodies[NEW]["node"]["superseded_by"] is None


def test_a_superseded_task_counts_as_completed_only_once_its_replacement_is(root: Path) -> None:
    assert plan_counts(root) == {"SUPERSEDED": 1, "READY": 2}

    set_status(root, NEW, Status.COMPLETED)

    assert plan_counts(root) == {"COMPLETED": 2, "READY": 1}
