"""The lifecycle verbs of the CLI: claiming the next step, closing it, repair verbs,
conditions, jobs, discovery and the fresh-start archive."""

import json
import re
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import Result
from typer.testing import CliRunner

from taskmanager.cli.main import app
from taskmanager.core.models import Job
from taskmanager.core.status import JobKind, JobState, Status
from taskmanager.db.cache_repo import CacheRepository
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.job_repo import JobRepository
from taskmanager.db.node_repo import NodeRepository
from taskmanager.di.container import create_container
from taskmanager.engine.discovery import djb2
from taskmanager.engine.operations import Operations

runner = CliRunner()


def tm(root: Path, *args: str) -> Result:
    return runner.invoke(app, [*args, "-C", str(root)])


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def estate(root: Path) -> None:
    """A spec, a plan and two tasks in the repository `core`, which has an `origin/main`;
    `S1-P1-b` depends on `S1-P1-a`."""
    origin = root / "core.git"
    git(root, "init", "--bare", "-b", "main", str(origin))
    git(root, "clone", str(origin), str(root / "core"))
    work = root / "core"
    git(work, "config", "user.email", "ci@example.com")
    git(work, "config", "user.name", "CI")
    git(work, "commit", "--allow-empty", "-m", "init")
    git(work, "push", "origin", "HEAD:main")
    git(work, "fetch", "origin")
    assert tm(root, "init").exit_code == 0
    tm(root, "spec", "add", "S", "--slug", "S1")
    tm(root, "plan", "add", "P", "--spec", "S1", "--slug", "P1")
    tm(root, "task", "add", "a", "--plan", "S1-P1", "--slug", "a")
    tm(root, "task", "add", "b", "--plan", "S1-P1", "--slug", "b", "--depends-on", "S1-P1-a")
    for task in ("S1-P1-a", "S1-P1-b"):
        assert tm(root, "task", "update", task, "--repo", "core").exit_code == 0


def get(root: Path, node_id: str) -> dict[str, Any]:
    res = tm(root, "task", "get", node_id, "--json")
    assert res.exit_code == 0, res.output
    doc: dict[str, Any] = json.loads(res.stdout)
    return doc


def start(root: Path, node_id: str, *extra: str) -> tuple[int, dict[str, Any]]:
    res = tm(root, "task", "start", node_id, "--agent", "agent-a", "--session", "sess-1", *extra)
    return res.exit_code, yaml.safe_load(res.stdout)


def test_start_claims_implement_then_complete_and_a_review_that_approves(tmp_path: Path) -> None:
    estate(tmp_path)
    code, step = start(tmp_path, "S1-P1-a")
    assert code == 0
    assert set(step) == {
        "action",
        "reason",
        "model",
        "job",
        "repos",
        "branch",
        "base",
        "worktree",
        "worktrees",
        "token",
    }
    assert (step["action"], step["repos"], step["branch"], step["base"]) == (
        "implement",
        ["core"],
        "tm/S1-P1-a",
        "main",
    )
    assert step["worktree"] and Path(step["worktree"]).is_dir()
    assert tm(tmp_path, "task", "heartbeat", "S1-P1-a").exit_code == 0
    leases = json.loads(tm(tmp_path, "run", "list", "--json").stdout)["leases"]
    assert [(lease["task_id"], lease["action"]) for lease in leases] == [("S1-P1-a", "implement")]

    assert tm(tmp_path, "task", "complete", "S1-P1-a").exit_code == 0
    doc = get(tmp_path, "S1-P1-a")
    assert (doc["status"], doc["state"], doc["phase"], doc["lease"]) == (
        "IMPLEMENTED",
        "WAITING_REVIEW",
        "DISPATCHED",
        None,
    )

    code, step = start(tmp_path, "S1-P1-a")
    assert (code, step["action"]) == (0, "review")
    tm(tmp_path, "section", "set", "S1-P1-a:review", "no findings")
    assert (
        tm(tmp_path, "task", "review", "S1-P1-a", "--approve", "--verdict", "clean").exit_code == 0
    )
    doc = get(tmp_path, "S1-P1-a")
    assert (doc["status"], doc["outcome"], doc["verdict"], doc["state"]) == (
        "REVIEWED",
        "approve",
        "clean",
        "WAITING_MERGE",
    )


