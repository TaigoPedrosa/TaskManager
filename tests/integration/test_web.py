"""Integration tests for the TaskManager web visualizer and CLI commands."""

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from taskmanager.cli.main import _find_available_port, app
from taskmanager.core.enums import (
    NodeKind,
    NodeStatus,
    RelationType,
    VerificationType,
    VirtualStatus,
)
from taskmanager.core.models import Lease, Node, NodeRelation, NodeSection, NodeVerification
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.web.app import create_app
from taskmanager.web.static_export import export_static_html

runner = CliRunner()


def test_web_api_endpoints_and_ui(tmp_path: Path) -> None:
    db_mgr = DatabaseManager(tmp_path / ".taskmanager")
    db_mgr.init_all()
    node_repo = NodeRepository(db_mgr)

    # Add sample spec, plan, task
    node_repo.save_node(Node(id="AUTH", kind=NodeKind.SPEC, title="Authentication Spec"))
    node_repo.save_node(Node(id="AUTH-P1", kind=NodeKind.PLAN, title="JWT Auth Plan"))
    node_repo.save_node(
        Node(
            id="AUTH-T1",
            kind=NodeKind.TASK,
            title="Implement Token Verification",
            status=NodeStatus.NOT_STARTED,
            priority=85,
            acceptable_models=["claude-3-7-sonnet"],
        )
    )
    node_repo.save_section(
        NodeSection(
            node_id="AUTH-T1",
            section_key="steps",
            ordinal=1,
            header="## Steps",
            content="- [ ] Step 1: Write test",
        )
    )
    node_repo.add_verification(
        NodeVerification(
            node_id="AUTH-T1",
            verification_type=VerificationType.FILE_EXISTS,
            target_path="src/auth/jwt.py",
        )
    )

    fastapi_app = create_app(tmp_path)
    client = TestClient(fastapi_app)

    # 1. Index HTML
    res_index = client.get("/")
    assert res_index.status_code == 200
    assert "TaskManager — Interactive Visualizer" in res_index.text

    # 2. /api/tree
    res_tree = client.get("/api/tree")
    assert res_tree.status_code == 200
    tree_data = res_tree.json()
    assert isinstance(tree_data, list)
    assert any(item["id"] == "AUTH" for item in tree_data)

    # 3. /api/graph
    res_graph = client.get("/api/graph")
    assert res_graph.status_code == 200
    graph_data = res_graph.json()
    assert "nodes" in graph_data
    assert "edges" in graph_data
    node_ids = {n["id"] for n in graph_data["nodes"]}
    assert "AUTH" in node_ids
    assert "AUTH-T1" in node_ids

    # 4. /api/nodes/{id}
    res_node = client.get("/api/nodes/AUTH-T1")
    assert res_node.status_code == 200
    node_detail = res_node.json()
    assert node_detail["node"]["id"] == "AUTH-T1"
    assert node_detail["node"]["priority"] == 85
    assert node_detail["virtual_status"] == "READY"
    assert len(node_detail["sections"]) == 1
    assert "## Steps" in node_detail["rendered_markdown"]
    assert len(node_detail["verifications"]) == 1

    # 5. /api/nodes/nonexistent -> 404
    res_404 = client.get("/api/nodes/NONEXISTENT")
    assert res_404.status_code == 404

    # 6. /api/stats
    res_stats = client.get("/api/stats")
    assert res_stats.status_code == 200
    stats = res_stats.json()
    assert stats["total"] >= 1
    assert stats["READY"] >= 1

    # 7. WebSocket connection
    with client.websocket_connect("/ws") as ws:
        # Connection established successfully
        assert ws is not None


def test_static_html_export(tmp_path: Path) -> None:
    db_mgr = DatabaseManager(tmp_path / ".taskmanager")
    db_mgr.init_all()
    node_repo = NodeRepository(db_mgr)
    node_repo.save_node(Node(id="AUTH", kind=NodeKind.SPEC, title="Auth Spec"))

    out_file = tmp_path / "dashboard.html"
    result_path = export_static_html(tmp_path, out_file)
    assert result_path.exists()
    content = result_path.read_text(encoding="utf-8")
    assert "window.STATIC_DATA" in content
    assert "AUTH" in content


