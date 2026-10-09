"""install.sh runs only against a scratch HOME, tool dir and Claude profile, with a stub `claude`
that keeps its marketplaces and plugins as files under the profile."""

import hashlib
import os
import shutil
import signal
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "install.sh"
VERSION = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["version"]
PLUGIN = "taskmanager@taskmanager"

STUB_CLAUDE = """#!{python}
import json, os, sys
from pathlib import Path

args = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as log:
    log.write(" ".join(args) + "\\n")
root = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "stub"
markets, plugins = root / "marketplaces", root / "plugins"
json_out = "--json" in args
args = [a for a in args if a != "--json"]

def version(source):
    pinned = os.environ.get("STUB_PLUGIN_VERSION")
    if pinned:
        return pinned
    manifest = Path(source) / "plugin" / ".claude-plugin" / "plugin.json"
    return json.loads(manifest.read_text())["version"]

match args:
    case ["plugin", "marketplace", "add", source]:
        (markets / "taskmanager").write_text(source)
    case ["plugin", "marketplace", "list"]:
        print(json.dumps([{{"name": p.name}} for p in markets.iterdir()], indent=2))
    case ["plugin", "marketplace", "update", name]:
        sys.exit(0 if (markets / name).exists() else 1)
    case ["plugin", "marketplace", "remove", name]:
        if not (markets / name).exists():
            sys.exit(1)
        (markets / name).unlink()
    case ["plugin", "install", plugin]:
        market = markets / plugin.split("@")[1]
        if not market.exists():
            sys.exit(1)
        (plugins / plugin).write_text(version(market.read_text()))
    case ["plugin", "update", plugin]:
        sys.exit(0 if (plugins / plugin).exists() else 1)
    case ["plugin", "list"]:
        rows = [{{"id": p.name, "version": p.read_text()}} for p in plugins.iterdir()]
        print(json.dumps(rows, indent=2))
    case ["plugin", "uninstall", plugin]:
        if not (plugins / plugin).exists():
            sys.exit(1)
        (plugins / plugin).unlink()
    case _:
        sys.exit(2)
"""


@dataclass(frozen=True)
class Scratch:
    home: Path
    profile: Path
    tool_bin: Path
    log: Path
    env: dict[str, str]

    @property
    def gemini_link(self) -> Path:
        return self.home / ".gemini" / "extensions" / "taskmanager"

    def plugins(self) -> dict[str, str]:
        return {p.name: p.read_text() for p in (self.profile / "stub" / "plugins").iterdir()}

    def marketplaces(self) -> dict[str, str]:
        return {p.name: p.read_text() for p in (self.profile / "stub" / "marketplaces").iterdir()}

    def tools(self) -> str:
        return run_checked(["uv", "tool", "list"], self.env).stdout


