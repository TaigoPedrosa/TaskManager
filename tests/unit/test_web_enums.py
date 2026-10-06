"""The web visualizer's status and phase registries."""

import re

import pytest

from taskmanager.core.status import DisplayStatus, Phase
from taskmanager.web.enums import AppIcon, PhaseVisual, StatusGroup, StatusVisual, WebViewMode

DISPLAY_CODES = {d.value for d in DisplayStatus}
PHASE_CODES = {p.value for p in Phase}


def _luminance(hex_colour: str) -> float:
    channels = [int(hex_colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(foreground: str, background: str) -> float:
    lighter, darker = sorted((_luminance(foreground), _luminance(background)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def test_every_display_status_has_exactly_one_theme() -> None:
    assert {m.value.code for m in StatusVisual} == DISPLAY_CODES
    assert set(StatusVisual.all_themes_dict()) == DISPLAY_CODES
    assert all(m.name == m.value.code for m in StatusVisual)
    for code in DISPLAY_CODES:
        d = StatusVisual.from_status(code).to_dict()
        assert d["code"] == code and d["label"] and d["icon"]


def test_an_unknown_status_falls_back_to_the_stale_theme() -> None:
    assert StatusVisual.from_status("NOT_STARTED") == StatusVisual.STALE


def test_every_phase_has_exactly_one_theme() -> None:
    assert {m.value.code for m in PhaseVisual} == PHASE_CODES
    assert set(PhaseVisual.all_themes_dict()) == PHASE_CODES


def test_status_labels_icons_and_colours_are_distinct() -> None:
    themes = [m.value for m in StatusVisual]
    for field in ("label", "icon", "dark_fg", "light_fg"):
        values = [getattr(t, field) for t in themes]
        assert len(set(values)) == len(values), field
    assert len({(t.dark_fg, t.dark_bg) for t in themes}) == len(themes)


@pytest.mark.parametrize(
    "theme",
    [m.value for m in StatusVisual] + [m.value for m in PhaseVisual],
    ids=[f"status-{m.name}" for m in StatusVisual] + [f"phase-{m.name}" for m in PhaseVisual],
)
def test_text_contrast_is_at_least_aa_in_both_themes(theme: object) -> None:
    assert _contrast(theme.dark_fg, theme.dark_bg) >= 4.5  # type: ignore[attr-defined]
    assert _contrast(theme.light_fg, theme.light_bg) >= 4.5  # type: ignore[attr-defined]
    assert _contrast("#f3f4f6", theme.dark_bg) >= 4.5  # type: ignore[attr-defined]


def test_descriptions_are_one_sentence_and_every_group_is_used() -> None:
    for theme in [m.value for m in StatusVisual] + [m.value for m in PhaseVisual]:
        assert theme.description.endswith(".")
        assert theme.description.count(". ") == 0
    assert {m.value.group for m in StatusVisual} == set(StatusGroup)


def test_css_defines_both_theme_variables_for_every_status_and_phase() -> None:
    css = StatusVisual.css() + PhaseVisual.css()
    for prefix, codes in (("st", DISPLAY_CODES), ("ph", PHASE_CODES)):
        for code in codes:
            assert re.search(rf"(?<!\.dark )\.{prefix}-{code}\{{--st-fg:#\w+;--st-bg:#\w+\}}", css)
            assert re.search(rf"\.dark \.{prefix}-{code}\{{--st-fg:#\w+;--st-bg:#\w+\}}", css)


def test_the_landed_theme_follows_completed_and_names_its_owed_review() -> None:
    # Bar segments and the progress text follow the theme order, and the text lowercases the
    # label: "5 completed · 3 landed".
    codes = list(StatusVisual.all_themes_dict())
    assert codes.index("LANDED") == codes.index("COMPLETED") + 1
    landed = StatusVisual.LANDED.value
    assert landed.label == "Landed"
    assert landed.description.startswith("Landed, review owed: ")
    assert landed.group == StatusGroup.WAITING


def test_app_icon_sprite_carries_the_new_status_icons() -> None:
    sprite = AppIcon.generate_svg_sprite()
    assert 'id="icon-git-merge"' in sprite and 'id="icon-octagon-x"' in sprite


def test_webview_mode_values() -> None:
    assert WebViewMode.GRAPH.value == "graph"
    assert WebViewMode.WAVES.value == "waves"
