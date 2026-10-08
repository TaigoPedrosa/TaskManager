import sys
from pathlib import Path

import pytest

from taskmanager.cli.main import main

REPO = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("command", ["install", "plugin"])
def test_console_entry_with_an_installer_command_refuses_it_as_unknown(
    command: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["tm", command])

    with pytest.raises(SystemExit) as exited:
        main()

    assert exited.value.code == 2
    assert f"No such command '{command}'." in capsys.readouterr().err


def test_repository_ships_no_install_script() -> None:
    assert not (REPO / "install.sh").exists()
