import json
from pathlib import Path
from typing import Any

import pytest
from lifecycle_estate import add, make_estate, stored

from taskmanager.core.enums import NodeKind
from taskmanager.core.models import Job, Lease
from taskmanager.core.status import Action, JobKind, JobState, Merge, Outcome, Status
from taskmanager.engine.claims import Claims
from taskmanager.engine.discovery import discover, djb2

MIGRATION = ["api/migrations/versions/001_add.py"]


def batch(claims: Claims, **args: Any) -> dict[str, Any]:
    options: dict[str, Any] = {"specs": None, "session": "s1", "slots": 10, "max_strong": 10}
    options.update(args)
    payload, count = discover(claims, **options)
    data: dict[str, Any] = json.loads(payload)
    assert count == len(data["chosen"])
    return data


def chosen(data: dict[str, Any]) -> list[tuple[str, str]]:
    return [(entry["id"], entry["action"]) for entry in data["chosen"]]


def hold(
    claims: Claims,
    node_id: str,
    session: str,
    step: Status,
    action: Action,
    model: str | None = None,
) -> None:
    """Claims `node_id` into `step` from its stored status, as a dispatcher's claim would."""
    node = stored(claims, node_id)
    lease = Lease(
        task_id=node_id,
        agent_id=f"agent-{node_id}",
        session_id=session,
        branch_name=f"tm/{node_id}",
        ttl_seconds=3600,
        action=action,
        model=model,
    )
    moved = node.model_copy(update={"status": step, "claimed_from": node.status})
    assert claims.runtime.claim(lease, [], moved)


def waiting_job(claims: Claims, node_id: str, kind: JobKind) -> str:
    job = claims.jobs.create(
        Job(
            kind=kind,
            node_id=node_id,
            repo="api",
            target="main" if kind == JobKind.LAND else "tm/P",
            state=JobState.NEEDS_AGENT,
            step="gate",
            worktree=f"/tmp/waiting-{node_id}",
            result={"reason": "conflict", "source": "origin/main"},
        )
    )
    claims.runtime.park(node_id)
    return job.id


def test_the_batch_checksum_is_djb2_over_the_payload_bytes() -> None:
    assert djb2("") == 5381
    assert djb2("a") == (5381 * 33 + ord("a")) & 0xFFFFFFFF


def test_every_kind_of_node_is_offered_with_its_next_step_model_and_requirements(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path)
    add(claims, "S", NodeKind.SPEC)
    add(claims, "P1", NodeKind.PLAN, parent="S", review=True, fix=True, status=Status.IMPLEMENTED)
    add(claims, "C1", parent="P1", status=Status.COMPLETED)
    add(claims, "P2", NodeKind.PLAN, parent="S")
    add(claims, "T1", parent="P2", models=["claude-haiku-4"], requires=["figma"])
    add(claims, "T2", parent="P2", status=Status.REVIEWED, outcome=Outcome.APPROVE)

    data = batch(claims)

    assert chosen(data) == [("T2", "merge"), ("P1", "review"), ("T1", "implement")]
    by_id = {entry["id"]: entry for entry in data["chosen"]}
    assert (by_id["P1"]["model"], by_id["T1"]["model"], by_id["T2"]["model"]) == (
        "opus",
        "haiku",
        "sonnet",
    )
    assert by_id["T1"]["requires"] == ["figma"]
    assert by_id["T1"]["repos"] == ["api"]
    assert by_id["P1"]["kind"] == "plan"


def test_a_landing_or_a_sync_waiting_for_an_agent_is_offered_with_its_job(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)
    add(claims, "T1", status=Status.REVIEWED, outcome=Outcome.APPROVE)
    hold(claims, "T1", "other", Status.MERGING, Action.MERGE)
    land = waiting_job(claims, "T1", JobKind.LAND)
    add(claims, "X")
    assert claims.hold_for_sync("X")
    sync = waiting_job(claims, "X", JobKind.SYNC)

    data = batch(claims)

    jobs = {entry["id"]: (entry["action"], entry["job"]) for entry in data["chosen"]}
    assert jobs == {"T1": ("merge", land), "X": ("sync", sync)}


def test_a_node_that_cannot_be_claimed_is_held_with_its_reason(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)
    add(claims, "D")
    add(claims, "T1", depends=("D",))

    data = batch(claims)

    assert chosen(data) == [("D", "implement")]
    assert "T1: waits on D" in data["held"]


@pytest.mark.parametrize(
    ("specs", "expected"),
    [
        (None, {"T1", "T2", "T3"}),
        (["S1"], {"T1"}),
        (["S1", "S2"], {"T1", "T2"}),
        (["none"], {"T3"}),
    ],
)
def test_specs_scope_the_batch_and_none_names_the_nodes_under_no_spec(
    tmp_path: Path, specs: list[str] | None, expected: set[str]
) -> None:
    claims = make_estate(tmp_path)
    for spec, plan, task in (("S1", "P1", "T1"), ("S2", "P2", "T2")):
        add(claims, spec, NodeKind.SPEC)
        add(claims, plan, NodeKind.PLAN, parent=spec)
        add(claims, task, parent=plan)
    add(claims, "T3")

    data = batch(claims, specs=specs)

    assert {entry["id"] for entry in data["chosen"]} == expected


