import re
from pathlib import Path


def _parse_frontmatter(text: str) -> dict[str, str]:
    match = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    if not match:
        return {}
    frontmatter: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, val = line.split(":", 1)
            frontmatter[key.strip()] = val.strip().strip("\"'")
    return frontmatter


def test_bundled_skills_exist() -> None:
    skill_dir = Path("src/taskmanager/skills")
    tm_skill = skill_dir / "taskmanager/SKILL.md"
    dispatcher_skill = skill_dir / "dispatcher/SKILL.md"

    assert tm_skill.exists()
    assert dispatcher_skill.exists()

    tm_text = tm_skill.read_text(encoding="utf-8")
    tm_fm = _parse_frontmatter(tm_text)
    assert tm_fm.get("name") == "taskmanager"
    assert (
        tm_fm.get("description")
        == "Use when executing, tracking, or updating tasks and plans via the taskmanager CLI"
    )

    dispatcher_text = dispatcher_skill.read_text(encoding="utf-8")
    dispatcher_fm = _parse_frontmatter(dispatcher_text)
    assert dispatcher_fm.get("name") == "dispatcher"
    assert (
        dispatcher_fm.get("description")
        == "Use when planning waves, routing tasks, balancing concurrency, and dispatching subagents"
    )


def test_taskmanager_skill_cli_instructions() -> None:
    tm_skill = Path("src/taskmanager/skills/taskmanager/SKILL.md")
    assert tm_skill.exists()
    content = tm_skill.read_text(encoding="utf-8")

    assert "tm render" in content
    assert "--view subagent" in content
    assert "tm run start" in content
    assert "--worktree" in content
    assert "tm run heartbeat" in content
    assert "tm verify run" in content
    assert "tm run stop" in content
    assert "WAITING_REVIEW" in content
    assert "AUTH-USER-LOGIN:steps" in content


def test_dispatcher_skill_cli_instructions() -> None:
    dispatcher_skill = Path("src/taskmanager/skills/dispatcher/SKILL.md")
    assert dispatcher_skill.exists()
    content = dispatcher_skill.read_text(encoding="utf-8")

    assert "disjoint" in content.lower()
    assert "tm next" in content
    assert "--strategy balanced" in content
    assert "acceptable_models" in content
    assert "review" in content.lower()
    assert "supersede" in content.lower()
