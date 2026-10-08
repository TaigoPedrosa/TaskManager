import importlib.metadata
import json
import re
import subprocess
import tomllib
import zipfile
from importlib.resources import files
from pathlib import Path

import taskmanager

RELEASE = "0.3.7"
REPO = Path(__file__).resolve().parents[2]


def read_json(path: str) -> dict[str, object]:
    loaded: dict[str, object] = json.loads((REPO / path).read_text(encoding="utf-8"))
    return loaded


def test_version_defined() -> None:
    assert hasattr(taskmanager, "__version__")
    assert isinstance(taskmanager.__version__, str)


def test_every_manifest_carries_the_release_version() -> None:
    marketplace = read_json(".claude-plugin/marketplace.json")["plugins"]
    assert isinstance(marketplace, list)
    project = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    versions = {
        "pyproject": project["version"],
        "package": taskmanager.__version__,
        "plugin": read_json("plugin/.claude-plugin/plugin.json")["version"],
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


def test_gemini_manifest_loads_a_context_file_naming_only_shipped_guides() -> None:
    context = REPO / str(read_json("gemini-extension.json")["contextFileName"])
    named = set(re.findall(r"`tm guide (\w+)`", context.read_text(encoding="utf-8")))
    shipped = {
        entry.name.removesuffix(".md")
        for entry in files("taskmanager").joinpath("guides").iterdir()
    }
    assert named
    assert named <= shipped


def test_every_console_script_enters_through_main() -> None:
    scripts = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "scripts"
    ]
    assert scripts == {
        "tm": "taskmanager.cli.main:main",
        "taskmanager": "taskmanager.cli.main:main",
    }


def test_license_file_is_mit() -> None:
    assert (REPO / "LICENSE").read_text(encoding="utf-8").startswith("MIT License\n")


def test_installed_metadata_declares_mit() -> None:
    metadata = importlib.metadata.metadata("taskmanager")
    assert metadata["License-Expression"] == "MIT"
    assert metadata.get_all("License-File") == ["LICENSE"]


def test_every_plugin_manifest_declares_mit() -> None:
    marketplace = read_json(".claude-plugin/marketplace.json")["plugins"]
    assert isinstance(marketplace, list)
    licenses = {
        "plugin": read_json("plugin/.claude-plugin/plugin.json")["license"],
        "marketplace": [plugin["license"] for plugin in marketplace],
    }
    assert licenses == {"plugin": "MIT", "marketplace": ["MIT"]}


def test_wheel_ships_the_license(tmp_path: Path) -> None:
    build = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path), str(REPO)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert build.returncode == 0, build.stderr
    (wheel,) = tmp_path.glob("*.whl")
    with zipfile.ZipFile(wheel) as archive:
        dist_info = f"taskmanager-{RELEASE}.dist-info"
        names = archive.namelist()
        metadata = archive.read(f"{dist_info}/METADATA").decode()
    assert f"{dist_info}/licenses/LICENSE" in names
    assert "License-Expression: MIT\n" in metadata