@pytest.fixture(scope="module")
def uv_cache(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("uv-cache")


def run(argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    proc = subprocess.Popen(
        argv,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=600)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        raise
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def run_checked(argv: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    res = run(argv, env)
    assert res.returncode == 0, res.stdout + res.stderr
    return res


def install_sh(scratch: Scratch, *args: str) -> subprocess.CompletedProcess[str]:
    return run(["bash", str(SCRIPT), *args], scratch.env)


def scratch_env(tmp_path: Path, uv_cache: Path, *, claude: bool = True, uv: bool = True) -> Scratch:
    home = tmp_path / "home"
    profile = home / "claude-profile"
    for d in (profile / "stub" / "marketplaces", profile / "stub" / "plugins"):
        d.mkdir(parents=True)
    tool_bin = tmp_path / "tool-bin"
    sysbin = tmp_path / "sysbin"
    sysbin.mkdir()
    real_uv, real_git = shutil.which("uv"), shutil.which("git")
    assert real_uv is not None
    assert real_git is not None
    (sysbin / "git").symlink_to(real_git)
    if uv:
        (sysbin / "uv").symlink_to(real_uv)
    path = [str(sysbin), str(tool_bin), "/usr/bin", "/bin"]
    if claude:
        stub_bin = tmp_path / "stub-bin"
        stub_bin.mkdir()
        stub = stub_bin / "claude"
        stub.write_text(STUB_CLAUDE.format(python=sys.executable))
        stub.chmod(0o755)
        path.insert(0, str(stub_bin))
    xdg = tmp_path / "xdg"
    env = {
        "HOME": str(home),
        "PATH": os.pathsep.join(path),
        "CLAUDE_CONFIG_DIR": str(profile),
        "UV_TOOL_DIR": str(tmp_path / "tools"),
        "UV_TOOL_BIN_DIR": str(tool_bin),
        "UV_CACHE_DIR": str(uv_cache),
        "UV_PYTHON": sys.executable,
        "UV_PYTHON_DOWNLOADS": "never",
        "XDG_CONFIG_HOME": str(xdg / "config"),
        "XDG_DATA_HOME": str(xdg / "data"),
        "XDG_CACHE_HOME": str(xdg / "cache"),
        "XDG_STATE_HOME": str(xdg / "state"),
        "XDG_BIN_HOME": str(xdg / "bin"),
        "STUB_LOG": str(tmp_path / "claude-calls.log"),
        "TM_ROOT": os.environ["TM_ROOT"],
        "NO_COLOR": "1",
    }
    return Scratch(home, profile, tool_bin, tmp_path / "claude-calls.log", env)


def snapshot(root: Path) -> dict[str, str]:
    seen: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if p.is_symlink():
            seen[str(p.relative_to(root))] = f"-> {os.readlink(p)}"
        elif p.is_file():
            seen[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
        else:
            seen[str(p.relative_to(root))] = "dir"
    return seen


def test_install_from_a_checkout_installs_tm_and_the_plugin_at_one_version(
    tmp_path: Path, uv_cache: Path
) -> None:
    scratch = scratch_env(tmp_path, uv_cache)

    res = install_sh(scratch, "install", "--from", str(REPO))

    assert res.returncode == 0, res.stdout + res.stderr
    tm = run_checked([str(scratch.tool_bin / "tm"), "--version"], scratch.env)
    assert tm.stdout.split()[-1] == VERSION
    calls = scratch.log.read_text().splitlines()
    assert f"plugin marketplace add {REPO}" in calls
    assert f"plugin install {PLUGIN}" in calls
    assert scratch.plugins() == {PLUGIN: VERSION}
    assert "gemini: skipped, ~/.gemini does not exist" in res.stdout
    status = install_sh(scratch, "status")
    assert status.returncode == 0, status.stdout + status.stderr
    assert f"{PLUGIN} {VERSION}" in status.stdout


def test_install_run_twice_leaves_the_same_state_and_exits_zero(
    tmp_path: Path, uv_cache: Path
) -> None:
    scratch = scratch_env(tmp_path, uv_cache)
    first = install_sh(scratch, "--from", str(REPO))
    assert first.returncode == 0, first.stdout + first.stderr
    state = (scratch.plugins(), scratch.marketplaces(), scratch.tools())

    again = install_sh(scratch, "--from", str(REPO))

    assert again.returncode == 0, again.stdout + again.stderr
    assert (scratch.plugins(), scratch.marketplaces(), scratch.tools()) == state
    assert scratch.marketplaces() == {"taskmanager": str(REPO)}


def test_uninstall_removes_tm_the_plugin_and_the_gemini_link_and_nothing_else(
    tmp_path: Path, uv_cache: Path
) -> None:
    scratch = scratch_env(tmp_path, uv_cache)
    (scratch.home / ".gemini").mkdir()
    before = snapshot(scratch.home)
    installed = install_sh(scratch, "--from", str(REPO))
    assert installed.returncode == 0, installed.stdout + installed.stderr
    assert scratch.gemini_link.resolve() == REPO
    assert (scratch.gemini_link / "gemini-extension.json").is_file()

    first = install_sh(scratch, "uninstall")
    second = install_sh(scratch, "uninstall")

    assert first.returncode == 0, first.stdout + first.stderr
    assert second.returncode == 0, second.stdout + second.stderr
    assert not (scratch.tool_bin / "tm").exists()
    assert "taskmanager" not in scratch.tools()
    assert scratch.plugins() == {}
    assert scratch.marketplaces() == {}
    assert not scratch.gemini_link.is_symlink()
    assert snapshot(scratch.home) == before
    status = install_sh(scratch, "status")
    assert status.returncode != 0


def test_install_without_claude_installs_tm_and_prints_the_plugin_commands(
    tmp_path: Path, uv_cache: Path
) -> None:
    scratch = scratch_env(tmp_path, uv_cache, claude=False)

    res = install_sh(scratch, "--from", str(REPO))

    assert res.returncode == 0, res.stdout + res.stderr
    run_checked([str(scratch.tool_bin / "tm"), "--version"], scratch.env)
    assert f"claude plugin marketplace add {REPO}" in res.stdout
    assert f"claude plugin install {PLUGIN}" in res.stdout
    assert not scratch.log.exists()


def test_install_without_uv_exits_naming_uv(tmp_path: Path, uv_cache: Path) -> None:
    scratch = scratch_env(tmp_path, uv_cache, uv=False)
    assert run(["sh", "-c", "command -v uv"], scratch.env).returncode != 0

    res = install_sh(scratch, "--from", str(REPO))

    assert res.returncode != 0
    assert "uv is required" in res.stderr
    assert not scratch.log.exists()


def test_status_with_the_plugin_at_another_version_exits_naming_both(
    tmp_path: Path, uv_cache: Path
) -> None:
    scratch = scratch_env(tmp_path, uv_cache)
    scratch.env["STUB_PLUGIN_VERSION"] = "9.9.9"

    installed = install_sh(scratch, "--from", str(REPO))
    status = install_sh(scratch, "status")

    assert installed.returncode != 0
    assert f"tm {VERSION} differs from plugin 9.9.9" in installed.stderr
    assert status.returncode != 0
    assert f"tm {VERSION} differs from plugin 9.9.9" in status.stdout


def test_install_script_parses_and_passes_shellcheck() -> None:
    assert subprocess.run(["bash", "-n", str(SCRIPT)], check=False).returncode == 0
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        pytest.skip("shellcheck is not on PATH")
    res = subprocess.run([shellcheck, str(SCRIPT)], capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stdout
