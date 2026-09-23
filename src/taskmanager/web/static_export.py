"""Static standalone HTML generator for TaskManager offline visualizer."""

import base64
import mimetypes
import re
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from taskmanager.engine.assets import ASSET_NAME_RE
from taskmanager.web.app import create_app
from taskmanager.web.ui import get_web_html

# Above this, an attachment ships as a name-only link in the static export rather than
# bloating a single HTML file that has to stay under the artifact size limit.
_MAX_INLINE_ASSET_BYTES = 2 * 1024 * 1024

# A section's own markdown image, e.g. ![home capture](evidence/home-1440.png): the browser
# renders this from the raw markdown client-side (marked.parse), so unlike an attachment there
# is no server-side rewrite step to skip in static mode -- the path is either embedded here,
# at export time, or it is a broken image with no project tree beneath it to resolve against.
_MD_IMAGE_RE = re.compile(r"(!\[[^\]]*\]\()([^)\s]+)(\))")


def _embed_attachments(details: dict[str, Any], project_root: Path) -> None:
    assets_dir = project_root / ".taskmanager" / "assets"
    for detail in details.values():
        attachments = (detail.get("node") or {}).get("frontmatter", {}).get("attachments") or []
        for entry in attachments:
            # Same pattern `/assets/{name}` enforces: a node's `attachments` frontmatter is
            # attacker-writable through `PATCH /api/nodes/{id}`, so an unvalidated name here
            # would let the export join `../../elsewhere` onto `assets_dir` and embed a file
            # from outside the project as a `data:` URI.
            if not ASSET_NAME_RE.fullmatch(entry.get("asset") or ""):
                continue
            asset_path = assets_dir / entry["asset"]
            mime = entry.get("mime") or mimetypes.guess_type(entry["asset"])[0] or ""
            if not mime.startswith("image/") or not asset_path.is_file():
                continue
            if asset_path.stat().st_size > _MAX_INLINE_ASSET_BYTES:
                continue
            data = base64.b64encode(asset_path.read_bytes()).decode("ascii")
            entry["data_uri"] = f"data:{mime};base64,{data}"


def _embed_one_image(match: re.Match[str], root: Path) -> str:
    src = match.group(2)
    if re.match(r"^(https?:|data:)", src) or src.startswith("/assets/"):
        return match.group(0)
    try:
        candidate = (root / src).resolve()
    except OSError, ValueError:
        return match.group(0)
    if not candidate.is_relative_to(root) or not candidate.is_file():
        return match.group(0)
    mime = mimetypes.guess_type(candidate.name)[0] or ""
    if not mime.startswith("image/") or candidate.stat().st_size > _MAX_INLINE_ASSET_BYTES:
        return match.group(0)
    data = base64.b64encode(candidate.read_bytes()).decode("ascii")
    return f"{match.group(1)}data:{mime};base64,{data}{match.group(3)}"


def _embed_section_images(sections: list[dict[str, Any]], root: Path) -> None:
    for section in sections:
        content = section.get("content")
        if content:
            section["content"] = _MD_IMAGE_RE.sub(lambda m: _embed_one_image(m, root), content)


def _embed_images_in_tree(node: dict[str, Any], root: Path) -> None:
    _embed_section_images(node.get("sections") or [], root)
    for child in node.get("children") or []:
        _embed_images_in_tree(child, root)


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
    root = project_root.resolve()
    for root_node in tree:
        _embed_images_in_tree(root_node, root)
    for detail in details.values():
        _embed_section_images(detail.get("sections") or [], root)

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
