from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from lifecycle_estate import add, make_estate

from taskmanager.core.enums import NodeKind, RelationType
from taskmanager.core.models import Condition, Job, Lease, LedgerEvent, NodeRelation
from taskmanager.core.status import DecisionStatus, JobKind, JobState, Status
from taskmanager.engine.claims import Claims
from taskmanager.engine.config import ProjectConfig
from taskmanager.engine.gates import RED_TARGET
from taskmanager.engine.monitor import Monitor

NOW = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self, at: datetime = NOW) -> None:
        self.at = at
        self.slept: list[float] = []
        self.on_sleep: Callable[[], None] = lambda: None

    def now(self) -> datetime:
        return self.at

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.at += timedelta(seconds=seconds)
        self.on_sleep()


class Kit:
    def __init__(self, claims: Claims) -> None:
        self.claims = claims
        self.clock = FakeClock()
        self.version = 0
        self.reads: list[str] = []
        self.monitor = Monitor(claims, self.clock, self.changes)

    def changes(self) -> int:
        self.reads.append("version")
        return self.version

    def row(
        self,
        command: str,
        target: str | None,
        payload: dict[str, object] | None = None,
        actor: str = "cli",
        at: datetime = NOW,
    ) -> int:
        event = LedgerEvent(
            actor_id=actor, command=command, target_id=target, payload=payload or {}, timestamp=at
        )
        self.claims.ops.ledger_repo.append(event)
        assert event.id is not None
        return event.id

    def lines(
        self, after: int = 0, specs: list[str] | None = None, session: str | None = None
    ) -> list[str]:
        return self.monitor.batch(after, specs, session).lines


@pytest.fixture
def kit(tmp_path: Path) -> Kit:
    return Kit(make_estate(tmp_path))


def two_specs(claims: Claims) -> None:
    add(claims, "S1", NodeKind.SPEC)
    add(claims, "S2", NodeKind.SPEC)
    add(claims, "T1", parent="S1")
    add(claims, "T2", parent="S2")


def test_batch_reads_rows_after_the_cursor_and_returns_the_highest_id_read(kit: Kit) -> None:
    add(kit.claims, "T1")
    first = kit.row("task update", "T1", {"to": "READY"})
    second = kit.row("job sync", "T1", {"step": 2})
    third = kit.row("job land", "T1", {"to": "LANDED"})

    batch = kit.monitor.batch(first, None, None)

    assert batch.lines == ["job sync T1 step=2", "job land T1 to=LANDED"]
    assert batch.cursor == third > second


def test_batch_with_nothing_new_keeps_the_cursor(kit: Kit) -> None:
    add(kit.claims, "T1")
    last = kit.row("job land", "T1", {"to": "LANDED"})

    batch = kit.monitor.batch(last, None, None)

    assert (batch.lines, batch.cursor) == ([], last)


def test_a_denied_row_prints_nothing_and_the_cursor_moves_past_it(kit: Kit) -> None:
    add(kit.claims, "T1")
    denied = [
        kit.row(command, "T1")
        for command in (
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
        )
    ]

    batch = kit.monitor.batch(0, None, None)

    assert (batch.lines, batch.cursor) == ([], denied[-1])


def test_a_row_the_session_wrote_prints_nothing_and_another_actors_row_prints(kit: Kit) -> None:
    add(kit.claims, "T1")
    kit.row("job land", "T1", {"to": "LANDED"}, actor="wave-1")
    last = kit.row("job sync", "T1", {"step": 2}, actor="wave-2")

    batch = kit.monitor.batch(0, None, "wave-1")

    assert batch.lines == ["job sync T1 step=2"]
    assert batch.cursor == last


def test_a_row_stamped_more_than_sixty_seconds_before_the_cursor_s_newest_prints_nothing(
    kit: Kit,
) -> None:
    add(kit.claims, "T1")
    cursor = kit.row("job land", "T1", {"to": "LANDED"}, at=NOW)
    kit.row("job sync", "T1", {"step": 1}, at=NOW - timedelta(seconds=61))
    kit.row("job sync", "T1", {"step": 2}, at=NOW - timedelta(seconds=60))
    last = kit.row("job sync", "T1", {"step": 3}, at=NOW + timedelta(seconds=5))

    batch = kit.monitor.batch(cursor, None, None)

    assert batch.lines == ["job sync T1 step=2", "job sync T1 step=3"]
    assert batch.cursor == last


