import json
from pathlib import Path

import pytest
from typer.testing import CliRunner, Result

from taskmanager.cli.main import app

CODEX_JSON = json.dumps(
    {
        "models": [
            {"slug": "gpt-5", "visibility": "list"},
            {"slug": "internal-x", "visibility": "hide"},
            {"slug": "gpt-5-mini", "visibility": "list"},
        ]
    }
)
AGY_OUT = "Fetching available models...\\ngemini-3\\tGemini 3\\nclaude-x\\tClaude X"


@pytest.fixture
def estate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "estate"
    root.mkdir()
    monkeypatch.setenv("TM_ROOT", str(root))
    res = CliRunner().invoke(app, ["init", "--yes", "--path", str(root)])
    assert res.exit_code == 0, res.output
    return root


@pytest.fixture
def bin_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "bin"
    path.mkdir()
    monkeypatch.setenv("PATH", str(path))
    return path


def stub(bin_dir: Path, name: str, script: str) -> None:
    path = bin_dir / name
    path.write_text(f"#!/bin/sh\n{script}\n", encoding="utf-8")
    path.chmod(0o755)


def run(*args: str) -> Result:
    return CliRunner().invoke(app, ["harness", *args])


def test_harness_list_prints_a_row_per_known_harness_with_cli_and_tier_models(
    estate: Path, bin_dir: Path
) -> None:
    stub(bin_dir, "codex", "exit 0")
    res = run("list")
    assert res.exit_code == 0, res.output
    lines = res.output.splitlines()
    assert [ln.split(":")[0] for ln in lines] == [
        "claude-code",
        "codex",
        "cursor",
        "copilot",
        "antigravity",
        "kiro",
        "gemini",
    ]
    assert lines[0] == "claude-code: cli missing; light=haiku; standard=sonnet; strong=opus"
    assert lines[1] == (
        "codex: cli found; light=harness default; standard=harness default; strong=harness default"
    )


def test_harness_list_shows_a_configured_model_and_a_configured_unknown_name(
    estate: Path, bin_dir: Path
) -> None:
    for key, value in (
        ("harnesses.codex.models.light", "gpt-5-mini"),
        ("harnesses.claude-code.models.strong", "fable"),
        ("harnesses.acme.models.standard", "acme-1"),
    ):
        res = CliRunner().invoke(app, ["config", "set", key, value])
        assert res.exit_code == 0, res.output
    lines = run("list").output.splitlines()
    assert "strong=fable" in lines[0]
    assert lines[1].startswith("codex: cli missing; light=gpt-5-mini;")
    assert lines[-1] == (
        "acme: cli -; light=harness default; standard=acme-1; strong=harness default"
    )


def test_harness_models_claude_code_lists_its_aliases_without_a_process(
    estate: Path, bin_dir: Path
) -> None:
    res = run("models", "claude-code")
    assert res.exit_code == 0, res.output
    assert res.output.splitlines() == [
        "haiku",
        "sonnet",
        "opus",
        "fable",
        "next: tm config set harnesses.claude-code.models.<tier> <model>",
    ]


def test_harness_models_codex_lists_only_visible_slugs(estate: Path, bin_dir: Path) -> None:
    stub(
        bin_dir,
        "codex",
        f"[ \"$1 $2\" = \"debug models\" ] || exit 9\nprintf '%s' '{CODEX_JSON}'",
    )
    res = run("models", "codex")
    assert res.exit_code == 0, res.output
    assert res.output.splitlines() == [
        "gpt-5",
        "gpt-5-mini",
        "next: tm config set harnesses.codex.models.<tier> <model>",
    ]


def test_harness_models_antigravity_prints_each_id(estate: Path, bin_dir: Path) -> None:
    stub(bin_dir, "agy", f'[ "$1" = models ] || exit 9\nprintf "{AGY_OUT}\\n"')
    res = run("models", "antigravity")
    assert res.exit_code == 0, res.output
    assert res.output.splitlines()[:2] == ["gemini-3", "claude-x"]


