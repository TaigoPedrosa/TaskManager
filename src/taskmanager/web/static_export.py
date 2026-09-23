"""Static standalone HTML generator for TaskManager offline visualizer."""

import base64
import mimetypes
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from taskmanager.web.app import create_app
from taskmanager.web.ui import get_web_html

# Above this, an attachment ships as a name-only link in the static export rather than
# bloating a single HTML file that has to stay under the artifact size limit.
_MAX_INLINE_ASSET_BYTES = 2 * 1024 * 1024


def _embed_attachments(details: dict[str, Any], project_root: Path) -> None:
    assets_dir = project_root / ".taskmanager" / "assets"
    for detail in details.values():
        attachments = (detail.get("node") or {}).get("frontmatter", {}).get("attachments") or []
        for entry in attachments:
            asset_path = assets_dir / entry["asset"]
            mime = entry.get("mime") or mimetypes.guess_type(entry["asset"])[0] or ""
            if not mime.startswith("image/") or not asset_path.is_file():
                continue
            if asset_path.stat().st_size > _MAX_INLINE_ASSET_BYTES:
                continue
            data = base64.b64encode(asset_path.read_bytes()).decode("ascii")
            entry["data_uri"] = f"data:{mime};base64,{data}"


def export_static_html(project_root: Path, output_file: Path) -> Path:
    app = create_app(project_root)
    client = TestClient(app)

    tree = client.get("/api/tree").json()
    graph = client.get("/api/graph").json()
    stats = client.get("/api/stats").json()
    decisions = client.get("/api/decisions").json()

    # Fetch details for all nodes
    details: dict[str, Any] = {}
    for node_item in graph.get("nodes", []):
        node_id = node_item["id"]
        res = client.get(f"/api/nodes/{node_id}")
        if res.status_code == 200:
            details[node_id] = res.json()

    _embed_attachments(details, project_root)

    initial_data = {
        "tree": tree,
        "graph": graph,
        "stats": stats,
        "details": details,
        "decisions": decisions,
    }

    html_content = get_web_html(initial_data=initial_data)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(html_content, encoding="utf-8")
    return output_file
