import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from taskmanager.web.app import add_progress
from taskmanager.web.ui import get_web_html


def _task(status: str) -> dict[str, Any]:
    return {"kind": "task", "virtual_status": status, "children": []}


def test_add_progress_counts_set_aside_work_apart_from_completed() -> None:
    plan: dict[str, Any] = {
        "kind": "plan",
        "children": [
            _task("COMPLETED"),
            _task("DEFERRED"),
            _task("ABANDONED"),
            _task("SUPERSEDED"),
        ],
    }
    spec: dict[str, Any] = {"kind": "spec", "children": [plan, _task("READY")]}

    add_progress(spec)

    assert plan["progress"] == {
        "total": 4,
        "counts": {"COMPLETED": 1, "DEFERRED": 1, "ABANDONED": 1, "SUPERSEDED": 1},
    }
    assert spec["progress"]["total"] == 5
    assert spec["progress"]["counts"]["COMPLETED"] == 1


def test_add_progress_of_a_plan_without_tasks_is_empty() -> None:
    plan: dict[str, Any] = {"kind": "plan", "children": []}

    add_progress(plan)

    assert plan["progress"] == {"total": 0, "counts": {}}


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed to syntax-check the page")
def test_page_scripts_parse(tmp_path: Path) -> None:
    scripts = re.findall(r"<script>(.*?)</script>", get_web_html(), re.DOTALL)
    script_file = tmp_path / "page.js"
    script_file.write_text("\n;\n".join(scripts), encoding="utf-8")

    result = subprocess.run(
        ["node", "--check", str(script_file)], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr


def _function_body(html: str, name: str) -> str:
    match = re.search(rf"function {name}\([^)]*\) \{{(.*?)\n    \}}\n", html, re.DOTALL)
    assert match, f"{name} not found in the page"
    return match.group(1)


def test_toolbar_holds_view_switcher_search_and_stats_in_one_row() -> None:
    html = get_web_html()
    toolbar = re.search(r'<header id="toolbar".*?</header>', html, re.DOTALL)
    assert toolbar, "single merged toolbar not found"
    row = toolbar.group(0)
    assert 'id="view-doc-btn"' in row
    assert 'id="view-graph-btn"' in row
    assert 'id="search-box"' in row
    assert 'id="stats-digest"' in row
    assert "lg:flex-nowrap" in row
    assert 'id="filter-bar"' not in html


def test_search_box_appears_exactly_once() -> None:
    assert get_web_html().count('id="search-box"') == 1


def test_stats_digest_chips_carry_no_per_status_label_text() -> None:
    body = _function_body(get_web_html(), "updateStatsDigest")
    assert "<span>${esc(theme.label)}</span>" not in body
    assert "<span>All tasks</span>" not in body
    assert "chip.title = theme.label" in body


def test_sidebar_pane_starts_hidden_and_toggles_with_view_mode() -> None:
    html = get_web_html()
    aside = re.search(r'<aside id="sidebar-pane"[^>]*>', html)
    assert aside, "sidebar-pane not found"
    assert "hidden" in aside.group(0)

    set_view_mode = _function_body(html, "setViewMode")
    assert "sidebarPane.classList.add('hidden')" in set_view_mode
    assert "sidebarPane.classList.remove('hidden')" in set_view_mode


def test_sidebar_resize_handle_clamps_and_persists_width() -> None:
    html = get_web_html()
    assert 'id="sidebar-resize-handle"' in html
    assert "SIDEBAR_MIN_WIDTH = 200" in html
    assert "SIDEBAR_MAX_WIDTH = 560" in html
    assert "localStorage.setItem('tm-sidebar-width'" in html
    assert "localStorage.getItem('tm-sidebar-width')" in html


def test_tree_row_carries_exactly_one_status_marker() -> None:
    body = _function_body(get_web_html(), "createNodeRow")
    assert body.count("statusDot(") == 1
    assert "statusChip(" not in body
    assert "statusIcon(" not in body


def test_document_sections_default_collapsed_and_remember_expand_state() -> None:
    html = get_web_html()
    render_sections = _function_body(html, "renderSections")
    assert "<details open class" not in html
    assert "isOpen ? 'open' : ''" in render_sections
    assert "expandedSections.has(id)" in render_sections
    assert "expandedSections" in _function_body(html, "attachSectionToggleHandlers")


def test_plan_and_task_headers_are_sticky_at_distinct_offsets() -> None:
    html = get_web_html()
    assert "plan-header sticky top-0 z-20" in html
    assert (
        "task-header bg-zinc-900/90 backdrop-blur-sm hover:bg-zinc-900 sticky top-12 z-10" in html
    )


def test_graph_layout_gives_nodes_room_and_a_shape_per_kind() -> None:
    render_graph = _function_body(get_web_html(), "renderGraph")
    assert "levelSeparation: 240" in render_graph
    assert "nodeSpacing: 320" in render_graph
    assert "size: 16" in render_graph
    assert "widthConstraint: { minimum: 170, maximum: 260 }" in render_graph
    assert "GRAPH_SHAPE_BY_KIND" in get_web_html()
