"""Unit tests for web visualizer enums and icon registries."""

import re

import pytest

from taskmanager.core.enums import NodeStatus, VirtualStatus
from taskmanager.web.enums import AppIcon, StatusGroup, StatusVisual, WebViewMode

ALL_STATUS_CODES = {s.value for s in NodeStatus} | {v.value for v in VirtualStatus}


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


def _luminance(hex_colour: str) -> float:
    channels = [int(hex_colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(foreground: str, background: str) -> float:
    lighter, darker = sorted((_luminance(foreground), _luminance(background)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def test_status_registry_has_exactly_one_theme_per_enum_member() -> None:
    assert {m.value.code for m in StatusVisual} == ALL_STATUS_CODES
    assert set(StatusVisual.all_themes_dict()) == ALL_STATUS_CODES
    assert all(m.name == m.value.code for m in StatusVisual)


def test_status_registry_labels_icons_and_colours_are_distinct() -> None:
    themes = [m.value for m in StatusVisual]
    for field in ("label", "icon", "dark_fg", "light_fg"):
        values = [getattr(t, field) for t in themes]
        assert len(set(values)) == len(values), field
    assert len({(t.dark_fg, t.dark_bg) for t in themes}) == len(themes)


@pytest.mark.parametrize("status", list(StatusVisual), ids=lambda m: m.name)
def test_status_text_contrast_is_at_least_aa_in_both_themes(status: StatusVisual) -> None:
    theme = status.value
    assert _contrast(theme.dark_fg, theme.dark_bg) >= 4.5
    assert _contrast(theme.light_fg, theme.light_bg) >= 4.5
    assert _contrast("#f3f4f6", theme.dark_bg) >= 4.5


def test_status_descriptions_are_one_sentence_and_every_group_is_used() -> None:
    for status in StatusVisual:
        description = status.value.description
        assert description.endswith(".")
        assert description.count(". ") == 0
    assert {m.value.group for m in StatusVisual} == set(StatusGroup)


def test_status_css_defines_both_theme_variables_for_every_status() -> None:
    css = StatusVisual.css()
    for code in ALL_STATUS_CODES:
        assert re.search(rf"(?<!\.dark )\.st-{code}\{{--st-fg:#\w+;--st-bg:#\w+\}}", css)
        assert re.search(rf"\.dark \.st-{code}\{{--st-fg:#\w+;--st-bg:#\w+\}}", css)