def test_free_text_is_last_and_one_json_quoted_string(kit: Kit) -> None:
    add(kit.claims, "T1")
    kit.row("decision_add", "T1", {"question": 'Which one?\nthe "second"', "n": 3})

    assert kit.lines() == ['decision_add T1 n=3 "Which one?\\nthe \\"second\\""']


@pytest.mark.parametrize(
    ("command", "payload", "line"),
    [
        (
            "job parked",
            {"job": "J1", "reason": "needs a merge"},
            'job parked T1 job=J1 "needs a merge"',
        ),
        (
            "condition met",
            {"idx": 1, "needs": "ci is green"},
            'condition met T1 idx=1 "ci is green"',
        ),
    ],
)
def test_a_row_names_its_command_its_node_and_its_fields(
    kit: Kit, command: str, payload: dict[str, object], line: str
) -> None:
    add(kit.claims, "T1")
    kit.row(command, "T1", payload)

    assert kit.lines() == [line]


def test_a_scoped_batch_prints_in_scope_nodes_only(kit: Kit) -> None:
    two_specs(kit.claims)
    kit.row("task update", "T1", {"from": "READY"})
    kit.row("task update", "T2", {"from": "READY"})
    kit.row("task update", "S1", {"from": "READY"})

    assert kit.lines(specs=["S1"]) == ["task update T1 from=READY", "task update S1 from=READY"]


def test_an_out_of_scope_move_prints_when_an_in_scope_node_waits_on_it_by_an_edge(kit: Kit) -> None:
    two_specs(kit.claims)
    add(kit.claims, "T3", parent="S2")
    kit.claims.nodes.add_relation(
        NodeRelation(source_id="T1", target_id="T2", relation_type=RelationType.DEPENDS_ON)
    )
    kit.row("job land", "T2", {"to": "LANDED"})
    kit.row("job land", "T3", {"to": "LANDED"})
    kit.row("task update", "T2", {"note": "no move"})

    assert kit.lines(specs=["S1"]) == ["job land T2 to=LANDED"]


def test_an_out_of_scope_move_prints_when_the_edge_is_on_an_in_scope_container(kit: Kit) -> None:
    two_specs(kit.claims)
    kit.claims.nodes.add_relation(
        NodeRelation(source_id="S1", target_id="T2", relation_type=RelationType.DEPENDS_ON)
    )
    kit.row("job land", "T2", {"to": "LANDED"})

    assert kit.lines(specs=["S1"]) == ["job land T2 to=LANDED"]


def test_an_out_of_scope_move_prints_when_it_declares_a_file_an_in_scope_node_declares(
    kit: Kit,
) -> None:
    add(kit.claims, "S1", NodeKind.SPEC)
    add(kit.claims, "S2", NodeKind.SPEC)
    add(kit.claims, "T1", parent="S1", files=["api/a.py", "api/b.py"])
    add(kit.claims, "T2", parent="S2", files=["api/b.py"])
    add(kit.claims, "T3", parent="S2", files=["api/c.py"])
    kit.row("job land", "T2", {"to": "LANDED"})
    kit.row("job land", "T3", {"to": "LANDED"})

    assert kit.lines(specs=["S1"]) == ["job land T2 to=LANDED"]


def test_an_out_of_scope_move_prints_when_it_writes_the_migration_chain_an_in_scope_node_writes(
    kit: Kit,
) -> None:
    add(kit.claims, "S1", NodeKind.SPEC)
    add(kit.claims, "S2", NodeKind.SPEC)
    add(kit.claims, "T1", parent="S1", files=["api/db/migrations/001.sql"])
    add(kit.claims, "T2", parent="S2", files=["api/db/migrations/002.sql"])
    add(kit.claims, "T3", parent="S2", files=["api/c.py"])
    kit.row("job land", "T2", {"to": "LANDED"})
    kit.row("job land", "T3", {"to": "LANDED"})

    assert kit.lines(specs=["S1"]) == ["job land T2 to=LANDED"]


def decision_lines(kit: Kit, specs: list[str] | None, session: str | None = None) -> list[str]:
    return [line.split(" ", 2)[2] for line in kit.lines(specs=specs, session=session)]


def test_a_decision_prints_when_a_node_it_blocks_is_in_scope(kit: Kit) -> None:
    two_specs(kit.claims)
    kit.claims.ops.add_decision("mine?", blocks=["T1"])
    kit.claims.ops.add_decision("other?", blocks=["T2"])

    assert decision_lines(kit, ["S1"]) == ['"mine?"']


