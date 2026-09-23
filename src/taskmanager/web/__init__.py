"""Web visualizer package for TaskManager."""

from taskmanager.web.app import create_app
from taskmanager.web.enums import AppIcon, StatusVisual, WebViewMode
from taskmanager.web.ui import get_web_html

__all__ = [
    "AppIcon",
    "StatusVisual",
    "WebViewMode",
    "create_app",
    "get_web_html",
]
