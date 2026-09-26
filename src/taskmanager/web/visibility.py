"""Server-side filters, visible sets, facets and projected edges: `filters.js`'s semantics
ported rule for rule (its lines 1-55 and 173-196) so the live view can decide, for a
subscription's filters and open set, which rows it holds without a client round trip.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from taskmanager.core.status import DisplayStatus, Phase

NO_REPO = "(none)"
NO_SPEC = "(none)"
NO_PHASE = "(none)"

_STATUS_CODES = frozenset(s.value for s in DisplayStatus)
_PHASE_CODES = frozenset(p.value for p in Phase)


@dataclass(frozen=True)
class Filters:
    status_mode: Mapping[str, str] = field(default_factory=dict)
    phase_mode: Mapping[str, str] = field(default_factory=dict)
    repo_mode: Mapping[str, str] = field(default_factory=dict)
    model_mode: Mapping[str, str] = field(default_factory=dict)
    spec_mode: Mapping[str, str] = field(default_factory=dict)
    score_min: float | None = None
    score_max: float | None = None
    q: str = ""


def _split(raw: str) -> list[str]:
    return [v for v in raw.split(",") if v]


def _mode_map(
    params: Mapping[str, str], include_key: str, exclude_key: str, valid: frozenset[str] | None
) -> dict[str, str]:
    mode: dict[str, str] = {}
    for value in _split(params.get(include_key, "")):
        if valid is None or value in valid:
            mode[value] = "include"
    for value in _split(params.get(exclude_key, "")):
        if valid is None or value in valid:
            mode[value] = "exclude"
    return mode


def _parse_score(raw: str | None) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except ValueError as e:
        raise ValueError(f"smin/smax must be a number, got {raw!r}") from e


def parse_filters(params: Mapping[str, str]) -> Filters:
    """Mirrors `filters.js` `readHash`'s parsing, minus the DOM it also touches."""
    return Filters(
        status_mode=_mode_map(params, "status", "xstatus", _STATUS_CODES),
        phase_mode=_mode_map(params, "phase", "xphase", _PHASE_CODES),
        repo_mode=_mode_map(params, "repo", "xrepo", None),
        model_mode=_mode_map(params, "model", "xmodel", None),
        spec_mode=_mode_map(params, "spec", "xspec", None),
        score_min=_parse_score(params.get("smin")),
        score_max=_parse_score(params.get("smax")),
        q=(params.get("q") or "").lower(),
    )


def _dimension_passes(mode: Mapping[str, str], values: Sequence[str]) -> bool:
    # mirrors filters.js `dimensionPasses`
    if any(mode.get(v) == "exclude" for v in values):
        return False
    any_includes = any(m == "include" for m in mode.values())
    return not any_includes or any(mode.get(v) == "include" for v in values)


def _structural_filter_active(filters: Filters) -> bool:
    # mirrors filters.js `structuralFilterActive`
    return bool(
        filters.status_mode
        or filters.phase_mode
        or filters.repo_mode
        or filters.model_mode
        or filters.spec_mode
        or filters.score_min is not None
        or filters.score_max is not None
    )


def _spec_of(rows: Mapping[str, Mapping[str, Any]], node_id: str) -> str:
    # mirrors the `_specId` filters.js's `collectTasks` stamps on every task: its nearest
    # spec ancestor, NO_SPEC when it hangs off a standalone plan or nothing at all
    current: str | None = rows[node_id]["parent"]
    while current is not None:
        row = rows.get(current)
        if row is None:
            return NO_SPEC
        if row["kind"] == "spec":
            return current
        current = row["parent"]
    return NO_SPEC


