"""Integration tests for `GET /api/waves`: the wave simulator served from live state."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from taskmanager.cli.main import app as cli_app
from taskmanager.core.models import Lease
from taskmanager.core.status import Action, Status
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.di.container import create_container
from taskmanager.engine.config import DISPATCH_DEFAULTS, ConfigStore
from taskmanager.engine.operations import Operations
from taskmanager.engine.snapshot import SnapshotBuilder
from taskmanager.web.app import create_app

runner = CliRunner()
Web = tuple[TestClient, Path]


@pytest.fixture
def web(tmp_path: Path) -> Web:
    DatabaseManager(tmp_path / ".taskmanager").init_all()
    return TestClient(create_app(tmp_path)), tmp_path


def _claimable_task(root: Path, spec_slug: str, task_slug: str, *, priority: int = 50) -> str:
    ops = create_container(root).get(Operations)
    spec = ops.add_spec(spec_slug, slug=spec_slug)
    plan = ops.add_plan(spec_slug, spec, slug="P", review=False, fix=False)
    task_id = ops.add_task(task_slug, plan, slug=task_slug, priority=priority)
    ops.update_node(task_id, repo="api")
    return task_id


def test_get_meta_answers_dispatch_from_config(web: Web) -> None:
    client, root = web
    body = client.get("/api/meta").json()
    assert body["dispatch"] == {
        "wave_size": DISPATCH_DEFAULTS["wave_size"],
        "tick_budget": DISPATCH_DEFAULTS["tick_budget"],
    }

    ConfigStore(root).set("dispatch.wave_size", "5")
    ConfigStore(root).set("dispatch.tick_budget", "5")
    body = client.get("/api/meta").json()
    assert body["dispatch"]["tick_budget"] == 5


def test_depth_outside_1_20_answers_400_naming_the_bound(web: Web) -> None:
    client, _root = web
    for depth in (0, 21):
        res = client.get("/api/waves", params={"depth": depth})
        assert res.status_code == 400
        assert "1..20" in res.json()["detail"]


def test_waves_answer_the_depth_cap_the_page_stops_compute_at(web: Web) -> None:
    client, _root = web
    body = client.get("/api/waves").json()
    assert body["max_depth"] == 20
    assert client.get("/api/waves", params={"depth": body["max_depth"]}).status_code == 200
    assert client.get("/api/waves", params={"depth": body["max_depth"] + 1}).status_code == 400


def test_size_outside_1_tick_budget_answers_400_naming_the_configured_bound(web: Web) -> None:
    client, root = web
    ConfigStore(root).set("dispatch.wave_size", "5")
    ConfigStore(root).set("dispatch.tick_budget", "5")

    res = client.get("/api/waves", params={"size": 6})

    assert res.status_code == 400
    assert res.json()["detail"] == "wave size must be 1–5 (this project's dispatch.tick_budget)."


def test_size_defaults_to_the_project_wave_size(web: Web) -> None:
    client, root = web
    _claimable_task(root, "S", "T", priority=90)
    ConfigStore(root).set("dispatch.wave_size", "1")
    ConfigStore(root).set("dispatch.tick_budget", "1")

    body = client.get("/api/waves").json()

    assert [e["id"] for e in body["waves"][0]["entries"]] == ["S-P-T"]


def test_wave_one_matches_wave_discover_for_the_same_state(web: Web) -> None:
    client, root = web
    _claimable_task(root, "SA", "A", priority=90)
    _claimable_task(root, "SB", "B", priority=10)

    api_entries = client.get("/api/waves", params={"size": 1}).json()["waves"][0]["entries"]

    res = runner.invoke(
        cli_app,
        [
            "wave",
            "discover",
            "--session",
            "sess",
            "--slots",
            "1",
            "--max-strong",
            "5",
            "--path",
            str(root),
        ],
    )
    chosen = json.loads(res.stdout.splitlines()[0])["chosen"]

    assert [e["id"] for e in api_entries] == [c["id"] for c in chosen]
    assert [e["action"] for e in api_entries] == [c["action"] for c in chosen]
    assert [e["model"] for e in api_entries] == [c["model"] for c in chosen]


def test_spec_scopes_like_wave_discover_spec(web: Web) -> None:
    client, root = web
    _claimable_task(root, "S1", "A")
    _claimable_task(root, "S2", "B")

    body = client.get("/api/waves", params={"spec": "S1"}).json()

    assert [e["id"] for e in body["waves"][0]["entries"]] == ["S1-P-A"]


def test_a_call_builds_the_snapshot_once_however_deep(
    web: Web, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, root = web
    _claimable_task(root, "S", "A")
    calls: list[None] = []
    original = SnapshotBuilder.build

    def counted(self: SnapshotBuilder) -> object:
        calls.append(None)
        return original(self)

    monkeypatch.setattr(SnapshotBuilder, "build", counted)

    res = client.get("/api/waves", params={"depth": 5})

    assert res.status_code == 200
    assert len(res.json()["waves"]) == 5
    assert len(calls) == 1


def test_waves_name_each_held_node_and_the_lease_an_in_flight_step_runs_under(web: Web) -> None:
    client, root = web
    running = _claimable_task(root, "S", "A")
    waiting = _claimable_task(root, "S2", "B")
    ops = create_container(root).get(Operations)
    ops.set_dependencies(waiting, [running], [])
    node_repo = NodeRepository(DatabaseManager(root / ".taskmanager"))
    node = node_repo.get_node(running)
    assert node is not None
    node.status, node.claimed_from = Status.IMPLEMENTING, Status.READY
    heartbeat = datetime.now(tz=UTC)
    lease = Lease(
        task_id=running,
        agent_id="wf-a",
        session_id="s",
        branch_name=f"tm/{running}",
        action=Action.IMPLEMENT,
        last_heartbeat=heartbeat,
    )
    assert RuntimeRepository(node_repo.db).claim(lease, [], node)

    body = client.get("/api/waves").json()

    entry = next(e for e in body["waves"][0]["entries"] if e["id"] == running)
    assert entry["in_flight"]
    assert body["nodes"][running]["lease"]["agent_id"] == "wf-a"
    assert datetime.fromisoformat(body["nodes"][running]["lease"]["last_heartbeat"]) == heartbeat
    assert any(h.startswith(f"{waiting}: ") for h in body["waves"][0]["held"])
    assert body["nodes"][waiting] == {
        "kind": "task",
        "title": "B",
        "display": "BLOCKED_BY_TASK",
        "lease": None,
    }
