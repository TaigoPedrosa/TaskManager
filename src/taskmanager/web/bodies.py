"""Bodies for many nodes built from one `DisplayView`, and the diff between two as protocol
items. `build_bodies` reads sections and jobs in one bulk query each; every other part -- the
node itself, its relations, its verifications, its conditions, its lease -- comes from the
snapshot's own bulk read, so the statement count never grows with how many ids are asked for."""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from taskmanager.core.enums import NodeKind, RelationType
from taskmanager.core.models import Condition, Job, Lease, Node, NodeSection
from taskmanager.core.status import DecisionStatus
from taskmanager.db.cache_repo import CacheRepository, _command_hash
from taskmanager.db.job_repo import _COLUMNS as _JOB_COLUMNS
from taskmanager.db.job_repo import JobRepository, _row_to_job
from taskmanager.db.node_repo import NodeRepository
from taskmanager.engine.assets import ASSET_NAME_RE
from taskmanager.engine.chains import base_chain, landing_chain, satisfied
from taskmanager.engine.snapshot import (
    DisplayView,
    chain_holder,
    phase_of,
    stored_status,
    waits_on,
)

# The body keys a session's `bodies` map replaces whole on change; `sections` diffs one key at a
# time instead, so it is handled separately in `body_items`.
_BODY_PARTS = (
    "node",
    "dependency_details",
    "dependent_details",
    "verifications",
    "conditions",
    "jobs",
    "lease",
)


@dataclass(frozen=True)
class BodyRepos:
    """What `build_bodies` needs beyond the `DisplayView` it is handed: the tables a snapshot's
    bulk read leaves out (sections, and every job regardless of state -- the snapshot keeps only
    the live ones), the condition cache and its TTL, and the attachments directory."""

    node_repo: NodeRepository
    job_repo: JobRepository
    cache: CacheRepository
    condition_ttl: int
    assets_dir: Path


def lifecycle_fields(n: Node, view: DisplayView) -> dict[str, Any]:
    # Read through stored_status so a node saved under an old name shows its new one.
    status = {"status": stored_status(n).value, "display": view.display(n)}
    if n.kind == NodeKind.DECISION:
        # A decision has no cycle: it is never claimed, reviewed, fixed or landed.
        return status
    return {
        **status,
        "phase": phase_of(n),
        "review": n.review,
        "fix": n.fix,
        "merge": n.merge.value,
        "outcome": n.outcome.value if n.outcome else None,
        "verdict": n.verdict,
        "fix_for": n.fix_for.value if n.fix_for else None,
        "claimed_from": n.claimed_from.value if n.claimed_from else None,
        "review_cycles": n.review_cycles,
        "merge_attempts": n.merge_attempts,
        "step_failures": n.step_failures,
        "branch": n.branch or f"tm/{n.id}",
        "requires": n.requires,
        "land_order": n.land_order,
        "landing_chain": landing_chain(view.snapshot, n.id),
        "base_chain": base_chain(view.snapshot, n.id),
    }


def _finished(view: DisplayView, source_id: str, target: Node) -> bool:
    if target.kind == NodeKind.DECISION:
        return stored_status(target) != DecisionStatus.OPEN
    nodes = view.snapshot.nodes
    return (
        source_id in nodes and target.id in nodes and satisfied(view.snapshot, source_id, target.id)
    )


def _relation_row(
    view: DisplayView, all_nodes: dict[str, Node], rel_id: str, finished: bool
) -> dict[str, Any]:
    rel = all_nodes.get(rel_id)
    return {
        "id": rel_id,
        "title": rel.title if rel else None,
        "kind": rel.kind.value if rel else None,
        "status": view.display(rel) if rel else None,
        # A missing node blocks.
        "finished": rel is not None and finished,
    }


