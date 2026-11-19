import threading
from pathlib import Path

import pytest

from taskmanager.core.enums import NodeKind
from taskmanager.core.models import Condition, Node
from taskmanager.core.status import ConditionStage
from taskmanager.db.cache_repo import CacheRepository
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.ledger_repo import LedgerRepository
from taskmanager.db.node_repo import NodeRepository
from taskmanager.engine.claims import Claims
from taskmanager.engine.conditions import ConditionRunner
from taskmanager.engine.config import ConfigStore

COMMAND = "test -f ready"


class Kit:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.db = DatabaseManager(root / ".taskmanager")
        self.db.init_all()
        self.nodes = NodeRepository(self.db)
        self.cache = CacheRepository(self.db)
        self.ledger = LedgerRepository(self.db)
        self.nodes.save_node(Node(id="T1", kind=NodeKind.TASK, title="t"))
        self.condition = self.nodes.add_condition(
            Condition(node_id="T1", needs="the build is ready", command=COMMAND)
        )

    def runner(self, cache: CacheRepository | None = None) -> ConditionRunner:
        return ConditionRunner(
            self.root, self.nodes, cache or self.cache, self.ledger, ttl=0, timeout=60
        )

    def run(self) -> None:
        self.runner().unmet("T1", ConditionStage.CLAIM)

    def store(self, command: str, code: int) -> None:
        self.cache.put_condition("T1", self.condition.idx, command, code)

    def rows(self) -> list[tuple[str, dict[str, object]]]:
        return [
            (e.command, e.payload)
            for e in self.ledger.list_events("T1")
            if e.command.startswith("condition ")
        ]

    def set_ready(self, ready: bool) -> None:
        flag = self.root / "ready"
        if ready:
            flag.write_text("x", encoding="utf-8")
        else:
            flag.unlink(missing_ok=True)


@pytest.fixture
def kit(tmp_path: Path) -> Kit:
    return Kit(tmp_path)


def test_an_unmet_result_that_re_runs_met_records_one_condition_met(kit: Kit) -> None:
    kit.store(COMMAND, 1)
    kit.set_ready(True)
    kit.run()
    assert kit.rows() == [
        ("condition met", {"idx": kit.condition.idx, "needs": "the build is ready"})
    ]


def test_a_met_result_that_re_runs_unmet_records_one_condition_unmet(kit: Kit) -> None:
    kit.store(COMMAND, 0)
    kit.set_ready(False)
    kit.run()
    assert kit.rows() == [
        ("condition unmet", {"idx": kit.condition.idx, "needs": "the build is ready"})
    ]


@pytest.mark.parametrize(("stored", "ready"), [(0, True), (1, False)])
def test_a_re_run_with_the_same_outcome_records_nothing(kit: Kit, stored: int, ready: bool) -> None:
    kit.store(COMMAND, stored)
    kit.set_ready(ready)
    kit.run()
    assert kit.rows() == []


def test_a_re_run_that_stays_unmet_with_another_exit_code_records_nothing(kit: Kit) -> None:
    kit.store(COMMAND, 7)
    kit.run()
    assert kit.rows() == []


@pytest.mark.parametrize("ready", [True, False])
def test_a_first_evaluation_records_nothing(kit: Kit, ready: bool) -> None:
    kit.set_ready(ready)
    kit.run()
    assert kit.rows() == []


@pytest.mark.parametrize(("stored", "ready"), [(1, True), (0, False)])
def test_a_changed_command_records_nothing(kit: Kit, stored: int, ready: bool) -> None:
    kit.store("test -f other", stored)
    kit.set_ready(ready)
    kit.run()
    assert kit.rows() == []


class _Latched(CacheRepository):
    def __init__(
        self, db: DatabaseManager, before: threading.Event | None, after: threading.Event | None
    ) -> None:
        super().__init__(db)
        self.before = before
        self.after = after

    def swap_condition(
        self, node_id: str, idx: int, command: str, exit_code: int
    ) -> tuple[str, int] | None:
        if self.before is not None:
            assert self.before.wait(timeout=30)
        previous = super().swap_condition(node_id, idx, command, exit_code)
        if self.after is not None:
            self.after.set()
        return previous


def test_two_runners_re_running_the_same_condition_record_one_row(kit: Kit) -> None:
    kit.store(COMMAND, 1)
    kit.set_ready(True)
    first_committed = threading.Event()
    runners = [
        kit.runner(_Latched(kit.db, None, first_committed)),
        kit.runner(_Latched(kit.db, first_committed, None)),
    ]
    errors: list[BaseException] = []

    def go(runner: ConditionRunner) -> None:
        try:
            runner.unmet("T1", ConditionStage.CLAIM)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=go, args=(r,)) for r in runners]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
        assert not t.is_alive()
    assert errors == []
    assert first_committed.is_set()
    assert kit.rows() == [
        ("condition met", {"idx": kit.condition.idx, "needs": "the build is ready"})
    ]


def test_the_stored_row_is_read_and_replaced_inside_one_immediate_transaction(kit: Kit) -> None:
    statements: list[str] = []
    with kit.db.get_cache_connection() as conn:
        conn.set_trace_callback(statements.append)
        try:
            kit.cache.swap_condition("T1", kit.condition.idx, COMMAND, 0)
        finally:
            conn.set_trace_callback(None)
    kinds = [s.split()[0] for s in statements]
    assert kinds == ["BEGIN", "SELECT", "INSERT", "COMMIT"]
    assert statements[0] == "BEGIN IMMEDIATE"


def test_a_flip_from_claims_opened_for_a_session_carries_that_session_as_actor(kit: Kit) -> None:
    kit.store(COMMAND, 1)
    kit.set_ready(True)
    Claims.open(
        kit.root, ConfigStore(kit.root).project().model_copy(update={"condition_ttl": 0}), "S"
    ).conditions.unmet("T1", ConditionStage.CLAIM)
    flips = [e for e in kit.ledger.list_events("T1") if e.command == "condition met"]
    assert [e.actor_id for e in flips] == ["S"]
