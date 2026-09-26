"""The static export's file size for the 1,000-node estate, recorded for the report."""

from pathlib import Path

from estate import seed as seed_estate

from taskmanager.web.static_export import export_static_html

# 1 spec + 111 plans + 111 * 8 tasks = 1,000 nodes, matching the other perf suites' estate math.
_MAX_EXPORT_BYTES = 20_000_000


def test_export_size_on_the_1000_node_estate(tmp_path: Path) -> None:
    seed_estate(tmp_path, plans=111)
    out = export_static_html(tmp_path, tmp_path / "export.html")
    size = out.stat().st_size
    print(f"static export size at 1,000 nodes: {size:,} bytes")
    assert size <= _MAX_EXPORT_BYTES
