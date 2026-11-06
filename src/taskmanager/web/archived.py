"""The archived specs as the web reads them: the same view, rows and bodies as the hot estate,
built over `archive.db`."""

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from taskmanager.db.cache_repo import CacheRepository
from taskmanager.db.job_repo import JobRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.engine.archive import archived_repo
from taskmanager.engine.snapshot import DisplayView, SnapshotBuilder
from taskmanager.web.bodies import BodyRepos, build_bodies
from taskmanager.web.rows import build_rows

type Stores = tuple[DisplayView, BodyRepos]


def archive_stores(
    root: Path, cache: CacheRepository, condition_ttl: int, assets_dir: Path
) -> Stores:
    nodes = archived_repo(root)
    jobs = JobRepository(nodes.db)
    view = DisplayView(
        SnapshotBuilder(nodes, RuntimeRepository(nodes.db), jobs), cache, condition_ttl
    )
    return view, BodyRepos(nodes, jobs, cache, condition_ttl, assets_dir)


def archived_rows(cold: DisplayView) -> dict[str, dict[str, Any]]:
    return build_rows(cold, frozenset(cold.snapshot.graph_data().nodes))


def rows_with_archive(view: DisplayView, cold: DisplayView) -> dict[str, dict[str, Any]]:
    return build_rows(view) | archived_rows(cold)


def bodies_with_archive(
    view: DisplayView, repos: BodyRepos, ids: Sequence[str], cold: Callable[[], Stores]
) -> dict[str, dict[str, Any]]:
    """The bodies of `ids` from the hot estate, and those it does not hold from the archive. The
    archive is opened only when an id is missing from the hot one."""
    bodies = build_bodies(view, ids, repos=repos)
    if missing := [i for i in dict.fromkeys(ids) if i not in bodies]:
        cold_view, cold_repos = cold()
        bodies |= build_bodies(cold_view, missing, repos=cold_repos)
    return bodies
