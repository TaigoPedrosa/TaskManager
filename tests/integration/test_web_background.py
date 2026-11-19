import json
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import typer

from taskmanager.cli import main as cli
from taskmanager.db.connection import DatabaseManager

TM = [sys.executable, "-c", "from taskmanager.cli.main import main; main()"]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _tm(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*TM, *args],
        cwd=root,
        env={k: v for k, v in os.environ.items() if k != "TM_ROOT"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.fixture
def estate(tmp_path: Path) -> Iterator[Path]:
    DatabaseManager(tmp_path / ".taskmanager").init_all()
    yield tmp_path
    record = tmp_path / ".taskmanager" / "web.json"
    if record.exists():
        pid = json.loads(record.read_text())["pid"]
        if _alive(pid):
            os.kill(pid, signal.SIGKILL)


class FakeClock:
    def __init__(self) -> None:
        self._now = datetime(2026, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def sleep(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
        time.sleep(0.02)


def test_bg_serves_detached_and_stop_ends_it(estate: Path) -> None:
    port = _free_port()
    first = _tm(estate, "web", "--bg", "--no-open", "--port", str(port))
    assert first.returncode == 0, first.stderr
    record = json.loads((estate / ".taskmanager" / "web.json").read_text())
    assert record["url"] == f"http://127.0.0.1:{port}"
    assert record["url"] in first.stdout
    assert f"pid {record['pid']}" in first.stdout
    assert str(estate / ".taskmanager" / "web.log") in first.stdout
    assert httpx.get(record["url"] + "/", timeout=5).status_code == 200
    assert os.getsid(record["pid"]) == record["pid"]

    again = _tm(estate, "web", "--bg", "--no-open", "--port", str(port))
    assert again.returncode == 0, again.stderr
    assert f"pid {record['pid']}" in again.stdout
    assert record["url"] in again.stdout
    assert json.loads((estate / ".taskmanager" / "web.json").read_text()) == record

    stopped = _tm(estate, "web", "stop")
    assert stopped.returncode == 0, stopped.stderr
    assert str(record["pid"]) in stopped.stdout
    assert not (estate / ".taskmanager" / "web.json").exists()
    assert not _alive(record["pid"])


def test_bg_replaces_a_record_naming_a_dead_pid(estate: Path) -> None:
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    record_path = estate / ".taskmanager" / "web.json"
    record_path.write_text(json.dumps({"pid": dead.pid, "url": "http://127.0.0.1:1"}))
    port = _free_port()
    assert _tm(estate, "web", "--bg", "--no-open", "--port", str(port)).returncode == 0
    assert json.loads(record_path.read_text())["pid"] != dead.pid
    assert _tm(estate, "web", "stop").returncode == 0


def test_stop_leaves_a_process_that_reused_the_recorded_pid(estate: Path) -> None:
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        record_path = estate / ".taskmanager" / "web.json"
        record_path.write_text(
            json.dumps({"pid": other.pid, "url": "http://127.0.0.1:1", "started": "0"})
        )
        stopped = _tm(estate, "web", "stop")
        assert stopped.returncode == 0, stopped.stderr
        assert "No background web server is running" in stopped.stdout
        assert other.poll() is None
        assert not record_path.exists()
    finally:
        other.kill()
        other.wait()


def test_bg_refusals_start_nothing(estate: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
    exposed = _tm(estate, "web", "--bg", "--host", "0.0.0.0", "--no-open")
    assert exposed.returncode == 1
    assert "--expose" in exposed.stderr
    bare = tmp_path_factory.mktemp("bare")
    (bare / ".taskmanager").mkdir()
    uninitialized = _tm(bare, "web", "--bg", "--no-open")
    assert uninitialized.returncode == 1
    assert "not initialized" in uninitialized.stderr
    for root in (estate, bare):
        assert not (root / ".taskmanager" / "web.json").exists()
        assert not (root / ".taskmanager" / "web.log").exists()


def test_bg_child_that_exits_early_fails_with_the_log_tail(estate: Path) -> None:
    command = [sys.executable, "-c", "print('boom'); raise SystemExit(3)"]
    with pytest.raises(typer.Exit) as exit_info:
        cli._start_web_background(estate, command, FakeClock())
    assert exit_info.value.exit_code == 1
    assert "boom" in (estate / ".taskmanager" / "web.log").read_text()


def test_bg_child_that_is_never_ready_is_stopped_on_the_injected_clock(estate: Path) -> None:
    command = [sys.executable, "-c", "import time; print('waiting', flush=True); time.sleep(60)"]
    clock = FakeClock()
    start = clock.now()
    with pytest.raises(typer.Exit) as exit_info:
        cli._start_web_background(estate, command, clock)
    assert exit_info.value.exit_code == 1
    assert clock.now() - start >= timedelta(seconds=10)


def test_stop_without_a_live_server_removes_a_stale_record(estate: Path) -> None:
    assert "No background web server is running" in _tm(estate, "web", "stop").stdout
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    record = estate / ".taskmanager" / "web.json"
    record.write_text(json.dumps({"pid": dead.pid, "url": "http://127.0.0.1:1"}))
    result = _tm(estate, "web", "stop")
    assert result.returncode == 0
    assert not record.exists()


def test_stop_reports_a_process_that_survives_sigterm(
    estate: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stubborn = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "print('up', flush=True); time.sleep(60)"
            ),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert stubborn.stdout is not None
        stubborn.stdout.readline()
        record = estate / ".taskmanager" / "web.json"
        record.write_text(json.dumps({"pid": stubborn.pid, "url": "http://127.0.0.1:1"}))
        monkeypatch.setattr(cli, "_WEB_STOP_SECONDS", 0.5)
        with pytest.raises(typer.Exit) as exit_info:
            cli.web_stop(estate)
        assert exit_info.value.exit_code == 1
        assert record.exists()
    finally:
        stubborn.kill()
        stubborn.wait()


def test_foreground_exit_removes_only_its_own_record(estate: Path) -> None:
    port = _free_port()
    proc = subprocess.Popen(
        [*TM, "web", "--no-open", "--port", str(port), "-C", str(estate)],
        env={k: v for k, v in os.environ.items() if k != "TM_ROOT"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    record = estate / ".taskmanager" / "web.json"
    try:
        for _ in range(100):
            if record.exists() and _alive(proc.pid):
                try:
                    if httpx.get(f"http://127.0.0.1:{port}/", timeout=1).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
            time.sleep(0.1)
        assert json.loads(record.read_text())["pid"] == proc.pid
        record.write_text(json.dumps({"pid": os.getpid(), "url": "http://other"}))
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
        assert json.loads(record.read_text())["pid"] == os.getpid()
        record.unlink()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