@pytest.mark.parametrize(
    ("name", "binary", "args"),
    [("cursor", "cursor-agent", "models"), ("kiro", "kiro-cli", "chat --list-models")],
)
def test_harness_models_runs_the_listing_command_of_cursor_and_kiro(
    estate: Path, bin_dir: Path, name: str, binary: str, args: str
) -> None:
    stub(bin_dir, binary, f'[ "$*" = "{args}" ] || exit 9\necho m1\necho m2')
    res = run("models", name)
    assert res.exit_code == 0, res.output
    assert res.output.splitlines()[:2] == ["m1", "m2"]


def test_harness_models_with_a_missing_cli_exits_1(estate: Path, bin_dir: Path) -> None:
    res = run("models", "codex")
    assert res.exit_code == 1
    assert "codex is not on PATH" in res.output


def test_harness_models_with_a_failing_cli_exits_1_with_its_message_and_writes_nothing(
    estate: Path, bin_dir: Path
) -> None:
    stub(bin_dir, "cursor-agent", "echo 'not logged in' >&2; exit 3")
    before = (estate / ".taskmanager" / "config.yaml").read_bytes()
    res = run("models", "cursor")
    assert res.exit_code == 1
    assert "not logged in" in res.output
    assert (estate / ".taskmanager" / "config.yaml").read_bytes() == before


def test_harness_models_with_unreadable_codex_output_exits_1(estate: Path, bin_dir: Path) -> None:
    stub(bin_dir, "codex", "echo nope")
    assert run("models", "codex").exit_code == 1


@pytest.mark.parametrize(
    ("name", "said"),
    [("copilot", "copilot --model auto"), ("gemini", "gemini -m <model>")],
)
def test_harness_models_of_a_cli_with_no_listing_exits_0_and_says_so(
    estate: Path, bin_dir: Path, name: str, said: str
) -> None:
    res = run("models", name)
    assert res.exit_code == 0, res.output
    assert said in res.output


def test_harness_models_of_an_unknown_name_exits_1(estate: Path, bin_dir: Path) -> None:
    res = run("models", "nope")
    assert res.exit_code == 1
    assert "unknown harness nope" in res.output


def test_harness_models_with_a_failing_cli_that_prints_to_stdout_shows_that_message(
    estate: Path, bin_dir: Path
) -> None:
    stub(bin_dir, "kiro-cli", "echo 'login first'; echo second; exit 2")
    res = run("models", "kiro")
    assert res.exit_code == 1
    assert "login first" in res.output
    assert "second" not in res.output


def test_harness_models_with_a_silent_failing_cli_names_its_exit_code(
    estate: Path, bin_dir: Path
) -> None:
    stub(bin_dir, "kiro-cli", "exit 4")
    res = run("models", "kiro")
    assert res.exit_code == 1
    assert "kiro-cli exited 4" in res.output


def test_harness_models_with_a_cli_that_cannot_start_exits_1(estate: Path, bin_dir: Path) -> None:
    path = bin_dir / "cursor-agent"
    path.write_text("#!/nonexistent-interpreter\n", encoding="utf-8")
    path.chmod(0o755)
    res = run("models", "cursor")
    assert res.exit_code == 1
    assert "cursor-agent models" in res.output


def test_harness_models_skips_blank_lines_of_a_plain_listing(estate: Path, bin_dir: Path) -> None:
    stub(bin_dir, "cursor-agent", "printf 'm1 - First\\n\\n   \\nm2\\n'")
    res = run("models", "cursor")
    assert res.output.splitlines()[:3] == [
        "m1",
        "m2",
        "next: tm config set harnesses.cursor.models.<tier> <model>",
    ]


def test_harness_models_of_codex_with_no_models_key_or_no_visibility_lists_nothing(
    estate: Path, bin_dir: Path
) -> None:
    stub(bin_dir, "codex", "echo '{}'")
    res = run("models", "codex")
    assert res.exit_code == 1
    assert "printed no model list" in res.output
    stub(bin_dir, "codex", """echo '{"models": [{"slug": "a"}]}'""")
    res = run("models", "codex")
    assert res.exit_code == 0
    assert res.output.splitlines() == ["next: tm config set harnesses.codex.models.<tier> <model>"]
