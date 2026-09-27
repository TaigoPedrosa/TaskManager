"""Importing a document whose spec and plan share an id is refused before it touches the engine,
so the refusal costs nothing extra on an estate the size of a real installation."""

import time
from pathlib import Path
from typing import Any

import pytest
from estate import seed

from taskmanager.renderers.importers import BulkImporter

_LIVE_ESTATE_PLANS = 15  # seed(plans=15) -> 1 spec + 15 plans + 120 tasks = 136 nodes
_REFUSAL_BUDGET_SECONDS = 2.0


def _dup_id_shape() -> dict[str, Any]:
    return {
        "spec": {"id": "DUP", "title": "spec"},
        "plans": [{"id": "DUP", "title": "plan", "tasks": [{"id": "DUP-a", "title": "a"}]}],
    }


def test_a_duplicate_id_import_refuses_within_two_seconds_on_a_live_sized_estate(
    tmp_path: Path,
) -> None:
    node_repo = seed(tmp_path, plans=_LIVE_ESTATE_PLANS)
    assert len(node_repo.list_nodes()) >= 119
    importer = BulkImporter(node_repo)

    start = time.perf_counter()
    with pytest.raises(ValueError, match="nothing written"):
        importer.import_dict(_dup_id_shape())
    elapsed = time.perf_counter() - start

    assert elapsed < _REFUSAL_BUDGET_SECONDS
    assert node_repo.get_node("DUP") is None
