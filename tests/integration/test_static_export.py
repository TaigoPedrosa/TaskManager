import json
import re
from pathlib import Path

from typer.testing import CliRunner

from taskmanager.cli.main import app

runner = CliRunner()


def _static_data(html: str) -> dict:  # type: ignore[type-arg]
    match = re.search(r"window\.STATIC_DATA = (.*);</script>", html)
    assert match is not None
    return json.loads(match.group(1).replace("<\\/", "</"))  # type: ignore[no-any-return]


def test_web_export_embeds_a_decisions_body_with_context_and_options(tmp_path: Path) -> None:
    runner.invoke(app, ["init", "-C", str(tmp_path)])
    add_res = runner.invoke(
        app,
        [
            "decision",
            "add",
            "Which way?",
            "--slug",
            "way",
            "--context",
            "some context",
            "--option",
            "a|Do X",
            "-C",
            str(tmp_path),
        ],
    )
    assert add_res.exit_code == 0, add_res.output

    export_res = runner.invoke(
        app, ["web", "export", "-o", str(tmp_path / "export.html"), "-C", str(tmp_path)]
    )
    assert export_res.exit_code == 0, export_res.output

    data = _static_data((tmp_path / "export.html").read_text(encoding="utf-8"))
    body = data["bodies"]["decision-way"]
    assert body["node"]["title"] == "Which way?"
    assert any(s["content"] == "some context" for s in body["sections"])
    options = body["node"]["frontmatter"]["decision"]["options"]
    assert options[0]["label"] == "Do X"
