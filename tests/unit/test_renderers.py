from pathlib import Path

import pytest

from taskmanager.core.enums import NodeKind, NodeStatus
from taskmanager.core.models import Node, NodeSection
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.node_repo import NodeRepository
from taskmanager.renderers.importers import BulkImporter
from taskmanager.renderers.markdown import MarkdownRenderer


def test_markdown_renderer_projections(tmp_path: Path) -> None:
    db = DatabaseManager(tmp_path)
    db.init_all()
    repo = NodeRepository(db)

    task = Node(
        id="AUTH-T01",
        kind=NodeKind.TASK,
        title="JWT Auth Task",
        status=NodeStatus.NOT_STARTED,
        priority=80,
        acceptable_models=["sonnet"],
    )
    repo.save_node(task)
    repo.save_section(
        NodeSection(
            node_id="AUTH-T01",
            section_key="steps",
            ordinal=1,
            header="### Steps",
            content="- [ ] Step 1: Write test",
        )
    )

    renderer = MarkdownRenderer(repo)
    summary = renderer.render("AUTH-T01", view="summary")
    assert "id: AUTH-T01" in summary
    assert "JWT Auth Task" in summary

    subagent_brief = renderer.render("AUTH-T01", view="subagent")
    assert "### Steps" in subagent_brief
    assert "- [ ] Step 1: Write test" in subagent_brief

    full_doc = renderer.render("AUTH-T01", view="full")
    assert "# JWT Auth Task" in full_doc
    assert "### Steps" in full_doc
    assert "- [ ] Step 1: Write test" in full_doc


def test_markdown_renderer_summary_with_overview(tmp_path: Path) -> None:
    db = DatabaseManager(tmp_path)
    db.init_all()
    repo = NodeRepository(db)

    node = Node(
        id="SPEC-01",
        kind=NodeKind.SPEC,
        title="Authentication Spec",
        status=NodeStatus.NOT_STARTED,
        priority=50,
        acceptable_models=["opus"],
        frontmatter={"owner": "security-team"},
    )
    repo.save_node(node)
    repo.save_section(
        NodeSection(
            node_id="SPEC-01",
            section_key="overview",
            ordinal=1,
            header="## Overview",
            content="This spec details the OAuth2 implementation.",
        )
    )

    renderer = MarkdownRenderer(repo)
    summary = renderer.render("SPEC-01", view="summary")
    assert "id: SPEC-01" in summary
    assert "kind: spec" in summary
    assert "owner: security-team" in summary
    assert "This spec details the OAuth2 implementation." in summary


def test_markdown_renderer_errors(tmp_path: Path) -> None:
    db = DatabaseManager(tmp_path)
    db.init_all()
    repo = NodeRepository(db)
    renderer = MarkdownRenderer(repo)

    with pytest.raises(ValueError, match="not found"):
        renderer.render("NON-EXISTENT")

    node = Node(
        id="TASK-01",
        kind=NodeKind.TASK,
        title="Sample Task",
    )
    repo.save_node(node)

    with pytest.raises(ValueError, match="Unknown view"):
        renderer.render("TASK-01", view="unsupported")


def test_bulk_importer_json(tmp_path: Path) -> None:
    db = DatabaseManager(tmp_path)
    db.init_all()
    repo = NodeRepository(db)
    importer = BulkImporter(repo)

    payload = {
        "spec": {"id": "AUTH", "title": "Auth Spec", "priority": 60},
        "plans": [
            {
                "id": "AUTH-P1",
                "title": "Token Plan",
                "priority": 70,
                "sections": [
                    {
                        "section_key": "context",
                        "header": "## Context",
                        "content": "Plan context details.",
                    }
                ],
                "tasks": [
                    {
                        "id": "AUTH-T1",
                        "title": "Create Token",
                        "priority": 90,
                        "acceptable_models": ["sonnet"],
                        "sections": {
                            "steps": {
                                "header": "### Steps",
                                "content": "- [ ] Implement JWT issuance",
                            }
                        },
                    },
                    {
                        "id": "AUTH-T2",
                        "title": "Validate Token",
                        "priority": 85,
                        "depends_on": ["AUTH-T1"],
                    },
                ],
            }
        ],
    }
    importer.import_dict(payload)

    spec = repo.get_node("AUTH")
    assert spec is not None
    assert spec.title == "Auth Spec"
    assert spec.priority == 60

    plan = repo.get_node("AUTH-P1")
    assert plan is not None
    assert plan.title == "Token Plan"
    plan_sec = repo.get_section("AUTH-P1", "context")
    assert plan_sec is not None
    assert plan_sec.content == "Plan context details."

    t1 = repo.get_node("AUTH-T1")
    assert t1 is not None
    assert t1.priority == 90
    assert t1.acceptable_models == ["sonnet"]
    t1_sec = repo.get_section("AUTH-T1", "steps")
    assert t1_sec is not None
    assert "- [ ] Implement JWT issuance" in t1_sec.content

    t2 = repo.get_node("AUTH-T2")
    assert t2 is not None

    spec_children = repo.get_children("AUTH")
    assert "AUTH-P1" in spec_children

    plan_children = repo.get_children("AUTH-P1")
    assert "AUTH-T1" in plan_children
    assert "AUTH-T2" in plan_children

    t2_deps = repo.get_dependencies("AUTH-T2")
    assert "AUTH-T1" in t2_deps
