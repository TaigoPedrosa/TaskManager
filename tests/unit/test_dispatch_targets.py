from pathlib import Path

import pytest
from typer.testing import CliRunner

from taskmanager.cli.main import app
from taskmanager.engine.config import ConfigStore

runner = CliRunner()

REPO = Path(__file__).resolve().parents[2]
SKILL_COPIES = (
    REPO / "skills/dispatcher/SKILL.md",
    REPO / "src/taskmanager/skills/dispatcher/SKILL.md",
)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    assert runner.invoke(app, ["init", "-C", str(tmp_path)]).exit_code == 0
    return tmp_path


def tm(root: Path, *args: str) -> tuple[int, str]:
    res = runner.invoke(app, [*args, "-C", str(root)])
    return res.exit_code, res.stdout


def test_config_list_shows_the_dispatch_defaults(root: Path) -> None:
    _, out = tm(root, "config", "list")
    assert "dispatch.tick_min = 300  (default)" in out
    assert "dispatch.tick_max = 900  (default)" in out
    assert "dispatch.wave_size = 10  (default)" in out
    assert "dispatch.tick_budget = 40  (default)" in out


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("dispatch.tick_min", "123"),
        ("dispatch.tick_max", "1200"),
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
    ["dispatch.tick_min", "dispatch.tick_max", "dispatch.wave_size", "dispatch.tick_budget"],
)
def test_a_non_positive_dispatch_value_is_refused_and_writes_nothing(root: Path, key: str) -> None:
    code, out = tm(root, "config", "set", key, "0")
    assert code == 1
    assert "Traceback" not in out
    assert len(out.strip().splitlines()) == 1
    assert not ConfigStore(root).path.exists()


def test_tick_min_above_tick_max_is_refused_and_writes_nothing(root: Path) -> None:
    code, out = tm(root, "config", "set", "dispatch.tick_min", "1000")
    assert code == 1
    assert len(out.strip().splitlines()) == 1
    assert "tick_min" in out and "tick_max" in out
    assert not ConfigStore(root).path.exists()


def test_wave_size_above_tick_budget_is_refused_and_writes_nothing(root: Path) -> None:
    code, out = tm(root, "config", "set", "dispatch.wave_size", "50")
    assert code == 1
    assert len(out.strip().splitlines()) == 1
    assert "wave_size" in out and "tick_budget" in out
    assert not ConfigStore(root).path.exists()


def test_a_valid_pair_can_be_set_in_either_order(root: Path) -> None:
    # Each side is within the other's default, so whichever is set first still passes.
    assert tm(root, "config", "set", "dispatch.tick_max", "800")[0] == 0
    assert tm(root, "config", "set", "dispatch.tick_min", "400")[0] == 0
    assert tm(root, "config", "get", "dispatch.tick_min") == (0, "400\n")
    assert tm(root, "config", "get", "dispatch.tick_max") == (0, "800\n")


def test_raising_the_low_value_past_an_already_stored_high_one_stays_refused(root: Path) -> None:
    assert tm(root, "config", "set", "dispatch.tick_max", "400")[0] == 0
    code, out = tm(root, "config", "set", "dispatch.tick_min", "500")
    assert code == 1
    assert "tick_min" in out and "tick_max" in out
    assert tm(root, "config", "get", "dispatch.tick_min") == (0, "300\n")


def test_lowering_the_high_value_below_an_already_stored_low_one_stays_refused(root: Path) -> None:
    assert tm(root, "config", "set", "dispatch.tick_min", "500")[0] == 0
    code, out = tm(root, "config", "set", "dispatch.tick_max", "400")
    assert code == 1
    assert "tick_min" in out and "tick_max" in out
    assert tm(root, "config", "get", "dispatch.tick_max") == (0, "900\n")


def test_guide_dispatch_prints_the_default_effective_values_as_the_typical_target(
    root: Path,
) -> None:
    _, out = tm(root, "guide", "dispatch")
    assert "{{" not in out
    assert "typical target" in out.lower()
    assert "override" in out.lower()
    assert "wakeup every 300–900 s" in out
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


@pytest.mark.parametrize("path", SKILL_COPIES, ids=[str(p) for p in SKILL_COPIES])
def test_dispatcher_skill_copies_describe_the_staggered_tick(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    for needle in ("wave_size", "tick_budget", "maxBatch", "exclude", "staggered"):
        assert needle in text, needle


def test_dispatcher_skill_copies_stay_identical() -> None:
    texts = {p.read_text(encoding="utf-8") for p in SKILL_COPIES}
    assert len(texts) == 1
