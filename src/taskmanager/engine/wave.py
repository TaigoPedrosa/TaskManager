import json
import logging
import sqlite3

from taskmanager.core.enums import NodeKind, NodeStatus
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.engine.heuristics import RecommendationEngine
from taskmanager.engine.operations import Operations

# A task in one of these statuses is claimable by a fresh wave: past NOT_STARTED review dance
# with nobody currently working it (READY tasks are found separately, through get_next_tasks).
ENTRY_STATUSES = (NodeStatus.WAITING_REVIEW, NodeStatus.WAITING_FIXES, NodeStatus.WAITING_MERGE)

# A task in one of these statuses has not yet landed on origin/main, so a migration it declares
# still holds its repo's one-writer chain lock (see `_writes_migration`).
UNMERGED_STATUSES = (
    NodeStatus.IMPLEMENTING,
    NodeStatus.WAITING_REVIEW,
    NodeStatus.REVIEWING,
    NodeStatus.WAITING_FIXES,
    NodeStatus.FIXING,
    NodeStatus.WAITING_MERGE,
    NodeStatus.MERGING,
)

_TIER = {"haiku": 1, "sonnet": 2, "opus": 3, "fable": 3}
STRONG = ("opus", "fable")


def djb2(payload: str) -> int:
    checksum = 5381
    for byte in payload.encode("utf-8"):
        checksum = (checksum * 33 + byte) & 0xFFFFFFFF
    return checksum


def _family(model_ids: list[str]) -> str | None:
    found = [name for model_id in model_ids for name in _TIER if name in model_id]
    return min(found, key=_TIER.__getitem__) if found else None


def _writes_migration(files: list[str]) -> bool:
    return any("migrations/versions/" in f for f in files)


def _awaiting_decisions(node_repo: NodeRepository, task_id: str) -> list[str]:
    awaiting = []
    for dep_id, _gate in node_repo.get_dependency_edges(task_id):
        dep = node_repo.get_node(dep_id)
        if (
            dep is not None
            and dep.kind == NodeKind.DECISION
            and dep.status not in (NodeStatus.COMPLETED, NodeStatus.ABANDONED)
        ):
            awaiting.append(dep_id)
    return awaiting


def discover_batch(
    node_repo: NodeRepository,
    runtime_repo: RuntimeRepository,
    operations: Operations,
    heuristics: RecommendationEngine,
    specs: list[str],
    session: str,
    slots: int,
    max_strong: int,
    exclude: list[str] | None = None,
    release: list[str] | None = None,
) -> tuple[str, int]:
    """The next tm-wave batch: the JSON payload string and its `chosen` count.

    Ports `.claude/bin/tm-wave-discover.py` (SocialSrc) in-process, so a dispatcher no longer
    shells out to a project-local script to run `tm` against its own database. Every read goes
    through the same repositories and engines the rest of the CLI uses -- `declared_files`
    included, which is the union of a task's frontmatter and its path-bearing verifications,
    a superset of the ported script's frontmatter-only reading and the one every other file-lock
    and readiness check in this codebase already uses.
    """
    exclude = exclude or []
    release = release or []

    try:
        operations.sweep_leases()
    except (sqlite3.Error, OSError) as exc:
        # Best-effort, same as the ported script's `subprocess.run(..., capture_output=True)`
        # ignoring `tm run sweep`'s exit code: a discovery pass proceeds on stale leases rather
        # than failing the whole wave over a sweep that couldn't write.
        logging.getLogger(__name__).debug("Failed to sweep expired leases: %s", exc)

    with runtime_repo.db.get_runtime_connection() as conn:
        lease_rows = conn.execute("SELECT task_id, agent_id, session_id FROM leases").fetchall()
    leases = [{"task_id": row[0], "agent_id": row[1], "session_id": row[2]} for row in lease_rows]
    leased = {lease["task_id"] for lease in leases}
    mine = [lease for lease in leases if lease["session_id"] == session]
    free = slots - len(mine)
    strong_free = max_strong - sum(
        any(f"-{s}-" in lease["agent_id"] for s in STRONG) for lease in mine
    )

    chain_held: dict[str, str] = {}
    for status in UNMERGED_STATUSES:
        for task in node_repo.list_nodes(kind=NodeKind.TASK, status=status):
            if _writes_migration(node_repo.declared_files(task.id)):
                chain_held.setdefault(task.target_repo or "", task.id)

    candidates: dict[str, str] = {}
    for spec in specs:
        for status in ENTRY_STATUSES:
            for task in node_repo.list_nodes(kind=NodeKind.TASK, status=status):
                if node_repo.get_ancestor_of_kind(task.id, NodeKind.SPEC) == spec:
                    candidates.setdefault(task.id, status.value)
        for scored in heuristics.get_next_tasks(spec_id=spec, limit=500):
            candidates.setdefault(scored.task_id, "READY")

    chosen: list[dict[str, object]] = []
    held: list[str] = []
    taken_files: set[str] = set()
    for task_id, status in candidates.items():
        if task_id in leased:
            continue
        if task_id in exclude:
            held.append(f"{task_id}: excluded by args")
            continue
        node = node_repo.get_node(task_id)
        if node is None:
            continue
        repo = node.target_repo or ""
        files = node_repo.declared_files(task_id)
        model = _family(node.acceptable_models) or "sonnet"

        why: list[str] = []
        awaiting = _awaiting_decisions(node_repo, task_id)
        if awaiting:
            why.append(f"awaiting owner decision {', '.join(awaiting)}")
        sections = {section.section_key for section in node_repo.get_all_sections(task_id)}
        if "hold" in sections and task_id not in release:
            why.append("hold section: read tm section get <id>:hold, then pass --release")
        if status == "READY" and _writes_migration(files) and repo in chain_held:
            why.append(f"{repo} migration chain held by {chain_held[repo]}")
        if taken_files.intersection(files):
            why.append("declared_files overlap a task chosen this wave")
        if len(chosen) >= free:
            why.append("no free slot")
        if model in STRONG and strong_free <= 0:
            why.append(f"no free {'/'.join(STRONG)} slot")
        if why:
            held.append(f"{task_id}: {'; '.join(why)}")
            continue

        chosen.append(
            {
                "id": task_id,
                "status": status,
                "repo": repo,
                "model": model,
                "review": _family(node.frontmatter.get("review_models") or []) or "sonnet",
                "migration": _writes_migration(files),
                "blockers": node.frontmatter.get("external_blockers") or [],
            }
        )
        taken_files.update(files)
        if model in STRONG:
            strong_free -= 1
        if status == "READY" and _writes_migration(files):
            chain_held[repo] = task_id

    waiting = sum(entry.endswith(": no free slot") for entry in held)
    held = [entry for entry in held if not entry.endswith(": no free slot")]
    payload = json.dumps(
        {"chosen": chosen, "held": held, "waiting_for_slot": waiting, "mine": len(mine)},
        separators=(",", ":"),
        sort_keys=True,
    )
    return payload, len(chosen)
