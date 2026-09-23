from pathlib import Path

import pytest

from taskmanager.core.enums import NodeKind, NodeStatus, RelationType, VerificationType
from taskmanager.core.models import Node, NodeRelation, NodeSection, NodeVerification
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


def test_markdown_renderer_subagent_with_parent_and_verifications(tmp_path: Path) -> None:
    db = DatabaseManager(tmp_path)
    db.init_all()
    repo = NodeRepository(db)

    plan = Node(
        id="AUTH-P1",
        kind=NodeKind.PLAN,
        title="Token Architecture",
    )
    repo.save_node(plan)
    repo.save_section(
        NodeSection(
            node_id="AUTH-P1",
            section_key="context",
            ordinal=1,
            header="## Context",
            content="Must comply with RFC 7519 standards.",
        )
    )

    task = Node(
        id="AUTH-T1",
        kind=NodeKind.TASK,
        title="Issue JWT",
    )
    repo.save_node(task)
    repo.add_relation(
        NodeRelation(
            source_id="AUTH-P1",
            target_id="AUTH-T1",
            relation_type=RelationType.CONTAINS,
        )
    )
    repo.add_verification(
        NodeVerification(
            node_id="AUTH-T1",
            verification_type=VerificationType.FILE_EXISTS,
            target_path="src/jwt.py",
        )
    )

    renderer = MarkdownRenderer(repo)
    brief = renderer.render("AUTH-T1", view="subagent")
    assert "Parent Context (Token Architecture)" in brief
    assert "Must comply with RFC 7519 standards." in brief
    assert "# Task Brief: Issue JWT" in brief
    assert "### Verifications" in brief
    assert "file_exists: `src/jwt.py`" in brief


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
                        "verifications": [
                            {
                                "type": "file_exists",
                                "target_path": "src/token.py",
                            }
                        ],
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

    t1_ver = repo.get_verifications("AUTH-T1")
    assert len(t1_ver) == 1
    assert t1_ver[0].target_path == "src/token.py"

    t2 = repo.get_node("AUTH-T2")
    assert t2 is not None

    spec_children = repo.get_children("AUTH")
    assert "AUTH-P1" in spec_children

    plan_children = repo.get_children("AUTH-P1")
    assert "AUTH-T1" in plan_children
    assert "AUTH-T2" in plan_children

    t2_deps = repo.get_dependencies("AUTH-T2")
    assert "AUTH-T1" in t2_deps
    assert repo.get_dependency_edges("AUTH-T2") == [("AUTH-T1", NodeStatus.COMPLETED)]


def test_markdown_renderer_renders_a_decision(tmp_path: Path) -> None:
    from taskmanager.engine.decisions import DecisionAnswer, DecisionData, DecisionOption

    db = DatabaseManager(tmp_path)
    db.init_all()
    repo = NodeRepository(db)

    data = DecisionData(
        options=[
            DecisionOption(key="a", label="Option A", recommended=True),
            DecisionOption(key="b", label="Option B"),
        ],
        answer=DecisionAnswer(
            option="a", rationale="because", answered_by="owner", answered_at="2026-01-01T00:00:00"
        ),
    )
    node = Node(
        id="decision-D1",
        kind=NodeKind.DECISION,
        title="Which way?",
        status=NodeStatus.COMPLETED,
        frontmatter={"decision": data.model_dump(mode="json")},
    )
    repo.save_node(node)
    repo.save_section(
        NodeSection(
            node_id="decision-D1",
            section_key="context",
            ordinal=1,
            header="## Context",
            content="some context",
        )
    )

    rendered = MarkdownRenderer(repo).render("decision-D1")
    assert "Which way?" in rendered
    assert "some context" in rendered
    assert "Option A" in rendered
    assert "**(recommended)**" in rendered
    assert "Option B" in rendered
    assert "Chosen: a" in rendered
    assert "Rationale: because" in rendered
    assert "owner" in rendered


def test_bulk_importer_gated_dependency(tmp_path: Path) -> None:
    db = DatabaseManager(tmp_path)
    db.init_all()
    repo = NodeRepository(db)
    importer = BulkImporter(repo)

    importer.import_dict(
        {
            "tasks": [
                {"id": "AUTH-T1", "title": "Implement"},
                {
                    "id": "AUTH-T2",
                    "title": "Review",
                    "depends_on": [{"id": "AUTH-T1", "gate": "WAITING_REVIEW"}],
                },
            ]
        }
    )

    assert repo.get_dependency_edges("AUTH-T2") == [("AUTH-T1", NodeStatus.WAITING_REVIEW)]


def test_bulk_importer_top_level_decisions_key(tmp_path: Path) -> None:
    db = DatabaseManager(tmp_path)
    db.init_all()
    repo = NodeRepository(db)
    importer = BulkImporter(repo)

    importer.import_dict(
        {
            "tasks": [{"id": "T1", "title": "Task"}],
            "decisions": [
                {
                    "id": "decision-D1",
                    "title": "Which way?",
                    "frontmatter": {"decision": {"options": [], "allow_custom": True}},
                }
            ],
        }
    )

    node = repo.get_node("decision-D1")
    assert node is not None
    assert node.kind == NodeKind.DECISION
    assert node.title == "Which way?"


def test_bulk_importer_refuses_unknown_key_on_a_decision(tmp_path: Path) -> None:
    db = DatabaseManager(tmp_path)
    db.init_all()
    importer = BulkImporter(NodeRepository(db))

    with pytest.raises(ValueError, match="unknown keys"):
        importer.import_dict({"decisions": [{"id": "decision-D1", "title": "Q", "bogus": 1}]})
