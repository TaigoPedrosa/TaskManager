import re
import shutil
import subprocess
from pathlib import Path

import pytest

from taskmanager.web.ui import get_web_html


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


def test_no_toggle_all_sections_button_survives_the_document_view() -> None:
    # The "Expand all sections" toolbar button only ever showed in the Document view; no
    # other mode ever un-hid it, so it and its bookkeeping (an id list appended to on every
    # renderSections() call, only ever cleared by the Document renderer) must go together.
    html = get_web_html()
    assert 'id="toggle-sections-btn"' not in html
    assert "toggleSectionsBtn" not in html
    assert "allSectionIds" not in html


def test_status_icon_carries_a_title_and_chip_is_legend_only() -> None:
    html = get_web_html()
    status_icon = _function_body(html, "statusIcon")
    assert 'title="${esc(t.label)}"' in status_icon
    # statusChip (visible label) survives only in its own definition, the legend, and the
    # waves view's from/to transition (a wave card names no other status marker at all).
    assert html.count("statusChip(") == 3
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


def test_group_header_toggle_is_wired_independently_of_node_and_section_collapse() -> None:
    html = get_web_html()
    shared = _function_body(html, "attachGroupHeaderHandlers")
    assert "'.group-header'" in shared
    assert "collapsedGroups.has(id)) collapsedGroups.delete(id)" in shared
    assert "collapsedGroups.add(id)" in shared


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
    html = get_web_html()
    render_graph = _function_body(html, "renderGraph")
    assert "levelSeparation: 240" in render_graph
    assert "nodeSpacing: 320" in render_graph
    vis_node = _function_body(html, "graphVisNode")
    assert "size: 16" in vis_node
    assert "widthConstraint: { minimum: 170, maximum: 260 }" in vis_node
    assert "GRAPH_SHAPE_BY_KIND" in html


def test_graph_never_destroys_the_network_instance() -> None:
    # The network is built once (renderGraph); every later store change patches its DataSets
    # in place, so pan/zoom/selection are never lost to a rebuild.
    assert "networkInstance.destroy" not in _static_js("graph.js")
    sync_graph = _function_body(get_web_html(), "syncGraph")
    assert "renderGraph()" in sync_graph
    assert "syncGraphNodes(patch.rowIds)" in sync_graph
    assert "syncGraphEdges()" in sync_graph


def test_graph_container_label_is_a_counts_summary_not_a_status() -> None:
    vis_node = _function_body(get_web_html(), "graphVisNode")
    assert "progressText(countsForRow(row))" in vis_node


def test_graph_double_click_toggles_a_container_through_the_store() -> None:
    render_graph = _function_body(get_web_html(), "renderGraph")
    assert "doubleClick" in render_graph
    assert "toggleExpand(row)" in render_graph


def test_filters_js_no_longer_hides_graph_nodes_itself() -> None:
    # The server now leaves a filtered-out node out of the visible set entirely, so the
    # client has nothing left to dim after the fact.
    assert "function applyGraphFilter" not in _static_js("filters.js")


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
        "store.js": "function createStore(options)",
        "core.js": "function canEdit()",
        "filters.js": "function filtersToF()",
        "tree.js": "function renderTree()",
        "graph.js": "const GRAPH_SHAPE_BY_KIND",
        "edit.js": "function openDialog(",
        "detail.js": "async function showGraphInspector(nodeId)",
        "waves.js": "function fetchWaves()",
        "main.js": "readHash();\nrenderLegend();",
    }
    for source_file, needle in known_strings_by_file.items():
        assert needle in html, f"{source_file}'s own content ({needle!r}) missing from the page"