def test_a_blocked_start_exits_3_and_writes_nothing(tmp_path: Path) -> None:
    estate(tmp_path)
    code, step = start(tmp_path, "S1-P1-b")
    assert code == 3
    assert step["action"] == "blocked" and step["reason"]
    doc = get(tmp_path, "S1-P1-b")
    assert (doc["status"], doc["state"], doc["lease"], doc["blocked_by"]) == (
        "READY",
        "BLOCKED_BY_TASK",
        None,
        ["S1-P1-a"],
    )


def test_review_needs_exactly_one_of_approve_and_reject(tmp_path: Path) -> None:
    estate(tmp_path)
    for flags in ([], ["--approve", "--reject"]):
        res = tm(tmp_path, "task", "review", "S1-P1-a", *flags)
        assert res.exit_code == 2 and "--approve or --reject" in res.output


def test_release_blocked_writes_what_the_node_waits_on_and_returns_it(tmp_path: Path) -> None:
    estate(tmp_path)
    tm(tmp_path, "task", "add", "c", "--plan", "S1-P1", "--slug", "c")
    tm(tmp_path, "task", "update", "S1-P1-c", "--repo", "core")
    assert start(tmp_path, "S1-P1-c")[0] == 0
    bare = tm(tmp_path, "task", "release", "S1-P1-c", "--blocked")
    assert bare.exit_code == 2 and "--depends" in bare.output
    named = tm(tmp_path, "task", "release", "S1-P1-c", "--blocked", "--depends", "S1-P1-a")
    assert named.exit_code == 0, named.output
    doc = get(tmp_path, "S1-P1-c")
    assert (doc["status"], doc["lease"], doc["step_failures"]) == ("READY", None, 0)
    assert [d["id"] for d in doc["depends_on"]] == ["S1-P1-a"]


def test_a_transient_release_counts_a_step_failure(tmp_path: Path) -> None:
    estate(tmp_path)
    assert start(tmp_path, "S1-P1-a")[0] == 0
    assert tm(tmp_path, "task", "release", "S1-P1-a").exit_code == 0
    doc = get(tmp_path, "S1-P1-a")
    assert (doc["status"], doc["lease"], doc["step_failures"]) == ("READY", None, 1)


def test_an_expired_lease_is_swept_back_to_the_status_it_was_claimed_from(tmp_path: Path) -> None:
    estate(tmp_path)
    assert start(tmp_path, "S1-P1-a", "--ttl", "1")[0] == 0
    time.sleep(1.2)
    swept = tm(tmp_path, "run", "sweep")
    assert swept.exit_code == 0 and "S1-P1-a" in swept.output
    doc = get(tmp_path, "S1-P1-a")
    assert (doc["status"], doc["lease"], doc["step_failures"]) == ("READY", None, 1)


def test_defer_reopen_and_abandon_each_record_their_note(tmp_path: Path) -> None:
    estate(tmp_path)
    assert tm(tmp_path, "task", "defer", "S1-P1-a", "--note", "after the launch").exit_code == 0
    doc = get(tmp_path, "S1-P1-a")
    assert doc["status"] == "DEFERRED" and "deferral" in doc["sections"]
    assert tm(tmp_path, "task", "reopen", "S1-P1-a", "--note", "launch done").exit_code == 0
    doc = get(tmp_path, "S1-P1-a")
    assert doc["status"] == "READY" and "reopen" in doc["sections"]
    assert tm(tmp_path, "task", "abandon", "S1-P1-a", "--note", "dropped").exit_code == 0
    doc = get(tmp_path, "S1-P1-a")
    assert doc["status"] == "ABANDONED" and "abandonment" in doc["sections"]
    assert tm(tmp_path, "task", "defer", "S1-P1-b").exit_code == 2


