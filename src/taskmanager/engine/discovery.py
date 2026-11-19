"""One dispatch wave's batch: every claimable node with the step it would take next.

Discovery reads and never claims; each chosen node is claimed by `tm task start`, which runs the
same checks again inside its transaction. The wave choice is `engine.selection`'s: pure over the
snapshot this reads once, so a claim and a wave never disagree. Discovery passes its own `Claims`
methods in for a candidate's next action and blocked reason, so a condition still runs its
command exactly as the real claim would.
"""

import json
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from typing import Final

from taskmanager.db.utils import lease_alive
from taskmanager.engine import selection
from taskmanager.engine.claims import Claims
from taskmanager.engine.stepgraph import Snapshot

# Long enough for a wave to claim what it chose; a run that dies frees its ids by this.
RESERVATION_SECONDS: Final = 600


def _model(claims: Claims, snap: Snapshot, node_id: str) -> str | None:
    """The model a reserved node's claim would name, read from `snap` since the node may sit
    outside this run's specs; None once the node has been archived."""
    node = snap.graph_data().nodes.get(node_id)
    return None if node is None else claims.next_step(node, snap)[1]


def djb2(payload: str) -> int:
    """The `__CHECK h=` checksum `tm wave discover` prints after its payload: a caller that
    echoes the payload back through a model can reject a transcription that is not byte-exact."""
    checksum = 5381
    for byte in payload.encode("utf-8"):
        checksum = (checksum * 33 + byte) & 0xFFFFFFFF
    return checksum


def discover(
    claims: Claims,
    specs: list[str] | None,
    session: str,
    slots: int,
    max_strong: int,
    exclude: list[str] | None = None,
    hold_merge: list[str] | None = None,
    reserve: str | None = None,
) -> tuple[str, int]:
    claims.sweep()
    snap = claims.snapshots.build()
    # Before any transaction: the condition runner refuses to run inside one.
    found, held = selection.candidates(
        snap,
        specs,
        repo_order=claims.config.repo_order,
        models=claims.config.models,
        next_step=claims.next_step,
        blocked_reason=claims.blocked_reason,
        gated=selection.gated_repos(claims.config),
    )
    strong = claims.config.models.strong
    with claims.runtime.db.spec_transaction() if reserve else nullcontext():
        if reserve:
            # A retried discovery of the same run replaces what its lost attempt reserved.
            claims.runtime.unreserve_run(reserve)
        now = datetime.now(tz=UTC)
        leases = claims.runtime.list_leases()
        # A lease parked for an agent (no TTL) is a stopped job nobody runs, so it fills no slot.
        mine = [
            lease
            for lease in leases
            if lease.session_id == session and lease.ttl_seconds is not None
        ]
        # `found` predates this transaction, so a claim any session committed since may hold a
        # candidate's node. A hand-over's own lease stays parked until its claim takes it.
        # Each node a live lease holds, and whether that lease is parked.
        leased_now = {
            lease.task_id: lease.ttl_seconds is None
            for lease in leases
            if lease_alive(lease.ttl_seconds, lease.last_heartbeat, now)
        }
        found = [
            c
            for c in found
            if c.node.id not in leased_now or (c.job is not None and leased_now[c.node.id])
        ]
        reserved = claims.runtime.live_reservations(now)
        leased = {lease.task_id for lease in mine}
        pending = [
            _model(claims, snap, node_id)
            for node_id, holder in reserved.items()
            if holder == session and node_id not in leased
        ]
        free = slots - len(mine) - len(pending)
        strong_free = (
            max_strong
            - sum(lease.model in strong for lease in mine)
            - sum(model in strong for model in pending)
        )
        # A reserved id takes its files as an excluded one does, but no "excluded by args" line.
        result = selection.select(
            [c for c in found if c.node.id not in reserved],
            snap,
            free,
            strong_free,
            [*(exclude or []), *reserved],
            hold_merge or [],
            strong,
        )
        if reserve:
            claims.runtime.reserve(
                [str(entry["id"]) for entry in result.chosen],
                session,
                reserve,
                now + timedelta(seconds=RESERVATION_SECONDS),
            )
    payload = json.dumps(
        {
            "chosen": result.chosen,
            "held": [*held, *result.held],
            "waiting_for_slot": result.waiting_for_slot,
            "mine": len(mine),
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return payload, len(result.chosen)
