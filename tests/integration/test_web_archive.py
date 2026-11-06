import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from taskmanager.cli.main import app as cli_app
from taskmanager.core.enums import NodeKind, RelationType
from taskmanager.core.models import LedgerEvent, Node, NodeRelation
from taskmanager.core.status import Status
from taskmanager.db.cache_repo import CacheRepository
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.job_repo import JobRepository
from taskmanager.db.ledger_repo import LedgerRepository
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.di.container import create_container
from taskmanager.engine.config import ConfigError, ConfigStore
from taskmanager.engine.operations import Operations
from taskmanager.engine.snapshot import SnapshotBuilder
from taskmanager.web import live
from taskmanager.web.app import create_app
from taskmanager.web.live import LiveHub
from taskmanager.web.rows import statuses_hash
from taskmanager.web.static_export import export_static_html

runner = CliRunner()


def tm(root: Path, *args: str) -> None:
    res = runner.invoke(cli_app, [*args, "-C", str(root)])
    assert res.exit_code == 0, res.output


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, message: dict[str, Any]) -> None:
        self.sent.append(message)


class Estate:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.db = DatabaseManager(root / ".taskmanager")
        self.db.init_all()
        self.nodes = NodeRepository(self.db)
        self.ledger = LedgerRepository(self.db)

    def spec(self, spec_id: str, status: Status) -> None:
        parent: str | None = None
        for node_id, kind in (
            (spec_id, NodeKind.SPEC),
            (f"{spec_id}-plan", NodeKind.PLAN),
            (f"{spec_id}-task", NodeKind.TASK),
        ):
            self.nodes.save_node(Node(id=node_id, kind=kind, title=node_id, status=status))
            if parent is not None:
                self.nodes.add_relation(
                    NodeRelation(
                        source_id=parent, target_id=node_id, relation_type=RelationType.CONTAINS
                    )
                )
            parent = node_id

    def completed(self, spec_id: str, at: datetime) -> None:
        self.ledger.append(
            LedgerEvent(
                timestamp=at,
                actor_id="tm",
                command="rollup",
                target_id=spec_id,
                payload={"from": "READY", "to": "COMPLETED"},
            )
        )

    def hub(self, clock: Any) -> LiveHub:
        return LiveHub(
            snapshots=SnapshotBuilder(
                self.nodes, RuntimeRepository(self.db), JobRepository(self.db)
            ),
            cache=CacheRepository(self.db),
            node_repo=self.nodes,
            job_repo=JobRepository(self.db),
            assets_dir=self.db.taskmanager_dir / "assets",
            condition_ttl=lambda: 300,
            state_db=self.db.state_db,
            cache_db=self.db.cache_db,
            clock=clock,
        )


@pytest.fixture
def estate(tmp_path: Path) -> Estate:
    built = Estate(tmp_path)
    built.spec("OLD", Status.COMPLETED)
    built.completed("OLD", datetime.now(tz=UTC) - timedelta(days=4))
    built.spec("LIVE", Status.READY)
    tm(tmp_path, "spec", "archive", "OLD")
    return built


def _specs(entries: list[dict[str, Any]]) -> set[str | None]:
    return {entry["spec"] for entry in entries}


@pytest.mark.parametrize(
    ("query", "specs"),
    [("", {"LIVE"}), ("?archived=include", {"LIVE", "OLD"}), ("?archived=only", {"OLD"})],
)
def test_api_statuses_archived_mode_selects_specs(
    estate: Estate, query: str, specs: set[str]
) -> None:
    body = TestClient(create_app(estate.root)).get(f"/api/statuses{query}").json()

    assert _specs(body["statuses"]) == specs
    assert body["hash"] == statuses_hash(body["statuses"])


def test_the_web_leaves_a_finished_spec_hot_with_automatic_archival_off(tmp_path: Path) -> None:
    hot = Estate(tmp_path)
    hot.spec("OLD", Status.COMPLETED)
    hot.completed("OLD", datetime.now(tz=UTC) - timedelta(days=4))
    ConfigStore(tmp_path).set("archive.automatically_after_hours", "0")

    with TestClient(create_app(tmp_path)) as client:
        body = client.get("/api/statuses").json()

    assert _specs(body["statuses"]) == {"OLD"}
    assert hot.nodes.get_node("OLD") is not None


def test_config_refuses_a_negative_archive_window(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="archive.automatically_after_hours"):
        ConfigStore(tmp_path).set("archive.automatically_after_hours", "-1")


