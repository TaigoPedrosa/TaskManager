import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from lifecycle_estate import make_estate
from typer.testing import CliRunner, Result

from taskmanager.cli import main as cli_main
from taskmanager.core.models import LedgerEvent
from taskmanager.engine.claims import Claims
from taskmanager.engine.monitor import Monitor

NOW = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
runner = CliRunner()


class FakeClock:
    def __init__(self) -> None:
        self.at = NOW
        self.slept: list[float] = []
        self.on_sleep: list[Callable[[], None]] = []

    def now(self) -> datetime:
        return self.at

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.at += timedelta(seconds=seconds)
        if self.on_sleep:
            self.on_sleep.pop(0)()


class Estate:
    def __init__(self, root: Path, claims: Claims, clock: FakeClock) -> None:
        self.claims = claims
        self.clock = clock
        self.version = 0
        self.root = root

    def row(self, command: str, payload: dict[str, object] | None = None) -> int:
        event = LedgerEvent(
            actor_id="cli",
            command=command,
            target_id="config",
            payload=payload or {},
            timestamp=NOW,
        )
        self.claims.ops.ledger_repo.append(event)
        self.version += 1
        assert event.id is not None
        return event.id

    def run(self, *args: str) -> Result:
        return runner.invoke(cli_main.app, ["monitor", *args, "-C", str(self.root)])

    def cursor_file(self, session: str) -> Path:
        return self.root / ".taskmanager" / "monitor" / f"{session}.json"

    def stored(self, session: str) -> dict[str, object]:
        return json.loads(self.cursor_file(session).read_text())


@pytest.fixture
def estate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Estate:
    clock = FakeClock()
    found = Estate(tmp_path / "estate", make_estate(tmp_path), clock)
    monkeypatch.setattr(cli_main, "SystemClock", lambda: clock)
    monkeypatch.setattr(cli_main, "ledger_changes", lambda _claims: lambda: found.version)
    return found


def test_monitor_without_a_cursor_prints_start_and_the_ledger_sequence(estate: Estate) -> None:
    last = estate.row("config set", {"key": "a"})
    result = estate.run("--once")
    assert result.exit_code == 0, result.output
    assert result.stdout == f"start\ncursor {last}\n"


def test_monitor_session_stores_after_and_scope_then_resumes_from_them(estate: Estate) -> None:
    first = estate.row("config set", {"key": "a"})
    result = estate.run("--once", "--session", "s1", "--after", "0", "--spec", "S1")
    assert result.exit_code == 0, result.output
    assert result.stdout == (
        f"config set config key=a\ncursor {first}\n"
        f"next: tm monitor --once --session s1 --after {first} --spec S1\n"
    )
    assert estate.stored("s1") == {"after": first, "specs": ["S1"], "due": []}

    second = estate.row("config unset", {"key": "a"})
    result = estate.run("--once", "--session", "s1")
    assert result.exit_code == 0, result.output
    assert estate.stored("s1") == {"after": second, "specs": ["S1"], "due": []}
    assert "config unset" in result.stdout


def test_monitor_once_skips_dropped_rows_then_gathers_the_settle_window(estate: Estate) -> None:
    estate.row("config set", {"key": "seen"})
    estate.clock.on_sleep = [
        lambda: estate.row("note add"),
        lambda: estate.row("config set", {"key": "b"}),
        lambda: estate.row("config set", {"key": "c"}),
    ]
    result = estate.run("--once", "--after", "1")
    assert result.exit_code == 0, result.output
    last = estate.claims.ops.ledger_repo.after(0)[-1].id
    assert result.stdout == (
        "config set config key=b\nconfig set config key=c\n"
        f"cursor {last}\nnext: tm monitor --once --after {last}\n"
    )
    assert estate.clock.slept[-1] == 2.0


def test_monitor_stream_prints_each_batch_and_stores_its_cursor(estate: Estate) -> None:
    estate.row("config set", {"key": "a"})
    ids: list[int] = []

    def more() -> None:
        ids.append(estate.row("config set", {"key": "b"}))

    def stop() -> None:
        raise KeyboardInterrupt

    estate.clock.on_sleep = [more, stop]
    result = estate.run("--session", "s2", "--after", "0")
    assert (
        result.stdout
        == f"config set config key=a\ncursor 1\nconfig set config key=b\ncursor {ids[0]}\n"
    )
    assert estate.stored("s2") == {"after": ids[0], "specs": None, "due": []}


def test_monitor_after_past_the_ledger_exits_2_and_stores_nothing(estate: Estate) -> None:
    last = estate.row("config set", {"key": "a"})
    result = estate.run("--session", "s3", "--after", str(last + 1))
    assert result.exit_code == 2
    assert not estate.cursor_file("s3").exists()


@pytest.mark.parametrize("name", ["..", ".x", "a/b"])
def test_monitor_session_name_outside_the_pattern_exits_2_and_touches_no_file(
    estate: Estate, name: str
) -> None:
    result = estate.run("--once", "--session", name)
    assert result.exit_code == 2
    assert not (estate.root / ".taskmanager" / "monitor").exists()


def test_monitor_failure_exits_1_with_the_reason_and_stores_nothing(
    estate: Estate, monkeypatch: pytest.MonkeyPatch
) -> None:
    estate.row("config set", {"key": "a"})

    def broken(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("ledger unreadable")

    monkeypatch.setattr(Monitor, "wait", broken)
    result = estate.run("--once", "--session", "s4", "--after", "0")
    assert result.exit_code == 1
    assert "ledger unreadable" in result.stderr
    assert not estate.cursor_file("s4").exists()
