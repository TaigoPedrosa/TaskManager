"""Static standalone HTML generator for TaskManager offline visualizer.

Builds `STATIC_DATA` from `rows.py`, `visibility.py` and `bodies.py` directly -- the same
read path the live view and `/ws` share -- rather than driving the HTTP app through a
`TestClient`.
"""

import base64
import mimetypes
import re
from pathlib import Path
from typing import Any

from taskmanager.core.enums import NodeKind
from taskmanager.db.cache_repo import CacheRepository
from taskmanager.db.job_repo import JobRepository
from taskmanager.db.node_repo import NodeRepository
from taskmanager.di.container import create_container
from taskmanager.engine.assets import ASSET_NAME_RE
from taskmanager.engine.config import ConfigStore
from taskmanager.engine.snapshot import DisplayView, SnapshotBuilder
from taskmanager.web.app import VENDOR_DIR, _decision_item
from taskmanager.web.bodies import BodyRepos, build_bodies
from taskmanager.web.rows import build_rows, statuses, statuses_hash
from taskmanager.web.ui import get_web_html

# Above this, an attachment ships as a name-only link in the static export rather than
# bloating a single HTML file that has to stay under the artifact size limit.
_MAX_INLINE_ASSET_BYTES = 2 * 1024 * 1024

# A section's own markdown image, e.g. ![home capture](evidence/home-1440.png): the browser
# renders this from the raw markdown client-side (marked.parse), so unlike an attachment there
# is no server-side rewrite step to skip in static mode -- the path is either embedded here,
# at export time, or it is a broken image with no project tree beneath it to resolve against.
_MD_IMAGE_RE = re.compile(r"(!\[[^\]]*\]\()([^)\s]+)(\))")

# A `data:` src rather than an inline body: the script's bytes never pass through the HTML
# parser, so no `</script>` or `<!--` inside a library can end or swallow the element.
_VENDOR_SCRIPT_RE = re.compile(r'<script src="/vendor/([\w-]+\.min\.js)"></script>')


def _embed_vendor_script(match: re.Match[str]) -> str:
    data = base64.b64encode((VENDOR_DIR / match.group(1)).read_bytes()).decode("ascii")
    return f'<script src="data:text/javascript;base64,{data}"></script>'


def _embed_attachments(bodies: dict[str, Any], project_root: Path) -> None:
    assets_dir = project_root / ".taskmanager" / "assets"
    for body in bodies.values():
        attachments = (body.get("node") or {}).get("frontmatter", {}).get("attachments") or []
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


def export_static_html(project_root: Path, output_file: Path) -> Path:
    assets_dir = project_root / ".taskmanager" / "assets"
    container = create_container(project_root)
    node_repo = container.get(NodeRepository)
    job_repo = container.get(JobRepository)
    cache = container.get(CacheRepository)
    snapshots = container.get(SnapshotBuilder)
    condition_ttl = ConfigStore(project_root).project().condition_ttl

    view = DisplayView(snapshots, cache, condition_ttl)
    rows = build_rows(view)
    entries = statuses(rows)
    edges = [[source, target, "depends_on"] for source, target in view.snapshot.edges]

    repos = BodyRepos(
        node_repo=node_repo,
        job_repo=job_repo,
        cache=cache,
        condition_ttl=condition_ttl,
        assets_dir=assets_dir,
    )
    decision_nodes = node_repo.list_nodes(kind=NodeKind.DECISION)
    # `rows` excludes decisions (rows.py skips them), but the Decisions view opens one by id
    # through the same `bodies` map every other node detail comes from.
    bodies = build_bodies(view, [*rows.keys(), *(d.id for d in decision_nodes)], repos=repos)
    decisions = [_decision_item(d, node_repo, assets_dir) for d in decision_nodes]

    _embed_attachments(bodies, project_root)
    root = project_root.resolve()
    for body in bodies.values():
        _embed_section_images(body.get("sections") or [], root)

    initial_data = {
        "statuses": entries,
        "hash": statuses_hash(entries),
        "rows": rows,
        "edges": edges,
        "bodies": bodies,
        "decisions": decisions,
    }

    html_content = _VENDOR_SCRIPT_RE.sub(
        _embed_vendor_script, get_web_html(initial_data=initial_data)
    )
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(html_content, encoding="utf-8")
    return output_file
