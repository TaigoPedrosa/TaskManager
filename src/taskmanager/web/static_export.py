"""Static standalone HTML generator for TaskManager offline visualizer."""

from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from taskmanager.web.app import create_app
from taskmanager.web.ui import get_web_html


def export_static_html(project_root: Path, output_file: Path) -> Path:
    app = create_app(project_root)
    client = TestClient(app)

    tree = client.get("/api/tree").json()
    graph = client.get("/api/graph").json()
    stats = client.get("/api/stats").json()

    # Fetch details for all nodes
    details: dict[str, Any] = {}
    for node_item in graph.get("nodes", []):
        node_id = node_item["id"]
        res = client.get(f"/api/nodes/{node_id}")
        if res.status_code == 200:
            details[node_id] = res.json()

    initial_data = {
        "tree": tree,
        "graph": graph,
        "stats": stats,
        "details": details,
    }

    html_content = get_web_html(initial_data=initial_data)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(html_content, encoding="utf-8")
    return output_file
