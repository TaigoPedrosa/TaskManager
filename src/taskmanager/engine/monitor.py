import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Protocol

from taskmanager.core.enums import NodeKind
from taskmanager.core.models import LedgerEvent
from taskmanager.core.status import EXITS, ConditionStage, Status
from taskmanager.db.cache_repo import _command_hash
from taskmanager.engine.claims import Claims
from taskmanager.engine.conditions import ConditionRunner
from taskmanager.engine.decisions import DecisionData
from taskmanager.engine.selection import _in_scope
from taskmanager.engine.snapshot import SnapshotBuilder
from taskmanager.engine.stepgraph import Snapshot

MONITOR_ACTOR: Final = "monitor"
# Rows are never printed for these: they are what an agent writes while it works, not what
# happened to the estate.
DENIED: Final = frozenset(
    {
        "task_heartbeat",
        "section_set",
        "section_remove",
        "note add",
        "check add",
        "verification_add",
        "verification_run",
        "verify remove",
        "finding add",
        "finding closed",
        "finding disputed",
        "finding open",
        "attach",
        "detach",
        "attachment_check",
        "task start",
        "task_start",
        "task start undone",
        "job handover",
    }
)
# A writer that stamped its row late (a restore, a clock behind) must not replay as news.
STALE_AFTER: Final = timedelta(seconds=60)
LEASE_OVERDUE_AFTER: Final = timedelta(seconds=300)
_FREE_TEXT: Final = ("question", "reason", "needs")
_BARE = re.compile(r"^[A-Za-z0-9._/:@+-]+$")
_POLL_SECONDS: Final = 1.0


class Clock(Protocol):
    def now(self) -> datetime: ...

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(tz=UTC)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


@dataclass(frozen=True)
class Batch:
    lines: list[str]
    cursor: int


def ledger_changes(claims: Claims) -> Callable[[], int]:
    """SQLite's count of commits other connections made to the ledger."""
    db = claims.ops.ledger_repo.db

    def version() -> int:
        with db.get_ledger_connection() as conn:
            return int(conn.execute("PRAGMA data_version").fetchone()[0])

    return version


