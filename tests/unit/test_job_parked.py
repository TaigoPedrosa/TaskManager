from pathlib import Path

from lifecycle_estate import add, attach_landing, gated, make_estate, on_branch, push_main
from test_landing import TRUE, estate_with, land, land_ungated, reviewed_task
from test_sync_landing import landed

from taskmanager.core.enums import NodeKind
from taskmanager.core.models import LedgerEvent
from taskmanager.core.status import JobState, Merge, Status
from taskmanager.engine.claims import Claims


def events(claims: Claims, command: str) -> list[LedgerEvent]:
    return [e for e in claims.ops.ledger_repo.list_events("T1", 500) if e.command == command]


def test_a_landing_that_stops_for_an_agent_records_one_job_parked_row(tmp_path: Path) -> None:
    claims, landing = estate_with(tmp_path, TRUE)
    reviewed_task(claims, path="app.py", content="branch\n")
    push_main(claims.root / "api", "app.py", "main\n")

    job_id, state = land(claims, landing)

    assert state == JobState.NEEDS_AGENT
    parked = events(claims, "job parked")
    assert [(e.target_id, e.payload) for e in parked] == [
        ("T1", {"job": job_id, "reason": "conflict"})
    ]


def test_a_resumed_job_that_stops_again_records_job_stopped_again_and_no_job_parked_row(
    tmp_path: Path,
) -> None:
    claims, landing = estate_with(tmp_path, TRUE)
    reviewed_task(claims)
    job_id, _ = land_ungated(claims, landing)
    assert len(events(claims, "job parked")) == 1

    claims.start("T1", "agent", "s1")
    assert landing.resume(job_id) == JobState.NEEDS_AGENT

    assert len(events(claims, "job parked")) == 1
    assert len(events(claims, "job stopped again")) == 1


def test_a_sync_that_stops_for_an_agent_records_job_parked(tmp_path: Path) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    api = claims.root / "api"
    add(claims, "P", NodeKind.PLAN, review=True, fix=True)
    add(claims, "Y", status=Status.COMPLETED)
    add(claims, "T1", parent="P", merge=Merge.PARENT, depends=("Y",))
    on_branch(api, "tm/P", "app.py", "parent\n")
    landed(api, "Y", "app.py", "main\n")
    landing = attach_landing(claims)
    first = claims.start("T1", "implementer", "s1")
    assert first.job is not None

    assert landing.run(first.job) == JobState.NEEDS_AGENT

    assert [e.payload for e in events(claims, "job parked")] == [
        {"job": first.job, "reason": "conflict"}
    ]
