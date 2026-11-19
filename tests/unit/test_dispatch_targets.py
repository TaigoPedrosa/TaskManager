from pathlib import Path

import pytest
from typer.testing import CliRunner

from taskmanager.cli.main import app
from taskmanager.engine.config import ConfigError, ConfigStore

runner = CliRunner()

REPO = Path(__file__).resolve().parents[2]
DISPATCHER_SKILL = REPO / "plugin/skills/dispatcher/SKILL.md"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    assert runner.invoke(app, ["init", "-C", str(tmp_path)]).exit_code == 0
    return tmp_path


def tm(root: Path, *args: str) -> tuple[int, str]:
    res = runner.invoke(app, [*args, "-C", str(root)])
    return res.exit_code, res.output


def test_config_list_shows_the_dispatch_defaults(root: Path) -> None:
    _, out = tm(root, "config", "list")
    assert "tick_min" not in out and "tick_max" not in out
    assert "dispatch.wave_size = 10  (default)" in out
    assert "dispatch.tick_budget = 40  (default)" in out


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("dispatch.wave_size", "5"),
        ("dispatch.tick_budget", "123"),
    ],
)
def test_each_dispatch_key_is_settable_and_sourced_like_any_other(
    root: Path, key: str, value: str
) -> None:
    assert tm(root, "config", "set", key, value)[0] == 0
    assert tm(root, "config", "get", key) == (0, f"{value}\n")
    assert ConfigStore(root).resolve(key).source == "config"


@pytest.mark.parametrize(
    "key",
    ["dispatch.wave_size", "dispatch.tick_budget"],
)
def test_a_non_positive_dispatch_value_is_refused_and_writes_nothing(root: Path, key: str) -> None:
    code, out = tm(root, "config", "set", key, "0")
    assert code == 1
    assert "Traceback" not in out
    assert len(out.strip().splitlines()) == 1
    assert not ConfigStore(root).path.exists()


def test_wave_size_above_tick_budget_is_refused_and_writes_nothing(root: Path) -> None:
    code, out = tm(root, "config", "set", "dispatch.wave_size", "50")
    assert code == 1
    assert len(out.strip().splitlines()) == 1
    assert "wave_size" in out and "tick_budget" in out
    assert not ConfigStore(root).path.exists()


def test_guide_dispatch_prints_the_default_effective_values_as_the_typical_target(
    root: Path,
) -> None:
    _, out = tm(root, "guide", "dispatch")
    assert "{{" not in out
    assert "typical target" in out.lower()
    assert "override" in out.lower()
    assert "tick_min" not in out and "tick_max" not in out
    assert "waves of at most 10 nodes" in out
    assert "at most 40 nodes dispatched per tick" in out


def test_changing_a_dispatch_key_changes_the_guide_text(root: Path) -> None:
    assert tm(root, "config", "set", "dispatch.tick_budget", "77")[0] == 0
    _, out = tm(root, "guide", "dispatch")
    assert "at most 77 nodes dispatched per tick" in out
    assert "at most 40 nodes dispatched per tick" not in out


def test_guide_describes_the_staggered_tick(root: Path) -> None:
    _, out = tm(root, "guide", "dispatch")
    for needle in ("wave_size", "tick_budget", "maxBatch", "exclude", "staggered"):
        assert needle in out, needle


def test_dispatcher_skill_describes_the_staggered_tick() -> None:
    text = DISPATCHER_SKILL.read_text(encoding="utf-8")
    for needle in ("wave_size", "tick_budget", "maxBatch", "exclude", "staggered"):
        assert needle in text, needle


def test_replace_that_would_break_the_wave_bound_is_refused_and_writes_nothing(root: Path) -> None:
    store = ConfigStore(root)
    with pytest.raises(ConfigError):
        store.replace({"dispatch": {"wave_size": 50, "tick_budget": 20}})
    assert not store.path.exists()
