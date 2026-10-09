import json
import os
from pathlib import Path

import pytest
from click.testing import Result
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from taskmanager.cli.main import app
from taskmanager.core.enums import VerificationType
from taskmanager.core.models import NodeVerification
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.node_repo import NodeRepository
from taskmanager.web.app import create_app

runner = CliRunner()

IDIOM = "git -C <repo> grep -qE '<regex>' \"${TM_VERIFY_REF:-origin/main}\" -- <path>"
RETIRED = [VerificationType.SYMBOL_SIGNATURE, VerificationType.AST_EXPORT]


def _flat(result: Result) -> str:
    return " ".join(result.output.split())


def _import(estate: Path, checks: list[dict[str, str]]) -> Result:
    doc = estate.parent / "doc.json"
    task = {"id": "P-1", "title": "a", "verifications": checks}
    doc.write_text(
        json.dumps(
            {
                "spec": {"id": "S", "title": "S"},
                "plans": [{"id": "P", "title": "P", "tasks": [task]}],
            }
        ),
        encoding="utf-8",
    )
    return runner.invoke(app, ["import", "-f", str(doc), "-C", str(estate)])


@pytest.fixture
def estate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    path = tmp_path / "estate"
    path.mkdir()
    assert runner.invoke(app, ["init", "-C", str(path)]).exit_code == 0
    assert _import(path, []).exit_code == 0
    return path


def _node_repo(estate: Path) -> NodeRepository:
    return NodeRepository(DatabaseManager(estate / ".taskmanager"))


def _store_legacy(estate: Path, v_type: VerificationType) -> None:
    _node_repo(estate).add_verification(
        NodeVerification(
            node_id="P-1", verification_type=v_type, target_path="a.py", expected_pattern="f"
        )
    )


def _run(estate: Path) -> Result:
    return runner.invoke(app, ["verify", "run", "P-1", "-C", str(estate)])


@pytest.mark.parametrize("v_type", RETIRED)
def test_import_with_a_retired_type_is_refused_naming_the_idiom(
    estate: Path, v_type: VerificationType
) -> None:
    result = _import(estate, [{"type": v_type, "target_path": "a.py", "expected_pattern": "f"}])

    assert result.exit_code == 1
    assert f"{v_type} is retired" in _flat(result)
    assert IDIOM in _flat(result)
    assert _node_repo(estate).get_verifications("P-1") == []


@pytest.mark.parametrize("v_type", RETIRED)
def test_verify_add_with_a_retired_type_is_refused_naming_the_idiom(
    estate: Path, v_type: VerificationType
) -> None:
    result = runner.invoke(
        app,
        [
            *("verify", "add", "P-1", "--type", v_type, "--target", "a.py"),
            *("--pattern", "f", "-C", str(estate)),
        ],
    )

    assert result.exit_code == 1
    assert f"{v_type} is retired" in _flat(result)
    assert IDIOM in _flat(result)
    assert _node_repo(estate).get_verifications("P-1") == []


@pytest.mark.parametrize("v_type", RETIRED)
def test_web_post_with_a_retired_type_is_refused_naming_the_idiom(
    estate: Path, v_type: VerificationType
) -> None:
    client = TestClient(create_app(estate))

    res = client.post(
        "/api/nodes/P-1/verifications",
        json={"type": v_type, "target_path": "a.py", "expected_pattern": "f"},
    )

    assert res.status_code == 400
    assert IDIOM in res.text.replace('\\"', '"')
    assert _node_repo(estate).get_verifications("P-1") == []


def test_web_meta_offers_no_retired_type(estate: Path) -> None:
    types = TestClient(create_app(estate)).get("/api/meta").json()["verification_types"]

    assert VerificationType.TEST_COMMAND in types
    assert not set(RETIRED) & set(types)


@pytest.mark.parametrize("v_type", RETIRED)
def test_a_stored_retired_row_fails_naming_the_idiom(
    estate: Path, v_type: VerificationType
) -> None:
    _store_legacy(estate, v_type)
    (estate / "a.py").write_text("def f() -> None: ...\n", encoding="utf-8")

    result = _run(estate)

    assert result.exit_code == 1
    assert "FAIL" in _flat(result)
    assert f"{v_type} is retired" in _flat(result)


def test_a_write_beside_a_stored_retired_row_is_not_refused(estate: Path) -> None:
    _store_legacy(estate, VerificationType.AST_EXPORT)

    result = runner.invoke(
        app,
        ["verify", "add", "P-1", "--type", "file_exists", "--target", "a.py", "-C", str(estate)],
    )

    assert result.exit_code == 0, result.output


@pytest.mark.parametrize("v_type", RETIRED)
def test_an_older_export_restores_with_its_retired_rows_failing(
    estate: Path, tmp_path: Path, v_type: VerificationType
) -> None:
    _store_legacy(estate, v_type)
    exported = tmp_path / "export"
    assert runner.invoke(app, ["export", str(exported), "-C", str(estate)]).exit_code == 0
    restored = tmp_path / "restored"

    restore = runner.invoke(app, ["restore", str(exported), "-C", str(restored)])
    result = _run(restored)

    assert restore.exit_code == 0, restore.output
    assert result.exit_code == 1
    assert f"{v_type} is retired" in _flat(result)
