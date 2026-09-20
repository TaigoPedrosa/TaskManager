"""Integration tests for the TaskManager web visualizer and CLI commands."""

from pathlib import Path

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from taskmanager.cli.main import _find_available_port, app
from taskmanager.core.enums import NodeKind, NodeStatus, VerificationType
from taskmanager.core.models import Node, NodeSection, NodeVerification
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.node_repo import NodeRepository
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
    assert stats["ready"] >= 1

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
