"""Assembles the web visualizer page from taskmanager/web/static/ into one document.

The static files are plain HTML/CSS/JS (no template braces to double), inlined in a fixed
order so the served page and the static export both stay a single self-contained document.
"""

import json
from importlib.resources import files
from typing import Any

from taskmanager.web.enums import AppIcon, PhaseVisual, StatusVisual, WebViewMode

# Load order matters: later files may reference functions defined in earlier ones at
# top-level (e.g. main.js's Initialize block calls functions core.js/filters.js/tree.js
# define), the same constraint plain sequential <script> tags impose in the browser.
_JS_FILES = (
    "core.js",
    "filters.js",
    "tree.js",
    "graph.js",
    "edit.js",
    "detail.js",
    "decisions.js",
    "main.js",
)


def _read_static(*parts: str) -> str:
    resource = files("taskmanager.web").joinpath("static", *parts)
    return resource.read_text(encoding="utf-8")


def get_web_html(initial_data: dict[str, Any] | None = None) -> str:
    embedded_script = ""
    if initial_data is not None:
        # A section's own content is user-writable (`PUT /api/nodes/{id}/sections/{key}`) and
        # ends up inside `initial_data` verbatim; a `</script>` in it would otherwise close this
        # tag early and run whatever text follows as markup, in the one output (the static
        # export) that has no server left to sanitise on the way out.
        raw_json = json.dumps(initial_data).replace("</", "<\\/")
        embedded_script = f"<script>window.STATIC_DATA = {raw_json};</script>"

    runtime_data = (
        "<script>\n"
        f"    window.STATUS_THEMES = {json.dumps(StatusVisual.all_themes_dict())};\n"
        f"    window.STATUS_GROUPS = {json.dumps(StatusVisual.groups_list())};\n"
        f"    window.PHASE_THEMES = {json.dumps(PhaseVisual.all_themes_dict())};\n"
        "    window.VIEW_MODES = {\n"
        f"      DOCUMENT: '{WebViewMode.DOCUMENT.value}',\n"
        f"      GRAPH: '{WebViewMode.GRAPH.value}'\n"
        "    };\n"
        "  </script>"
    )

    css = _read_static("app.css").replace(
        "<!--slot:status-css-->", StatusVisual.css() + PhaseVisual.css()
    )

    html = _read_static("index.html")
    html = html.replace("<!--slot:app-css-->", css)
    html = html.replace("<!--slot:runtime-data-->", runtime_data)
    html = html.replace("<!--slot:static-data-->", embedded_script)
    html = html.replace("<!--slot:icon-sprite-->", AppIcon.generate_svg_sprite())
    for name in _JS_FILES:
        slot = f"<!--slot:js-{name.removesuffix('.js')}-->"
        html = html.replace(slot, _read_static("js", name))
    return html
