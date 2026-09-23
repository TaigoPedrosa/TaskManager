"""Attachment embedding in the static export (§4): image attachments up to 2 MB ship as
`data:` URIs; larger or missing ones stay name-only links the page renders without a server."""

from pathlib import Path

from taskmanager.core.enums import NodeKind
from taskmanager.core.models import Node
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.node_repo import NodeRepository
from taskmanager.engine.assets import store_asset
from taskmanager.web.static_export import export_static_html

_PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
    "53de000000174944415478da62fcffff3f0305050005000500a0e0d9670000"
    "0000000049454e44ae426082"
)


def _seed(root: Path) -> None:
    db_mgr = DatabaseManager(root / ".taskmanager")
    db_mgr.init_all()
    node_repo = NodeRepository(db_mgr)
    node_repo.save_node(Node(id="SPEC", kind=NodeKind.SPEC, title="Spec"))
    asset_name, mime = store_asset(root / ".taskmanager" / "assets", _write_source(root))
    node = node_repo.get_node("SPEC")
    assert node is not None
    node.frontmatter["attachments"] = [
        {"asset": asset_name, "name": "shot.png", "caption": "", "mime": mime, "source": {}}
    ]
    node_repo.save_node(node)


def _write_source(root: Path) -> Path:
    src = root / "shot.png"
    src.write_bytes(_PNG_1PX)
    return src


def test_small_image_attachment_embeds_as_data_uri(tmp_path: Path) -> None:
    _seed(tmp_path)
    out = export_static_html(tmp_path, tmp_path / "export.html")
    html = out.read_text(encoding="utf-8")
    assert "data:image/png;base64," in html


def test_oversized_attachment_is_not_embedded(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import taskmanager.web.static_export as static_export_mod

    monkeypatch.setattr(static_export_mod, "_MAX_INLINE_ASSET_BYTES", 1)
    _seed(tmp_path)
    out = export_static_html(tmp_path, tmp_path / "export.html")
    html = out.read_text(encoding="utf-8")
    assert "data:image/png;base64," not in html
