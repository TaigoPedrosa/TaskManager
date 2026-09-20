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
