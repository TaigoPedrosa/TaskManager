import json
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from taskmanager.engine.config import ProjectConfig
from taskmanager.engine.routing import TIERS, resolve

UNMAPPED: Final = "harness default"
LISTING_TIMEOUT: Final = 30


class HarnessError(Exception):
    pass


def _codex_slugs(out: str) -> list[str]:
    try:
        listed = json.loads(out)["models"]
        return [m["slug"] for m in listed if m.get("visibility") == "list"]
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise HarnessError("codex debug models printed no model list") from exc


def _tabbed_ids(out: str) -> list[str]:
    # `agy models` opens with a progress line that carries no tab.
    return [line.split("\t")[0] for line in out.splitlines() if "\t" in line]


def _first_words(out: str) -> list[str]:
    return [line.split()[0] for line in out.splitlines() if line.strip()]


@dataclass(frozen=True)
class Harness:
    binary: str
    argv: tuple[str, ...] = ()
    parse: Callable[[str], list[str]] | None = None
    static: tuple[str, ...] = ()
    no_listing: str = ""


# One row per harness tm knows: the CLI it runs on and how that CLI lists its models.
HARNESSES: Final = {
    "claude-code": Harness("claude", static=("haiku", "sonnet", "opus", "fable")),
    "codex": Harness("codex", ("debug", "models"), _codex_slugs),
    "cursor": Harness("cursor-agent", ("models",), _first_words),
    "copilot": Harness(
        "copilot", no_listing="copilot lists no models; `copilot --model auto` lets Copilot choose"
    ),
    "antigravity": Harness("agy", ("models",), _tabbed_ids),
    "kiro": Harness("kiro-cli", ("chat", "--list-models"), _first_words),
    "gemini": Harness(
        "gemini", no_listing="gemini lists no models; pass one with `gemini -m <model>`"
    ),
}


def on_path(name: str) -> bool:
    return shutil.which(HARNESSES[name].binary) is not None


def rows(config: ProjectConfig) -> list[list[str]]:
    """One row per known or configured harness: its name, whether its CLI is on PATH, and the
    model each tier runs on."""
    names = [*HARNESSES, *(n for n in config.harnesses if n not in HARNESSES)]
    return [
        [
            name,
            ("found" if on_path(name) else "missing") if name in HARNESSES else "-",
            *(resolve(tier, name, config) or UNMAPPED for tier in TIERS),
        ]
        for name in names
    ]


def known(name: str) -> Harness:
    harness = HARNESSES.get(name)
    if harness is None:
        raise HarnessError(f"unknown harness {name}; known: {', '.join(HARNESSES)}")
    return harness


def models(harness: Harness) -> list[str]:
    """The models the harness's own CLI offers; HarnessError when it cannot say."""
    if harness.parse is None:
        return list(harness.static)
    path = shutil.which(harness.binary)
    if path is None:
        raise HarnessError(f"{harness.binary} is not on PATH")
    try:
        res = subprocess.run(
            [path, *harness.argv],
            capture_output=True,
            text=True,
            check=False,
            timeout=LISTING_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise HarnessError(f"{harness.binary} {' '.join(harness.argv)}: {exc}") from exc
    if res.returncode != 0:
        said = (res.stderr.strip() or res.stdout.strip()).splitlines()
        raise HarnessError(said[0] if said else f"{harness.binary} exited {res.returncode}")
    return harness.parse(res.stdout)