def test_reset_moves_a_node_to_a_stable_status_and_refuses_a_step_status(tmp_path: Path) -> None:
    estate(tmp_path)
    ok = tm(tmp_path, "task", "reset", "S1-P1-a", "--to", "IMPLEMENTED", "--note", "repair")
    assert ok.exit_code == 0, ok.output
    doc = get(tmp_path, "S1-P1-a")
    assert (doc["status"], doc["state"]) == ("IMPLEMENTED", "WAITING_REVIEW")
    refused = tm(tmp_path, "task", "reset", "S1-P1-a", "--to", "REVIEWING", "--note", "x")
    assert refused.exit_code == 1
    assert get(tmp_path, "S1-P1-a")["status"] == "IMPLEMENTED"


def test_condition_add_and_remove_and_prose_is_refused(tmp_path: Path) -> None:
    estate(tmp_path)
    added = tm(
        tmp_path,
        "task",
        "condition",
        "add",
        "S1-P1-a",
        "--needs",
        "staging up",
        "--command",
        "true",
        "--stage",
        "landing",
    )
    assert added.exit_code == 0, added.output
    conditions = get(tmp_path, "S1-P1-a")["conditions"]
    assert [(c["needs"], c["command"], c["stage"]) for c in conditions] == [
        ("staging up", "true", "landing")
    ]
    idx = str(conditions[0]["idx"])
    assert tm(tmp_path, "task", "condition", "remove", "S1-P1-a", idx).exit_code == 0
    assert get(tmp_path, "S1-P1-a")["conditions"] == []
    prose = tm(
        tmp_path,
        "task",
        "condition",
        "add",
        "S1-P1-a",
        "--needs",
        "sign-off",
        "--command",
        "the design is signed off",
    )
    assert prose.exit_code == 1 and "decision" in prose.output


def test_task_update_sets_flags_merge_requires_and_land_order(tmp_path: Path) -> None:
    # S1-P1-a can't take merge=parent here: S1-P1-b depends on it and stays merge=main, and a
    # plan is implemented only once every child has landed, so that pair would deadlock the step
    # graph (S1-P1-b.start needs S1-P1.landed, which needs S1-P1-b.landed). An uninvolved sibling
    # exercises the same CLI write with no such cycle.
    estate(tmp_path)
    plan = tm(tmp_path, "task", "update", "S1-P1", "--review", "--fix", "--land-order", "core,web")
    assert plan.exit_code == 0, plan.output
    tm(tmp_path, "task", "add", "d", "--plan", "S1-P1", "--slug", "d")
    task = tm(
        tmp_path,
        "task",
        "update",
        "S1-P1-d",
        "--merge",
        "parent",
        "--no-fix",
        "--requires",
        "figma,browser",
    )
    assert task.exit_code == 0, task.output
    d = get(tmp_path, "S1-P1-d")
    assert (d["review"], d["fix"], d["merge"], d["requires"]) == (
        True,
        False,
        "parent",
        ["figma", "browser"],
    )
    assert get(tmp_path, "S1-P1")["land_order"] == ["core", "web"]
    assert tm(tmp_path, "task", "update", "S1-P1-b", "--no-review").exit_code != 0
    assert get(tmp_path, "S1-P1-b")["review"] is True


@pytest.mark.parametrize("verb", ["start", "stop", "release", "heartbeat"])
def test_the_old_run_verbs_are_gone(tmp_path: Path, verb: str) -> None:
    assert tm(tmp_path, "init").exit_code == 0
    res = tm(tmp_path, "run", verb, "X")
    assert res.exit_code == 2 and "No such command" in res.output