def _task_passes(
    filters: Filters, rows: Mapping[str, Mapping[str, Any]], row: Mapping[str, Any]
) -> bool:
    # mirrors filters.js `taskPasses`
    if not _dimension_passes(filters.status_mode, [row["display"]]):
        return False
    if not _dimension_passes(filters.phase_mode, [row["phase"] or NO_PHASE]):
        return False
    if not _dimension_passes(filters.repo_mode, [row["target_repo"] or NO_REPO]):
        return False
    if not _dimension_passes(filters.model_mode, row["acceptable_models"] or []):
        return False
    if not _dimension_passes(filters.spec_mode, [_spec_of(rows, row["id"])]):
        return False
    score = row["score"]
    if (
        filters.score_min is not None
        and isinstance(score, int | float)
        and score < filters.score_min
    ):
        return False
    return not (
        filters.score_max is not None
        and isinstance(score, int | float)
        and score > filters.score_max
    )


def _text_matches(filters: Filters, row: Mapping[str, Any]) -> bool:
    # mirrors filters.js `textMatches`
    return filters.q in row["title"].lower() or filters.q in row["id"].lower()


def _text_accepts(filters: Filters, row: Mapping[str, Any], parent_text_ok: bool) -> bool:
    # mirrors filters.js `textAccepts`
    return filters.q == "" or parent_text_ok or _text_matches(filters, row)


def _children_by_parent(rows: Mapping[str, Mapping[str, Any]]) -> dict[str | None, list[str]]:
    by_parent: dict[str | None, list[str]] = {}
    for row in rows.values():
        by_parent.setdefault(row["parent"], []).append(row["id"])
    for children in by_parent.values():
        children.sort(key=lambda node_id: (rows[node_id]["ordinal"], node_id))
    return by_parent


def _text_ok_by_id(
    rows: Mapping[str, Mapping[str, Any]],
    by_parent: Mapping[str | None, list[str]],
    filters: Filters,
) -> dict[str, bool]:
    # A node's textOk also holds for every one of its descendants (filters.js's `nodeVisible`
    # passes it down as `parentTextOk`), so this is a single top-down pass, not per-node.
    text_ok: dict[str, bool] = {}

    def visit(node_id: str, parent_text_ok: bool) -> None:
        ok = _text_accepts(filters, rows[node_id], parent_text_ok)
        text_ok[node_id] = ok
        for child in by_parent.get(node_id, []):
            visit(child, ok)

    for root in by_parent.get(None, []):
        visit(root, False)
    return text_ok


def _node_visible_by_id(
    rows: Mapping[str, Mapping[str, Any]],
    by_parent: Mapping[str | None, list[str]],
    filters: Filters,
    text_ok: Mapping[str, bool],
) -> dict[str, bool]:
    # mirrors filters.js `nodeVisible`, computed once for every node (it always explores the
    # whole subtree regardless of what is open) rather than per call
    visible: dict[str, bool] = {}
    structural_active = _structural_filter_active(filters)

    def visit(node_id: str) -> bool:
        if node_id in visible:
            return visible[node_id]
        row = rows[node_id]
        if row["kind"] == "task":
            result = text_ok[node_id] and _task_passes(filters, rows, row)
        else:
            result = any(visit(child) for child in by_parent.get(node_id, [])) or (
                not structural_active and text_ok[node_id]
            )
        visible[node_id] = result
        return result

    for node_id in rows:
        visit(node_id)
    return visible


def visible_ids(
    rows: Mapping[str, Mapping[str, Any]], filters: Filters, open: Sequence[str]
) -> list[str]:
    by_parent = _children_by_parent(rows)
    text_ok = _text_ok_by_id(rows, by_parent, filters)
    node_visible = _node_visible_by_id(rows, by_parent, filters, text_ok)
    open_set = set(open)
    result: list[str] = []

    def walk(node_id: str) -> None:
        if not node_visible.get(node_id, False):
            return
        result.append(node_id)
        if node_id in open_set:
            for child in by_parent.get(node_id, []):
                walk(child)

    for node_id in by_parent.get(None, []):
        walk(node_id)
    return result


