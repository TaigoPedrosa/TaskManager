"""Which completed specs the web leaves out of its counts, read from the ledger and the clock on
every snapshot rather than stored, so a spec crosses into the archive without any write."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from taskmanager.core.enums import NodeKind
from taskmanager.core.status import Status
from taskmanager.db.ledger_repo import LedgerRepository
from taskmanager.engine.stepgraph import Snapshot


@dataclass(frozen=True)
class Archive:
    specs: frozenset[str]
    # The archived specs and every node under them.
    nodes: frozenset[str]
    # When the next completed spec still in view crosses into the archive.
    next_boundary: datetime | None


def archived_specs(
    snapshot: Snapshot, ledger: LedgerRepository, now: datetime, days: int
) -> Archive:
    if days <= 0:
        return Archive(frozenset(), frozenset(), None)
    completed = [
        node.id
        for node in snapshot.graph_data().nodes.values()
        if node.kind == NodeKind.SPEC and node.status == Status.COMPLETED
    ]
    keep_for = timedelta(days=days)
    specs: set[str] = set()
    boundaries: list[datetime] = []
    for spec_id, completed_at in ledger.completed_at(completed).items():
        boundary = completed_at + keep_for
        if boundary <= now:
            specs.add(spec_id)
        else:
            boundaries.append(boundary)
    nodes = {node_id for spec_id in specs for node_id in (spec_id, *snapshot.descendants(spec_id))}
    return Archive(frozenset(specs), frozenset(nodes), min(boundaries, default=None))