def test_static_files_are_packaged_and_resolve_at_runtime() -> None:
    from importlib.resources import files

    static_dir = files("taskmanager.web").joinpath("static")
    assert static_dir.joinpath("index.html").is_file()
    assert static_dir.joinpath("app.css").is_file()
    for name in (
        "store.js",
        "core.js",
        "filters.js",
        "tree.js",
        "graph.js",
        "edit.js",
        "detail.js",
        "waves.js",
        "main.js",
    ):
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
    # writeHash() and setFilters() need the same shape (the protocol's F), so both build it
    # from filtersToF() rather than each writing their own p.set(...) calls.
    html = get_web_html()
    to_f = _function_body(html, "filtersToF")
    read_hash = _function_body(html, "readHash")
    assert "new URLSearchParams(filtersToF())" in _function_body(html, "writeHash")
    for key in ("status", "xstatus", "repo", "xrepo", "model", "xmodel", "spec", "xspec"):
        assert f"p.get('{key}')" in read_hash, f"readHash does not read {key}"
        assert f"F.{key} =" in to_f, f"filtersToF does not write {key}"


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
    # acceptable_models is a list: the store's own facets already count a task once per
    # model it accepts (dimensionCounts's valuesOf), so the popover just reads that count
    # instead of re-deriving the overlap client-side.
    html = get_web_html()
    assert "createTriStatePopover(modelFilterEl" in html
    assert "createTriStatePopover(specFilterEl" in html
    assert "window.tmStore.facets.model || {}" in html
    assert "window.tmStore.facets.spec || {}" in html


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
    assert '<span class="text-zinc-400 flex-shrink-0">(${o.count})</span>' in options


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
    for fn_name in (
        "removeDependency",
        "removeSection",
        "removeVerification",
        "releaseLease",
        "removeCondition",
    ):
        body = _function_body(html, fn_name)
        assert "confirmDialog(" in body, f"{fn_name} does not confirm before writing"
    assert "destructive: verb === 'abandon'" in _function_body(html, "openVerbDialog")


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


def test_answer_byline_and_popover_count_meet_aa_contrast() -> None:
    # zinc-500 on zinc-950/zinc-900 measured 4.11:1 and 3.66:1, both below AA's 4.5:1;
    # zinc-400 clears it on the same backgrounds.
    html = get_web_html()
    assert 'text-zinc-500">by ${esc(data.answer.answered_by)}' not in html
    assert 'text-zinc-400">by ${esc(data.answer.answered_by)}' in html
    assert '<span class="text-zinc-400 flex-shrink-0">(${o.count})</span>' in html


def test_toolbar_dividers_hide_below_the_wrap_breakpoint() -> None:
    # A 1px-wide divider with no content of its own wrapped onto its own empty line once the
    # toolbar wrapped at 375/768; it hides where flex-wrap's own line break already separates
    # the groups it used to mark.
    html = get_web_html()
    assert html.count('<div class="hidden sm:block w-px h-6 bg-zinc-800 flex-shrink-0"></div>') == 2


def test_decision_option_description_renders_as_markdown() -> None:
    body = _function_body(get_web_html(), "optionCardHtml")
    assert "renderSectionBody(opt.description)" in body
    assert "esc(opt.description)" not in body


def test_dialog_refusal_also_shows_a_toast() -> None:
    html = get_web_html()
    dialog_call_site = re.search(r"form\.addEventListener\('submit'.*?\}\);", html, re.DOTALL)
    assert dialog_call_site, "dialog submit handler not found"
    catch_block = dialog_call_site.group(0).split("catch")[1].split("finally")[0]
    assert "toast(message, 'error')" in catch_block


def test_decision_option_row_inputs_are_named_by_aria_label_not_placeholder() -> None:
    body = _function_body(get_web_html(), "decisionOptionRowHtml")
    assert 'aria-label="Option key"' in body
    assert 'aria-label="Option label"' in body
    assert 'aria-label="Option description"' in body
    assert ">Recommended" in body
    assert ">rec." not in body


def test_new_decision_blocked_tasks_field_offers_a_picker() -> None:
    body = _function_body(get_web_html(), "openNewDecisionDialog")
    assert 'list="nd-blocks-list"' in body
    assert '<datalist id="nd-blocks-list">' in body


