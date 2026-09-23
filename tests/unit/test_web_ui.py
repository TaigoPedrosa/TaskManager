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
    # A top-level function's own closing brace sits at column 0, whichever static/js/*.js
    # file get_web_html() inlined it from; a nested function's closing brace does not, so
    # this also captures through the end of an enclosing top-level function for one.
    match = re.search(rf"function {name}\([^)]*\) \{{(.*?)\n\}}\n", html, re.DOTALL)
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
    assert "chip.title = `${theme.label}" in body


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


def test_plan_and_task_headers_are_not_sticky() -> None:
    html = get_web_html()
    assert "sticky" not in html
    assert (
        'class="h-12 px-4 rounded-t-xl bg-zinc-900/95 backdrop-blur-sm border-b border-zinc-800 flex items-center justify-between cursor-pointer plan-header"'
        in html
    )
    assert (
        'class="h-10 px-3 rounded-t-lg flex items-center justify-between cursor-pointer task-header bg-zinc-900/90 backdrop-blur-sm hover:bg-zinc-900"'
        in html
    )


def test_status_icon_carries_a_title_and_chip_is_legend_only() -> None:
    html = get_web_html()
    status_icon = _function_body(html, "statusIcon")
    assert 'title="${esc(t.label)}"' in status_icon
    # statusChip (visible label) survives only in its own definition and the legend.
    assert html.count("statusChip(") == 2
    assert "renderLegend" in html
    legend = _function_body(html, "renderLegend")
    assert "statusChip(" in legend


def test_group_headers_default_all_collapsed() -> None:
    html = get_web_html()
    group_collapsed = _function_body(html, "groupCollapsed")
    assert "collapsedGroups.has(groupId) ? !defaultCollapsed : defaultCollapsed" in group_collapsed

    render_sections = _function_body(html, "renderSections")
    assert "groupCollapsed(groupId, true)" in render_sections
    assert "renderGroupHeader(groupId, 'Sections'" in render_sections

    render_plan_card = _function_body(html, "renderPlanCard")
    assert "groupCollapsed(tasksGroupId, true)" in render_plan_card
    assert "renderGroupHeader(tasksGroupId, 'Tasks'" in render_plan_card


def test_group_header_toggle_is_wired_independently_of_node_and_section_collapse() -> None:
    html = get_web_html()
    attach = _function_body(html, "attachCollapsibleHandlers")
    assert "'.group-header'" in attach
    assert "collapsedGroups.has(id)) collapsedGroups.delete(id)" in attach
    assert "collapsedGroups.add(id)" in attach

    # The all-sections toolbar button only ever touches expandedSections, never the groups.
    toggle_sections_handler = re.search(
        r"toggleSectionsBtn\.addEventListener\('click', \(\) => \{(.*?)\n\}\);",
        html,
        re.DOTALL,
    )
    assert toggle_sections_handler, "toggleSectionsBtn click handler not found"
    assert "collapsedGroups" not in toggle_sections_handler.group(1)


def test_graph_layout_gives_nodes_room_and_a_shape_per_kind() -> None:
    render_graph = _function_body(get_web_html(), "renderGraph")
    assert "levelSeparation: 240" in render_graph
    assert "nodeSpacing: 320" in render_graph
    assert "size: 16" in render_graph
    assert "widthConstraint: { minimum: 170, maximum: 260 }" in render_graph
    assert "GRAPH_SHAPE_BY_KIND" in get_web_html()


def test_page_inlines_every_static_js_file() -> None:
    # One string that only that file defines, so a broken concatenation (a file dropped,
    # or read from the wrong path) shows up as a specific missing feature, not a blank page.
    html = get_web_html()
    known_strings_by_file = {
        "core.js": "function canEdit()",
        "filters.js": "const NO_REPO = '(none)';",
        "tree.js": "function renderTaskCard(task)",
        "graph.js": "const GRAPH_SHAPE_BY_KIND",
        "edit.js": "function openDialog(",
        "detail.js": "async function showGraphInspector(nodeId)",
        "main.js": "readHash();\nrenderLegend();",
    }
    for source_file, needle in known_strings_by_file.items():
        assert needle in html, f"{source_file}'s own content ({needle!r}) missing from the page"