def dependency_details(
    node_id: str, view: DisplayView, all_nodes: dict[str, Node], deps: list[str]
) -> list[dict[str, Any]]:
    rows = []
    for dep_id in deps:
        dep = all_nodes.get(dep_id)
        rows.append(
            _relation_row(
                view, all_nodes, dep_id, dep is not None and _finished(view, node_id, dep)
            )
        )
    # What a container waits on its children wait on too, and a migration writer waits
    # behind its chain's holder: both are named, marked as edges not its own.
    snap = view.snapshot
    node = all_nodes.get(node_id)
    if node is None or node_id not in snap.nodes:
        return rows
    work, decisions = waits_on(snap, node)
    owners = snap.edge_owners(node_id)
    for dep_id, owner in owners.items():
        if owner != node_id and dep_id in snap.nodes:
            row = _relation_row(
                view, all_nodes, dep_id, dep_id not in work and dep_id not in decisions
            )
            rows.append(row | {"inherited_from": owner})
    holder = chain_holder(snap, node)
    if holder is not None and holder not in owners:
        rows.append(
            _relation_row(view, all_nodes, holder, False) | {"migration_chain": node.target_repo}
        )
    return rows


def dependent_details(
    node_id: str, view: DisplayView, all_nodes: dict[str, Node], blocked_by: list[str]
) -> list[dict[str, Any]]:
    node = all_nodes.get(node_id)
    return [
        _relation_row(view, all_nodes, src_id, node is not None and _finished(view, src_id, node))
        for src_id in blocked_by
    ]


def lease_dict(lease: Lease | None) -> dict[str, Any] | None:
    if lease is None:
        return None
    return {
        "agent_id": lease.agent_id,
        "session_id": lease.session_id,
        "branch_name": lease.branch_name,
        "worktree_path": lease.worktree_path,
        "action": lease.action.value if lease.action else None,
        "ttl_seconds": lease.ttl_seconds,
    }


def attachment_size(assets_dir: Path, asset_name: str) -> int | None:
    # Same asset-name check and containment check as the asset-serving route: a frontmatter
    # attachment entry is untrusted, so a crafted or corrupted one reads as missing rather than
    # stat'd wherever it points.
    if not ASSET_NAME_RE.fullmatch(asset_name):
        return None
    resolved = assets_dir.resolve()
    candidate = (resolved / asset_name).resolve()
    if not candidate.is_relative_to(resolved):
        return None
    try:
        return candidate.stat().st_size
    except OSError:
        return None


