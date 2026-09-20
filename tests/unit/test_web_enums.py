"""Unit tests for web visualizer enums and icon registries."""

from taskmanager.core.enums import NodeStatus, VirtualStatus
from taskmanager.web.enums import AppIcon, StatusVisual, WebViewMode


def test_status_visual_covers_all_node_and_virtual_statuses() -> None:
    all_statuses = [s.value for s in NodeStatus] + [v.value for v in VirtualStatus]
    for status_str in all_statuses:
        visual = StatusVisual.from_status(status_str)
        assert visual is not None
        assert visual.name == status_str
        d = visual.to_dict()
        assert d["code"] == status_str
        assert len(d["label"]) > 0
        assert len(d["icon"]) > 0
        assert d["graph_bg"].startswith("#")
        assert d["graph_border"].startswith("#")


def test_status_visual_unknown_fallback() -> None:
    fallback = StatusVisual.from_status("UNKNOWN_STATUS_XYZ")
    assert fallback == StatusVisual.NOT_STARTED


def test_app_icon_sprite_generation() -> None:
    sprite = AppIcon.generate_svg_sprite()
    assert '<svg xmlns="http://www.w3.org/2000/svg"' in sprite
    assert 'id="icon-play-circle"' in sprite
    assert 'id="icon-file-text"' in sprite
    assert 'id="icon-network"' in sprite


def test_webview_mode_values() -> None:
    assert WebViewMode.DOCUMENT.value == "document"
    assert WebViewMode.GRAPH.value == "graph"