def test_api_statuses_unknown_archived_mode_is_refused(estate: Estate) -> None:
    response = TestClient(create_app(estate.root)).get("/api/statuses?archived=all")

    assert response.status_code == 400
    assert "archived must be one of exclude, include, only" in response.json()["detail"]


@pytest.mark.parametrize(
    ("query", "roots"),
    [("", ["LIVE"]), ("&archived=include", ["LIVE", "OLD"]), ("&archived=only", ["OLD"])],
)
def test_api_nodes_archived_mode_selects_roots(
    estate: Estate, query: str, roots: list[str]
) -> None:
    body = TestClient(create_app(estate.root)).get(f"/api/nodes?parent=root{query}").json()

    assert sorted(item["id"] for item in body["items"]) == roots


@pytest.mark.parametrize(
    ("filters", "specs", "facet_status"),
    [
        ({}, {"LIVE"}, {"READY": 1}),
        ({"archived": "include"}, {"LIVE", "OLD"}, {"READY": 1, "COMPLETED": 3}),
        ({"archived": "only"}, {"OLD"}, {"COMPLETED": 3}),
    ],
)
def test_ws_snapshot_archived_mode_selects_rows_statuses_and_facets(
    estate: Estate, filters: dict[str, str], specs: set[str], facet_status: dict[str, int]
) -> None:
    with (
        TestClient(create_app(estate.root)) as client,
        client.websocket_connect("/ws") as ws,
    ):
        ws.send_text(
            json.dumps(
                {"type": "subscribe", "id": 1, "filters": filters, "open": [], "reset": True}
            )
        )
        message = ws.receive_json()

    assert message["type"] == "snapshot"
    assert {row["id"] for row in message["rows"]} == specs
    assert _specs(message["statuses"]) == specs
    assert message["hash"] == statuses_hash(message["statuses"])
    assert message["facets"]["status"] == facet_status


def test_hub_moves_a_spec_into_the_archive_when_the_clock_passes_its_boundary(
    tmp_path: Path,
) -> None:
    estate = Estate(tmp_path)
    estate.spec("SOON", Status.COMPLETED)
    estate.spec("LIVE", Status.READY)
    now = [datetime(2026, 10, 8, 12, 0, tzinfo=UTC)]
    estate.completed("SOON", now[0] - timedelta(days=3) + timedelta(hours=1))
    hub = estate.hub(clock=lambda: now[0])
    socket = FakeSocket()
    archive_socket = FakeSocket()

    async def scenario() -> None:
        await hub.refresh()
        for sock, filters in ((socket, {}), (archive_socket, {"archived": "only"})):
            frame = {"type": "subscribe", "id": 1, "filters": filters, "open": [], "reset": True}
            await hub.handle_frame(hub.open_session(sock), json.dumps(frame))
        now[0] += timedelta(minutes=59)
        await hub.refresh()
        now[0] += timedelta(minutes=2)
        await hub.refresh()

    asyncio.run(scenario())

    snapshot, *updates = socket.sent
    assert {row["id"] for row in snapshot["rows"]} == {"LIVE", "SOON"}
    [update] = updates
    assert {"op": "drop", "id": "SOON"} in update["items"]
    assert {"op": "statuses", "spec": "SOON", "entry": None} in update["items"]
    assert update["hash"] == statuses_hash(hub.statuses)
    archive_snapshot, archive_update = archive_socket.sent
    assert archive_snapshot["rows"] == []
    assert {"op": "row", "row": hub.rows["SOON"]} in archive_update["items"]
    assert archive_update["hash"] == statuses_hash(hub.statuses_by_mode["only"])
    assert _specs(hub.statuses_by_mode["only"]) == {"SOON"}


def test_hub_rebuilds_when_only_the_ledger_records_a_completion(tmp_path: Path) -> None:
    estate = Estate(tmp_path)
    estate.spec("DONE", Status.COMPLETED)
    now = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
    hub = estate.hub(clock=lambda: now)

    async def scenario() -> None:
        await hub.refresh()
        estate.completed("DONE", now - timedelta(days=4))
        await hub.refresh()

    asyncio.run(scenario())

    assert hub.rows["DONE"]["archived"] is True
    assert _specs(hub.statuses) == set()