def test_find_available_port() -> None:
    port = _find_available_port("127.0.0.1", 6701)
    assert isinstance(port, int)
    assert port >= 6701


def test_cli_web_export_command(tmp_path: Path) -> None:
    # Initialize repo
    res_init = runner.invoke(app, ["init", "--path", str(tmp_path)])
    assert res_init.exit_code == 0

    out_file = tmp_path / "exported.html"
    res_export = runner.invoke(app, ["web", "export", "-o", str(out_file), "--path", str(tmp_path)])
    assert res_export.exit_code == 0
    assert "Exported static HTML visualizer" in res_export.stdout
    assert out_file.exists()


def test_cli_web_uninitialized_error(tmp_path: Path) -> None:
    empty_dir = tmp_path / "empty_proj"
    empty_dir.mkdir()
    res = runner.invoke(app, ["web", "export", "--path", str(empty_dir)])
    assert res.exit_code != 0
    assert "not initialized" in res.stdout


ALL_STATUS_CODES = {s.value for s in NodeStatus} | {v.value for v in VirtualStatus}


@pytest.fixture
def every_status_project(tmp_path: Path) -> Path:
    db_mgr = DatabaseManager(tmp_path / ".taskmanager")
    db_mgr.init_all()
    node_repo = NodeRepository(db_mgr)
    node_repo.save_node(Node(id="SPEC", kind=NodeKind.SPEC, title="Spec"))
    node_repo.save_node(Node(id="PLAN", kind=NodeKind.PLAN, title="Plan"))
    node_repo.add_relation(
        NodeRelation(source_id="SPEC", target_id="PLAN", relation_type=RelationType.CONTAINS)
    )

    def add_task(task_id: str, status: NodeStatus, repo: str = "core") -> None:
        node_repo.save_node(
            Node(
                id=task_id,
                kind=NodeKind.TASK,
                title=f"Task {task_id}",
                status=status,
                target_repo=repo,
                acceptable_models=["claude-opus-5"] if repo == "core" else ["gemini-flash"],
            )
        )
        node_repo.add_relation(
            NodeRelation(source_id="PLAN", target_id=task_id, relation_type=RelationType.CONTAINS)
        )

    for status in NodeStatus:
        if status is not NodeStatus.NOT_STARTED:
            add_task(
                f"T-{status.value}",
                status,
                repo="web" if status is NodeStatus.IMPLEMENTING else "core",
            )
    add_task("T-READY", NodeStatus.NOT_STARTED)
    add_task("T-BLOCKED", NodeStatus.NOT_STARTED)
    add_task("T-INFLIGHT", NodeStatus.NOT_STARTED)
    node_repo.add_relation(
        NodeRelation(
            source_id="T-BLOCKED", target_id="T-IMPLEMENTING", relation_type=RelationType.DEPENDS_ON
        )
    )
    node_repo.add_relation(
        NodeRelation(
            source_id="T-BLOCKED", target_id="T-SUPERSEDED", relation_type=RelationType.DEPENDS_ON
        )
    )
    node_repo.save_section(
        NodeSection(
            node_id="T-DEFERRED",
            section_key="deferral",
            ordinal=1,
            header="## Deferral",
            content="line one\nline two",
        )
    )
    RuntimeRepository(db_mgr).acquire_lease(
        Lease(
            task_id="T-INFLIGHT",
            agent_id="agent",
            session_id="session",
            branch_name="branch",
            acquired_at=datetime.now(tz=UTC),
            last_heartbeat=datetime.now(tz=UTC),
            ttl_seconds=300,
        ),
        [],
    )
    return tmp_path


