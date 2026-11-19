import os
import re
import shlex
import shutil
import signal
import subprocess
from itertools import dropwhile
from pathlib import Path

from taskmanager.core.models import Condition, LedgerEvent
from taskmanager.core.status import ConditionStage
from taskmanager.db.cache_repo import CacheRepository, _command_hash
from taskmanager.db.ledger_repo import LedgerRepository
from taskmanager.db.node_repo import NodeRepository

# Words a POSIX shell runs itself, so a command may start with one though nothing on PATH has it.
_SHELL_WORDS = frozenset(
    {"[", "[[", "!", "(", "{", "test", "true", "false", "cd", "command", "exit", "if", "for"}
)
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# What timeout(1) exits with, so a command cut off at the limit reads as unmet like any other.
_TIMED_OUT = 124


def is_executable(command: str) -> bool:
    """Whether `command` starts with something a shell can run: a builtin, a path, or a program on
    PATH. A condition whose command is prose ("the owner has signed off") is refused, because
    nothing can ever evaluate it: that is a decision."""
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    runnable = list(dropwhile(_ASSIGNMENT.match, words))
    if not runnable:
        return False
    first = runnable[0]
    return first in _SHELL_WORDS or "/" in first or shutil.which(first) is not None


class ConditionRunner:
    def __init__(
        self,
        root: Path,
        node_repo: NodeRepository,
        cache_repo: CacheRepository,
        ledger_repo: LedgerRepository,
        ttl: int,
        timeout: int,
        actor: str = "cli",
    ) -> None:
        self.root = root
        self.node_repo = node_repo
        self.cache_repo = cache_repo
        self.ledger_repo = ledger_repo
        self.actor = actor
        self.ttl = ttl
        self.timeout = timeout

    def unmet(self, node_id: str, stage: ConditionStage) -> list[Condition]:
        """The node's conditions of `stage` whose command does not exit 0, each result reused
        for `ttl` seconds."""
        if self.node_repo.db.in_transaction:
            raise RuntimeError(
                "conditions run outside any open transaction: a command may take the whole "
                "timeout, and the database stays locked for as long"
            )
        return [
            c
            for c in self.node_repo.get_conditions(node_id)
            if c.stage == stage and self._exit_code(c) != 0
        ]

    def _exit_code(self, condition: Condition) -> int:
        cached = self.cache_repo.get_condition(
            condition.node_id, condition.idx, condition.command, self.ttl
        )
        if cached is not None:
            return cached
        # Its own process group, so a timeout kills everything the shell started, not only the
        # shell; nothing reads the output, so nothing can block on a pipe a child still holds.
        proc = subprocess.Popen(
            condition.command,
            shell=True,
            cwd=self.root,
            env={**os.environ, "TM_ROOT": str(self.root)},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            code = proc.wait(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            code = _TIMED_OUT
        previous = self.cache_repo.swap_condition(
            condition.node_id, condition.idx, condition.command, code
        )
        # A first evaluation and a changed command have no earlier outcome to flip from.
        flipped = (
            previous is not None
            and previous[0] == _command_hash(condition.command)
            and (previous[1] == 0) != (code == 0)
        )
        if flipped:
            self.ledger_repo.append(
                LedgerEvent(
                    actor_id=self.actor,
                    command="condition met" if code == 0 else "condition unmet",
                    target_id=condition.node_id,
                    payload={"idx": condition.idx, "needs": condition.needs},
                )
            )
        return code
