import sys

import pytest

from taskmanager.cli.main import main


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
