"""Enums and visual theme registries for the TaskManager web visualizer."""

from enum import Enum, StrEnum
from typing import Any, NamedTuple


class WebViewMode(StrEnum):
    DOCUMENT = "document"
    GRAPH = "graph"


class IconData(NamedTuple):
    name: str
    inner_svg: str


class AppIcon(Enum):
    PLAY_CIRCLE = IconData(
        "play-circle",
        '<circle cx="12" cy="12" r="10"/><polygon points="10 8 16 12 10 16"/>',
    )
    FLAME = IconData(
        "flame",
        '<path d="M8.5 14.5A2.5 2.5 0 0 0 11 12c0-1.38-.5-2-1-3-1.072-2.143-.224-4.054 2-6 .5 2.5 2 4.9 4 6.5 2 1.6 3 3.5 3 5.5a7 7 0 1 1-14 0c0-1.153.433-2.294 1-3a2.5 2.5 0 0 0 2.5 2.5z"/>',
    )
    PLAY = IconData(
        "play",
        '<polygon points="6 3 20 12 6 21 6 3"/>',
    )
    CLOCK = IconData(
        "clock",
        '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
    )
    EYE = IconData(
        "eye",
        '<path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/>',
    )
    ALERT_TRIANGLE = IconData(
        "alert-triangle",
        '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><line x1="12" x2="12" y1="9" y2="13"/><line x1="12" x2="12.01" y1="17" y2="17"/>',
    )
    WRENCH = IconData(
        "wrench",
        '<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/>',
    )
    GIT_PULL_REQUEST = IconData(
        "git-pull-request",
        '<circle cx="18" cy="18" r="3"/><circle cx="6" cy="6" r="3"/><path d="M13 6h3a2 2 0 0 1 2 2v7"/><line x1="6" x2="6" y1="9" y2="21"/>',
    )
    CHECK_CIRCLE_2 = IconData(
        "check-circle-2",
        '<circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/>',
    )
    LOCK = IconData(
        "lock",
        '<rect width="18" height="11" x="3" y="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
    )
    CIRCLE_DASHED = IconData(
        "circle-dashed",
        '<path d="M10.1 2.18a9.93 9.93 0 0 1 3.8 0"/><path d="M17.6 3.71a9.95 9.95 0 0 1 2.69 2.7"/><path d="M21.82 10.1a9.93 9.93 0 0 1 0 3.8"/><path d="M20.29 17.6a9.95 9.95 0 0 1-2.7 2.69"/><path d="M13.9 21.82a9.94 9.94 0 0 1-3.8 0"/><path d="M6.4 20.29a9.95 9.95 0 0 1-2.69-2.7"/><path d="M2.18 13.9a9.93 9.93 0 0 1 0-3.8"/><path d="M3.71 6.4a9.95 9.95 0 0 1 2.7-2.69"/>',
    )
    ARCHIVE = IconData(
        "archive",
        '<rect width="20" height="5" x="2" y="3" rx="1"/><path d="M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8"/><path d="M10 12h4"/>',
    )
    X_CIRCLE = IconData(
        "x-circle",
        '<circle cx="12" cy="12" r="10"/><path d="m15 9-6 6"/><path d="m9 9 6 6"/>',
    )
    PAUSE_CIRCLE = IconData(
        "pause-circle",
        '<circle cx="12" cy="12" r="10"/><line x1="10" x2="10" y1="15" y2="9"/><line x1="14" x2="14" y1="15" y2="9"/>',
    )
    LAYERS = IconData(
        "layers",
        '<polygon points="12 2 2 7 12 12 22 7 12 2"/><polyline points="2 17 12 22 22 17"/><polyline points="2 12 12 17 22 12"/>',
    )
    FILE_TEXT = IconData(
        "file-text",
        '<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/><path d="M10 9H8"/><path d="M16 13H8"/><path d="M16 17H8"/>',
    )
    NETWORK = IconData(
        "network",
        '<rect x="16" y="16" width="6" height="6" rx="1"/><rect x="2" y="16" width="6" height="6" rx="1"/><rect x="9" y="2" width="6" height="6" rx="1"/><path d="M5 16v-3a1 1 0 0 1 1-1h12a1 1 0 0 1 1 1v3"/><path d="M12 12V8"/>',
    )
    ROTATE_CW = IconData(
        "rotate-cw",
        '<path d="M21 12a9 9 0 1 1-9-9c2.52 0 4.93 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/>',
    )
    SEARCH = IconData(
        "search",
        '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
    )
    CHEVRON_RIGHT = IconData(
        "chevron-right",
        '<path d="m9 18 6-6-6-6"/>',
    )
    CHEVRON_DOWN = IconData(
        "chevron-down",
        '<path d="m6 9 6 6 6-6"/>',
    )
    SHIELD_CHECK = IconData(
        "shield-check",
        '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10"/><path d="m9 12 2 2 4-4"/>',
    )
    GIT_BRANCH = IconData(
        "git-branch",
        '<line x1="6" x2="6" y1="3" y2="15"/><circle cx="18" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M18 9a9 9 0 0 1-9 9"/>',
    )
    MAXIMIZE_2 = IconData(
        "maximize-2",
        '<polyline points="15 3 21 3 21 9"/><polyline points="9 21 3 21 3 15"/><line x1="21" x2="14" y1="3" y2="10"/><line x1="3" x2="10" y1="21" y2="14"/>',
    )
    X = IconData(
        "x",
        '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
    )
    CHEVRONS_UP_DOWN = IconData(
        "chevrons-up-down",
        '<path d="m7 15 5 5 5-5"/><path d="m7 9 5-5 5 5"/>',
    )
    BOT = IconData(
        "bot",
        '<path d="M12 8V4H8"/><rect width="16" height="12" x="4" y="8" rx="2"/><path d="M2 14h2"/><path d="M20 14h2"/><path d="M15 13v2"/><path d="M9 13v2"/>',
    )
    CHECK = IconData(
        "check",
        '<path d="M20 6 9 17l-5-5"/>',
    )
    INFO = IconData(
        "info",
        '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/>',
    )

    def as_symbol(self) -> str:
        return (
            f'<symbol id="icon-{self.value.name}" viewBox="0 0 24 24" fill="none" '
            f'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
            f"{self.value.inner_svg}</symbol>"
        )

    @classmethod
    def generate_svg_sprite(cls) -> str:
        symbols = "\n  ".join(icon.as_symbol() for icon in cls)
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" style="display: none;">\n  {symbols}\n</svg>'
        )