def test_wave_discover_takes_an_optional_spec_and_has_no_release_flag(tmp_path: Path) -> None:
    estate(tmp_path)
    base = ["wave", "discover", "--session", "s", "--slots", "4", "--max-strong", "1"]
    for extra in ([], ["--spec", "S1"]):
        res = tm(tmp_path, *base, *extra)
        assert res.exit_code == 0, res.output
        payload, check = res.stdout.rstrip("\n").split("\n")
        data = json.loads(payload)
        assert "S1-P1-a" in [c["id"] for c in data["chosen"]]
        assert check == f"__CHECK n={len(data['chosen'])} h={djb2(payload)}"
    assert tm(tmp_path, *base, "--release", "S1-P1-a").exit_code == 2


def test_job_status_of_an_unknown_job_is_refused(tmp_path: Path) -> None:
    assert tm(tmp_path, "init").exit_code == 0
    res = tm(tmp_path, "job", "status", "nope")
    assert res.exit_code == 1 and "nope" in res.output


def test_land_start_refuses_an_unknown_node(tmp_path: Path) -> None:
    estate(tmp_path)
    res = tm(tmp_path, "land", "start", "NOPE")
    assert res.exit_code == 1 and "NOPE" in res.output


def test_init_archive_moves_a_pre_lifecycle_estate_aside_and_starts_fresh(tmp_path: Path) -> None:
    old = tmp_path / ".taskmanager"
    old.mkdir()
    conn = sqlite3.connect(old / "spec.db")
    conn.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()
    refused = tm(tmp_path, "spec", "list")
    assert refused.exit_code == 1 and "tm init --archive" in refused.output
    assert "Traceback" not in refused.output
    res = tm(tmp_path, "init", "--archive")
    assert res.exit_code == 0, res.output
    archives = sorted(old.glob("archive-*"))
    assert len(archives) == 1 and (archives[0] / "spec.db").is_file()
    assert (old / "state.db").is_file()
    assert tm(tmp_path, "spec", "list").exit_code == 0


def test_lists_show_the_stored_status_the_display_and_the_phase(tmp_path: Path) -> None:
    estate(tmp_path)
    rows = json.loads(tm(tmp_path, "task", "list", "--json").stdout)
    assert {r["id"]: (r["status"], r["state"], r["phase"]) for r in rows} == {
        "S1-P1-a": ("READY", "READY", "QUEUED"),
        "S1-P1-b": ("READY", "BLOCKED_BY_TASK", "QUEUED"),
    }


def test_start_cuts_its_worktree_under_the_directory_it_is_given(tmp_path: Path) -> None:
    estate(tmp_path)
    wt_dir = (tmp_path / "scratch" / "worktrees").resolve()
    code, step = start(tmp_path, "S1-P1-a", "--worktree-dir", str(wt_dir))
    assert (code, step["action"]) == (0, "implement")
    worktree = Path(step["worktree"]).resolve()
    assert worktree.is_dir() and worktree.is_relative_to(wt_dir)
    assert isinstance(step["worktrees"], dict)
    assert all(Path(p).resolve().is_relative_to(wt_dir) for p in step["worktrees"].values())


def test_task_get_names_the_next_action_the_lifecycle_gives(tmp_path: Path) -> None:
    estate(tmp_path)
    assert [get(tmp_path, n)["next_action"] for n in ("S1-P1-a", "S1-P1-b", "S1-P1")] == [
        "implement",
        "implement",
        None,
    ]
    assert start(tmp_path, "S1-P1-a")[0] == 0
    assert get(tmp_path, "S1-P1-a")["next_action"] is None
    assert tm(tmp_path, "task", "complete", "S1-P1-a").exit_code == 0
    assert get(tmp_path, "S1-P1-a")["next_action"] == "review"

    db = DatabaseManager(tmp_path / ".taskmanager")
    node_repo, jobs = NodeRepository(db), JobRepository(db)
    node = node_repo.get_node("S1-P1-b")
    assert node is not None
    node.status, node.claimed_from = Status.MERGING, Status.IMPLEMENTED
    node_repo.save_node(node)
    job = jobs.create(Job(kind=JobKind.LAND, node_id="S1-P1-b", repo="core", target="main"))
    assert get(tmp_path, "S1-P1-b")["next_action"] is None
    jobs.set_state(job.model_copy(update={"state": JobState.NEEDS_AGENT}))
    assert get(tmp_path, "S1-P1-b")["next_action"] == "merge"