def test_stats_reports_every_status_including_zeros(every_status_project: Path) -> None:
    stats = TestClient(create_app(every_status_project)).get("/api/stats").json()

    assert ALL_STATUS_CODES <= stats.keys()
    assert stats["total"] == 13
    assert {code: stats[code] for code in ALL_STATUS_CODES} == {
        **{code: 1 for code in ALL_STATUS_CODES},
        NodeStatus.NOT_STARTED.value: 0,
    }


def test_tree_progress_counts_each_status_separately(every_status_project: Path) -> None:
    tree = TestClient(create_app(every_status_project)).get("/api/tree").json()
    spec = tree[0]
    plan = spec["children"][0]

    expected = {code: 1 for code in ALL_STATUS_CODES if code != NodeStatus.NOT_STARTED.value}
    assert plan["progress"] == {"total": 13, "counts": expected}
    assert spec["progress"] == plan["progress"]
    assert plan["progress"]["counts"]["COMPLETED"] == 1


def test_tree_task_lists_dependencies_with_their_own_status(every_status_project: Path) -> None:
    plan = TestClient(create_app(every_status_project)).get("/api/tree").json()[0]["children"][0]
    blocked = next(t for t in plan["children"] if t["id"] == "T-BLOCKED")

    assert blocked["virtual_status"] == "BLOCKED"
    assert blocked["dependency_details"] == [
        {
            "id": "T-IMPLEMENTING",
            "title": "Task T-IMPLEMENTING",
            "status": "IMPLEMENTING",
            "finished": False,
        },
        {
            "id": "T-SUPERSEDED",
            "title": "Task T-SUPERSEDED",
            "status": "SUPERSEDED",
            "finished": True,
        },
    ]


def test_node_detail_lists_dependencies_and_every_section(every_status_project: Path) -> None:
    client = TestClient(create_app(every_status_project))
    blocked = client.get("/api/nodes/T-BLOCKED").json()
    deferred = client.get("/api/nodes/T-DEFERRED").json()

    assert [d["id"] for d in blocked["dependency_details"] if not d["finished"]] == [
        "T-IMPLEMENTING"
    ]
    assert deferred["virtual_status"] == "DEFERRED"
    assert deferred["sections"][0]["content"] == "line one\nline two"


def test_graph_nodes_carry_repo_and_models_for_filtering(every_status_project: Path) -> None:
    nodes = TestClient(create_app(every_status_project)).get("/api/graph").json()["nodes"]
    by_id = {n["id"]: n for n in nodes}

    assert by_id["T-COMPLETED"]["target_repo"] == "core"
    assert by_id["T-COMPLETED"]["acceptable_models"] == ["claude-opus-5"]
    assert by_id["T-IMPLEMENTING"]["target_repo"] == "web"
    assert by_id["T-IMPLEMENTING"]["acceptable_models"] == ["gemini-flash"]
    assert by_id["T-INFLIGHT"]["status"] == "IN_FLIGHT"


def test_static_export_embeds_every_status_and_the_filter_ui(
    every_status_project: Path, tmp_path: Path
) -> None:
    out = export_static_html(every_status_project, tmp_path / "out" / "all.html")
    html = out.read_text(encoding="utf-8")

    themes_match = re.search(r"window.STATUS_THEMES = (\{.*?\});\n", html)
    static_match = re.search(r"window.STATIC_DATA = (\{.*?\});</script>", html)
    assert themes_match
    assert static_match
    themes = json.loads(themes_match.group(1))
    static = json.loads(static_match.group(1))
    assert set(themes) == ALL_STATUS_CODES
    assert ALL_STATUS_CODES <= static["stats"].keys()
    assert static["tree"][0]["progress"]["total"] == 13
    for element_id in (
        "stats-digest",
        "repo-filter",
        "model-filter",
        "active-filters",
        "legend-panel",
        "sidebar-resize-handle",
        "toggle-sections-btn",
    ):
        assert f'id="{element_id}"' in html
    assert html.count('id="search-box"') == 1
    assert '<aside id="sidebar-pane" class="hidden' in html