def test_a_decision_prints_when_its_raiser_or_its_subject_is_in_scope(kit: Kit) -> None:
    two_specs(kit.claims)
    kit.claims.ops.add_decision("raised?", raised_by="T1")
    kit.claims.ops.add_decision("about?", subject="T1")
    kit.claims.ops.add_decision("far?", raised_by="T2", subject="T2")

    assert decision_lines(kit, ["S1"]) == ['"raised?"', '"about?"']


def test_a_decision_tied_to_no_node_prints_only_unscoped(kit: Kit) -> None:
    two_specs(kit.claims)
    kit.claims.ops.add_decision("loose?")

    assert decision_lines(kit, ["S1"]) == []
    assert decision_lines(kit, None) == ['"loose?"']


def test_a_decision_tied_to_no_node_prints_to_the_session_that_raised_it(kit: Kit) -> None:
    two_specs(kit.claims)
    decision = kit.claims.ops.add_decision("loose?")
    node = kit.claims.nodes.get_node(decision)
    assert node is not None
    data = {**node.frontmatter["decision"], "raised_by": "wave-1"}
    kit.claims.nodes.save_node(node.model_copy(update={"frontmatter": {"decision": data}}))

    assert decision_lines(kit, ["S1"], session="wave-1") == ['"loose?"']
    assert decision_lines(kit, ["S1"], session="wave-2") == []


def test_a_config_change_prints_in_every_scope(kit: Kit) -> None:
    two_specs(kit.claims)
    kit.row("config set", "config", {"key": "max_fix_rounds.task"})
    kit.row("config unset", "config", {"key": "verify_timeout"})

    expected = [
        "config set config key=max_fix_rounds.task",
        "config unset config key=verify_timeout",
    ]
    assert kit.lines(specs=["S1"]) == expected
    assert kit.lines(specs=["S9"]) == expected
    assert kit.lines(specs=None) == expected


def lease(claims: Claims, node_id: str, heartbeat: datetime, ttl: int | None = 600) -> None:
    claims.runtime.acquire_lease(
        Lease(
            task_id=node_id,
            agent_id="a",
            session_id="s",
            branch_name=f"tm/{node_id}",
            acquired_at=heartbeat,
            last_heartbeat=heartbeat,
            ttl_seconds=ttl,
        ),
        [],
    )


def test_a_ttl_lease_prints_overdue_once_it_is_300_seconds_past_its_expiry(kit: Kit) -> None:
    two_specs(kit.claims)
    lease(kit.claims, "T1", NOW - timedelta(seconds=600 + 299))

    assert kit.lines(specs=["S1"]) == []
    kit.clock.at = NOW + timedelta(seconds=1)
    assert kit.lines(specs=["S1"]) == ["lease T1 none overdue"]
    assert kit.lines(specs=["S1"]) == []


def test_an_overdue_lease_prints_only_in_scope_and_never_a_lease_without_a_ttl(kit: Kit) -> None:
    two_specs(kit.claims)
    long_ago = NOW - timedelta(days=1)
    lease(kit.claims, "T2", long_ago)
    add(kit.claims, "T3", parent="S1")
    lease(kit.claims, "T3", long_ago, ttl=None)

    assert kit.lines(specs=["S1"]) == []
    assert kit.lines(specs=["S2"]) == ["lease T2 none overdue"]


def park_red_target(claims: Claims, node_id: str, since: datetime) -> None:
    claims.nodes.add_condition(
        Condition(node_id=node_id, needs=f"{RED_TARGET} api main", command="true")
    )
    claims.jobs.create(
        Job(
            kind=JobKind.LAND,
            node_id=node_id,
            repo="api",
            target="main",
            state=JobState.CONDITION_UNMET,
            result={
                "red_target": {
                    "repo": "api",
                    "target": "main",
                    "sha": "a" * 40,
                    "since": since.isoformat(),
                    "failing": ["suite.case"],
                }
            },
        )
    )


def red_lines(kit: Kit) -> list[str]:
    return [line for line in kit.lines() if line.startswith("red target")]


