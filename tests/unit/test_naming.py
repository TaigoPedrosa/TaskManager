import pytest

from taskmanager.core.naming import LevelNamingRule, NamingConfig, QualifiedPath, SlugGenerator


def test_parse_qualified_path() -> None:
    p1 = QualifiedPath.parse("AUTH-USER-LOGIN:steps")
    assert p1.node_id == "AUTH-USER-LOGIN"
    assert p1.section_key == "steps"

    p2 = QualifiedPath.parse("AUTH-USER:overview")
    assert p2.node_id == "AUTH-USER"
    assert p2.section_key == "overview"

    p3 = QualifiedPath.parse("AUTH")
    assert p3.node_id == "AUTH"
    assert p3.section_key is None

    p4 = QualifiedPath.parse("AUTH:")
    assert p4.node_id == "AUTH"
    assert p4.section_key is None

    p5 = QualifiedPath.parse("  AUTH-USER-LOGIN:steps  ")
    assert p5.node_id == "AUTH-USER-LOGIN"
    assert p5.section_key == "steps"


def test_slug_generator_with_slug() -> None:
    config = NamingConfig()
    gen = SlugGenerator(config)

    assert gen.generate_spec_id(slug="AUTH") == "AUTH"
    assert gen.generate_plan_id(parent_spec_id="AUTH", slug="USER") == "AUTH-USER"
    assert gen.generate_task_id(parent_plan_id="AUTH-USER", slug="LOGIN") == "AUTH-USER-LOGIN"


def test_slug_generator_custom_separator() -> None:
    config = NamingConfig(separator="/")
    gen = SlugGenerator(config)

    assert gen.generate_plan_id(parent_spec_id="AUTH", slug="USER") == "AUTH/USER"
    assert gen.generate_task_id(parent_plan_id="AUTH/USER", slug="LOGIN") == "AUTH/USER/LOGIN"


def test_slug_generator_autoincrement() -> None:
    config = NamingConfig()
    gen = SlugGenerator(config)

    assert gen.generate_spec_id(counter=1) == "S1"
    assert gen.generate_plan_id(parent_spec_id="AUTH", counter=1) == "AUTH-P1"
    assert gen.generate_task_id(parent_plan_id="AUTH-USER", counter=2) == "AUTH-USER-T2"


def test_slug_generator_custom_padding() -> None:
    config = NamingConfig(
        spec=LevelNamingRule(prefix="SPEC", pad=3),
        plan=LevelNamingRule(prefix="PLAN", pad=2),
        task=LevelNamingRule(prefix="TASK", pad=4),
    )
    gen = SlugGenerator(config)

    assert gen.generate_spec_id(counter=5) == "SPEC005"
    assert gen.generate_plan_id(parent_spec_id="SPEC005", counter=3) == "SPEC005-PLAN03"
    assert (
        gen.generate_task_id(parent_plan_id="SPEC005-PLAN03", counter=42)
        == "SPEC005-PLAN03-TASK0042"
    )


def test_require_slug_enforcement() -> None:
    task_required = NamingConfig(task=LevelNamingRule(require_slug=True))
    gen_task = SlugGenerator(task_required)

    with pytest.raises(ValueError, match="Slug is required for task"):
        gen_task.generate_task_id(parent_plan_id="AUTH-USER", slug=None)

    spec_required = NamingConfig(spec=LevelNamingRule(require_slug=True))
    gen_spec = SlugGenerator(spec_required)

    with pytest.raises(ValueError, match="Slug is required for spec"):
        gen_spec.generate_spec_id(slug=None)

    plan_required = NamingConfig(plan=LevelNamingRule(require_slug=True))
    gen_plan = SlugGenerator(plan_required)

    with pytest.raises(ValueError, match="Slug is required for plan"):
        gen_plan.generate_plan_id(parent_spec_id="AUTH", slug=None)