class Monitor:
    def __init__(
        self,
        claims: Claims,
        clock: Clock,
        changes: Callable[[], int],
        announced: frozenset[str] = frozenset(),
    ) -> None:
        self.announced = announced
        self._claims = claims
        self._clock = clock
        self._changes = changes
        c = claims.conditions
        self._runner = ConditionRunner(
            c.root, c.node_repo, c.cache_repo, c.ledger_repo, c.ttl, c.timeout, MONITOR_ACTOR
        )

    def batch(self, after: int, specs: list[str] | None, session: str | None = None) -> Batch:
        snap = self._claims.snapshots.build()
        self._recheck_conditions(snap, specs)
        ledger = self._claims.ops.ledger_repo
        events = ledger.after(after)
        newest = ledger.newest_at(after)
        lines: list[str] = []
        watched: set[str] | None = None
        for event in events:
            if event.command in DENIED or event.actor_id == session:
                continue
            if newest is not None and newest - event.timestamp > STALE_AFTER:
                continue
            if specs is not None and not self._in_scope(snap, event, specs, session):
                if "to" not in event.payload or event.target_id not in snap.nodes:
                    continue
                if watched is None:
                    watched = _watched(snap, specs)
                if event.target_id not in watched:
                    continue
            lines.append(_line(event))
        due = self._due(snap, specs)
        # A due line is a state, not a row: it prints once and again only after it cleared.
        lines += [line for line in due if line not in self.announced]
        self.announced = frozenset(due)
        cursor = events[-1].id if events and events[-1].id is not None else after
        return Batch(lines, cursor)

    def next_wake(self, specs: list[str] | None) -> datetime | None:
        now = self._clock.now()
        snap = self._claims.snapshots.build()
        data = snap.graph_data()
        times = [
            lease.last_heartbeat + timedelta(seconds=lease.ttl_seconds) + LEASE_OVERDUE_AFTER
            for node_id, lease in data.leases.items()
            if lease.ttl_seconds is not None and self._scoped(snap, node_id, specs)
        ]
        times += self._claims.red_target_deadlines()
        ttl = timedelta(seconds=self._runner.ttl)
        times += [at + ttl for _, at in self._held_results(snap, specs).values()]
        return min((t for t in times if t > now), default=None)

    def wait(
        self, after: int, specs: list[str] | None, session: str | None, timeout: float
    ) -> Batch:
        """The first batch with something to print, or the cursor at the end of `timeout`. The
        change counter is read before the rows, so a write landing in between is seen by the next
        read rather than lost."""
        deadline = self._clock.now() + timedelta(seconds=timeout)
        while True:
            version = self._changes()
            found = self.batch(after, specs, session)
            after = found.cursor
            if found.lines or self._clock.now() >= deadline:
                return Batch(found.lines, after)
            wake = min(deadline, self.next_wake(specs) or deadline)
            while self._changes() == version and (left := wake - self._clock.now()) > timedelta(0):
                self._clock.sleep(min(_POLL_SECONDS, left.total_seconds()))

    def _scoped(self, snap: Snapshot, node_id: str, specs: list[str] | None) -> bool:
        node = snap.graph_data().nodes.get(node_id)
        return node is not None and _in_scope(snap, node, specs)

    def _in_scope(
        self, snap: Snapshot, event: LedgerEvent, specs: list[str], session: str | None
    ) -> bool:
        if event.command in ("config set", "config unset"):
            return True
        nodes = snap.graph_data().nodes
        found = nodes.get(event.target_id or "")
        if found is None:
            return False
        if found.kind != NodeKind.DECISION:
            return _in_scope(snap, found, specs)
        decision = DecisionData.model_validate(found.frontmatter.get("decision", {}))
        tied = [d for d, dep in snap.edges if dep == found.id]
        tied += [n for n in (decision.raised_by, decision.subject) if n is not None]
        tied = [n for n in tied if n in nodes]
        if not tied:
            return decision.raised_by is not None and decision.raised_by == session
        return any(_in_scope(snap, nodes[n], specs) for n in tied)

    def _held_results(
        self, snap: Snapshot, specs: list[str] | None
    ) -> dict[tuple[str, int], tuple[ConditionStage, datetime]]:
        """The in-scope conditions of unfinished nodes that have a result for their current
        command, each with its stage and when that result was recorded."""
        data = snap.graph_data()
        recorded = self._runner.cache_repo.condition_checked_at()
        held: dict[tuple[str, int], tuple[ConditionStage, datetime]] = {}
        for node_id, conditions in data.conditions.items():
            node = data.nodes[node_id]
            if node.status in EXITS or node.status == Status.COMPLETED:
                continue
            if not _in_scope(snap, node, specs):
                continue
            for c in conditions:
                found = recorded.get((node_id, c.idx))
                if found is not None and found[0] == _command_hash(c.command):
                    held[(node_id, c.idx)] = (c.stage, found[1])
        return held

    def _recheck_conditions(self, snap: Snapshot, specs: list[str] | None) -> None:
        expired = self._clock.now() - timedelta(seconds=self._runner.ttl)
        stale = {
            (node_id, stage)
            for (node_id, _), (stage, at) in self._held_results(snap, specs).items()
            if at <= expired
        }
        for node_id, stage in sorted(stale):
            self._runner.unmet(node_id, stage)

    def _due(self, snap: Snapshot, specs: list[str] | None) -> list[str]:
        now = self._clock.now()
        lines = [
            f"lease {node_id} {lease.action.value if lease.action else 'none'} overdue"
            for node_id, lease in snap.graph_data().leases.items()
            if lease.ttl_seconds is not None
            and self._scoped(snap, node_id, specs)
            and now - lease.last_heartbeat - timedelta(seconds=lease.ttl_seconds)
            >= LEASE_OVERDUE_AFTER
        ]
        lines += [f"red target {t.repo}/{t.target} due" for t in self._claims.red_targets_due(now)]
        return lines


def _watched(snap: Snapshot, specs: list[str]) -> set[str]:
    """The nodes outside `specs` that a node inside them waits on: by an edge, a declared file
    both touch, or the migration chain of their repository."""
    data = snap.graph_data()
    scoped = [
        n.id
        for n in data.nodes.values()
        if n.kind != NodeKind.DECISION and _in_scope(snap, n, specs)
    ]
    watched = {dep for n in scoped for dep in snap.inherited_edges(n)}
    chain_repos = {snap.nodes[n].repo for n in scoped if snap.nodes[n].writes_migration}
    mine = {key for n in scoped for key in SnapshotBuilder.lock_set(n, snap)}
    for other in snap.nodes.values():
        if other.kind == NodeKind.DECISION:
            continue
        if other.writes_migration and other.repo in chain_repos:
            watched.add(other.id)
        if mine.intersection(SnapshotBuilder.lock_set(other.id, snap)):
            watched.add(other.id)
    return watched


def _token(value: object) -> str:
    if isinstance(value, str) and _BARE.match(value):
        return value
    return json.dumps(value, separators=(",", ":"))


def _line(event: LedgerEvent) -> str:
    free = next((k for k in _FREE_TEXT if k in event.payload), None)
    fields = [f"{k}={_token(v)}" for k, v in event.payload.items() if k != free]
    parts = [event.command, event.target_id or "-", *fields]
    if free is not None:
        parts.append(json.dumps(str(event.payload[free])))
    return " ".join(parts)
