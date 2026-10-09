"""One dispatch wave's batch: every claimable node with the step it would take next.

Discovery reads and never claims; each chosen node is claimed by `tm task start`, which runs the
same checks again inside its transaction. The wave choice is `engine.selection`'s: pure over the
snapshot this reads once, so a claim and a wave never disagree. Discovery passes its own `Claims`
methods in for a candidate's next action and blocked reason, so a condition still runs its
command exactly as the real claim would.
"""

import json

from taskmanager.engine import selection
from taskmanager.engine.claims import Claims


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
) -> tuple[str, int]:
    claims.sweep()
    snap = claims.snapshots.build()
    # A lease parked for an agent (no TTL) is a stopped job nobody runs, so it fills no slot.
    mine = [
        lease
        for lease in claims.runtime.list_leases()
        if lease.session_id == session and lease.ttl_seconds is not None
    ]
    free = slots - len(mine)
    strong = claims.config.models.strong
    strong_free = max_strong - sum(lease.model in strong for lease in mine)
    found, held = selection.candidates(
        snap,
        specs,
        repo_order=claims.config.repo_order,
        models=claims.config.models,
        next_step=claims.next_step,
        blocked_reason=claims.blocked_reason,
        gated=selection.gated_repos(claims.config),
    )
    result = selection.select(
        found, snap, free, strong_free, exclude or [], hold_merge or [], strong
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
