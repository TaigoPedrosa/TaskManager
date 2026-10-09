from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[2]
SCRATCH_VARIABLES = ["HOME", "UV_TOOL_DIR", "UV_TOOL_BIN_DIR", "UV_CACHE_DIR", "CLAUDE_CONFIG_DIR"]
LIFECYCLE = [
    './install.sh install --from "$GITHUB_WORKSPACE"',
    "./install.sh status",
    './install.sh install --from "$GITHUB_WORKSPACE"',
    "./install.sh uninstall",
    "if ./install.sh status; then",
]


def workflow() -> dict[Any, Any]:
    loaded = yaml.safe_load((REPO / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def runs(job: dict[str, Any]) -> list[str]:
    return [step["run"] for step in job["steps"] if "run" in step]


def test_ci_has_an_install_job_that_runs_install_status_reinstall_uninstall() -> None:
    job = workflow()["jobs"]["install"]
    scripts = runs(job)

    lifecycle = [
        line for script in scripts for line in script.splitlines() if "./install.sh" in line
    ]
    assert [line.strip() for line in lifecycle] == LIFECYCLE
    scratch = next(script for script in scripts if "GITHUB_ENV" in script)
    assert [v for v in SCRATCH_VARIABLES if f'echo "{v}=$scratch/' not in scratch] == []
    assert scripts.index(scratch) < next(i for i, s in enumerate(scripts) if "./install.sh" in s)


def test_ci_install_job_fails_when_status_succeeds_after_uninstall() -> None:
    last = [s for s in runs(workflow()["jobs"]["install"]) if "./install.sh" in s][-1]

    assert last.splitlines()[0] == "if ./install.sh status; then"
    assert "exit 1" in last


def test_ci_install_job_checks_tm_version_against_the_marketplace_right_after_install() -> None:
    scripts = runs(workflow()["jobs"]["install"])
    check = next(i for i, s in enumerate(scripts) if "tm --version" in s)

    assert "./install.sh install" in scripts[check - 1]
    assert ".claude-plugin/marketplace.json" in scripts[check]
    assert "exit 1" in scripts[check]


def test_ci_install_job_runs_on_every_push_and_pull_request_apart_from_the_gates() -> None:
    flow = workflow()
    # PyYAML reads the bare key `on` as the boolean true.
    triggers = flow[True]

    assert "push" in triggers
    assert "pull_request" in triggers
    assert "needs" not in flow["jobs"]["install"]
    assert not any("./install.sh" in s for s in runs(flow["jobs"]["gates"]))
