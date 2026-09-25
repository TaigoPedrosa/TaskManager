import json
from pathlib import Path

from typer.testing import CliRunner

from taskmanager.cli.main import app

runner = CliRunner()


def _seed_task(tmp_path: Path) -> None:
    runner.invoke(app, ["init", "-C", str(tmp_path)])
    runner.invoke(app, ["spec", "add", "S", "--slug", "S1", "-C", str(tmp_path)])
    runner.invoke(app, ["plan", "add", "P", "--spec", "S1", "--slug", "P1", "-C", str(tmp_path)])
    runner.invoke(app, ["task", "add", "T", "--plan", "S1-P1", "--slug", "T1", "-C", str(tmp_path)])


def test_decision_add_blocks_task_and_reads_awaiting_decision(tmp_path: Path) -> None:
    _seed_task(tmp_path)
    res = runner.invoke(
        app,
        [
            "decision",
            "add",
            "Which way?",
            "--slug",
            "way",
            "--option",
            "a|Do X",
            "--option",
            "b|Do Y",
            "--recommend",
            "a",
            "--blocks",
            "S1-P1-T1",
            "-C",
            str(tmp_path),
        ],
    )
    assert res.exit_code == 0, res.output
    assert "decision-way" in res.output

    task = json.loads(
        runner.invoke(app, ["task", "get", "S1-P1-T1", "--json", "-C", str(tmp_path)]).stdout
    )
    assert task["state"] == "AWAITING_DECISION"
    assert task["awaiting_decisions"] == ["decision-way"]


def test_decision_list_filters_by_status(tmp_path: Path) -> None:
    _seed_task(tmp_path)
    runner.invoke(app, ["decision", "add", "Q1", "--slug", "q1", "-C", str(tmp_path)])
    runner.invoke(app, ["decision", "add", "Q2", "--slug", "q2", "-C", str(tmp_path)])
    runner.invoke(app, ["decision", "withdraw", "decision-q2", "-C", str(tmp_path)])

    open_out = runner.invoke(
        app, ["decision", "list", "--status", "open", "--json", "-C", str(tmp_path)]
    ).stdout
    assert "decision-q1" in open_out
    assert "decision-q2" not in open_out

    withdrawn_out = runner.invoke(
        app, ["decision", "list", "--status", "withdrawn", "--json", "-C", str(tmp_path)]
    ).stdout
    assert "decision-q2" in withdrawn_out


def test_decision_answer_reopen_and_unblocks_task(tmp_path: Path) -> None:
    _seed_task(tmp_path)
    runner.invoke(
        app,
        [
            "decision",
            "add",
            "Which way?",
            "--slug",
            "way",
            "--option",
            "a|Do X",
            "--blocks",
            "S1-P1-T1",
            "-C",
            str(tmp_path),
        ],
    )
    res = runner.invoke(
        app, ["decision", "answer", "decision-way", "--option", "a", "-C", str(tmp_path)]
    )
    assert res.exit_code == 0, res.output

    task = json.loads(
        runner.invoke(app, ["task", "get", "S1-P1-T1", "--json", "-C", str(tmp_path)]).stdout
    )
    assert task["state"] == "READY"

    res = runner.invoke(app, ["decision", "reopen", "decision-way", "-C", str(tmp_path)])
    assert res.exit_code == 0, res.output
    task = json.loads(
        runner.invoke(app, ["task", "get", "S1-P1-T1", "--json", "-C", str(tmp_path)]).stdout
    )
    assert task["state"] == "AWAITING_DECISION"


def test_decision_withdraw_unblocks_task(tmp_path: Path) -> None:
    _seed_task(tmp_path)
    runner.invoke(
        app,
        [
            "decision",
            "add",
            "Which way?",
            "--slug",
            "way",
            "--blocks",
            "S1-P1-T1",
            "-C",
            str(tmp_path),
        ],
    )
    res = runner.invoke(
        app, ["decision", "withdraw", "decision-way", "--reason", "moot", "-C", str(tmp_path)]
    )
    assert res.exit_code == 0, res.output
    task = json.loads(
        runner.invoke(app, ["task", "get", "S1-P1-T1", "--json", "-C", str(tmp_path)]).stdout
    )
    assert task["state"] == "READY"