def test_static_export_carries_the_archive_flag_and_its_own_statuses(
    estate: Estate, tmp_path: Path
) -> None:
    html = export_static_html(estate.root, tmp_path / "out.html").read_text()
    data = json.loads(html.split("window.STATIC_DATA = ", 1)[1].split(";</script>", 1)[0])

    assert {node_id for node_id, row in data["rows"].items() if row["archived"]} == {
        "OLD",
        "OLD-plan",
        "OLD-task",
    }
    assert _specs(data["statuses"]) == {"LIVE"}
    assert _specs(data["archived_statuses"]) == {"OLD"}


def test_the_web_archive_reads_the_archive_store(tmp_path: Path) -> None:
    tm(tmp_path, "init")
    ops = create_container(tmp_path).get(Operations)
    spec = ops.add_spec("Finished", slug="S")
    plan = ops.add_plan("p", spec, slug="P", review=False, fix=False)
    task = ops.add_task("t", plan, slug="T")
    ops.add_spec("Current", slug="LIVE")
    tm(tmp_path, "task", "abandon", task, "--note", "dropped")
    tm(tmp_path, "spec", "archive", spec)
    client = TestClient(create_app(tmp_path))

    def ids(parent: str, mode: str) -> list[str]:
        body = client.get(f"/api/nodes?parent={parent}&archived={mode}").json()
        return sorted(item["id"] for item in body["items"])

    assert ids("root", "only") == [spec]
    assert ids(spec, "only") == [plan]
    assert ids("root", "exclude") == ["LIVE"]
    assert ids("root", "include") == sorted(["LIVE", spec])
    detail = client.get(f"/api/nodes/{task}").json()
    assert detail["node"]["id"] == task
    assert detail["sections"][0]["key"] == "abandonment"


def test_a_rebuild_with_no_boundary_passed_and_no_archive_change_skips_the_archive_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    estate = Estate(tmp_path)
    estate.spec("OLD", Status.COMPLETED)
    estate.spec("LIVE", Status.READY)
    now = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
    estate.completed("OLD", now - timedelta(days=4))
    hub = estate.hub(clock=lambda: now)
    calls = {"archive_due": 0, "cold_rows": 0}
    due, cold_rows = live.archive_due, live.archived_rows

    def counting_due(*args: Any) -> list[str]:
        calls["archive_due"] += 1
        return due(*args)

    def counting_rows(cold: Any) -> dict[str, dict[str, Any]]:
        calls["cold_rows"] += 1
        return cold_rows(cold)

    monkeypatch.setattr(live, "archive_due", counting_due)
    monkeypatch.setattr(live, "archived_rows", counting_rows)

    async def scenario() -> None:
        await hub.refresh()
        assert calls == {"archive_due": 1, "cold_rows": 1}
        tm(tmp_path, "spec", "add", "Another")
        await hub.refresh()

    asyncio.run(scenario())

    assert calls == {"archive_due": 1, "cold_rows": 1}
    assert hub.rows["OLD"]["archived"] is True
    assert "S1" in hub.rows


def test_a_spec_due_but_not_archived_is_tried_again_once_a_minute_not_on_every_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    estate = Estate(tmp_path)
    estate.spec("STUCK", Status.COMPLETED)
    clock = {"now": datetime(2026, 10, 8, 12, 0, tzinfo=UTC)}
    estate.completed("STUCK", clock["now"] - timedelta(days=4))
    hub = estate.hub(clock=lambda: clock["now"])
    tries: list[datetime] = []

    def blocked(_root: Path, now: datetime, *_: Any) -> list[str]:
        tries.append(now)
        return []

    monkeypatch.setattr(live, "archive_due", blocked)

    async def scenario() -> None:
        await hub.refresh()
        tm(tmp_path, "spec", "add", "Another")
        await hub.refresh()
        assert len(tries) == 1
        assert hub._deadline is not None
        assert hub._deadline <= clock["now"] + live.ARCHIVE_RETRY
        clock["now"] += live.ARCHIVE_RETRY
        await hub.refresh()

    asyncio.run(scenario())

    assert len(tries) == 2
    assert hub.rows["STUCK"]["archived"] is False


def test_a_rebuild_reads_the_archive_again_once_it_changes(tmp_path: Path) -> None:
    estate = Estate(tmp_path)
    estate.spec("LATER", Status.COMPLETED)
    now = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
    estate.completed("LATER", now)
    ConfigStore(tmp_path).set("archive.automatically_after_hours", "0")
    hub = estate.hub(clock=lambda: now)

    async def scenario() -> None:
        await hub.refresh()
        assert "LATER" in hub.rows
        tm(tmp_path, "spec", "archive", "LATER")
        await hub.refresh()

    asyncio.run(scenario())

    assert hub.rows["LATER"]["archived"] is True
