import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
README = (REPO / "README.md").read_text(encoding="utf-8")


def _sections() -> list[str]:
    return re.findall(r"^## (.+)$", README, re.MULTILINE)


def test_readme_opens_with_requirements_install_and_quickstart() -> None:
    assert _sections()[:3] == ["Requirements", "Install", "Quickstart"]


def test_readme_links_only_to_files_that_exist() -> None:
    targets = re.findall(r"\]\(([^)#]+)\)", README)
    local = [t for t in targets if not t.startswith(("http://", "https://"))]
    assert "docs/tm-web.png" in local
    assert [t for t in local if not (REPO / t).is_file()] == []
