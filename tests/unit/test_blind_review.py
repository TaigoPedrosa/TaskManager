from pathlib import Path

import pytest
from typer.testing import CliRunner

from taskmanager.cli.main import app
from taskmanager.core.models import Check, NodeSection, Note
from taskmanager.core.status import CheckResult, Status
from taskmanager.db.node_repo import NodeRepository
from taskmanager.di.container import create_container
from taskmanager.engine.operations import Operations

runner = CliRunner()
TASK = "S-P-t"


def tm(*args: str) -> str:
    res = runner.invoke(app, list(args))
    assert res.exit_code == 0, res.output
    return res.output


@pytest.fixture
def root(tmp_path: Path) -> Path:
    tm("init", "-C", str(tmp_path))
    ops = create_container(tmp_path).get(Operations)
    ops.add_task("t", ops.add_plan("plan", ops.add_spec("spec", slug="S"), slug="P"), slug="t")
    for ordinal, (key, content) in enumerate(
        [("acceptance", "the acceptance line"), ("report", "the implementer's report")], 1
    ):
        ops.node_repo.save_section(
            NodeSection(
                node_id=TASK,
                section_key=key,
                ordinal=ordinal,
                header=f"## {key.title()}",
                content=content,
            )
        )
    ops.node_repo.add_check(
        Check(node_id=TASK, deleted="a.py:guard", result=CheckResult.RED, test="test_guard")
    )
    ops.node_repo.add_check(Check(node_id=TASK, deleted="a.py:escape", result=CheckResult.GREEN))
    ops.node_repo.add_note(Note(node_id=TASK, text="the implementer's note"))
    return tmp_path


CHECKS = "### Checks\n- #1 `a.py:guard`: red, `test_guard` failed\n- #2 `a.py:escape`: green\n\n"
NOTES = "### Notes\n- the implementer's note\n"


def brief(root: Path, status: Status, claimed_from: Status) -> str:
    repo = create_container(root).get(NodeRepository)
    node = repo.get_node(TASK)
    assert node is not None
    repo.save_node(node.model_copy(update={"status": status, "claimed_from": claimed_from}))
    return tm("render", TASK, "--view", "subagent", "-C", str(root))


def test_review_brief_by_default_leaves_out_the_report(root: Path) -> None:
    out = brief(root, Status.REVIEWING, Status.IMPLEMENTED)
    assert "the acceptance line" in out
    assert "the implementer's report" not in out
    assert "the implementer's note" not in out
    assert CHECKS in out


def test_review_brief_with_blind_off_keeps_the_report(root: Path) -> None:
    tm("config", "set", "review.blind", "false", "-C", str(root))
    out = brief(root, Status.REVIEWING, Status.IMPLEMENTED)
    assert "the acceptance line" in out
    assert "the implementer's report" in out
    assert CHECKS + NOTES in out


@pytest.mark.parametrize("blind", ["true", "false"])
def test_fix_brief_keeps_the_report_whatever_review_blind(root: Path, blind: str) -> None:
    tm("config", "set", "review.blind", blind, "-C", str(root))
    out = brief(root, Status.FIXING, Status.REVIEWED)
    assert "the implementer's report" in out
    assert CHECKS + NOTES in out


def test_full_view_of_a_review_keeps_the_report(root: Path) -> None:
    brief(root, Status.REVIEWING, Status.IMPLEMENTED)
    out = tm("render", TASK, "--view", "full", "-C", str(root))
    assert "the implementer's report" in out
    assert CHECKS + NOTES in out


def test_config_set_review_blind_refuses_a_non_boolean(root: Path) -> None:
    res = runner.invoke(app, ["config", "set", "review.blind", "sometimes", "-C", str(root)])
    assert res.exit_code != 0
    assert "review.blind" in res.output
