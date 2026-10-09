import json
from pathlib import Path
from typing import Any

from lifecycle_estate import add, gated, make_estate, stored

from taskmanager.core.enums import NodeKind
from taskmanager.core.status import Action, DisplayStatus
from taskmanager.engine.claims import Claims
from taskmanager.engine.discovery import discover
from taskmanager.engine.heuristics import RecommendationEngine
from taskmanager.engine.snapshot import DisplayView

LOCK_FILES = ["src/app.py", "src/db.py"]


def batch(claims: Claims, **args: Any) -> dict[str, Any]:
    options: dict[str, Any] = {"specs": None, "session": "s1", "slots": 10, "max_strong": 10}
    options.update(args)
    payload, count = discover(claims, **options)
    data: dict[str, Any] = json.loads(payload)
    assert count == len(data["chosen"])
    return data


def chosen_ids(data: dict[str, Any]) -> set[str]:
    return {entry["id"] for entry in data["chosen"]}


def next_task_ids(claims: Claims) -> set[str]:
    engine = RecommendationEngine(claims.nodes, claims.runtime, claims.snapshots)
    return {t.task_id for t in engine.get_next_tasks(limit=10)}


def test_two_repos_declaring_the_same_path_are_both_chosen_and_both_claim(
    tmp_path: Path,
) -> None:
    claims = make_estate(
        tmp_path, repos=("workers", "scheduler"), config=gated("workers", "scheduler")
    )
    add(claims, "W1", repo="workers", files=LOCK_FILES)
    add(claims, "S1", repo="scheduler", files=LOCK_FILES)

    data = batch(claims)
    assert chosen_ids(data) == {"W1", "S1"}
    assert data["held"] == []
    assert next_task_ids(claims) == {"W1", "S1"}

    w1 = claims.start("W1", "agent-w", "s1")
    s1 = claims.start("S1", "agent-s", "s1")

    assert w1.action == Action.IMPLEMENT
    assert s1.action == Action.IMPLEMENT
    assert claims.runtime.get_lease("W1") is not None
    assert claims.runtime.get_lease("S1") is not None


def test_two_same_repo_tasks_on_the_same_path_collide_in_discovery_heuristics_and_claim(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path, repos=("workers",), config=gated("workers"))
    add(claims, "W1", repo="workers", files=["src/app.py"], priority=90)
    add(claims, "W2", repo="workers", files=["src/app.py"], priority=80)

    data = batch(claims)
    assert chosen_ids(data) == {"W1"}
    assert "W2: declared_files overlap a node chosen this wave" in data["held"]
    assert next_task_ids(claims) == {"W1"}

    claims.start("W1", "agent-1", "s1")
    result = claims.start("W2", "agent-2", "s1")

    assert result.action == Action.BLOCKED
    assert "declared files locked" in (result.reason or "")
    view = DisplayView(claims.snapshots)
    assert view.display(stored(claims, "W2")) == DisplayStatus.BLOCKED_BY_LEASE.value


def test_a_container_spanning_two_repositories_locks_each_descendants_files_under_its_own_repo(
    tmp_path: Path,
) -> None:
    claims = make_estate(
        tmp_path, repos=("workers", "scheduler"), config=gated("workers", "scheduler")
    )
    add(claims, "PLAN", NodeKind.PLAN)
    add(claims, "W1", parent="PLAN", repo="workers", files=LOCK_FILES)
    add(claims, "S1", parent="PLAN", repo="scheduler", files=LOCK_FILES)

    snap = claims.snapshots.build()
    locked = claims.snapshots.lock_set("PLAN", snap)

    assert set(locked) == {
        "workers:src/app.py",
        "workers:src/db.py",
        "scheduler:src/app.py",
        "scheduler:src/db.py",
    }


def test_run_list_shows_the_qualified_keys_a_claim_wrote(tmp_path: Path) -> None:
    claims = make_estate(tmp_path, repos=("workers",), config=gated("workers"))
    add(claims, "W1", repo="workers", files=LOCK_FILES)

    claims.start("W1", "agent-w", "s1")

    locked_paths = {lock.file_path for lock in claims.runtime.list_locks()}
    assert locked_paths == {"workers:src/app.py", "workers:src/db.py"}


def test_a_manifest_or_lockfile_never_holds_back_a_second_claim(tmp_path: Path) -> None:
    claims = make_estate(tmp_path, repos=("workers",), config=gated("workers"))
    add(claims, "W1", repo="workers", files=["pyproject.toml", "uv.lock", "src/a.py"], priority=90)
    add(claims, "W2", repo="workers", files=["pyproject.toml", "uv.lock", "src/b.py"], priority=80)

    assert chosen_ids(batch(claims)) == {"W1", "W2"}
    assert next_task_ids(claims) == {"W1", "W2"}
    claims.start("W1", "agent-1", "s1")
    assert claims.start("W2", "agent-2", "s1").action != Action.BLOCKED


def test_a_container_never_locks_a_manifest_or_lockfile_its_descendants_declare(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path, repos=("workers",), config=gated("workers"))
    add(claims, "PLAN", NodeKind.PLAN)
    add(claims, "W1", parent="PLAN", repo="workers", files=["pyproject.toml", "src/a.py"])
    add(claims, "W2", parent="PLAN", repo="workers", files=["svc/uv.lock", "src/b.py"])

    snap = claims.snapshots.build()

    assert claims.snapshots.lock_set("PLAN", snap) == ["workers:src/a.py", "workers:src/b.py"]