def test_static_files_are_packaged_and_resolve_at_runtime() -> None:
    from importlib.resources import files

    static_dir = files("taskmanager.web").joinpath("static")
    assert static_dir.joinpath("index.html").is_file()
    assert static_dir.joinpath("app.css").is_file()
    for name in ("core.js", "filters.js", "tree.js", "graph.js", "edit.js", "detail.js", "main.js"):
        assert static_dir.joinpath("js", name).is_file(), f"js/{name} did not ship"


def test_can_edit_is_false_only_in_static_export_mode() -> None:
    html = get_web_html()
    can_edit = _function_body(html, "canEdit")
    assert "return !isStaticMode;" in can_edit
    assert "isStaticMode = typeof window.STATIC_DATA !== 'undefined';" in html


def test_new_menu_renders_nothing_in_a_read_only_static_export() -> None:
    body = _function_body(get_web_html(), "renderNewMenu")
    assert "if (!canEdit()) return;" in body


def test_new_menu_offers_spec_plan_and_task_but_not_decision() -> None:
    body = _function_body(get_web_html(), "renderNewMenu")
    assert 'data-new-kind="spec"' in body
    assert 'data-new-kind="plan"' in body
    assert 'data-new-kind="task"' in body
    assert "decision" not in body.lower()


def test_dialog_traps_focus_and_closes_on_escape_with_focus_return() -> None:
    html = get_web_html()
    body = _function_body(html, "openDialog")
    assert "e.key === 'Escape'" in body
    assert "e.key === 'Tab'" in body
    assert "trigger.focus()" in body
    assert "aria-modal" in body


def test_dialog_submit_shows_the_refusal_without_closing() -> None:
    # An OperationError's message (400/404/409) is surfaced in the form, and the dialog is
    # never closed by the catch branch -- only a successful onSubmit calls close().
    html = get_web_html()
    dialog_call_site = re.search(r"form\.addEventListener\('submit'.*?\}\);", html, re.DOTALL)
    assert dialog_call_site, "dialog submit handler not found"
    handler = dialog_call_site.group(0)
    assert "errorEl.textContent" in handler
    assert "close" not in handler.split("catch")[1].split("finally")[0]


def test_section_preview_and_stored_markdown_are_sanitised_with_dompurify() -> None:
    html = get_web_html()
    assert "cdn.jsdelivr.net/npm/dompurify" in html
    assert "DOMPurify.sanitize(unsafeRenderSectionBody(content))" in html
    open_section_dialog = _function_body(html, "openSectionDialog")
    assert "renderSectionBody(content.value)" in open_section_dialog


def test_frontmatter_editor_treats_declared_files_as_a_list() -> None:
    row = _function_body(get_web_html(), "frontmatterRowHtml")
    assert "key === 'declared_files' || Array.isArray(value)" in row
    assert "<textarea" in row


def test_destructive_actions_confirm_before_writing() -> None:
    html = get_web_html()
    for fn_name in ("removeDependency", "removeSection", "removeVerification", "releaseLease"):
        body = _function_body(html, fn_name)
        assert "confirmDialog(" in body, f"{fn_name} does not confirm before writing"
    abandon = _function_body(html, "changeStatus")
    assert "confirmDialog(" in abandon
    assert "status === 'ABANDONED'" in abandon


def test_edit_dialog_only_offers_models_repo_and_frontmatter_for_tasks() -> None:
    body = _function_body(get_web_html(), "openEditNodeDialog")
    assert "isTask ? fieldRow('Acceptable models" in body
    assert "isTask ? fieldRow('Target repo" in body
    assert "isTask ? frontmatterEditorHtml(node.frontmatter) : ''" in body


def test_api_helper_surfaces_the_servers_own_refusal_message() -> None:
    body = _function_body(get_web_html(), "api")
    assert "data && data.detail" in body
    assert "!res.ok" in body
