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
    description = tm_fm.get("description", "")
    words = set(re.findall(r"[a-z]+", description.lower()))
    assert {"tm", "claim", "review", "merge", "guide"} <= words, (
        f"the description must name the CLI and the moments it triggers on: {description!r}"
    )

    dispatcher_text = dispatcher_skill.read_text(encoding="utf-8")
    dispatcher_fm = _parse_frontmatter(dispatcher_text)
    assert dispatcher_fm.get("name") == "dispatcher"
    dispatcher_description = dispatcher_fm.get("description", "").lower()
    for word in ("dispatch", "wave", "plan"):
        assert word in dispatcher_description, (
            f"the description must name the moments it triggers on: {dispatcher_description!r}"
        )


def test_taskmanager_skill_sends_the_agent_to_the_guides() -> None:
    """The procedure lives in `tm guide <topic>`; a copy of it in the skill is a second source."""
    tm_skill = Path("src/taskmanager/skills/taskmanager/SKILL.md")
    assert tm_skill.exists()
    content = tm_skill.read_text(encoding="utf-8")

    assert "tm guide" in content
    for topic in ("implement", "review", "fix", "merge"):
        assert f"tm guide {topic}" in content, f"the skill does not route {topic} to its guide"
    assert len(content.splitlines()) <= 60, "the skill is restating what the guides already print"


def test_the_bundled_skill_matches_the_plugin_skill() -> None:
    """Two copies ship: the plugin reads one and the package the other."""
    plugin = Path("skills/taskmanager/SKILL.md").read_text(encoding="utf-8")
    bundled = Path("src/taskmanager/skills/taskmanager/SKILL.md").read_text(encoding="utf-8")
    assert plugin == bundled


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
