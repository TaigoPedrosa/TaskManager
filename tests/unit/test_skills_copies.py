from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PLUGIN = REPO / "skills"
BUNDLED = REPO / "src/taskmanager/skills"
INSTALL = "uv tool install git+https://github.com/TaigoPedrosa/TaskManager@v"


def _files(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def test_every_plugin_skill_file_has_a_byte_identical_bundled_copy() -> None:
    assert _files(PLUGIN) == _files(BUNDLED)
    assert [
        f for f in _files(PLUGIN) if (PLUGIN / f).read_bytes() != (BUNDLED / f).read_bytes()
    ] == []


@pytest.mark.parametrize("skill", ["taskmanager", "dispatcher"])
def test_skill_says_how_to_install_tm_before_its_first_guide_line(skill: str) -> None:
    text = (PLUGIN / skill / "SKILL.md").read_text(encoding="utf-8")
    body = text.split("\n---\n", 1)[1]
    assert INSTALL in body
    assert body.index(INSTALL) < body.index("tm guide")
    assert "When `tm` is not on PATH, say so and stop; run nothing in its place." in body
