import json
import tomllib
from pathlib import Path

import taskmanager

RELEASE = "0.3.6"
REPO = Path(__file__).resolve().parents[2]


def test_version_defined() -> None:
    assert hasattr(taskmanager, "__version__")
    assert isinstance(taskmanager.__version__, str)


def test_every_manifest_carries_the_release_version() -> None:
    def read_json(path: str) -> dict[str, object]:
        loaded: dict[str, object] = json.loads((REPO / path).read_text(encoding="utf-8"))
        return loaded

    marketplace = read_json(".claude-plugin/marketplace.json")["plugins"]
    assert isinstance(marketplace, list)
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    versions = {
        "pyproject": project["version"],
        "package": taskmanager.__version__,
        "plugin": read_json(".claude-plugin/plugin.json")["version"],
        "marketplace": [plugin["version"] for plugin in marketplace],
        "gemini": read_json("gemini-extension.json")["version"],
    }
    assert versions == {
        "pyproject": RELEASE,
        "package": RELEASE,
        "plugin": RELEASE,
        "marketplace": [RELEASE],
        "gemini": RELEASE,
    }


def test_every_console_script_enters_through_main() -> None:
    scripts = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "scripts"
    ]
    assert scripts == {
        "tm": "taskmanager.cli.main:main",
        "taskmanager": "taskmanager.cli.main:main",
    }
