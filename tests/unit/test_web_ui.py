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

    # Only the COMPLETED task counts: DEFERRED/ABANDONED/SUPERSEDED can never finish, so they
    # are set aside rather than dragging `total` (and a bar built on it) down with them.
    assert plan["progress"] == {
        "done": 1,
        "total": 1,
        "set_aside": 3,
        "counts": {"COMPLETED": 1, "DEFERRED": 1, "ABANDONED": 1, "SUPERSEDED": 1},
    }
    assert spec["progress"]["done"] == 1
    assert spec["progress"]["total"] == 2  # the plan's one counted task + the spec's own READY one
    assert spec["progress"]["set_aside"] == 3
    assert spec["progress"]["counts"]["COMPLETED"] == 1


def test_add_progress_of_a_plan_without_tasks_is_empty() -> None:
    plan: dict[str, Any] = {"kind": "plan", "children": []}

    add_progress(plan)

    assert plan["progress"] == {"done": 0, "total": 0, "set_aside": 0, "counts": {}}


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed to syntax-check the page")
def test_page_scripts_parse(tmp_path: Path) -> None:
    scripts = re.findall(r"<script>(.*?)</script>", get_web_html(), re.DOTALL)
    script_file = tmp_path / "page.js"
    script_file.write_text("\n;\n".join(scripts), encoding="utf-8")

    result = subprocess.run(
        ["node", "--check", str(script_file)], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, result.stderr


def test_static_data_escapes_a_closing_script_tag_inside_section_content() -> None:
    # A section's content is user-writable (`PUT /api/nodes/{id}/sections/{key}`) and lands in
    # `initial_data` verbatim; an unescaped `</script>` in it closes the tag early and runs
    # whatever text follows as markup, in the one output (the static export) with no server
    # left on the way out to sanitise it.
    html = get_web_html(initial_data={"tree": [], "details": {"x": "</script><img src=x>"}})
    assert "</script><img" not in html
    assert "<\\/script><img" in html


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


def test_tree_row_is_keyboard_reachable_and_operable() -> None:
    # A canvas node in the graph view has no DOM presence to Tab to, so this tree row is the
    # only reachable path to any task's detail panel for a keyboard user; it needs a role,
    # a tab stop and an Enter/Space handler, not only row.onclick.
    html = get_web_html()
    body = _function_body(html, "createNodeRow")
    assert "role', 'treeitem'" in body
    assert "tabindex', '0'" in body
    assert "row.addEventListener('keydown'" in body
    assert "selectNode(node.id)" in body
    assert 'role="tree"' in html


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
    shared = _function_body(html, "attachGroupHeaderHandlers")
    assert "'.group-header'" in shared
    assert "collapsedGroups.has(id)) collapsedGroups.delete(id)" in shared
    assert "collapsedGroups.add(id)" in shared
    attach = _function_body(html, "attachCollapsibleHandlers")
    assert "attachGroupHeaderHandlers(document, renderUnifiedDocument)" in attach

    # The all-sections toolbar button only ever touches expandedSections, never the groups.
    toggle_sections_handler = re.search(
        r"toggleSectionsBtn\.addEventListener\('click', \(\) => \{(.*?)\n\}\);",
        html,
        re.DOTALL,
    )
    assert toggle_sections_handler, "toggleSectionsBtn click handler not found"
    assert "collapsedGroups" not in toggle_sections_handler.group(1)


def test_group_header_is_keyboard_operable_everywhere_it_renders() -> None:
    # renderSections()'s "Sections (N)" row is reused by the decisions detail pane, which has
    # no plan/task collapse of its own and used to wire nothing for it at all -- a rendered
    # group header that neither responds to a click there nor to Enter anywhere.
    html = get_web_html()
    header = _function_body(html, "renderGroupHeader")
    assert 'role="button"' in header
    assert 'tabindex="0"' in header
    assert "aria-expanded=" in header
    shared = _function_body(html, "attachGroupHeaderHandlers")
    assert "header.onkeydown" in shared
    detail = _function_body(html, "renderDecisionDetail")
    assert "attachGroupHeaderHandlers(decisionsDetailEl" in detail


def test_graph_layout_gives_nodes_room_and_a_shape_per_kind() -> None:
    render_graph = _function_body(get_web_html(), "renderGraph")
    assert "levelSeparation: 240" in render_graph
    assert "nodeSpacing: 320" in render_graph
    assert "size: 16" in render_graph
    assert "widthConstraint: { minimum: 170, maximum: 260 }" in render_graph
    assert "GRAPH_SHAPE_BY_KIND" in get_web_html()


def test_graph_inspector_is_full_width_below_lg_not_a_fixed_384px() -> None:
    # A fixed w-96 (384px) drawer beside a 320px sidebar left no usable canvas at 375/768 and
    # overlapped the sidebar's own action bar outright.
    html = get_web_html()
    inspector = re.search(r'<div id="graph-inspector" class="([^"]*)"', html)
    assert inspector, "graph-inspector not found"
    classes = inspector.group(1)
    assert "w-full" in classes
    assert "lg:w-96" in classes
    assert re.search(r"(?<!lg:)w-96", classes) is None


def test_sidebar_clamps_narrower_than_the_graph_pane_it_shares() -> None:
    assert "#sidebar-pane { max-width: 50vw; }" in get_web_html()


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


def test_tri_state_grammar_is_implemented_once() -> None:
    # One gesture handler shared by status chips and every model/spec/repo popover row,
    # not one click-to-cycle per control.
    html = get_web_html()
    assert html.count("function triStateHandlers(") == 1
    body = _function_body(html, "triStateHandlers")
    assert "addEventListener('dblclick'" in body
    assert "TRI_STATE_DBLCLICK_MS" in body
    assert "e.shiftKey" in body
    assert "TRI_STATE_DBLCLICK_MS = 220" in html


def test_status_chips_use_the_tri_state_grammar_not_a_three_way_cycle() -> None:
    body = _function_body(get_web_html(), "updateStatsDigest")
    assert "triStateHandlers(chip" in body
    assert "cycleStatusMode" not in body


def test_hash_round_trips_include_and_exclude_for_every_tri_state_dimension() -> None:
    html = get_web_html()
    write_hash = _function_body(html, "writeHash")
    read_hash = _function_body(html, "readHash")
    for key in ("status", "xstatus", "repo", "xrepo", "model", "xmodel", "spec", "xspec"):
        assert f"p.get('{key}')" in read_hash, f"readHash does not read {key}"
        assert f"p.set('{key}'" in write_hash, f"writeHash does not write {key}"


def test_repo_filter_is_a_tri_state_popover_not_a_select() -> None:
    # Scoped to the toolbar: the editing UI's own dialogs (status, supersede, gate...)
    # legitimately use <select> for plain pickers unrelated to the tri-state filters.
    html = get_web_html()
    toolbar = re.search(r'<header id="toolbar".*?</header>', html, re.DOTALL)
    assert toolbar, "toolbar header not found"
    assert "<select" not in toolbar.group(0)
    assert '<div id="repo-filter" class="relative"></div>' in html
    assert "createTriStatePopover(repoFilter" in html


def test_model_and_spec_filters_are_multi_valued_tri_state_popovers() -> None:
    html = get_web_html()
    assert "createTriStatePopover(modelFilterEl" in html
    assert "createTriStatePopover(specFilterEl" in html
    # acceptable_models is a list: matching means any overlap, not exact equality.
    assert "t.acceptable_models && t.acceptable_models.length ? t.acceptable_models : []" in html


def test_every_tri_state_control_carries_no_gesture_hover_text() -> None:
    # §6.2 forbids a tooltip that explains the gesture; a row's title names the value and its
    # filter state instead, same shape as a status chip's own title.
    html = get_web_html()
    assert "Click: include" not in html
    assert "Double-click: exclude" not in html
    assert "Click again: clear" not in html
    row = _function_body(html, "renderOptions")
    assert "${esc(dimension)} ${esc(o.label)} · ${triModeLabel(mode)}" in row


def test_accessible_name_states_dimension_value_and_mode() -> None:
    html = get_web_html()
    assert "Status ${theme.label}: ${triModeLabel(mode)}" in html
    assert "'included'" in html and "'excluded'" in html and "'not filtered'" in html


def test_tri_state_popover_row_has_exactly_two_buttons_no_neutral() -> None:
    # §6.2: "exactly two icon toggle buttons, plus and minus, with no neutral button".
    html = get_web_html()
    assert "TRI_ICON_CIRCLE" not in html
    options = _function_body(html, "renderOptions")
    assert options.count("triBtn(") == 2
    assert "'include'" in options and "'exclude'" in options
    assert "'neutral'" not in options


def test_tri_state_popover_exclude_active_colour_is_red_not_gray() -> None:
    # The exclude button used to turn the same gray whether excluded or not, so its state was
    # colour-invisible; §6.2 wants it red when active.
    html = get_web_html()
    tri_btn = _function_body(html, "triBtn")
    assert "isActive ? activeClasses" in tri_btn
    options = _function_body(html, "renderOptions")
    assert "bg-red-600" in options
    assert "mode === 'exclude', 'bg-red-600" in options


def test_tri_state_popover_button_click_toggles_back_to_neutral() -> None:
    # Clicking an already-selected plus (or minus) used to re-select the same mode instead of
    # clearing it, so a second click on the green plus never returned a value to neutral.
    click = _function_body(get_web_html(), "renderOptions")
    assert "current === btn.dataset.mode ? null : btn.dataset.mode" in click


def test_tri_state_popover_buttons_carry_aria_pressed() -> None:
    tri_btn = _function_body(get_web_html(), "triBtn")
    assert 'aria-pressed="${isActive}"' in tri_btn


def test_tri_state_popover_row_is_not_nested_interactive() -> None:
    # A div[role=button][tabindex=0] wrapping two real <button>s is axe's nested-interactive
    # violation; the row is a plain, non-focusable label area now, and the group has no
    # listbox/option mismatch (aria-required-children) or missing accessible name
    # (aria-input-field-name) either.
    html = get_web_html()
    options = _function_body(html, "renderOptions")
    assert 'role="button"' not in options
    assert 'tabindex="0"' not in options
    assert 'role="group" aria-label="${esc(dimension)} values"' in html
    assert 'role="listbox"' not in html


def test_tri_state_popover_label_and_count_do_not_share_one_truncated_span() -> None:
    # A long label used to truncate together with its count in one <span>, hiding the count.
    options = _function_body(get_web_html(), "renderOptions")
    assert '<span class="truncate min-w-0 flex-1">${esc(o.label)}</span>' in options
    assert '<span class="text-zinc-500 flex-shrink-0">(${o.count})</span>' in options


def test_tri_state_popover_closes_on_escape_and_returns_focus() -> None:
    body = _function_body(get_web_html(), "createTriStatePopover")
    assert "e.key === 'Escape'" in body
    assert "close(true)" in body
    assert ".tri-btn-main').focus()" in body


def test_tri_state_popover_clamps_to_the_viewport() -> None:
    html = get_web_html()
    body = _function_body(html, "createTriStatePopover")
    assert "clampToViewport" in body
    shared = _function_body(html, "clampToViewport")
    assert "window.innerWidth" in shared


def test_new_menu_also_clamps_to_the_viewport() -> None:
    # right-0 overflowed off-screen to the left at 375px, same shape as the filter popovers,
    # so it shares the one clampToViewport() helper rather than a second copy of the fix.
    body = _function_body(get_web_html(), "renderNewMenu")
    assert "clampToViewport(pop)" in body


def test_status_chip_rebuild_preserves_keyboard_focus() -> None:
    # Enter on a status chip is how a keyboard user applies a filter; the digest fully
    # rebuilds its chips on every render, which used to drop focus to BODY so a following
    # Shift+Enter landed on nothing.
    body = _function_body(get_web_html(), "updateStatsDigest")
    assert "statsDigest.contains(document.activeElement)" in body
    assert "toFocus.focus()" in body


def test_add_dependency_is_a_picker_not_free_text() -> None:
    # §6.3: a search picker over ids and titles, and "Wait on decision" is the same picker
    # filtered to decisions -- both used to be missing, the field a bare free-text input.
    body = _function_body(get_web_html(), "openAddDependencyDialog")
    assert "decisionsOnly" in body
    assert 'list="${listId}"' in body
    assert "<datalist" in body
    detail = _function_body(get_web_html(), "renderDependencies")
    assert "+ Wait on decision" in detail
    wire = _function_body(get_web_html(), "wireDependencyControls")
    assert "openAddDependencyDialog(node, true)" in wire


def test_decision_answer_form_custom_text_clears_the_chosen_cards_highlight() -> None:
    # Typing a custom answer used to clear chosenOption while the previously picked card kept
    # its emerald highlight, showing a choice the form would not actually send.
    body = _function_body(get_web_html(), "wireDecisionAnswerForm")
    assert "paintChosen(null)" in body
    assert "aria-checked" in body
    assert 'role="radio"' in _function_body(get_web_html(), "optionCardHtml")


def test_decision_withdraw_collects_a_reason() -> None:
    body = _function_body(get_web_html(), "wireDecisionAnswerForm")
    assert "wd-reason" in body
    assert "reason: ''" not in body


def test_open_decision_offers_editing_its_blocked_tasks() -> None:
    # §6.4: an open decision offers editing of blocked tasks; POST .../blocks was never
    # called from the page at all before this.
    detail = _function_body(get_web_html(), "renderDecisionDetail")
    assert "dec-block-add" in detail
    assert "dec-block-remove" in detail
    assert "/blocks`, { add:" in detail
    assert "/blocks`, { remove:" in detail


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
    assert "frontmatterValueKind(key, value)" in row
    assert "<textarea" in row


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed to exercise the JS")
def test_frontmatter_value_kind_sends_json_not_object_object(tmp_path: Path) -> None:
    # An attachments-shaped array (objects, not paths) used to render through the same
    # newline-join as declared_files, which stringifies each element to the literal text
    # "[object Object]" and, on save, sent that text back as the field's new value -- silently
    # destroying every attachment (or any other object-valued field) an ordinary title edit
    # touched. `declared_files` and a genuine string list still take the one-path-per-line form.
    fn = _function_body(get_web_html(), "frontmatterValueKind")
    script = tmp_path / "check.js"
    script.write_text(
        f"function frontmatterValueKind(key, value) {{{fn}\n}}\n"
        "const assert = require('node:assert');\n"
        "assert.strictEqual(frontmatterValueKind('declared_files', []), 'list');\n"
        "assert.strictEqual(frontmatterValueKind('x', ['a', 'b']), 'list');\n"
        "assert.strictEqual(frontmatterValueKind('attachments', [{asset: 'a.png'}]), 'json');\n"
        "assert.strictEqual(frontmatterValueKind('decision', {options: []}), 'json');\n"
        "assert.strictEqual(frontmatterValueKind('x', 'hello'), 'scalar');\n"
        "assert.strictEqual(frontmatterValueKind('x', 5), 'scalar');\n"
        "console.log('OK');\n",
        encoding="utf-8",
    )
    result = subprocess.run(["node", str(script)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_destructive_actions_confirm_before_writing() -> None:
    html = get_web_html()
    for fn_name in ("removeDependency", "removeSection", "removeVerification", "releaseLease"):
        body = _function_body(html, fn_name)
        assert "confirmDialog(" in body, f"{fn_name} does not confirm before writing"
    abandon = _function_body(html, "changeStatus")
    assert "confirmDialog(" in abandon
    assert "status === 'ABANDONED'" in abandon


def test_confirm_dialog_closes_before_the_reload_not_after() -> None:
    # A confirm has no fields left to correct on a refusal, so it closes as soon as the button
    # is pressed; onConfirm routinely ends in a tree/graph reload that used to keep the dialog
    # open (Withdraw measured closing 65s after its own success toast on a large estate).
    body = _function_body(get_web_html(), "confirmDialog")
    close_index = body.index("close();")
    confirm_index = body.index("await onConfirm();")
    assert close_index < confirm_index
    assert "catch (err)" in body


def test_edit_dialog_only_offers_models_repo_and_frontmatter_for_tasks() -> None:
    body = _function_body(get_web_html(), "openEditNodeDialog")
    assert "isTask ? fieldRow('Acceptable models" in body
    assert "isTask ? fieldRow('Target repo" in body
    assert "isTask ? frontmatterEditorHtml(node.frontmatter) : ''" in body


def test_status_digest_and_legend_never_render_not_started() -> None:
    # §3.2a: NOT_STARTED is a stored status, never a live one a task's own virtual_status can
    # equal, so a chip for it could only ever read a count of zero.
    html = get_web_html()
    digest = _function_body(html, "updateStatsDigest")
    assert "code !== 'NOT_STARTED'" in digest
    legend = _function_body(html, "renderLegend")
    assert "t.code !== 'NOT_STARTED'" in legend


def test_decision_dependency_row_uses_open_answered_withdrawn_not_node_status() -> None:
    html = get_web_html()
    table = _function_body(html, "renderRelationTable")
    assert "d.kind === 'decision'" in table
    assert "decisionStatusIcon(d.status)" in table
    assert "NOT_STARTED: 'help-circle'" in html


def test_action_bar_hides_a_transition_already_at_its_own_target() -> None:
    # Reopen showed on an already NOT_STARTED task, Mark completed/Defer/Abandon each showed
    # while already the status they claim to set.
    body = _function_body(get_web_html(), "renderActionBar")
    assert "node.status !== 'COMPLETED'" in body
    assert "node.status !== 'DEFERRED'" in body
    assert "node.status !== 'NOT_STARTED'" in body
    assert "node.status !== 'ABANDONED'" in body


def test_other_status_and_gate_pickers_omit_not_started() -> None:
    html = get_web_html()
    assert "'NOT_STARTED', 'IMPLEMENTING'" not in html
    assert "REAL_NODE_STATUSES = [\n  'IMPLEMENTING'" in html


def test_remove_confirmations_name_the_thing_not_its_internal_id() -> None:
    html = get_web_html()
    render_ver = _function_body(html, "renderVerifications")
    assert 'aria-label="Remove verification ${esc(v.target_path)}"' in render_ver
    remove_ver = _function_body(html, "removeVerification")
    assert 'target ? `"${target}" `' in remove_ver
    detach = _function_body(html, "detachAttachment")
    assert "name || asset" in detach


def test_view_switcher_keeps_its_square_shape_across_every_mode() -> None:
    # The first view switch replaced the square h-full aspect-square classes with a
    # differently-shaped px-3 py-1.5 string, so the button visibly changed size.
    html = get_web_html()
    set_view_mode = _function_body(html, "setViewMode")
    assert "VIEW_BTN_ACTIVE" in set_view_mode
    assert "VIEW_BTN_INACTIVE" in set_view_mode
    assert "aspect-square" in html
    assert "px-3 py-1.5 rounded-md font-medium bg-zinc-800" not in html
    # decisions.js's own setViewMode wrapper had the identical bug for its third view.
    assert "viewDocBtn.className = VIEW_BTN_INACTIVE" in html


def test_api_helper_surfaces_the_servers_own_refusal_message() -> None:
    body = _function_body(get_web_html(), "api")
    assert "data && data.detail" in body
    assert "!res.ok" in body


def test_attach_file_button_is_in_the_tab_order() -> None:
    # The file input is display:none (out of the tab order by construction); the wrapping
    # <label> is the reachable control, but a <label> has no native keyboard activation the
    # way a <button> does, so it needs both a tab stop and its own Enter/Space handler.
    html = get_web_html()
    render = _function_body(html, "renderAttachments")
    assert 'tabindex="0" class="att-add-btn' in render
    assert 'tabindex="-1">' in render
    wire = _function_body(html, "wireAttachmentControls")
    assert "addBtn.addEventListener('keydown'" in wire
    assert "fileInput.click()" in wire


def test_lightbox_is_a_modal_dialog_with_a_focus_trap() -> None:
    body = _function_body(get_web_html(), "openLightbox")
    assert "role', 'dialog'" in body
    assert "aria-modal', 'true'" in body
    assert "e.key === 'Tab'" in body
    assert "closeBtn.focus()" in body


def test_load_indicator_shows_while_tree_and_graph_are_loading() -> None:
    # /api/tree and /api/graph each take ~12s on the estate's own 541-task tree; the pane
    # stayed blank with nothing to tell a slow load from a broken one, on first load and
    # again after every write.
    html = get_web_html()
    assert 'id="load-indicator"' in html
    body = _function_body(html, "loadAllData")
    assert "loadIndicator.classList.remove('hidden')" in body
    assert "loadIndicator.classList.add('hidden')" in body
    assert "finally" in body