class StatusGroup(Enum):
    NOT_STARTED = "Not started"
    IN_PROGRESS = "In progress"
    WAITING = "Waiting"
    FINISHED = "Finished"
    SET_ASIDE = "Set aside"


class StatusTheme(NamedTuple):
    code: str
    label: str
    icon: AppIcon
    group: StatusGroup
    description: str
    dark_fg: str
    dark_bg: str
    light_fg: str
    light_bg: str


class StatusVisual(Enum):
    NOT_STARTED = StatusTheme(
        "NOT_STARTED",
        "Not Started",
        AppIcon.CIRCLE_DASHED,
        StatusGroup.NOT_STARTED,
        "Recorded but not begun; shown as Ready or Blocked once its dependencies are checked.",
        "#a1a1aa",
        "#27272a",
        "#52525b",
        "#f4f4f5",
    )
    READY = StatusTheme(
        "READY",
        "Ready",
        AppIcon.PLAY_CIRCLE,
        StatusGroup.NOT_STARTED,
        "Not started and every dependency is finished, so it can be claimed now.",
        "#bef264",
        "#365314",
        "#3f6212",
        "#ecfccb",
    )
    BLOCKED = StatusTheme(
        "BLOCKED",
        "Blocked",
        AppIcon.LOCK,
        StatusGroup.NOT_STARTED,
        "Not started and at least one dependency is not yet completed or superseded.",
        "#fca5a5",
        "#7f1d1d",
        "#b91c1c",
        "#fee2e2",
    )
    IMPLEMENTING = StatusTheme(
        "IMPLEMENTING",
        "Implementing",
        AppIcon.PLAY,
        StatusGroup.IN_PROGRESS,
        "An agent is writing the change.",
        "#a5b4fc",
        "#312e81",
        "#4338ca",
        "#e0e7ff",
    )
    IN_FLIGHT = StatusTheme(
        "IN_FLIGHT",
        "In Flight",
        AppIcon.FLAME,
        StatusGroup.IN_PROGRESS,
        "An agent holds an active lease on it right now.",
        "#7dd3fc",
        "#0c4a6e",
        "#0369a1",
        "#e0f2fe",
    )
    REVIEWING = StatusTheme(
        "REVIEWING",
        "Reviewing",
        AppIcon.EYE,
        StatusGroup.IN_PROGRESS,
        "A reviewer is reading the change.",
        "#c4b5fd",
        "#4c1d95",
        "#6d28d9",
        "#ede9fe",
    )
    FIXING = StatusTheme(
        "FIXING",
        "Fixing",
        AppIcon.WRENCH,
        StatusGroup.IN_PROGRESS,
        "An agent is fixing what the review found.",
        "#f9a8d4",
        "#831843",
        "#be185d",
        "#fce7f3",
    )
    WAITING_REVIEW = StatusTheme(
        "WAITING_REVIEW",
        "Waiting Review",
        AppIcon.CLOCK,
        StatusGroup.WAITING,
        "The change is done and waits for a reviewer.",
        "#fcd34d",
        "#78350f",
        "#92400e",
        "#fef3c7",
    )
    WAITING_FIXES = StatusTheme(
        "WAITING_FIXES",
        "Waiting Fixes",
        AppIcon.ALERT_TRIANGLE,
        StatusGroup.WAITING,
        "The review found defects and the fixes wait for an agent.",
        "#fdba74",
        "#7c2d12",
        "#9a3412",
        "#ffedd5",
    )
    WAITING_MERGE = StatusTheme(
        "WAITING_MERGE",
        "Waiting Merge",
        AppIcon.GIT_PULL_REQUEST,
        StatusGroup.WAITING,
        "The review is closed and the branch waits to be merged.",
        "#5eead4",
        "#134e4a",
        "#115e59",
        "#ccfbf1",
    )
    COMPLETED = StatusTheme(
        "COMPLETED",
        "Completed",
        AppIcon.CHECK_CIRCLE_2,
        StatusGroup.FINISHED,
        "Merged and verified; the only status that counts as done.",
        "#86efac",
        "#14532d",
        "#166534",
        "#dcfce7",
    )
    SUPERSEDED = StatusTheme(
        "SUPERSEDED",
        "Superseded",
        AppIcon.ARCHIVE,
        StatusGroup.SET_ASIDE,
        "Replaced by another task; it unblocks dependents but is not counted as done.",
        "#f0abfc",
        "#701a75",
        "#a21caf",
        "#fae8ff",
    )
    ABANDONED = StatusTheme(
        "ABANDONED",
        "Abandoned",
        AppIcon.X_CIRCLE,
        StatusGroup.SET_ASIDE,
        "Dropped for good and never counted as done.",
        "#d6d3d1",
        "#44403c",
        "#57534e",
        "#e7e5e4",
    )
    DEFERRED = StatusTheme(
        "DEFERRED",
        "Deferred",
        AppIcon.PAUSE_CIRCLE,
        StatusGroup.SET_ASIDE,
        "Postponed to a later date and never counted as done.",
        "#94a3b8",
        "#1e293b",
        "#475569",
        "#e2e8f0",
    )

    @classmethod
    def from_status(cls, status: str) -> StatusVisual:
        try:
            return cls[status.upper()]
        except KeyError:
            return cls.NOT_STARTED

    def to_dict(self) -> dict[str, Any]:
        theme = self.value
        return {
            "code": theme.code,
            "label": theme.label,
            "icon": theme.icon.value.name,
            "group": theme.group.name,
            "description": theme.description,
            "dark_fg": theme.dark_fg,
            "dark_bg": theme.dark_bg,
            "light_fg": theme.light_fg,
            "light_bg": theme.light_bg,
            "graph_bg": theme.dark_bg,
            "graph_border": theme.dark_fg,
        }

    @classmethod
    def all_themes_dict(cls) -> dict[str, dict[str, Any]]:
        return {item.value.code: item.to_dict() for item in cls}

    @classmethod
    def groups_list(cls) -> list[dict[str, str]]:
        return [{"code": g.name, "label": g.value} for g in StatusGroup]

    @classmethod
    def css(cls) -> str:
        light = "".join(
            f".st-{t.code}{{--st-fg:{t.light_fg};--st-bg:{t.light_bg}}}"
            for t in (m.value for m in cls)
        )
        dark = "".join(
            f".dark .st-{t.code}{{--st-fg:{t.dark_fg};--st-bg:{t.dark_bg}}}"
            for t in (m.value for m in cls)
        )
        return light + dark