def test_decisions_tabs_are_a_real_tablist_with_arrow_navigation() -> None:
    html = get_web_html()
    body = _function_body(html, "renderDecisionsTabs")
    assert 'aria-controls="decisions-list"' in body
    assert "e.key === 'ArrowRight'" in body
    assert "decisionsListEl.setAttribute('role', 'tabpanel')" in body


def test_new_menu_supports_arrow_key_navigation_and_escape_from_an_item() -> None:
    # Esc used to be handled only on the trigger button, so it did nothing once focus had
    # moved onto a menu item; ArrowDown/ArrowUp had no handler at all.
    body = _function_body(get_web_html(), "renderNewMenu")
    assert "pop.addEventListener('keydown'" in body
    assert "e.key !== 'ArrowDown' && e.key !== 'ArrowUp'" in body
    assert "btn.focus()" in body


def test_decisions_load_failure_is_an_error_state_not_an_empty_queue() -> None:
    # A /api/decisions failure read as "No open decisions." -- an empty queue, not a broken
    # one, with the badge hiding too, which is exactly the case that most looks like nothing
    # is wrong.
    html = get_web_html()
    refresh = _function_body(html, "refreshDecisionsData")
    assert "decisionsLoadFailed = true" in refresh
    assert "toast(`Could not load decisions" in refresh
    render_list = _function_body(html, "renderDecisionsList")
    assert "if (decisionsLoadFailed)" in render_list
    assert 'role="alert"' in render_list


def test_refresh_decisions_data_reads_the_paginated_envelopes_items() -> None:
    # /api/decisions answers {items, next}; reading the response itself as the list
    # breaks decisionsData.filter with no test catching it.
    refresh = _function_body(get_web_html(), "refreshDecisionsData")
    assert "const res = await api('GET', `/api/decisions?" in refresh
    assert "decisionsData = res.items;" in refresh
    assert "decisionsNextCursor = res.next;" in refresh


def test_dialog_initial_focus_prefers_a_form_field_over_the_close_button() -> None:
    body = _function_body(get_web_html(), "openDialog")
    assert "firstFieldOrFallback().focus()" in body
    assert "(focusables()[0] || panel).focus()" not in body


def test_new_menu_item_returns_focus_to_the_trigger_button_not_body() -> None:
    # The clicked item is already hidden by setOpen(false), and Plan/Task additionally await
    # an /api/meta fetch before opening -- by the time openDialog captures
    # document.activeElement, a menu item was never a trigger that survives either delay.
    body = _function_body(get_web_html(), "renderNewMenu")
    assert "btn.focus();\n    const kind = item.getAttribute" in body


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


def test_load_indicator_follows_the_stores_pending_flag() -> None:
    # A subscribe/response round trip (opening a plan, the first snapshot) can take a while on
    # a large estate; the indicator now tracks the store's own `pending` rather than a
    # per-fetch try/finally, since there is no fetch left to wrap.
    html = get_web_html()
    assert 'id="load-indicator"' in html
    body = _function_body(html, "syncConnectionUi")
    assert "!window.tmStore.pending" in body
    # Waves' own fetch cycle runs outside the store, so it feeds the same bar through this flag.
    assert "!wavesLoadPending" in body


def test_a_refused_subscribe_notifies_the_store_and_core_toasts_it() -> None:
    # A refused subscribe (bad filters, too many watched ids) clears `pending` internally, but
    # nothing followed it back to the DOM until the store also notifies its listeners; core.js's
    # listener is what turns that into the acceptance's toast.
    html = get_web_html()
    handle_message = _function_body(html, "handleMessage")
    assert "msg.type === 'error'" in handle_message
    assert "error: msg.detail" in handle_message
    on_change = re.search(r"window\.tmStore\.onChange\(\(patch\) => \{(.*?)\}\);", html, re.DOTALL)
    assert on_change, "onChange handler not found"
    assert "toast(patch.error, 'error')" in on_change.group(1)


