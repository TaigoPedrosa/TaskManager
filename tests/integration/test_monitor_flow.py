import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from taskmanager.cli.main import _claims
from taskmanager.core.models import LedgerEvent

TM = [sys.executable, "-m", "taskmanager.cli.main"]
HANG_GUARD_SECONDS = 60


def tm(
    root: Path, *args: str, timeout: int = HANG_GUARD_SECONDS
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*TM, *args, "-C", str(root)],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def complete_row(root: Path, node: str) -> int:
    event = LedgerEvent(
        actor_id="agent",
        command="task complete",
        target_id=node,
        payload={},
        timestamp=datetime.now(UTC),
    )
    _claims(root).ops.ledger_repo.append(event)
    assert event.id is not None
    return event.id


def cursor_file(root: Path, session: str) -> Path:
    return root / ".taskmanager" / "monitor" / f"{session}.json"


@pytest.fixture(name="root")
def root_fixture(tmp_path: Path) -> Path:
    res = tm(tmp_path, "init")
    assert res.returncode == 0, res.stderr
    started = tm(tmp_path, "monitor", "--once", "--session", "S")
    assert started.returncode == 0, started.stderr
    assert started.stdout.startswith("start\ncursor ")
    return tmp_path


def test_once_subprocess_prints_the_row_the_cursor_and_the_next_command(root: Path) -> None:
    row = complete_row(root, "S1-P1-a")
    res = tm(root, "monitor", "--once", "--session", "S")
    assert res.returncode == 0, res.stderr
    assert res.stdout == (
        f"task complete S1-P1-a\ncursor {row}\nnext: tm monitor --once --session S --after {row}\n"
    )


def test_a_session_resumes_the_batch_a_dying_process_printed_and_next_reads_only_later_rows(
    root: Path,
) -> None:
    first = complete_row(root, "S1-P1-a")
    # A directory where the cursor's temp file goes makes the store fail after the batch is
    # printed: the process dies between printing and persisting.
    blocker = cursor_file(root, "S").with_suffix(".tmp")
    blocker.mkdir()
    died = tm(root, "monitor", "--once", "--session", "S")
    assert died.returncode == 1
    assert f"task complete S1-P1-a\ncursor {first}\n" in died.stdout
    blocker.rmdir()

    again = tm(root, "monitor", "--once", "--session", "S")
    assert again.returncode == 0, again.stderr
    assert again.stdout == died.stdout

    second = complete_row(root, "S1-P1-b")
    next_line = again.stdout.splitlines()[-1].removeprefix("next: tm ")
    resumed = tm(root, *next_line.split())
    assert resumed.returncode == 0, resumed.stderr
    assert resumed.stdout == (
        f"task complete S1-P1-b\ncursor {second}\nnext: tm monitor --once --session S --after {second}\n"
    )


def test_stream_subprocess_stopped_after_a_batch_leaves_the_batch_cursor_stored(
    root: Path,
) -> None:
    row = complete_row(root, "S1-P1-a")
    proc = subprocess.Popen(
        [*TM, "monitor", "--session", "S", "-C", str(root)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        lines = [proc.stdout.readline(), proc.stdout.readline()]
        assert lines == ["task complete S1-P1-a\n", f"cursor {row}\n"]
        deadline = time.monotonic() + HANG_GUARD_SECONDS
        stored = None
        while time.monotonic() < deadline:
            stored = json.loads(cursor_file(root, "S").read_text())["after"]
            if stored == row:
                break
            time.sleep(0.05)
    finally:
        proc.terminate()
        proc.communicate(timeout=HANG_GUARD_SECONDS)
    assert stored == row