def test_slots_strong_slots_exclusions_and_file_overlap_shape_the_batch(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)
    add(claims, "T1", files=["api/a.py"], priority=90)
    add(claims, "T2", files=["api/a.py"], priority=80)
    add(claims, "T3", models=["claude-opus-4"], priority=70)
    add(claims, "T4", priority=60)
    add(claims, "T5", priority=50)
    add(claims, "T6", priority=40)

    data = batch(claims, slots=2, max_strong=0, exclude=["T4"])

    assert chosen(data) == [("T1", "implement"), ("T5", "implement")]
    assert "T2: declared_files overlap a node chosen this wave" in data["held"]
    assert "T3: no free opus/fable slot" in data["held"]
    assert "T4: excluded by args" in data["held"]
    assert data["waiting_for_slot"] == 1


def test_the_session_leases_take_its_slots(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)
    add(claims, "T0")
    hold(claims, "T0", "s1", Status.IMPLEMENTING, Action.IMPLEMENT)
    add(claims, "T1")

    data = batch(claims, slots=1)

    assert (chosen(data), data["waiting_for_slot"], data["mine"]) == ([], 1, 1)


def test_a_session_lease_routed_to_a_strong_model_takes_a_strong_slot(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)
    add(claims, "T0")
    hold(claims, "T0", "s1", Status.IMPLEMENTING, Action.IMPLEMENT, model="opus")
    add(claims, "T1", models=["claude-opus-4"], priority=90)
    add(claims, "T2", priority=80)

    data = batch(claims, max_strong=1)

    assert chosen(data) == [("T2", "implement")]
    assert "T1: no free opus/fable slot" in data["held"]


def test_a_migration_writer_holds_its_repository_chain_until_it_lands_on_main(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path)
    add(claims, "A", files=MIGRATION, status=Status.IMPLEMENTED)
    add(claims, "B", files=["api/migrations/versions/002_more.py"])
    add(claims, "C", files=["api/app.py"])

    data = batch(claims)

    assert chosen(data) == [("A", "review"), ("C", "implement")]
    assert "B: api migration chain held by A" in data["held"]


def test_a_sibling_building_on_a_landed_migration_proceeds_while_others_wait(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path)
    add(claims, "P", NodeKind.PLAN)
    add(claims, "A", parent="P", merge=Merge.PARENT, files=MIGRATION, status=Status.COMPLETED)
    add(claims, "B", parent="P", merge=Merge.PARENT, files=["api/migrations/versions/002_b.py"])
    add(claims, "C", files=["api/migrations/versions/003_c.py"])

    data = batch(claims)

    assert chosen(data) == [("B", "implement")]
    assert "C: api migration chain held by A" in data["held"]


def test_two_ready_migration_writers_in_one_repository_are_never_chosen_together(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path)
    add(claims, "B1", files=["api/migrations/versions/001_b1.py"], priority=90)
    add(claims, "B2", files=["api/migrations/versions/002_b2.py"], priority=80)

    data = batch(claims)

    assert chosen(data) == [("B1", "implement")]
    assert "B2: api migration chain held by B1" in data["held"]
    assert next(e for e in data["chosen"] if e["id"] == "B1")["migration"] is True


def test_the_migration_chain_follows_migration_order_not_declared_priority(
    tmp_path: Path,
) -> None:
    """A candidate order driven by priority would grant the chain to whichever ready writer this
    wave reaches first: here that is A, the higher-priority one. `migration_order` instead ranks
    by ordinal, putting B ahead, so discovery must grant the chain to B and hold A behind it."""
    claims = make_estate(tmp_path)
    add(claims, "A", files=MIGRATION, priority=90, ordinal=1)
    add(claims, "B", files=["api/migrations/versions/002_b.py"], priority=10, ordinal=0)

    data = batch(claims)

    assert chosen(data) == [("B", "implement")]
    assert "A: api migration chain held by B" in data["held"]


def test_a_node_the_lifecycle_cannot_read_is_held_and_its_siblings_are_still_chosen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from taskmanager.core.lifecycle import LifecycleError

    claims = make_estate(tmp_path)
    add(claims, "BAD")
    add(claims, "OK")
    real = Claims.next_step

    def unreadable(self: Claims, node: Any) -> Any:
        if node.id == "BAD":
            raise LifecycleError("REVIEWED with no outcome")
        return real(self, node)

    monkeypatch.setattr(Claims, "next_step", unreadable)
    data = batch(claims)
    assert chosen(data) == [("OK", "implement")]
    assert data["held"] == ["BAD: REVIEWED with no outcome"]


def test_a_held_merge_is_skipped_while_the_same_nodes_other_steps_are_offered(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path)
    add(claims, "T1", status=Status.REVIEWED, outcome=Outcome.APPROVE)
    add(claims, "T2", status=Status.IMPLEMENTED)
    add(claims, "T3")
    add(claims, "T4", status=Status.REVIEWED, outcome=Outcome.APPROVE)
    hold(claims, "T4", "other", Status.MERGING, Action.MERGE)
    waiting_job(claims, "T4", JobKind.LAND)

    data = batch(claims, hold_merge=["T1", "T2", "T3", "T4"])

    assert chosen(data) == [("T2", "review"), ("T3", "implement")]
    assert "T1: merge held by the dispatcher" in data["held"]
    assert "T4: merge held by the dispatcher" in data["held"]


def test_a_job_parked_for_an_agent_takes_no_slot_of_its_session(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)
    add(claims, "T0", status=Status.REVIEWED, outcome=Outcome.APPROVE)
    hold(claims, "T0", "s1", Status.MERGING, Action.MERGE)
    waiting_job(claims, "T0", JobKind.LAND)
    add(claims, "T1")

    data = batch(claims, slots=1)

    assert (chosen(data), data["waiting_for_slot"], data["mine"]) == ([("T0", "merge")], 1, 0)