def test_a_red_target_prints_due_once_its_landings_waited_past_the_decision_delay(kit: Kit) -> None:
    add(kit.claims, "T1", status=Status.REVIEWED, outcome="approve")
    delay = kit.claims.config.red_target_decision_after
    park_red_target(kit.claims, "T1", NOW - timedelta(seconds=delay - 1))

    assert red_lines(kit) == []
    kit.clock.at = NOW + timedelta(seconds=1)
    assert red_lines(kit) == ["red target api/main due"]
    assert [t.target for t in kit.claims.red_targets_due(kit.clock.at)] == ["main"]


def test_a_red_target_with_a_decision_is_not_due_and_the_sweep_opens_one_decision_per_due_target(
    kit: Kit,
) -> None:
    add(kit.claims, "T1", status=Status.REVIEWED, outcome="approve")
    park_red_target(kit.claims, "T1", datetime.now(tz=UTC) - timedelta(days=1))
    kit.clock.at = datetime.now(tz=UTC)

    assert red_lines(kit) == ["red target api/main due"]
    opened = kit.claims._escalate_red_targets()

    assert len(opened) == 1
    assert kit.claims.red_targets_due() == []
    assert red_lines(kit) == []
    assert kit.claims._escalate_red_targets() == []


def test_the_sweep_opens_a_decision_for_the_targets_red_targets_due_names(
    kit: Kit, monkeypatch: pytest.MonkeyPatch
) -> None:
    add(kit.claims, "T1", status=Status.REVIEWED, outcome="approve")
    park_red_target(kit.claims, "T1", datetime.now(tz=UTC) - timedelta(days=1))
    monkeypatch.setattr(Claims, "red_targets_due", lambda self, now=None, targets=None: [])

    assert kit.claims._escalate_red_targets() == []


def test_a_red_target_whose_decision_was_answered_is_not_due(kit: Kit) -> None:
    add(kit.claims, "T1", status=Status.REVIEWED, outcome="approve")
    park_red_target(kit.claims, "T1", datetime.now(tz=UTC) - timedelta(days=1))
    (decision_id,) = kit.claims._escalate_red_targets()
    found = kit.claims.nodes.get_node(decision_id)
    assert found is not None
    kit.claims.nodes.save_node(found.model_copy(update={"status": DecisionStatus.ANSWERED}))

    assert kit.claims.red_targets_due(datetime.now(tz=UTC) + timedelta(days=2)) == []


def condition_kit(kit: Kit, command: str, ttl: int = 300) -> Condition:
    kit.claims.conditions.ttl = ttl
    kit.monitor = Monitor(kit.claims, kit.clock, kit.changes)
    add(kit.claims, "S1", NodeKind.SPEC)
    add(kit.claims, "S2", NodeKind.SPEC)
    add(kit.claims, "T1", parent="S1")
    add(kit.claims, "T2", parent="S2")
    return kit.claims.nodes.add_condition(
        Condition(node_id="T1", needs="the build", command=command)
    )


def test_a_held_condition_whose_result_expired_is_re_run_as_the_monitor_and_its_flip_prints(
    kit: Kit, tmp_path: Path
) -> None:
    flag = tmp_path / "ready"
    condition = condition_kit(kit, f"test -f {flag}", ttl=0)
    kit.claims.conditions.cache_repo.put_condition("T1", condition.idx, condition.command, 1)
    flag.write_text("x")
    kit.clock.at = datetime.now(tz=UTC) + timedelta(seconds=5)

    lines = kit.lines(specs=["S1"])

    assert lines == [f'condition met T1 idx={condition.idx} "the build"']
    (event,) = [
        e for e in kit.claims.ops.ledger_repo.list_events("T1") if e.command == "condition met"
    ]
    assert event.actor_id == "monitor"


def test_a_condition_whose_result_is_still_fresh_is_not_re_run(kit: Kit, tmp_path: Path) -> None:
    flag = tmp_path / "ready"
    condition = condition_kit(kit, f"test -f {flag}", ttl=300)
    kit.claims.conditions.cache_repo.put_condition("T1", condition.idx, condition.command, 1)
    flag.write_text("x")
    kit.clock.at = datetime.now(tz=UTC) + timedelta(seconds=299)

    assert kit.lines(specs=["S1"]) == []


def test_a_condition_outside_the_scope_is_not_re_run(kit: Kit, tmp_path: Path) -> None:
    flag = tmp_path / "ready"
    condition = condition_kit(kit, f"test -f {flag}", ttl=0)
    kit.claims.conditions.cache_repo.put_condition("T1", condition.idx, condition.command, 1)
    flag.write_text("x")
    kit.clock.at = datetime.now(tz=UTC) + timedelta(seconds=5)

    assert kit.lines(specs=["S2"]) == []


