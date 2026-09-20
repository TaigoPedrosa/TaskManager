"""Web visualizer package for TaskManager."""

from taskmanager.web.app import create_app
from taskmanager.web.static_export import export_static_html
from taskmanager.web.ui import get_web_html

__all__ = ["create_app", "export_static_html", "get_web_html"]