def test_closing_or_releasing_a_step_in_another_agents_name_is_refused(tmp_path: Path) -> None:
    estate(tmp_path)
    assert start(tmp_path, "S1-P1-a")[0] == 0
    for verb in ("complete", "release"):
        res = tm(tmp_path, "task", verb, "S1-P1-a", "--agent", "agent-b")
        assert res.exit_code == 1, res.output
    doc = get(tmp_path, "S1-P1-a")
    assert (doc["status"], doc["lease"]["agent_id"], doc["step_failures"]) == (
        "IMPLEMENTING",
        "agent-a",
        0,
    )
    assert tm(tmp_path, "task", "complete", "S1-P1-a", "--agent", "agent-a").exit_code == 0

    assert start(tmp_path, "S1-P1-a")[1]["action"] == "review"
    tm(tmp_path, "section", "set", "S1-P1-a:review", "no findings")
    wrong = tm(tmp_path, "task", "review", "S1-P1-a", "--approve", "--agent", "agent-b")
    assert wrong.exit_code == 1, wrong.output
    assert get(tmp_path, "S1-P1-a")["status"] == "REVIEWING"
    assert tm(tmp_path, "task", "release", "S1-P1-a", "--agent", "agent-a").exit_code == 0
    doc = get(tmp_path, "S1-P1-a")
    assert (doc["status"], doc["lease"]) == ("IMPLEMENTED", None)


def test_job_status_waits_while_the_job_runs_and_returns_once_it_leaves_running(
    tmp_path: Path,
) -> None:
    estate(tmp_path)
    jobs = JobRepository(DatabaseManager(tmp_path / ".taskmanager"))
    job = jobs.create(Job(kind=JobKind.LAND, node_id="S1-P1-a", repo="core", target="main"))

    began = time.monotonic()
    res = tm(tmp_path, "job", "status", job.id, "--wait", "1")
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout)["state"] == "running"
    assert time.monotonic() - began >= 1

    def finish() -> None:
        time.sleep(0.5)
        own = JobRepository(DatabaseManager(tmp_path / ".taskmanager"))
        own.set_state(job.model_copy(update={"state": JobState.SUCCEEDED}))

    worker = threading.Thread(target=finish)
    worker.start()
    began = time.monotonic()
    res = tm(tmp_path, "job", "status", job.id, "--wait", "60")
    worker.join()
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout)["state"] == "succeeded"
    assert time.monotonic() - began < 10


REMOVED_RUN_VERBS = re.compile(r"\btm run (start|stop|release|heartbeat)\b")


def test_no_guide_or_command_page_shows_a_removed_run_verb() -> None:
    repo = Path(__file__).resolve().parents[2]
    guides = sorted((repo / "src" / "taskmanager" / "guides").glob("*.md"))
    pages = [*guides, repo / "commands" / "task.md"]
    assert len(pages) == 8, pages
    shown = [
        f"{page.name}: {line}"
        for page in pages
        for line in page.read_text(encoding="utf-8").splitlines()
        if REMOVED_RUN_VERBS.search(line)
    ]
    assert shown == []


def approved(root: Path, node_id: str) -> None:
    assert start(root, node_id)[0] == 0
    assert tm(root, "task", "complete", node_id).exit_code == 0
    assert start(root, node_id)[1]["action"] == "review"
    tm(root, "section", "set", f"{node_id}:review", "no findings")
    assert tm(root, "task", "review", node_id, "--approve").exit_code == 0


