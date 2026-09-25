"""Nothing in the package names the pre-lifecycle vocabulary."""

import re
from pathlib import Path

import taskmanager

FORBIDDEN = re.compile(
    r"\b("
    + "|".join(  # noqa: FLY002
        [
            "NodeStatus",
            "VirtualStatus",
            "NOT_STARTED",
            "WAITING_FIXES",
            "IN_FLIGHT",
            "GraphEngine",
            "ExecutionCoordinator",
            "discover_batch",
            "gate_satisfied",
            "REVIEW_GATE",
            "review_gate",
            "virtual_status",
            "get_spec_connection",
            "get_runtime_connection",
            "get_dependency_edges",
            "_LEGACY",
            "_LEGACY_DECISION",
        ]
    )
    + r")\b"
)


def test_the_package_names_no_pre_lifecycle_vocabulary() -> None:
    root = Path(taskmanager.__file__).parent
    hits = [
        f"{path.relative_to(root)}:{number}: {match.group(1)}"
        for path in sorted(root.rglob("*"))
        if path.suffix in {".py", ".js", ".html", ".css"}
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        for match in FORBIDDEN.finditer(line)
    ]
    assert hits == []


def test_the_engine_package_has_no_graph_runtime_or_wave_module() -> None:
    root = Path(taskmanager.__file__).parent / "engine"
    assert [name for name in ("graph.py", "runtime.py", "wave.py") if (root / name).exists()] == []
