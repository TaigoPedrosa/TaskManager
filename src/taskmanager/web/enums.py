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


class StatusTheme(NamedTuple):
    code: str
    label: str
    icon: AppIcon
    badge_class: str
    text_class: str
    bg_class: str
    border_class: str
    graph_bg: str
    graph_border: str


class StatusVisual(Enum):
    READY = StatusTheme(
        code="READY",
        label="Ready",
        icon=AppIcon.PLAY_CIRCLE,
        badge_class="bg-emerald-950/80 text-emerald-400 border-emerald-800",
        text_class="text-emerald-400",
        bg_class="bg-emerald-950/50",
        border_class="border-emerald-800",
        graph_bg="#065f46",
        graph_border="#10b981",
    )
    IN_FLIGHT = StatusTheme(
        code="IN_FLIGHT",
        label="In Flight",
        icon=AppIcon.FLAME,
        badge_class="bg-blue-950/80 text-blue-400 border-blue-800",
        text_class="text-blue-400",
        bg_class="bg-blue-950/50",
        border_class="border-blue-800",
        graph_bg="#1e3a8a",
        graph_border="#3b82f6",
    )
    IMPLEMENTING = StatusTheme(
        code="IMPLEMENTING",
        label="Implementing",
        icon=AppIcon.PLAY,
        badge_class="bg-indigo-950/80 text-indigo-400 border-indigo-800",
        text_class="text-indigo-400",
        bg_class="bg-indigo-950/50",
        border_class="border-indigo-800",
        graph_bg="#312e81",
        graph_border="#6366f1",
    )
    WAITING_REVIEW = StatusTheme(
        code="WAITING_REVIEW",
        label="Review Wait",
        icon=AppIcon.CLOCK,
        badge_class="bg-amber-950/80 text-amber-400 border-amber-800",
        text_class="text-amber-400",
        bg_class="bg-amber-950/50",
        border_class="border-amber-800",
        graph_bg="#78350f",
        graph_border="#f59e0b",
    )
    REVIEWING = StatusTheme(
        code="REVIEWING",
        label="Reviewing",
        icon=AppIcon.EYE,
        badge_class="bg-yellow-950/80 text-yellow-400 border-yellow-800",
        text_class="text-yellow-400",
        bg_class="bg-yellow-950/50",
        border_class="border-yellow-800",
        graph_bg="#713f12",
        graph_border="#eab308",
    )
    WAITING_FIXES = StatusTheme(
        code="WAITING_FIXES",
        label="Fixes Wait",
        icon=AppIcon.ALERT_TRIANGLE,
        badge_class="bg-rose-950/80 text-rose-400 border-rose-800",
        text_class="text-rose-400",
        bg_class="bg-rose-950/50",
        border_class="border-rose-800",
        graph_bg="#881337",
        graph_border="#f43f5e",
    )
    FIXING = StatusTheme(
        code="FIXING",
        label="Fixing",
        icon=AppIcon.WRENCH,
        badge_class="bg-pink-950/80 text-pink-400 border-pink-800",
        text_class="text-pink-400",
        bg_class="bg-pink-950/50",
        border_class="border-pink-800",
        graph_bg="#831843",
        graph_border="#ec4899",
    )
    WAITING_MERGE = StatusTheme(
        code="WAITING_MERGE",
        label="Merge Wait",
        icon=AppIcon.GIT_PULL_REQUEST,
        badge_class="bg-cyan-950/80 text-cyan-400 border-cyan-800",
        text_class="text-cyan-400",
        bg_class="bg-cyan-950/50",
        border_class="border-cyan-800",
        graph_bg="#164e63",
        graph_border="#06b6d4",
    )
    COMPLETED = StatusTheme(
        code="COMPLETED",
        label="Completed",
        icon=AppIcon.CHECK_CIRCLE_2,
        badge_class="bg-green-950/80 text-green-400 border-green-800",
        text_class="text-green-400",
        bg_class="bg-green-950/50",
        border_class="border-green-800",
        graph_bg="#14532d",
        graph_border="#22c55e",
    )
    BLOCKED = StatusTheme(
        code="BLOCKED",
        label="Blocked",
        icon=AppIcon.LOCK,
        badge_class="bg-red-950/80 text-red-400 border-red-800",
        text_class="text-red-400",
        bg_class="bg-red-950/50",
        border_class="border-red-800",
        graph_bg="#450a0a",
        graph_border="#ef4444",
    )
    NOT_STARTED = StatusTheme(
        code="NOT_STARTED",
        label="Not Started",
        icon=AppIcon.CIRCLE_DASHED,
        badge_class="bg-zinc-900/80 text-zinc-400 border-zinc-700",
        text_class="text-zinc-400",
        bg_class="bg-zinc-900/50",
        border_class="border-zinc-700",
        graph_bg="#1f2937",
        graph_border="#4b5563",
    )
    SUPERSEDED = StatusTheme(
        code="SUPERSEDED",
        label="Superseded",
        icon=AppIcon.ARCHIVE,
        badge_class="bg-purple-950/80 text-purple-400 border-purple-800",
        text_class="text-purple-400",
        bg_class="bg-purple-950/50",
        border_class="border-purple-800",
        graph_bg="#581c87",
        graph_border="#a855f7",
    )
    ABANDONED = StatusTheme(
        code="ABANDONED",
        label="Abandoned",
        icon=AppIcon.X_CIRCLE,
        badge_class="bg-stone-900/80 text-stone-400 border-stone-700",
        text_class="text-stone-400",
        bg_class="bg-stone-900/50",
        border_class="border-stone-700",
        graph_bg="#292524",
        graph_border="#78716c",
    )
    DEFERRED = StatusTheme(
        code="DEFERRED",
        label="Deferred",
        icon=AppIcon.PAUSE_CIRCLE,
        badge_class="bg-slate-900/80 text-slate-400 border-slate-700",
        text_class="text-slate-400",
        bg_class="bg-slate-900/50",
        border_class="border-slate-700",
        graph_bg="#0f172a",
        graph_border="#64748b",
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
            "badge_class": theme.badge_class,
            "text_class": theme.text_class,
            "bg_class": theme.bg_class,
            "border_class": theme.border_class,
            "graph_bg": theme.graph_bg,
            "graph_border": theme.graph_border,
        }

    @classmethod
    def all_themes_dict(cls) -> dict[str, dict[str, Any]]:
        return {item.value.code: item.to_dict() for item in cls}