def test_a_step_closes_or_releases_only_under_the_token_its_claim_printed(tmp_path: Path) -> None:
    estate(tmp_path)
    code, step = start(tmp_path, "S1-P1-a")
    assert code == 0 and step["token"]
    wrong = tm(tmp_path, "task", "complete", "S1-P1-a", "--token", "not-this-claim")
    assert wrong.exit_code == 1 and "not-this-claim" in wrong.output
    assert tm(tmp_path, "task", "complete", "S1-P1-a", "--token", step["token"]).exit_code == 0

    first = step["token"]
    code, step = start(tmp_path, "S1-P1-a")
    assert (code, step["action"]) == (0, "review") and step["token"] != first
    tm(tmp_path, "section", "set", "S1-P1-a:review", "no findings")
    stale = tm(tmp_path, "task", "review", "S1-P1-a", "--approve", "--token", first)
    assert stale.exit_code == 1, stale.output
    assert tm(tmp_path, "task", "release", "S1-P1-a", "--token", first).exit_code == 1
    assert get(tmp_path, "S1-P1-a")["status"] == "REVIEWING"
    assert tm(tmp_path, "task", "release", "S1-P1-a", "--token", step["token"]).exit_code == 0
    assert get(tmp_path, "S1-P1-a")["status"] == "IMPLEMENTED"

    code, step = start(tmp_path, "S1-P1-a")
    tm(tmp_path, "section", "set", "S1-P1-a:review", "still no findings")
    ok = tm(tmp_path, "task", "review", "S1-P1-a", "--approve", "--token", step["token"])
    assert ok.exit_code == 0, ok.output


def test_wave_discover_holds_only_the_merges_it_is_told_to_hold(tmp_path: Path) -> None:
    estate(tmp_path)
    approved(tmp_path, "S1-P1-a")
    base = ["wave", "discover", "--session", "s", "--slots", "4", "--max-strong", "1"]

    def batch(*extra: str) -> dict[str, Any]:
        res = tm(tmp_path, *base, *extra)
        assert res.exit_code == 0, res.output
        doc: dict[str, Any] = json.loads(res.stdout.split("\n")[0])
        return doc

    offered = batch()
    assert [(c["id"], c["action"]) for c in offered["chosen"]] == [("S1-P1-a", "merge")]
    held = batch("--hold-merge", "S1-P1-a", "--hold-merge", "S1-P1-b")
    assert held["chosen"] == []
    assert "S1-P1-a: merge held by the dispatcher" in held["held"]


