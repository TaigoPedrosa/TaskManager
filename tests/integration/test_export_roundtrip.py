import json
from pathlib import Path

from typer.testing import CliRunner

from taskmanager.cli.main import app
from taskmanager.core.enums import NodeKind, RelationType
from taskmanager.core.lifecycle import claim, next_action
from taskmanager.core.models import Lease
from taskmanager.core.status import Status
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.di.container import create_container
from taskmanager.engine.config import ConfigStore
from taskmanager.engine.operations import Operations
from taskmanager.engine.snapshot import apply_cycle, cycle_of

runner = CliRunner()


def tm(*args: str) -> None:
    res = runner.invoke(app, list(args))
    assert res.exit_code == 0, res.output


def repo(root: Path) -> NodeRepository:
    return create_container(root).get(NodeRepository)


def in_step(root: Path, node_id: str, claimed_from: Status) -> None:
    node_repo = repo(root)
    node = node_repo.get_node(node_id)
    assert node is not None
    node = node.model_copy(update={"status": claimed_from})
    node_repo.save_node(node)
    cycle = cycle_of(node, sensitive=False)
    lease = Lease(
        task_id=node_id,
        agent_id="a",
        session_id="s",
        branch_name=f"tm/{node_id}",
        action=next_action(cycle),
        ttl_seconds=3600,
    )
    assert RuntimeRepository(node_repo.db).claim(lease, [], apply_cycle(node, claim(cycle)))


def seeded(root: Path) -> None:
    tm("init", "-C", str(root))
    ops = create_container(root).get(Operations)
    spec = ops.add_spec("S", slug="S", order=3)
    first = ops.add_plan("first", spec, slug="P1", order=2)
    second = ops.add_plan("second", spec, slug="P2", order=5)
    ops.add_task("building", first, slug="build")
    ops.add_task("reviewing", first, slug="review")
    # The replacement's document restores before the one holding what it supersedes.
    ops.add_task("new", first, slug="new")
    ops.add_task("old", second, slug="old")
    ops.supersede("S-P2-old", "S-P1-new")
    in_step(root, "S-P1-build", Status.READY)
    in_step(root, "S-P1-review", Status.IMPLEMENTED)
    tm("config", "set", "worktree_dir", str(root / "worktrees"), "-C", str(root))
    tm("config", "set", "max_merge_attempts", "5", "-C", str(root))


def test_export_restore_export_round_trips_steps_supersedes_ordinals_and_config(
    tmp_path: Path,
) -> None:
    source, fresh = tmp_path / "source", tmp_path / "fresh"
    source.mkdir()
    fresh.mkdir()
    seeded(source)
    building = repo(source).get_node("S-P1-build")
    assert building is not None and building.status == Status.IMPLEMENTING

    e1, e2 = tmp_path / "e1", tmp_path / "e2"
    tm("export", str(e1), "-C", str(source))
    tm("restore", str(e1), "-C", str(fresh))
    tm("export", str(e2), "-C", str(fresh))

    assert sorted(f.name for f in e1.iterdir()) == sorted(f.name for f in e2.iterdir())
    for f in sorted(e1.glob("*.json")):
        assert f.read_bytes() == (e2 / f.name).read_bytes(), f.name

    restored = repo(fresh)
    build, review = restored.get_node("S-P1-build"), restored.get_node("S-P1-review")
    assert build is not None and review is not None
    assert (build.status, build.claimed_from) == (Status.READY, None)
    assert (review.status, review.claimed_from, review.review_cycles) == (
        Status.IMPLEMENTED,
        None,
        0,
    )
    assert restored.relations(RelationType.SUPERSEDES) == [("S-P1-new", "S-P2-old")]
    ordinals = {
        n.id: n.ordinal for k in (NodeKind.SPEC, NodeKind.PLAN) for n in restored.list_nodes(kind=k)
    }
    assert ordinals == {"S": 3, "S-P1": 2, "S-P2": 5}

    config_text = (e1 / "_config.json").read_text(encoding="utf-8")
    assert json.loads(config_text) == {"max_merge_attempts": 5}
    assert str(tmp_path) not in config_text
    assert ConfigStore(fresh).document() == {"max_merge_attempts": 5}


def test_restore_keeps_the_restoring_roots_worktree_dir_over_a_version_1_exports(
    tmp_path: Path,
) -> None:
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    target.mkdir()
    tm("init", "-C", str(source))
    create_container(source).get(Operations).add_spec("S", slug="S")
    export = tmp_path / "e"
    tm("export", str(export), "-C", str(source))
    (export / "_format.json").write_text(
        json.dumps({"format": "tm-lifecycle", "version": 1}), encoding="utf-8"
    )
    (export / "_config.json").write_text(
        json.dumps({"worktree_dir": "/elsewhere/worktrees", "max_merge_attempts": 5}),
        encoding="utf-8",
    )
    tm("init", "-C", str(target))
    tm("config", "set", "worktree_dir", str(target / "worktrees"), "-C", str(target))

    tm("restore", str(export), "-C", str(target))

    assert ConfigStore(target).document() == {
        "max_merge_attempts": 5,
        "worktree_dir": str(target / "worktrees"),
    }
    assert repo(target).get_node("S") is not None


def test_restore_of_an_export_holding_only_machine_local_config_clears_the_targets_shared_keys(
    tmp_path: Path,
) -> None:
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    target.mkdir()
    tm("init", "-C", str(source))
    create_container(source).get(Operations).add_spec("S", slug="S")
    tm("config", "set", "worktree_dir", str(source / "worktrees"), "-C", str(source))
    export = tmp_path / "e"
    tm("export", str(export), "-C", str(source))
    tm("init", "-C", str(target))
    tm("config", "set", "worktree_dir", str(target / "worktrees"), "-C", str(target))
    tm("config", "set", "max_merge_attempts", "9", "-C", str(target))

    tm("restore", str(export), "-C", str(target))

    assert ConfigStore(target).document() == {"worktree_dir": str(target / "worktrees")}
    assert json.loads((export / "_config.json").read_text(encoding="utf-8")) == {}
