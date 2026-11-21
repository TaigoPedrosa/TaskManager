import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from lifecycle_estate import add, gated, make_estate
from test_discovery import batch, hold, waiting_job
from typer.testing import CliRunner

from taskmanager.cli.main import app as cli_app
from taskmanager.core.status import Action, JobKind, Outcome, Status
from taskmanager.engine.claims import Claims
from taskmanager.engine.config import HarnessConfig, ProjectConfig
from taskmanager.engine.discovery import discover
from taskmanager.engine.operations import OperationError

CODEX_NOTE = "harness default: harnesses.codex.models.standard is unset"


def with_config(**update: Any) -> ProjectConfig:
    return gated("api").model_copy(update=update)


def start_cli(claims: Claims, *flags: str) -> Any:
    return CliRunner().invoke(
        cli_app,
        [
            "task",
            "start",
            "T1",
            "--agent",
            "a1",
            "--session",
            "s1",
            "--worktree-dir",
            str(claims.root / "api" / ".wt"),
            "--json",
            *flags,
            "-C",
            str(claims.root),
        ],
    )


def ledger_start(claims: Claims) -> dict[str, Any]:
    events = [e for e in claims.ops.ledger_repo.list_events("T1") if e.command == "task start"]
    assert len(events) == 1
    return events[0].payload


def test_a_claude_code_claim_prints_the_resolved_model_and_stores_the_tier(tmp_path: Path) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1")

    res = start_cli(claims, "--harness", "claude-code")

    assert res.exit_code == 0, res.output
    out = json.loads(res.stdout)
    assert (out["model"], out["route"]) == ("sonnet", "standard")
    assert "model_note" not in out
    lease = claims.runtime.get_lease("T1")
    assert lease is not None
    assert lease.route == "standard"
    assert ledger_start(claims) == {
        "action": "implement",
        "agent": "a1",
        "from": "READY",
        "route": "standard",
        "model": "sonnet",
    }


@pytest.mark.parametrize("name", ["antigravity", "codex", "copilot", "cursor", "gemini", "kiro"])
def test_a_listed_harness_with_no_profile_prints_null_and_the_note(
    tmp_path: Path, name: str
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1")

    res = start_cli(claims, "--harness", name)

    assert res.exit_code == 0, res.output
    out = json.loads(res.stdout)
    note = f"harness default: harnesses.{name}.models.standard is unset"
    assert (out["model"], out["route"], out["model_note"]) == (None, "standard", note)
    assert ledger_start(claims)["model"] is None
    assert ledger_start(claims)["route"] == "standard"


def test_a_profile_that_maps_the_tier_prints_its_string_and_no_note(tmp_path: Path) -> None:
    claims = make_estate(
        tmp_path,
        config=with_config(harnesses={"codex": HarnessConfig(models={"standard": "gpt-5-mini"})}),
    )
    add(claims, "T1")

    out = json.loads(start_cli(claims, "--harness", "codex").stdout)

    assert (out["model"], out["route"]) == ("gpt-5-mini", "standard")
    assert "model_note" not in out


def test_a_configured_name_no_listing_knows_is_a_known_harness(tmp_path: Path) -> None:
    claims = make_estate(
        tmp_path,
        config=with_config(harnesses={"my-cli": HarnessConfig(models={"light": "tiny"})}),
    )
    add(claims, "T1")

    out = json.loads(start_cli(claims, "--harness", "my-cli").stdout)

    assert out["model"] is None


def test_without_the_flag_the_config_harness_decides(tmp_path: Path) -> None:
    claims = make_estate(
        tmp_path,
        config=with_config(
            harness="codex", harnesses={"codex": HarnessConfig(models={"standard": "gpt-5-mini"})}
        ),
    )
    add(claims, "T1")

    out = json.loads(start_cli(claims).stdout)

    assert out["model"] == "gpt-5-mini"


def test_tm_config_set_harness_switches_the_default(tmp_path: Path) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1")
    set_profile = CliRunner().invoke(
        cli_app,
        ["config", "set", "harnesses.codex.models.standard", "gpt-5-mini", "-C", str(claims.root)],
    )
    assert set_profile.exit_code == 0, set_profile.output
    switch = CliRunner().invoke(
        cli_app, ["config", "set", "harness", "codex", "-C", str(claims.root)]
    )
    assert switch.exit_code == 0, switch.output
    assert (
        yaml.safe_load((claims.root / ".taskmanager" / "config.yaml").read_text())["harness"]
        == "codex"
    )

    res = start_cli(claims)

    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout)["model"] == "gpt-5-mini"


