import time
from pathlib import Path

import pytest

from taskmanager.core.enums import NodeKind
from taskmanager.core.models import Condition, Node
from taskmanager.core.status import ConditionStage
from taskmanager.db.cache_repo import CacheRepository
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.node_repo import NodeRepository
from taskmanager.engine.conditions import ConditionRunner, is_executable


@pytest.mark.parametrize(
    ("command", "executable"),
    [
        ("true", True),
        ("test -f README.md", True),
        ("[ -f README.md ]", True),
        ("git status --porcelain", True),
        ("./scripts/staging-up.sh", True),
        ("FOO=1 git status", True),
        ("( cd core && git status )", True),
        ("", False),
        ("   ", False),
        ("FOO=1", False),
        ("'unterminated quote", False),
        ("Wait for the owner's go-ahead", False),
        ("staging is up", False),
    ],
)
def test_a_command_is_executable_only_when_a_shell_can_start_it(
    command: str, executable: bool
) -> None:
    assert is_executable(command) is executable


class Kit:
    def __init__(self, root: Path) -> None:
        self.root = root
        db = DatabaseManager(root / ".taskmanager")
        db.init_all()
        self.nodes = NodeRepository(db)
        self.cache = CacheRepository(db)
        self.nodes.save_node(Node(id="T1", kind=NodeKind.TASK, title="t"))

    def condition(self, command: str, stage: ConditionStage = ConditionStage.CLAIM) -> Condition:
        return self.nodes.add_condition(
            Condition(node_id="T1", needs=command, command=command, stage=stage)
        )

    def runner(self, ttl: int = 300, timeout: int = 60) -> ConditionRunner:
        return ConditionRunner(self.root, self.nodes, self.cache, ttl=ttl, timeout=timeout)


@pytest.fixture
def kit(tmp_path: Path) -> Kit:
    return Kit(tmp_path)


def test_only_the_failing_conditions_of_the_asked_stage_are_unmet(kit: Kit) -> None:
    kit.condition("true")
    failing_claim = kit.condition("false")
    failing_landing = kit.condition("exit 3", ConditionStage.LANDING)
    kit.condition("true", ConditionStage.LANDING)
    runner = kit.runner()
    assert runner.unmet("T1", ConditionStage.CLAIM) == [failing_claim]
    assert runner.unmet("T1", ConditionStage.LANDING) == [failing_landing]


def test_a_node_with_no_conditions_has_none_unmet(kit: Kit) -> None:
    assert kit.runner().unmet("T1", ConditionStage.CLAIM) == []


def test_a_command_runs_from_the_project_root_with_tm_root_set(kit: Kit) -> None:
    (kit.root / "marker").write_text("x", encoding="utf-8")
    kit.condition("test -f marker")
    kit.condition(f'test "$TM_ROOT" = "{kit.root}"')
    assert kit.runner().unmet("T1", ConditionStage.CLAIM) == []


def test_a_result_is_reused_for_the_ttl_and_rerun_after_it(kit: Kit) -> None:
    runs = kit.root / "runs"
    kit.condition(f"echo run >> {runs}; test -f {kit.root / 'flag'}")
    cached = kit.runner(ttl=300)
    assert len(cached.unmet("T1", ConditionStage.CLAIM)) == 1
    (kit.root / "flag").write_text("up", encoding="utf-8")
    assert len(cached.unmet("T1", ConditionStage.CLAIM)) == 1
    assert runs.read_text(encoding="utf-8").count("run") == 1
    assert kit.runner(ttl=0).unmet("T1", ConditionStage.CLAIM) == []
    assert runs.read_text(encoding="utf-8").count("run") == 2


def test_a_command_cut_off_at_the_timeout_is_unmet(kit: Kit) -> None:
    slow = kit.condition("sleep 5; true")
    started = time.monotonic()
    assert kit.runner(timeout=1).unmet("T1", ConditionStage.CLAIM) == [slow]
    assert time.monotonic() - started < 4


def test_conditions_refuse_to_run_inside_an_open_transaction(kit: Kit) -> None:
    kit.condition("true")
    with pytest.raises(RuntimeError, match="outside any open transaction"), kit.nodes.transaction():
        kit.runner().unmet("T1", ConditionStage.CLAIM)