def test_decision_block_and_unblock(tmp_path: Path) -> None:
    _seed_task(tmp_path)
    runner.invoke(app, ["decision", "add", "Q", "--slug", "q1", "-C", str(tmp_path)])
    res = runner.invoke(
        app, ["decision", "block", "decision-q1", "--tasks", "S1-P1-T1", "-C", str(tmp_path)]
    )
    assert res.exit_code == 0, res.output
    task = json.loads(
        runner.invoke(app, ["task", "get", "S1-P1-T1", "--json", "-C", str(tmp_path)]).stdout
    )
    assert task["state"] == "AWAITING_DECISION"

    res = runner.invoke(
        app, ["decision", "unblock", "decision-q1", "--tasks", "S1-P1-T1", "-C", str(tmp_path)]
    )
    assert res.exit_code == 0, res.output
    task = json.loads(
        runner.invoke(app, ["task", "get", "S1-P1-T1", "--json", "-C", str(tmp_path)]).stdout
    )
    assert task["state"] == "READY"


def test_render_a_decision(tmp_path: Path) -> None:
    _seed_task(tmp_path)
    runner.invoke(
        app,
        [
            "decision",
            "add",
            "Which way?",
            "--slug",
            "way",
            "--option",
            "a|Do X",
            "--recommend",
            "a",
            "-C",
            str(tmp_path),
        ],
    )
    res = runner.invoke(app, ["render", "decision-way", "-C", str(tmp_path)])
    assert res.exit_code == 0, res.output
    assert "Which way?" in res.output
    assert "Do X" in res.output
    assert "recommended" in res.output


def test_attach_detach_and_attachments_check(tmp_path: Path) -> None:
    _seed_task(tmp_path)
    project_file = tmp_path / "shot.png"
    project_file.write_bytes(b"png-bytes")

    res = runner.invoke(app, ["attach", "S1-P1-T1", str(project_file), "-C", str(tmp_path)])
    assert res.exit_code == 0, res.output

    listed = json.loads(
        runner.invoke(
            app, ["attachments", "S1-P1-T1", "--check", "--json", "-C", str(tmp_path)]
        ).stdout
    )
    assert len(listed) == 1
    assert listed[0]["source"]["state"] == "fresh"
    asset_name = listed[0]["asset"]
    assert (tmp_path / ".taskmanager" / "assets" / asset_name).exists()

    res = runner.invoke(app, ["detach", "S1-P1-T1", asset_name, "-C", str(tmp_path)])
    assert res.exit_code == 0, res.output
    assert not (tmp_path / ".taskmanager" / "assets" / asset_name).exists()
    assert (
        json.loads(
            runner.invoke(app, ["attachments", "S1-P1-T1", "--json", "-C", str(tmp_path)]).stdout
        )
        == []
    )


def test_decision_add_unknown_recommend_exits_nonzero(tmp_path: Path) -> None:
    _seed_task(tmp_path)
    res = runner.invoke(
        app,
        [
            "decision",
            "add",
            "Q",
            "--slug",
            "q1",
            "--option",
            "a|A",
            "--recommend",
            "nope",
            "-C",
            str(tmp_path),
        ],
    )
    assert res.exit_code != 0


def test_a_decisions_payload_carries_no_step_flags(tmp_path: Path) -> None:
    _seed_task(tmp_path)
    runner.invoke(app, ["decision", "add", "Q1", "--slug", "q1", "-C", str(tmp_path)])
    [row] = json.loads(
        runner.invoke(app, ["decision", "list", "--json", "-C", str(tmp_path)]).stdout
    )
    got = json.loads(
        runner.invoke(app, ["decision", "get", "decision-q1", "--json", "-C", str(tmp_path)]).stdout
    )
    for payload in (row, got):
        assert not {"review", "fix", "merge"} & payload.keys()