def test_an_unknown_harness_exits_two_names_the_profiles_and_writes_no_lease(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1")

    res = start_cli(claims, "--harness", "nope")

    assert res.exit_code == 2
    text = " ".join(re.sub(r"[│╭╰╮╯─]", " ", res.output).split())
    assert "known profiles: antigravity, claude-code, codex, copilot, cursor, gemini, kiro" in text
    assert "tm config set harnesses.nope.models.<light|standard|strong>" in text
    assert claims.runtime.get_lease("T1") is None
    discovery = CliRunner().invoke(
        cli_app,
        ["wave", "discover", "--session", "s1", "--harness", "nope", "-C", str(claims.root)],
    )
    assert discovery.exit_code == 2


def test_the_engine_refuses_an_unknown_harness_before_any_lease(tmp_path: Path) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1")

    with pytest.raises(OperationError, match="unknown harness 'nope'"):
        claims.start("T1", "a1", "s1", harness="nope")

    assert claims.runtime.get_lease("T1") is None


def test_a_pinned_model_id_is_printed_as_it_is_and_is_its_own_route(tmp_path: Path) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1", models=["claude-haiku-4"])

    out = json.loads(start_cli(claims, "--harness", "claude-code").stdout)

    assert (out["model"], out["route"]) == ("claude-haiku-4", "claude-haiku-4")


def test_discovery_resolves_each_chosen_entry_for_the_harness(tmp_path: Path) -> None:
    claims = make_estate(
        tmp_path,
        config=with_config(harnesses={"codex": HarnessConfig(models={"strong": "gpt-5"})}),
    )
    add(claims, "T1")

    default = batch(claims)["chosen"][0]
    codex = batch(claims, harness="codex")["chosen"][0]

    assert (default["model"], default["route"]) == ("sonnet", "standard")
    assert (codex["model"], codex["route"]) == (None, "standard")


def test_discovery_lines_print_the_resolved_model_and_a_dash_for_a_null_one(
    tmp_path: Path,
) -> None:
    claims = make_estate(
        tmp_path,
        config=with_config(harnesses={"codex": HarnessConfig(models={"strong": "gpt-5"})}),
    )
    add(claims, "T1")

    def lines(harness: str) -> str:
        res = CliRunner().invoke(
            cli_app,
            [
                "wave", "discover", "--session", "s1", "--lines", "--harness", harness,
                "-C", str(claims.root),
            ],
        )  # fmt: skip
        assert res.exit_code == 0, res.output
        return str(res.stdout.splitlines()[0])

    assert lines("claude-code").startswith("N T1 implement sonnet task ")
    assert lines("codex").startswith("N T1 implement - task ")


def test_discovery_counts_strong_slots_by_route_not_by_resolved_model(tmp_path: Path) -> None:
    claims = make_estate(tmp_path, config=gated("api"))
    add(claims, "T1", models=["strong"])

    payload, count = discover(claims, None, "s1", 5, 0, harness="claude-code")

    assert count == 0
    assert json.loads(payload)["held"] == ["T1: no free strong-model slot"]


def test_a_handed_over_job_prints_its_model_resolved_for_the_harness(tmp_path: Path) -> None:
    claims = make_estate(
        tmp_path,
        config=with_config(harnesses={"codex": HarnessConfig(models={"standard": "gpt-5-mini"})}),
    )
    add(claims, "T1", status=Status.REVIEWED, outcome=Outcome.APPROVE)
    hold(claims, "T1", "other", Status.MERGING, Action.MERGE)
    waiting_job(claims, "T1", JobKind.LAND)

    handed = claims.start("T1", "a1", "s1", harness="codex")

    assert handed.action == Action.MERGE
    assert (handed.model, handed.route, handed.model_note) == ("gpt-5-mini", "standard", None)
    lease = claims.runtime.get_lease("T1")
    assert lease is not None
    assert lease.route == "standard"


def test_a_handed_over_job_on_an_unmapped_tier_carries_the_note(tmp_path: Path) -> None:
    claims = make_estate(
        tmp_path,
        config=with_config(harnesses={"codex": HarnessConfig(models={"strong": "gpt-5"})}),
    )
    add(claims, "T1", status=Status.REVIEWED, outcome=Outcome.APPROVE)
    hold(claims, "T1", "other", Status.MERGING, Action.MERGE)
    waiting_job(claims, "T1", JobKind.LAND)

    handed = claims.start("T1", "a1", "s1", harness="codex")

    assert (handed.model, handed.model_note) == (None, CODEX_NOTE)


def test_an_unknown_config_harness_exits_two_on_a_claim_without_the_flag(tmp_path: Path) -> None:
    claims = make_estate(tmp_path, config=with_config(harness="nope"))
    add(claims, "T1")

    res = start_cli(claims)

    assert res.exit_code == 2
    assert claims.runtime.get_lease("T1") is None


def test_discovery_without_the_flag_resolves_for_the_config_harness(tmp_path: Path) -> None:
    claims = make_estate(tmp_path, config=with_config(harness="codex"))
    add(claims, "T1")

    entry = batch(claims)["chosen"][0]

    assert (entry["model"], entry["route"]) == (None, "standard")