def attachments_with_size(
    assets_dir: Path, attachments: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    return [
        {**a, "size_bytes": attachment_size(assets_dir, a.get("asset", ""))} for a in attachments
    ]


def frontmatter_with_attachment_sizes(
    assets_dir: Path, frontmatter: dict[str, Any]
) -> dict[str, Any]:
    attachments = frontmatter.get("attachments")
    if not attachments:
        return frontmatter
    return {**frontmatter, "attachments": attachments_with_size(assets_dir, attachments)}


def _bulk_sections(node_repo: NodeRepository, ids: Sequence[str]) -> dict[str, list[NodeSection]]:
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    with node_repo.db.get_state_connection() as conn:
        rows = conn.execute(
            f"""
            SELECT node_id, section_key, ordinal, header, content
            FROM node_sections
            WHERE node_id IN ({placeholders})
            ORDER BY node_id ASC, ordinal ASC
            """,
            tuple(ids),
        ).fetchall()
    sections: dict[str, list[NodeSection]] = {}
    for r in rows:
        sections.setdefault(r[0], []).append(
            NodeSection(node_id=r[0], section_key=r[1], ordinal=r[2], header=r[3], content=r[4])
        )
    return sections


def _bulk_jobs(job_repo: JobRepository, ids: Sequence[str]) -> dict[str, list[Job]]:
    # `GraphData.jobs` keeps only the live ones (what discovery needs); a body shows every job a
    # node ever ran, so this reads the table itself rather than the snapshot's bulk read.
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    with job_repo.db.get_state_connection() as conn:
        rows = conn.execute(
            f"SELECT {_JOB_COLUMNS} FROM jobs WHERE node_id IN ({placeholders}) ORDER BY rowid ASC",
            tuple(ids),
        ).fetchall()
    jobs: dict[str, list[Job]] = {}
    for r in rows:
        job = _row_to_job(r)
        jobs.setdefault(job.node_id, []).append(job)
    return jobs


def _condition_result(
    cached: dict[tuple[str, int], tuple[str, int]], node_id: str, condition: Condition
) -> int | None:
    entry = cached.get((node_id, condition.idx))
    if entry is None:
        return None
    command_hash, exit_code = entry
    return exit_code if command_hash == _command_hash(condition.command) else None


def build_bodies(
    view: DisplayView, ids: Sequence[str], *, repos: BodyRepos
) -> dict[str, dict[str, Any]]:
    data = view.snapshot.graph_data()
    wanted = list(dict.fromkeys(i for i in ids if i in data.nodes))
    if not wanted:
        return {}
    deps_by_source: dict[str, list[str]] = {}
    blocked_by_target: dict[str, list[str]] = {}
    for source, target in data.relations[RelationType.DEPENDS_ON]:
        deps_by_source.setdefault(source, []).append(target)
        blocked_by_target.setdefault(target, []).append(source)
    sections = _bulk_sections(repos.node_repo, wanted)
    jobs = _bulk_jobs(repos.job_repo, wanted)
    cached_conditions = repos.cache.all_conditions(repos.condition_ttl)

    bodies: dict[str, dict[str, Any]] = {}
    for node_id in wanted:
        node = data.nodes[node_id]
        bodies[node_id] = {
            "node": {
                "id": node.id,
                "kind": node.kind.value,
                "title": node.title,
                **lifecycle_fields(node, view),
                "priority": node.priority,
                "ordinal": node.ordinal,
                "target_repo": node.target_repo,
                "acceptable_models": node.acceptable_models,
                "frontmatter": frontmatter_with_attachment_sizes(
                    repos.assets_dir, node.frontmatter
                ),
            },
            "dependency_details": dependency_details(
                node_id, view, data.nodes, deps_by_source.get(node_id, [])
            ),
            "dependent_details": dependent_details(
                node_id, view, data.nodes, blocked_by_target.get(node_id, [])
            ),
            "sections": [
                {
                    "key": s.section_key,
                    "header": s.header,
                    "content": s.content,
                    "ordinal": s.ordinal,
                }
                for s in sections.get(node_id, [])
            ],
            "verifications": [
                {
                    "id": v.id,
                    "verification_type": v.verification_type.value,
                    "target_path": v.target_path,
                    "expected_pattern": v.expected_pattern,
                }
                for v in data.verifications.get(node_id, [])
            ],
            "conditions": [
                {
                    "idx": c.idx,
                    "needs": c.needs,
                    "command": c.command,
                    "stage": c.stage.value,
                    "last_result": _condition_result(cached_conditions, node_id, c),
                }
                for c in data.conditions.get(node_id, [])
            ],
            "jobs": [j.model_dump(mode="json") for j in jobs.get(node_id, [])],
            "lease": lease_dict(data.leases.get(node_id)),
        }
    return bodies


def body_items(
    node_id: str, old: dict[str, Any] | None, new: dict[str, Any]
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    old_sections = {s["key"]: s for s in old["sections"]} if old is not None else {}
    new_sections = {s["key"]: s for s in new["sections"]}
    for key in sorted(old_sections.keys() | new_sections.keys()):
        before, after = old_sections.get(key), new_sections.get(key)
        if before == after:
            continue
        section = (
            {"header": after["header"], "content": after["content"], "ordinal": after["ordinal"]}
            if after is not None
            else None
        )
        items.append({"op": "section", "id": node_id, "key": key, "section": section})
    for part in _BODY_PARTS:
        if old is not None and old.get(part) == new.get(part):
            continue
        items.append({"op": "body", "id": node_id, "part": part, "value": new.get(part)})
    return items