def test_a_condition_with_no_result_yet_is_not_run(kit: Kit, tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    condition_kit(kit, f"touch {marker}", ttl=0)
    kit.clock.at = datetime.now(tz=UTC) + timedelta(days=1)

    assert kit.lines(specs=["S1"]) == []
    assert not marker.exists()


def test_next_wake_is_the_earliest_of_the_lease_the_red_target_and_the_condition_times(
    kit: Kit,
) -> None:
    condition = condition_kit(kit, "true", ttl=300)
    cache = kit.claims.conditions.cache_repo
    cache.put_condition("T1", condition.idx, condition.command, 0)
    checked = cache.condition_checked_at()[("T1", condition.idx)][1]
    start = checked - timedelta(seconds=10)
    add(kit.claims, "T3", parent="S1")
    add(kit.claims, "T4", parent="S1", status=Status.REVIEWED, outcome="approve")
    delay = kit.claims.config.red_target_decision_after
    lease(kit.claims, "T3", start + timedelta(seconds=100))
    park_red_target(kit.claims, "T4", start - timedelta(seconds=delay - 500))

    due = {
        "condition": start + timedelta(seconds=310),
        "red target": start + timedelta(seconds=500),
        "lease": start + timedelta(seconds=100 + 600 + 300),
    }
    kit.clock.at = start
    woken = []
    while (wake := kit.monitor.next_wake(["S1"])) is not None:
        woken.append(wake)
        kit.clock.at = wake

    assert woken == [due["condition"], due["red target"], due["lease"]]


def test_next_wake_ignores_other_scopes_and_leases_without_a_ttl(kit: Kit) -> None:
    two_specs(kit.claims)
    lease(kit.claims, "T2", NOW + timedelta(seconds=10))
    lease(kit.claims, "T1", NOW + timedelta(seconds=20), ttl=None)

    assert kit.monitor.next_wake(["S1"]) is None
    assert kit.monitor.next_wake(["S2"]) == NOW + timedelta(seconds=10 + 600 + 300)


def test_wait_reads_the_change_counter_before_the_rows(kit: Kit) -> None:
    add(kit.claims, "T1")
    kit.row("job land", "T1", {"to": "LANDED"})
    original = kit.claims.ops.ledger_repo.after

    def after(cursor: int) -> list[LedgerEvent]:
        kit.reads.append("rows")
        return original(cursor)

    kit.claims.ops.ledger_repo.after = after  # type: ignore[method-assign]

    batch = kit.monitor.wait(0, None, None, timeout=30)

    assert batch.lines == ["job land T1 to=LANDED"]
    assert kit.reads == ["version", "rows"]
    assert kit.clock.slept == []


def test_wait_sleeps_until_the_counter_moves_then_prints_the_new_row(kit: Kit) -> None:
    add(kit.claims, "T1")

    def commit_elsewhere() -> None:
        kit.version += 1
        kit.row("job land", "T1", {"to": "LANDED"})

    kit.clock.on_sleep = commit_elsewhere

    batch = kit.monitor.wait(0, None, None, timeout=30)

    assert batch.lines == ["job land T1 to=LANDED"]
    assert kit.clock.slept == [1.0]


def test_wait_returns_the_cursor_it_reached_when_the_timeout_passes(kit: Kit) -> None:
    add(kit.claims, "T1")
    denied = kit.row("note add", "T1")

    batch = kit.monitor.wait(0, None, None, timeout=3)

    assert (batch.lines, batch.cursor) == ([], denied)
    assert kit.clock.at == NOW + timedelta(seconds=3)


def test_wait_wakes_at_the_next_due_moment_without_a_change(kit: Kit) -> None:
    add(kit.claims, "S1", NodeKind.SPEC)
    add(kit.claims, "T1", parent="S1")
    lease(kit.claims, "T1", NOW - timedelta(seconds=600 + 299))

    batch = kit.monitor.wait(0, ["S1"], None, timeout=60)

    assert batch.lines == ["lease T1 none overdue"]
    assert kit.clock.at == NOW + timedelta(seconds=1)
    assert sum(kit.clock.slept) == 1


def test_a_project_config_is_all_the_monitor_needs_to_open(tmp_path: Path) -> None:
    claims = make_estate(tmp_path, config=ProjectConfig())

    assert Monitor(claims, FakeClock(), lambda: 0).batch(0, None, None).lines == []