def _passes_other_dimensions(
    filters: Filters, rows: Mapping[str, Mapping[str, Any]], row: Mapping[str, Any], exclude: str
) -> bool:
    # mirrors filters.js `passesOtherDimensions`
    if exclude != "status" and not _dimension_passes(filters.status_mode, [row["display"]]):
        return False
    if exclude != "phase" and not _dimension_passes(filters.phase_mode, [row["phase"] or NO_PHASE]):
        return False
    if exclude != "repo" and not _dimension_passes(
        filters.repo_mode, [row["target_repo"] or NO_REPO]
    ):
        return False
    if exclude != "model" and not _dimension_passes(
        filters.model_mode, row["acceptable_models"] or []
    ):
        return False
    if exclude != "spec" and not _dimension_passes(filters.spec_mode, [_spec_of(rows, row["id"])]):
        return False
    score = row["score"]
    if (
        filters.score_min is not None
        and isinstance(score, int | float)
        and score < filters.score_min
    ):
        return False
    if (
        filters.score_max is not None
        and isinstance(score, int | float)
        and score > filters.score_max
    ):
        return False
    return not (filters.q != "" and not _text_matches(filters, row))


def _dimension_counts(
    filters: Filters,
    rows: Mapping[str, Mapping[str, Any]],
    dimension: str,
    values_of: Any,
) -> dict[str, int]:
    # mirrors filters.js `computeDimensionCounts`
    counts: dict[str, int] = {}
    for row in rows.values():
        if row["kind"] != "task" or not _passes_other_dimensions(filters, rows, row, dimension):
            continue
        for value in values_of(row):
            counts[value] = counts.get(value, 0) + 1
    return counts


def facets(rows: Mapping[str, Mapping[str, Any]], filters: Filters) -> dict[str, Any]:
    scores = [
        row["score"]
        for row in rows.values()
        if row["kind"] == "task" and isinstance(row["score"], int | float)
    ]
    return {
        "status": _dimension_counts(filters, rows, "status", lambda r: [r["display"]]),
        "phase": _dimension_counts(filters, rows, "phase", lambda r: [r["phase"] or NO_PHASE]),
        "repo": _dimension_counts(filters, rows, "repo", lambda r: [r["target_repo"] or NO_REPO]),
        "model": _dimension_counts(filters, rows, "model", lambda r: r["acceptable_models"] or []),
        "spec": _dimension_counts(filters, rows, "spec", lambda r: [_spec_of(rows, r["id"])]),
        "score": {"min": min(scores), "max": max(scores)} if scores else {"min": 0, "max": 100},
    }


def _nearest_visible_ancestor(
    rows: Mapping[str, Mapping[str, Any]], visible_set: set[str], node_id: str
) -> str | None:
    row = rows.get(node_id)
    if row is None:
        return None
    current: str | None = row["parent"]
    while current is not None:
        if current in visible_set:
            return current
        current = rows[current]["parent"]
    return None


def _map_end(
    rows: Mapping[str, Mapping[str, Any]], visible_set: set[str], node_id: str
) -> str | None:
    if node_id in visible_set:
        return node_id
    return _nearest_visible_ancestor(rows, visible_set, node_id)


def project_edges(
    edges: Sequence[Sequence[str]], rows: Mapping[str, Mapping[str, Any]], visible: Sequence[str]
) -> list[list[str]]:
    visible_set = set(visible)
    result: list[list[str]] = []
    seen: set[tuple[str, str, str]] = set()

    def add(source: str, target: str, kind: str) -> None:
        key = (source, target, kind)
        if key in seen:
            return
        seen.add(key)
        result.append([source, target, kind])

    for source, target, kind in edges:
        if kind != "depends_on":
            continue
        mapped_source = _map_end(rows, visible_set, source)
        mapped_target = _map_end(rows, visible_set, target)
        if mapped_source is None or mapped_target is None or mapped_source == mapped_target:
            continue
        add(mapped_source, mapped_target, "depends_on")

    for node_id in visible:
        parent = rows[node_id]["parent"]
        if parent in visible_set:
            add(parent, node_id, "contains")

    return result