def test_land_start_on_a_node_no_merge_claim_holds_pushes_nothing(tmp_path: Path) -> None:
    estate(tmp_path)
    core = tmp_path / "core"
    git(core, "checkout", "-q", "-b", "tm/S1-P1-a")
    (core / "a.py").write_text("x = 1\n")
    git(core, "add", "a.py")
    git(core, "commit", "-q", "-m", "a")
    git(core, "checkout", "-q", "main")
    before = subprocess.run(
        ["git", "ls-remote", "origin", "refs/heads/main"],
        cwd=core,
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    for status in ("READY", "IMPLEMENTED"):
        if status != "READY":
            tm(tmp_path, "task", "reset", "S1-P1-a", "--to", status, "--note", "repair")
        res = tm(tmp_path, "land", "start", "S1-P1-a")
        assert res.exit_code == 1 and "merge claim" in res.output, res.output

    after = subprocess.run(
        ["git", "ls-remote", "origin", "refs/heads/main"],
        cwd=core,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert after == before
    doc = get(tmp_path, "S1-P1-a")
    assert (doc["status"], doc["jobs"], doc["next_action"]) == ("IMPLEMENTED", [], "review")


def test_job_resume_in_another_agents_name_or_under_another_claims_token_is_refused(
    tmp_path: Path,
) -> None:
    estate(tmp_path)
    assert start(tmp_path, "S1-P1-a")[0] == 0
    jobs = JobRepository(DatabaseManager(tmp_path / ".taskmanager"))
    job = jobs.create(Job(kind=JobKind.LAND, node_id="S1-P1-a", repo="core", target="main"))
    jobs.set_state(
        job.model_copy(update={"state": JobState.NEEDS_AGENT, "result": {"reason": "conflict"}})
    )
    other = tm(tmp_path, "job", "resume", job.id, "--agent", "agent-b")
    assert other.exit_code == 1 and "agent-b holds no live lease" in other.output, other.output
    stale = tm(tmp_path, "job", "resume", job.id, "--token", "not-this-claim")
    assert stale.exit_code == 1 and "not-this-claim" in stale.output, stale.output
    stored_job = jobs.get(job.id)
    assert stored_job is not None and stored_job.state == JobState.NEEDS_AGENT


def test_verify_add_refuses_a_check_its_node_cannot_run_and_an_unknown_node(
    tmp_path: Path,
) -> None:
    estate(tmp_path)
    tm(tmp_path, "task", "add", "c", "--plan", "S1-P1", "--slug", "c", "--merge", "parent")
    bad = tm(
        tmp_path,
        "verify",
        "add",
        "S1-P1-c",
        "--type",
        "test_command",
        "--target",
        "git show origin/main:x",
    )
    assert bad.exit_code == 1 and "origin/main" in bad.output, bad.output
    assert get(tmp_path, "S1-P1-c")["verifications"] == []
    assert tm(tmp_path, "task", "update", "S1-P1-c", "--title", "c2").exit_code == 0
    unknown = tm(tmp_path, "verify", "add", "NOPE", "--type", "file_exists", "--target", "x")
    assert unknown.exit_code == 1 and "NOPE" in unknown.output
    assert unknown.exception is None or isinstance(unknown.exception, SystemExit)


def test_a_refused_task_update_prints_its_message_and_exits_1(tmp_path: Path) -> None:
    estate(tmp_path)
    res = tm(tmp_path, "task", "update", "S1-P1-a", "--no-review")
    assert res.exit_code == 1 and "fix needs review" in res.output, res.output


def test_task_get_names_what_its_ancestors_wait_on(tmp_path: Path) -> None:
    estate(tmp_path)
    assert tm(tmp_path, "plan", "add", "Q", "--spec", "S1", "--slug", "P2").exit_code == 0
    tm(tmp_path, "task", "add", "c", "--plan", "S1-P2", "--slug", "c")
    ops = create_container(tmp_path).get(Operations)
    ops.set_dependencies("S1-P2", ["S1-P1-a"], [])
    doc = get(tmp_path, "S1-P2-c")
    assert (doc["state"], doc["blocked_by"]) == ("BLOCKED_BY_TASK", ["S1-P1-a"])

    added = tm(tmp_path, "decision", "add", "Which way?", "--slug", "way", "--blocks", "S1-P2")
    assert added.exit_code == 0, added.output
    doc = get(tmp_path, "S1-P2-c")
    assert (doc["state"], doc["awaiting_decisions"]) == ("AWAITING_DECISION", ["decision-way"])


def test_a_completed_reset_of_a_task_with_no_repository_is_refused(tmp_path: Path) -> None:
    estate(tmp_path)
    tm(tmp_path, "task", "add", "c", "--plan", "S1-P1", "--slug", "c")
    res = tm(tmp_path, "task", "reset", "S1-P1-c", "--to", "COMPLETED", "--note", "done by hand")
    assert res.exit_code == 1 and "no target_repo" in res.output, res.output
    assert get(tmp_path, "S1-P1-c")["status"] == "READY"


def test_next_leaves_out_a_task_whose_claim_condition_last_failed(tmp_path: Path) -> None:
    estate(tmp_path)
    added = tm(
        tmp_path, "task", "condition", "add", "S1-P1-a", "--needs", "up", "--command", "false"
    )
    assert added.exit_code == 0, added.output
    container = create_container(tmp_path)
    [condition] = container.get(NodeRepository).get_conditions("S1-P1-a")
    container.get(CacheRepository).put_condition("S1-P1-a", condition.idx, "false", 1)
    assert get(tmp_path, "S1-P1-a")["state"] == "BLOCKED_BY_CONDITION"
    ranked = json.loads(tm(tmp_path, "next", "--json").stdout)
    assert "S1-P1-a" not in [t["task_id"] for t in ranked]