def test_tri_state_buttons_use_the_icon_sprite_not_inline_svg() -> None:
    html = get_web_html()
    tri_btn = _function_body(html, "triBtn")
    assert "renderIcon(iconName" in tri_btn
    assert "TRI_ICON_PLUS" not in html
    assert "TRI_ICON_MINUS" not in html
    options = _function_body(html, "renderOptions")
    assert "triBtn('include', 'plus'" in options
    assert "triBtn('exclude', 'minus'" in options


def test_status_exclude_toggle_uses_a_token_not_raw_hex() -> None:
    html = get_web_html()
    rule = re.search(r"\.st-toggle\.st-mode-exclude \{[^}]*\}", html)
    assert rule, "st-mode-exclude rule not found"
    assert "#71717a" not in rule.group(0)
    assert "#3f3f46" not in rule.group(0)
    assert "var(--tone-dim-fg)" in rule.group(0)
    assert "var(--tone-dim-border)" in rule.group(0)


def test_attachment_card_shows_size_and_source_uri_always_visible() -> None:
    # §4: name, size, source URI and capture age/staleness must all be visible on the card
    # itself, not only in a hover title.
    render = _function_body(get_web_html(), "renderAttachments")
    assert "humanBytes(entry.size_bytes)" in render
    assert "entry.source && entry.source.uri" in render
    assert 'title="${esc(uri)}">${esc(uri)}' in render


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed to exercise the JS")
def test_human_bytes_formats_and_hides_unknown_size(tmp_path: Path) -> None:
    fn = _function_body(get_web_html(), "humanBytes")
    script = tmp_path / "check.js"
    script.write_text(
        f"function humanBytes(n) {{{fn}\n}}\n"
        "const assert = require('node:assert');\n"
        "assert.strictEqual(humanBytes(null), null);\n"
        "assert.strictEqual(humanBytes(undefined), null);\n"
        "assert.strictEqual(humanBytes(512), '512 B');\n"
        "assert.strictEqual(humanBytes(86016), '84 KB');\n"
        "assert.strictEqual(humanBytes(1048576), '1 MB');\n"
        "console.log('OK');\n",
        encoding="utf-8",
    )
    result = subprocess.run(["node", str(script)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_toolbar_filters_collapse_behind_a_toggle_below_sm() -> None:
    # At 375px the five filter controls plus + New routinely wrapped the toolbar onto
    # several lines; below `sm` they now sit behind one toggle, keeping row 1 to one row.
    html = get_web_html()
    assert '<button id="filters-toggle-btn" type="button" aria-expanded="false"' in html
    assert "sm:hidden" in html.split('id="filters-toggle-btn"')[1].split(">")[0]
    assert 'id="filter-controls-group" class="hidden sm:flex' in html
    toggle = _function_body(html, "renderFiltersToggle")
    assert "activeFilterCount()" in toggle
    assert "filterControlsGroup.classList.toggle('hidden', !filtersPanelOpen)" in toggle
    assert "aria-expanded" in toggle


def test_the_page_names_no_pre_lifecycle_status_or_status_setter() -> None:
    html = get_web_html()
    for word in (
        "NOT_STARTED",
        "WAITING_FIXES",
        "IN_FLIGHT",
        "virtual_status",
        "REAL_NODE_STATUSES",
        "/status`",
        "dep-gate",
    ):
        assert word not in html, word


def test_the_page_carries_phase_themes_and_a_phase_filter() -> None:
    html = get_web_html()
    assert "window.PHASE_THEMES = " in html
    assert '<div id="phase-filter" class="relative"></div>' in html
    assert "createTriStatePopover(phaseFilterEl" in html
    read_hash = _function_body(html, "readHash")
    to_f = _function_body(html, "filtersToF")
    for key in ("phase", "xphase"):
        assert f"p.get('{key}')" in read_hash and f"F.{key} =" in to_f


def test_action_bar_offers_verbs_by_stored_status() -> None:
    body = _function_body(get_web_html(), "renderActionBar")
    assert "REOPENABLE.includes(node.status)" in body
    assert "SETTABLE_ASIDE.includes(node.status)" in body
    assert "ab-reset" in body and "ab-flags" in body
    wire = _function_body(get_web_html(), "wireActionBar")
    for verb in ("'reopen'", "'defer'", "'abandon'"):
        assert f"openVerbDialog(node, {verb})" in wire
    assert "openResetDialog(node)" in wire and "openFlagsDialog(node)" in wire


def test_every_verb_collects_a_note_and_abandon_is_destructive() -> None:
    body = _function_body(get_web_html(), "openVerbDialog")
    assert "vb-note" in body and "A note is required." in body
    assert "destructive: verb === 'abandon'" in body
    assert "vb-new-branch" in body


def test_the_inspector_shows_the_lifecycle_panel() -> None:
    html = get_web_html()
    panel = _function_body(html, "renderLifecycle")
    for field in (
        "n.status",
        "n.outcome",
        "n.verdict",
        "n.review_cycles",
        "n.merge_attempts",
        "n.step_failures",
        "n.requires",
        "detail.conditions",
        "detail.jobs",
        "c.last_result",
        "c.stage",
        "landingChainText(n)",
    ):
        assert field in panel, field
    # showGraphInspector only opens and watches the node; renderGraphInspector is the part
    # that draws the panel, re-run by the store's own onChange on every later update too.
    assert "renderLifecycle(detail, editable)" in _function_body(html, "renderGraphInspector")


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed to exercise the JS")
def test_landing_chain_text_reads_the_base_chain(tmp_path: Path) -> None:
    fn = _function_body(get_web_html(), "landingChainText")
    script = tmp_path / "check.js"
    script.write_text(
        "function esc(s) { return String(s); }\n"
        f"function landingChainText(node) {{{fn}\n}}\n"
        "const assert = require('node:assert');\n"
        "assert.strictEqual(landingChainText({base_chain: ['MAIN']}), 'lands on main');\n"
        "assert.strictEqual(landingChainText({base_chain: ['P', 'MAIN']}),"
        " 'on tm/P; waits for P → main');\n"
        "assert.strictEqual(landingChainText({base_chain: []}), '');\n"
        "console.log('OK');\n",
        encoding="utf-8",
    )
    result = subprocess.run(["node", str(script)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_decisions_read_open_answered_and_withdrawn_and_show_option_effects() -> None:
    html = get_web_html()
    assert "{ key: 'open', label: 'Open', status: 'OPEN' }" in html
    assert "OPEN: 'help-circle'" in html
    card = _function_body(html, "optionCardHtml")
    assert "opt.effect && opt.effect !== 'none'" in card


def test_static_export_themes_every_display_status(tmp_path: Path) -> None:
    import json

    from taskmanager.core.status import DisplayStatus, Phase

    html = get_web_html(initial_data={"tree": []})
    themes = json.loads(re.search(r"window.STATUS_THEMES = (\{.*?\});\n", html).group(1))  # type: ignore[union-attr]
    phases = json.loads(re.search(r"window.PHASE_THEMES = (\{.*?\});\n", html).group(1))  # type: ignore[union-attr]
    assert set(themes) == {d.value for d in DisplayStatus}
    assert set(phases) == {p.value for p in Phase}


def _static_js(name: str) -> str:
    from importlib.resources import files

    return files("taskmanager.web").joinpath("static", "js", name).read_text(encoding="utf-8")


def test_no_whole_tree_fetch_survives_in_core_js() -> None:
    # core.js now opens on one subscribe and store.js's own rows/statuses/facets; a fetch of
    # the whole tree on load, or a per-node fetch on load, would defeat that entirely.
    core = _static_js("core.js")
    for word in ("loadAllData", "treeData"):
        assert word not in core, word


def test_filters_js_no_longer_computes_visibility_itself() -> None:
    # taskPasses/nodeVisible/passesOtherDimensions/computeDimensionCounts/collectTasks all had
    # a twin already living in store.js; the store answers with the visible set and the facets
    # directly now, so this file only owns the controls and the URL hash.
    filters_src = _static_js("filters.js")
    for name in (
        "function taskPasses(",
        "function nodeVisible(",
        "function passesOtherDimensions(",
        "function computeDimensionCounts(",
        "function collectTasks(",
    ):
        assert name not in filters_src, name


def test_core_creates_the_store_and_follows_its_connection_state() -> None:
    html = get_web_html()
    assert "window.tmStore = isStaticMode" in html
    assert "createStore({ staticData: window.STATIC_DATA })" in html
    assert "window.tmStore.onChange(" in html


def test_expand_collapse_drive_the_stores_open_and_watch_sets_not_a_local_flag() -> None:
    toggle = _function_body(get_web_html(), "toggleExpand")
    assert "window.tmStore.open([row.id])" in toggle
    assert "window.tmStore.watch([row.id])" in toggle
    assert "window.tmStore.close(closed)" in toggle
    assert "window.tmStore.unwatch(closed)" in toggle


def test_graph_inspector_shows_loading_until_watched_body_arrives() -> None:
    inspector = _function_body(get_web_html(), "renderGraphInspector")
    assert "window.tmStore.bodies.get(nodeId)" in inspector
    assert "Loading" in inspector


def _class_tokens(tag: str) -> list[str]:
    match = re.search(r'class="([^"]*)"', tag)
    assert match, f"no class attribute in {tag!r}"
    return match.group(1).split()


def test_waves_view_is_the_default_and_reuses_the_documents_toolbar_slot() -> None:
    html = get_web_html()
    assert "window.VIEW_MODES.WAVES;" in html
    doc_btn = re.search(r'<button id="view-doc-btn"[^>]*>', html)
    assert doc_btn, "view-doc-btn not found"
    assert "Waves view" in doc_btn.group(0)
    assert 'id="icon-file-text"' in html
    waves_pane = re.search(r'<div id="waves-pane"[^>]*>', html)
    graph_pane = re.search(r'<section id="graph-pane"[^>]*>', html)
    network_canvas = re.search(r'<div id="network-canvas"[^>]*>', html)
    assert waves_pane and "hidden" not in _class_tokens(waves_pane.group(0))
    assert graph_pane and "hidden" not in _class_tokens(graph_pane.group(0))
    assert network_canvas and "hidden" in _class_tokens(network_canvas.group(0))


def test_waves_pane_nests_inside_graph_pane_ahead_of_its_inspector() -> None:
    # Sharing graph-pane's own #graph-inspector (rather than a second copy of the drawer) is
    # what lets a wave card open the same detail drawer showGraphInspector already provides.
    html = get_web_html()
    graph_pane = re.search(r'<section id="graph-pane".*?</section>', html, re.DOTALL)
    assert graph_pane, "graph-pane not found"
    body = graph_pane.group(0)
    assert body.index('id="waves-pane"') < body.index('id="graph-inspector"')


def test_set_view_mode_toggles_the_canvas_layer_and_waves_pane() -> None:
    body = _function_body(get_web_html(), "setViewMode")
    assert "wavesPane.classList.toggle('hidden', mode !== window.VIEW_MODES.WAVES)" in body
    assert "networkCanvas.classList.toggle('hidden', mode !== window.VIEW_MODES.GRAPH)" in body
    assert "graphFitWrap.classList.toggle('hidden', mode !== window.VIEW_MODES.GRAPH)" in body
    assert (
        "viewDocBtn.addEventListener('click', () => setViewMode(window.VIEW_MODES.WAVES))"
        in _static_js("core.js")
    )


def test_wave_size_bounds_come_from_meta_never_a_constant() -> None:
    # §"The wave-size input starts at /api/meta dispatch.wave_size and its maximum is
    # dispatch.tick_budget; neither number appears as a constant in waves.js."
    waves = _static_js("waves.js")
    assert "meta.dispatch.wave_size" in waves
    assert "meta.dispatch.tick_budget" in waves
    for literal in ("= 10", "= 40", "|| 10", "|| 40"):
        assert literal not in waves, literal


def test_wave_status_chip_folds_implemented_and_reviewed_to_waiting() -> None:
    waves = _static_js("waves.js")
    assert "IMPLEMENTED: 'WAITING_REVIEW'" in waves
    assert "REVIEWED: 'WAITING_MERGE'" in waves
    assert "FIXED: 'WAITING_REVIEW'" in waves


def test_wave_compute_disabled_while_loading_or_the_last_wave_is_empty() -> None:
    body = _function_body(_static_js("waves.js"), "waveComputeDisabled")
    assert "if (waveLoading) return true;" in body
    assert "last.entries.length === 0" in body


def test_wave_entry_card_is_a_focusable_button_naming_its_own_id_and_title() -> None:
    body = _function_body(_static_js("waves.js"), "waveEntryHtml")
    assert '<button type="button" class="wave-entry-card' in body
    assert 'aria-label="${esc(entry.id)}: ${esc(entry.title)}"' in body
    wire = _function_body(_static_js("waves.js"), "wireWavesHandlers")
    assert "showGraphInspector(btn.getAttribute('data-node-id'))" in wire


def test_wave_refetch_on_statuses_change_is_coalesced_per_frame() -> None:
    schedule = _function_body(_static_js("waves.js"), "scheduleWavesRefetch")
    assert "if (waveRefetchScheduled) return;" in schedule
    assert "requestAnimationFrame(" in schedule
    assert "patch.statusesChanged" in _static_js("waves.js")


def test_a_filter_change_pushes_to_the_store_exactly_through_one_helper() -> None:
    # Every control that mutates `filters` calls applyFilterChange(), never renderAll()
    # directly, so the URL, the store's filters and the DOM redraw always move together.
    html = get_web_html()
    apply_change = _function_body(html, "applyFilterChange")
    assert "window.tmStore.setFilters(filtersToF())" in apply_change
    assert "writeHash()" in apply_change
    render_all = _function_body(html, "renderAll")
    assert "setFilters" not in render_all


def test_a_filter_change_also_refetches_waves() -> None:
    # waveSpecFilter() reads filters.specMode directly rather than taking it as an argument
    # (waves.js's own doc comment), so a spec include/exclude change has no other way to reach
    # it -- window.tmStore's own patch never reports statusesChanged for a filter change.
    apply_change = _function_body(get_web_html(), "applyFilterChange")
    assert "scheduleWavesRefetch()" in apply_change


def test_static_export_opens_on_graph_not_waves() -> None:
    # A static export has no /api/waves behind it; only Graph renders from the rows/edges
    # get_web_html() embeds directly, so that is the one view a static export can open on.
    html = get_web_html(initial_data={"rows": {}, "edges": [], "statuses": [], "bodies": {}})
    assert "if (isStaticMode) setViewMode(window.VIEW_MODES.GRAPH);" in html


def test_no_dead_highlight_css_survives_the_tree_views_removal() -> None:
    # selectNode's Document-view branch was the only code that ever added
    # .node-highlighted; tree.js's Graph-only branch never does.
    html = get_web_html()
    assert "node-highlighted" not in html
    assert "pulse-highlight" not in html
    assert "networkInstance.selectNodes([nodeId])" in _static_js("tree.js")
    assert "node-highlighted" not in _function_body(html, "selectNode")
