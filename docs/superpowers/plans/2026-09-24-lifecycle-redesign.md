# Lifecycle Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace tm's mixed stored/virtual status model with one stored dispatch status, derived displays, per-node review/fix/merge flags, node-level landing on a parent branch or `main`, deterministic landings run as detached jobs, and a fresh-start cutover.

**Architecture:** Pure, table-tested modules hold the rules (status vocabulary, state machine, container rollup, display derivation, landing chains, expanded step graph, write validation). Storage gains the new columns and coordination tables in one `state.db`, so a claim is one transaction. Engine services (conditions, decisions, claims, gates, landing and sync jobs, discovery) sit on the storage and the pure rules; the CLI, web and `tm-wave.js` switch to them last, and the old vocabulary is deleted once nothing reads it.

**Tech Stack:** Python 3.14, uv, typer, pydantic 2, SQLite (WAL, sqlite-vec), FastAPI + vanilla JS web, pytest, ruff, mypy strict; `tm-wave.js` is a Claude Code workflow script tested with `node --test` (Node 26).

**Spec:** `docs/superpowers/specs/2026-09-24-lifecycle-redesign.md` — every task argues from it; read the sections each task cites.

## Global Constraints

- **The live tm is an editable install of `/Users/taigo.pedrosa/Documents/TaskManager/src`.** Never edit, check out, merge into, or fast-forward that primary checkout; never run `install.sh`, `uv tool install` or `claude plugin` commands. All work happens in the worktree `…/scratchpad/tm-lifecycle` on branch `feat/lifecycle`, cut `--no-track` from `origin/main`.
- **Never read or write `/Users/taigo.pedrosa/Documents/SocialSrc/.taskmanager/`**, and never run a `tm` command without `--path <tmp estate>` or a `TM_ROOT` pointing at a temporary directory. The live `tm` on `PATH` is the old version: tests invoke the code under test through `uv run --directory <worktree>`, never the bare `tm`.
- **Publishing is pushing `feat/lifecycle` and the tag `v0.3.0`, never `main`.** Merging to `main` happens only at the owner-approved cutover (spec §9.2), because the primary checkout that the live `tm` runs from tracks `main`.
- Gates, run from the worktree with `uv run --directory <worktree> …`, each as `cmd; echo $?` in the foreground: `pytest -q`, `ruff check`, `ruff format --check`, `mypy`. `origin/main` is red on three of them today (six `test_search_of_hostile_text_exits_zero` cases, two mypy errors in `heuristics.py` and `wave.py`, three unformatted files); Task 0 makes them green, and every later task ends with all four exiting 0 over the whole repository.
- Never `cd` in a Bash command; use `git -C`, `uv run --directory`, or a `( cd x && y )` subshell.
- Commits: explicit pathspec (`git add <files> && git commit -m …` in one invocation), a conventional prefix naming the change and its end goal, no AI-attribution trailer, a body only for a fact the diff cannot show.
- Code comments say why, never what; no comment names a task, plan, spec section, review or round. Test names, log lines and error messages state behaviour, not history.
- Until Task 21, `Node.status` is typed `NodeStatus | Status` and `NodeStatus` still exists, so untouched modules keep working; `StrEnum` members compare equal to their string value, so `NodeStatus.COMPLETED == Status.COMPLETED`. Task 21 deletes the old vocabulary.
- **A test never resolves an estate from its working directory.** From a worktree of the TaskManager repository, `tm`'s root lookup follows `git rev-parse --git-common-dir` to the primary checkout and finds its live `.taskmanager`. Every test passes `-C <tmp_path>` before any `--`, or sets `TM_ROOT` to a temporary directory; a test that could fall through to the lookup runs from an empty directory.
- Temporary git repositories in tests have a bare `origin` created with `git init --bare`, and a clone; nothing in a test touches the network.
- Refusals are `OperationError(message, status_code)` from `taskmanager.engine.operations` (400 invalid input, 404 unknown node, 409 conflict with current state); the CLI prints the message verbatim and exits 1, and `blocked` from `tm task start` exits 3.

## Setup

Once, before Task 0:

```bash
S=/private/tmp/claude-502/-Users-taigo-pedrosa-Documents-SocialSrc/0e15c669-90c6-4917-ad14-163de6ca65b9/scratchpad
git -C /Users/taigo.pedrosa/Documents/TaskManager fetch -q origin main
git -C /Users/taigo.pedrosa/Documents/TaskManager worktree add --no-track -b feat/lifecycle "$S/tm-lifecycle" origin/main; echo $?
git -C "$S/tm-lifecycle" rev-parse --abbrev-ref HEAD
uv sync --directory "$S/tm-lifecycle" --group dev; echo $?
```

Expected: both commands exit 0 and the branch prints `feat/lifecycle`. `<worktree>` in every task below is `$S/tm-lifecycle`. The primary checkout is only the source of the `worktree add`; nothing else in it changes.

## Review Focus

1. Two sessions claiming the same node at the same instant: exactly one lease results, the loser gets `action: blocked` and every table is unchanged by its attempt (Task 9 pins it with two threads).
2. A landing job killed after its push but before `COMPLETED`: the node is swept back to `claimed_from`, and the next merge claim recognises the branch as already landed and completes without a second merge commit (Task 14).
3. A node building on `tm/P` whose dependency landed on `main` while `tm/P` lags: the claim syncs `main` into `tm/P` before implement starts, never after (Task 15).
4. An import whose flags break a §2.3 rule or close a cycle: nothing is written, and the message prints the cycle path (Task 17).
5. The old `v0.2.0` binary run against a new estate fails with SQLite's "file is not a database"; the new binary run against an old estate refuses with the `tm init --archive` instruction (Task 8).

## Interface contract

Every task implements or consumes exactly these names. A task may add private helpers; it may not rename or re-type anything here.

### `taskmanager/core/status.py` (Task 1)

```python
class Status(StrEnum):
    (
        READY,
        IMPLEMENTING,
        IMPLEMENTED,
        REVIEWING,
        REVIEWED,
        FIXING,
        FIXED,
        MERGING,
    )
    COMPLETED, FAILED, DEFERRED, ABANDONED, SUPERSEDED  # value == name


IN_STEP: frozenset[Status]  # IMPLEMENTING, REVIEWING, FIXING, MERGING
EXITS: frozenset[Status]  # DEFERRED, ABANDONED, SUPERSEDED
STABLE: frozenset[Status]  # READY, IMPLEMENTED, REVIEWED, FIXED, COMPLETED, FAILED
SET_ASIDE = EXITS  # children a container rollup does not count


class DecisionStatus(StrEnum):
    OPEN, ANSWERED, WITHDRAWN


class Outcome(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    MERGE_FAILED = "merge_failed"


class Merge(StrEnum):
    PARENT = "parent"
    MAIN = "main"


class Phase(StrEnum):
    QUEUED, DISPATCHED, COMPLETED, FAILED, DEFERRED, ABANDONED, SUPERSEDED


class DisplayStatus(StrEnum):
    (
        READY,
        IMPLEMENTING,
        REVIEWING,
        FIXING,
        MERGING,
        COMPLETED,
        FAILED,
        DEFERRED,
        ABANDONED,
    )
    (
        SUPERSEDED,
        WAITING_REVIEW,
        WAITING_FIX,
        WAITING_MERGE,
        WAITING_MERGE_AGENT,
        STALE,
    )
    AWAITING_DECISION, BLOCKED_BY_TASK, BLOCKED_BY_CONDITION, BLOCKED_BY_SYNC, BLOCKED_BY_LEASE


class Action(StrEnum):
    IMPLEMENT = "implement"
    REVIEW = "review"
    FIX = "fix"
    MERGE = "merge"
    SYNC = "sync"
    BLOCKED = "blocked"


class Event(StrEnum):
    COMPLETE = "complete"
    APPROVE = "approve"
    REJECT = "reject"
    LANDED = "landed"
    OWN_DEFECT = "own_defect"
    RELEASE = "release"
    RELEASE_BLOCKED = "release_blocked"
    EXPIRED = "expired"


class JobKind(StrEnum):
    LAND = "land"
    SYNC = "sync"


class JobState(StrEnum):
    RUNNING = "running"
    NEEDS_AGENT = "needs_agent"
    SUCCEEDED = "succeeded"
    OWN_DEFECT = "own_defect"
    CONDITION_UNMET = "condition_unmet"
    EXPIRED = "expired"


class ConditionStage(StrEnum):
    CLAIM = "claim"
    LANDING = "landing"


class DecisionEffect(StrEnum):
    NONE = "none"
    ABANDON = "abandon"
    DEFER = "defer"
    REOPEN = "reopen"
    DROP_EDGE = "drop_edge"
```

### `taskmanager/core/lifecycle.py` (Task 2)

```python
@dataclass(frozen=True)
class Cycle:
    status: Status
    container: bool = False
    review: bool = True
    fix: bool = True
    outcome: Outcome | None = None
    fix_for: Outcome | None = None
    claimed_from: Status | None = None
    review_cycles: int = 0
    merge_attempts: int = 0
    step_failures: int = 0

@dataclass(frozen=True)
class Caps:
    fix_rounds_task: int = 2
    fix_rounds_container: int = 3
    merge_attempts: int = 3
    step_failures: int = 3

class LifecycleError(ValueError): ...

def next_action(c: Cycle) -> Action | None          # None: COMPLETED, FAILED, an exit, a container at READY, or a node in a step
def claim(c: Cycle) -> Cycle                        # stable -> its -ING, claimed_from set, counters per spec §3.1-3.2
def advance(c: Cycle, event: Event, caps: Caps) -> Cycle   # every row of spec §3.1, FAILED rows included; RELEASE/EXPIRED
                                                    # on a stable status add a step failure (cap -> FAILED); a review
                                                    # released or expired without a verdict refunds its counted cycle
def fix_round(c: Cycle) -> int                      # 1-based: review_cycles when the fix answers REJECT, 0 for a MERGE_FAILED fix
def reopen(c: Cycle, children_all_completed: bool) -> Cycle
def defer(c: Cycle) -> Cycle
def abandon(c: Cycle) -> Cycle
def reset(c: Cycle, to: Status, outcome: Outcome | None) -> Cycle
```

### `taskmanager/core/rollup.py` (Task 3)

```python
def rollup(current: Status, children: Sequence[Status]) -> Status   # spec §3.1 "Containers", without the git empty-diff rule
```

### `taskmanager/core/display.py` (Task 4)

```python
@dataclass(frozen=True)
class Facts:
    lease: Literal["live", "expired", "none"] = "none"
    job_needs_agent: bool = False
    open_decision: bool = False
    unsatisfied_edge: bool = False
    unmet_condition: bool = False
    sync_pending: bool = False
    files_locked: bool = False
    descendant_started: bool = False

def phase(status: Status) -> Phase
def display_status(c: Cycle, f: Facts) -> DisplayStatus   # spec §7.2, first row wins
```

### `taskmanager/engine/chains.py` (Task 5)

```python
MAIN: Final = "MAIN"
class Tree(Protocol):
    def parent(self, node_id: str) -> str | None: ...
    def merge(self, node_id: str) -> Merge: ...
    def status(self, node_id: str) -> Status | DecisionStatus: ...
def landing_target(t: Tree, node_id: str) -> str
def base_chain(t: Tree, node_id: str) -> list[str]
def landing_chain(t: Tree, node_id: str) -> list[str]
def meeting(t: Tree, x: str, y: str) -> str                        # Z of spec §4.1
def satisfied(t: Tree, x: str, y: str) -> bool
def sync_pairs(t: Tree, x: str, y: str) -> list[tuple[str, str]]   # (source target, base node) merges, top-down; [] when none
```

### `taskmanager/engine/stepgraph.py` (Task 6)

```python
@dataclass(frozen=True)
class SnapNode:
    id: str
    kind: NodeKind
    parent: str | None
    merge: Merge
    status: Status | DecisionStatus
    review: bool
    fix: bool
    repo: str | None
    writes_migration: bool
    busy: bool                      # live lease or running job
    literal_origin_main: bool       # owns a test_command naming origin/main literally

@dataclass
class Snapshot:                     # satisfies chains.Tree
    nodes: dict[str, SnapNode]
    edges: list[tuple[str, str]]    # (dependent, dependency)
    def parent(self, node_id: str) -> str | None: ...
    def merge(self, node_id: str) -> Merge: ...
    def status(self, node_id: str) -> Status | DecisionStatus: ...
    def children(self, node_id: str) -> list[str]: ...
    def descendants(self, node_id: str) -> list[str]: ...
    def inherited_edges(self, node_id: str) -> list[str]: ...   # own and every ancestor's dependencies

def find_cycle(s: Snapshot) -> list[str] | None   # vertex labels "<id>.start|implemented|landed", first == last;
                                                  # an inherited edge of descendant D on Y waits on meeting(D, Y)
def migration_order(s: Snapshot, repo: str) -> list[str]   # the order discovery grants a repository's migration chain
def format_cycle(path: list[str]) -> str          # "A.start ← P.landed ← …"
```

### `taskmanager/engine/validation.py` (Task 7)

```python
@dataclass(frozen=True)
class Refusal:
    node_id: str
    rule: int                       # spec §2.3 rule number
    message: str
class BranchFacts(Protocol):
    def branch_exists(self, node_id: str) -> bool: ...
    def base_matches(self, node_id: str, new_target: str) -> bool: ...   # new_target is chains.MAIN or a node id;
        # true when, in every repository holding the branch, merge-base(branch, ref(new_target)) ==
        # merge-base(branch, ref(current target)), ref(MAIN) = origin/main
def validate(before: Snapshot, after: Snapshot, touched: set[str], branches: BranchFacts) -> list[Refusal]
```

### Storage (Tasks 8–9)

- `taskmanager/core/models.py`: `Node` gains `claimed_from: Status | None`, `review: bool`, `fix: bool`, `merge: Merge`, `outcome: Outcome | None`, `verdict: str | None`, `fix_for: Outcome | None`, `review_cycles: int`, `merge_attempts: int`, `step_failures: int`, `branch: str | None` (None reads as `tm/<id>`), `requires: list[str]`, `land_order: list[str]`; `status: NodeStatus | Status | DecisionStatus` until Task 21. New pydantic models `Condition(node_id, idx, needs, command, stage)` (`idx` assigned by `add_condition`), `Job(id="", kind, node_id, repo, target, state, step, worktree, pid, heartbeat, result: dict[str, Any])`, `BranchLock(repo, branch, holder, heartbeat)`, and the frozen dataclass `GateRun(exit_code: int, failing: frozenset[str] | None, tail: str)`. `Lease` gains `action: Action | None`, `review_hash: str | None`, `model: str | None` (the routed family, read by discovery's strong-slot count), and `ttl_seconds: int | None` (None while a stopped job waits for an agent).
- `taskmanager/db/connection.py` (Task 8): `DatabaseManager` opens `state.db`, `cache.db`, `ledger.db`; `get_state_connection()`, with `get_spec_connection` and `get_runtime_connection` kept as aliases until Task 21; `get_cache_connection()`; `init_all()` writes the tombstones; `is_pre_lifecycle() -> bool`; `PreLifecycleEstate(Exception)` raised when the first connection opens on an old estate; `@staticmethod archive_pre_lifecycle(root) -> Path` (raises `ValueError` when there is nothing to archive). Leases and file locks live in `state.db` from Task 8.
- `taskmanager/db/node_repo.py`: `get_conditions(node_id) -> list[Condition]`, `add_condition(c) -> Condition`, `remove_condition(node_id, idx) -> bool`; `relations(relation_type) -> list[tuple[str, str]]` (Task 10).
- `taskmanager/db/runtime_repo.py` (Task 9): `claim(lease, locks, node) -> bool` — one `BEGIN IMMEDIATE`, plain `INSERT`s, writes `status`, `claimed_from`, `outcome`, `fix_for`, the counters and `updated_at` from `node`, requires the stored status to equal `node.claimed_from`, returns False having written nothing on any conflict, raises `RuntimeError` inside an open `NodeRepository.transaction()`; `list_leases() -> list[Lease]`, `list_locks() -> list[FileLock]`, `park(node_id) -> None` (ttl to NULL), `take_over(node_id, agent, session, ttl, model) -> bool` (one conditional `UPDATE … WHERE ttl_seconds IS NULL`); `get_lease`, `heartbeat`, `release_lease`, `get_conflicting_tasks` keep their signatures; `sweep_expired_leases` skips NULL-ttl leases. Every write commits through the state connection's single commit.
- `taskmanager/db/job_repo.py` (Task 9): `JobRepository(db_mgr)`: `create(job) -> Job` (assigns `id = f"{kind}-{uuid4().hex[:12]}"`), `get(job_id) -> Job | None`, `for_node(node_id) -> list[Job]` (creation order), `update(job) -> None` (`KeyError` for an unknown id), `waiting_for_agent() -> list[Job]`, `acquire_branch(repo, branch, holder) -> bool` (a lock whose holder is not a `running` job is stale and taken over), `release_branch(repo, branch, holder) -> None` (no-op when not held).
- `taskmanager/db/cache_repo.py` (Task 9): `CacheRepository(db_mgr)`: `get_baseline(repo, sha, template_hash) -> GateRun | None`, `put_baseline(repo, sha, template_hash, run) -> None`, `get_condition(node_id, idx, command, max_age) -> int | None`, `put_condition(node_id, idx, command, exit_code) -> None` (keyed with the command's hash, so a reused index never serves a removed command's result).

### Engine services (Tasks 10–18)

- `taskmanager/engine/snapshot.py` (Task 10): `SnapshotBuilder(node_repo, runtime_repo, job_repo)`: `.build() -> Snapshot`, `.facts(node_id, snapshot) -> Facts` (leaves `unmet_condition` False; callers fold in the condition result with `dataclasses.replace`), `.cycle(node) -> Cycle`, `.lock_set(node_id, snapshot) -> list[str]`; module functions `writes_migration(files)`, `stored_status(node) -> Status | DecisionStatus` (reads the old names still on disk until Task 21), `cycle_of(node) -> Cycle`, `apply_cycle(node, cycle) -> Node`, `node_busy(runtime_repo, job_repo, node_id) -> bool`, constant `CONTAINERS`. `literal_origin_main` ignores `origin/main` inside a `${TM_VERIFY_REF:-…}` expansion.
- `taskmanager/engine/conditions.py` (Task 11): `ConditionRunner(root, node_repo, cache_repo, ttl, timeout).unmet(node_id, stage) -> list[Condition]` (each command in its own process group, killed at the timeout as exit 124, `TM_ROOT` exported); `is_executable(command) -> bool`.
- `taskmanager/engine/config.py` (Task 11): `ProjectConfig` gains `max_fix_rounds: FixRounds(task=2, container=3)`, `max_merge_attempts=3`, `max_step_failures=3`, `condition_ttl=300`, `condition_timeout=60`, `red_target_decision_after=3600`, `lease_ttl: dict[str, int] | int`, `repo_order: list[str] = []`, `repos: dict[str, RepoConfig]`, `RepoConfig.gates: dict[str, Gate]` keyed `main`/`parent`, `Gate(command, junit=None, timeout=3600)`; `ProjectConfig.lease_ttl_for(action) -> int`, `ConfigStore.project() -> ProjectConfig`, `ConfigStore.lease_ttl(action, flag=None) -> int`. `lease_ttl`, `repos` and `repo_order` are set as one YAML value each.
- `taskmanager/engine/decisions.py` (Task 12): `open_failed_decision(ops, node_id, reason, evidence) -> str`, `open_stranded_decision(ops, node_id, status, dependents) -> str` (both add the `depends_on` edge from each blocked node, and work inside an open transaction), `apply_effect(ops, decision_id, effect) -> list[str]`, `stranded_dependents(ops, node_id) -> list[str]`, `roll_up_ancestors(node_repo, node_id) -> list[tuple[str, Status]]`; `DecisionOption.effect`; `--option "key|Label|description|effect"`; `Operations(…, actor, job_repo: JobRepository | None = None)` gains `busy(node_id)` and `append_section(node_id, key, text)`; `add_decision`/`link_decision` accept any non-decision node; `answer_decision` applies the chosen option's effect. Stored decision statuses switch to `DecisionStatus` in Task 17.
- `taskmanager/engine/routing.py` (Task 13): `model_for(action: Action, node: Node, fix_round: int) -> str` returns a family (`haiku` < `sonnet` < `opus` < `fable`); a REJECT fix at `fix_round >= 3` goes to the strongest acceptable family, never below `opus`; a `MERGE_FAILED` fix, merge and sync agents get `sonnet`.
- `taskmanager/engine/claims.py` (Task 13): `ClaimResult(action, reason, model, job, repos, branch, base, worktree, worktrees: dict[str, str])`; `DecisionSpec(question, options=[], recommend=None)`; `Blocker(depends: list[str], decision: DecisionSpec | None, condition: Condition | None)`; `Claims(root, config, ops, jobs, snapshots, conditions)`, `Claims.open(root, config=None)`; `.start(node_id, agent, session, ttl=None, worktree_dir: Path | None = None) -> ClaimResult` (sweeps first; an implement claim reuses an existing branch cut from the same target); `.complete(node_id, agent=None) -> Status`, `.review(node_id, approve, verdict, agent=None) -> Status`, `.release(node_id, blocked=None, agent=None) -> Status` (with `agent`, refused unless the live lease is that agent's), `.heartbeat(node_id) -> bool`, `.sweep() -> list[str]` (also escalates parked red-target landings), `.reopen`, `.reset`, `.defer`, `.abandon` → `Status`; readers and landing callbacks named in Task 13.
- `taskmanager/engine/gates.py` (Task 14): `render`, `template_hash`, `run_gate(command, cwd, timeout, junit_glob) -> GateRun`, `attribute(tip, base) -> Literal["push", "own_defect", "red_target", "unattributed"]`; re-exports `GateRun` from `core.models`; `python -m taskmanager.engine.gates red-target --root … --repo … --sha … --template-hash …` is the red-target condition's command, stored as an ordinary landing-stage condition.
- `taskmanager/engine/landing.py` (Tasks 14–15): `Landing(root, config, claims, cache, jobs, detach=True)` (sets `claims.landing`), `Landing.open(root)`; `.start_land(node_id) -> str`, `.start_sync(node_id, pairs) -> str`, `.run(job_id) -> JobState`, `.resume(job_id, own_defect=None, push=False) -> JobState`; `python -m taskmanager.engine.landing run <job_id> --root <root>`. A moved target at push or compare-and-swap is merged in and re-gated; refused pushes and swaps share one budget of 3 per job.
- `taskmanager/engine/git.py` gains `is_ancestor`, `diff_quiet`, `rev_parse`, `fetch`, `ensure_branch`, `rename_branch` (Task 13) and `merge_no_ff`, `update_ref_cas`, `ls_remote`, `push`, `add_detached_worktree` (Task 14).
- `taskmanager/engine/discovery.py` (Task 16): `discover(claims, specs: list[str] | None, session, slots, max_strong, exclude=None) -> tuple[str, int]`, `djb2` (moved from `wave.py`, which re-exports it until Task 21); chosen entries `{id, kind, action, model, repos, requires, job, migration}`; strong slots counted by `Lease.model`; the migration chain granted in `stepgraph.migration_order`.
- CLI additions consumed by `tm-wave.js` (Task 18): `tm task start … --worktree-dir <dir>`, `tm task get` printing `next_action` (`lifecycle.next_action` for a stable status, `merge` for a `MERGING` node whose job waits for an agent, else null), `tm task complete|review|release … --agent <agent>`, `tm job status <job> --wait <seconds>`.

## Task map

| # | Task | Group |
|---|---|---|
| 0 | Green baseline on origin/main's gates | Baseline |
| 1 | Status vocabulary | Pure rules |
| 2 | State machine | Pure rules |
| 3 | Container rollup | Pure rules |
| 4 | Display and phase | Pure rules |
| 5 | Landing and base chains | Pure rules |
| 6 | Expanded step graph and cycle check | Pure rules |
| 7 | Write-time validation | Pure rules |
| 8 | Fresh storage: files, tombstones, archive, node columns, conditions | Storage |
| 9 | Coordination tables: leases in state.db, atomic claim, jobs, branch locks, cache | Storage |
| 10 | Snapshot builder | Engine |
| 11 | Config keys and condition runner | Engine |
| 12 | Decisions on any node, option effects, FAILED and stranded decisions | Engine |
| 13 | Claims engine and model routing | Engine |
| 14 | Gates, baselines and landing jobs | Engine |
| 15 | Sync jobs and container landings across repositories | Engine |
| 16 | Discovery | Engine |
| 17 | Operations and import/export on the new model, validation on every write | Switch |
| 18 | CLI verbs | Switch |
| 19 | Web API | Switch |
| 20 | Web UI | Switch |
| 21 | Delete the old vocabulary and add the status CHECK | Switch |
| 22 | `tm-wave.js` | Workflow |
| 23 | Guides | Docs |
| 24 | Skills, commands, README, versions | Docs |
| 25 | SocialSrc cutover preparation | Docs |
| 26 | End-to-end run on a scratch estate | Release |
| 27 | Branch review and publishing | Release |

---

### Task 0: Green baseline on origin/main's gates

**Spec:** §11 (the repository's gates run at every step)
**Files:**
- Modify: `tests/unit/test_search_engine.py` (the `_cli` helper only; no assertion changes)
- Modify: `src/taskmanager/engine/heuristics.py`
- Modify: `src/taskmanager/engine/wave.py`
- Reformat: `docs/superpowers/specs/2026-09-22-decisions-and-web-editing.md`, `tests/unit/test_heuristics.py`, `tests/unit/test_repos.py`

**Interfaces:**
- Consumes: nothing
- Produces: nothing new; every later task's "all four gates exit 0" starts from the green tree this task leaves

Measured at `origin/main` = `23792eb` in a throwaway clone. Four causes across the three red gates:

1. **Six `test_search_of_hostile_text_exits_zero` cases exit 2.** The CLI prints ``Invalid value: no .taskmanager at <cwd>: pass -C, set TM_ROOT, or run `tm init` there``. The test's `_cli` helper appends `-C <root>` after the arguments. These six are the only calls that pass `--`, which lets the query start with `-`. After `--` every word is positional, so `-C` and the path become query words and `tm` resolves its root from the working directory. The CLI is right: that is what `--` means. The defect is the helper's argument order, and the fix goes in the helper; the assertion stays exactly as written.
   - The result depends on the working directory. From a directory with no estate the cases exit 2. From one holding an empty `.taskmanager` they exit 1. From a worktree of `/Users/taigo.pedrosa/Documents/TaskManager`, `_get_root` (read, not run) follows `git rev-parse --git-common-dir` to the primary checkout and would find its live `.taskmanager`: the six would pass by searching the live estate for `- -C <tmp path>`, a `tm` command without a temporary estate, which the Global Constraints forbid.
   - So the red step runs them from an empty directory beside the worktree, and the green step runs them from both places.
2. **mypy `heuristics.py:167 [arg-type]`.** `plan_spec.get(parent_plan_id)` looks up a `str | None`. The `None` lookup is deliberate: the comment above it says a task with no plan matches the `"none"` spec sentinel because `.get(None) is None`. The fix is the dict's declared type, not a guard that would change which tasks match.
3. **mypy `wave.py:120 [assignment]`.** `for task_id, status in candidates.items()` reuses the name `status` that the earlier `for status in UNMERGED_STATUSES` and `for status in ENTRY_STATUSES` loops bound as a `NodeStatus`, but a candidate's status is a `str`. The loop variable gets its own name at its four uses.
4. **Formatting.** The lockfile's ruff (0.16.8) formats the Python code blocks inside Markdown as well as `.py` files, which is why a spec document is on the list. `ruff format` on the three files changes layout only.

- [ ] **Step 1: Watch the three gates fail**, each in the foreground:

```bash
mkdir -p <worktree>/../tm-no-estate
uv run --project <worktree> --directory <worktree>/../tm-no-estate pytest <worktree>/tests/unit/test_search_engine.py -q -p no:cacheprovider -k hostile_text; echo $?
uv run --directory <worktree> mypy; echo $?
uv run --directory <worktree> ruff format --check; echo $?
```

Expected:
- pytest: six `FAILED ../<worktree dir name>/tests/unit/test_search_engine.py::test_search_of_hostile_text_exits_zero[<query>] - assert (2 == 0)` lines, one each for `-`, `"`, `AND`, `a:`, `(` and the empty query, then `6 failed, 62 deselected`, and exit `1`.
- mypy: exit `1`, with these lines:

```
src/taskmanager/engine/heuristics.py:167: error: Argument 1 to "get" of "dict" has incompatible type "str | None"; expected "str"  [arg-type]
src/taskmanager/engine/wave.py:120: error: Incompatible types in assignment (expression has type "str", variable has type "NodeStatus")  [assignment]
Found 2 errors in 2 files (checked 42 source files)
```

- ruff: `3 files would be reformatted, 81 files already formatted` and exit `1`.

- [ ] **Step 2: Put `-C` before `--` in the test helper.** In `tests/unit/test_search_engine.py`, replace

```python
def _cli(root: Path, *args: str) -> tuple[int, str]:
    res = runner.invoke(app, [*args, "-C", str(root)])
    return res.exit_code, res.stdout
```

with

```python
def _cli(root: Path, *args: str) -> tuple[int, str]:
    # Everything after `--` is a query word, so -C has to come before it.
    cut = args.index("--") if "--" in args else len(args)
    res = runner.invoke(app, [*args[:cut], "-C", str(root), *args[cut:]])
    return res.exit_code, res.stdout
```

- [ ] **Step 3: Declare the `None` key the spec lookup relies on.** In `src/taskmanager/engine/heuristics.py`, `RecommendationEngine.get_next_tasks`, replace

```python
        plan_spec = (
```

with

```python
        plan_spec: dict[str | None, str | None] = (
```

- [ ] **Step 4: Give the candidate loop's status its own name.** In `src/taskmanager/engine/wave.py`, `discover_batch`, make these four replacements; each old line occurs exactly once in the file.

```python
    for task_id, status in candidates.items():
```

becomes

```python
    for task_id, entry_status in candidates.items():
```

```python
        if status == "READY" and _writes_migration(files) and repo in chain_held:
```

becomes

```python
        if entry_status == "READY" and _writes_migration(files) and repo in chain_held:
```

```python
                "status": status,
```

becomes

```python
                "status": entry_status,
```

```python
        if status == "READY" and _writes_migration(files):
```

becomes

```python
        if entry_status == "READY" and _writes_migration(files):
```

Then `grep -n "status" <worktree>/src/taskmanager/engine/wave.py` shows no bare `status` after the `for task_id, entry_status` line.

- [ ] **Step 5: Format the three files**

```bash
uv run --directory <worktree> ruff format docs/superpowers/specs/2026-09-22-decisions-and-web-editing.md tests/unit/test_heuristics.py tests/unit/test_repos.py; echo $?
```

Expected: `3 files reformatted` and `0`.

- [ ] **Step 6: Run the tests and the gates**, each in the foreground:

```bash
uv run --project <worktree> --directory <worktree>/../tm-no-estate pytest <worktree>/tests/unit/test_search_engine.py -q -p no:cacheprovider -k hostile_text; echo $?
uv run --directory <worktree> pytest -q; echo $?
uv run --directory <worktree> ruff check; echo $?
uv run --directory <worktree> ruff format --check; echo $?
uv run --directory <worktree> mypy; echo $?
rmdir <worktree>/../tm-no-estate
```

Expected:
- the hostile-text cases from the empty directory: `6 passed, 62 deselected` and `0` (the full-suite run below covers them from the worktree);
- the full suite: `721 passed` (at `23792eb`), no failures, and `0`;
- `ruff check`: `All checks passed!` and `0`;
- `ruff format --check`: `84 files already formatted` and `0`;
- `mypy`: `Success: no issues found in 42 source files` and `0`.

- [ ] **Step 7: Commit**, one cause per commit:

```bash
git -C <worktree> add tests/unit/test_search_engine.py && git -C <worktree> commit -m "fix(tests): pass -C before -- so hostile-text searches open their own estate"
git -C <worktree> add src/taskmanager/engine/heuristics.py src/taskmanager/engine/wave.py && git -C <worktree> commit -m "fix(engine): type the no-plan spec lookup and keep the candidate loop off a NodeStatus name"
git -C <worktree> add docs/superpowers/specs/2026-09-22-decisions-and-web-editing.md tests/unit/test_heuristics.py tests/unit/test_repos.py && git -C <worktree> commit -m "style: ruff format the three files origin/main left unformatted"
```

### Task 1: Status vocabulary

**Spec:** §2.1, §2.2, §4.2 (effects), §4.3 (stages), §5.2 (actions), §6.2 (jobs), §7.1, §7.2
**Files:**
- Create: `src/taskmanager/core/status.py`
- Test: `tests/unit/test_status.py`

**Interfaces:**
- Consumes: `NodeStatus` from `taskmanager/core/enums.py` (only to pin that shared names compare equal)
- Produces: `Status`, `IN_STEP`, `EXITS`, `STABLE`, `SET_ASIDE`, `DecisionStatus`, `Outcome`, `Merge`, `Phase`, `DisplayStatus`, `Action`, `Event`, `JobKind`, `JobState`, `ConditionStage`, `DecisionEffect`

The vocabulary every later task imports. Nothing reads it yet, so the old `NodeStatus`/`VirtualStatus` stay untouched; the last test pins the Global Constraint that a name both vocabularies share compares equal across them, which Tasks 8-20 rely on while `Node.status` is typed `NodeStatus | Status`.

- [ ] **Step 1: Write the failing test** — `tests/unit/test_status.py`:

```python
from enum import StrEnum

import pytest

from taskmanager.core.enums import NodeStatus
from taskmanager.core.status import (
    EXITS,
    IN_STEP,
    SET_ASIDE,
    STABLE,
    Action,
    ConditionStage,
    DecisionEffect,
    DecisionStatus,
    DisplayStatus,
    Event,
    JobKind,
    JobState,
    Merge,
    Outcome,
    Phase,
    Status,
)


@pytest.mark.parametrize(
    ("enum", "values"),
    [
        (
            Status,
            [
                "READY",
                "IMPLEMENTING",
                "IMPLEMENTED",
                "REVIEWING",
                "REVIEWED",
                "FIXING",
                "FIXED",
                "MERGING",
                "COMPLETED",
                "FAILED",
                "DEFERRED",
                "ABANDONED",
                "SUPERSEDED",
            ],
        ),
        (DecisionStatus, ["OPEN", "ANSWERED", "WITHDRAWN"]),
        (Outcome, ["approve", "reject", "merge_failed"]),
        (Merge, ["parent", "main"]),
        (
            Phase,
            ["QUEUED", "DISPATCHED", "COMPLETED", "FAILED", "DEFERRED", "ABANDONED", "SUPERSEDED"],
        ),
        (
            DisplayStatus,
            [
                "READY",
                "IMPLEMENTING",
                "REVIEWING",
                "FIXING",
                "MERGING",
                "COMPLETED",
                "FAILED",
                "DEFERRED",
                "ABANDONED",
                "SUPERSEDED",
                "WAITING_REVIEW",
                "WAITING_FIX",
                "WAITING_MERGE",
                "WAITING_MERGE_AGENT",
                "STALE",
                "AWAITING_DECISION",
                "BLOCKED_BY_TASK",
                "BLOCKED_BY_CONDITION",
                "BLOCKED_BY_SYNC",
                "BLOCKED_BY_LEASE",
            ],
        ),
        (Action, ["implement", "review", "fix", "merge", "sync", "blocked"]),
        (
            Event,
            [
                "complete",
                "approve",
                "reject",
                "landed",
                "own_defect",
                "release",
                "release_blocked",
                "expired",
            ],
        ),
        (JobKind, ["land", "sync"]),
        (
            JobState,
            ["running", "needs_agent", "succeeded", "own_defect", "condition_unmet", "expired"],
        ),
        (ConditionStage, ["claim", "landing"]),
        (DecisionEffect, ["none", "abandon", "defer", "reopen", "drop_edge"]),
    ],
)
def test_each_vocabulary_holds_exactly_its_stored_values(
    enum: type[StrEnum], values: list[str]
) -> None:
    assert [member.value for member in enum] == values


@pytest.mark.parametrize("enum", [Status, DecisionStatus, Phase, DisplayStatus])
def test_upper_case_vocabularies_store_their_member_name(enum: type[StrEnum]) -> None:
    assert all(member.value == member.name for member in enum)


def test_every_stored_status_is_exactly_one_of_in_step_stable_or_exit() -> None:
    assert IN_STEP | STABLE | EXITS == frozenset(Status)
    assert not IN_STEP & STABLE
    assert not IN_STEP & EXITS
    assert not STABLE & EXITS


def test_in_step_statuses_are_the_ing_statuses() -> None:
    assert IN_STEP == {s for s in Status if s.value.endswith("ING")}


def test_a_rollup_sets_aside_exactly_the_exits() -> None:
    assert SET_ASIDE == {Status.DEFERRED, Status.ABANDONED, Status.SUPERSEDED}


@pytest.mark.parametrize(
    "name",
    [
        "IMPLEMENTING",
        "REVIEWING",
        "FIXING",
        "MERGING",
        "COMPLETED",
        "DEFERRED",
        "ABANDONED",
        "SUPERSEDED",
    ],
)
def test_a_status_both_vocabularies_name_compares_equal_across_them(name: str) -> None:
    assert NodeStatus[name] == Status[name]
```

- [ ] **Step 2: Run it and watch it fail**

```bash
uv run --directory <worktree> pytest tests/unit/test_status.py -q; echo $?
```

Expected: collection stops with `ModuleNotFoundError: No module named 'taskmanager.core.status'`, `1 error`, exit code `2`.

- [ ] **Step 3: Implement** — `src/taskmanager/core/status.py`:

```python
from enum import StrEnum


class Status(StrEnum):
    READY = "READY"
    IMPLEMENTING = "IMPLEMENTING"
    IMPLEMENTED = "IMPLEMENTED"
    REVIEWING = "REVIEWING"
    REVIEWED = "REVIEWED"
    FIXING = "FIXING"
    FIXED = "FIXED"
    MERGING = "MERGING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    DEFERRED = "DEFERRED"
    ABANDONED = "ABANDONED"
    SUPERSEDED = "SUPERSEDED"


IN_STEP: frozenset[Status] = frozenset(
    {Status.IMPLEMENTING, Status.REVIEWING, Status.FIXING, Status.MERGING}
)
EXITS: frozenset[Status] = frozenset({Status.DEFERRED, Status.ABANDONED, Status.SUPERSEDED})
STABLE: frozenset[Status] = frozenset(
    {
        Status.READY,
        Status.IMPLEMENTED,
        Status.REVIEWED,
        Status.FIXED,
        Status.COMPLETED,
        Status.FAILED,
    }
)
# A set-aside child can never complete, so a container's rollup counts it on neither side.
SET_ASIDE = EXITS


class DecisionStatus(StrEnum):
    OPEN = "OPEN"
    ANSWERED = "ANSWERED"
    WITHDRAWN = "WITHDRAWN"


class Outcome(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    MERGE_FAILED = "merge_failed"


class Merge(StrEnum):
    PARENT = "parent"
    MAIN = "main"


class Phase(StrEnum):
    QUEUED = "QUEUED"
    DISPATCHED = "DISPATCHED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    DEFERRED = "DEFERRED"
    ABANDONED = "ABANDONED"
    SUPERSEDED = "SUPERSEDED"


class DisplayStatus(StrEnum):
    READY = "READY"
    IMPLEMENTING = "IMPLEMENTING"
    REVIEWING = "REVIEWING"
    FIXING = "FIXING"
    MERGING = "MERGING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    DEFERRED = "DEFERRED"
    ABANDONED = "ABANDONED"
    SUPERSEDED = "SUPERSEDED"
    WAITING_REVIEW = "WAITING_REVIEW"
    WAITING_FIX = "WAITING_FIX"
    WAITING_MERGE = "WAITING_MERGE"
    WAITING_MERGE_AGENT = "WAITING_MERGE_AGENT"
    STALE = "STALE"
    AWAITING_DECISION = "AWAITING_DECISION"
    BLOCKED_BY_TASK = "BLOCKED_BY_TASK"
    BLOCKED_BY_CONDITION = "BLOCKED_BY_CONDITION"
    BLOCKED_BY_SYNC = "BLOCKED_BY_SYNC"
    BLOCKED_BY_LEASE = "BLOCKED_BY_LEASE"


class Action(StrEnum):
    IMPLEMENT = "implement"
    REVIEW = "review"
    FIX = "fix"
    MERGE = "merge"
    SYNC = "sync"
    BLOCKED = "blocked"


class Event(StrEnum):
    COMPLETE = "complete"
    APPROVE = "approve"
    REJECT = "reject"
    LANDED = "landed"
    OWN_DEFECT = "own_defect"
    RELEASE = "release"
    RELEASE_BLOCKED = "release_blocked"
    EXPIRED = "expired"


class JobKind(StrEnum):
    LAND = "land"
    SYNC = "sync"


class JobState(StrEnum):
    RUNNING = "running"
    NEEDS_AGENT = "needs_agent"
    SUCCEEDED = "succeeded"
    OWN_DEFECT = "own_defect"
    CONDITION_UNMET = "condition_unmet"
    # The job's lease was swept or released before the job finished: a killed job must not
    # read as running forever and hold its node.
    EXPIRED = "expired"


class ConditionStage(StrEnum):
    CLAIM = "claim"
    LANDING = "landing"


class DecisionEffect(StrEnum):
    NONE = "none"
    ABANDON = "abandon"
    DEFER = "defer"
    REOPEN = "reopen"
    DROP_EDGE = "drop_edge"
```

- [ ] **Step 4: Run the tests and the gates**, each in the foreground:

```bash
uv run --directory <worktree> pytest tests/unit/test_status.py -q; echo $?
uv run --directory <worktree> pytest -q; echo $?
uv run --directory <worktree> ruff check; echo $?
uv run --directory <worktree> ruff format --check; echo $?
uv run --directory <worktree> mypy; echo $?
```

Expected: `27 passed` and `0` for the first; every test passing and `0` for the full suite; `All checks passed!` and `0` for `ruff check`; nothing to reformat and `0` for `ruff format --check`; `Success: no issues found` and `0` for `mypy`. No existing test changes: nothing reads this module yet.

- [ ] **Step 5: Commit**

```bash
git -C <worktree> add src/taskmanager/core/status.py tests/unit/test_status.py && git -C <worktree> commit -m "feat(core): add the stored status vocabulary with derived phase and display names"
```

### Task 2: State machine

**Spec:** §3.1 (the cycle), §3.2, §3.4 (reopen, reset, defer, abandon), §5.5 (the fix round)
**Files:**
- Create: `src/taskmanager/core/lifecycle.py`
- Test: `tests/unit/test_lifecycle.py`

**Interfaces:**
- Consumes: `Status`, `IN_STEP`, `STABLE`, `Action`, `Event`, `Outcome` (Task 1)
- Produces: `Cycle`, `Caps`, `LifecycleError`, `next_action`, `claim`, `advance`, `fix_round`, `reopen`, `defer`, `abandon`, `reset`

Every row of §3.1 is a parametrize row with its full expected `Cycle`; every (status, event) pair with no row is enumerated and must raise. Behaviour the contract settles beyond the table, each pinned by a test:

- `next_action` returns None for a node in a step: handing a stopped landing to a new agent is decided by `Claims` from the job row, not here.
- `fix_round` is 1-based: `review_cycles` for a fix answering `reject` (so a container's third fix is round 3, which `model_for` routes to `opus` or above), 0 for a fix answering `merge_failed`.
- A review released or expired without a verdict refunds the cycle its claim counted, so an agent dying mid-review does not use up a fix round.
- `RELEASE` and `EXPIRED` on `READY`, `IMPLEMENTED`, `REVIEWED` or `FIXED` keep the status and add one step failure, `FAILED` at the cap: a sync job's agent works for a node that was never claimed (§5.2), and leaving the job unresolved still counts (§6.2). `RELEASE_BLOCKED` there changes nothing, so a sync agent can name what the node waits on. `COMPLETED` and `FAILED` refuse all three: landed code cannot fail a step, and a failed node is already at its end.

- [ ] **Step 1: Write the failing test** — `tests/unit/test_lifecycle.py`:

```python
import itertools

import pytest

from taskmanager.core.lifecycle import (
    Caps,
    Cycle,
    LifecycleError,
    abandon,
    advance,
    claim,
    defer,
    fix_round,
    next_action,
    reopen,
    reset,
)
from taskmanager.core.status import IN_STEP, Action, Event, Outcome, Status

S = Status
APPROVE = Outcome.APPROVE
REJECT = Outcome.REJECT
MERGE_FAILED = Outcome.MERGE_FAILED
E = Event
CAPS = Caps()
NO_FIX = {"review": True, "fix": False}
UNREVIEWED = {"review": False, "fix": False}


@pytest.mark.parametrize(
    ("cycle", "action"),
    [
        (Cycle(S.READY), Action.IMPLEMENT),
        (Cycle(S.READY, container=True), None),
        (Cycle(S.IMPLEMENTED), Action.REVIEW),
        (Cycle(S.IMPLEMENTED, **UNREVIEWED), Action.MERGE),
        (Cycle(S.IMPLEMENTED, container=True), Action.REVIEW),
        (Cycle(S.REVIEWED, outcome=APPROVE), Action.MERGE),
        (Cycle(S.REVIEWED, outcome=REJECT), Action.FIX),
        (Cycle(S.REVIEWED, outcome=REJECT, **NO_FIX), Action.MERGE),
        (Cycle(S.REVIEWED, outcome=MERGE_FAILED), Action.FIX),
        (Cycle(S.FIXED, fix_for=REJECT), Action.REVIEW),
        (Cycle(S.FIXED, fix_for=MERGE_FAILED), Action.REVIEW),
        (Cycle(S.IMPLEMENTING), None),
        (Cycle(S.REVIEWING), None),
        (Cycle(S.FIXING), None),
        (Cycle(S.MERGING), None),
        (Cycle(S.COMPLETED), None),
        (Cycle(S.FAILED), None),
        (Cycle(S.DEFERRED), None),
        (Cycle(S.ABANDONED), None),
        (Cycle(S.SUPERSEDED), None),
    ],
)
def test_next_action_follows_the_status_and_flags(cycle: Cycle, action: Action | None) -> None:
    assert next_action(cycle) == action


def test_reviewed_without_an_outcome_is_refused_with_the_reset_fix() -> None:
    with pytest.raises(LifecycleError, match="--outcome"):
        next_action(Cycle(S.REVIEWED))


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (Cycle(S.READY), Cycle(S.IMPLEMENTING, claimed_from=S.READY)),
        (
            Cycle(S.IMPLEMENTED),
            Cycle(S.REVIEWING, claimed_from=S.IMPLEMENTED, review_cycles=1),
        ),
        (
            Cycle(S.IMPLEMENTED, **UNREVIEWED),
            Cycle(S.MERGING, claimed_from=S.IMPLEMENTED, **UNREVIEWED),
        ),
        (
            Cycle(S.IMPLEMENTED, container=True),
            Cycle(S.REVIEWING, container=True, claimed_from=S.IMPLEMENTED, review_cycles=1),
        ),
        (
            Cycle(S.REVIEWED, outcome=REJECT, review_cycles=1),
            Cycle(
                S.FIXING,
                outcome=REJECT,
                fix_for=REJECT,
                claimed_from=S.REVIEWED,
                review_cycles=1,
            ),
        ),
        (
            Cycle(S.REVIEWED, outcome=MERGE_FAILED, review_cycles=1, merge_attempts=1),
            Cycle(
                S.FIXING,
                outcome=MERGE_FAILED,
                fix_for=MERGE_FAILED,
                claimed_from=S.REVIEWED,
                review_cycles=1,
                merge_attempts=1,
            ),
        ),
        (
            Cycle(S.REVIEWED, outcome=REJECT, review_cycles=1, **NO_FIX),
            Cycle(
                S.MERGING,
                outcome=REJECT,
                claimed_from=S.REVIEWED,
                review_cycles=1,
                **NO_FIX,
            ),
        ),
        (
            Cycle(S.REVIEWED, outcome=APPROVE, review_cycles=1),
            Cycle(S.MERGING, outcome=APPROVE, claimed_from=S.REVIEWED, review_cycles=1),
        ),
        (
            Cycle(S.FIXED, outcome=REJECT, fix_for=REJECT, review_cycles=1),
            Cycle(
                S.REVIEWING,
                outcome=REJECT,
                fix_for=REJECT,
                claimed_from=S.FIXED,
                review_cycles=2,
            ),
        ),
        (
            Cycle(S.FIXED, outcome=MERGE_FAILED, fix_for=MERGE_FAILED, review_cycles=1),
            Cycle(
                S.REVIEWING,
                outcome=MERGE_FAILED,
                fix_for=MERGE_FAILED,
                claimed_from=S.FIXED,
                review_cycles=1,
            ),
        ),
    ],
)
def test_a_claim_enters_the_next_step_and_counts_only_review_rounds(
    before: Cycle, after: Cycle
) -> None:
    assert claim(before) == after


def test_a_claim_leaves_step_failures_to_the_step_that_ends() -> None:
    assert claim(Cycle(S.READY, step_failures=2)).step_failures == 2


@pytest.mark.parametrize(
    "cycle",
    [
        Cycle(S.READY, container=True),
        Cycle(S.IMPLEMENTING),
        Cycle(S.REVIEWING),
        Cycle(S.FIXING),
        Cycle(S.MERGING),
        Cycle(S.COMPLETED),
        Cycle(S.FAILED),
        Cycle(S.DEFERRED),
        Cycle(S.ABANDONED),
        Cycle(S.SUPERSEDED),
    ],
)
def test_a_claim_with_no_next_action_is_refused(cycle: Cycle) -> None:
    with pytest.raises(LifecycleError, match="no step to claim"):
        claim(cycle)


@pytest.mark.parametrize(
    ("before", "event", "after"),
    [
        (
            Cycle(S.IMPLEMENTING, claimed_from=S.READY, step_failures=2),
            E.COMPLETE,
            Cycle(S.IMPLEMENTED),
        ),
        (
            Cycle(S.FIXING, outcome=REJECT, fix_for=REJECT, claimed_from=S.REVIEWED),
            E.COMPLETE,
            Cycle(S.FIXED, outcome=REJECT, fix_for=REJECT),
        ),
        (
            Cycle(S.REVIEWING, claimed_from=S.IMPLEMENTED, review_cycles=1, step_failures=1),
            E.APPROVE,
            Cycle(S.REVIEWED, outcome=APPROVE, review_cycles=1),
        ),
        (
            Cycle(S.REVIEWING, claimed_from=S.IMPLEMENTED, review_cycles=1),
            E.REJECT,
            Cycle(S.REVIEWED, outcome=REJECT, review_cycles=1),
        ),
        (
            Cycle(S.REVIEWING, claimed_from=S.FIXED, fix_for=REJECT, review_cycles=2),
            E.REJECT,
            Cycle(S.REVIEWED, outcome=REJECT, fix_for=REJECT, review_cycles=2),
        ),
        (
            Cycle(S.REVIEWING, claimed_from=S.FIXED, fix_for=REJECT, review_cycles=3),
            E.REJECT,
            Cycle(S.FAILED, outcome=REJECT, fix_for=REJECT, review_cycles=3),
        ),
        (
            Cycle(
                S.REVIEWING,
                container=True,
                claimed_from=S.FIXED,
                fix_for=REJECT,
                review_cycles=3,
            ),
            E.REJECT,
            Cycle(
                S.REVIEWED,
                container=True,
                outcome=REJECT,
                fix_for=REJECT,
                review_cycles=3,
            ),
        ),
        (
            Cycle(
                S.REVIEWING,
                container=True,
                claimed_from=S.FIXED,
                fix_for=REJECT,
                review_cycles=4,
            ),
            E.REJECT,
            Cycle(
                S.FAILED,
                container=True,
                outcome=REJECT,
                fix_for=REJECT,
                review_cycles=4,
            ),
        ),
        (
            Cycle(S.REVIEWING, claimed_from=S.IMPLEMENTED, review_cycles=5, **NO_FIX),
            E.REJECT,
            Cycle(S.REVIEWED, outcome=REJECT, review_cycles=5, **NO_FIX),
        ),
        (
            Cycle(S.MERGING, outcome=REJECT, claimed_from=S.REVIEWED, **NO_FIX),
            E.LANDED,
            Cycle(S.COMPLETED, outcome=REJECT, **NO_FIX),
        ),
        (
            Cycle(S.MERGING, outcome=APPROVE, claimed_from=S.REVIEWED),
            E.OWN_DEFECT,
            Cycle(S.REVIEWED, outcome=MERGE_FAILED, merge_attempts=1),
        ),
        (
            Cycle(S.MERGING, outcome=APPROVE, claimed_from=S.REVIEWED, merge_attempts=2),
            E.OWN_DEFECT,
            Cycle(S.REVIEWED, outcome=MERGE_FAILED, merge_attempts=3),
        ),
        (
            Cycle(S.MERGING, outcome=APPROVE, claimed_from=S.REVIEWED, merge_attempts=3),
            E.OWN_DEFECT,
            Cycle(S.FAILED, outcome=MERGE_FAILED, merge_attempts=4),
        ),
        (
            Cycle(S.MERGING, claimed_from=S.IMPLEMENTED, **UNREVIEWED),
            E.OWN_DEFECT,
            Cycle(S.FAILED, outcome=MERGE_FAILED, merge_attempts=1, **UNREVIEWED),
        ),
        (
            Cycle(S.IMPLEMENTING, claimed_from=S.READY),
            E.RELEASE,
            Cycle(S.READY, step_failures=1),
        ),
        (
            Cycle(S.REVIEWING, claimed_from=S.IMPLEMENTED, review_cycles=1),
            E.RELEASE,
            Cycle(S.IMPLEMENTED, review_cycles=0, step_failures=1),
        ),
        (
            Cycle(S.REVIEWING, claimed_from=S.FIXED, fix_for=REJECT, review_cycles=2),
            E.EXPIRED,
            Cycle(S.FIXED, fix_for=REJECT, review_cycles=1, step_failures=1),
        ),
        (
            Cycle(
                S.REVIEWING,
                claimed_from=S.FIXED,
                fix_for=MERGE_FAILED,
                review_cycles=1,
            ),
            E.RELEASE,
            Cycle(S.FIXED, fix_for=MERGE_FAILED, review_cycles=1, step_failures=1),
        ),
        (
            Cycle(S.MERGING, outcome=APPROVE, claimed_from=S.REVIEWED),
            E.EXPIRED,
            Cycle(S.REVIEWED, outcome=APPROVE, step_failures=1),
        ),
        (
            Cycle(S.FIXING, outcome=REJECT, fix_for=REJECT, claimed_from=S.REVIEWED),
            E.RELEASE_BLOCKED,
            Cycle(S.REVIEWED, outcome=REJECT, fix_for=REJECT),
        ),
        (
            Cycle(S.REVIEWING, claimed_from=S.IMPLEMENTED, review_cycles=1, step_failures=1),
            E.RELEASE_BLOCKED,
            Cycle(S.IMPLEMENTED, review_cycles=0, step_failures=1),
        ),
        (
            Cycle(S.IMPLEMENTING, claimed_from=S.READY, step_failures=2),
            E.RELEASE,
            Cycle(S.FAILED, step_failures=3),
        ),
        (
            Cycle(S.MERGING, outcome=APPROVE, claimed_from=S.REVIEWED, step_failures=2),
            E.EXPIRED,
            Cycle(S.FAILED, outcome=APPROVE, step_failures=3),
        ),
        (Cycle(S.READY), E.RELEASE, Cycle(S.READY, step_failures=1)),
        (
            Cycle(S.FIXED, fix_for=REJECT, review_cycles=2),
            E.EXPIRED,
            Cycle(S.FIXED, fix_for=REJECT, review_cycles=2, step_failures=1),
        ),
        (
            Cycle(S.REVIEWED, outcome=APPROVE, step_failures=2),
            E.EXPIRED,
            Cycle(S.FAILED, outcome=APPROVE, step_failures=3),
        ),
        (
            Cycle(S.IMPLEMENTED, step_failures=1),
            E.RELEASE_BLOCKED,
            Cycle(S.IMPLEMENTED, step_failures=1),
        ),
    ],
)
def test_an_event_moves_the_node_along_its_row(before: Cycle, event: Event, after: Cycle) -> None:
    assert advance(before, event, CAPS) == after


_ACCEPTED = {
    (S.IMPLEMENTING, E.COMPLETE),
    (S.FIXING, E.COMPLETE),
    (S.REVIEWING, E.APPROVE),
    (S.REVIEWING, E.REJECT),
    (S.MERGING, E.LANDED),
    (S.MERGING, E.OWN_DEFECT),
    *itertools.product(
        IN_STEP | {S.READY, S.IMPLEMENTED, S.REVIEWED, S.FIXED},
        (E.RELEASE, E.RELEASE_BLOCKED, E.EXPIRED),
    ),
}


@pytest.mark.parametrize(
    ("status", "event"),
    [pair for pair in itertools.product(Status, Event) if pair not in _ACCEPTED],
)
def test_an_event_its_status_has_no_row_for_is_refused(status: Status, event: Event) -> None:
    claimed_from = S.READY if status in IN_STEP else None
    with pytest.raises(LifecycleError, match="does not accept"):
        advance(Cycle(status, claimed_from=claimed_from), event, CAPS)


def test_a_release_with_no_claimed_from_is_refused_with_the_reset_fix() -> None:
    with pytest.raises(LifecycleError, match="reset"):
        advance(Cycle(S.IMPLEMENTING), E.RELEASE, CAPS)


def _walk(cycle: Cycle, *steps: Event | None) -> Cycle:
    for step in steps:
        cycle = claim(cycle) if step is None else advance(cycle, step, CAPS)
    return cycle


# Claim the step, finish it, claim the review that follows, reject.
REJECTED_ROUND = (None, E.COMPLETE, None, E.REJECT)


def test_a_task_fails_on_the_third_rejection_after_two_fix_rounds() -> None:
    reviewed = _walk(Cycle(S.READY), *REJECTED_ROUND, *REJECTED_ROUND)
    assert reviewed.status == S.REVIEWED
    assert _walk(reviewed, *REJECTED_ROUND).status == S.FAILED


def test_a_container_gets_a_third_fix_round_before_failing() -> None:
    reviewed = _walk(
        Cycle(S.IMPLEMENTED, container=True), None, E.REJECT, *REJECTED_ROUND, *REJECTED_ROUND
    )
    assert reviewed.status == S.REVIEWED
    assert _walk(reviewed, *REJECTED_ROUND).status == S.FAILED


def test_a_review_of_a_landing_fix_uses_no_fix_round() -> None:
    approved = _walk(Cycle(S.READY), None, E.COMPLETE, None, E.APPROVE)
    after_landing_fix = _walk(approved, None, E.OWN_DEFECT, None, E.COMPLETE, None, E.REJECT)
    assert after_landing_fix.status == S.REVIEWED
    assert after_landing_fix.review_cycles == 1


def test_landing_fixes_stop_after_the_attempt_cap() -> None:
    approved = _walk(Cycle(S.READY), None, E.COMPLETE, None, E.APPROVE)
    landing_fix = (None, E.OWN_DEFECT, None, E.COMPLETE, None, E.APPROVE)
    three_fixed = _walk(approved, *landing_fix, *landing_fix, *landing_fix)
    assert (three_fixed.status, three_fixed.merge_attempts) == (S.REVIEWED, 3)
    failed = _walk(three_fixed, None, E.OWN_DEFECT)
    assert (failed.status, failed.merge_attempts) == (S.FAILED, 4)


def test_step_failures_reset_when_a_step_makes_progress() -> None:
    twice_released = _walk(Cycle(S.READY), None, E.RELEASE, None, E.EXPIRED)
    assert twice_released.step_failures == 2
    assert _walk(twice_released, None, E.COMPLETE).step_failures == 0


@pytest.mark.parametrize(
    ("cycle", "round_"),
    [
        (Cycle(S.FIXING, fix_for=REJECT, review_cycles=1), 1),
        (Cycle(S.FIXING, fix_for=REJECT, review_cycles=2), 2),
        (Cycle(S.FIXING, container=True, fix_for=REJECT, review_cycles=3), 3),
        (Cycle(S.FIXING, fix_for=MERGE_FAILED, review_cycles=2), 0),
        (Cycle(S.REVIEWED, outcome=REJECT, review_cycles=2), 2),
        (Cycle(S.REVIEWED, outcome=MERGE_FAILED, review_cycles=2), 0),
    ],
)
def test_fix_round_is_the_review_round_the_fix_answers(cycle: Cycle, round_: int) -> None:
    assert fix_round(cycle) == round_


@pytest.mark.parametrize(
    ("cycle", "children_all_completed", "status"),
    [
        (Cycle(S.FAILED), False, S.READY),
        (Cycle(S.DEFERRED), False, S.READY),
        (Cycle(S.ABANDONED), True, S.READY),
        (Cycle(S.FAILED, container=True), True, S.IMPLEMENTED),
        (Cycle(S.DEFERRED, container=True), False, S.READY),
    ],
)
def test_reopen_restarts_the_cycle_with_cleared_counters(
    cycle: Cycle, children_all_completed: bool, status: Status
) -> None:
    worn = Cycle(
        cycle.status,
        container=cycle.container,
        fix=False,
        outcome=REJECT,
        fix_for=REJECT,
        review_cycles=3,
        merge_attempts=2,
        step_failures=1,
    )
    assert reopen(worn, children_all_completed) == Cycle(
        status, container=cycle.container, fix=False
    )


@pytest.mark.parametrize(
    "status",
    [s for s in Status if s not in (S.FAILED, S.DEFERRED, S.ABANDONED)],
)
def test_reopen_is_refused_outside_failed_deferred_and_abandoned(status: Status) -> None:
    with pytest.raises(LifecycleError, match="reopen"):
        reopen(Cycle(status), children_all_completed=False)


@pytest.mark.parametrize("status", [S.READY, S.IMPLEMENTED, S.REVIEWED, S.FIXED, S.FAILED])
def test_defer_and_abandon_accept_every_stable_status_but_completed(status: Status) -> None:
    assert defer(Cycle(status)).status == S.DEFERRED
    assert abandon(Cycle(status)).status == S.ABANDONED


def test_defer_and_abandon_refuse_landed_code() -> None:
    for verb in (defer, abandon):
        with pytest.raises(LifecycleError, match="landed code"):
            verb(Cycle(S.COMPLETED))


@pytest.mark.parametrize(
    "status",
    [S.IMPLEMENTING, S.REVIEWING, S.FIXING, S.MERGING, S.DEFERRED, S.ABANDONED, S.SUPERSEDED],
)
def test_defer_and_abandon_refuse_a_node_in_a_step_or_already_set_aside(status: Status) -> None:
    for verb in (defer, abandon):
        with pytest.raises(LifecycleError, match="cannot become"):
            verb(Cycle(status))


@pytest.mark.parametrize(
    ("to", "outcome", "after"),
    [
        (S.READY, None, Cycle(S.READY, review_cycles=2)),
        (S.IMPLEMENTED, None, Cycle(S.IMPLEMENTED, review_cycles=2)),
        (S.REVIEWED, REJECT, Cycle(S.REVIEWED, outcome=REJECT, review_cycles=2)),
        (
            S.FIXED,
            MERGE_FAILED,
            Cycle(S.FIXED, outcome=MERGE_FAILED, fix_for=MERGE_FAILED, review_cycles=2),
        ),
        (S.COMPLETED, None, Cycle(S.COMPLETED, review_cycles=2)),
    ],
)
def test_reset_moves_a_stable_node_and_keeps_its_review_count(
    to: Status, outcome: Outcome | None, after: Cycle
) -> None:
    failed = Cycle(S.FAILED, outcome=APPROVE, review_cycles=2, step_failures=3)
    assert reset(failed, to, outcome) == after


@pytest.mark.parametrize(
    ("cycle", "to", "outcome", "message"),
    [
        (Cycle(S.REVIEWING), S.READY, None, "in a step"),
        (Cycle(S.FAILED), S.MERGING, None, "reset goes to"),
        (Cycle(S.FAILED), S.DEFERRED, None, "reset goes to"),
        (Cycle(S.FAILED), S.REVIEWED, None, "needs --outcome"),
        (Cycle(S.FAILED), S.FIXED, None, "needs --outcome"),
        (Cycle(S.FAILED), S.READY, REJECT, "applies to"),
        (Cycle(S.FAILED), S.FIXED, APPROVE, "never approve"),
        (Cycle(S.FAILED, **NO_FIX), S.FIXED, REJECT, "fix off"),
        (Cycle(S.FAILED, **NO_FIX), S.REVIEWED, MERGE_FAILED, "fix off"),
    ],
)
def test_reset_refuses_a_target_the_cycle_cannot_route(
    cycle: Cycle, to: Status, outcome: Outcome | None, message: str
) -> None:
    with pytest.raises(LifecycleError, match=message):
        reset(cycle, to, outcome)
```

- [ ] **Step 2: Run it and watch it fail**

```bash
uv run --directory <worktree> pytest tests/unit/test_lifecycle.py -q; echo $?
```

Expected: collection stops with `ModuleNotFoundError: No module named 'taskmanager.core.lifecycle'`, `1 error`, exit code `2`.

- [ ] **Step 3: Implement** — `src/taskmanager/core/lifecycle.py`:

```python
from dataclasses import dataclass, replace

from taskmanager.core.status import IN_STEP, STABLE, Action, Event, Outcome, Status


@dataclass(frozen=True)
class Cycle:
    status: Status
    container: bool = False
    review: bool = True
    fix: bool = True
    outcome: Outcome | None = None
    fix_for: Outcome | None = None
    claimed_from: Status | None = None
    review_cycles: int = 0
    merge_attempts: int = 0
    step_failures: int = 0


@dataclass(frozen=True)
class Caps:
    fix_rounds_task: int = 2
    fix_rounds_container: int = 3
    merge_attempts: int = 3
    step_failures: int = 3


class LifecycleError(ValueError):
    """A transition the stored state does not allow; the message names the state and the fix."""


_STEP_OF = {
    Action.IMPLEMENT: Status.IMPLEMENTING,
    Action.REVIEW: Status.REVIEWING,
    Action.FIX: Status.FIXING,
    Action.MERGE: Status.MERGING,
}
_REOPENABLE = frozenset({Status.FAILED, Status.DEFERRED, Status.ABANDONED})
# Stable statuses a job's agent can still hold a lease over without a claim: a sync job runs
# for a node its claim left where it was.
_UNCLAIMED_WITH_A_JOB = STABLE - {Status.COMPLETED, Status.FAILED}
_RESET_TARGETS = frozenset(
    {Status.READY, Status.IMPLEMENTED, Status.REVIEWED, Status.FIXED, Status.COMPLETED}
)


def next_action(c: Cycle) -> Action | None:
    """The step a claim on `c` starts. None when no claim can start one: COMPLETED, FAILED, an
    exit, a container at READY (its implement step is its children's work), and a node already
    in a step, whose lease or job is what an agent takes over."""
    match c.status:
        case Status.READY:
            return None if c.container else Action.IMPLEMENT
        case Status.IMPLEMENTED:
            return Action.REVIEW if c.review else Action.MERGE
        case Status.REVIEWED:
            return _after_review(c)
        case Status.FIXED:
            return Action.REVIEW
        case _:
            return None


def _after_review(c: Cycle) -> Action:
    if c.outcome == Outcome.MERGE_FAILED or (c.outcome == Outcome.REJECT and c.fix):
        return Action.FIX
    if c.outcome is None:
        raise LifecycleError("REVIEWED with no outcome: reset the node with --outcome")
    # A rejection nobody below fixes still lands, on a parent whose own review will see it.
    return Action.MERGE


def _counted(c: Cycle) -> bool:
    # A review after a landing fix checks that fix; it is not another review round.
    return c.claimed_from == Status.IMPLEMENTED or (
        c.claimed_from == Status.FIXED and c.fix_for == Outcome.REJECT
    )


def claim(c: Cycle) -> Cycle:
    action = next_action(c)
    if action is None:
        raise LifecycleError(f"a node at {c.status} has no step to claim")
    claimed = replace(c, status=_STEP_OF[action], claimed_from=c.status)
    if action == Action.FIX:
        return replace(claimed, fix_for=c.outcome)
    if action == Action.REVIEW and _counted(claimed):
        return replace(claimed, review_cycles=c.review_cycles + 1)
    return claimed


def _progress(
    c: Cycle,
    status: Status,
    outcome: Outcome | None = None,
    merge_attempts: int | None = None,
) -> Cycle:
    return replace(
        c,
        status=status,
        claimed_from=None,
        step_failures=0,
        outcome=c.outcome if outcome is None else outcome,
        merge_attempts=c.merge_attempts if merge_attempts is None else merge_attempts,
    )


def _rejected(c: Cycle, caps: Caps) -> Cycle:
    cap = caps.fix_rounds_container if c.container else caps.fix_rounds_task
    out_of_rounds = c.fix and c.review_cycles - 1 >= cap
    return _progress(c, Status.FAILED if out_of_rounds else Status.REVIEWED, Outcome.REJECT)


def _landing_failed(c: Cycle, caps: Caps) -> Cycle:
    failed = not c.fix or c.merge_attempts >= caps.merge_attempts
    return _progress(
        c,
        Status.FAILED if failed else Status.REVIEWED,
        Outcome.MERGE_FAILED,
        c.merge_attempts + 1,
    )


def _without_progress(c: Cycle, caps: Caps, back_to: Status, counts_as_failure: bool) -> Cycle:
    step_failures = c.step_failures + (1 if counts_as_failure else 0)
    return replace(
        c,
        status=Status.FAILED if step_failures >= caps.step_failures else back_to,
        claimed_from=None,
        step_failures=step_failures,
    )


def _back(c: Cycle, caps: Caps, counts_as_failure: bool) -> Cycle:
    if c.claimed_from is None:
        raise LifecycleError(f"{c.status} has no claimed_from to return to: run a reset")
    # A review that never delivered a verdict must not use up a fix round.
    uncounted = 1 if c.status == Status.REVIEWING and _counted(c) else 0
    returned = _without_progress(c, caps, c.claimed_from, counts_as_failure)
    return replace(returned, review_cycles=c.review_cycles - uncounted)


def advance(c: Cycle, event: Event, caps: Caps) -> Cycle:
    match (c.status, event):
        case (Status.IMPLEMENTING, Event.COMPLETE):
            return _progress(c, Status.IMPLEMENTED)
        case (Status.FIXING, Event.COMPLETE):
            return _progress(c, Status.FIXED)
        case (Status.REVIEWING, Event.APPROVE):
            return _progress(c, Status.REVIEWED, Outcome.APPROVE)
        case (Status.REVIEWING, Event.REJECT):
            return _rejected(c, caps)
        case (Status.MERGING, Event.LANDED):
            return _progress(c, Status.COMPLETED)
        case (Status.MERGING, Event.OWN_DEFECT):
            return _landing_failed(c, caps)
        case (status, Event.RELEASE | Event.EXPIRED) if status in IN_STEP:
            return _back(c, caps, counts_as_failure=True)
        case (status, Event.RELEASE_BLOCKED) if status in IN_STEP:
            return _back(c, caps, counts_as_failure=False)
        case (status, Event.RELEASE | Event.EXPIRED) if status in _UNCLAIMED_WITH_A_JOB:
            return _without_progress(c, caps, c.status, counts_as_failure=True)
        case (status, Event.RELEASE_BLOCKED) if status in _UNCLAIMED_WITH_A_JOB:
            return c
    raise LifecycleError(f"a node at {c.status} does not accept {event}")


def fix_round(c: Cycle) -> int:
    """The 1-based review round the fix `c` is in, or would start, answers; 0 for a fix
    answering a landing failure, which is not a review round."""
    answers = c.fix_for if c.status == Status.FIXING else c.outcome
    return c.review_cycles if answers == Outcome.REJECT else 0


def reopen(c: Cycle, children_all_completed: bool) -> Cycle:
    if c.status not in _REOPENABLE:
        raise LifecycleError(f"only FAILED, DEFERRED or ABANDONED reopen; this node is {c.status}")
    ready = Status.IMPLEMENTED if c.container and children_all_completed else Status.READY
    return Cycle(status=ready, container=c.container, review=c.review, fix=c.fix)


def _set_aside(c: Cycle, to: Status) -> Cycle:
    if c.status == Status.COMPLETED:
        raise LifecycleError(f"a COMPLETED node is landed code and cannot become {to}")
    if c.status not in STABLE:
        raise LifecycleError(
            f"a node at {c.status} cannot become {to}: wait for its step to end, or stop it"
        )
    return replace(c, status=to, claimed_from=None)


def defer(c: Cycle) -> Cycle:
    return _set_aside(c, Status.DEFERRED)


def abandon(c: Cycle) -> Cycle:
    return _set_aside(c, Status.ABANDONED)


def reset(c: Cycle, to: Status, outcome: Outcome | None) -> Cycle:
    if c.status in IN_STEP:
        raise LifecycleError(
            f"a node at {c.status} is in a step: wait for it to end, or stop it, then reset"
        )
    if to not in _RESET_TARGETS:
        raise LifecycleError(
            f"reset goes to READY, IMPLEMENTED, REVIEWED, FIXED or COMPLETED, not {to}"
        )
    if to in (Status.REVIEWED, Status.FIXED) and outcome is None:
        raise LifecycleError(f"a reset to {to} needs --outcome")
    if to not in (Status.REVIEWED, Status.FIXED) and outcome is not None:
        raise LifecycleError(f"--outcome applies to a reset to REVIEWED or FIXED, not {to}")
    if to == Status.FIXED and outcome == Outcome.APPROVE:
        raise LifecycleError("a fix answers reject or merge_failed, never approve")
    if not c.fix and (to == Status.FIXED or outcome == Outcome.MERGE_FAILED):
        raise LifecycleError("this node has fix off: nothing fixes it; turn fix on first")
    return replace(
        c,
        status=to,
        claimed_from=None,
        step_failures=0,
        outcome=outcome,
        fix_for=outcome if to == Status.FIXED else None,
    )
```

- [ ] **Step 4: Run the tests and the gates**, each in the foreground:

```bash
uv run --directory <worktree> pytest tests/unit/test_lifecycle.py -q; echo $?
uv run --directory <worktree> pytest -q; echo $?
uv run --directory <worktree> ruff check; echo $?
uv run --directory <worktree> ruff format --check; echo $?
uv run --directory <worktree> mypy; echo $?
```

Expected: `197 passed` and `0` for the first; every test passing and `0` for the full suite; `All checks passed!` and `0` for `ruff check`; nothing to reformat and `0` for `ruff format --check`; `Success: no issues found` and `0` for `mypy`. No existing test changes: nothing reads this module yet.

- [ ] **Step 5: Commit**

```bash
git -C <worktree> add src/taskmanager/core/lifecycle.py tests/unit/test_lifecycle.py && git -C <worktree> commit -m "feat(core): state machine for claims, step closes, caps and repair verbs"
```

### Task 3: Container rollup

**Spec:** §3.1 (Containers), §2.3 rule 6 (a container past READY moves back to READY)
**Files:**
- Create: `src/taskmanager/core/rollup.py`
- Test: `tests/unit/test_rollup.py`

**Interfaces:**
- Consumes: `Status`, `IN_STEP`, `EXITS`, `SET_ASIDE` (Task 1)
- Produces: `rollup`

The empty-diff completion needs git and stays with the caller (Task 17). Statuses the container holds on its own (a step, `COMPLETED`, `FAILED`, an exit) are never re-derived; a counted child back in play takes `IMPLEMENTED`/`REVIEWED`/`FIXED` back to `READY`, which is rule 6's "moves back to READY in the same write", so Task 17 gets it by calling `rollup` and needs no separate step. Exits stay put, so a container someone deferred is not brought back by a child change; `reopen` is the way back.

- [ ] **Step 1: Write the failing test** — `tests/unit/test_rollup.py`:

```python
import pytest

from taskmanager.core.rollup import rollup
from taskmanager.core.status import Status

S = Status


@pytest.mark.parametrize(
    ("current", "children", "derived"),
    [
        (S.READY, [], S.READY),
        (S.IMPLEMENTED, [], S.READY),
        (S.READY, [S.READY], S.READY),
        (S.READY, [S.COMPLETED, S.IMPLEMENTING], S.READY),
        (S.READY, [S.COMPLETED], S.IMPLEMENTED),
        (S.READY, [S.COMPLETED, S.COMPLETED], S.IMPLEMENTED),
        (S.READY, [S.COMPLETED, S.DEFERRED, S.ABANDONED, S.SUPERSEDED], S.IMPLEMENTED),
        (S.IMPLEMENTED, [S.COMPLETED], S.IMPLEMENTED),
        (S.REVIEWED, [S.COMPLETED], S.REVIEWED),
        (S.FIXED, [S.COMPLETED], S.FIXED),
        (S.IMPLEMENTED, [S.COMPLETED, S.READY], S.READY),
        (S.REVIEWED, [S.COMPLETED, S.FAILED], S.READY),
        (S.FIXED, [S.COMPLETED, S.READY], S.READY),
        (S.READY, [S.SUPERSEDED], S.COMPLETED),
        (S.READY, [S.SUPERSEDED, S.SUPERSEDED], S.COMPLETED),
        (S.READY, [S.SUPERSEDED, S.DEFERRED], S.DEFERRED),
        (S.READY, [S.ABANDONED, S.DEFERRED], S.DEFERRED),
        (S.READY, [S.ABANDONED], S.ABANDONED),
        (S.READY, [S.ABANDONED, S.SUPERSEDED], S.ABANDONED),
        (S.IMPLEMENTED, [S.DEFERRED], S.DEFERRED),
    ],
)
def test_a_container_status_is_derived_from_its_counted_children(
    current: Status, children: list[Status], derived: Status
) -> None:
    assert rollup(current, children) == derived


@pytest.mark.parametrize(
    "current",
    [
        S.REVIEWING,
        S.FIXING,
        S.MERGING,
        S.IMPLEMENTING,
        S.COMPLETED,
        S.FAILED,
        S.DEFERRED,
        S.ABANDONED,
        S.SUPERSEDED,
    ],
)
@pytest.mark.parametrize("children", [[], [S.READY], [S.COMPLETED], [S.DEFERRED]])
def test_a_status_the_container_holds_on_its_own_is_never_re_derived(
    current: Status, children: list[Status]
) -> None:
    assert rollup(current, children) == current
```

- [ ] **Step 2: Run it and watch it fail**

```bash
uv run --directory <worktree> pytest tests/unit/test_rollup.py -q; echo $?
```

Expected: collection stops with `ModuleNotFoundError: No module named 'taskmanager.core.rollup'`, `1 error`, exit code `2`.

- [ ] **Step 3: Implement** — `src/taskmanager/core/rollup.py`:

```python
from collections.abc import Sequence

from taskmanager.core.status import EXITS, IN_STEP, SET_ASIDE, Status

# Owned by the container's own cycle or by an explicit verb, never re-derived from children:
# a step in flight, landed code, a failure awaiting its decision, and an exit someone chose.
_KEPT = IN_STEP | EXITS | {Status.COMPLETED, Status.FAILED}


def rollup(current: Status, children: Sequence[Status]) -> Status:
    """A container's stored status re-derived from its children's, in the same write as any
    change to them. The empty-diff completion needs git and is the caller's."""
    if current in _KEPT:
        return current
    if not children:
        return Status.READY
    counted = [child for child in children if child not in SET_ASIDE]
    if not counted:
        if all(child == Status.SUPERSEDED for child in children):
            return Status.COMPLETED
        return Status.DEFERRED if Status.DEFERRED in children else Status.ABANDONED
    if all(child == Status.COMPLETED for child in counted):
        return Status.IMPLEMENTED if current == Status.READY else current
    # A child back in play takes a container past READY back to READY: its review, if any,
    # would otherwise read a branch still missing that child's code.
    return Status.READY
```

- [ ] **Step 4: Run the tests and the gates**, each in the foreground:

```bash
uv run --directory <worktree> pytest tests/unit/test_rollup.py -q; echo $?
uv run --directory <worktree> pytest -q; echo $?
uv run --directory <worktree> ruff check; echo $?
uv run --directory <worktree> ruff format --check; echo $?
uv run --directory <worktree> mypy; echo $?
```

Expected: `56 passed` and `0` for the first; every test passing and `0` for the full suite; `All checks passed!` and `0` for `ruff check`; nothing to reformat and `0` for `ruff format --check`; `Success: no issues found` and `0` for `mypy`. No existing test changes: nothing reads this module yet.

- [ ] **Step 5: Commit**

```bash
git -C <worktree> add src/taskmanager/core/rollup.py tests/unit/test_rollup.py && git -C <worktree> commit -m "feat(core): re-derive a container's stored status from its children"
```

### Task 4: Display and phase

**Spec:** §7.1, §7.2
**Files:**
- Create: `src/taskmanager/core/display.py`
- Test: `tests/unit/test_display.py`

**Interfaces:**
- Consumes: `Cycle`, `next_action` (Task 2); `Status`, `EXITS`, `IN_STEP`, `Action`, `DisplayStatus`, `Phase` (Task 1)
- Produces: `Facts`, `phase`, `display_status`

Every row of §7.2 is a test row, each paired with facts that also match every later row, so precedence is asserted row by row; the last test proves every `DisplayStatus` is reachable. The `WAITING_*` rows are the next action's name, so they are read off `next_action` rather than restated.

- [ ] **Step 1: Write the failing test** — `tests/unit/test_display.py`:

```python
import pytest

from taskmanager.core.display import Facts, display_status, phase
from taskmanager.core.lifecycle import Cycle
from taskmanager.core.status import DisplayStatus, Outcome, Phase, Status

S = Status
D = DisplayStatus
LIVE = Facts(lease="live")
EVERY_BLOCKER = Facts(
    open_decision=True,
    unsatisfied_edge=True,
    unmet_condition=True,
    sync_pending=True,
    files_locked=True,
)


@pytest.mark.parametrize(
    ("status", "shown"),
    [
        (S.READY, Phase.QUEUED),
        (S.IMPLEMENTING, Phase.DISPATCHED),
        (S.IMPLEMENTED, Phase.DISPATCHED),
        (S.REVIEWING, Phase.DISPATCHED),
        (S.REVIEWED, Phase.DISPATCHED),
        (S.FIXING, Phase.DISPATCHED),
        (S.FIXED, Phase.DISPATCHED),
        (S.MERGING, Phase.DISPATCHED),
        (S.COMPLETED, Phase.COMPLETED),
        (S.FAILED, Phase.FAILED),
        (S.DEFERRED, Phase.DEFERRED),
        (S.ABANDONED, Phase.ABANDONED),
        (S.SUPERSEDED, Phase.SUPERSEDED),
    ],
)
def test_phase_groups_every_step_between_ready_and_completed(status: Status, shown: Phase) -> None:
    assert phase(status) == shown


ROWS = [
    ("exit shows itself", Cycle(S.DEFERRED), EVERY_BLOCKER, D.DEFERRED),
    ("abandoned shows itself", Cycle(S.ABANDONED), Facts(), D.ABANDONED),
    ("superseded shows itself", Cycle(S.SUPERSEDED), Facts(), D.SUPERSEDED),
    ("completed shows itself", Cycle(S.COMPLETED), EVERY_BLOCKER, D.COMPLETED),
    ("failed shows itself", Cycle(S.FAILED), Facts(open_decision=True), D.FAILED),
    (
        "stopped landing waits for an agent",
        Cycle(S.MERGING),
        Facts(lease="live", job_needs_agent=True),
        D.WAITING_MERGE_AGENT,
    ),
    ("live implement", Cycle(S.IMPLEMENTING), LIVE, D.IMPLEMENTING),
    ("live review", Cycle(S.REVIEWING), LIVE, D.REVIEWING),
    ("live fix", Cycle(S.FIXING), LIVE, D.FIXING),
    ("live landing", Cycle(S.MERGING), LIVE, D.MERGING),
    (
        "a live step outranks every blocker",
        Cycle(S.IMPLEMENTING),
        Facts(
            lease="live",
            open_decision=True,
            unsatisfied_edge=True,
            unmet_condition=True,
            sync_pending=True,
            files_locked=True,
        ),
        D.IMPLEMENTING,
    ),
    ("expired lease", Cycle(S.REVIEWING), Facts(lease="expired"), D.STALE),
    ("no lease row", Cycle(S.FIXING), Facts(lease="none"), D.STALE),
    ("stale outranks a decision", Cycle(S.MERGING), Facts(open_decision=True), D.STALE),
    ("open decision", Cycle(S.READY), EVERY_BLOCKER, D.AWAITING_DECISION),
    (
        "unsatisfied edge",
        Cycle(S.IMPLEMENTED),
        Facts(unsatisfied_edge=True, unmet_condition=True, sync_pending=True, files_locked=True),
        D.BLOCKED_BY_TASK,
    ),
    (
        "unmet condition",
        Cycle(S.REVIEWED, outcome=Outcome.APPROVE),
        Facts(unmet_condition=True, sync_pending=True, files_locked=True),
        D.BLOCKED_BY_CONDITION,
    ),
    (
        "sync pending",
        Cycle(S.READY),
        Facts(sync_pending=True, files_locked=True),
        D.BLOCKED_BY_SYNC,
    ),
    ("files locked", Cycle(S.FIXED), Facts(files_locked=True), D.BLOCKED_BY_LEASE),
    (
        "blocked outranks a started container",
        Cycle(S.READY, container=True),
        Facts(unsatisfied_edge=True, descendant_started=True),
        D.BLOCKED_BY_TASK,
    ),
    (
        "container with a started descendant",
        Cycle(S.READY, container=True),
        Facts(descendant_started=True),
        D.IMPLEMENTING,
    ),
    ("container nothing started", Cycle(S.READY, container=True), Facts(), D.READY),
    (
        "a task ignores descendant_started",
        Cycle(S.READY),
        Facts(descendant_started=True),
        D.READY,
    ),
    ("ready", Cycle(S.READY), Facts(), D.READY),
    ("implemented with review", Cycle(S.IMPLEMENTED), Facts(), D.WAITING_REVIEW),
    (
        "implemented without review",
        Cycle(S.IMPLEMENTED, review=False, fix=False),
        Facts(),
        D.WAITING_MERGE,
    ),
    (
        "approved",
        Cycle(S.REVIEWED, outcome=Outcome.APPROVE),
        Facts(),
        D.WAITING_MERGE,
    ),
    (
        "rejected with fix on",
        Cycle(S.REVIEWED, outcome=Outcome.REJECT),
        Facts(),
        D.WAITING_FIX,
    ),
    (
        "landing failed",
        Cycle(S.REVIEWED, outcome=Outcome.MERGE_FAILED),
        Facts(),
        D.WAITING_FIX,
    ),
    (
        "rejected with fix off",
        Cycle(S.REVIEWED, fix=False, outcome=Outcome.REJECT),
        Facts(),
        D.WAITING_MERGE,
    ),
    ("fixed", Cycle(S.FIXED, fix_for=Outcome.REJECT), Facts(), D.WAITING_REVIEW),
]


@pytest.mark.parametrize(
    ("cycle", "facts", "shown"), [row[1:] for row in ROWS], ids=[row[0] for row in ROWS]
)
def test_the_first_matching_display_row_wins(
    cycle: Cycle, facts: Facts, shown: DisplayStatus
) -> None:
    assert display_status(cycle, facts) == shown


def test_every_display_status_is_reachable() -> None:
    assert {row[3] for row in ROWS} == set(DisplayStatus)
```

- [ ] **Step 2: Run it and watch it fail**

```bash
uv run --directory <worktree> pytest tests/unit/test_display.py -q; echo $?
```

Expected: collection stops with `ModuleNotFoundError: No module named 'taskmanager.core.display'`, `1 error`, exit code `2`.

- [ ] **Step 3: Implement** — `src/taskmanager/core/display.py`:

```python
from dataclasses import dataclass
from typing import Literal

from taskmanager.core.lifecycle import Cycle, next_action
from taskmanager.core.status import EXITS, IN_STEP, Action, DisplayStatus, Phase, Status


@dataclass(frozen=True)
class Facts:
    lease: Literal["live", "expired", "none"] = "none"
    job_needs_agent: bool = False
    open_decision: bool = False
    unsatisfied_edge: bool = False
    unmet_condition: bool = False
    sync_pending: bool = False
    files_locked: bool = False
    descendant_started: bool = False


_WAITING = {
    Action.REVIEW: DisplayStatus.WAITING_REVIEW,
    Action.FIX: DisplayStatus.WAITING_FIX,
    Action.MERGE: DisplayStatus.WAITING_MERGE,
}


def phase(status: Status) -> Phase:
    if status == Status.READY:
        return Phase.QUEUED
    if status in EXITS or status in (Status.COMPLETED, Status.FAILED):
        return Phase(status.value)
    return Phase.DISPATCHED


def display_status(c: Cycle, f: Facts) -> DisplayStatus:
    """The one status a reader sees; the first matching condition wins."""
    if c.status in EXITS or c.status in (Status.COMPLETED, Status.FAILED):
        return DisplayStatus(c.status.value)
    if c.status == Status.MERGING and f.job_needs_agent:
        return DisplayStatus.WAITING_MERGE_AGENT
    if c.status in IN_STEP:
        return DisplayStatus(c.status.value) if f.lease == "live" else DisplayStatus.STALE
    for holds, shown in (
        (f.open_decision, DisplayStatus.AWAITING_DECISION),
        (f.unsatisfied_edge, DisplayStatus.BLOCKED_BY_TASK),
        (f.unmet_condition, DisplayStatus.BLOCKED_BY_CONDITION),
        (f.sync_pending, DisplayStatus.BLOCKED_BY_SYNC),
        (f.files_locked, DisplayStatus.BLOCKED_BY_LEASE),
    ):
        if holds:
            return shown
    if c.status == Status.READY:
        started = c.container and f.descendant_started
        return DisplayStatus.IMPLEMENTING if started else DisplayStatus.READY
    action = next_action(c)
    assert action is not None, f"{c.status} is stable and not READY, so it has a next step"
    return _WAITING[action]
```

- [ ] **Step 4: Run the tests and the gates**, each in the foreground:

```bash
uv run --directory <worktree> pytest tests/unit/test_display.py -q; echo $?
uv run --directory <worktree> pytest -q; echo $?
uv run --directory <worktree> ruff check; echo $?
uv run --directory <worktree> ruff format --check; echo $?
uv run --directory <worktree> mypy; echo $?
```

Expected: `45 passed` and `0` for the first; every test passing and `0` for the full suite; `All checks passed!` and `0` for `ruff check`; nothing to reformat and `0` for `ruff format --check`; `Success: no issues found` and `0` for `mypy`. No existing test changes: nothing reads this module yet.

- [ ] **Step 5: Commit**

```bash
git -C <worktree> add src/taskmanager/core/display.py tests/unit/test_display.py && git -C <worktree> commit -m "feat(core): derive phase and display status from stored state and runtime facts"
```

### Task 5: Landing and base chains

**Spec:** §4.1, §6.1, §6.4 (what a claim syncs)
**Files:**
- Create: `src/taskmanager/engine/chains.py`
- Test: `tests/unit/test_chains.py`

**Interfaces:**
- Consumes: `Status`, `DecisionStatus`, `Merge` (Task 1)
- Produces: `MAIN`, `Tree`, `landing_target`, `base_chain`, `landing_chain`, `meeting`, `satisfied`, `sync_pairs`

The test tree is three levels deep (spec, plan, task) and mixes `parent` and `main` landings at each level. Satisfaction reads statuses and the tree only, so an edge across repositories is decided exactly like one inside a repository; there is no repository argument to test.

- [ ] **Step 1: Write the failing test** — `tests/unit/test_chains.py`:

```python
from dataclasses import dataclass, field

import pytest

from taskmanager.core.status import DecisionStatus, Merge, Status
from taskmanager.engine.chains import (
    MAIN,
    base_chain,
    landing_chain,
    landing_target,
    meeting,
    satisfied,
    sync_pairs,
)

PARENT = Merge.PARENT


@dataclass
class Tree:
    """S is a spec; P and Q are its plans; T1, T2, G sit under P and U under Q; M is a
    top-level task; D is a decision. P, T1, T2 and U land on their parent's branch."""

    statuses: dict[str, Status | DecisionStatus] = field(default_factory=dict)
    rows: dict[str, tuple[str | None, Merge]] = field(
        default_factory=lambda: {
            "S": (None, Merge.MAIN),
            "P": ("S", PARENT),
            "Q": ("S", Merge.MAIN),
            "T1": ("P", PARENT),
            "T2": ("P", PARENT),
            "G": ("P", Merge.MAIN),
            "U": ("Q", PARENT),
            "M": (None, Merge.MAIN),
            "D": (None, Merge.MAIN),
            "ORPHAN": (None, PARENT),
        }
    )

    def parent(self, node_id: str) -> str | None:
        return self.rows[node_id][0]

    def merge(self, node_id: str) -> Merge:
        return self.rows[node_id][1]

    def status(self, node_id: str) -> Status | DecisionStatus:
        return self.statuses.get(node_id, Status.READY)


@pytest.mark.parametrize(
    ("node", "target"),
    [("S", MAIN), ("P", "S"), ("Q", MAIN), ("T1", "P"), ("G", MAIN), ("U", "Q"), ("ORPHAN", MAIN)],
)
def test_landing_target_is_the_parent_only_for_merge_parent(node: str, target: str) -> None:
    assert landing_target(Tree(), node) == target


@pytest.mark.parametrize(
    ("node", "chain"),
    [
        ("S", [MAIN]),
        ("P", ["S", MAIN]),
        ("T1", ["P", "S", MAIN]),
        ("G", [MAIN]),
        ("U", ["Q", MAIN]),
        ("M", [MAIN]),
    ],
)
def test_base_chain_lists_the_branches_a_node_builds_on_nearest_first(
    node: str, chain: list[str]
) -> None:
    assert base_chain(Tree(), node) == chain


@pytest.mark.parametrize(
    ("node", "chain"),
    [
        ("S", ["S"]),
        ("P", ["P", "S"]),
        ("T1", ["T1", "P", "S"]),
        ("G", ["G"]),
        ("U", ["U", "Q"]),
        ("M", ["M"]),
    ],
)
def test_landing_chain_follows_the_code_up_to_the_node_that_lands_on_main(
    node: str, chain: list[str]
) -> None:
    assert landing_chain(Tree(), node) == chain


@pytest.mark.parametrize(
    ("x", "y", "z", "pairs"),
    [
        ("T2", "T1", "T1", []),
        ("T1", "M", "M", [(MAIN, "S"), ("S", "P")]),
        ("P", "M", "M", [(MAIN, "S")]),
        ("M", "T1", "S", []),
        ("G", "T1", "S", []),
        ("U", "T1", "S", [(MAIN, "Q")]),
        ("T1", "G", "G", [(MAIN, "S"), ("S", "P")]),
        ("T1", "U", "Q", [(MAIN, "S"), ("S", "P")]),
        ("Q", "P", "S", []),
    ],
)
def test_a_dependency_meets_the_dependent_where_its_code_reaches_a_base_branch(
    x: str, y: str, z: str, pairs: list[tuple[str, str]]
) -> None:
    tree = Tree()
    assert meeting(tree, x, y) == z
    assert sync_pairs(tree, x, y) == pairs


@pytest.mark.parametrize(
    ("x", "y", "statuses", "expected"),
    [
        ("T2", "T1", {"T1": Status.COMPLETED}, True),
        ("T2", "T1", {"T1": Status.MERGING}, False),
        ("M", "T1", {"T1": Status.COMPLETED, "P": Status.COMPLETED}, False),
        ("M", "T1", {"T1": Status.COMPLETED, "P": Status.COMPLETED, "S": Status.COMPLETED}, True),
        ("G", "T1", {"T1": Status.COMPLETED, "P": Status.COMPLETED}, False),
        ("T1", "M", {"M": Status.COMPLETED}, True),
        ("T1", "M", {"M": Status.REVIEWED}, False),
        ("U", "T1", {"S": Status.COMPLETED}, True),
        ("T2", "T1", {"T1": Status.SUPERSEDED}, True),
        ("M", "T1", {"T1": Status.SUPERSEDED}, True),
        ("T2", "T1", {"T1": Status.ABANDONED}, False),
        ("T2", "T1", {"T1": Status.FAILED}, False),
        ("M", "D", {"D": DecisionStatus.OPEN}, False),
        ("M", "D", {"D": DecisionStatus.ANSWERED}, True),
        ("M", "D", {"D": DecisionStatus.WITHDRAWN}, True),
    ],
)
def test_an_edge_is_satisfied_once_the_meeting_node_has_landed(
    x: str, y: str, statuses: dict[str, Status | DecisionStatus], expected: bool
) -> None:
    assert satisfied(Tree(statuses), x, y) is expected
```

- [ ] **Step 2: Run it and watch it fail**

```bash
uv run --directory <worktree> pytest tests/unit/test_chains.py -q; echo $?
```

Expected: collection stops with `ModuleNotFoundError: No module named 'taskmanager.engine.chains'`, `1 error`, exit code `2`.

- [ ] **Step 3: Implement** — `src/taskmanager/engine/chains.py`:

```python
"""Where a node lands and what it builds on, decided from stored statuses and the tree alone:
no git call and no repository comparison, so satisfaction is the same in every repository."""

from typing import Final, Protocol

from taskmanager.core.status import DecisionStatus, Merge, Status

MAIN: Final = "MAIN"


class Tree(Protocol):
    def parent(self, node_id: str) -> str | None: ...
    def merge(self, node_id: str) -> Merge: ...
    def status(self, node_id: str) -> Status | DecisionStatus: ...


def landing_target(t: Tree, node_id: str) -> str:
    parent = t.parent(node_id)
    # A parentless node with merge=parent is refused at write time; reading it as landing on
    # main keeps every chain finite while that refusal is reported.
    return parent if parent is not None and t.merge(node_id) == Merge.PARENT else MAIN


def base_chain(t: Tree, node_id: str) -> list[str]:
    """The branches `node_id` builds on, nearest first, ending with MAIN."""
    chain = [landing_target(t, node_id)]
    while chain[-1] != MAIN:
        chain.append(landing_target(t, chain[-1]))
    return chain


def landing_chain(t: Tree, node_id: str) -> list[str]:
    """`node_id`, then each node whose branch carries its code on, up to the first that lands
    on MAIN."""
    chain = [node_id]
    while (target := landing_target(t, chain[-1])) != MAIN:
        chain.append(target)
    return chain


def meeting(t: Tree, x: str, y: str) -> str:
    """The node whose landing puts `y`'s code on a branch `x` builds on. It always exists,
    because the last node of every landing chain lands on MAIN and every base chain ends there."""
    bases = base_chain(t, x)
    return next(z for z in landing_chain(t, y) if landing_target(t, z) in bases)


def satisfied(t: Tree, x: str, y: str) -> bool:
    status = t.status(y)
    if isinstance(status, DecisionStatus):
        return status != DecisionStatus.OPEN
    return status == Status.SUPERSEDED or t.status(meeting(t, x, y)) == Status.COMPLETED


def sync_pairs(t: Tree, x: str, y: str) -> list[tuple[str, str]]:
    """The merges that carry `y`'s landed code down to `x`'s own base, as (source, base) pairs,
    top-down: each base is brought up to date from the one it lands on."""
    bases = base_chain(t, x)
    reached = bases.index(landing_target(t, meeting(t, x, y)))
    return [(bases[j + 1], bases[j]) for j in range(reached - 1, -1, -1)]
```

- [ ] **Step 4: Run the tests and the gates**, each in the foreground:

```bash
uv run --directory <worktree> pytest tests/unit/test_chains.py -q; echo $?
uv run --directory <worktree> pytest -q; echo $?
uv run --directory <worktree> ruff check; echo $?
uv run --directory <worktree> ruff format --check; echo $?
uv run --directory <worktree> mypy; echo $?
```

Expected: `43 passed` and `0` for the first; every test passing and `0` for the full suite; `All checks passed!` and `0` for `ruff check`; nothing to reformat and `0` for `ruff format --check`; `Success: no issues found` and `0` for `mypy`. No existing test changes: nothing reads this module yet.

- [ ] **Step 5: Commit**

```bash
git -C <worktree> add src/taskmanager/engine/chains.py tests/unit/test_chains.py && git -C <worktree> commit -m "feat(engine): decide dependency satisfaction from landing and base chains"
```

### Task 6: Expanded step graph and cycle check

**Spec:** §4.4, §5.6 (the migration chain), §11 (cycle check)
**Files:**
- Create: `src/taskmanager/engine/stepgraph.py`
- Test: `tests/unit/test_stepgraph.py`

**Interfaces:**
- Consumes: `landing_chain`, `meeting` (Task 5); `Status`, `DecisionStatus`, `Merge`, `EXITS`, `SET_ASIDE` (Task 1); `NodeKind` (`core/enums.py`)
- Produces: `SnapNode`, `Snapshot` (satisfies `chains.Tree`), `find_cycle`, `format_cycle`, `migration_order` (consumed by Task 16)

The property test draws 500 seeded random estates (specs, nested plans, tasks, decisions, mixed landings, set-aside and superseded nodes, random edges; about a third come out cyclic) and checks `find_cycle` against a brute-force reachability oracle built independently from the §4.4 definition, and that each printed path is a real cycle of that oracle's edges. The migration chain is covered by explicit cases, including the one across a container boundary.

- **Defaults.** Every `SnapNode` field but `id` and `kind` defaults to the task column default (`parent=None`, `merge=Merge.MAIN`, `status=Status.READY`, `review=True`, `fix=True`, `repo=None`, `writes_migration=False`, `busy=False`, `literal_origin_main=False`), so a test names only what it varies. No field is renamed or re-typed, and Task 10 sets every field.
- **Inherited edges.** An edge `X → Y` adds `meeting(D, Y).landed → D.start` for `X` and each descendant `D`, as the contract states: `Claims` (Task 13) and the display facts (Task 10) evaluate an inherited edge as `satisfied(snapshot, D, Y)`, and the check models that same wait. It differs from `meeting(X, Y)` for a `merge = main` descendant under a container that lands on its parent.
- **Migration chain.** A writer is a task (a container's `declared_files` default to the union of its descendants', so it would wait on its own child) that is not an exit and whose code has not reached `main`. `migration_order(s, repo)` sorts the writers by who holds the chain now (`COMPLETED` on a container branch, then in a step, then the rest), then by where their code reaches `main` in the chain-free graph, then by own start, then by id. Each later writer `B` waits on `meeting(B, A).landed` for every earlier writer `A`: `P.landed` when `A` lands on `tm/P` and `B` builds on `main`, which is the deadlock across a container boundary §11 names; `A` itself for a sibling in the same container, which builds on the landed migration (§5.6) and never writes a second migration head on one branch at once. Task 16 grants the chain in exactly this order, so discovery imposes the waits the check modelled.

- [ ] **Step 1: Write the failing test** — `tests/unit/test_stepgraph.py`:

```python
import random

import pytest

from taskmanager.core.enums import NodeKind
from taskmanager.core.status import SET_ASIDE, DecisionStatus, Merge, Status
from taskmanager.engine.chains import meeting
from taskmanager.engine.stepgraph import (
    SnapNode,
    Snapshot,
    find_cycle,
    format_cycle,
    migration_order,
)

SPEC, PLAN, TASK, DECISION = NodeKind.SPEC, NodeKind.PLAN, NodeKind.TASK, NodeKind.DECISION
PARENT = Merge.PARENT


def snap(*nodes: SnapNode, edges: list[tuple[str, str]] | None = None) -> Snapshot:
    return Snapshot({n.id: n for n in nodes}, edges or [])


def test_snapshot_walks_the_tree_and_inherits_ancestor_edges() -> None:
    s = snap(
        SnapNode("S", SPEC),
        SnapNode("P", PLAN, parent="S", merge=PARENT),
        SnapNode("T2", TASK, parent="P"),
        SnapNode("T1", TASK, parent="P"),
        SnapNode("X", TASK),
        SnapNode("Y", TASK),
        edges=[("T1", "X"), ("S", "Y"), ("P", "X")],
    )
    assert s.children("P") == ["T1", "T2"]
    assert s.descendants("S") == ["P", "T1", "T2"]
    assert s.inherited_edges("T1") == ["X", "Y"]
    assert s.inherited_edges("T2") == ["X", "Y"]
    assert s.inherited_edges("X") == []


def test_a_dependency_on_the_own_container_prints_the_cycle_as_a_waits_on_path() -> None:
    s = snap(
        SnapNode("P", PLAN),
        SnapNode("A", TASK, parent="P", merge=PARENT),
        edges=[("A", "P")],
    )
    assert format_cycle(find_cycle(s) or []) == (
        "A.start ← P.landed ← P.implemented ← A.landed ← A.implemented ← A.start"
    )


def test_an_acyclic_graph_has_no_cycle() -> None:
    s = snap(
        SnapNode("P", PLAN),
        SnapNode("A", TASK, parent="P", merge=PARENT),
        SnapNode("B", TASK, parent="P", merge=PARENT),
        SnapNode("M", TASK),
        edges=[("B", "A"), ("A", "M")],
    )
    assert find_cycle(s) is None


def test_an_edge_on_a_container_gates_its_descendants_and_can_close_a_cycle() -> None:
    s = snap(
        SnapNode("P", PLAN),
        SnapNode("D", TASK, parent="P"),
        SnapNode("Y", TASK),
        edges=[("P", "Y"), ("Y", "D")],
    )
    cycle = find_cycle(s)
    assert cycle is not None
    assert {"D.start", "Y.landed", "Y.start", "D.landed"} <= set(cycle)


def test_a_superseded_dependency_waits_on_nothing() -> None:
    s = snap(
        SnapNode("A", TASK),
        SnapNode("B", TASK, status=Status.SUPERSEDED),
        edges=[("A", "B"), ("B", "A")],
    )
    assert find_cycle(s) is None


def test_a_set_aside_child_does_not_hold_its_container() -> None:
    s = snap(
        SnapNode("P", PLAN),
        SnapNode("K", TASK, parent="P", merge=PARENT, status=Status.DEFERRED),
        edges=[("K", "P")],
    )
    assert find_cycle(s) is None


def test_edges_to_decisions_are_left_out() -> None:
    s = snap(
        SnapNode("A", TASK),
        SnapNode("Q", DECISION, status=DecisionStatus.OPEN),
        edges=[("A", "Q")],
    )
    assert find_cycle(s) is None


def _migration_estate(a_status: Status) -> Snapshot:
    # A writes a migration on tm/P; B writes one straight to main; C, also on tm/P, needs B.
    return snap(
        SnapNode("P", PLAN, repo="core"),
        SnapNode(
            "A", TASK, parent="P", merge=PARENT, repo="core", writes_migration=True, status=a_status
        ),
        SnapNode("C", TASK, parent="P", merge=PARENT, repo="core"),
        SnapNode("B", TASK, repo="core", writes_migration=True),
        edges=[("C", "B")],
    )


def test_the_migration_chain_is_granted_first_to_the_writer_whose_code_reaches_main_first() -> None:
    s = _migration_estate(Status.READY)
    assert migration_order(s, "core") == ["B", "A"]
    assert find_cycle(s) is None


def test_a_chain_held_across_a_container_boundary_closes_a_cycle() -> None:
    cycle = find_cycle(_migration_estate(Status.IMPLEMENTING))
    assert cycle is not None
    assert {"B.start", "P.landed", "C.start", "B.landed"} <= set(cycle)


def test_sibling_writers_take_the_chain_in_dependency_order() -> None:
    s = snap(
        SnapNode("P", PLAN, repo="core"),
        SnapNode("A1", TASK, parent="P", merge=PARENT, repo="core", writes_migration=True),
        SnapNode("A2", TASK, parent="P", merge=PARENT, repo="core", writes_migration=True),
        edges=[("A1", "A2")],
    )
    assert migration_order(s, "core") == ["A2", "A1"]
    assert find_cycle(s) is None


def test_a_writer_whose_code_is_on_main_no_longer_holds_the_chain() -> None:
    s = snap(
        SnapNode("A", TASK, repo="core", writes_migration=True, status=Status.COMPLETED),
        SnapNode("B", TASK, repo="core", writes_migration=True),
        SnapNode("W", TASK, repo="web", writes_migration=True),
    )
    assert migration_order(s, "core") == ["B"]


def _random_snapshot(rng: random.Random) -> Snapshot:
    nodes: dict[str, SnapNode] = {}
    for i in range(rng.randint(1, 8)):
        kind = rng.choice([SPEC, PLAN, PLAN, TASK, TASK, TASK, DECISION])
        containers = [n.id for n in nodes.values() if n.kind in (SPEC, PLAN)]
        parent = None
        if kind in (PLAN, TASK) and containers and rng.random() < 0.7:
            parent = rng.choice(containers)
        merge = PARENT if parent is not None and rng.random() < 0.6 else Merge.MAIN
        status: Status | DecisionStatus = rng.choice(
            [Status.READY] * 4
            + [Status.COMPLETED, Status.IMPLEMENTING, Status.SUPERSEDED, Status.DEFERRED]
        )
        if kind == DECISION:
            status = DecisionStatus.OPEN
        nodes[f"N{i}"] = SnapNode(f"N{i}", kind, parent=parent, merge=merge, status=status)
    ids = list(nodes)
    edges = [
        (x, y)
        for x, y in (tuple(rng.sample(ids, 2)) for _ in range(rng.randint(0, 6)) if len(ids) > 1)
    ]
    return Snapshot(nodes, edges)


def _oracle_edges(s: Snapshot) -> set[tuple[str, str]]:
    work = {i for i, n in s.nodes.items() if n.kind != DECISION}
    edges: set[tuple[str, str]] = set()
    for i in work:
        edges |= {(f"{i}.start", f"{i}.implemented"), (f"{i}.implemented", f"{i}.landed")}
        parent = s.nodes[i].parent
        if parent in work and s.nodes[i].status not in SET_ASIDE:
            edges.add((f"{i}.landed", f"{parent}.implemented"))

    def under(d: str, x: str) -> bool:
        node: str | None = d
        while node is not None:
            if node == x:
                return True
            node = s.nodes[node].parent
        return False

    for x, y in s.edges:
        if x in work and y in work and s.nodes[y].status != Status.SUPERSEDED:
            for d in work:
                if under(d, x):
                    edges.add((f"{meeting(s, d, y)}.landed", f"{d}.start"))
    return edges


def _oracle_has_cycle(edges: set[tuple[str, str]]) -> bool:
    successors: dict[str, set[str]] = {}
    for a, b in edges:
        successors.setdefault(a, set()).add(b)
    for vertex, direct in successors.items():
        seen: set[str] = set()
        frontier = list(direct)
        while frontier:
            current = frontier.pop()
            if current == vertex:
                return True
            if current not in seen:
                seen.add(current)
                frontier.extend(successors.get(current, ()))
    return False


@pytest.mark.parametrize("seed", range(500))
def test_a_cycle_is_found_exactly_when_one_exists_and_the_path_is_real(seed: int) -> None:
    s = _random_snapshot(random.Random(seed))
    edges = _oracle_edges(s)
    path = find_cycle(s)
    assert (path is not None) == _oracle_has_cycle(edges)
    if path is not None:
        assert path[0] == path[-1] and len(path) > 2
        assert all((path[i + 1], path[i]) in edges for i in range(len(path) - 1))
```

- [ ] **Step 2: Run it and watch it fail**

```bash
uv run --directory <worktree> pytest tests/unit/test_stepgraph.py -q; echo $?
```

Expected: collection stops with `ModuleNotFoundError: No module named 'taskmanager.engine.stepgraph'`, `1 error`, exit code `2`.

- [ ] **Step 3: Implement** — `src/taskmanager/engine/stepgraph.py`:

```python
"""The expanded step graph: every node as start -> implemented -> landed, with an edge wherever
one step waits on another. It is the only deadlock guard, so every graph-changing write is
refused when it closes a cycle here."""

from dataclasses import dataclass
from graphlib import CycleError, TopologicalSorter

from taskmanager.core.enums import NodeKind
from taskmanager.core.status import EXITS, SET_ASIDE, DecisionStatus, Merge, Status
from taskmanager.engine.chains import landing_chain, meeting

CONTAINERS = frozenset({NodeKind.PLAN, NodeKind.SPEC})
Graph = dict[str, set[str]]


@dataclass(frozen=True)
class SnapNode:
    id: str
    kind: NodeKind
    parent: str | None = None
    merge: Merge = Merge.MAIN
    status: Status | DecisionStatus = Status.READY
    review: bool = True
    fix: bool = True
    repo: str | None = None
    writes_migration: bool = False
    busy: bool = False
    literal_origin_main: bool = False


@dataclass
class Snapshot:
    nodes: dict[str, SnapNode]
    edges: list[tuple[str, str]]

    def parent(self, node_id: str) -> str | None:
        return self.nodes[node_id].parent

    def merge(self, node_id: str) -> Merge:
        return self.nodes[node_id].merge

    def status(self, node_id: str) -> Status | DecisionStatus:
        return self.nodes[node_id].status

    def children(self, node_id: str) -> list[str]:
        # ponytail: scans every node per call; index children once snapshots reach thousands.
        return sorted(n.id for n in self.nodes.values() if n.parent == node_id)

    def descendants(self, node_id: str) -> list[str]:
        found: list[str] = []
        for child in self.children(node_id):
            found.append(child)
            found.extend(self.descendants(child))
        return found

    def inherited_edges(self, node_id: str) -> list[str]:
        """Every dependency of `node_id` and of each of its ancestors, own first."""
        found: list[str] = []
        owner: str | None = node_id
        while owner is not None:
            found.extend(dep for d, dep in self.edges if d == owner and dep not in found)
            owner = self.parent(owner)
        return found


def _is_work(s: Snapshot, node_id: str) -> bool:
    return node_id in s.nodes and s.nodes[node_id].kind != NodeKind.DECISION


def _base_graph(s: Snapshot) -> Graph:
    graph: Graph = {}
    work = [n for n in s.nodes.values() if n.kind != NodeKind.DECISION]
    for n in work:
        graph[f"{n.id}.start"] = {f"{n.id}.implemented"}
        graph[f"{n.id}.implemented"] = {f"{n.id}.landed"}
        graph[f"{n.id}.landed"] = set()
    for n in work:
        if n.parent is not None and _is_work(s, n.parent) and n.status not in SET_ASIDE:
            graph[f"{n.id}.landed"].add(f"{n.parent}.implemented")
    for dependent, dependency in s.edges:
        if not (_is_work(s, dependent) and _is_work(s, dependency)):
            continue
        if s.status(dependency) == Status.SUPERSEDED:
            continue
        # An edge on a container gates every descendant's claim, each at the point where the
        # dependency's code reaches a branch that descendant builds on.
        for d in [dependent, *s.descendants(dependent)]:
            if _is_work(s, d):
                graph[f"{meeting(s, d, dependency)}.landed"].add(f"{d}.start")
    return graph


def _topological_rank(graph: Graph) -> dict[str, int]:
    predecessors: dict[str, set[str]] = {vertex: set() for vertex in graph}
    for vertex in sorted(graph):
        for successor in graph[vertex]:
            predecessors[successor].add(vertex)
    try:
        return {v: i for i, v in enumerate(TopologicalSorter(predecessors).static_order())}
    except CycleError:
        return {}


def _migration_order(s: Snapshot, repo: str, rank: dict[str, int]) -> list[str]:
    def reaches_main(node_id: str) -> str:
        return landing_chain(s, node_id)[-1]

    writers = [
        n
        for n in s.nodes.values()
        if n.repo == repo
        and n.writes_migration
        and n.kind not in CONTAINERS
        and n.status not in EXITS
        and s.status(reaches_main(n.id)) != Status.COMPLETED
    ]

    def key(n: SnapNode) -> tuple[int, int, int, str]:
        # Whoever already holds the chain keeps it: landed on a container branch first, then
        # in a step, then the rest in the order their code can reach main.
        held = 0 if n.status == Status.COMPLETED else 1 if n.status != Status.READY else 2
        main_rank = rank.get(f"{reaches_main(n.id)}.landed", 0)
        return held, main_rank, rank.get(f"{n.id}.start", 0), n.id

    return [n.id for n in sorted(writers, key=key)]


def migration_order(s: Snapshot, repo: str) -> list[str]:
    """The order `repo`'s migration chain is granted in: each writer waits until every earlier
    one's code reaches a branch it builds on. Discovery grants in this order, so the cycle
    check sees the same waits discovery will impose."""
    return _migration_order(s, repo, _topological_rank(_base_graph(s)))


def _with_migration_chain(s: Snapshot, graph: Graph) -> Graph:
    chained = {vertex: set(successors) for vertex, successors in graph.items()}
    rank = _topological_rank(graph)
    repos = sorted({n.repo for n in s.nodes.values() if n.writes_migration and n.repo})
    for repo in repos:
        order = _migration_order(s, repo, rank)
        for i, first in enumerate(order):
            for later in order[i + 1 :]:
                chained[f"{meeting(s, later, first)}.landed"].add(f"{later}.start")
    return chained


def _readable(loop: list[str]) -> list[str]:
    """`loop` runs along the edges; the result runs against them, so each vertex is followed by
    the one it waits on, starting at the smallest start vertex so one cycle prints one way."""
    waits = loop[::-1][:-1]
    first = min((v for v in waits if v.endswith(".start")), default=waits[0])
    i = waits.index(first)
    rotated = waits[i:] + waits[:i]
    return [*rotated, rotated[0]]


def _cycle(graph: Graph) -> list[str] | None:
    on_path: set[str] = set()
    done: set[str] = set()
    for root in sorted(graph):
        if root in done:
            continue
        path = [root]
        pending = [iter(sorted(graph[root]))]
        on_path.add(root)
        while pending:
            successor = next(pending[-1], None)
            if successor is None:
                finished = path.pop()
                on_path.discard(finished)
                done.add(finished)
                pending.pop()
            elif successor in on_path:
                return _readable([*path[path.index(successor) :], successor])
            elif successor not in done:
                path.append(successor)
                on_path.add(successor)
                pending.append(iter(sorted(graph.get(successor, ()))))
    return None


def find_cycle(s: Snapshot) -> list[str] | None:
    graph = _base_graph(s)
    return _cycle(graph) or _cycle(_with_migration_chain(s, graph))


def format_cycle(path: list[str]) -> str:
    return " ← ".join(path)
```

- [ ] **Step 4: Run the tests and the gates**, each in the foreground:

```bash
uv run --directory <worktree> pytest tests/unit/test_stepgraph.py -q; echo $?
uv run --directory <worktree> pytest -q; echo $?
uv run --directory <worktree> ruff check; echo $?
uv run --directory <worktree> ruff format --check; echo $?
uv run --directory <worktree> mypy; echo $?
```

Expected: `511 passed` and `0` for the first; every test passing and `0` for the full suite; `All checks passed!` and `0` for `ruff check`; nothing to reformat and `0` for `ruff format --check`; `Success: no issues found` and `0` for `mypy`. No existing test changes: nothing reads this module yet.

- [ ] **Step 5: Commit**

```bash
git -C <worktree> add src/taskmanager/engine/stepgraph.py tests/unit/test_stepgraph.py && git -C <worktree> commit -m "feat(engine): refuse cyclic writes with the expanded step graph and print the cycle"
```

### Task 7: Write-time validation

**Spec:** §2.3 (rules 1-8), §4.2 (decision link and effects on a busy node), §3.4
**Files:**
- Create: `src/taskmanager/engine/validation.py`
- Test: `tests/unit/test_validation.py`

**Interfaces:**
- Consumes: `MAIN`, `landing_target` (Task 5); `SnapNode`, `Snapshot`, `find_cycle`, `format_cycle` (Task 6); `Status`, `EXITS`, `Merge` (Task 1); `NodeKind`
- Produces: `Refusal`, `BranchFacts`, `validate`; Tasks 13 and 17 call `validate` on every verb and graph write, never on a step's own close

Rule 7 is structural: a node busy in `before` is refused when its status, its parent or its waits change (a status change, a move, a link to a decision, a dropped edge), which covers `defer`, `abandon`, `supersede`, `reset`, `move` and every decision effect; adding a plain dependency stays allowed, because it gates the next claim. `complete`, `review`, a landing's result and `release` also change a busy node's status, so a step's own close never goes through `validate`. Rule 6 checks every ancestor, not only the parent: a child under a deferred plan inside a `COMPLETED` spec could never land. `BranchFacts.base_matches` receives `chains.MAIN` or a node id; its implementation (Task 10 or 17) maps `MAIN` to `origin/main` and a node id to that node's branch.

One test per rule, each asserting exactly which (node, rule) pairs are refused, plus a parent write that strands a child, a `merge` change after the branch exists (refused, accepted when the branch was cut from the new target, accepted when no branch exists), and a cycle refusal quoting its path verbatim.

- [ ] **Step 1: Write the failing test** — `tests/unit/test_validation.py`:

```python
from dataclasses import dataclass, field, replace

import pytest

from taskmanager.core.enums import NodeKind
from taskmanager.core.status import DecisionStatus, Merge, Status
from taskmanager.engine.chains import MAIN
from taskmanager.engine.stepgraph import SnapNode, Snapshot
from taskmanager.engine.validation import Refusal, validate

SPEC, PLAN, TASK, DECISION = NodeKind.SPEC, NodeKind.PLAN, NodeKind.TASK, NodeKind.DECISION
PARENT = Merge.PARENT


@dataclass
class Branches:
    existing: set[str] = field(default_factory=set)
    cut_from: dict[str, str] = field(default_factory=dict)

    def branch_exists(self, node_id: str) -> bool:
        return node_id in self.existing

    def base_matches(self, node_id: str, new_target: str) -> bool:
        return self.cut_from.get(node_id) == new_target


def snap(*nodes: SnapNode, edges: list[tuple[str, str]] | None = None) -> Snapshot:
    return Snapshot({n.id: n for n in nodes}, edges or [])


def with_node(s: Snapshot, n: SnapNode) -> Snapshot:
    return Snapshot({**s.nodes, n.id: n}, list(s.edges))


def rules(
    before: Snapshot, after: Snapshot, touched: set[str], branches: Branches | None = None
) -> list[tuple[str, int]]:
    found = validate(before, after, touched, branches or Branches())
    return [(r.node_id, r.rule) for r in found]


PLAN_P = SnapNode("P", PLAN)
EMPTY = snap(PLAN_P)


def test_a_valid_write_is_accepted() -> None:
    after = with_node(EMPTY, SnapNode("T", TASK, parent="P", merge=PARENT))
    assert validate(EMPTY, after, {"T"}, Branches()) == []


def test_fix_without_review_is_refused() -> None:
    after = with_node(EMPTY, SnapNode("T", TASK, review=False, fix=True))
    assert rules(EMPTY, after, {"T"}) == [("T", 1)]


@pytest.mark.parametrize(
    ("parent", "child", "refused"),
    [
        (SnapNode("P", PLAN), SnapNode("T", TASK, fix=False), True),
        (SnapNode("P", PLAN), SnapNode("T", TASK, parent="P", fix=False), True),
        (SnapNode("P", PLAN), SnapNode("T", TASK, parent="P", merge=PARENT, fix=False), False),
        (
            SnapNode("P", PLAN, review=False, fix=False),
            SnapNode("T", TASK, parent="P", merge=PARENT, fix=False),
            True,
        ),
        (SnapNode("P", PLAN), SnapNode("T", TASK, review=False, fix=False), False),
    ],
)
def test_review_without_fix_needs_a_parent_that_reviews_and_fixes_the_landing(
    parent: SnapNode, child: SnapNode, refused: bool
) -> None:
    before = snap(parent)
    after = with_node(before, child)
    assert rules(before, after, {"T"}) == ([("T", 2)] if refused else [])


def test_a_parent_write_that_strands_a_child_is_refused_on_the_child() -> None:
    child = SnapNode("T", TASK, parent="P", merge=PARENT, fix=False)
    before = snap(PLAN_P, child)
    after = with_node(before, replace(PLAN_P, review=False, fix=False))
    assert rules(before, after, {"P"}) == [("T", 2)]


@pytest.mark.parametrize(
    "node",
    [SnapNode("S", SPEC, merge=PARENT), SnapNode("T", TASK, merge=PARENT)],
)
def test_merge_parent_on_a_spec_or_a_parentless_node_is_refused(node: SnapNode) -> None:
    after = snap(node)
    assert rules(snap(), after, {node.id}) == [(node.id, 3)]


@pytest.mark.parametrize(
    ("branches", "refused"),
    [
        (Branches(), False),
        (Branches(existing={"T"}, cut_from={"T": MAIN}), True),
        (Branches(existing={"T"}, cut_from={"T": "P"}), False),
    ],
)
def test_changing_where_a_node_lands_needs_its_branch_cut_from_the_new_target(
    branches: Branches, refused: bool
) -> None:
    before = snap(PLAN_P, SnapNode("T", TASK, parent="P"))
    after = with_node(before, SnapNode("T", TASK, parent="P", merge=PARENT))
    assert rules(before, after, {"T"}, branches) == ([("T", 4)] if refused else [])


def test_moving_a_node_whose_branch_exists_to_another_parent_branch_is_refused() -> None:
    q = SnapNode("Q", PLAN)
    before = snap(PLAN_P, q, SnapNode("T", TASK, parent="P", merge=PARENT))
    after = with_node(before, SnapNode("T", TASK, parent="Q", merge=PARENT))
    branches = Branches(existing={"T"}, cut_from={"T": "P"})
    assert rules(before, after, {"T"}, branches) == [("T", 4)]


def test_a_merge_change_that_keeps_the_target_is_not_a_retarget() -> None:
    before = snap(SnapNode("T", TASK))
    after = snap(SnapNode("T", TASK, merge=PARENT))
    branches = Branches(existing={"T"}, cut_from={"T": "elsewhere"})
    assert rules(before, after, {"T"}, branches) == [("T", 3)]


@pytest.mark.parametrize(("merge", "refused"), [(PARENT, True), (Merge.MAIN, False)])
def test_a_literal_origin_main_verification_is_refused_on_a_parent_landing(
    merge: Merge, refused: bool
) -> None:
    after = with_node(EMPTY, SnapNode("T", TASK, parent="P", merge=merge, literal_origin_main=True))
    assert rules(EMPTY, after, {"T"}) == ([("T", 5)] if refused else [])


COMPLETED_P = SnapNode("P", PLAN, status=Status.COMPLETED)
BUSY_P = SnapNode("P", PLAN, status=Status.REVIEWING, busy=True)


@pytest.mark.parametrize("holder", [COMPLETED_P, BUSY_P], ids=["completed", "busy"])
@pytest.mark.parametrize(
    ("child_before", "child_after"),
    [
        (None, SnapNode("T", TASK, parent="P")),
        (SnapNode("T", TASK, parent="Q"), SnapNode("T", TASK, parent="P")),
        (
            SnapNode("T", TASK, parent="P", status=Status.FAILED),
            SnapNode("T", TASK, parent="P"),
        ),
        (
            SnapNode("T", TASK, parent="P", status=Status.DEFERRED),
            SnapNode("T", TASK, parent="P"),
        ),
    ],
    ids=["new", "moved-in", "reopened-failed", "reopened-deferred"],
)
def test_nothing_arrives_under_a_completed_or_busy_container(
    holder: SnapNode, child_before: SnapNode | None, child_after: SnapNode
) -> None:
    before = snap(holder, SnapNode("Q", PLAN))
    if child_before is not None:
        before = with_node(before, child_before)
    after = with_node(before, child_after)
    assert rules(before, after, {"T"}) == [("T", 6)]


def test_nothing_arrives_under_a_container_whose_ancestor_completed() -> None:
    before = snap(
        SnapNode("S", SPEC, status=Status.COMPLETED),
        SnapNode("P", PLAN, parent="S", status=Status.DEFERRED),
    )
    after = with_node(before, SnapNode("T", TASK, parent="P"))
    assert rules(before, after, {"T"}) == [("T", 6)]


def test_a_completed_container_arriving_with_its_children_in_one_write_is_accepted() -> None:
    before = snap()
    after = snap(
        COMPLETED_P,
        SnapNode("T", TASK, parent="P", status=Status.COMPLETED),
    )
    assert rules(before, after, {"P", "T"}) == []


def test_a_child_already_under_a_completed_container_may_be_edited() -> None:
    before = snap(COMPLETED_P, SnapNode("T", TASK, parent="P", status=Status.COMPLETED))
    after = with_node(before, SnapNode("T", TASK, parent="P", status=Status.COMPLETED, repo="x"))
    assert rules(before, after, {"T"}) == []


BUSY_T = SnapNode("T", TASK, status=Status.IMPLEMENTING, busy=True)


@pytest.mark.parametrize(
    ("after_node", "after_edges"),
    [
        (replace(BUSY_T, status=Status.DEFERRED), [("T", "X")]),
        (replace(BUSY_T, status=Status.READY), [("T", "X")]),
        (replace(BUSY_T, parent="P"), [("T", "X")]),
        (BUSY_T, [("T", "X"), ("T", "D")]),
        (BUSY_T, []),
    ],
    ids=["deferred", "reset", "moved", "decision-linked", "edge-dropped"],
)
def test_a_busy_node_refuses_every_change_of_state_place_or_waits(
    after_node: SnapNode, after_edges: list[tuple[str, str]]
) -> None:
    others = [PLAN_P, SnapNode("X", TASK), SnapNode("D", DECISION, status=DecisionStatus.OPEN)]
    before = snap(BUSY_T, *others, edges=[("T", "X")])
    after = snap(after_node, *others, edges=after_edges)
    assert rules(before, after, {"T"}) == [("T", 7)]


def test_a_busy_node_may_gain_a_dependency_for_its_next_claim() -> None:
    before = snap(BUSY_T, SnapNode("X", TASK))
    after = snap(BUSY_T, SnapNode("X", TASK), edges=[("T", "X")])
    assert rules(before, after, {"T"}) == []


def test_a_write_that_closes_a_cycle_is_refused_with_the_path() -> None:
    before = snap(PLAN_P, SnapNode("A", TASK, parent="P", merge=PARENT))
    after = snap(PLAN_P, SnapNode("A", TASK, parent="P", merge=PARENT), edges=[("A", "P")])
    refusals = validate(before, after, {"A"}, Branches())
    assert refusals == [
        Refusal(
            "A",
            8,
            "this write makes the step graph cyclic: A.start ← P.landed ← P.implemented ← "
            "A.landed ← A.implemented ← A.start; remove an edge on the cycle or change where "
            "a node on it lands",
        )
    ]


def test_an_untouched_node_is_not_checked() -> None:
    broken = SnapNode("Z", TASK, review=False, fix=True)
    before = snap(PLAN_P, broken)
    after = with_node(before, SnapNode("T", TASK))
    assert rules(before, after, {"T"}) == []


def test_a_decision_is_not_held_to_node_flag_rules() -> None:
    after = snap(SnapNode("D", DECISION, status=DecisionStatus.OPEN, review=False))
    assert rules(snap(), after, {"D"}) == []
```

- [ ] **Step 2: Run it and watch it fail**

```bash
uv run --directory <worktree> pytest tests/unit/test_validation.py -q; echo $?
```

Expected: collection stops with `ModuleNotFoundError: No module named 'taskmanager.engine.validation'`, `1 error`, exit code `2`.

- [ ] **Step 3: Implement** — `src/taskmanager/engine/validation.py`:

```python
"""The rules every write must keep. A write is described by the snapshot before it, the snapshot
after it, and the nodes it touched; a refusal names the node, the rule broken and the fix.

A step's own close (complete, review, landing, release) is the lease holder's write, made
through the lifecycle, and does not come here: rule 7 guards a busy node against everyone else."""

from dataclasses import dataclass
from typing import Protocol

from taskmanager.core.enums import NodeKind
from taskmanager.core.status import EXITS, Merge, Status
from taskmanager.engine.chains import MAIN, landing_target
from taskmanager.engine.stepgraph import SnapNode, Snapshot, find_cycle, format_cycle

_SET_ASIDE_OR_FAILED = EXITS | {Status.FAILED}


@dataclass(frozen=True)
class Refusal:
    node_id: str
    rule: int
    message: str


class BranchFacts(Protocol):
    def branch_exists(self, node_id: str) -> bool: ...
    def base_matches(self, node_id: str, new_target: str) -> bool: ...


def _flags(after: Snapshot, n: SnapNode) -> list[Refusal]:
    refusals: list[Refusal] = []
    if n.fix and not n.review:
        refusals.append(Refusal(n.id, 1, f"{n.id}: fix is on with review off; turn review on"))
    if n.review and not n.fix:
        parent = after.nodes.get(n.parent) if n.parent is not None else None
        fixed_above = (
            n.merge == Merge.PARENT and parent is not None and parent.review and parent.fix
        )
        if not fixed_above:
            refusals.append(
                Refusal(
                    n.id,
                    2,
                    f"{n.id}: review is on with fix off, so a rejection must land on a parent "
                    "that reviews and fixes it; set merge=parent under such a parent, or "
                    "turn fix on",
                )
            )
    if n.merge == Merge.PARENT and (n.kind == NodeKind.SPEC or n.parent is None):
        refusals.append(
            Refusal(n.id, 3, f"{n.id}: a spec or a parentless node lands on main; set merge=main")
        )
    if n.merge == Merge.PARENT and n.literal_origin_main:
        refusals.append(
            Refusal(
                n.id,
                5,
                f"{n.id}: lands on its parent's branch but a test_command names origin/main; "
                "read the landing target from TM_VERIFY_REF instead",
            )
        )
    return refusals


def _retarget(
    before: Snapshot, after: Snapshot, n: SnapNode, branches: BranchFacts
) -> list[Refusal]:
    if n.id not in before.nodes or not branches.branch_exists(n.id):
        return []
    new_target = landing_target(after, n.id)
    if new_target == landing_target(before, n.id) or branches.base_matches(n.id, new_target):
        return []
    # A branch cut from a container's branch would carry that container's unreviewed code.
    where = "main" if new_target == MAIN else new_target
    return [
        Refusal(
            n.id,
            4,
            f"{n.id}: its branch exists and was not cut from {where}; reopen it with "
            "--new-branch before changing where it lands",
        )
    ]


def _placement(before: Snapshot, after: Snapshot, n: SnapNode) -> list[Refusal]:
    old = before.nodes.get(n.id)
    arrived = old is None or old.parent != n.parent
    reopened = (
        old is not None
        and old.status in _SET_ASIDE_OR_FAILED
        and n.status not in _SET_ASIDE_OR_FAILED
    )
    if not (arrived or reopened):
        return []
    refusals: list[Refusal] = []
    ancestor = n.parent
    while ancestor is not None and ancestor in after.nodes:
        # Judged by the ancestor's state before this write: an ancestor created in the same
        # write (a restore bringing a completed plan with its children) refuses nothing.
        holder = before.nodes.get(ancestor)
        if holder is None:
            ancestor = after.nodes[ancestor].parent
            continue
        if holder.status == Status.COMPLETED:
            refusals.append(
                Refusal(n.id, 6, f"{n.id}: {ancestor} is COMPLETED; file a new plan instead")
            )
        elif holder.busy:
            refusals.append(
                Refusal(
                    n.id,
                    6,
                    f"{n.id}: {ancestor} is in a step; wait for it to end, or stop it",
                )
            )
        ancestor = holder.parent
    return refusals


def _deps(s: Snapshot, node_id: str) -> set[str]:
    return {dep for d, dep in s.edges if d == node_id}


def _busy(before: Snapshot, after: Snapshot, n: SnapNode) -> list[Refusal]:
    old = before.nodes.get(n.id)
    if old is None or not old.busy:
        return []
    old_deps, new_deps = _deps(before, n.id), _deps(after, n.id)
    linked_decision = any(
        after.nodes[dep].kind == NodeKind.DECISION
        for dep in new_deps - old_deps
        if dep in after.nodes
    )
    dropped_edge = not old_deps <= new_deps
    moved_or_set = old.parent != n.parent or old.status != n.status
    if not (moved_or_set or linked_decision or dropped_edge):
        return []
    return [
        Refusal(
            n.id,
            7,
            f"{n.id} holds a live lease or a running job; wait for its step to end, or stop "
            "it, then retry",
        )
    ]


def validate(
    before: Snapshot, after: Snapshot, touched: set[str], branches: BranchFacts
) -> list[Refusal]:
    present = {node_id for node_id in touched if node_id in after.nodes}
    # A write to a container can break a rule its children keep only through it.
    checked = present | {child for node_id in present for child in after.children(node_id)}
    refusals: list[Refusal] = []
    for node_id in sorted(checked):
        n = after.nodes[node_id]
        if n.kind == NodeKind.DECISION:
            continue
        refusals += _flags(after, n)
        refusals += _retarget(before, after, n, branches)
        refusals += _placement(before, after, n)
        refusals += _busy(before, after, n)
    cycle = find_cycle(after)
    if cycle is not None:
        refusals.append(
            Refusal(
                cycle[0].rsplit(".", 1)[0],
                8,
                f"this write makes the step graph cyclic: {format_cycle(cycle)}; "
                "remove an edge on the cycle or change where a node on it lands",
            )
        )
    return refusals
```

- [ ] **Step 4: Run the tests and the gates**, each in the foreground:

```bash
uv run --directory <worktree> pytest tests/unit/test_validation.py -q; echo $?
uv run --directory <worktree> pytest -q; echo $?
uv run --directory <worktree> ruff check; echo $?
uv run --directory <worktree> ruff format --check; echo $?
uv run --directory <worktree> mypy; echo $?
```

Expected: `36 passed` and `0` for the first; every test passing and `0` for the full suite; `All checks passed!` and `0` for `ruff check`; nothing to reformat and `0` for `ruff format --check`; `Success: no issues found` and `0` for `mypy`. No existing test changes: nothing reads this module yet.

- [ ] **Step 5: Commit**

```bash
git -C <worktree> add src/taskmanager/engine/validation.py tests/unit/test_validation.py && git -C <worktree> commit -m "feat(engine): validate every write against the flag, target, placement and busy-node rules"
```

---

### Task 8: Fresh storage: files, tombstones, archive, node columns, conditions

**Spec:** §2.1, §4.3 (the `node_conditions` table), §5.1 (leases beside the nodes), §9.1
**Files:**
- Modify: `src/taskmanager/db/schema.py` (rewritten: `STATE_SCHEMA_SQL` replaces `SPEC_SCHEMA_SQL`, `INDEX_STATE_SQL` and `RUNTIME_SCHEMA_SQL`; `SCHEMA_VERSION`; the lifecycle columns with their `CHECK`s; `node_conditions`; `leases` and `file_locks` move in)
- Modify: `src/taskmanager/db/connection.py` (rewritten: `state.db`, `get_state_connection` with `get_spec_connection`/`get_runtime_connection` as aliases, `in_transaction`, `is_pre_lifecycle`, `PreLifecycleEstate`, tombstones in `init_all`, `archive_pre_lifecycle`; `_ensure_spec_migrations` deleted)
- Modify: `src/taskmanager/db/__init__.py` (exports)
- Modify: `src/taskmanager/core/models.py` (`Node` lifecycle fields and container flag defaults; new `Condition`)
- Modify: `src/taskmanager/db/node_repo.py` (`_NODE_COLUMNS`, `_status`, `save_node`, `get_node`, `list_nodes`, `_row_to_node`; new `get_conditions`, `add_condition`, `remove_condition`)
- Modify: `src/taskmanager/db/runtime_repo.py` (`acquire_lease`, `heartbeat`, `release_lease`, `sweep_expired_leases` commit through `spec_commit`, so lease writes join an open transaction)
- Modify: `src/taskmanager/engine/graph.py` (status parameters and returns widened for `NodeStatus | Status | DecisionStatus`)
- Modify: `src/taskmanager/engine/operations.py` (`_SWEEP_BACK` key type)
- Modify: `src/taskmanager/engine/decisions.py` (`DECISION_STATUS_LABELS` key type)
- Modify: `tests/unit/test_db_schema.py` (delete `test_database_initialization`, `test_ensure_spec_migrations_heals_a_database_missing_only_the_uniqueness_index`, `test_ensure_spec_migrations_skips_remediation_once_healthy`)
- Modify: `tests/unit/test_repos.py` (delete `test_spec_migrations_still_self_heal_a_dropped_table`)
- Modify: `tests/unit/test_search_engine.py` (delete `test_the_index_state_table_exists_in_a_database_made_before_it`)
- Test: `tests/unit/test_storage.py`

**Interfaces:**
- Consumes: `Status`, `DecisionStatus`, `Outcome`, `Merge`, `ConditionStage` (Task 1)
- Produces: `DatabaseManager.get_state_connection`, `.in_transaction`, `.is_pre_lifecycle`, `DatabaseManager.archive_pre_lifecycle(root) -> Path`, `PreLifecycleEstate`, `SCHEMA_VERSION`, `STATE_SCHEMA_SQL`; `Node` lifecycle fields; `Condition`; `NodeRepository.get_conditions/add_condition/remove_condition`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_storage.py`:

```python
import re
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

import taskmanager
from taskmanager.core.enums import NodeKind, NodeStatus
from taskmanager.core.models import Condition, Lease, Node
from taskmanager.core.status import ConditionStage, DecisionStatus, Merge, Outcome, Status
from taskmanager.db.connection import DatabaseManager, PreLifecycleEstate
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.db.schema import SCHEMA_VERSION

PRE_LIFECYCLE = (
    "this directory holds a pre-lifecycle estate: run `tm init --archive` to move it to "
    "`.taskmanager/archive-<timestamp>/` and start fresh, then re-import the ongoing work"
)


def _fresh(tmp_path: Path) -> DatabaseManager:
    db = DatabaseManager(tmp_path / ".taskmanager")
    db.init_all()
    return db


def _old_estate(tm_dir: Path) -> dict[str, bytes]:
    """A directory as a pre-lifecycle tm left it: three SQLite files, one row each."""
    tm_dir.mkdir(parents=True)
    for name in ("spec.db", "runtime.db", "ledger.db"):
        conn = sqlite3.connect(tm_dir / name)
        conn.execute("CREATE TABLE t (v TEXT)")
        conn.execute("INSERT INTO t VALUES (?)", (name,))
        conn.commit()
        conn.close()
    return {name: (tm_dir / name).read_bytes() for name in ("spec.db", "runtime.db", "ledger.db")}


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_init_creates_state_and_ledger_at_the_current_schema_version(tmp_path: Path) -> None:
    db = _fresh(tmp_path)
    with db.get_state_connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone() == (SCHEMA_VERSION,)
        assert {
            "nodes",
            "node_sections",
            "node_relations",
            "node_verifications",
            "node_conditions",
            "nodes_fts",
            "embedding_metadata",
            "vec_nodes",
            "index_state",
            "leases",
            "file_locks",
        } <= _tables(conn)
    with db.get_ledger_connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone() == (SCHEMA_VERSION,)
        assert "ledger_events" in _tables(conn)


@pytest.mark.parametrize("name", ["spec.db", "runtime.db"])
def test_init_leaves_a_tombstone_an_old_binary_fails_to_open(tmp_path: Path, name: str) -> None:
    _fresh(tmp_path)
    tombstone = tmp_path / ".taskmanager" / name
    assert f"taskmanager {taskmanager.__version__}" in tombstone.read_text(encoding="utf-8")
    old_binary = sqlite3.connect(tombstone)
    with pytest.raises(sqlite3.DatabaseError, match="file is not a database"):
        old_binary.execute("PRAGMA journal_mode = WAL;")
    old_binary.close()


def test_a_tombstoned_directory_opens_as_the_new_estate(tmp_path: Path) -> None:
    _fresh(tmp_path)
    again = DatabaseManager(tmp_path / ".taskmanager")
    assert not again.is_pre_lifecycle()
    assert again.is_initialized()


@pytest.mark.parametrize(
    "call",
    [
        lambda db: db.init_all(),
        lambda db: NodeRepository(db).get_node("any"),
        lambda db: RuntimeRepository(db).get_lease("any"),
        lambda db: db.get_ledger_connection().__enter__(),
    ],
    ids=["init", "node-read", "lease-read", "ledger"],
)
def test_a_pre_lifecycle_estate_refuses_every_open_with_the_archive_instruction(
    tmp_path: Path, call: Callable[[DatabaseManager], object]
) -> None:
    tm_dir = tmp_path / ".taskmanager"
    before = _old_estate(tm_dir)
    db = DatabaseManager(tm_dir)
    with pytest.raises(PreLifecycleEstate) as refused:
        call(db)
    assert str(refused.value) == PRE_LIFECYCLE
    assert {n: (tm_dir / n).read_bytes() for n in before} == before
    assert not (tm_dir / "state.db").exists()


def test_archive_moves_the_old_estate_intact_and_a_fresh_init_follows(tmp_path: Path) -> None:
    tm_dir = tmp_path / ".taskmanager"
    before = _old_estate(tm_dir)
    (tm_dir / "config.yaml").write_text("lease_ttl: 60\n", encoding="utf-8")

    archive = DatabaseManager.archive_pre_lifecycle(tmp_path)

    assert archive.parent == tm_dir
    assert re.fullmatch(r"archive-\d{8}T\d{12}Z", archive.name)
    assert {n: (archive / n).read_bytes() for n in before} == before
    assert (tm_dir / "config.yaml").read_text(encoding="utf-8") == "lease_ttl: 60\n"
    db = DatabaseManager(tm_dir)
    db.init_all()
    assert NodeRepository(db).list_nodes() == []


def test_archive_refuses_a_directory_holding_no_pre_lifecycle_estate(tmp_path: Path) -> None:
    _fresh(tmp_path)
    with pytest.raises(ValueError, match="no pre-lifecycle estate"):
        DatabaseManager.archive_pre_lifecycle(tmp_path)
    assert [p.name for p in (tmp_path / ".taskmanager").iterdir() if p.is_dir()] == []


def test_every_lifecycle_column_round_trips(tmp_path: Path) -> None:
    repo = NodeRepository(_fresh(tmp_path))
    node = Node(
        id="T1",
        kind=NodeKind.TASK,
        title="t",
        status=Status.REVIEWING,
        claimed_from=Status.FIXED,
        review=True,
        fix=False,
        merge=Merge.PARENT,
        outcome=Outcome.MERGE_FAILED,
        verdict="rebuilt the index",
        fix_for=Outcome.REJECT,
        review_cycles=2,
        merge_attempts=1,
        step_failures=1,
        branch="tm/T1@1",
        requires=["figma"],
        land_order=["core", "web"],
    )
    repo.save_node(node)
    loaded = repo.get_node("T1")
    assert loaded is not None
    assert loaded.model_dump(exclude={"created_at", "updated_at"}) == node.model_dump(
        exclude={"created_at", "updated_at"}
    )


@pytest.mark.parametrize(
    ("kind", "review", "fix"),
    [
        (NodeKind.TASK, True, True),
        (NodeKind.PLAN, False, False),
        (NodeKind.SPEC, False, False),
        (NodeKind.DECISION, True, True),
    ],
)
def test_a_container_defaults_to_no_review_and_a_task_to_review_and_fix(
    kind: NodeKind, review: bool, fix: bool
) -> None:
    node = Node(id="N", kind=kind, title="n")
    assert (node.review, node.fix) == (review, fix)


def test_a_container_keeps_the_flags_its_planner_set() -> None:
    node = Node(id="P", kind=NodeKind.PLAN, title="p", review=True, fix=True)
    assert (node.review, node.fix) == (True, True)


@pytest.mark.parametrize(
    ("status", "vocabulary"),
    [
        (NodeStatus.NOT_STARTED, NodeStatus),
        (NodeStatus.COMPLETED, NodeStatus),
        (Status.READY, Status),
        (Status.IMPLEMENTED, Status),
        (Status.FAILED, Status),
        (DecisionStatus.OPEN, DecisionStatus),
        (DecisionStatus.ANSWERED, DecisionStatus),
    ],
)
def test_a_stored_status_reads_back_in_its_own_vocabulary(
    tmp_path: Path, status: NodeStatus | Status | DecisionStatus, vocabulary: type
) -> None:
    repo = NodeRepository(_fresh(tmp_path))
    repo.save_node(Node(id="N", kind=NodeKind.TASK, title="n", status=status))
    loaded = repo.get_node("N")
    assert loaded is not None
    assert loaded.status == status
    assert isinstance(loaded.status, vocabulary)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("fix", "1, review = 0"),
        ("merge", "'sideways'"),
        ("outcome", "'maybe'"),
        ("fix_for", "'maybe'"),
        ("claimed_from", "'MERGING'"),
        ("review", "2"),
        ("review_cycles", "-1"),
        ("merge_attempts", "-1"),
        ("step_failures", "-1"),
    ],
)
def test_the_schema_refuses_an_out_of_range_lifecycle_value(
    tmp_path: Path, column: str, value: str
) -> None:
    db = _fresh(tmp_path)
    NodeRepository(db).save_node(Node(id="T1", kind=NodeKind.TASK, title="t"))
    with db.get_state_connection() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(f"UPDATE nodes SET {column} = {value} WHERE id = 'T1'")


def test_conditions_are_numbered_listed_in_order_and_removed(tmp_path: Path) -> None:
    repo = NodeRepository(_fresh(tmp_path))
    repo.save_node(Node(id="T1", kind=NodeKind.TASK, title="t"))
    first = repo.add_condition(Condition(node_id="T1", needs="staging up", command="true"))
    second = repo.add_condition(
        Condition(node_id="T1", needs="gate green", command="exit 0", stage=ConditionStage.LANDING)
    )
    assert (first.idx, second.idx) == (1, 2)
    assert repo.get_conditions("T1") == [first, second]
    assert repo.remove_condition("T1", 1) is True
    assert repo.remove_condition("T1", 1) is False
    assert repo.get_conditions("T1") == [second]
    assert repo.add_condition(Condition(node_id="T1", needs="n", command="true")).idx == 3


def test_a_nodes_conditions_go_with_it(tmp_path: Path) -> None:
    db = _fresh(tmp_path)
    repo = NodeRepository(db)
    repo.save_node(Node(id="T1", kind=NodeKind.TASK, title="t"))
    repo.add_condition(Condition(node_id="T1", needs="n", command="true"))
    with db.get_state_connection() as conn:
        conn.execute("DELETE FROM nodes WHERE id = 'T1'")
        conn.commit()
    assert repo.get_conditions("T1") == []


@pytest.mark.parametrize(
    ("command", "stage"), [("   ", "claim"), ("true", "later")], ids=["blank", "stage"]
)
def test_the_schema_refuses_a_blank_command_or_an_unknown_stage(
    tmp_path: Path, command: str, stage: str
) -> None:
    db = _fresh(tmp_path)
    NodeRepository(db).save_node(Node(id="T1", kind=NodeKind.TASK, title="t"))
    with db.get_state_connection() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO node_conditions (node_id, idx, needs, command, stage) "
            "VALUES ('T1', 1, 'n', ?, ?)",
            (command, stage),
        )


def test_a_lease_write_joins_an_open_node_transaction(tmp_path: Path) -> None:
    db = _fresh(tmp_path)
    nodes, leases = NodeRepository(db), RuntimeRepository(db)
    with pytest.raises(RuntimeError), nodes.transaction():
        assert db.in_transaction
        nodes.save_node(Node(id="T1", kind=NodeKind.TASK, title="t"))
        leases.acquire_lease(
            Lease(task_id="T1", agent_id="a", session_id="s", branch_name="tm/T1"), []
        )
        raise RuntimeError("the step failed after its lease was written")
    assert not db.in_transaction
    assert nodes.get_node("T1") is None
    assert leases.get_lease("T1") is None
```

In `tests/unit/test_db_schema.py`, delete the whole of `test_database_initialization`, `test_ensure_spec_migrations_heals_a_database_missing_only_the_uniqueness_index`, `test_ensure_spec_migrations_skips_remediation_once_healthy`.

In `tests/unit/test_repos.py`, delete the whole of `test_spec_migrations_still_self_heal_a_dropped_table`.

In `tests/unit/test_search_engine.py`, delete the whole of `test_the_index_state_table_exists_in_a_database_made_before_it`.

- [ ] **Step 2: Run it and watch it fail**

```bash
uv run --directory <worktree> pytest tests/unit/test_storage.py -q; echo $?
```

Expected: `ImportError: cannot import name 'PreLifecycleEstate' from 'taskmanager.db.connection'` during collection, exit 2.

- [ ] **Step 3: Implement**

The deleted tests pin the self-healing `ALTER`s and the lazy `index_state` creation, which §9.1 removes: a fresh estate is created whole by `tm init`, and an old one is refused, never healed. `state.db` holds every table a claim touches, so `get_runtime_connection` is the same per-thread connection as `get_state_connection`; `runtime.db` and `spec.db` are tombstones from now on. The CLI surfaces `PreLifecycleEstate` and gains `init --archive` in Task 18; until then a pre-lifecycle directory refuses with the exception's message.

Replace the whole of `src/taskmanager/db/schema.py` with:

```python
# The schema a fresh `tm init` writes. A later change bumps this and migrates by user_version.
SCHEMA_VERSION = 1

STATE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    status TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 50,
    ordinal INTEGER NOT NULL DEFAULT 0,
    target_repo TEXT,
    acceptable_models TEXT NOT NULL DEFAULT '[]',
    frontmatter_json TEXT NOT NULL DEFAULT '{}',
    claimed_from TEXT CHECK (claimed_from IN ('READY', 'IMPLEMENTED', 'REVIEWED', 'FIXED')),
    review INTEGER NOT NULL DEFAULT 1 CHECK (review IN (0, 1)),
    fix INTEGER NOT NULL DEFAULT 1 CHECK (fix IN (0, 1)),
    merge TEXT NOT NULL DEFAULT 'main' CHECK (merge IN ('parent', 'main')),
    outcome TEXT CHECK (outcome IN ('approve', 'reject', 'merge_failed')),
    verdict TEXT,
    fix_for TEXT CHECK (fix_for IN ('approve', 'reject', 'merge_failed')),
    review_cycles INTEGER NOT NULL DEFAULT 0 CHECK (review_cycles >= 0),
    merge_attempts INTEGER NOT NULL DEFAULT 0 CHECK (merge_attempts >= 0),
    step_failures INTEGER NOT NULL DEFAULT 0 CHECK (step_failures >= 0),
    branch TEXT,
    requires TEXT NOT NULL DEFAULT '[]',
    land_order TEXT NOT NULL DEFAULT '[]',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CHECK (fix <= review)
);

CREATE TABLE IF NOT EXISTS node_sections (
    node_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    section_key TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    header TEXT NOT NULL,
    content TEXT NOT NULL,
    PRIMARY KEY (node_id, section_key)
);

CREATE TABLE IF NOT EXISTS node_relations (
    source_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    target_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    relation_type TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (source_id, target_id, relation_type)
);

CREATE TABLE IF NOT EXISTS node_verifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    verification_type TEXT NOT NULL,
    target_path TEXT NOT NULL,
    expected_pattern TEXT,
    codegraph_query_json TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_node_verifications ON node_verifications (
    node_id, verification_type, target_path, COALESCE(expected_pattern, '')
);

CREATE TABLE IF NOT EXISTS node_conditions (
    node_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    needs TEXT NOT NULL,
    command TEXT NOT NULL CHECK (trim(command) <> ''),
    stage TEXT NOT NULL DEFAULT 'claim' CHECK (stage IN ('claim', 'landing')),
    PRIMARY KEY (node_id, idx)
);

CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
    node_id UNINDEXED,
    title,
    frontmatter_text,
    content_text,
    tokenize='porter unicode61'
);

CREATE TABLE IF NOT EXISTS embedding_metadata (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    model_name TEXT NOT NULL,
    dimensions INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- One row per embedded (node, target, section): the hash of the text its vectors were made from.
CREATE TABLE IF NOT EXISTS index_state (
    node_id TEXT NOT NULL,
    target_type TEXT NOT NULL,
    section_key TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    PRIMARY KEY (node_id, target_type, section_key)
);

-- Leases live beside the nodes so a claim checks and writes both in one transaction.
CREATE TABLE IF NOT EXISTS leases (
    task_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    account_id TEXT,
    worktree_path TEXT,
    branch_name TEXT NOT NULL,
    acquired_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_heartbeat TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    ttl_seconds INTEGER NOT NULL DEFAULT 300
);

CREATE TABLE IF NOT EXISTS file_locks (
    file_path TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES leases(task_id) ON DELETE CASCADE,
    lock_type TEXT NOT NULL DEFAULT 'write'
);
"""


def vec_nodes_sql(dimensions: int) -> str:
    # No primary key: a node holds one vector per title, section and chunk.
    return f"""
    CREATE VIRTUAL TABLE IF NOT EXISTS vec_nodes USING vec0(
        node_id TEXT,
        target_type TEXT,
        section_key TEXT,
        embedding FLOAT[{dimensions}] DISTANCE_METRIC=cosine
    );
    """


LEDGER_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ledger_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    actor_id TEXT NOT NULL,
    command TEXT NOT NULL,
    target_id TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}',
    diff_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_ledger_target ON ledger_events(target_id);
"""
```

Replace the whole of `src/taskmanager/db/connection.py` with:

```python
import sqlite3
import threading
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import sqlite_vec  # type: ignore[import-untyped]

import taskmanager
from taskmanager.db.schema import (
    LEDGER_SCHEMA_SQL,
    SCHEMA_VERSION,
    STATE_SCHEMA_SQL,
    vec_nodes_sql,
)

_SQLITE_HEADER = b"SQLite format 3\x00"
# The database files a pre-lifecycle tm kept; an archive moves each with its WAL and shared memory.
_LEGACY_FILES = ("spec.db", "runtime.db", "ledger.db")
# Written as plain text where a pre-lifecycle tm looks for its databases, so it fails with "file is
# not a database" instead of silently creating an empty estate beside this one.
_TOMBSTONES = ("spec.db", "runtime.db")

PRE_LIFECYCLE_MESSAGE = (
    "this directory holds a pre-lifecycle estate: run `tm init --archive` to move it to "
    "`.taskmanager/archive-<timestamp>/` and start fresh, then re-import the ongoing work"
)


class PreLifecycleEstate(Exception):
    def __init__(self) -> None:
        super().__init__(PRE_LIFECYCLE_MESSAGE)


def _is_sqlite(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return fh.read(len(_SQLITE_HEADER)) == _SQLITE_HEADER
    except OSError:
        return False


class DatabaseManager:
    def __init__(self, taskmanager_dir: Path) -> None:
        self.taskmanager_dir = taskmanager_dir
        self.dir = taskmanager_dir
        self.state_db = taskmanager_dir / "state.db"
        self.ledger_db = taskmanager_dir / "ledger.db"
        # One connection per database *per thread*, reused for that thread's lifetime rather
        # than reopened on every get_*_connection() call (reopening, with its extension load,
        # dominated web load time). Per thread because the web server runs its database routes
        # in a threadpool: one shared connection interleaves concurrent cursors, so a
        # fetchone() returns another request's row or None. Each connection sees every other
        # connection's commits through WAL, so this changes lifecycle only, not isolation.
        self._local = threading.local()
        # Every connection with the thread that owns it. The web server's worker threads retire
        # after a few idle seconds, and a retired thread's thread-local dict is only dropped when
        # the garbage collector gets to it, so its connection would hold the database's file
        # descriptors open indefinitely: one leaked set per retired thread, until the process
        # hit its fd limit and every new thread failed with "unable to open database file".
        # Closing the dead threads' connections before opening a new one keeps the open set
        # bounded by the threads alive at once.
        self._all_conns: list[tuple[threading.Thread, sqlite3.Connection]] = []
        self._all_conns_lock = threading.Lock()

    def is_pre_lifecycle(self) -> bool:
        return not self.state_db.exists() and _is_sqlite(self.taskmanager_dir / "spec.db")

    def _create_connection(self, db_path: Path, load_vec: bool = False) -> sqlite3.Connection:
        if self.is_pre_lifecycle():
            raise PreLifecycleEstate()
        self.taskmanager_dir.mkdir(parents=True, exist_ok=True)
        self._close_dead_threads_connections()
        # check_same_thread=False only so a dead thread's connection, and close(), can be closed
        # from whichever thread gets there; no connection is ever used by a thread other than
        # its own.
        conn = sqlite3.connect(str(db_path), timeout=5.0, check_same_thread=False)
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        conn.execute("PRAGMA foreign_keys = ON;")
        if load_vec:
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
        with self._all_conns_lock:
            self._all_conns.append((threading.current_thread(), conn))
        return conn

    def _close_dead_threads_connections(self) -> None:
        dead: list[tuple[threading.Thread, sqlite3.Connection]] = []
        with self._all_conns_lock:
            alive, self._all_conns = self._all_conns, []
            for owner, conn in alive:
                (self._all_conns if owner.is_alive() else dead).append((owner, conn))
        for _, conn in dead:
            conn.close()

    def _thread_conn(self, name: str, db_path: Path, load_vec: bool = False) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._local, name, None)
        if conn is None:
            conn = self._create_connection(db_path, load_vec=load_vec)
            setattr(self._local, name, conn)
        return conn

    @property
    def _spec_tx_depth(self) -> int:
        """>0 while this thread holds `spec_transaction()` open: its repository calls skip
        their own commit, so a run of writes lands as one commit or none. Per thread, like the
        connection, so another thread's writes keep committing."""
        return int(getattr(self._local, "spec_tx_depth", 0))

    @_spec_tx_depth.setter
    def _spec_tx_depth(self, value: int) -> None:
        self._local.spec_tx_depth = value

    @property
    def in_transaction(self) -> bool:
        return self._spec_tx_depth > 0

    @contextmanager
    def get_state_connection(self) -> Generator[sqlite3.Connection]:
        yield self._thread_conn("state", self.state_db, load_vec=True)

    # Nodes, leases and locks share one database, so every repository's writes can join one
    # transaction; both other names still have callers.
    get_spec_connection = get_state_connection
    get_runtime_connection = get_state_connection

    def spec_commit(self, conn: sqlite3.Connection) -> None:
        """The commit every repository write ends with -- except while `spec_transaction()`
        is open, where the caller wants one commit (or one rollback) for the whole run rather
        than one per write, so a later write failing does not leave an earlier one visible."""
        if self._spec_tx_depth == 0:
            conn.commit()

    @contextmanager
    def spec_transaction(self) -> Generator[sqlite3.Connection]:
        with self.get_state_connection() as conn:
            self._spec_tx_depth += 1
            try:
                yield conn
            except BaseException:
                if self._spec_tx_depth == 1:
                    conn.rollback()
                raise
            else:
                if self._spec_tx_depth == 1:
                    conn.commit()
            finally:
                self._spec_tx_depth -= 1

    @contextmanager
    def get_ledger_connection(self) -> Generator[sqlite3.Connection]:
        yield self._thread_conn("ledger", self.ledger_db)

    def close(self) -> None:
        """Close every thread's cached connections. Optional -- process exit does this too --
        but a long-lived caller (the web server) that wants to drop file handles explicitly can."""
        with self._all_conns_lock:
            conns, self._all_conns = self._all_conns, []
        for _, conn in conns:
            conn.close()
        self._local = threading.local()

    def init_all(self, vector_dimensions: int = 384) -> None:
        with self.get_state_connection() as conn:
            conn.executescript(STATE_SCHEMA_SQL)
            conn.execute(vec_nodes_sql(vector_dimensions))
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.commit()

        with self.get_ledger_connection() as conn:
            conn.executescript(LEDGER_SCHEMA_SQL)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.commit()

        tombstone = (
            f"taskmanager {taskmanager.__version__} owns this directory; its estate is in "
            "state.db. This file is not a database, so an older tm fails here instead of "
            "opening an empty estate.\n"
        )
        for name in _TOMBSTONES:
            (self.taskmanager_dir / name).write_text(tombstone, encoding="utf-8")

    def is_initialized(self) -> bool:
        if not self.state_db.exists():
            return False
        try:
            with self.get_state_connection() as conn:
                row = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='nodes'"
                ).fetchone()
                return bool(row)
        except sqlite3.Error, OSError:
            return False

    @staticmethod
    def archive_pre_lifecycle(root: Path) -> Path:
        """Move a pre-lifecycle estate's databases under `.taskmanager/archive-<timestamp>/`,
        untouched, so `init_all` can start a fresh one. Nothing is ever deleted."""
        tm_dir = root / ".taskmanager"
        if not DatabaseManager(tm_dir).is_pre_lifecycle():
            raise ValueError(f"{tm_dir} holds no pre-lifecycle estate to archive")
        stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%S%fZ")
        archive = tm_dir / f"archive-{stamp}"
        archive.mkdir()
        for name in _LEGACY_FILES:
            for suffix in ("", "-wal", "-shm"):
                src = tm_dir / f"{name}{suffix}"
                if src.exists():
                    src.rename(archive / src.name)
        return archive
```

Replace the whole of `src/taskmanager/db/__init__.py` with:

```python
from taskmanager.db.connection import DatabaseManager as DatabaseManager
from taskmanager.db.connection import PreLifecycleEstate as PreLifecycleEstate
from taskmanager.db.ledger_repo import LedgerRepository as LedgerRepository
from taskmanager.db.node_repo import NodeRepository as NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository as RuntimeRepository
from taskmanager.db.schema import LEDGER_SCHEMA_SQL as LEDGER_SCHEMA_SQL
from taskmanager.db.schema import STATE_SCHEMA_SQL as STATE_SCHEMA_SQL

__all__ = [
    "LEDGER_SCHEMA_SQL",
    "STATE_SCHEMA_SQL",
    "DatabaseManager",
    "LedgerRepository",
    "NodeRepository",
    "PreLifecycleEstate",
    "RuntimeRepository",
]
```

In `src/taskmanager/core/models.py`, replace:

```python
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from taskmanager.core.enums import (
    LedgerCommand,
    LockType,
    NodeKind,
    NodeStatus,
    RelationType,
    VerificationType,
)
```

with:

```python
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from taskmanager.core.enums import (
    LedgerCommand,
    LockType,
    NodeKind,
    NodeStatus,
    RelationType,
    VerificationType,
)
from taskmanager.core.status import ConditionStage, DecisionStatus, Merge, Outcome, Status

_CONTAINERS = frozenset({NodeKind.PLAN, NodeKind.SPEC})
```

In `src/taskmanager/core/models.py`, replace `Node` in full with:

```python
class Node(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: NodeKind
    title: str
    status: NodeStatus | Status | DecisionStatus = NodeStatus.NOT_STARTED
    priority: int = Field(default=50, ge=1, le=100)
    ordinal: int = 0
    target_repo: str | None = None
    acceptable_models: list[str] = Field(default_factory=list)
    frontmatter: dict[str, Any] = Field(default_factory=dict)
    claimed_from: Status | None = None
    review: bool = True
    fix: bool = True
    merge: Merge = Merge.MAIN
    outcome: Outcome | None = None
    verdict: str | None = None
    fix_for: Outcome | None = None
    review_cycles: int = Field(default=0, ge=0)
    merge_attempts: int = Field(default=0, ge=0)
    step_failures: int = Field(default=0, ge=0)
    branch: str | None = None
    requires: list[str] = Field(default_factory=list)
    land_order: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)

    @model_validator(mode="before")
    @classmethod
    def _containers_default_to_no_review(cls, data: Any) -> Any:
        # A plan or spec reviews and fixes only when its planner asks for it; a task does unless
        # its planner opts out.
        if isinstance(data, dict) and data.get("kind") in _CONTAINERS:
            return {"review": False, "fix": False, **data}
        return data
```

In `src/taskmanager/core/models.py`, add after `Node`:

```python
class Condition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str
    idx: int = 0
    needs: str
    command: str
    stage: ConditionStage = ConditionStage.CLAIM
```

In `src/taskmanager/db/node_repo.py`, replace:

```python
from taskmanager.core.models import (
    Node,
    NodeRelation,
    NodeSection,
    NodeVerification,
)
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.utils import parse_db_datetime, to_db_timestamp
```

with:

```python
from taskmanager.core.models import (
    Condition,
    Node,
    NodeRelation,
    NodeSection,
    NodeVerification,
)
from taskmanager.core.status import ConditionStage, DecisionStatus, Status
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.utils import parse_db_datetime, to_db_timestamp

_NODE_COLUMNS = (
    "id, kind, title, status, priority, ordinal, target_repo, acceptable_models, "
    "frontmatter_json, claimed_from, review, fix, merge, outcome, verdict, fix_for, "
    "review_cycles, merge_attempts, step_failures, branch, requires, land_order, "
    "created_at, updated_at"
)


def _status(raw: str) -> NodeStatus | Status | DecisionStatus:
    # A value both vocabularies share reads as NodeStatus, so modules not yet on the new
    # vocabulary keep matching it; each name only the new vocabulary has reads as its own.
    for vocabulary in (NodeStatus, Status):
        try:
            return vocabulary(raw)
        except ValueError:
            continue
    return DecisionStatus(raw)
```

In `src/taskmanager/db/node_repo.py`, replace `NodeRepository.save_node` in full with:

```python
def save_node(self, node: Node) -> None:
    with self.db.get_spec_connection() as conn:
        conn.execute(
            f"""
            INSERT INTO nodes ({_NODE_COLUMNS})
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                kind=excluded.kind,
                title=excluded.title,
                status=excluded.status,
                priority=excluded.priority,
                ordinal=excluded.ordinal,
                target_repo=excluded.target_repo,
                acceptable_models=excluded.acceptable_models,
                frontmatter_json=excluded.frontmatter_json,
                claimed_from=excluded.claimed_from,
                review=excluded.review,
                fix=excluded.fix,
                merge=excluded.merge,
                outcome=excluded.outcome,
                verdict=excluded.verdict,
                fix_for=excluded.fix_for,
                review_cycles=excluded.review_cycles,
                merge_attempts=excluded.merge_attempts,
                step_failures=excluded.step_failures,
                branch=excluded.branch,
                requires=excluded.requires,
                land_order=excluded.land_order,
                updated_at=excluded.updated_at;
            """,
            (
                node.id,
                node.kind.value,
                node.title,
                node.status.value,
                node.priority,
                node.ordinal,
                node.target_repo,
                json.dumps(node.acceptable_models),
                json.dumps(node.frontmatter),
                node.claimed_from.value if node.claimed_from else None,
                int(node.review),
                int(node.fix),
                node.merge.value,
                node.outcome.value if node.outcome else None,
                node.verdict,
                node.fix_for.value if node.fix_for else None,
                node.review_cycles,
                node.merge_attempts,
                node.step_failures,
                node.branch,
                json.dumps(node.requires),
                json.dumps(node.land_order),
                to_db_timestamp(node.created_at),
                to_db_timestamp(node.updated_at),
            ),
        )
        cursor = conn.execute(
            """
            UPDATE nodes_fts
            SET title = ?, frontmatter_text = ?
            WHERE node_id = ?;
            """,
            (node.title, json.dumps(node.frontmatter), node.id),
        )
        if cursor.rowcount == 0:
            rowid_row = conn.execute("SELECT rowid FROM nodes WHERE id = ?", (node.id,)).fetchone()
            rowid = rowid_row[0] if rowid_row else None
            content_row = conn.execute(
                """
                SELECT COALESCE(GROUP_CONCAT(content, ' '), '')
                FROM (SELECT content FROM node_sections WHERE node_id = ? ORDER BY ordinal ASC)
                """,
                (node.id,),
            ).fetchone()
            content_text = content_row[0] if content_row else ""
            conn.execute(
                """
                INSERT INTO nodes_fts (rowid, node_id, title, frontmatter_text, content_text)
                VALUES (?, ?, ?, ?, ?);
                """,
                (rowid, node.id, node.title, json.dumps(node.frontmatter), content_text),
            )
        self.db.spec_commit(conn)
```

In `src/taskmanager/db/node_repo.py`, replace `NodeRepository.get_node` in full with:

```python
def get_node(self, node_id: str) -> Node | None:
    with self.db.get_spec_connection() as conn:
        row = conn.execute(f"SELECT {_NODE_COLUMNS} FROM nodes WHERE id = ?", (node_id,)).fetchone()
        if not row:
            return None
        return self._row_to_node(row)
```

In `src/taskmanager/db/node_repo.py`, replace `NodeRepository.list_nodes` in full with:

```python
def list_nodes(self, kind: NodeKind | None = None, status: NodeStatus | None = None) -> list[Node]:
    query = f"SELECT {_NODE_COLUMNS} FROM nodes WHERE 1=1"
    params: list[str] = []
    if kind is not None:
        query += " AND kind = ?"
        params.append(kind.value if hasattr(kind, "value") else str(kind))
    if status is not None:
        query += " AND status = ?"
        params.append(status.value if hasattr(status, "value") else str(status))
    query += " ORDER BY ordinal ASC, priority DESC, id ASC"
    with self.db.get_spec_connection() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
        return [self._row_to_node(r) for r in rows]
```

In `src/taskmanager/db/node_repo.py`, replace `NodeRepository._row_to_node` with `_row_to_node`, followed by the three new condition methods:

```python
@staticmethod
def _row_to_node(row: tuple[Any, ...]) -> Node:
    return Node.model_validate(
        {
            "id": row[0],
            "kind": NodeKind(row[1]),
            "title": row[2],
            "status": _status(row[3]),
            "priority": row[4],
            "ordinal": row[5],
            "target_repo": row[6],
            "acceptable_models": json.loads(row[7]),
            "frontmatter": json.loads(row[8]),
            "claimed_from": row[9],
            "review": bool(row[10]),
            "fix": bool(row[11]),
            "merge": row[12],
            "outcome": row[13],
            "verdict": row[14],
            "fix_for": row[15],
            "review_cycles": row[16],
            "merge_attempts": row[17],
            "step_failures": row[18],
            "branch": row[19],
            "requires": json.loads(row[20]),
            "land_order": json.loads(row[21]),
            "created_at": parse_db_datetime(row[22]),
            "updated_at": parse_db_datetime(row[23]),
        }
    )


def get_conditions(self, node_id: str) -> list[Condition]:
    with self.db.get_state_connection() as conn:
        rows = conn.execute(
            "SELECT node_id, idx, needs, command, stage FROM node_conditions "
            "WHERE node_id = ? ORDER BY idx ASC",
            (node_id,),
        ).fetchall()
    return [
        Condition(node_id=r[0], idx=r[1], needs=r[2], command=r[3], stage=ConditionStage(r[4]))
        for r in rows
    ]


def add_condition(self, condition: Condition) -> Condition:
    with self.db.get_state_connection() as conn:
        (idx,) = conn.execute(
            "SELECT COALESCE(MAX(idx), 0) + 1 FROM node_conditions WHERE node_id = ?",
            (condition.node_id,),
        ).fetchone()
        conn.execute(
            "INSERT INTO node_conditions (node_id, idx, needs, command, stage) "
            "VALUES (?, ?, ?, ?, ?)",
            (condition.node_id, idx, condition.needs, condition.command, condition.stage.value),
        )
        self.db.spec_commit(conn)
    return condition.model_copy(update={"idx": idx})


def remove_condition(self, node_id: str, idx: int) -> bool:
    with self.db.get_state_connection() as conn:
        cursor = conn.execute(
            "DELETE FROM node_conditions WHERE node_id = ? AND idx = ?", (node_id, idx)
        )
        self.db.spec_commit(conn)
        return cursor.rowcount > 0
```

In `src/taskmanager/db/runtime_repo.py`, replace `RuntimeRepository.acquire_lease` in full with:

```python
    def acquire_lease(self, lease: Lease, locks: list[FileLock]) -> None:
        with self.db.get_runtime_connection() as conn:
            conn.execute(
                """
                INSERT INTO leases (
                    task_id, agent_id, session_id, account_id,
                    worktree_path, branch_name, acquired_at, last_heartbeat, ttl_seconds
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    agent_id=excluded.agent_id,
                    session_id=excluded.session_id,
                    account_id=excluded.account_id,
                    worktree_path=excluded.worktree_path,
                    branch_name=excluded.branch_name,
                    last_heartbeat=excluded.last_heartbeat,
                    ttl_seconds=excluded.ttl_seconds;
                """,
                (
                    lease.task_id,
                    lease.agent_id,
                    lease.session_id,
                    lease.account_id,
                    lease.worktree_path,
                    lease.branch_name,
                    to_db_timestamp(lease.acquired_at),
                    to_db_timestamp(lease.last_heartbeat),
                    lease.ttl_seconds,
                ),
            )
            conn.execute("DELETE FROM file_locks WHERE task_id = ?", (lease.task_id,))
            for lock in locks:
                conn.execute(
                    """
                    INSERT INTO file_locks (file_path, task_id, lock_type)
                    VALUES (?, ?, ?)
                    ON CONFLICT(file_path) DO UPDATE SET
                        task_id=excluded.task_id,
                        lock_type=excluded.lock_type;
                    """,
                    (lock.file_path, lock.task_id, lock.lock_type),
                )
            self.db.spec_commit(conn)
```

In `src/taskmanager/db/runtime_repo.py`, replace `RuntimeRepository.heartbeat` in full with:

```python
    def heartbeat(self, task_id: str) -> bool:
        now_str = to_db_timestamp(None)
        with self.db.get_runtime_connection() as conn:
            cursor = conn.execute(
                "UPDATE leases SET last_heartbeat = ? WHERE task_id = ?",
                (now_str, task_id),
            )
            self.db.spec_commit(conn)
            return cursor.rowcount > 0
```

In `src/taskmanager/db/runtime_repo.py`, replace `RuntimeRepository.release_lease` in full with:

```python
    def release_lease(self, task_id: str) -> None:
        with self.db.get_runtime_connection() as conn:
            conn.execute("DELETE FROM leases WHERE task_id = ?", (task_id,))
            conn.execute("DELETE FROM file_locks WHERE task_id = ?", (task_id,))
            self.db.spec_commit(conn)
```

In `src/taskmanager/db/runtime_repo.py`, replace `RuntimeRepository.sweep_expired_leases` in full with:

```python
def sweep_expired_leases(self) -> list[str]:
    expired_tasks: list[str] = []
    now = datetime.now(tz=UTC)
    with self.db.get_runtime_connection() as conn:
        rows = conn.execute("SELECT task_id, ttl_seconds, last_heartbeat FROM leases").fetchall()
        for r in rows:
            task_id = r[0]
            ttl = int(r[1])
            last_hb = parse_db_datetime(r[2])
            if (now - last_hb).total_seconds() > ttl:
                expired_tasks.append(task_id)
        for t in expired_tasks:
            conn.execute("DELETE FROM leases WHERE task_id = ?", (t,))
            conn.execute("DELETE FROM file_locks WHERE task_id = ?", (t,))
        self.db.spec_commit(conn)
    return expired_tasks
```

In `src/taskmanager/engine/graph.py`, replace:

```python
from datetime import UTC, datetime
```

with:

```python
from collections.abc import Sequence
from datetime import UTC, datetime
```

In `src/taskmanager/engine/graph.py`, replace:

```python
from taskmanager.core.models import Lease, Node, NodeRelation
```

with:

```python
from taskmanager.core.models import Lease, Node, NodeRelation
from taskmanager.core.status import DecisionStatus, Status
```

In `src/taskmanager/engine/graph.py`, replace:

```python
LIFECYCLE_ORDER: dict[NodeStatus, int] = {
```

with:

```python
LIFECYCLE_ORDER: dict[str, int] = {
```

In `src/taskmanager/engine/graph.py`, replace:

```python
def gate_satisfied(status: NodeStatus, gate: NodeStatus) -> bool:
```

with:

```python
def gate_satisfied(status: str, gate: str) -> bool:
```

In `src/taskmanager/engine/graph.py`, replace:

```python
    def resolve_task_state(self, task_id: str) -> VirtualStatus | NodeStatus:
```

with:

```python
    def resolve_task_state(
        self, task_id: str
    ) -> VirtualStatus | NodeStatus | Status | DecisionStatus:
```

In `src/taskmanager/engine/graph.py`, replace:

```python
        counted_states: list[NodeStatus | VirtualStatus],
        set_aside_statuses: list[NodeStatus],
```

with:

```python
        counted_states: Sequence[str],
        set_aside_statuses: Sequence[str],
```

In `src/taskmanager/engine/graph.py`, replace:

```python
    def resolve_plan_status(self, plan_id: str) -> NodeStatus | VirtualStatus:
```

with:

```python
    def resolve_plan_status(
        self, plan_id: str
    ) -> NodeStatus | VirtualStatus | Status | DecisionStatus:
```

In `src/taskmanager/engine/graph.py`, replace:

```python
    def resolve_spec_status(self, spec_id: str) -> NodeStatus | VirtualStatus:
```

with:

```python
    def resolve_spec_status(
        self, spec_id: str
    ) -> NodeStatus | VirtualStatus | Status | DecisionStatus:
```

In `src/taskmanager/engine/operations.py`, replace:

```python
_SWEEP_BACK: dict[NodeStatus, NodeStatus] = {
```

with:

```python
_SWEEP_BACK: dict[str, NodeStatus] = {
```

In `src/taskmanager/engine/decisions.py`, replace:

```python
DECISION_STATUS_LABELS: dict[NodeStatus, str] = {
```

with:

```python
DECISION_STATUS_LABELS: dict[str, str] = {
```

- [ ] **Step 4: Run the tests and the gates**

```bash
uv run --directory <worktree> pytest tests/unit/test_storage.py -q; echo $?
uv run --directory <worktree> pytest -q; echo $?
uv run --directory <worktree> ruff check; echo $?
uv run --directory <worktree> ruff format --check; echo $?
uv run --directory <worktree> mypy; echo $?
```

Expected: every command prints `0` last.

- [ ] **Step 5: Commit**

```bash
git -C <worktree> add src/taskmanager/db/schema.py src/taskmanager/db/connection.py src/taskmanager/db/__init__.py src/taskmanager/core/models.py src/taskmanager/db/node_repo.py src/taskmanager/db/runtime_repo.py src/taskmanager/engine/graph.py src/taskmanager/engine/operations.py src/taskmanager/engine/decisions.py tests/unit/test_storage.py tests/unit/test_db_schema.py tests/unit/test_repos.py tests/unit/test_search_engine.py && git -C <worktree> commit -m "feat(storage): open a fresh state.db, tombstone the old paths and refuse a pre-lifecycle estate"
```

### Task 9: Coordination tables: leases in state.db, atomic claim, jobs, branch locks, cache

**Spec:** §5.1, §6.2 (the `jobs` and `branch_locks` rows), §9.1 (`cache.db`), §11 Claims
**Files:**
- Modify: `src/taskmanager/db/schema.py` (`leases` gains `action`, `review_hash`, `model` and a nullable `ttl_seconds`; `jobs` (every `JobState`, `expired` included), `branch_locks`; new `CACHE_SCHEMA_SQL`)
- Modify: `src/taskmanager/db/connection.py` (`cache_db`, `get_cache_connection`, `init_all` creates `cache.db`)
- Modify: `src/taskmanager/db/__init__.py` (exports)
- Modify: `src/taskmanager/core/models.py` (`Lease.action`, `.review_hash`, `.model`, nullable `.ttl_seconds`; new `Job`, `BranchLock`, `GateRun`)
- Modify: `src/taskmanager/db/runtime_repo.py` (rewritten: `lease_alive`, `claim`, `list_leases`, `list_locks`, `park`, `take_over`; null TTL handling in every liveness check, and the sweep skips a null-TTL lease)
- Modify: `src/taskmanager/engine/graph.py` (`GraphEngine._is_lease_active` reads a null TTL as live)
- Modify: `src/taskmanager/di/container.py` (`job_repo`, `cache_repo` providers)
- Create: `src/taskmanager/db/job_repo.py`
- Create: `src/taskmanager/db/cache_repo.py`
- Test: `tests/unit/test_coordination.py`

**Interfaces:**
- Consumes: `Action`, `JobKind`, `JobState` (with `EXPIRED`), `Status` (Task 1); `state.db`, `in_transaction`, `SCHEMA_VERSION` (Task 8)
- Produces: `RuntimeRepository.claim(lease, locks, node) -> bool`, `.list_leases() -> list[Lease]`, `.list_locks() -> list[FileLock]`, `.park(node_id) -> None`, `.take_over(node_id, agent, session, ttl, model) -> bool`; `lease_alive(ttl, last_heartbeat, now)`; `Lease.action/.review_hash/.model: str | None/.ttl_seconds: int | None`; `Job`, `BranchLock`, `GateRun`; `JobRepository`; `CacheRepository`; `DatabaseManager.get_cache_connection`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_coordination.py`:

```python
import sqlite3
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from taskmanager.core.enums import LockType, NodeKind
from taskmanager.core.models import FileLock, GateRun, Job, Lease, Node
from taskmanager.core.status import Action, JobKind, JobState, Status
from taskmanager.db.cache_repo import CacheRepository
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.job_repo import JobRepository
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.db.schema import SCHEMA_VERSION


@pytest.fixture
def db(tmp_path: Path) -> DatabaseManager:
    mgr = DatabaseManager(tmp_path / ".taskmanager")
    mgr.init_all()
    return mgr


def _task(db: DatabaseManager, node_id: str, status: Status = Status.READY) -> None:
    NodeRepository(db).save_node(Node(id=node_id, kind=NodeKind.TASK, title=node_id, status=status))


def _lease(node_id: str, agent: str = "agent-a", **extra: Any) -> Lease:
    fields: dict[str, Any] = {"action": Action.IMPLEMENT, **extra}
    return Lease(
        task_id=node_id, agent_id=agent, session_id="s", branch_name=f"tm/{node_id}", **fields
    )


def _claimed(node_id: str) -> Node:
    return Node(
        id=node_id,
        kind=NodeKind.TASK,
        title=node_id,
        status=Status.IMPLEMENTING,
        claimed_from=Status.READY,
    )


def _dump(db: DatabaseManager) -> tuple[list[str], list[str]]:
    with db.get_state_connection() as state, db.get_cache_connection() as cache:
        return list(state.iterdump()), list(cache.iterdump())


def test_init_creates_the_cache_and_the_coordination_tables(db: DatabaseManager) -> None:
    with db.get_cache_connection() as conn:
        assert conn.execute("PRAGMA user_version").fetchone() == (SCHEMA_VERSION,)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"gate_baselines", "condition_results"} <= tables
    with db.get_state_connection() as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"leases", "file_locks", "jobs", "branch_locks"} <= tables


def test_a_lease_keeps_its_action_review_hash_model_and_null_ttl(db: DatabaseManager) -> None:
    repo = RuntimeRepository(db)
    repo.acquire_lease(
        _lease("T1", action=Action.MERGE, review_hash="abc", model="sonnet", ttl_seconds=None),
        [FileLock(file_path="a.py", task_id="T1")],
    )
    loaded = repo.get_lease("T1")
    assert loaded is not None
    assert (loaded.action, loaded.review_hash, loaded.model, loaded.ttl_seconds) == (
        Action.MERGE,
        "abc",
        "sonnet",
        None,
    )


def test_every_lease_and_every_lock_is_listed(db: DatabaseManager) -> None:
    repo = RuntimeRepository(db)
    repo.acquire_lease(
        _lease("T2", agent="agent-b", model="opus"),
        [
            FileLock(file_path="b.py", task_id="T2"),
            FileLock(file_path="a.py", task_id="T2", lock_type=LockType.READ),
        ],
    )
    repo.acquire_lease(_lease("T1"), [])
    assert [(lease.task_id, lease.agent_id, lease.model) for lease in repo.list_leases()] == [
        ("T1", "agent-a", None),
        ("T2", "agent-b", "opus"),
    ]
    assert repo.list_locks() == [
        FileLock(file_path="a.py", task_id="T2", lock_type=LockType.READ),
        FileLock(file_path="b.py", task_id="T2"),
    ]


def test_a_null_ttl_lease_never_expires(db: DatabaseManager) -> None:
    repo = RuntimeRepository(db)
    long_ago = datetime.now(tz=UTC) - timedelta(days=30)
    repo.acquire_lease(
        _lease("T1", ttl_seconds=None, last_heartbeat=long_ago),
        [FileLock(file_path="a.py", task_id="T1")],
    )
    assert repo.sweep_expired_leases() == []
    assert repo.is_file_locked("a.py")
    assert repo.get_conflicting_tasks(["a.py"]) == {"a.py": "Task: T1, Agent: agent-a"}


def test_a_parked_lease_outlives_its_ttl_until_one_agent_takes_it_over(
    db: DatabaseManager,
) -> None:
    repo = RuntimeRepository(db)
    long_ago = datetime.now(tz=UTC) - timedelta(hours=1)
    repo.acquire_lease(
        _lease("T1", action=Action.MERGE, ttl_seconds=60, last_heartbeat=long_ago),
        [FileLock(file_path="a.py", task_id="T1")],
    )
    repo.park("T1")
    assert repo.sweep_expired_leases() == []
    assert repo.take_over("T1", "agent-b", "s2", 900, "sonnet") is True
    lease = repo.get_lease("T1")
    assert lease is not None
    assert (lease.agent_id, lease.session_id, lease.ttl_seconds, lease.model, lease.action) == (
        "agent-b",
        "s2",
        900,
        "sonnet",
        Action.MERGE,
    )
    assert repo.get_conflicting_tasks(["a.py"]) == {"a.py": "Task: T1, Agent: agent-b"}
    assert repo.take_over("T1", "agent-c", "s3", 900, "opus") is False
    assert repo.take_over("T9", "agent-c", "s3", 900, "opus") is False


def test_an_expired_ttl_lease_is_swept_and_frees_its_files(db: DatabaseManager) -> None:
    repo = RuntimeRepository(db)
    long_ago = datetime.now(tz=UTC) - timedelta(hours=1)
    repo.acquire_lease(
        _lease("T1", ttl_seconds=60, last_heartbeat=long_ago),
        [FileLock(file_path="a.py", task_id="T1")],
    )
    assert not repo.is_file_locked("a.py")
    assert repo.sweep_expired_leases() == ["T1"]
    assert repo.get_lease("T1") is None


def test_a_claim_writes_the_lease_the_locks_and_the_status_together(db: DatabaseManager) -> None:
    _task(db, "T1")
    runtime = RuntimeRepository(db)
    assert runtime.claim(_lease("T1"), [FileLock(file_path="a.py", task_id="T1")], _claimed("T1"))
    node = NodeRepository(db).get_node("T1")
    assert node is not None
    assert (node.status, node.claimed_from) == (Status.IMPLEMENTING, Status.READY)
    lease = runtime.get_lease("T1")
    assert lease is not None and lease.action == Action.IMPLEMENT
    assert runtime.get_conflicting_tasks(["a.py"]) == {"a.py": "Task: T1, Agent: agent-a"}


def _status_moved(db: DatabaseManager) -> None:
    _task(db, "T1", status=Status.IMPLEMENTED)


def _lease_exists(db: DatabaseManager) -> None:
    _task(db, "T1")
    RuntimeRepository(db).acquire_lease(_lease("T1", agent="earlier"), [])


def _file_held(db: DatabaseManager) -> None:
    _task(db, "T1")
    _task(db, "T2")
    RuntimeRepository(db).acquire_lease(_lease("T2"), [FileLock(file_path="a.py", task_id="T2")])


def _node_missing(db: DatabaseManager) -> None:
    pass


@pytest.mark.parametrize(
    "arrange",
    [_status_moved, _lease_exists, _file_held, _node_missing],
    ids=["status-moved", "lease-exists", "file-held", "node-missing"],
)
def test_a_refused_claim_leaves_both_databases_unchanged(
    db: DatabaseManager, arrange: Callable[[DatabaseManager], None]
) -> None:
    arrange(db)
    CacheRepository(db).put_condition("T1", 1, "true", 0)
    before = _dump(db)
    claimed = RuntimeRepository(db).claim(
        _lease("T1"), [FileLock(file_path="a.py", task_id="T1")], _claimed("T1")
    )
    assert claimed is False
    assert _dump(db) == before


def test_a_claim_refuses_to_run_inside_an_open_transaction(db: DatabaseManager) -> None:
    _task(db, "T1")
    with pytest.raises(RuntimeError, match="its own transaction"), NodeRepository(db).transaction():
        RuntimeRepository(db).claim(_lease("T1"), [], _claimed("T1"))


def test_a_claim_names_the_status_it_is_claimed_from(db: DatabaseManager) -> None:
    _task(db, "T1")
    unnamed = Node(id="T1", kind=NodeKind.TASK, title="T1", status=Status.IMPLEMENTING)
    with pytest.raises(ValueError, match="names the status"):
        RuntimeRepository(db).claim(_lease("T1"), [], unnamed)


@pytest.mark.parametrize("attempt", range(20))
def test_two_racing_claims_on_one_node_leave_exactly_one_lease(
    db: DatabaseManager, attempt: int
) -> None:
    node_id = f"T{attempt}"
    _task(db, node_id)
    barrier = threading.Barrier(2)
    results: dict[str, bool] = {}

    def claim(agent: str) -> None:
        lease = _lease(node_id, agent=agent)
        locks = [FileLock(file_path="shared.py", task_id=node_id)]
        barrier.wait()
        results[agent] = RuntimeRepository(db).claim(lease, locks, _claimed(node_id))

    threads = [threading.Thread(target=claim, args=(a,)) for a in ("agent-a", "agent-b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results.values()) == [False, True]
    winner = next(agent for agent, won in results.items() if won)
    with db.get_state_connection() as conn:
        assert conn.execute("SELECT task_id, agent_id FROM leases").fetchall() == [
            (node_id, winner)
        ]
        assert conn.execute("SELECT file_path, task_id FROM file_locks").fetchall() == [
            ("shared.py", node_id)
        ]
    node = NodeRepository(db).get_node(node_id)
    assert node is not None and node.status == Status.IMPLEMENTING


def test_a_job_gets_an_id_and_reads_back_whole(db: DatabaseManager) -> None:
    _task(db, "T1")
    jobs = JobRepository(db)
    job = jobs.create(
        Job(kind=JobKind.LAND, node_id="T1", repo="core", target="main", step="build")
    )
    assert job.id.startswith("land-")
    assert jobs.get(job.id) == job
    assert jobs.get("land-missing") is None


def test_a_job_update_moves_its_state_and_result(db: DatabaseManager) -> None:
    _task(db, "T1")
    jobs = JobRepository(db)
    job = jobs.create(Job(kind=JobKind.SYNC, node_id="T1", repo="core", target="tm/P"))
    stopped = job.model_copy(
        update={"state": JobState.NEEDS_AGENT, "step": "build", "result": {"reason": "conflict"}}
    )
    jobs.update(stopped)
    assert jobs.get(job.id) == stopped
    with pytest.raises(KeyError):
        jobs.update(job.model_copy(update={"id": "sync-missing"}))


def test_jobs_are_listed_per_node_and_by_waiting_for_an_agent(db: DatabaseManager) -> None:
    _task(db, "T1")
    _task(db, "T2")
    jobs = JobRepository(db)
    running = jobs.create(Job(kind=JobKind.LAND, node_id="T1", repo="core", target="main"))
    waiting = jobs.create(
        Job(
            kind=JobKind.LAND,
            node_id="T1",
            repo="web",
            target="main",
            state=JobState.NEEDS_AGENT,
        )
    )
    other = jobs.create(
        Job(kind=JobKind.SYNC, node_id="T2", repo="core", target="tm/P", state=JobState.NEEDS_AGENT)
    )
    assert jobs.for_node("T1") == [running, waiting]
    assert jobs.waiting_for_agent() == [waiting, other]


@pytest.mark.parametrize("state", list(JobState))
def test_every_job_state_is_stored(db: DatabaseManager, state: JobState) -> None:
    _task(db, "T1")
    jobs = JobRepository(db)
    job = jobs.create(Job(kind=JobKind.LAND, node_id="T1", repo="core", target="main", state=state))
    assert jobs.get(job.id) == job


@pytest.mark.parametrize(
    ("column", "value"), [("kind", "rebase"), ("state", "paused")], ids=["kind", "state"]
)
def test_the_schema_refuses_an_unknown_job_kind_or_state(
    db: DatabaseManager, column: str, value: str
) -> None:
    _task(db, "T1")
    job = JobRepository(db).create(Job(kind=JobKind.LAND, node_id="T1", repo="c", target="main"))
    with db.get_state_connection() as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(f"UPDATE jobs SET {column} = ? WHERE id = ?", (value, job.id))


def test_a_branch_lock_has_one_holder_at_a_time(db: DatabaseManager) -> None:
    _task(db, "T1")
    jobs = JobRepository(db)
    first = jobs.create(Job(kind=JobKind.LAND, node_id="T1", repo="core", target="tm/P"))
    second = jobs.create(Job(kind=JobKind.LAND, node_id="T1", repo="core", target="tm/P"))
    assert jobs.acquire_branch("core", "tm/P", first.id) is True
    assert jobs.acquire_branch("core", "tm/P", first.id) is True
    assert jobs.acquire_branch("core", "tm/P", second.id) is False
    assert jobs.acquire_branch("web", "tm/P", second.id) is True
    jobs.release_branch("core", "tm/P", second.id)
    assert jobs.acquire_branch("core", "tm/P", second.id) is False
    jobs.release_branch("core", "tm/P", first.id)
    assert jobs.acquire_branch("core", "tm/P", second.id) is True


def test_a_branch_lock_held_by_a_job_no_longer_running_is_taken_over(
    db: DatabaseManager,
) -> None:
    _task(db, "T1")
    jobs = JobRepository(db)
    dead = jobs.create(Job(kind=JobKind.LAND, node_id="T1", repo="core", target="tm/P"))
    live = jobs.create(Job(kind=JobKind.LAND, node_id="T1", repo="core", target="tm/P"))
    assert jobs.acquire_branch("core", "tm/P", dead.id)
    jobs.update(dead.model_copy(update={"state": JobState.NEEDS_AGENT}))
    assert jobs.acquire_branch("core", "tm/P", live.id) is True


@pytest.mark.parametrize(
    "run",
    [
        GateRun(exit_code=0, failing=None, tail="ok"),
        GateRun(exit_code=1, failing=frozenset({"t::a", "t::b"}), tail="2 failed"),
        GateRun(exit_code=1, failing=frozenset(), tail="collection error"),
    ],
    ids=["green", "red-with-report", "red-empty-report"],
)
def test_a_gate_baseline_reads_back_for_its_repo_sha_and_template(
    db: DatabaseManager, run: GateRun
) -> None:
    cache = CacheRepository(db)
    assert cache.get_baseline("core", "abc", "h1") is None
    cache.put_baseline("core", "abc", "h1", run)
    assert cache.get_baseline("core", "abc", "h1") == run
    assert cache.get_baseline("core", "abc", "h2") is None
    assert cache.get_baseline("core", "abd", "h1") is None
    assert cache.get_baseline("web", "abc", "h1") is None


def test_a_condition_result_is_served_while_fresh_and_for_the_same_command(
    db: DatabaseManager,
) -> None:
    cache = CacheRepository(db)
    assert cache.get_condition("T1", 1, "test -f x", max_age=300) is None
    cache.put_condition("T1", 1, "test -f x", 1)
    assert cache.get_condition("T1", 1, "test -f x", max_age=300) == 1
    assert cache.get_condition("T1", 1, "test -f y", max_age=300) is None
    assert cache.get_condition("T1", 2, "test -f x", max_age=300) is None
    cache.put_condition("T1", 1, "test -f x", 0)
    assert cache.get_condition("T1", 1, "test -f x", max_age=300) == 0


def test_a_condition_result_older_than_its_max_age_is_not_served(db: DatabaseManager) -> None:
    cache = CacheRepository(db)
    cache.put_condition("T1", 1, "true", 0)
    stale = (datetime.now(tz=UTC) - timedelta(seconds=301)).isoformat()
    with db.get_cache_connection() as conn:
        conn.execute("UPDATE condition_results SET checked_at = ?", (stale,))
        conn.commit()
    assert cache.get_condition("T1", 1, "true", max_age=300) is None
    assert cache.get_condition("T1", 1, "true", max_age=400) == 0
```

- [ ] **Step 2: Run it and watch it fail**

```bash
uv run --directory <worktree> pytest tests/unit/test_coordination.py -q; echo $?
```

Expected: `ModuleNotFoundError: No module named 'taskmanager.db.cache_repo'` during collection, exit 2.

- [ ] **Step 3: Implement**

`claim` issues `BEGIN IMMEDIATE` itself, so it refuses to run inside `NodeRepository.transaction()`: joining an open deferred transaction would drop the write lock that makes two racing claims serialise. The race test runs twenty times because one run proves little about a race. A refused claim is compared with `iterdump()` of both databases, which is the logical content the review focus asks to be unchanged (WAL pages make raw bytes an unreliable witness).

In `src/taskmanager/db/schema.py`, replace:

```python
    ttl_seconds INTEGER NOT NULL DEFAULT 300
```

with:

```python
    ttl_seconds INTEGER DEFAULT 300 CHECK (ttl_seconds IS NULL OR ttl_seconds > 0),
    action TEXT CHECK (action IN ('implement', 'review', 'fix', 'merge', 'sync')),
    review_hash TEXT,
    model TEXT
```

In `src/taskmanager/db/schema.py`, replace (the end of `STATE_SCHEMA_SQL`, which gains `jobs` and `branch_locks`, followed by the new `CACHE_SCHEMA_SQL`):

```python
    lock_type TEXT NOT NULL DEFAULT 'write'
);
"""
```

with:

```python
    lock_type TEXT NOT NULL DEFAULT 'write'
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('land', 'sync')),
    node_id TEXT NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    repo TEXT NOT NULL,
    target TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN (
            'running', 'needs_agent', 'succeeded', 'own_defect', 'condition_unmet', 'expired'
        )
    ),
    step TEXT,
    worktree TEXT,
    pid INTEGER,
    heartbeat TIMESTAMP NOT NULL,
    result_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_jobs_node ON jobs(node_id);

CREATE TABLE IF NOT EXISTS branch_locks (
    repo TEXT NOT NULL,
    branch TEXT NOT NULL,
    holder TEXT NOT NULL,
    heartbeat TIMESTAMP NOT NULL,
    PRIMARY KEY (repo, branch)
);
"""

# Derived results only: dropping this database loses time, never state.
CACHE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS gate_baselines (
    repo TEXT NOT NULL,
    target_sha TEXT NOT NULL,
    template_hash TEXT NOT NULL,
    exit_code INTEGER NOT NULL,
    failing_json TEXT,
    tail TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL,
    PRIMARY KEY (repo, target_sha, template_hash)
);

CREATE TABLE IF NOT EXISTS condition_results (
    node_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    command_hash TEXT NOT NULL,
    exit_code INTEGER NOT NULL,
    checked_at TIMESTAMP NOT NULL,
    PRIMARY KEY (node_id, idx)
);
"""
```

In `src/taskmanager/db/connection.py`, replace:

```python
from taskmanager.db.schema import (
    LEDGER_SCHEMA_SQL,
```

with:

```python
from taskmanager.db.schema import (
    CACHE_SCHEMA_SQL,
    LEDGER_SCHEMA_SQL,
```

In `src/taskmanager/db/connection.py`, replace:

```python
        self.ledger_db = taskmanager_dir / "ledger.db"
```

with:

```python
        self.ledger_db = taskmanager_dir / "ledger.db"
        self.cache_db = taskmanager_dir / "cache.db"
```

In `src/taskmanager/db/connection.py`, add after `DatabaseManager.get_ledger_connection`:

```python
    @contextmanager
    def get_cache_connection(self) -> Generator[sqlite3.Connection]:
        yield self._thread_conn("cache", self.cache_db)
```

In `src/taskmanager/db/connection.py`, replace `DatabaseManager.init_all` in full with:

```python
    def init_all(self, vector_dimensions: int = 384) -> None:
        with self.get_state_connection() as conn:
            conn.executescript(STATE_SCHEMA_SQL)
            conn.execute(vec_nodes_sql(vector_dimensions))
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.commit()

        with self.get_ledger_connection() as conn:
            conn.executescript(LEDGER_SCHEMA_SQL)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.commit()

        with self.get_cache_connection() as conn:
            conn.executescript(CACHE_SCHEMA_SQL)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.commit()

        tombstone = (
            f"taskmanager {taskmanager.__version__} owns this directory; its estate is in "
            "state.db. This file is not a database, so an older tm fails here instead of "
            "opening an empty estate.\n"
        )
        for name in _TOMBSTONES:
            (self.taskmanager_dir / name).write_text(tombstone, encoding="utf-8")
```

Replace the whole of `src/taskmanager/db/__init__.py` with:

```python
from taskmanager.db.cache_repo import CacheRepository as CacheRepository
from taskmanager.db.connection import DatabaseManager as DatabaseManager
from taskmanager.db.connection import PreLifecycleEstate as PreLifecycleEstate
from taskmanager.db.job_repo import JobRepository as JobRepository
from taskmanager.db.ledger_repo import LedgerRepository as LedgerRepository
from taskmanager.db.node_repo import NodeRepository as NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository as RuntimeRepository
from taskmanager.db.schema import CACHE_SCHEMA_SQL as CACHE_SCHEMA_SQL
from taskmanager.db.schema import LEDGER_SCHEMA_SQL as LEDGER_SCHEMA_SQL
from taskmanager.db.schema import STATE_SCHEMA_SQL as STATE_SCHEMA_SQL

__all__ = [
    "CACHE_SCHEMA_SQL",
    "LEDGER_SCHEMA_SQL",
    "STATE_SCHEMA_SQL",
    "CacheRepository",
    "DatabaseManager",
    "JobRepository",
    "LedgerRepository",
    "NodeRepository",
    "PreLifecycleEstate",
    "RuntimeRepository",
]
```

In `src/taskmanager/core/models.py`, replace:

```python
from datetime import datetime
```

with:

```python
from dataclasses import dataclass
from datetime import UTC, datetime
```

In `src/taskmanager/core/models.py`, replace:

```python
from taskmanager.core.status import ConditionStage, DecisionStatus, Merge, Outcome, Status
```

with:

```python
from taskmanager.core.status import (
    Action,
    ConditionStage,
    DecisionStatus,
    JobKind,
    JobState,
    Merge,
    Outcome,
    Status,
)
```

In `src/taskmanager/core/models.py`, replace `Lease` in full with:

```python
class Lease(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    agent_id: str
    session_id: str
    account_id: str | None = None
    worktree_path: str | None = None
    branch_name: str
    acquired_at: datetime = Field(default_factory=datetime.now)
    last_heartbeat: datetime = Field(default_factory=datetime.now)
    ttl_seconds: int | None = 300
    action: Action | None = None
    review_hash: str | None = None
    model: str | None = None
```

In `src/taskmanager/core/models.py`, add after `Lease`:

```python
class Job(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = ""
    kind: JobKind
    node_id: str
    repo: str
    target: str
    state: JobState = JobState.RUNNING
    step: str | None = None
    worktree: str | None = None
    pid: int | None = None
    heartbeat: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    result: dict[str, Any] = Field(default_factory=dict)


class BranchLock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo: str
    branch: str
    holder: str
    heartbeat: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))


@dataclass(frozen=True)
class GateRun:
    exit_code: int
    failing: frozenset[str] | None
    tail: str
```

Replace the whole of `src/taskmanager/db/runtime_repo.py` with:

```python
import sqlite3
from datetime import UTC, datetime

from typing import Any

from taskmanager.core.enums import LockType
from taskmanager.core.models import FileLock, Lease, Node
from taskmanager.core.status import Action
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.utils import parse_db_datetime, to_db_timestamp

_LEASE_COLUMNS = (
    "task_id, agent_id, session_id, account_id, worktree_path, branch_name, acquired_at, "
    "last_heartbeat, ttl_seconds, action, review_hash, model"
)
_INSERT_LEASE = f"INSERT INTO leases ({_LEASE_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"


def lease_alive(ttl: int | None, last_heartbeat: datetime, now: datetime) -> bool:
    # A null TTL is a landing job stopped for an agent: its lease holds until the agent resumes.
    return ttl is None or (now - last_heartbeat).total_seconds() <= ttl


def _lease_row(lease: Lease) -> tuple[object, ...]:
    return (
        lease.task_id,
        lease.agent_id,
        lease.session_id,
        lease.account_id,
        lease.worktree_path,
        lease.branch_name,
        to_db_timestamp(lease.acquired_at),
        to_db_timestamp(lease.last_heartbeat),
        lease.ttl_seconds,
        lease.action.value if lease.action else None,
        lease.review_hash,
        lease.model,
    )


def _row_to_lease(row: tuple[Any, ...]) -> Lease:
    return Lease(
        task_id=row[0],
        agent_id=row[1],
        session_id=row[2],
        account_id=row[3],
        worktree_path=row[4],
        branch_name=row[5],
        acquired_at=parse_db_datetime(row[6]),
        last_heartbeat=parse_db_datetime(row[7]),
        ttl_seconds=row[8],
        action=Action(row[9]) if row[9] else None,
        review_hash=row[10],
        model=row[11],
    )


class RuntimeRepository:
    def __init__(self, db_mgr: DatabaseManager) -> None:
        self.db = db_mgr

    def acquire_lease(self, lease: Lease, locks: list[FileLock]) -> None:
        with self.db.get_state_connection() as conn:
            conn.execute(
                f"""
                {_INSERT_LEASE}
                ON CONFLICT(task_id) DO UPDATE SET
                    agent_id=excluded.agent_id,
                    session_id=excluded.session_id,
                    account_id=excluded.account_id,
                    worktree_path=excluded.worktree_path,
                    branch_name=excluded.branch_name,
                    last_heartbeat=excluded.last_heartbeat,
                    ttl_seconds=excluded.ttl_seconds,
                    action=excluded.action,
                    review_hash=excluded.review_hash,
                    model=excluded.model;
                """,
                _lease_row(lease),
            )
            conn.execute("DELETE FROM file_locks WHERE task_id = ?", (lease.task_id,))
            for lock in locks:
                conn.execute(
                    """
                    INSERT INTO file_locks (file_path, task_id, lock_type)
                    VALUES (?, ?, ?)
                    ON CONFLICT(file_path) DO UPDATE SET
                        task_id=excluded.task_id,
                        lock_type=excluded.lock_type;
                    """,
                    (lock.file_path, lock.task_id, lock.lock_type),
                )
            self.db.spec_commit(conn)

    def claim(self, lease: Lease, locks: list[FileLock], node: Node) -> bool:
        """Take `node` from the status named by its `claimed_from` to the status it carries,
        with its lease and file locks, in one `BEGIN IMMEDIATE` transaction.

        False, with nothing written, when the stored status moved, a lease row exists for the
        node, or a lock row exists for one of the files. An expired lease still holds its rows
        until a sweep removes them, so callers sweep first.
        """
        if node.claimed_from is None:
            raise ValueError(f"a claim of '{node.id}' names the status it is claimed from")
        with self.db.get_state_connection() as conn:
            if self.db.in_transaction or conn.in_transaction:
                raise RuntimeError("a claim is its own transaction and cannot join an open one")
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = conn.execute("SELECT status FROM nodes WHERE id = ?", (node.id,)).fetchone()
                if row is None or row[0] != node.claimed_from.value:
                    conn.rollback()
                    return False
                conn.execute(_INSERT_LEASE, _lease_row(lease))
                conn.executemany(
                    "INSERT INTO file_locks (file_path, task_id, lock_type) VALUES (?, ?, ?)",
                    [(lock.file_path, lock.task_id, lock.lock_type.value) for lock in locks],
                )
                conn.execute(
                    """
                    UPDATE nodes SET status = ?, claimed_from = ?, outcome = ?, fix_for = ?,
                        review_cycles = ?, merge_attempts = ?, step_failures = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        node.status.value,
                        node.claimed_from.value,
                        node.outcome.value if node.outcome else None,
                        node.fix_for.value if node.fix_for else None,
                        node.review_cycles,
                        node.merge_attempts,
                        node.step_failures,
                        to_db_timestamp(None),
                        node.id,
                    ),
                )
            except sqlite3.IntegrityError:
                conn.rollback()
                return False
            except BaseException:
                conn.rollback()
                raise
            conn.commit()
            return True

    def get_lease(self, task_id: str) -> Lease | None:
        with self.db.get_state_connection() as conn:
            row = conn.execute(
                f"SELECT {_LEASE_COLUMNS} FROM leases WHERE task_id = ?", (task_id,)
            ).fetchone()
        return _row_to_lease(row) if row else None

    def list_leases(self) -> list[Lease]:
        """Every lease row, expired ones included: a caller that counts live leases sweeps or
        checks `lease_alive` first."""
        with self.db.get_state_connection() as conn:
            rows = conn.execute(f"SELECT {_LEASE_COLUMNS} FROM leases ORDER BY task_id").fetchall()
        return [_row_to_lease(r) for r in rows]

    def list_locks(self) -> list[FileLock]:
        with self.db.get_state_connection() as conn:
            rows = conn.execute(
                "SELECT file_path, task_id, lock_type FROM file_locks ORDER BY file_path"
            ).fetchall()
        return [FileLock(file_path=r[0], task_id=r[1], lock_type=LockType(r[2])) for r in rows]

    def park(self, node_id: str) -> None:
        """A job stopped for an agent keeps its lease and locks with no TTL, so no sweep frees
        the worktree it still owns."""
        with self.db.get_state_connection() as conn:
            conn.execute("UPDATE leases SET ttl_seconds = NULL WHERE task_id = ?", (node_id,))
            self.db.spec_commit(conn)

    def take_over(self, node_id: str, agent: str, session: str, ttl: int, model: str) -> bool:
        """Hand a parked lease to `agent` in one conditional write. A release followed by a new
        claim would let two agents each believe they hold the stopped job; here only the first
        writer finds the TTL still NULL."""
        with self.db.get_state_connection() as conn:
            cursor = conn.execute(
                """
                UPDATE leases SET agent_id = ?, session_id = ?, ttl_seconds = ?, model = ?,
                    last_heartbeat = ?
                WHERE task_id = ? AND ttl_seconds IS NULL
                """,
                (agent, session, ttl, model, to_db_timestamp(None), node_id),
            )
            self.db.spec_commit(conn)
            return cursor.rowcount > 0

    def heartbeat(self, task_id: str) -> bool:
        now_str = to_db_timestamp(None)
        with self.db.get_state_connection() as conn:
            cursor = conn.execute(
                "UPDATE leases SET last_heartbeat = ? WHERE task_id = ?",
                (now_str, task_id),
            )
            self.db.spec_commit(conn)
            return cursor.rowcount > 0

    def release_lease(self, task_id: str) -> None:
        with self.db.get_state_connection() as conn:
            conn.execute("DELETE FROM leases WHERE task_id = ?", (task_id,))
            conn.execute("DELETE FROM file_locks WHERE task_id = ?", (task_id,))
            self.db.spec_commit(conn)

    def is_file_locked(self, file_path: str) -> bool:
        return bool(self.get_conflicting_tasks([file_path]))

    def get_conflicting_tasks(self, file_paths: list[str]) -> dict[str, str]:
        if not file_paths:
            return {}
        now = datetime.now(tz=UTC)
        placeholders = ",".join("?" for _ in file_paths)
        with self.db.get_state_connection() as conn:
            rows = conn.execute(
                f"""
                SELECT f.file_path, f.task_id, l.agent_id, l.ttl_seconds, l.last_heartbeat
                FROM file_locks f
                JOIN leases l ON l.task_id = f.task_id
                WHERE f.file_path IN ({placeholders})
                """,
                tuple(file_paths),
            ).fetchall()
        return {
            path: f"Task: {task_id}, Agent: {agent_id}"
            for path, task_id, agent_id, ttl, last_hb in rows
            if lease_alive(ttl, parse_db_datetime(last_hb), now)
        }

    def sweep_expired_leases(self) -> list[str]:
        now = datetime.now(tz=UTC)
        with self.db.get_state_connection() as conn:
            rows = conn.execute(
                "SELECT task_id, ttl_seconds, last_heartbeat FROM leases "
                "WHERE ttl_seconds IS NOT NULL"
            ).fetchall()
            expired = [
                task_id
                for task_id, ttl, last_hb in rows
                if not lease_alive(ttl, parse_db_datetime(last_hb), now)
            ]
            for task_id in expired:
                conn.execute("DELETE FROM leases WHERE task_id = ?", (task_id,))
                conn.execute("DELETE FROM file_locks WHERE task_id = ?", (task_id,))
            self.db.spec_commit(conn)
        return expired
```

Create `src/taskmanager/db/job_repo.py`:

```python
import json
import uuid
from typing import Any

from taskmanager.core.models import Job
from taskmanager.core.status import JobKind, JobState
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.utils import parse_db_datetime, to_db_timestamp

_COLUMNS = "id, kind, node_id, repo, target, state, step, worktree, pid, heartbeat, result_json"


def _row_to_job(row: tuple[Any, ...]) -> Job:
    return Job(
        id=row[0],
        kind=JobKind(row[1]),
        node_id=row[2],
        repo=row[3],
        target=row[4],
        state=JobState(row[5]),
        step=row[6],
        worktree=row[7],
        pid=row[8],
        heartbeat=parse_db_datetime(row[9]),
        result=json.loads(row[10]),
    )


class JobRepository:
    """Landing and sync jobs, and the branch locks their compare-and-swap runs under. Both live in
    `state.db`, so a job row and the node status it moves commit together."""

    def __init__(self, db_mgr: DatabaseManager) -> None:
        self.db = db_mgr

    def create(self, job: Job) -> Job:
        created = job.model_copy(update={"id": job.id or f"{job.kind}-{uuid.uuid4().hex[:12]}"})
        with self.db.get_state_connection() as conn:
            conn.execute(
                f"INSERT INTO jobs ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    created.id,
                    created.kind.value,
                    created.node_id,
                    created.repo,
                    created.target,
                    created.state.value,
                    created.step,
                    created.worktree,
                    created.pid,
                    to_db_timestamp(created.heartbeat),
                    json.dumps(created.result),
                ),
            )
            self.db.spec_commit(conn)
        return created

    def get(self, job_id: str) -> Job | None:
        with self.db.get_state_connection() as conn:
            row = conn.execute(f"SELECT {_COLUMNS} FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _row_to_job(row) if row else None

    def for_node(self, node_id: str) -> list[Job]:
        with self.db.get_state_connection() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM jobs WHERE node_id = ? ORDER BY rowid ASC", (node_id,)
            ).fetchall()
        return [_row_to_job(r) for r in rows]

    def update(self, job: Job) -> None:
        with self.db.get_state_connection() as conn:
            cursor = conn.execute(
                """
                UPDATE jobs SET state = ?, step = ?, worktree = ?, pid = ?, heartbeat = ?,
                    result_json = ?
                WHERE id = ?
                """,
                (
                    job.state.value,
                    job.step,
                    job.worktree,
                    job.pid,
                    to_db_timestamp(job.heartbeat),
                    json.dumps(job.result),
                    job.id,
                ),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"no job '{job.id}'")
            self.db.spec_commit(conn)

    def waiting_for_agent(self) -> list[Job]:
        with self.db.get_state_connection() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM jobs WHERE state = ? ORDER BY rowid ASC",
                (JobState.NEEDS_AGENT.value,),
            ).fetchall()
        return [_row_to_job(r) for r in rows]

    def acquire_branch(self, repo: str, branch: str, holder: str) -> bool:
        """Take `branch`'s lock for the job `holder`. A lock held by a job that is no longer
        running is stale, since only a running job moves a branch, and is taken over."""
        with self.db.get_state_connection() as conn:
            conn.execute(
                "DELETE FROM branch_locks WHERE repo = ? AND branch = ? AND holder <> ? "
                "AND holder NOT IN (SELECT id FROM jobs WHERE state = ?)",
                (repo, branch, holder, JobState.RUNNING.value),
            )
            conn.execute(
                "INSERT INTO branch_locks (repo, branch, holder, heartbeat) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(repo, branch) DO UPDATE SET heartbeat = excluded.heartbeat"
                " WHERE branch_locks.holder = excluded.holder",
                (repo, branch, holder, to_db_timestamp(None)),
            )
            (held_by,) = conn.execute(
                "SELECT holder FROM branch_locks WHERE repo = ? AND branch = ?", (repo, branch)
            ).fetchone()
            self.db.spec_commit(conn)
            return bool(held_by == holder)

    def release_branch(self, repo: str, branch: str, holder: str) -> None:
        with self.db.get_state_connection() as conn:
            conn.execute(
                "DELETE FROM branch_locks WHERE repo = ? AND branch = ? AND holder = ?",
                (repo, branch, holder),
            )
            self.db.spec_commit(conn)
```

Create `src/taskmanager/db/cache_repo.py`:

```python
import hashlib
import json
from datetime import UTC, datetime

from taskmanager.core.models import GateRun
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.utils import parse_db_datetime, to_db_timestamp


def _command_hash(command: str) -> str:
    return hashlib.sha256(command.encode("utf-8")).hexdigest()


class CacheRepository:
    """Results worth reusing across commands: gate baselines per target sha, and condition exit
    codes. Every write commits at once; nothing here joins a `state.db` transaction."""

    def __init__(self, db_mgr: DatabaseManager) -> None:
        self.db = db_mgr

    def get_baseline(self, repo: str, sha: str, template_hash: str) -> GateRun | None:
        with self.db.get_cache_connection() as conn:
            row = conn.execute(
                "SELECT exit_code, failing_json, tail FROM gate_baselines "
                "WHERE repo = ? AND target_sha = ? AND template_hash = ?",
                (repo, sha, template_hash),
            ).fetchone()
        if row is None:
            return None
        failing = None if row[1] is None else frozenset(json.loads(row[1]))
        return GateRun(exit_code=row[0], failing=failing, tail=row[2])

    def put_baseline(self, repo: str, sha: str, template_hash: str, run: GateRun) -> None:
        failing = None if run.failing is None else json.dumps(sorted(run.failing))
        with self.db.get_cache_connection() as conn:
            conn.execute(
                """
                INSERT INTO gate_baselines
                    (repo, target_sha, template_hash, exit_code, failing_json, tail, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(repo, target_sha, template_hash) DO UPDATE SET
                    exit_code = excluded.exit_code,
                    failing_json = excluded.failing_json,
                    tail = excluded.tail,
                    created_at = excluded.created_at
                """,
                (repo, sha, template_hash, run.exit_code, failing, run.tail, to_db_timestamp(None)),
            )
            conn.commit()

    def get_condition(self, node_id: str, idx: int, command: str, max_age: int) -> int | None:
        """The exit code last recorded for this condition, while it is at most `max_age` seconds
        old and was recorded for the same command; None otherwise."""
        with self.db.get_cache_connection() as conn:
            row = conn.execute(
                "SELECT exit_code, checked_at FROM condition_results "
                "WHERE node_id = ? AND idx = ? AND command_hash = ?",
                (node_id, idx, _command_hash(command)),
            ).fetchone()
        if row is None:
            return None
        age = (datetime.now(tz=UTC) - parse_db_datetime(row[1])).total_seconds()
        return int(row[0]) if age <= max_age else None

    def put_condition(self, node_id: str, idx: int, command: str, exit_code: int) -> None:
        with self.db.get_cache_connection() as conn:
            conn.execute(
                """
                INSERT INTO condition_results (node_id, idx, command_hash, exit_code, checked_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(node_id, idx) DO UPDATE SET
                    command_hash = excluded.command_hash,
                    exit_code = excluded.exit_code,
                    checked_at = excluded.checked_at
                """,
                (node_id, idx, _command_hash(command), exit_code, to_db_timestamp(None)),
            )
            conn.commit()
```

In `src/taskmanager/engine/graph.py`, replace `GraphEngine._is_lease_active` in full with:

```python
    def _is_lease_active(self, lease: Lease) -> bool:
        if lease.ttl_seconds is None:
            return True
        last_hb = lease.last_heartbeat
        if last_hb.tzinfo is None:
            last_hb = last_hb.astimezone(UTC)
        now = datetime.now(tz=UTC)
        return (now - last_hb).total_seconds() <= lease.ttl_seconds
```

In `src/taskmanager/di/container.py`, replace:

```python
from taskmanager.db.connection import DatabaseManager
```

with:

```python
from taskmanager.db.cache_repo import CacheRepository
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.job_repo import JobRepository
```

In `src/taskmanager/di/container.py`, add after `TaskManagerProvider.ledger_repo`:

```python
@provide(scope=Scope.APP)
def job_repo(self, db_mgr: DatabaseManager) -> JobRepository:
    return JobRepository(db_mgr)


@provide(scope=Scope.APP)
def cache_repo(self, db_mgr: DatabaseManager) -> CacheRepository:
    return CacheRepository(db_mgr)
```

In `src/taskmanager/di/container.py`, replace:

```python
    get_ledger_repo = ledger_repo
```

with:

```python
    get_ledger_repo = ledger_repo
    get_job_repo = job_repo
    get_cache_repo = cache_repo
```

- [ ] **Step 4: Run the tests and the gates**

```bash
uv run --directory <worktree> pytest tests/unit/test_coordination.py -q; echo $?
uv run --directory <worktree> pytest -q; echo $?
uv run --directory <worktree> ruff check; echo $?
uv run --directory <worktree> ruff format --check; echo $?
uv run --directory <worktree> mypy; echo $?
```

Expected: every command prints `0` last.

- [ ] **Step 5: Commit**

```bash
git -C <worktree> add src/taskmanager/db/schema.py src/taskmanager/db/connection.py src/taskmanager/db/__init__.py src/taskmanager/core/models.py src/taskmanager/db/runtime_repo.py src/taskmanager/db/job_repo.py src/taskmanager/db/cache_repo.py src/taskmanager/engine/graph.py src/taskmanager/di/container.py tests/unit/test_coordination.py && git -C <worktree> commit -m "feat(storage): claim a node, its lease and its file locks in one immediate transaction"
```

### Task 10: Snapshot builder

**Spec:** §4.1, §4.4 (what the step graph reads), §5.4, §7.2 (the facts behind each row)
**Files:**
- Create: `src/taskmanager/engine/snapshot.py`
- Modify: `src/taskmanager/db/node_repo.py` (new `NodeRepository.relations`)
- Modify: `src/taskmanager/engine/wave.py` (`_writes_migration` deleted; its callers use `snapshot.writes_migration`, so the migration-path rule lives once)
- Test: `tests/unit/test_snapshot.py`

**Interfaces:**
- Consumes: `Cycle`, `next_action` (Task 2); `Facts` (Task 4); `chains.satisfied` (Task 5); `SnapNode`, `Snapshot` (Task 6); `lease_alive`, `JobRepository` (Task 9); `Node` fields (Task 8)
- Produces: `SnapshotBuilder(node_repo, runtime_repo, job_repo)` with `.build()`, `.facts()`, `.cycle()`, `.lock_set(node_id, snapshot)`; module functions `writes_migration`, `names_origin_main`, `stored_status`, `cycle_of`, `apply_cycle`, `node_busy`, constant `CONTAINERS`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_snapshot.py`:

```python
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from taskmanager.core.display import Facts
from taskmanager.core.enums import NodeKind, NodeStatus, RelationType, VerificationType
from taskmanager.core.lifecycle import Cycle
from taskmanager.core.models import FileLock, Job, Lease, Node, NodeRelation, NodeVerification
from taskmanager.core.status import DecisionStatus, JobKind, JobState, Merge, Outcome, Status
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.job_repo import JobRepository
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.engine.snapshot import SnapshotBuilder, apply_cycle, cycle_of


class Estate:
    def __init__(self, root: Path) -> None:
        db = DatabaseManager(root / ".taskmanager")
        db.init_all()
        self.nodes = NodeRepository(db)
        self.runtime = RuntimeRepository(db)
        self.jobs = JobRepository(db)
        self.builder = SnapshotBuilder(self.nodes, self.runtime, self.jobs)

    def add(
        self,
        node_id: str,
        kind: NodeKind = NodeKind.TASK,
        parent: str | None = None,
        **fields: object,
    ) -> None:
        self.nodes.save_node(
            Node.model_validate({"id": node_id, "kind": kind, "title": node_id, **fields})
        )
        if parent is not None:
            self.nodes.add_relation(
                NodeRelation(
                    source_id=parent, target_id=node_id, relation_type=RelationType.CONTAINS
                )
            )

    def depend(self, dependent: str, dependency: str) -> None:
        self.nodes.add_relation(
            NodeRelation(
                source_id=dependent, target_id=dependency, relation_type=RelationType.DEPENDS_ON
            )
        )

    def lease(
        self, node_id: str, age: timedelta = timedelta(0), files: tuple[str, ...] = ()
    ) -> None:
        self.runtime.acquire_lease(
            Lease(
                task_id=node_id,
                agent_id="a",
                session_id="s",
                branch_name=f"tm/{node_id}",
                ttl_seconds=60,
                last_heartbeat=datetime.now(tz=UTC) - age,
            ),
            [FileLock(file_path=f, task_id=node_id) for f in files],
        )

    def job(self, node_id: str, kind: JobKind, state: JobState) -> None:
        self.jobs.create(Job(kind=kind, node_id=node_id, repo="core", target="main", state=state))

    def facts(self, node_id: str) -> Facts:
        return self.builder.facts(node_id, self.builder.build())


@pytest.fixture
def estate(tmp_path: Path) -> Estate:
    return Estate(tmp_path)


def test_a_snapshot_carries_every_node_its_parent_and_every_edge(estate: Estate) -> None:
    estate.add("S", NodeKind.SPEC, status=Status.READY)
    estate.add("P", NodeKind.PLAN, parent="S", status=Status.READY, review=True, fix=True)
    estate.add(
        "T1",
        parent="P",
        status=Status.IMPLEMENTED,
        merge=Merge.PARENT,
        fix=False,
        target_repo="core",
    )
    estate.add("T2", parent="P", status=Status.READY)
    estate.depend("T2", "T1")
    snap = estate.builder.build()
    assert set(snap.nodes) == {"S", "P", "T1", "T2"}
    assert snap.edges == [("T2", "T1")]
    t1 = snap.nodes["T1"]
    assert (t1.parent, t1.kind, t1.merge, t1.status, t1.review, t1.fix, t1.repo) == (
        "P",
        NodeKind.TASK,
        Merge.PARENT,
        Status.IMPLEMENTED,
        True,
        False,
        "core",
    )
    assert (snap.nodes["P"].review, snap.nodes["S"].review, snap.nodes["S"].parent) == (
        True,
        False,
        None,
    )


@pytest.mark.parametrize(
    ("kind", "stored", "read"),
    [
        (NodeKind.TASK, NodeStatus.NOT_STARTED, Status.READY),
        (NodeKind.TASK, NodeStatus.WAITING_REVIEW, Status.IMPLEMENTED),
        (NodeKind.TASK, NodeStatus.WAITING_FIXES, Status.REVIEWED),
        (NodeKind.TASK, NodeStatus.WAITING_MERGE, Status.REVIEWED),
        (NodeKind.TASK, NodeStatus.COMPLETED, Status.COMPLETED),
        (NodeKind.TASK, Status.FAILED, Status.FAILED),
        (NodeKind.DECISION, NodeStatus.NOT_STARTED, DecisionStatus.OPEN),
        (NodeKind.DECISION, NodeStatus.COMPLETED, DecisionStatus.ANSWERED),
        (NodeKind.DECISION, NodeStatus.ABANDONED, DecisionStatus.WITHDRAWN),
        (NodeKind.DECISION, DecisionStatus.OPEN, DecisionStatus.OPEN),
    ],
)
def test_a_snapshot_reads_every_stored_status_in_the_new_vocabulary(
    estate: Estate, kind: NodeKind, stored: str, read: str
) -> None:
    estate.add("N", kind, status=stored)
    assert estate.builder.build().nodes["N"].status == read


@pytest.mark.parametrize(
    ("files", "migrates"),
    [
        (["core/src/app/models.py"], False),
        (["core/migrations/versions/0007_add_x.py"], True),
    ],
)
def test_a_node_declaring_a_migration_file_writes_a_migration(
    estate: Estate, files: list[str], migrates: bool
) -> None:
    estate.add("T", frontmatter={"declared_files": files})
    assert estate.builder.build().nodes["T"].writes_migration is migrates


@pytest.mark.parametrize(
    ("command", "literal"),
    [
        ("git -C core show origin/main:app.py | grep -q x", True),
        ('git -C core show "$TM_VERIFY_REF":app.py | grep -q x', False),
        ('git -C core show "${TM_VERIFY_REF:-origin/main}":app.py | grep -q x', False),
        (
            'git -C core show "${TM_VERIFY_REF:-origin/main}":a.py && git -C core log origin/main',
            True,
        ),
        ('git -C core show "${OTHER_REF:-origin/main}":app.py | grep -q x', True),
    ],
    ids=["literal", "variable", "variable-defaulting-to-it", "default-and-literal", "other-var"],
)
def test_a_test_command_naming_origin_main_is_flagged(
    estate: Estate, command: str, literal: bool
) -> None:
    estate.add("T")
    estate.nodes.add_verification(
        NodeVerification(
            node_id="T",
            verification_type=VerificationType.TEST_COMMAND,
            target_path="check",
            expected_pattern=command,
        )
    )
    assert estate.builder.build().nodes["T"].literal_origin_main is literal


@pytest.mark.parametrize(
    ("arrange", "busy"),
    [
        (lambda e: None, False),
        (lambda e: e.lease("T"), True),
        (lambda e: e.lease("T", age=timedelta(hours=1)), False),
        (lambda e: e.job("T", JobKind.LAND, JobState.RUNNING), True),
        (lambda e: e.job("T", JobKind.LAND, JobState.NEEDS_AGENT), True),
        (lambda e: e.job("T", JobKind.LAND, JobState.SUCCEEDED), False),
        (lambda e: e.job("T", JobKind.SYNC, JobState.OWN_DEFECT), False),
    ],
    ids=[
        "idle",
        "live-lease",
        "expired-lease",
        "running-job",
        "job-waiting-for-agent",
        "finished-job",
        "failed-job",
    ],
)
def test_a_node_is_busy_while_it_holds_a_live_lease_or_an_unfinished_job(
    estate: Estate, arrange: Callable[[Estate], object], busy: bool
) -> None:
    estate.add("T")
    arrange(estate)
    assert estate.builder.build().nodes["T"].busy is busy


def test_a_cycle_carries_the_stored_lifecycle_fields() -> None:
    node = Node(
        id="P",
        kind=NodeKind.PLAN,
        title="p",
        status=Status.REVIEWED,
        review=True,
        fix=True,
        outcome=Outcome.REJECT,
        fix_for=Outcome.REJECT,
        claimed_from=None,
        review_cycles=2,
        merge_attempts=1,
        step_failures=1,
    )
    assert cycle_of(node) == Cycle(
        status=Status.REVIEWED,
        container=True,
        review=True,
        fix=True,
        outcome=Outcome.REJECT,
        fix_for=Outcome.REJECT,
        review_cycles=2,
        merge_attempts=1,
        step_failures=1,
    )


@pytest.mark.parametrize(
    ("stored", "status", "outcome"),
    [
        (NodeStatus.NOT_STARTED, Status.READY, None),
        (NodeStatus.WAITING_FIXES, Status.REVIEWED, Outcome.REJECT),
        (NodeStatus.WAITING_MERGE, Status.REVIEWED, Outcome.APPROVE),
    ],
)
def test_a_cycle_reads_an_old_status_as_the_position_it_held(
    stored: NodeStatus, status: Status, outcome: Outcome | None
) -> None:
    c = cycle_of(Node(id="T", kind=NodeKind.TASK, title="t", status=stored))
    assert (c.status, c.outcome) == (status, outcome)


def test_applying_a_cycle_round_trips_through_storage(estate: Estate) -> None:
    estate.add("T", status=Status.READY)
    node = estate.nodes.get_node("T")
    assert node is not None
    moved = Cycle(status=Status.IMPLEMENTING, claimed_from=Status.READY, step_failures=2)
    estate.nodes.save_node(apply_cycle(node, moved))
    stored = estate.nodes.get_node("T")
    assert stored is not None and cycle_of(stored) == moved


def test_facts_of_an_idle_ready_task_are_all_clear(estate: Estate) -> None:
    estate.add("T", status=Status.READY)
    assert estate.facts("T") == Facts()


@pytest.mark.parametrize(
    ("age", "lease"), [(timedelta(0), "live"), (timedelta(hours=1), "expired")]
)
def test_facts_tell_a_live_lease_from_an_expired_one(
    estate: Estate, age: timedelta, lease: str
) -> None:
    estate.add("T", status=Status.IMPLEMENTING, claimed_from=Status.READY)
    estate.lease("T", age=age)
    assert estate.facts("T").lease == lease


def test_facts_see_a_landing_job_waiting_for_an_agent(estate: Estate) -> None:
    estate.add("T", status=Status.MERGING, claimed_from=Status.REVIEWED)
    estate.job("T", JobKind.LAND, JobState.NEEDS_AGENT)
    assert estate.facts("T").job_needs_agent is True


@pytest.mark.parametrize(
    ("decision_status", "open_decision"),
    [
        (DecisionStatus.OPEN, True),
        (DecisionStatus.ANSWERED, False),
        (DecisionStatus.WITHDRAWN, False),
    ],
)
def test_facts_see_an_open_decision_on_the_node_or_any_ancestor(
    estate: Estate, decision_status: DecisionStatus, open_decision: bool
) -> None:
    estate.add("P", NodeKind.PLAN, status=Status.READY)
    estate.add("T", parent="P", status=Status.READY)
    estate.add("D1", NodeKind.DECISION, status=decision_status)
    estate.depend("P", "D1")
    assert estate.facts("T").open_decision is open_decision
    assert estate.facts("T").unsatisfied_edge is False


@pytest.mark.parametrize(
    ("dependency_status", "unsatisfied"),
    [
        (Status.READY, True),
        (Status.MERGING, True),
        (Status.FAILED, True),
        (Status.COMPLETED, False),
        (Status.SUPERSEDED, False),
    ],
)
def test_facts_see_an_unsatisfied_edge_own_or_inherited(
    estate: Estate, dependency_status: Status, unsatisfied: bool
) -> None:
    estate.add("P", NodeKind.PLAN, status=Status.READY)
    estate.add("T", parent="P", status=Status.READY)
    estate.add("Y", status=dependency_status)
    estate.add("Z", status=dependency_status)
    estate.depend("T", "Y")
    estate.depend("P", "Z")
    assert estate.facts("T").unsatisfied_edge is unsatisfied
    assert estate.facts("P").unsatisfied_edge is unsatisfied


@pytest.mark.parametrize(
    ("state", "pending"),
    [
        (JobState.RUNNING, True),
        (JobState.NEEDS_AGENT, True),
        (JobState.SUCCEEDED, False),
    ],
)
def test_facts_see_a_sync_the_claim_waits_on(
    estate: Estate, state: JobState, pending: bool
) -> None:
    estate.add("T", status=Status.READY)
    estate.job("T", JobKind.SYNC, state)
    assert estate.facts("T").sync_pending is pending


@pytest.mark.parametrize(
    ("status", "locked"),
    [
        (Status.READY, True),
        (Status.REVIEWED, True),
        (Status.IMPLEMENTED, False),
        (Status.FIXED, False),
    ],
)
def test_facts_see_a_declared_file_locked_only_when_the_next_action_locks_files(
    estate: Estate, status: Status, locked: bool
) -> None:
    estate.add(
        "T",
        status=status,
        outcome=Outcome.REJECT if status == Status.REVIEWED else None,
        frontmatter={"declared_files": ["a.py"]},
    )
    estate.add("OTHER", status=Status.IMPLEMENTING, claimed_from=Status.READY)
    estate.lease("OTHER", files=("a.py",))
    assert estate.facts("T").files_locked is locked


def test_a_container_declaring_no_files_locks_its_descendants_files(estate: Estate) -> None:
    estate.add("P", NodeKind.PLAN, status=Status.READY)
    estate.add("T1", parent="P", frontmatter={"declared_files": ["a.py", "b.py"]})
    estate.add("T2", parent="P", frontmatter={"declared_files": ["b.py", "c.py"]})
    snap = estate.builder.build()
    assert estate.builder.lock_set("P", snap) == ["a.py", "b.py", "c.py"]
    estate.add("Q", NodeKind.PLAN, status=Status.READY, frontmatter={"declared_files": ["q.py"]})
    assert estate.builder.lock_set("Q", estate.builder.build()) == ["q.py"]


@pytest.mark.parametrize(
    ("child_statuses", "started"),
    [
        ([Status.READY, Status.READY], False),
        ([Status.READY, Status.ABANDONED], False),
        ([Status.READY, Status.IMPLEMENTING], True),
        ([Status.COMPLETED, Status.READY], True),
    ],
)
def test_facts_see_a_container_whose_descendant_has_started(
    estate: Estate, child_statuses: list[Status], started: bool
) -> None:
    estate.add("S", NodeKind.SPEC, status=Status.READY)
    estate.add("P", NodeKind.PLAN, parent="S", status=Status.READY)
    for n, status in enumerate(child_statuses):
        estate.add(f"T{n}", parent="P", status=status)
    assert estate.facts("S").descendant_started is started
```

- [ ] **Step 2: Run it and watch it fail**

```bash
uv run --directory <worktree> pytest tests/unit/test_snapshot.py -q; echo $?
```

Expected: `ModuleNotFoundError: No module named 'taskmanager.engine.snapshot'` during collection, exit 2.

- [ ] **Step 3: Implement**

Nodes written before Task 17 moves the writers still store `NOT_STARTED`, `WAITING_*` and, for decisions, `NOT_STARTED`/`COMPLETED`/`ABANDONED`; `stored_status` and `cycle_of` read each as the position it held, so every engine service can run on a mixed estate until Task 21 deletes the old names. `facts` leaves `unmet_condition` False: a condition is a shell command, and only the `ConditionRunner` (Task 11) runs one.

Create `src/taskmanager/engine/snapshot.py`:

```python
import re
from datetime import UTC, datetime
from typing import Literal

from taskmanager.core.display import Facts
from taskmanager.core.enums import NodeKind, RelationType, VerificationType
from taskmanager.core.lifecycle import Cycle, next_action
from taskmanager.core.models import Node
from taskmanager.core.status import (
    EXITS,
    Action,
    DecisionStatus,
    JobKind,
    JobState,
    Outcome,
    Status,
)
from taskmanager.db.job_repo import JobRepository
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository, lease_alive
from taskmanager.engine.chains import satisfied
from taskmanager.engine.stepgraph import SnapNode, Snapshot

CONTAINERS = frozenset({NodeKind.PLAN, NodeKind.SPEC})

# Writers not yet on the new vocabulary store these names; each maps to the position it means.
_LEGACY: dict[str, tuple[Status, Outcome | None]] = {
    "NOT_STARTED": (Status.READY, None),
    "WAITING_REVIEW": (Status.IMPLEMENTED, None),
    "WAITING_FIXES": (Status.REVIEWED, Outcome.REJECT),
    "WAITING_MERGE": (Status.REVIEWED, Outcome.APPROVE),
}
_LEGACY_DECISION: dict[str, DecisionStatus] = {
    "NOT_STARTED": DecisionStatus.OPEN,
    "COMPLETED": DecisionStatus.ANSWERED,
    "ABANDONED": DecisionStatus.WITHDRAWN,
}
# A job stopped for an agent still owns its node's worktree and lease, as a running one does.
_LIVE_JOB = frozenset({JobState.RUNNING, JobState.NEEDS_AGENT})
_LOCKING = frozenset({Action.IMPLEMENT, Action.FIX})
_NOT_STARTED = frozenset({Status.READY}) | EXITS
# `tm verify run --ref` exports TM_VERIFY_REF, so an `origin/main` fallback inside this
# expansion follows the ref being verified; only a bare `origin/main` pins a check to main.
_VERIFY_REF_EXPANSION = re.compile(r"\$\{TM_VERIFY_REF:-[^}]*\}")


def writes_migration(files: list[str]) -> bool:
    return any("migrations/versions/" in f for f in files)


def names_origin_main(command: str) -> bool:
    return "origin/main" in _VERIFY_REF_EXPANSION.sub("", command)


def stored_status(node: Node) -> Status | DecisionStatus:
    raw = node.status.value
    if node.kind == NodeKind.DECISION:
        return _LEGACY_DECISION.get(raw) or DecisionStatus(raw)
    legacy = _LEGACY.get(raw)
    return legacy[0] if legacy else Status(raw)


def cycle_of(node: Node) -> Cycle:
    legacy = _LEGACY.get(node.status.value)
    status, outcome = legacy if legacy else (Status(node.status.value), None)
    return Cycle(
        status=status,
        container=node.kind in CONTAINERS,
        review=node.review,
        fix=node.fix,
        outcome=node.outcome or outcome,
        fix_for=node.fix_for,
        claimed_from=node.claimed_from,
        review_cycles=node.review_cycles,
        merge_attempts=node.merge_attempts,
        step_failures=node.step_failures,
    )


def apply_cycle(node: Node, c: Cycle) -> Node:
    """`node` carrying `c`'s stored fields, for a writer to save."""
    return node.model_copy(
        update={
            "status": c.status,
            "claimed_from": c.claimed_from,
            "outcome": c.outcome,
            "fix_for": c.fix_for,
            "review_cycles": c.review_cycles,
            "merge_attempts": c.merge_attempts,
            "step_failures": c.step_failures,
            "updated_at": datetime.now(tz=UTC),
        }
    )


def node_busy(
    runtime_repo: RuntimeRepository, job_repo: JobRepository | None, node_id: str
) -> bool:
    """A live lease or an unfinished job: a step is running, and nothing may move the node."""
    lease = runtime_repo.get_lease(node_id)
    if lease is not None and lease_alive(
        lease.ttl_seconds, lease.last_heartbeat, datetime.now(tz=UTC)
    ):
        return True
    return job_repo is not None and any(j.state in _LIVE_JOB for j in job_repo.for_node(node_id))


# ponytail: one query per node for its files, lease and jobs; bulk reads once a graph is large
# enough for it to show.
class SnapshotBuilder:
    """The whole graph as the pure rules read it, and the per-node facts display derives from."""

    def __init__(
        self, node_repo: NodeRepository, runtime_repo: RuntimeRepository, job_repo: JobRepository
    ) -> None:
        self.node_repo = node_repo
        self.runtime_repo = runtime_repo
        self.job_repo = job_repo

    def build(self) -> Snapshot:
        parents: dict[str, str] = {}
        for source, target in self.node_repo.relations(RelationType.CONTAINS):
            parents.setdefault(target, source)
        nodes = {n.id: self._snap(n, parents.get(n.id)) for n in self.node_repo.list_nodes()}
        return Snapshot(nodes=nodes, edges=self.node_repo.relations(RelationType.DEPENDS_ON))

    def cycle(self, node: Node) -> Cycle:
        return cycle_of(node)

    def lock_set(self, node_id: str, snapshot: Snapshot) -> list[str]:
        """The files a claim of this node locks: its declared files, or for a container that
        declares none, the union of its descendants'."""
        own = self.node_repo.declared_files(node_id)
        if own or snapshot.nodes[node_id].kind not in CONTAINERS:
            return own
        files = [f for d in snapshot.descendants(node_id) for f in self.node_repo.declared_files(d)]
        return list(dict.fromkeys(files))

    def facts(self, node_id: str, snapshot: Snapshot) -> Facts:
        """Everything display derivation needs beyond the node's own cycle. `unmet_condition`
        is left False: conditions run through the ConditionRunner, whose result a caller folds
        in with `dataclasses.replace`."""
        node = self.node_repo.get_node(node_id)
        if node is None:
            raise KeyError(node_id)
        jobs = self.job_repo.for_node(node_id)
        deps = snapshot.inherited_edges(node_id)
        decisions = [d for d in deps if snapshot.nodes[d].kind == NodeKind.DECISION]
        locking = next_action(cycle_of(node)) in _LOCKING
        return Facts(
            lease=self._lease(node_id, datetime.now(tz=UTC)),
            job_needs_agent=any(
                j.kind == JobKind.LAND and j.state == JobState.NEEDS_AGENT for j in jobs
            ),
            open_decision=any(snapshot.status(d) == DecisionStatus.OPEN for d in decisions),
            unsatisfied_edge=any(
                not satisfied(snapshot, node_id, d) for d in deps if d not in decisions
            ),
            sync_pending=any(j.kind == JobKind.SYNC and j.state in _LIVE_JOB for j in jobs),
            files_locked=locking
            and bool(self.runtime_repo.get_conflicting_tasks(self.lock_set(node_id, snapshot))),
            descendant_started=any(
                snapshot.status(d) not in _NOT_STARTED for d in snapshot.descendants(node_id)
            ),
        )

    def _lease(self, node_id: str, now: datetime) -> Literal["live", "expired", "none"]:
        lease = self.runtime_repo.get_lease(node_id)
        if lease is None:
            return "none"
        return "live" if lease_alive(lease.ttl_seconds, lease.last_heartbeat, now) else "expired"

    def _snap(self, node: Node, parent: str | None) -> SnapNode:
        commands = [
            v.expected_pattern or v.target_path
            for v in self.node_repo.get_verifications(node.id)
            if v.verification_type == VerificationType.TEST_COMMAND
        ]
        busy = node_busy(self.runtime_repo, self.job_repo, node.id)
        return SnapNode(
            id=node.id,
            kind=node.kind,
            parent=parent,
            merge=node.merge,
            status=stored_status(node),
            review=node.review,
            fix=node.fix,
            repo=node.target_repo,
            writes_migration=writes_migration(self.node_repo.declared_files(node.id)),
            busy=busy,
            literal_origin_main=any(names_origin_main(c) for c in commands),
        )
```

In `src/taskmanager/db/node_repo.py`, add after `NodeRepository.remove_relation`:

```python
    def relations(self, relation_type: RelationType) -> list[tuple[str, str]]:
        """Every (source, target) pair of one relation type, in insertion order."""
        with self.db.get_state_connection() as conn:
            rows = conn.execute(
                "SELECT source_id, target_id FROM node_relations WHERE relation_type = ? "
                "ORDER BY rowid ASC",
                (relation_type.value,),
            ).fetchall()
        return [(r[0], r[1]) for r in rows]
```

In `src/taskmanager/engine/wave.py`, replace:

```python
from taskmanager.engine.operations import Operations
```

with:

```python
from taskmanager.engine.operations import Operations
from taskmanager.engine.snapshot import writes_migration
```

In `src/taskmanager/engine/wave.py`, replace:

```python
# still holds its repo's one-writer chain lock (see `_writes_migration`).
```

with:

```python
# still holds its repo's one-writer chain lock (see `writes_migration`).
```

In `src/taskmanager/engine/wave.py`, delete the whole of `_writes_migration`.

In `src/taskmanager/engine/wave.py`, replace:

```python
            if _writes_migration(node_repo.declared_files(task.id)):
```

with:

```python
            if writes_migration(node_repo.declared_files(task.id)):
```

In `src/taskmanager/engine/wave.py`, replace:

```python
        if status == "READY" and _writes_migration(files) and repo in chain_held:
```

with:

```python
        if status == "READY" and writes_migration(files) and repo in chain_held:
```

In `src/taskmanager/engine/wave.py`, replace:

```python
                "migration": _writes_migration(files),
```

with:

```python
                "migration": writes_migration(files),
```

In `src/taskmanager/engine/wave.py`, replace:

```python
        if status == "READY" and _writes_migration(files):
```

with:

```python
        if status == "READY" and writes_migration(files):
```

- [ ] **Step 4: Run the tests and the gates**

```bash
uv run --directory <worktree> pytest tests/unit/test_snapshot.py -q; echo $?
uv run --directory <worktree> pytest -q; echo $?
uv run --directory <worktree> ruff check; echo $?
uv run --directory <worktree> ruff format --check; echo $?
uv run --directory <worktree> mypy; echo $?
```

Expected: every command prints `0` last.

- [ ] **Step 5: Commit**

```bash
git -C <worktree> add src/taskmanager/engine/snapshot.py src/taskmanager/db/node_repo.py src/taskmanager/engine/wave.py tests/unit/test_snapshot.py && git -C <worktree> commit -m "feat(engine): build the graph snapshot and per-node facts the pure rules read"
```

### Task 11: Config keys and condition runner

**Spec:** §3.2 (caps), §4.3, §5.2 (per-action TTLs), §6.2 (`red_target_decision_after`), §8
**Files:**
- Modify: `src/taskmanager/engine/config.py` (`KEYS`, `_WHOLE`, `LEASE_TTL_DEFAULTS`, `FixRounds`, `Gate`, `RepoConfig`, `ProjectConfig`, `_flatten`, `_typed`, `ConfigStore.project`, `ConfigStore.embeddings`, `ConfigStore.lease_ttl`)
- Modify: `src/taskmanager/cli/main.py` (`run_start` reads the implement TTL through `ConfigStore.lease_ttl`; import of `Action`)
- Modify: `src/taskmanager/di/container.py` (`condition_runner` provider)
- Create: `src/taskmanager/engine/conditions.py`
- Modify: `tests/unit/test_config.py` (the `lease_ttl` default row, `test_get_prints_the_effective_value` and the two default-TTL expectations of `test_run_start_takes_its_worktree_directory_and_ttl_through_the_precedence` are rewritten for the per-action map; new bad-value rows and new tests appended)
- Test: `tests/unit/test_conditions.py`, `tests/unit/test_config.py`

**Interfaces:**
- Consumes: `Action`, `ConditionStage` (Task 1); `Condition`, `NodeRepository.get_conditions` (Task 8); `CacheRepository` (Task 9); `in_transaction` (Task 8)
- Produces: `ProjectConfig` fields of §8 with `lease_ttl_for(action)`; `ConfigStore.project()`, `ConfigStore.lease_ttl(action, flag=None)`; `FixRounds`, `Gate`, `RepoConfig`, `LEASE_TTL_DEFAULTS`; `ConditionRunner(root, node_repo, cache_repo, ttl, timeout).unmet`; `is_executable`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_conditions.py`:

```python
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
```

In `tests/unit/test_config.py`, replace:

```python
from taskmanager.core.models import Node
```

with:

```python
from taskmanager.core.models import Node
from taskmanager.core.status import Action
```

In `tests/unit/test_config.py`, replace:

```python
from taskmanager.engine.config import KEYS, ConfigError, ConfigStore, Resolved
```

with:

```python
from taskmanager.engine.config import (
    KEYS,
    LEASE_TTL_DEFAULTS,
    ConfigError,
    ConfigStore,
    FixRounds,
    Gate,
    ProjectConfig,
    RepoConfig,
    Resolved,
)
```

In `tests/unit/test_config.py`, replace:

```python
(("lease_ttl", "TM_LEASE_TTL", 900, "700", "500", (900, 700, 500, 300)),)
```

with:

```python
(("lease_ttl", "TM_LEASE_TTL", 900, "700", "500", (900, 700, 500, LEASE_TTL_DEFAULTS)),)
```

In `tests/unit/test_config.py`, replace `test_get_prints_the_effective_value` in full with:

```python
def test_get_prints_the_effective_value(root: Path) -> None:
    assert tm(root, "config", "get", "condition_ttl") == (0, "300\n")
    tm(root, "config", "set", "condition_ttl", "45")
    assert tm(root, "config", "get", "condition_ttl") == (0, "45\n")
```

In `tests/unit/test_config.py`, replace (the end of the parametrize table of `test_an_unknown_key_or_a_bad_value_is_one_line_and_exit_1`, which gains a row per new key):

```python
        ("set", "embeddings.api_key_env", "sk-proj-abc123"),
    ],
)
```

with:

```python
        ("set", "embeddings.api_key_env", "sk-proj-abc123"),
        ("set", "lease_ttl", "{deploy: 60}"),
        ("set", "lease_ttl", "{review: 0}"),
        ("set", "lease_ttl", "{review: ["),
        ("set", "max_fix_rounds.task", "-1"),
        ("set", "max_merge_attempts", "0"),
        ("set", "max_step_failures", "0"),
        ("set", "condition_ttl", "-5"),
        ("set", "condition_timeout", "0"),
        ("set", "red_target_decision_after", "0"),
        ("set", "repo_order", "core"),
        ("set", "repos", "{core: {gates: {staging: {command: make}}}}"),
        ("set", "repos", "{core: {gates: {main: {timeout: 60}}}}"),
        ("set", "repos", "{core: {gates: {main: {command: make, retries: 2}}}}"),
    ],
)
```

In `tests/unit/test_config.py`, replace:

```python
    assert start("T-1") == (root / ".worktrees", 300)
```

with:

```python
    assert start("T-1") == (root / ".worktrees", LEASE_TTL_DEFAULTS["implement"])
```

In `tests/unit/test_config.py`, replace:

```python
    assert start("T-5") == (root / ".worktrees", 300)
```

with:

```python
    assert start("T-5") == (root / ".worktrees", LEASE_TTL_DEFAULTS["implement"])
```

In `tests/unit/test_config.py`, add after `test_run_start_takes_its_worktree_directory_and_ttl_through_the_precedence`:

```python
def test_the_lifecycle_settings_default_to_the_documented_values() -> None:
    config = ProjectConfig()
    assert config.max_fix_rounds == FixRounds(task=2, container=3)
    assert (config.max_merge_attempts, config.max_step_failures) == (3, 3)
    assert (config.condition_ttl, config.condition_timeout) == (300, 60)
    assert config.red_target_decision_after == 3600
    assert config.lease_ttl == {
        "implement": 10800,
        "review": 3600,
        "fix": 7200,
        "merge": 3600,
        "sync": 3600,
    }
    assert (config.repo_order, config.repos) == ([], {})


@pytest.mark.parametrize(
    ("lease_ttl", "action", "seconds"),
    [
        (None, Action.IMPLEMENT, 10800),
        (None, Action.REVIEW, 3600),
        (None, Action.FIX, 7200),
        (None, Action.MERGE, 3600),
        (None, Action.SYNC, 3600),
        (500, Action.IMPLEMENT, 500),
        (500, Action.REVIEW, 3600),
        ({"review": 1800}, Action.REVIEW, 1800),
        ({"review": 1800}, Action.IMPLEMENT, 10800),
    ],
)
def test_each_action_has_its_lease_ttl_and_a_single_number_is_the_implementers(
    lease_ttl: dict[str, int] | int | None, action: Action, seconds: int
) -> None:
    config = ProjectConfig() if lease_ttl is None else ProjectConfig(lease_ttl=lease_ttl)
    assert config.lease_ttl_for(action) == seconds


def test_a_lease_ttl_flag_beats_every_stored_value(root: Path) -> None:
    store = ConfigStore(root)
    store.set("lease_ttl", "{implement: 100, review: 200}")
    assert store.lease_ttl(Action.REVIEW) == 200
    assert store.lease_ttl(Action.REVIEW, 50) == 50


def test_whole_valued_keys_are_set_as_yaml_and_stored_nested(root: Path) -> None:
    store = ConfigStore(root)
    store.set("lease_ttl", "{review: 1800}")
    store.set("repo_order", "[core, api, web]")
    store.set("repos", "{core: {gates: {main: {command: 'make ci', junit: 'out/*.xml'}}}}")
    store.set("max_fix_rounds.container", "4")
    assert yaml.safe_load(store.path.read_text()) == {
        "lease_ttl": {"review": 1800},
        "max_fix_rounds": {"container": 4},
        "repo_order": ["core", "api", "web"],
        "repos": {
            "core": {
                "gates": {"main": {"command": "make ci", "junit": "out/*.xml", "timeout": 3600}}
            }
        },
    }
    project = store.project()
    assert project.lease_ttl_for(Action.REVIEW) == 1800
    assert project.max_fix_rounds == FixRounds(task=2, container=4)
    assert project.repo_order == ["core", "api", "web"]
    assert project.repos == {
        "core": RepoConfig(gates={"main": Gate(command="make ci", junit="out/*.xml")})
    }


def test_a_hand_written_repos_block_reads_back_whole(root: Path) -> None:
    ConfigStore(root).path.write_text(
        "repos:\n"
        "  web:\n"
        "    gates:\n"
        "      main: {command: 'npm test', timeout: 900}\n"
        "      parent: {command: 'npm run lint'}\n",
        encoding="utf-8",
    )
    code, out = tm(root, "config", "get", "repos")
    assert code == 0 and "npm run lint" in out
    assert ConfigStore(root).project().repos["web"].gates == {
        "main": Gate(command="npm test", timeout=900),
        "parent": Gate(command="npm run lint"),
    }
```

- [ ] **Step 2: Run it and watch it fail**

```bash
uv run --directory <worktree> pytest tests/unit/test_conditions.py tests/unit/test_config.py -q; echo $?
```

Expected: `ModuleNotFoundError: No module named 'taskmanager.engine.conditions'` and `ImportError: cannot import name 'LEASE_TTL_DEFAULTS'`, both during collection, exit 2.

- [ ] **Step 3: Implement**

`lease_ttl`, `repos` and `repo_order` are set as one YAML value each (`tm config set repos '{core: {gates: {main: {command: ...}}}}'`), not as dotted keys: `repos` is keyed by repository names the key list cannot enumerate. A single number stored in `lease_ttl` keeps meaning the implementer's lease, as §8 says. The default implement TTL becomes 10800 s, so `tm run start` (removed in Task 18) now takes that default, and its test is rewritten to expect it.

In `src/taskmanager/engine/config.py`, replace:

```python
from typing import Any, Final, NamedTuple

import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator

from taskmanager.core.enums import EmbeddingProviderType
```

with:

```python
from typing import Any, Final, Literal, NamedTuple

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from taskmanager.core.enums import EmbeddingProviderType
from taskmanager.core.status import Action
```

In `src/taskmanager/engine/config.py`, replace `KEYS` in full with:

```python
KEYS: Final = (
    "embeddings.provider",
    "embeddings.model",
    "embeddings.base_url",
    "embeddings.api_key_env",
    "embeddings.dimensions",
    "worktree_dir",
    "lease_ttl",
    "max_fix_rounds.task",
    "max_fix_rounds.container",
    "max_merge_attempts",
    "max_step_failures",
    "condition_ttl",
    "condition_timeout",
    "red_target_decision_after",
    "repo_order",
    "repos",
)
```

In `src/taskmanager/engine/config.py`, add after `KEYS`:

```python
# Keys whose value is a whole mapping or list: stored and set as one value, never split into
# dotted keys, and parsed from YAML when set from the command line.
_WHOLE: Final = frozenset({"lease_ttl", "repos", "repo_order"})

LEASE_TTL_DEFAULTS: Final = {
    "implement": 10800,
    "review": 3600,
    "fix": 7200,
    "merge": 3600,
    "sync": 3600,
}
```

In `src/taskmanager/engine/config.py`, add after `EmbeddingsConfig`:

```python
class FixRounds(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: int = Field(default=2, ge=0)
    container: int = Field(default=3, ge=0)


class Gate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str = Field(min_length=1)
    junit: str | None = None
    timeout: int = Field(default=3600, gt=0)


class RepoConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gates: dict[Literal["main", "parent"], Gate] = Field(default_factory=dict)
```

In `src/taskmanager/engine/config.py`, replace `ProjectConfig` in full with:

```python
class ProjectConfig(BaseModel):
    embeddings: EmbeddingsConfig = Field(default_factory=EmbeddingsConfig)
    worktree_dir: str = Field(default=".worktrees", min_length=1)
    lease_ttl: dict[str, int] | int = Field(default_factory=lambda: dict(LEASE_TTL_DEFAULTS))
    max_fix_rounds: FixRounds = Field(default_factory=FixRounds)
    max_merge_attempts: int = Field(default=3, ge=1)
    max_step_failures: int = Field(default=3, ge=1)
    condition_ttl: int = Field(default=300, ge=0)
    condition_timeout: int = Field(default=60, gt=0)
    red_target_decision_after: int = Field(default=3600, gt=0)
    repo_order: list[str] = Field(default_factory=list)
    repos: dict[str, RepoConfig] = Field(default_factory=dict)

    @field_validator("lease_ttl")
    @classmethod
    def _positive_per_known_action(cls, value: dict[str, int] | int) -> dict[str, int] | int:
        seconds = [value] if isinstance(value, int) else list(value.values())
        if any(s <= 0 for s in seconds):
            raise ValueError("every TTL must be greater than 0")
        unknown = sorted(set(value) - set(LEASE_TTL_DEFAULTS)) if isinstance(value, dict) else []
        if unknown:
            raise ValueError(
                f"unknown action {', '.join(unknown)} (actions: {', '.join(LEASE_TTL_DEFAULTS)})"
            )
        return value

    def lease_ttl_for(self, action: Action) -> int:
        if isinstance(self.lease_ttl, int):
            # A single number is the implementer's lease; every other action keeps its default.
            return self.lease_ttl if action == Action.IMPLEMENT else LEASE_TTL_DEFAULTS[action]
        return self.lease_ttl.get(action, LEASE_TTL_DEFAULTS[action])
```

In `src/taskmanager/engine/config.py`, replace `_flatten` in full with:

```python
def _flatten(nested: dict[str, Any], prefix: str = "") -> Iterator[tuple[str, Any]]:
    for key, value in nested.items():
        if isinstance(value, dict) and f"{prefix}{key}" not in _WHOLE:
            yield from _flatten(value, f"{prefix}{key}.")
        else:
            yield f"{prefix}{key}", value
```

In `src/taskmanager/engine/config.py`, replace `_typed` in full with:

```python
def _typed(key: str, raw: Any) -> Any:
    """`raw` validated as `key`'s type and dumped JSON-ready; every message names the valid keys."""
    _require_key(key)
    if key in _WHOLE and isinstance(raw, str):
        try:
            raw = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            raise ConfigError(f"{key}: not valid YAML (valid keys: {', '.join(KEYS)})") from exc
    try:
        model = ProjectConfig.model_validate(_nest({key: raw}))
    except ValidationError as exc:
        msg = str(exc.errors()[0]["msg"]).removeprefix("Value error, ")
        raise ConfigError(f"{key}: {msg} (valid keys: {', '.join(KEYS)})") from exc
    return _lookup(model.model_dump(mode="json"), key)
```

In `src/taskmanager/engine/config.py`, replace `ConfigStore.embeddings` with `ConfigStore.embeddings`, preceded by the new `project` and followed by the new `lease_ttl`:

```python
def project(self) -> ProjectConfig:
    values = {key: r.value for key, r in self.effective().items()}
    return ProjectConfig.model_validate(_nest(values))


def embeddings(self) -> EmbeddingsConfig:
    return self.project().embeddings


def lease_ttl(self, action: Action, flag: int | None = None) -> int:
    """`flag` is this claim's own TTL and wins outright; otherwise the action's stored TTL."""
    if flag is not None:
        return int(_typed("lease_ttl", flag))
    return ProjectConfig(lease_ttl=self.resolve("lease_ttl").value).lease_ttl_for(action)
```

Create `src/taskmanager/engine/conditions.py`:

```python
import os
import re
import shlex
import shutil
import signal
import subprocess
from itertools import dropwhile
from pathlib import Path

from taskmanager.core.models import Condition
from taskmanager.core.status import ConditionStage
from taskmanager.db.cache_repo import CacheRepository
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
        ttl: int,
        timeout: int,
    ) -> None:
        self.root = root
        self.node_repo = node_repo
        self.cache_repo = cache_repo
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
        self.cache_repo.put_condition(condition.node_id, condition.idx, condition.command, code)
        return code
```

In `src/taskmanager/cli/main.py`, replace:

```python
from taskmanager.core.naming import QualifiedPath
```

with:

```python
from taskmanager.core.naming import QualifiedPath
from taskmanager.core.status import Action
```

In `src/taskmanager/cli/main.py`, replace:

```python
        lease_ttl = config.resolve("lease_ttl", ttl).value
```

with:

```python
        lease_ttl = config.lease_ttl(Action.IMPLEMENT, ttl)
```

In `src/taskmanager/di/container.py`, replace:

```python
from taskmanager.engine.config import ConfigStore
```

with:

```python
from taskmanager.engine.conditions import ConditionRunner
from taskmanager.engine.config import ConfigStore
```

In `src/taskmanager/di/container.py`, add after `TaskManagerProvider.cache_repo`:

```python
    @provide(scope=Scope.APP)
    def condition_runner(
        self, node_repo: NodeRepository, cache_repo: CacheRepository
    ) -> ConditionRunner:
        config = ConfigStore(self.root).project()
        return ConditionRunner(
            self.root, node_repo, cache_repo, config.condition_ttl, config.condition_timeout
        )
```

In `src/taskmanager/di/container.py`, replace:

```python
    get_cache_repo = cache_repo
```

with:

```python
    get_cache_repo = cache_repo
    get_condition_runner = condition_runner
```

- [ ] **Step 4: Run the tests and the gates**

```bash
uv run --directory <worktree> pytest tests/unit/test_conditions.py tests/unit/test_config.py -q; echo $?
uv run --directory <worktree> pytest -q; echo $?
uv run --directory <worktree> ruff check; echo $?
uv run --directory <worktree> ruff format --check; echo $?
uv run --directory <worktree> mypy; echo $?
```

Expected: every command prints `0` last.

- [ ] **Step 5: Commit**

```bash
git -C <worktree> add src/taskmanager/engine/config.py src/taskmanager/engine/conditions.py src/taskmanager/cli/main.py src/taskmanager/di/container.py tests/unit/test_conditions.py tests/unit/test_config.py && git -C <worktree> commit -m "feat(config): add the lifecycle settings and run node conditions with a cached, timed-out shell"
```

### Task 12: Decisions on any node, option effects, FAILED and stranded decisions

**Spec:** §2.3 rule 7, §3.2 (the FAILED decision), §3.4 (reopen, stranded dependents), §4.2
**Files:**
- Modify: `src/taskmanager/engine/decisions.py` (rewritten: `DecisionOption.effect`, `DecisionData.subject`/`.custom_effect`, `chosen_effect`, `stranded_dependents`, `open_failed_decision`, `open_stranded_decision`, `apply_effect`)
- Modify: `src/taskmanager/engine/snapshot.py` (new `roll_up_ancestors`)
- Modify: `src/taskmanager/engine/operations.py` (`__init__` and `with_actor` take `job_repo`; new `busy`, `append_section`, `_refuse_unlinkable`; `_parse_option`, `add_decision`, `answer_decision`, `link_decision`)
- Modify: `src/taskmanager/di/container.py` (`operations` provider passes `job_repo`)
- Modify: `src/taskmanager/web/app.py` (`create_app` passes a `JobRepository` to `Operations`)
- Modify: `tests/unit/test_operations.py` (`test_add_decision_blocks_non_task_refuses` and `test_link_decision_add_non_task_refuses` become the decision-on-decision refusals: a decision now blocks any node that is not itself a decision)
- Test: `tests/unit/test_decision_effects.py`

**Interfaces:**
- Consumes: `DecisionEffect`, `DecisionStatus`, `Status`, `EXITS` (Task 1); `abandon`, `defer`, `reopen`, `LifecycleError` (Task 2); `rollup` (Task 3); `cycle_of`, `apply_cycle`, `stored_status`, `node_busy`, `CONTAINERS` (Task 10); `JobRepository` (Task 9)
- Produces: `open_failed_decision(ops, node_id, reason, evidence) -> str`, `open_stranded_decision(ops, node_id, status, dependents) -> str`, `apply_effect(ops, decision_id, effect) -> list[str]`, `stranded_dependents(ops, node_id)`, `chosen_effect(data)`; `roll_up_ancestors(node_repo, node_id)`; `Operations.busy`, `Operations.append_section`, `Operations.add_decision(..., subject=, custom_effect=)`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/test_decision_effects.py`:

```python
from collections.abc import Callable
from pathlib import Path

import pytest

from taskmanager.core.enums import NodeKind, NodeStatus, RelationType
from taskmanager.core.models import Job, Lease, Node, NodeRelation
from taskmanager.core.status import DecisionEffect, JobKind, Outcome, Status
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.job_repo import JobRepository
from taskmanager.db.ledger_repo import LedgerRepository
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.engine.decisions import (
    DecisionOption,
    open_failed_decision,
    open_stranded_decision,
    read_decision,
    stranded_dependents,
)
from taskmanager.engine.graph import GraphEngine
from taskmanager.engine.operations import OperationError, Operations
from taskmanager.engine.runtime import ExecutionCoordinator
from taskmanager.engine.verification import VerificationEngine


class Kit:
    def __init__(self, root: Path) -> None:
        db = DatabaseManager(root / ".taskmanager")
        db.init_all()
        self.nodes = NodeRepository(db)
        self.runtime = RuntimeRepository(db)
        self.jobs = JobRepository(db)
        self.ledger = LedgerRepository(db)
        graph = GraphEngine(self.nodes, self.runtime)
        self.ops = Operations(
            self.nodes,
            self.runtime,
            graph,
            ExecutionCoordinator(self.nodes, self.runtime, graph),
            self.ledger,
            VerificationEngine(root),
            actor="tester",
            job_repo=self.jobs,
        )

    def add(
        self,
        node_id: str,
        kind: NodeKind = NodeKind.TASK,
        parent: str | None = None,
        **fields: object,
    ) -> None:
        self.nodes.save_node(
            Node.model_validate({"id": node_id, "kind": kind, "title": node_id, **fields})
        )
        if parent is not None:
            self.nodes.add_relation(
                NodeRelation(
                    source_id=parent, target_id=node_id, relation_type=RelationType.CONTAINS
                )
            )

    def depend(self, dependent: str, dependency: str) -> None:
        self.nodes.add_relation(
            NodeRelation(
                source_id=dependent, target_id=dependency, relation_type=RelationType.DEPENDS_ON
            )
        )

    def node(self, node_id: str) -> Node:
        node = self.nodes.get_node(node_id)
        assert node is not None
        return node

    def section(self, node_id: str, key: str) -> str:
        section = self.nodes.get_section(node_id, key)
        return section.content if section else ""


@pytest.fixture
def kit(tmp_path: Path) -> Kit:
    return Kit(tmp_path)


@pytest.mark.parametrize("kind", [NodeKind.SPEC, NodeKind.PLAN, NodeKind.TASK])
def test_a_decision_blocks_any_node_that_is_not_a_decision(kit: Kit, kind: NodeKind) -> None:
    kit.add("N", kind, status=Status.READY)
    decision_id = kit.ops.add_decision("Which way?", slug="d1", blocks=["N"])
    assert kit.nodes.get_dependencies("N") == [decision_id]
    other = kit.ops.add_decision("Later?", slug="d2")
    kit.ops.link_decision(other, add=["N"])
    assert kit.nodes.get_dependencies("N") == [decision_id, other]


def test_a_decision_cannot_block_or_be_linked_to_another_decision(kit: Kit) -> None:
    first = kit.ops.add_decision("First?", slug="d1")
    before = len(kit.ledger.list_events(limit=1000))
    with pytest.raises(OperationError) as refused:
        kit.ops.add_decision("Second?", slug="d2", blocks=[first])
    assert refused.value.status_code == 400
    assert kit.nodes.get_node("decision-d2") is None
    second = kit.ops.add_decision("Second?", slug="d3")
    with pytest.raises(OperationError) as refused:
        kit.ops.link_decision(second, add=[first])
    assert refused.value.status_code == 400
    assert len(kit.ledger.list_events(limit=1000)) == before + 1


def _live_lease(kit: Kit) -> None:
    kit.runtime.acquire_lease(
        Lease(task_id="T", agent_id="a", session_id="s", branch_name="tm/T"), []
    )


def _running_job(kit: Kit) -> None:
    kit.jobs.create(Job(kind=JobKind.LAND, node_id="T", repo="core", target="main"))


@pytest.mark.parametrize("arrange", [_live_lease, _running_job], ids=["lease", "job"])
def test_a_decision_is_not_linked_to_a_node_mid_step(
    kit: Kit, arrange: Callable[[Kit], None]
) -> None:
    kit.add("T", status=Status.MERGING, claimed_from=Status.REVIEWED)
    arrange(kit)
    with pytest.raises(OperationError) as refused:
        kit.ops.add_decision("Q?", slug="d1", blocks=["T"])
    assert refused.value.status_code == 409
    decision_id = kit.ops.add_decision("Q?", slug="d2")
    with pytest.raises(OperationError) as refused:
        kit.ops.link_decision(decision_id, add=["T"])
    assert refused.value.status_code == 409
    assert kit.nodes.get_dependencies("T") == []


@pytest.mark.parametrize(
    ("raw", "effect"),
    [
        ("a|Abandon it", DecisionEffect.NONE),
        ("a|Abandon it|why", DecisionEffect.NONE),
        ("a|Abandon it|why|abandon", DecisionEffect.ABANDON),
        ("r|Reopen|why|reopen", DecisionEffect.REOPEN),
        ("x|Drop|why|drop_edge", DecisionEffect.DROP_EDGE),
    ],
)
def test_an_option_names_its_effect_in_a_fourth_field(
    kit: Kit, raw: str, effect: DecisionEffect
) -> None:
    decision_id = kit.ops.add_decision("Q?", slug="d1", options=[raw])
    assert read_decision(kit.node(decision_id)).options[0].effect == effect


def test_an_unknown_effect_is_refused(kit: Kit) -> None:
    with pytest.raises(OperationError, match="'explode' is not an effect") as refused:
        kit.ops.add_decision("Q?", slug="d1", options=["a|A|why|explode"])
    assert refused.value.status_code == 400
    assert kit.nodes.get_node("decision-d1") is None


@pytest.mark.parametrize(
    ("effect", "status", "section"),
    [
        ("abandon", Status.ABANDONED, "abandonment"),
        ("defer", Status.DEFERRED, "deferral"),
    ],
)
def test_answering_applies_the_options_effect_to_every_blocked_node(
    kit: Kit, effect: str, status: Status, section: str
) -> None:
    kit.add("P", NodeKind.PLAN, status=Status.READY)
    kit.add("T1", parent="P", status=Status.READY)
    kit.add("T2", parent="P", status=Status.REVIEWED, outcome=Outcome.REJECT)
    kit.add("T3", parent="P", status=Status.READY)
    decision_id = kit.ops.add_decision(
        "Drop these?", slug="d1", options=[f"go|Go|why|{effect}", "keep|Keep"], blocks=["T1", "T2"]
    )
    kit.ops.answer_decision(decision_id, option="go", text="scope cut", by="owner")
    assert (kit.node("T1").status, kit.node("T2").status, kit.node("T3").status) == (
        status,
        status,
        Status.READY,
    )
    assert kit.section("T1", section) == f"{decision_id} answered Go: scope cut"
    event = kit.ledger.list_events(target_id=decision_id, limit=1)[0]
    assert event.payload == {"option": "go", "effect": effect, "affected": ["T1", "T2"]}


def test_an_option_with_no_effect_changes_nothing(kit: Kit) -> None:
    kit.add("T", status=Status.READY)
    decision_id = kit.ops.add_decision("Q?", slug="d1", options=["k|Keep"], blocks=["T"])
    kit.ops.answer_decision(decision_id, option="k")
    assert kit.node("T").status == Status.READY


def test_reopen_returns_a_failed_task_to_ready_with_a_clean_cycle(kit: Kit) -> None:
    kit.add(
        "T",
        status=Status.FAILED,
        outcome=Outcome.REJECT,
        fix_for=Outcome.REJECT,
        verdict="still leaks the session",
        review_cycles=3,
        merge_attempts=1,
        step_failures=2,
        branch="tm/T",
    )
    decision_id = open_failed_decision(kit.ops, "T", "review rejected 3 times", "leaks")
    kit.ops.answer_decision(decision_id, option="investigate", text="split the session fix out")
    node = kit.node("T")
    assert node.status == Status.READY
    assert (node.outcome, node.fix_for, node.verdict) == (None, None, None)
    assert (node.review_cycles, node.merge_attempts, node.step_failures) == (0, 0, 0)
    assert node.branch == "tm/T"
    assert kit.section("T", "reopen") == (
        f"{decision_id} answered Investigate and reopen it: split the session fix out"
    )


def test_a_custom_answer_to_a_failed_decision_reopens_with_the_answer_as_its_note(
    kit: Kit,
) -> None:
    kit.add("T", status=Status.FAILED)
    decision_id = open_failed_decision(kit.ops, "T", "3 step failures", "")
    kit.ops.answer_decision(decision_id, text="retry once the runner is back")
    assert kit.node("T").status == Status.READY
    assert "retry once the runner is back" in kit.section("T", "reopen")


def test_reopening_a_container_whose_children_all_landed_returns_it_to_implemented(
    kit: Kit,
) -> None:
    kit.add("P", NodeKind.PLAN, status=Status.FAILED)
    kit.add("T1", parent="P", status=Status.COMPLETED)
    kit.add("T2", parent="P", status=Status.ABANDONED)
    decision_id = open_failed_decision(kit.ops, "P", "review rejected 4 times", "")
    kit.ops.answer_decision(decision_id, option="investigate")
    assert kit.node("P").status == Status.IMPLEMENTED


def test_a_failed_decision_blocks_its_node_and_says_why(kit: Kit) -> None:
    kit.add("T", status=Status.FAILED)
    decision_id = open_failed_decision(kit.ops, "T", "merge failed 3 times", "gate: 2 failed")
    decision = kit.node(decision_id)
    data = read_decision(decision)
    assert decision.title == "T failed: abandon, or investigate?"
    assert [(o.key, o.effect) for o in data.options] == [
        ("abandon", DecisionEffect.ABANDON),
        ("investigate", DecisionEffect.REOPEN),
    ]
    assert (data.subject, data.raised_by, data.custom_effect) == ("T", "T", DecisionEffect.REOPEN)
    assert kit.nodes.get_dependencies("T") == [decision_id]
    assert kit.section(decision_id, "context") == "merge failed 3 times\n\ngate: 2 failed"


def test_a_failed_node_opens_no_stranded_decision_until_it_is_abandoned(kit: Kit) -> None:
    kit.add("Y", status=Status.FAILED)
    kit.add("X1", status=Status.READY)
    kit.add("X2", status=Status.READY)
    kit.depend("X1", "Y")
    kit.depend("X2", "Y")
    failed = open_failed_decision(kit.ops, "Y", "review rejected", "")
    assert kit.nodes.get_blocked_by(failed) == ["Y"]
    kit.ops.answer_decision(failed, option="abandon")
    stranded = [d for d in kit.nodes.get_dependencies("X1") if d != "Y"]
    assert len(stranded) == 1
    decision = kit.node(stranded[0])
    assert decision.title == "Y was ABANDONED: drop the edge, defer, or abandon the dependents?"
    assert kit.nodes.get_blocked_by(stranded[0]) == ["X1", "X2"]


def test_dropping_the_edge_frees_every_stranded_dependent(kit: Kit) -> None:
    kit.add("Y", status=Status.DEFERRED)
    kit.add("X1", status=Status.READY)
    kit.add("X2", status=Status.READY)
    kit.depend("X1", "Y")
    kit.depend("X2", "Y")
    decision_id = open_stranded_decision(kit.ops, "Y", Status.DEFERRED, ["X1", "X2"])
    kit.ops.answer_decision(decision_id, option="drop_edge")
    assert kit.nodes.get_dependencies("X1") == [decision_id]
    assert kit.nodes.get_dependencies("X2") == [decision_id]
    assert (kit.node("X1").status, kit.node("X2").status) == (Status.READY, Status.READY)


def test_only_dependents_with_work_ahead_are_stranded(kit: Kit) -> None:
    kit.add("Y", status=Status.DEFERRED)
    for node_id, status in [
        ("READY", Status.READY),
        ("REVIEWED", Status.REVIEWED),
        ("FAILED", Status.FAILED),
        ("DONE", Status.COMPLETED),
        ("GONE", Status.ABANDONED),
        ("LATER", Status.DEFERRED),
    ]:
        kit.add(node_id, status=status)
        kit.depend(node_id, "Y")
    assert stranded_dependents(kit.ops, "Y") == ["READY", "REVIEWED", "FAILED"]


def test_abandoning_a_plans_last_task_abandons_the_plan_and_strands_its_dependents(
    kit: Kit,
) -> None:
    kit.add("P", NodeKind.PLAN, status=Status.READY)
    kit.add("T", parent="P", status=Status.READY)
    kit.add("X", status=Status.READY)
    kit.depend("X", "P")
    decision_id = kit.ops.add_decision(
        "Drop T?", slug="d1", options=["y|Yes|why|abandon"], blocks=["T"]
    )
    kit.ops.answer_decision(decision_id, option="y")
    assert kit.node("P").status == Status.ABANDONED
    stranded = [d for d in kit.nodes.get_dependencies("X") if d != "P"]
    assert len(stranded) == 1
    assert kit.node(stranded[0]).title.startswith("P was ABANDONED")


@pytest.mark.parametrize(
    ("effect", "status"),
    [
        ("abandon", Status.COMPLETED),
        ("defer", Status.COMPLETED),
        ("reopen", Status.READY),
        ("abandon", Status.SUPERSEDED),
    ],
)
def test_an_effect_the_node_cannot_take_refuses_the_whole_answer(
    kit: Kit, effect: str, status: Status
) -> None:
    kit.add("T", status=status)
    kit.add("U", status=Status.FAILED)
    decision_id = kit.ops.add_decision(
        "Q?", slug="d1", options=[f"go|Go|why|{effect}"], blocks=["U", "T"]
    )
    before = len(kit.ledger.list_events(limit=1000))
    with pytest.raises(OperationError) as refused:
        kit.ops.answer_decision(decision_id, option="go")
    assert refused.value.status_code == 409
    assert kit.node(decision_id).status == NodeStatus.NOT_STARTED
    assert (kit.node("T").status, kit.node("U").status) == (status, Status.FAILED)
    assert kit.nodes.get_all_sections("U") == []
    assert len(kit.ledger.list_events(limit=1000)) == before


def test_an_effect_waits_for_a_step_that_started_after_the_link(kit: Kit) -> None:
    kit.add("T", status=Status.READY)
    decision_id = kit.ops.add_decision("Q?", slug="d1", options=["go|Go|why|defer"], blocks=["T"])
    _live_lease(kit)
    with pytest.raises(OperationError, match="while a step runs") as refused:
        kit.ops.answer_decision(decision_id, option="go")
    assert refused.value.status_code == 409
    assert kit.node("T").status == Status.READY
    assert kit.node(decision_id).status == NodeStatus.NOT_STARTED


def test_a_reopen_waits_while_another_decision_is_open_on_the_node(kit: Kit) -> None:
    kit.add("T", status=Status.FAILED)
    failed = open_failed_decision(kit.ops, "T", "3 step failures", "")
    kit.ops.add_decision("Budget?", slug="budget", blocks=["T"])
    with pytest.raises(OperationError, match="decision-budget is open"):
        kit.ops.answer_decision(failed, option="investigate")
    assert kit.node("T").status == Status.FAILED


def test_an_option_object_keeps_its_effect(kit: Kit) -> None:
    decision_id = kit.ops.add_decision(
        "Q?",
        slug="d1",
        options=[DecisionOption(key="a", label="A", effect=DecisionEffect.DEFER)],
    )
    assert read_decision(kit.node(decision_id)).options[0].effect == DecisionEffect.DEFER
```

In `tests/unit/test_operations.py`, replace `test_add_decision_blocks_non_task_refuses` in full with:

```python
def test_add_decision_refuses_to_block_another_decision(ops_setup: tuple) -> None:
    _node_repo, _runtime_repo, ledger_repo, ops = ops_setup
    first = ops.add_decision("Which way?", slug="d1")
    before = len(ledger_repo.list_events(limit=1000))
    with pytest.raises(OperationError) as exc:
        ops.add_decision("Then what?", slug="d2", blocks=[first])
    assert exc.value.status_code == 400
    assert len(ledger_repo.list_events(limit=1000)) == before
```

In `tests/unit/test_operations.py`, replace `test_link_decision_add_non_task_refuses` in full with:

```python
def test_link_decision_refuses_to_link_another_decision(ops_setup: tuple) -> None:
    _node_repo, _runtime_repo, _ledger_repo, ops = ops_setup
    first = ops.add_decision("Which way?", slug="d1")
    second = ops.add_decision("Then what?", slug="d2")
    with pytest.raises(OperationError) as exc:
        ops.link_decision(second, add=[first])
    assert exc.value.status_code == 400
```

- [ ] **Step 2: Run it and watch it fail**

```bash
uv run --directory <worktree> pytest tests/unit/test_decision_effects.py tests/unit/test_operations.py -q; echo $?
```

Expected: `ImportError: cannot import name 'open_failed_decision' from 'taskmanager.engine.decisions'` during collection, exit 2.

- [ ] **Step 3: Implement**

Decisions keep their stored `NOT_STARTED`/`COMPLETED`/`ABANDONED` values here; `stored_status` already reads them as `OPEN`/`ANSWERED`/`WITHDRAWN`. An effect runs inside the answering transaction and refuses the whole answer when any blocked node cannot take it, so a decision never leaves half its nodes ruled. The ledger entry is written after the commit, carrying the effect and the nodes it moved.

Replace the whole of `src/taskmanager/engine/decisions.py` with:

```python
from datetime import datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from taskmanager.core.enums import NodeKind, RelationType
from taskmanager.core.lifecycle import LifecycleError, abandon, defer, reopen
from taskmanager.core.models import Node
from taskmanager.core.status import EXITS, DecisionEffect, DecisionStatus, Status
from taskmanager.engine.snapshot import apply_cycle, cycle_of, roll_up_ancestors, stored_status

if TYPE_CHECKING:
    from taskmanager.engine.operations import Operations

# A decision's stored status is shown under its own names everywhere a human reads it -- the CLI
# table, `tm decision get`, the JSON/YAML rows.
DECISION_STATUS_LABELS: dict[str, str] = {
    "NOT_STARTED": "Open",
    "COMPLETED": "Answered",
    "ABANDONED": "Withdrawn",
}

# The section each effect appends its note to, beside the ledger entry.
_NOTE_SECTION = {
    DecisionEffect.ABANDON: "abandonment",
    DecisionEffect.DEFER: "deferral",
    DecisionEffect.REOPEN: "reopen",
}


class DecisionOption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    label: str
    description: str = ""
    recommended: bool = False
    effect: DecisionEffect = DecisionEffect.NONE


class DecisionAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    option: str | None = None
    text: str = ""
    rationale: str = ""
    answered_by: str
    answered_at: datetime


class DecisionData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    options: list[DecisionOption] = Field(default_factory=list)
    allow_custom: bool = True
    raised_by: str | None = None
    answer: DecisionAnswer | None = None
    withdrawn_reason: str = ""
    # The node the decision is about; `drop_edge` removes each blocked node's edge to it.
    subject: str | None = None
    # What a custom answer does, since it names no option to carry an effect.
    custom_effect: DecisionEffect = DecisionEffect.NONE


def read_decision(node: Node) -> DecisionData:
    raw: Any = node.frontmatter.get("decision") or {}
    return DecisionData.model_validate(raw)


def write_decision(node: Node, data: DecisionData) -> None:
    node.frontmatter["decision"] = data.model_dump(mode="json")


def chosen_effect(data: DecisionData) -> DecisionEffect:
    if data.answer is None:
        return DecisionEffect.NONE
    if data.answer.option is None:
        return data.custom_effect
    return next(o.effect for o in data.options if o.key == data.answer.option)


def _note(decision_id: str, data: DecisionData) -> str:
    answer = data.answer
    if answer is None:
        return decision_id
    label = next((o.label for o in data.options if o.key == answer.option), "a custom answer")
    said = answer.text or answer.rationale
    return f"{decision_id} answered {label}" + (f": {said}" if said else "")


def stranded_dependents(ops: Operations, node_id: str) -> list[str]:
    """Nodes whose own edge points at `node_id` and that still have work ahead of them."""
    stranded = []
    for dependent_id in ops.node_repo.get_blocked_by(node_id):
        dependent = ops.node_repo.get_node(dependent_id)
        if dependent is None or dependent.kind == NodeKind.DECISION:
            continue
        status = stored_status(dependent)
        if status != Status.COMPLETED and status not in EXITS:
            stranded.append(dependent_id)
    return stranded


def open_failed_decision(ops: Operations, node_id: str, reason: str, evidence: str) -> str:
    return ops.add_decision(
        f"{node_id} failed: abandon, or investigate?",
        context=f"{reason}\n\n{evidence}".strip(),
        options=[
            DecisionOption(
                key="abandon",
                label="Abandon it",
                description="The work is dropped; anything depending on it gets its own ruling.",
                effect=DecisionEffect.ABANDON,
            ),
            DecisionOption(
                key="investigate",
                label="Investigate and reopen it",
                description="It goes back to the start of its cycle with this answer as its note.",
                effect=DecisionEffect.REOPEN,
            ),
        ],
        raised_by=node_id,
        blocks=[node_id],
        subject=node_id,
        custom_effect=DecisionEffect.REOPEN,
    )


def open_stranded_decision(
    ops: Operations, node_id: str, status: Status, dependents: list[str]
) -> str:
    return ops.add_decision(
        f"{node_id} was {status}: drop the edge, defer, or abandon the dependents?",
        options=[
            DecisionOption(
                key="drop_edge",
                label="Drop the edge",
                description=f"Each dependent stops waiting on {node_id}.",
                effect=DecisionEffect.DROP_EDGE,
            ),
            DecisionOption(
                key="defer",
                label="Defer the dependents",
                effect=DecisionEffect.DEFER,
            ),
            DecisionOption(
                key="abandon",
                label="Abandon the dependents",
                effect=DecisionEffect.ABANDON,
            ),
        ],
        raised_by=node_id,
        blocks=dependents,
        subject=node_id,
    )


def _children_all_completed(ops: Operations, node_id: str) -> bool:
    counted = [
        s
        for c in ops.node_repo.get_children(node_id)
        if (child := ops.node_repo.get_node(c)) is not None
        and (s := stored_status(child)) not in EXITS
    ]
    return bool(counted) and all(s == Status.COMPLETED for s in counted)


def _open_decisions_on(ops: Operations, node_id: str) -> list[str]:
    return [
        d
        for d in ops.node_repo.get_dependencies(node_id)
        if (dep := ops.node_repo.get_node(d)) is not None
        and dep.kind == NodeKind.DECISION
        and stored_status(dep) == DecisionStatus.OPEN
    ]


def apply_effect(ops: Operations, decision_id: str, effect: DecisionEffect) -> list[str]:
    """Apply an answered decision's effect to every node it blocks, inside the caller's
    transaction, and return the nodes it changed. A node the effect cannot apply to refuses the
    whole answer, so no node is ever half-ruled."""
    # Operations imports this module, so importing it back at load time would be circular.
    from taskmanager.engine.operations import OperationError

    if effect == DecisionEffect.NONE:
        return []
    decision = ops.node_repo.get_node(decision_id)
    if decision is None:
        raise OperationError(f"decision '{decision_id}' not found", 404)
    data = read_decision(decision)
    blocked = ops.node_repo.get_blocked_by(decision_id)
    busy = [n for n in blocked if ops.busy(n)]
    if busy:
        raise OperationError(
            f"'{decision_id}' cannot {effect} {', '.join(busy)} while a step runs: "
            "wait for it to end, or stop it",
            409,
        )
    note = _note(decision_id, data)
    for node_id in blocked:
        if effect == DecisionEffect.DROP_EDGE:
            if data.subject is None:
                raise OperationError(f"'{decision_id}' names no subject to drop an edge to", 400)
            ops.node_repo.remove_relation(node_id, data.subject, RelationType.DEPENDS_ON)
            continue
        node = ops.node_repo.get_node(node_id)
        if node is None:
            continue
        if effect == DecisionEffect.REOPEN and (waiting := _open_decisions_on(ops, node_id)):
            raise OperationError(
                f"'{node_id}' cannot reopen while {', '.join(waiting)} is open", 409
            )
        try:
            if effect == DecisionEffect.ABANDON:
                cycle = abandon(cycle_of(node))
            elif effect == DecisionEffect.DEFER:
                cycle = defer(cycle_of(node))
            else:
                cycle = reopen(cycle_of(node), _children_all_completed(ops, node_id))
        except LifecycleError as exc:
            raise OperationError(
                f"'{decision_id}' cannot {effect} '{node_id}': {exc}", 409
            ) from exc
        updated = apply_cycle(node, cycle)
        if effect == DecisionEffect.REOPEN:
            updated.verdict = None
        ops.node_repo.save_node(updated)
        ops.append_section(node_id, _NOTE_SECTION[effect], note)
        moved = [(node_id, cycle.status), *roll_up_ancestors(ops.node_repo, node_id)]
        for moved_id, status in moved:
            if status in (Status.ABANDONED, Status.DEFERRED) and (
                dependents := stranded_dependents(ops, moved_id)
            ):
                open_stranded_decision(ops, moved_id, status, dependents)
    return blocked
```

In `src/taskmanager/engine/snapshot.py`, replace:

```python
from datetime import UTC, datetime
```

with:

```python
from dataclasses import replace
from datetime import UTC, datetime
```

In `src/taskmanager/engine/snapshot.py`, replace:

```python
from taskmanager.core.models import Node
```

with:

```python
from taskmanager.core.models import Node
from taskmanager.core.rollup import rollup
```

In `src/taskmanager/engine/snapshot.py`, add after `node_busy`:

```python
def roll_up_ancestors(node_repo: NodeRepository, node_id: str) -> list[tuple[str, Status]]:
    """Re-derive each ancestor container's status after a change under it, in the caller's
    transaction, and return every container that moved with its new status. The completion of a
    container whose branches match their base is the landing's to decide, not this."""
    moved: list[tuple[str, Status]] = []
    seen = {node_id}
    parents = node_repo.get_parent_ids(node_id)
    while parents and parents[0] not in seen:
        parent = node_repo.get_node(parents[0])
        if parent is None or parent.kind not in CONTAINERS:
            break
        seen.add(parent.id)
        children = [node_repo.get_node(c) for c in node_repo.get_children(parent.id)]
        statuses = [
            s for c in children if c is not None and isinstance(s := stored_status(c), Status)
        ]
        current = cycle_of(parent)
        derived = rollup(current.status, statuses)
        if derived != current.status:
            node_repo.save_node(apply_cycle(parent, replace(current, status=derived)))
            moved.append((parent.id, derived))
        parents = node_repo.get_parent_ids(parent.id)
    return moved
```

In `src/taskmanager/engine/operations.py`, replace:

```python
from taskmanager.db.ledger_repo import LedgerRepository
```

with:

```python
from taskmanager.core.status import DecisionEffect
from taskmanager.db.job_repo import JobRepository
from taskmanager.db.ledger_repo import LedgerRepository
```

In `src/taskmanager/engine/operations.py`, replace:

```python
(DecisionOption,)
(read_decision,)
```

with:

```python
(DecisionOption,)
(apply_effect,)
(chosen_effect,)
(read_decision,)
```

In `src/taskmanager/engine/operations.py`, replace:

```python
from taskmanager.engine.runtime import ExecutionCoordinator
```

with:

```python
from taskmanager.engine.runtime import ExecutionCoordinator
from taskmanager.engine.snapshot import node_busy
```

In `src/taskmanager/engine/operations.py`, replace `Operations.__init__` in full with:

```python
    def __init__(
        self,
        node_repo: NodeRepository,
        runtime_repo: RuntimeRepository,
        graph: GraphEngine,
        coordinator: ExecutionCoordinator,
        ledger_repo: LedgerRepository,
        verification_engine: VerificationEngine,
        actor: str = "cli",
        job_repo: JobRepository | None = None,
    ) -> None:
        self.node_repo = node_repo
        self.runtime_repo = runtime_repo
        self.graph = graph
        self.coordinator = coordinator
        self.ledger_repo = ledger_repo
        self.verification_engine = verification_engine
        self.actor = actor
        self.job_repo = job_repo
```

In `src/taskmanager/engine/operations.py`, replace `Operations.with_actor` with `with_actor`, followed by the new `busy`:

```python
def with_actor(self, actor: str) -> Operations:
    return Operations(
        self.node_repo,
        self.runtime_repo,
        self.graph,
        self.coordinator,
        self.ledger_repo,
        self.verification_engine,
        actor=actor,
        job_repo=self.job_repo,
    )


def busy(self, node_id: str) -> bool:
    return node_busy(self.runtime_repo, self.job_repo, node_id)
```

In `src/taskmanager/engine/operations.py`, add after `Operations._write_section`:

```python
    def append_section(self, node_id: str, section_key: str, text: str) -> None:
        """`text` added as a new paragraph at the end of the section, which is created if absent.
        Joins the caller's transaction and writes no ledger entry of its own."""
        existing = self.node_repo.get_section(node_id, section_key)
        content = f"{existing.content}\n\n{text}" if existing and existing.content else text
        self._write_section(node_id, section_key, content, existing.header if existing else None)
```

In `src/taskmanager/engine/operations.py`, replace `Operations._parse_option` with `_parse_option`, followed by the new `_refuse_unlinkable`:

```python
@staticmethod
def _parse_option(raw: str | DecisionOption) -> DecisionOption:
    if isinstance(raw, DecisionOption):
        return raw.model_copy()
    parts = raw.split("|")
    key = parts[0].strip() if parts else ""
    label = parts[1].strip() if len(parts) > 1 else ""
    if not key or not label:
        raise OperationError(f"--option takes 'key|Label|description|effect', got '{raw}'", 400)
    description = parts[2].strip() if len(parts) > 2 else ""
    named = parts[3].strip() if len(parts) > 3 else DecisionEffect.NONE.value
    try:
        effect = DecisionEffect(named)
    except ValueError as exc:
        raise OperationError(
            f"'{named}' is not an effect (effects: {', '.join(DecisionEffect)})", 400
        ) from exc
    return DecisionOption(key=key, label=label, description=description, effect=effect)


def _refuse_unlinkable(self, node_id: str, decision_id: str) -> None:
    node = self.node_repo.get_node(node_id)
    if node is None:
        raise OperationError(f"node '{node_id}' not found", 404)
    if node.kind == NodeKind.DECISION:
        raise OperationError(f"'{node_id}' is a decision; a decision waits on nothing", 400)
    if self.busy(node_id):
        raise OperationError(
            f"'{node_id}' has a step running: link the decision once it ends, or stop it", 409
        )
    if self.graph.would_cause_cycle(node_id, decision_id):
        raise OperationError(f"'{node_id}' -> '{decision_id}' would make a cycle", 409)
```

In `src/taskmanager/engine/operations.py`, replace `Operations.add_decision` in full with:

```python
def add_decision(
    self,
    question: str,
    slug: str | None = None,
    priority: int = 50,
    context: str | None = None,
    options: list[str] | list[DecisionOption] | None = None,
    recommend: str | None = None,
    allow_custom: bool = True,
    raised_by: str | None = None,
    blocks: list[str] | None = None,
    subject: str | None = None,
    custom_effect: DecisionEffect = DecisionEffect.NONE,
) -> str:
    if slug:
        decision_id = f"decision-{slug}"
        if self.node_repo.get_node(decision_id) is not None:
            raise OperationError(f"'{decision_id}' already exists", 409)
    else:
        existing = {n.id for n in self.node_repo.list_nodes(kind=NodeKind.DECISION)}
        counter = 1
        while f"decision-D{counter}" in existing:
            counter += 1
        decision_id = f"decision-D{counter}"

    parsed_options = [self._parse_option(o) for o in options or []]
    keys = [o.key for o in parsed_options]
    if len(keys) != len(set(keys)):
        raise OperationError("option keys must be unique", 400)
    if recommend is not None and recommend not in keys:
        raise OperationError(f"'{recommend}' is not one of the option keys", 400)
    for opt in parsed_options:
        opt.recommended = opt.key == recommend

    blocked_tasks = blocks or []
    for task_id in blocked_tasks:
        self._refuse_unlinkable(task_id, decision_id)
    for named in (raised_by, subject):
        if named is not None and self.node_repo.get_node(named) is None:
            raise OperationError(f"'{named}' not found", 404)

    data = DecisionData(
        options=parsed_options,
        allow_custom=allow_custom,
        raised_by=raised_by,
        subject=subject,
        custom_effect=custom_effect,
    )
    node = Node(
        id=decision_id,
        kind=NodeKind.DECISION,
        title=question,
        priority=priority,
        frontmatter={"decision": data.model_dump(mode="json")},
    )
    with self.node_repo.transaction():
        self.node_repo.save_node(node)
        if context:
            self.node_repo.save_section(
                NodeSection(
                    node_id=decision_id,
                    section_key="context",
                    ordinal=1,
                    header="## Context",
                    content=context,
                )
            )
        for task_id in blocked_tasks:
            self.node_repo.add_relation(
                NodeRelation(
                    source_id=task_id,
                    target_id=decision_id,
                    relation_type=RelationType.DEPENDS_ON,
                )
            )
    self._ledger(LedgerCommand.DECISION_ADD, target_id=decision_id, payload={"question": question})
    return decision_id
```

In `src/taskmanager/engine/operations.py`, replace `Operations.answer_decision` in full with:

```python
    def answer_decision(
        self,
        decision_id: str,
        option: str | None = None,
        text: str = "",
        rationale: str = "",
        by: str = "cli",
    ) -> None:
        node = self._get_decision(decision_id)
        if node.status != NodeStatus.NOT_STARTED:
            raise OperationError(f"decision '{decision_id}' is not open; reopen it first", 409)
        data = read_decision(node)
        if option is not None:
            if option not in {o.key for o in data.options}:
                raise OperationError(f"'{option}' is not an option of '{decision_id}'", 400)
        elif not data.allow_custom:
            raise OperationError(f"decision '{decision_id}' does not allow a custom answer", 400)
        elif not text:
            raise OperationError("give --option or --custom", 400)

        data.answer = DecisionAnswer(
            option=option,
            text=text,
            rationale=rationale,
            answered_by=by,
            answered_at=datetime.now(tz=UTC),
        )
        write_decision(node, data)
        node.status = NodeStatus.COMPLETED
        node.updated_at = datetime.now(tz=UTC)
        effect = chosen_effect(data)
        with self.node_repo.transaction():
            self.node_repo.save_node(node)
            affected = apply_effect(self, decision_id, effect)
        self._ledger(
            LedgerCommand.DECISION_ANSWER,
            target_id=decision_id,
            payload={"option": option, "effect": effect.value, "affected": affected},
        )
```

In `src/taskmanager/engine/operations.py`, replace `Operations.link_decision` in full with:

```python
    def link_decision(
        self, decision_id: str, add: list[str] | None = None, remove: list[str] | None = None
    ) -> None:
        self._get_decision(decision_id)
        add_ids = add or []
        remove_ids = remove or []
        for task_id in add_ids:
            self._refuse_unlinkable(task_id, decision_id)
        for task_id in remove_ids:
            if decision_id not in self.node_repo.get_dependencies(task_id):
                raise OperationError(f"'{task_id}' does not wait on '{decision_id}'", 409)
        with self.node_repo.transaction():
            for task_id in add_ids:
                self.node_repo.add_relation(
                    NodeRelation(
                        source_id=task_id,
                        target_id=decision_id,
                        relation_type=RelationType.DEPENDS_ON,
                    )
                )
            for task_id in remove_ids:
                self.node_repo.remove_relation(task_id, decision_id, RelationType.DEPENDS_ON)
        self._ledger(
            LedgerCommand.DECISION_LINK,
            target_id=decision_id,
            payload={"add": add_ids, "remove": remove_ids},
        )
```

In `src/taskmanager/di/container.py`, replace `TaskManagerProvider.operations` in full with:

```python
    @provide(scope=Scope.APP)
    def operations(
        self,
        node_repo: NodeRepository,
        runtime_repo: RuntimeRepository,
        graph_engine: GraphEngine,
        coordinator: ExecutionCoordinator,
        ledger_repo: LedgerRepository,
        verification_engine: VerificationEngine,
        job_repo: JobRepository,
    ) -> Operations:
        return Operations(
            node_repo,
            runtime_repo,
            graph_engine,
            coordinator,
            ledger_repo,
            verification_engine,
            job_repo=job_repo,
        )
```

In `src/taskmanager/web/app.py`, replace:

```python
from taskmanager.db.ledger_repo import LedgerRepository
```

with:

```python
from taskmanager.db.job_repo import JobRepository
from taskmanager.db.ledger_repo import LedgerRepository
```

In `src/taskmanager/web/app.py`, replace:

```python
        verification_engine,
        actor="web",
    )
```

with:

```python
        verification_engine,
        actor="web",
        job_repo=JobRepository(db_mgr),
    )
```

- [ ] **Step 4: Run the tests and the gates**

```bash
uv run --directory <worktree> pytest tests/unit/test_decision_effects.py tests/unit/test_operations.py -q; echo $?
uv run --directory <worktree> pytest -q; echo $?
uv run --directory <worktree> ruff check; echo $?
uv run --directory <worktree> ruff format --check; echo $?
uv run --directory <worktree> mypy; echo $?
```

Expected: every command prints `0` last.

- [ ] **Step 5: Commit**

```bash
git -C <worktree> add src/taskmanager/engine/decisions.py src/taskmanager/engine/snapshot.py src/taskmanager/engine/operations.py src/taskmanager/di/container.py src/taskmanager/web/app.py tests/unit/test_decision_effects.py tests/unit/test_operations.py && git -C <worktree> commit -m "feat(decisions): let a decision block any node and apply its answer's effect in one transaction"
```

---

### Task 13: Claims engine and model routing

**Spec:** §2.1, §2.2, §3.1–§3.4, §4.1, §4.2, §4.5, §5.1–§5.5, §6.1

**Files:**
- Create: `src/taskmanager/engine/routing.py`
- Create: `src/taskmanager/engine/claims.py`
- Modify: `src/taskmanager/engine/git.py` (adds module functions `_git`, `_run`, `rev_parse`, `is_ancestor`, `diff_quiet`, `fetch`, `ensure_branch`, `rename_branch`; `GitManager` unchanged)
- Create: `tests/unit/lifecycle_estate.py` (shared helpers: a `.taskmanager` beside git clones of bare origins)
- Test: `tests/unit/test_routing.py`
- Test: `tests/unit/test_claims.py`

No existing test changes: `ExecutionCoordinator` (`engine/runtime.py`) and `wave.discover_batch` stay in place, untouched, until the CLI switches to `Claims` (Task 18) and Task 21 deletes them.

**Interfaces:**
- Consumes: `Status`, `IN_STEP`, `EXITS`, `Action`, `Event`, `JobKind`, `JobState` (with `EXPIRED`), `ConditionStage`, `DecisionStatus`, `Merge`, `Outcome` (Task 1); `Cycle`, `Caps`, `LifecycleError`, `next_action`, `claim`, `advance` (accepting `RELEASE`/`EXPIRED` on a stable status), `fix_round` (1-based: `review_cycles` for a fix answering `REJECT`, 0 for a `MERGE_FAILED` fix), `reopen`, `defer`, `abandon`, `reset` (Task 2); `rollup` (Task 3); `satisfied` (Task 5); `Snapshot`, `SnapNode` (Task 6); `validate`, `Refusal`, `BranchFacts` (Task 7); `Node` columns, `Condition`, `Job` (`id=""` until created), `Lease.action`/`review_hash`/`model`/nullable `ttl_seconds`, `NodeRepository.get_conditions`/`add_condition` (Task 8); `RuntimeRepository.claim`/`get_lease`/`heartbeat`/`release_lease`/`get_conflicting_tasks`/`sweep_expired_leases`/`park`/`take_over(node_id, agent, session, ttl, model)`, `JobRepository.create` (assigns the id)/`get`/`for_node`/`update`/`release_branch`, `CacheRepository` (Task 9); `SnapshotBuilder.build`/`cycle` (Task 10); `ConditionRunner.unmet`, `is_executable`, `ProjectConfig` keys, `ConfigStore.project()` (Task 11); `open_failed_decision`, `open_stranded_decision`, `Operations.add_decision` on any non-decision node (Task 12).
- Produces: `routing.FAMILIES`, `routing.STRONG`, `routing.family(model_id) -> str | None`, `routing.model_for(action, node, fix_round) -> str`; `claims.ClaimResult` (with `worktrees: dict[str, str]`, repository to worktree), `claims.Blocker`, `claims.DecisionSpec`, `claims.LandingJobs` (protocol Landing satisfies), `claims.Claims` with the contract verbs (`start(…, worktree_dir=None)`; `complete`, `review`, `release` taking `agent=None`) plus `Claims.open(root, config=None)`, `node`, `is_container`, `branch_of`, `target_of`, `target_ref`, `repos_of`, `blocked_reason`, `next_step`, `verify`, `note`, `ttl_for`, attributes `root`, `config`, `ops`, `nodes`, `runtime`, `jobs`, `snapshots`, `conditions`, `landing`; every lease a claim or handover writes carries the routed family in `Lease.model`; `git.rev_parse`, `git.is_ancestor`, `git.diff_quiet`, `git.fetch`, `git.ensure_branch`, `git.rename_branch`; test helpers `git`, `commit`, `make_repo`, `on_branch`, `push_main`, `branch_at`, `make_estate`, `add`, `stored`, `section`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/lifecycle_estate.py`:

```python
"""A throwaway estate for engine tests: a `.taskmanager` beside git clones of bare origins.

Nothing here touches the network: every `origin` is a bare repository under the test's tmp_path.
"""

import subprocess
from pathlib import Path

import yaml

from taskmanager.core.enums import NodeKind, RelationType
from taskmanager.core.models import Node, NodeRelation
from taskmanager.core.status import Merge, Status
from taskmanager.db.connection import DatabaseManager
from taskmanager.engine.claims import Claims
from taskmanager.engine.config import ProjectConfig


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def commit(worktree: Path, path: str, content: str | None, message: str) -> str:
    """Writes `content` to `path` (None deletes it) and commits everything in the worktree."""
    target = worktree / path
    if content is None:
        target.unlink()
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    git(worktree, "add", "-A")
    git(worktree, "commit", "-q", "-m", message)
    return git(worktree, "rev-parse", "HEAD")


def make_repo(root: Path, name: str) -> Path:
    origin = root.parent / "origins" / f"{name}.git"
    origin.parent.mkdir(parents=True, exist_ok=True)
    git(origin.parent, "init", "-q", "--bare", "-b", "main", str(origin))
    clone = root / name
    git(root, "clone", "-q", str(origin), str(clone))
    git(clone, "symbolic-ref", "HEAD", "refs/heads/main")
    for key, value in (
        ("user.email", "tm@example.com"),
        ("user.name", "tm"),
        ("commit.gpgsign", "false"),
        ("tag.gpgsign", "false"),
    ):
        git(clone, "config", key, value)
    commit(clone, "README.md", "readme\n", "init")
    git(clone, "push", "-q", "origin", "HEAD:main")
    git(clone, "fetch", "-q", "origin")
    return clone


def _scratch(repo: Path, label: str) -> Path:
    return repo.parent.parent / "scratch" / f"{repo.name}-{label.replace('/', '-')}"


def on_branch(
    repo: Path, branch: str, path: str, content: str | None, base: str = "origin/main"
) -> str:
    """Commits to `branch`, cutting it from `base` when it does not exist yet, without checking
    it out in the clone."""
    scratch = _scratch(repo, branch)
    exists = (
        subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )
    if exists:
        git(repo, "worktree", "add", "-q", str(scratch), branch)
    else:
        git(repo, "worktree", "add", "-q", "--no-track", "-b", branch, str(scratch), base)
    sha = commit(scratch, path, content, f"{branch}: {path}")
    git(repo, "worktree", "remove", "--force", str(scratch))
    return sha


def push_main(repo: Path, path: str, content: str | None) -> str:
    """Moves `origin/main` by one commit, as another agent's landing would."""
    git(repo, "fetch", "-q", "origin")
    scratch = _scratch(repo, "main")
    git(repo, "worktree", "add", "-q", "--detach", str(scratch), "origin/main")
    sha = commit(scratch, path, content, f"main: {path}")
    git(scratch, "push", "-q", "origin", "HEAD:main")
    git(repo, "worktree", "remove", "--force", str(scratch))
    git(repo, "fetch", "-q", "origin")
    return sha


def branch_at(repo: Path, branch: str, ref: str = "origin/main") -> None:
    git(repo, "branch", "-q", "--no-track", branch, ref)


def make_estate(
    tmp_path: Path, repos: tuple[str, ...] = ("api",), config: ProjectConfig | None = None
) -> Claims:
    root = tmp_path / "estate"
    root.mkdir()
    for name in repos:
        make_repo(root, name)
    cfg = config or ProjectConfig()
    tm_dir = root / ".taskmanager"
    tm_dir.mkdir()
    (tm_dir / "config.yaml").write_text(
        yaml.safe_dump(cfg.model_dump(mode="json", exclude_defaults=True))
    )
    DatabaseManager(tm_dir).init_all()
    return Claims.open(root, cfg)


def add(
    claims: Claims,
    node_id: str,
    kind: NodeKind = NodeKind.TASK,
    *,
    parent: str | None = None,
    repo: str | None = "api",
    merge: Merge = Merge.MAIN,
    review: bool | None = None,
    fix: bool | None = None,
    status: Status = Status.READY,
    models: list[str] | None = None,
    files: list[str] | None = None,
    depends: tuple[str, ...] = (),
    **columns: object,
) -> None:
    """Saves a node straight through the repository: these tests exercise the claims engine,
    not the write-time validation in front of it."""
    container = kind in (NodeKind.PLAN, NodeKind.SPEC)
    reviewed = (not container) if review is None else review
    claims.nodes.save_node(
        Node(
            id=node_id,
            kind=kind,
            title=node_id,
            status=status,
            target_repo=None if container else repo,
            review=reviewed,
            fix=reviewed if fix is None else fix,
            merge=merge,
            acceptable_models=models or [],
            frontmatter={"declared_files": files} if files else {},
            **columns,
        )
    )
    if parent is not None:
        claims.nodes.add_relation(
            NodeRelation(source_id=parent, target_id=node_id, relation_type=RelationType.CONTAINS)
        )
    for dep in depends:
        claims.nodes.add_relation(
            NodeRelation(source_id=node_id, target_id=dep, relation_type=RelationType.DEPENDS_ON)
        )


def stored(claims: Claims, node_id: str) -> Node:
    found = claims.nodes.get_node(node_id)
    assert found is not None
    return found


def section(claims: Claims, node_id: str, key: str) -> str:
    found = claims.nodes.get_section(node_id, key)
    return found.content if found else ""
```

`tests/unit/test_routing.py`:

```python
import pytest

from taskmanager.core.enums import NodeKind
from taskmanager.core.models import Node
from taskmanager.core.status import Action, Outcome
from taskmanager.engine.routing import family, model_for


@pytest.mark.parametrize(
    ("model_id", "expected"),
    [
        ("claude-haiku-4-5", "haiku"),
        ("claude-sonnet-4-5", "sonnet"),
        ("claude-opus-5-5[1m]", "opus"),
        ("Fable-1", "fable"),
        ("gemini-2.5-pro", None),
    ],
)
def test_a_model_id_names_its_family(model_id: str, expected: str | None) -> None:
    assert family(model_id) == expected


@pytest.mark.parametrize(
    ("action", "kind", "models", "review_models", "fix_for", "fix_round", "expected"),
    [
        (
            Action.IMPLEMENT,
            NodeKind.TASK,
            ["claude-opus-4", "claude-sonnet-4"],
            [],
            None,
            0,
            "sonnet",
        ),
        (Action.IMPLEMENT, NodeKind.TASK, ["claude-haiku-4"], [], None, 0, "haiku"),
        (Action.IMPLEMENT, NodeKind.TASK, [], [], None, 0, "sonnet"),
        (Action.REVIEW, NodeKind.TASK, ["claude-opus-4"], [], None, 0, "sonnet"),
        (Action.REVIEW, NodeKind.TASK, ["claude-sonnet-4"], ["claude-opus-4"], None, 0, "opus"),
        (Action.REVIEW, NodeKind.PLAN, ["claude-sonnet-4"], [], None, 0, "opus"),
        (Action.REVIEW, NodeKind.SPEC, ["claude-sonnet-4", "fable-1"], [], None, 0, "fable"),
        (Action.REVIEW, NodeKind.PLAN, [], ["claude-haiku-4"], None, 0, "haiku"),
        (Action.FIX, NodeKind.TASK, ["claude-opus-4"], [], Outcome.REJECT, 1, "opus"),
        (
            Action.FIX,
            NodeKind.TASK,
            ["claude-haiku-4", "claude-opus-4"],
            [],
            Outcome.REJECT,
            2,
            "sonnet",
        ),
        (Action.FIX, NodeKind.TASK, ["fable-1"], [], Outcome.REJECT, 2, "fable"),
        (Action.FIX, NodeKind.PLAN, ["claude-sonnet-4"], [], Outcome.REJECT, 2, "sonnet"),
        (Action.FIX, NodeKind.PLAN, ["claude-sonnet-4"], [], Outcome.REJECT, 3, "opus"),
        (Action.FIX, NodeKind.PLAN, ["claude-sonnet-4", "fable-1"], [], Outcome.REJECT, 3, "fable"),
        (Action.FIX, NodeKind.TASK, ["claude-opus-4"], [], Outcome.MERGE_FAILED, 0, "sonnet"),
        (Action.MERGE, NodeKind.TASK, ["claude-opus-4"], [], None, 0, "sonnet"),
        (Action.SYNC, NodeKind.PLAN, ["claude-opus-4"], [], None, 0, "sonnet"),
    ],
)
def test_each_step_is_routed_to_its_model_family(
    action: Action,
    kind: NodeKind,
    models: list[str],
    review_models: list[str],
    fix_for: Outcome | None,
    fix_round: int,
    expected: str,
) -> None:
    node = Node(
        id="N1",
        kind=kind,
        title="N1",
        acceptable_models=models,
        frontmatter={"review_models": review_models} if review_models else {},
        fix_for=fix_for,
    )
    assert model_for(action, node, fix_round) == expected


def test_a_blocked_answer_has_no_model() -> None:
    with pytest.raises(ValueError, match="no model runs"):
        model_for(Action.BLOCKED, Node(id="N1", kind=NodeKind.TASK, title="N1"), 0)
```

`tests/unit/test_claims.py`:

```python
import threading
import time
from pathlib import Path

import pytest
from lifecycle_estate import add, branch_at, git, make_estate, on_branch, section, stored

from taskmanager.core.enums import NodeKind, RelationType
from taskmanager.core.models import Condition, Job, NodeRelation
from taskmanager.core.status import (
    Action,
    ConditionStage,
    JobKind,
    JobState,
    Merge,
    Outcome,
    Status,
)
from taskmanager.engine.claims import Blocker, ClaimResult, Claims
from taskmanager.engine.config import ProjectConfig
from taskmanager.engine.operations import OperationError


class FakeLanding:
    def __init__(self) -> None:
        self.started: list[str] = []

    def start_land(self, node_id: str) -> str:
        self.started.append(node_id)
        return "job-1"

    def start_sync(self, node_id: str, pairs: list[tuple[str, str]]) -> str:
        raise AssertionError("no sync is expected here")


def decisions_blocking(claims: Claims, node_id: str) -> list[str]:
    return [
        dep
        for dep in claims.nodes.get_dependencies(node_id)
        if stored(claims, dep).kind == NodeKind.DECISION
    ]


def test_an_implement_claim_cuts_the_branch_from_origin_main_and_locks_declared_files(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path)
    add(claims, "T1", files=["api/app.py"], models=["claude-opus-4", "claude-sonnet-4"])

    result = claims.start("T1", "agent-1", "s1")

    assert result.action == Action.IMPLEMENT
    assert result.model == "sonnet"
    assert (result.branch, result.base, result.repos) == ("tm/T1", "main", ["api"])
    assert result.worktree is not None
    assert result.worktrees == {"api": result.worktree}
    worktree = Path(result.worktree)
    assert git(worktree, "rev-parse", "--abbrev-ref", "HEAD") == "tm/T1"
    assert git(worktree, "rev-parse", "HEAD") == git(
        claims.root / "api", "rev-parse", "origin/main"
    )
    node = stored(claims, "T1")
    assert (node.status, node.claimed_from) == (Status.IMPLEMENTING, Status.READY)
    lease = claims.runtime.get_lease("T1")
    assert lease is not None
    assert (lease.action, lease.ttl_seconds, lease.model) == (
        Action.IMPLEMENT,
        claims.ttl_for(Action.IMPLEMENT),
        "sonnet",
    )
    assert claims.runtime.get_conflicting_tasks(["api/app.py"])


def test_a_claim_cuts_its_worktree_under_the_directory_the_caller_names(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)
    add(claims, "T1")

    result = claims.start("T1", "agent-1", "s1", worktree_dir=tmp_path / "wave")

    assert result.worktree is not None
    assert Path(result.worktree).parent == tmp_path / "wave"
    assert git(Path(result.worktree), "rev-parse", "--abbrev-ref", "HEAD") == "tm/T1"


def test_an_implement_claim_resumes_an_existing_branch_and_a_fix_reuses_its_worktree(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path)
    api = claims.root / "api"
    earlier = on_branch(api, "tm/T1", "a.py", "a = 1\n")
    add(claims, "T1")

    first = claims.start("T1", "implementer", "s1")

    assert first.worktree is not None
    assert git(Path(first.worktree), "rev-parse", "HEAD") == earlier
    assert git(api, "rev-parse", "tm/T1") == earlier
    claims.complete("T1")
    claims.start("T1", "reviewer", "s1")
    claims.ops.set_section("T1", "review", "a.py needs a docstring")
    claims.review("T1", approve=False)
    fix = claims.start("T1", "fixer", "s1")
    assert (fix.action, fix.worktree) == (Action.FIX, first.worktree)
    assert git(Path(fix.worktree), "rev-parse", "HEAD") == earlier


@pytest.mark.parametrize(("review_cycles", "model"), [(1, "sonnet"), (2, "sonnet"), (3, "opus")])
def test_a_container_fix_is_routed_by_the_one_based_review_round_it_answers(
    tmp_path: Path, review_cycles: int, model: str
) -> None:
    claims = make_estate(tmp_path)
    add(
        claims,
        "P",
        NodeKind.PLAN,
        review=True,
        fix=True,
        status=Status.REVIEWED,
        outcome=Outcome.REJECT,
        review_cycles=review_cycles,
        models=["claude-sonnet-4"],
    )

    result = claims.start("P", "fixer", "s1")

    assert (result.action, result.model) == (Action.FIX, model)
    lease = claims.runtime.get_lease("P")
    assert lease is not None and lease.model == model


def test_closing_a_step_with_an_agent_is_refused_unless_that_agent_holds_the_lease(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path)
    add(claims, "T1")
    add(claims, "T2", status=Status.IMPLEMENTED)
    claims.start("T1", "implementer", "s1")
    claims.start("T2", "reviewer", "s1")
    claims.ops.set_section("T2", "review", "fine")

    for close in (
        lambda: claims.complete("T1", agent="intruder"),
        lambda: claims.release("T1", agent="intruder"),
        lambda: claims.review("T2", approve=True, agent="intruder"),
    ):
        with pytest.raises(OperationError, match="holds no live lease") as refused:
            close()
        assert refused.value.status_code == 409

    assert stored(claims, "T1").status == Status.IMPLEMENTING
    assert stored(claims, "T2").status == Status.REVIEWING
    assert claims.complete("T1", agent="implementer") == Status.IMPLEMENTED
    assert claims.review("T2", approve=True, agent="reviewer") == Status.REVIEWED


def test_a_child_landing_on_its_parent_is_cut_from_the_parent_branch(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)
    api = claims.root / "api"
    add(claims, "P", NodeKind.PLAN)
    add(claims, "T1", parent="P", merge=Merge.PARENT)

    result = claims.start("T1", "agent-1", "s1")

    assert result.base == "tm/P"
    assert git(api, "rev-parse", "tm/P") == git(api, "rev-parse", "origin/main")
    assert result.worktree is not None
    assert git(Path(result.worktree), "rev-parse", "HEAD") == git(api, "rev-parse", "tm/P")


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        ("edge", "waits on D"),
        ("decision", "awaiting decision"),
        ("condition", "condition unmet: flag"),
        ("lock", "declared files locked"),
        ("no_repo", "no target_repo"),
        ("completed", "has no next action"),
    ],
)
def test_a_refused_claim_answers_blocked_and_writes_nothing(
    tmp_path: Path, setup: str, reason: str
) -> None:
    claims = make_estate(tmp_path)
    add(
        claims,
        "T1",
        files=["api/app.py"],
        repo=None if setup == "no_repo" else "api",
        status=Status.COMPLETED if setup == "completed" else Status.READY,
    )
    if setup == "edge":
        add(claims, "D")
        claims.nodes.add_relation(
            NodeRelation(source_id="T1", target_id="D", relation_type=RelationType.DEPENDS_ON)
        )
    if setup == "decision":
        claims.ops.add_decision("Which way?", blocks=["T1"])
    if setup == "condition":
        claims.nodes.add_condition(
            Condition(
                node_id="T1",
                idx=0,
                needs="flag",
                command=f"test -f {tmp_path / 'flag'}",
                stage=ConditionStage.CLAIM,
            )
        )
    if setup == "lock":
        add(claims, "T2", files=["api/app.py"])
        assert claims.start("T2", "agent-2", "s2").action == Action.IMPLEMENT
    before = stored(claims, "T1")

    result = claims.start("T1", "agent-1", "s1")

    assert result.action == Action.BLOCKED
    assert reason in (result.reason or "")
    assert stored(claims, "T1") == before
    assert claims.runtime.get_lease("T1") is None


def test_two_sessions_claiming_at_once_leave_exactly_one_lease(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)
    add(claims, "T1")
    barrier = threading.Barrier(2)
    results: list[ClaimResult] = []

    def claim(agent: str) -> None:
        barrier.wait()
        results.append(claims.start("T1", agent, agent))

    threads = [threading.Thread(target=claim, args=(agent,)) for agent in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(r.action for r in results) == [Action.BLOCKED, Action.IMPLEMENT]
    winner = next(r for r in results if r.action == Action.IMPLEMENT)
    lease = claims.runtime.get_lease("T1")
    assert lease is not None and winner.worktree is not None
    assert stored(claims, "T1").status == Status.IMPLEMENTING


@pytest.mark.parametrize(
    ("configured", "action", "ttl"),
    [
        (900, Action.IMPLEMENT, 900),
        (900, Action.REVIEW, 3600),
        ({"review": 60}, Action.REVIEW, 60),
        ({"review": 60}, Action.IMPLEMENT, 10800),
    ],
)
def test_lease_ttl_reads_a_per_action_map_or_a_scalar_implement_default(
    tmp_path: Path, configured: int | dict[str, int], action: Action, ttl: int
) -> None:
    claims = make_estate(tmp_path, config=ProjectConfig(lease_ttl=configured))
    assert claims.ttl_for(action) == ttl


def test_complete_closes_implement_and_releases_the_lease(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)
    add(claims, "T1")
    claims.start("T1", "agent-1", "s1")

    assert claims.complete("T1") == Status.IMPLEMENTED
    assert stored(claims, "T1").claimed_from is None
    assert claims.runtime.get_lease("T1") is None


def test_complete_refuses_a_node_that_is_not_mid_implement_or_fix(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)
    add(claims, "T1")
    with pytest.raises(OperationError) as refused:
        claims.complete("T1")
    assert refused.value.status_code == 409


def test_a_review_verdict_is_refused_until_the_review_section_changes(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)
    add(claims, "T1", status=Status.IMPLEMENTED)

    result = claims.start("T1", "reviewer", "s1")
    assert (result.action, result.model, result.worktree) == (Action.REVIEW, "sonnet", None)
    with pytest.raises(OperationError, match="unchanged since the claim"):
        claims.review("T1", approve=True)

    claims.ops.set_section("T1", "review", "looks right")
    assert claims.review("T1", approve=True, verdict="ship it") == Status.REVIEWED
    node = stored(claims, "T1")
    assert (node.outcome, node.verdict, node.review_cycles) == (Outcome.APPROVE, "ship it", 1)


def test_a_task_rejected_with_no_fix_round_left_fails_and_opens_a_decision(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)
    add(claims, "T1")
    claims.start("T1", "implementer", "s1")
    claims.complete("T1")

    status = Status.READY
    for round_ in range(3):
        assert claims.start("T1", "reviewer", "s1").action == Action.REVIEW
        claims.ops.set_section("T1", "review", f"finding {round_}")
        status = claims.review("T1", approve=False)
        if round_ < 2:
            assert status == Status.REVIEWED
            assert claims.start("T1", "fixer", "s1").action == Action.FIX
            assert claims.complete("T1") == Status.FIXED

    assert status == Status.FAILED
    assert len(decisions_blocking(claims, "T1")) == 1


def test_transient_releases_count_step_failures_until_the_cap_fails_the_node(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path)
    add(claims, "T1")
    for failures in (1, 2):
        claims.start("T1", "agent-1", "s1")
        assert claims.release("T1") == Status.READY
        assert stored(claims, "T1").step_failures == failures

    claims.start("T1", "agent-1", "s1")
    assert claims.release("T1") == Status.FAILED
    assert len(decisions_blocking(claims, "T1")) == 1


def test_a_blocked_release_writes_what_the_node_waits_on_without_counting_a_failure(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path)
    add(claims, "T1")
    add(claims, "D")
    claims.start("T1", "agent-1", "s1")

    assert claims.release("T1", Blocker(depends=["D"])) == Status.READY
    assert "D" in claims.nodes.get_dependencies("T1")
    assert stored(claims, "T1").step_failures == 0
    assert claims.start("T1", "agent-1", "s1").reason == "waits on D"


def test_a_blocked_release_naming_nothing_is_refused(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)
    add(claims, "T1")
    claims.start("T1", "agent-1", "s1")
    with pytest.raises(OperationError) as refused:
        claims.release("T1", Blocker())
    assert refused.value.status_code == 400
    assert stored(claims, "T1").status == Status.IMPLEMENTING


def test_a_blocked_release_with_a_prose_condition_is_refused(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)
    add(claims, "T1")
    claims.start("T1", "agent-1", "s1")
    prose = Condition(
        node_id="T1",
        idx=0,
        needs="design approved",
        command="the designer approves the frame",
        stage=ConditionStage.CLAIM,
    )
    with pytest.raises(OperationError, match="decision"):
        claims.release("T1", Blocker(condition=prose))
    assert stored(claims, "T1").status == Status.IMPLEMENTING


def test_a_blocked_release_that_closes_a_cycle_is_refused_with_the_cycle(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)
    add(claims, "T1")
    add(claims, "D", depends=("T1",))
    claims.start("T1", "agent-1", "s1")
    with pytest.raises(OperationError, match="←") as refused:
        claims.release("T1", Blocker(depends=["D"]))
    assert refused.value.status_code == 409
    assert "D" not in claims.nodes.get_dependencies("T1")
    assert claims.runtime.get_lease("T1") is not None


def test_sweep_returns_an_expired_claim_to_where_it_was_claimed_from(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)
    add(claims, "T1", status=Status.IMPLEMENTED)
    claims.start("T1", "reviewer", "s1", ttl=1)
    time.sleep(1.5)

    assert claims.sweep() == ["T1"]
    node = stored(claims, "T1")
    assert (node.status, node.step_failures, node.claimed_from) == (Status.IMPLEMENTED, 1, None)


def test_sweep_restores_a_mid_step_node_with_no_lease_row(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)
    add(claims, "T1", status=Status.REVIEWING, claimed_from=Status.IMPLEMENTED)

    assert claims.sweep() == ["T1"]
    assert stored(claims, "T1").status == Status.IMPLEMENTED


def test_a_merge_claim_starts_a_landing_job_and_returns_at_once(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)
    add(claims, "T1", status=Status.REVIEWED, outcome=Outcome.APPROVE, review_cycles=1)
    landing = FakeLanding()
    claims.landing = landing

    result = claims.start("T1", "merger", "s1")

    assert (result.action, result.job, result.model) == (Action.MERGE, "job-1", "sonnet")
    assert landing.started == ["T1"]
    assert stored(claims, "T1").status == Status.MERGING
    lease = claims.runtime.get_lease("T1")
    assert lease is not None and lease.action == Action.MERGE


def test_a_landing_waiting_for_an_agent_is_handed_to_the_next_claimant(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)
    add(claims, "T1", status=Status.REVIEWED, outcome=Outcome.APPROVE, review_cycles=1)
    claims.landing = FakeLanding()
    claims.start("T1", "merger", "s1")
    job = claims.jobs.create(
        Job(
            kind=JobKind.LAND,
            node_id="T1",
            repo="api",
            target="main",
            state=JobState.NEEDS_AGENT,
            step="gate",
            worktree="/tmp/landing-worktree",
            result={"reason": "conflict"},
        )
    )
    claims.runtime.park("T1")

    handed = claims.start("T1", "agent-2", "s2")

    assert (handed.action, handed.job, handed.worktree, handed.worktrees) == (
        Action.MERGE,
        job.id,
        "/tmp/landing-worktree",
        {"api": "/tmp/landing-worktree"},
    )
    lease = claims.runtime.get_lease("T1")
    assert lease is not None
    assert (lease.agent_id, lease.ttl_seconds, lease.model) == (
        "agent-2",
        claims.ttl_for(Action.MERGE),
        "sonnet",
    )
    assert claims.start("T1", "agent-3", "s3").action == Action.BLOCKED


def test_a_container_whose_children_completed_with_nothing_on_its_branch_completes(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path)
    add(claims, "P", NodeKind.PLAN)
    add(claims, "T1", parent="P", status=Status.REVIEWED, outcome=Outcome.APPROVE)
    branch_at(claims.root / "api", "tm/T1")

    assert claims.reset("T1", Status.COMPLETED, "landed by hand") == Status.COMPLETED
    assert stored(claims, "P").status == Status.COMPLETED
    assert "landed by hand" in section(claims, "T1", "reset")


def test_a_container_with_code_on_its_branch_stops_at_implemented(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)
    api = claims.root / "api"
    add(claims, "P", NodeKind.PLAN)
    add(
        claims,
        "T1",
        parent="P",
        merge=Merge.PARENT,
        status=Status.REVIEWED,
        outcome=Outcome.APPROVE,
    )
    branch_at(api, "tm/T1")
    on_branch(api, "tm/P", "p.py", "p = 1\n")

    claims.reset("T1", Status.COMPLETED, "landed by hand")

    assert stored(claims, "P").status == Status.IMPLEMENTED


def test_a_reset_to_completed_is_refused_while_the_branch_is_not_on_its_target(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path)
    add(claims, "T1", status=Status.REVIEWED, outcome=Outcome.APPROVE)
    on_branch(claims.root / "api", "tm/T1", "a.py", "a = 1\n")

    with pytest.raises(OperationError, match="is not on main"):
        claims.reset("T1", Status.COMPLETED, "landed by hand")
    assert stored(claims, "T1").status == Status.REVIEWED


def test_deferring_a_node_others_depend_on_opens_one_decision_for_the_dependents(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path)
    add(claims, "D")
    add(claims, "T1", depends=("D",))
    add(claims, "T2", depends=("D",))

    assert claims.defer("D", "waiting on the vendor") == Status.DEFERRED

    assert "waiting on the vendor" in section(claims, "D", "deferral")
    assert decisions_blocking(claims, "T1") == decisions_blocking(claims, "T2")
    assert len(decisions_blocking(claims, "T1")) == 1


def test_defer_is_refused_mid_step(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)
    add(claims, "T1")
    claims.start("T1", "agent-1", "s1")
    with pytest.raises(OperationError) as refused:
        claims.defer("T1", "later")
    assert refused.value.status_code == 409


def test_reopen_returns_a_deferred_task_to_ready_with_counters_cleared_and_the_note_kept(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path)
    add(claims, "T1", status=Status.DEFERRED, step_failures=2, review_cycles=1)

    assert claims.reopen("T1", "the vendor shipped") == Status.READY

    node = stored(claims, "T1")
    assert (node.step_failures, node.review_cycles, node.outcome) == (0, 0, None)
    assert "the vendor shipped" in section(claims, "T1", "reopen")


def test_reopen_is_refused_while_an_open_decision_blocks_the_node(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)
    add(claims, "T1", status=Status.FAILED)
    claims.ops.add_decision("Abandon T1?", blocks=["T1"])

    with pytest.raises(OperationError, match="open decision"):
        claims.reopen("T1", "try again")
    assert stored(claims, "T1").status == Status.FAILED


def test_reopen_with_a_new_branch_keeps_the_old_one_under_a_numbered_name(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)
    api = claims.root / "api"
    add(claims, "T1", status=Status.ABANDONED)
    old = on_branch(api, "tm/T1", "a.py", "a = 1\n")

    claims.reopen("T1", "start over", new_branch=True)

    assert git(api, "rev-parse", "tm/T1@1") == old
    assert git(api, "branch", "--list", "tm/T1") == ""
```

- [ ] **Step 2: Run them and watch them fail**

```
uv run --directory <worktree> pytest tests/unit/test_routing.py tests/unit/test_claims.py -q; echo $?
```

Expected: collection errors `ModuleNotFoundError: No module named 'taskmanager.engine.routing'` and `ModuleNotFoundError: No module named 'taskmanager.engine.claims'` (the second through `lifecycle_estate`), exit code 2.

- [ ] **Step 3: Implement**

Append to `src/taskmanager/engine/git.py` (after `class GitManager`; `subprocess` and `Path` are already imported):

```python
def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )


def _run(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def rev_parse(repo: Path, ref: str) -> str:
    """The commit `ref` names in `repo`, or "" when it names none (or `repo` is no repository)."""
    res = _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return res.stdout.strip() if res.returncode == 0 else ""


def is_ancestor(repo: Path, a: str, b: str) -> bool:
    return _git(repo, "merge-base", "--is-ancestor", a, b).returncode == 0


def diff_quiet(repo: Path, base: str, branch: str) -> bool:
    """True when `branch` changes no file against its merge base with `base`.

    Trees, not commits: a sync merge commit that brought nothing of the branch's own is not a
    change. A git error reads as a change, so nothing completes on a failed comparison.
    """
    return _git(repo, "diff", "--quiet", f"{base}...{branch}").returncode == 0


def fetch(repo: Path) -> bool:
    return _git(repo, "fetch", "-q", "origin", "main").returncode == 0


def ensure_branch(repo: Path, branch: str, base: str) -> bool:
    """Creates `branch` at `base` unless it exists; True when it was created.

    `--no-track`: a branch cut from `origin/main` would otherwise make a bare `git push` target
    the deploying `main`.
    """
    if rev_parse(repo, f"refs/heads/{branch}"):
        return False
    _run(repo, "branch", "--no-track", branch, base)
    return True


def rename_branch(repo: Path, old: str, new: str) -> None:
    _run(repo, "branch", "-m", old, new)
```

`src/taskmanager/engine/routing.py`:

```python
"""Which model family runs a step. tm names it at claim; the workflow maps a family to a model id."""

from taskmanager.core.enums import NodeKind
from taskmanager.core.models import Node
from taskmanager.core.status import Action, Outcome

# Cheapest first: `_families` sorts by this order, so [0] is the cheapest and [-1] the strongest.
FAMILIES = ("haiku", "sonnet", "opus", "fable")
STRONG = frozenset({"opus", "fable"})


def family(model_id: str) -> str | None:
    lowered = model_id.lower()
    return next((name for name in FAMILIES if name in lowered), None)


def _families(model_ids: list[str]) -> list[str]:
    found = {name for model_id in model_ids if (name := family(model_id)) is not None}
    return sorted(found, key=FAMILIES.index)


def _at_least_opus(families: list[str]) -> str:
    strongest = families[-1] if families else "opus"
    return strongest if strongest in STRONG else "opus"


def model_for(action: Action, node: Node, fix_round: int) -> str:
    families = _families(node.acceptable_models)
    review_families = _families([str(m) for m in node.frontmatter.get("review_models") or []])
    container = node.kind in (NodeKind.PLAN, NodeKind.SPEC)
    if action == Action.IMPLEMENT:
        return families[0] if families else "sonnet"
    if action == Action.REVIEW:
        if review_families:
            return review_families[0]
        # A container review is a branch review.
        return _at_least_opus(families) if container else "sonnet"
    if action == Action.FIX:
        if node.fix_for == Outcome.MERGE_FAILED:
            return "sonnet"
        # fix_round is the 1-based review round the fix answers; only a container reaches 3.
        if fix_round >= 3:
            return _at_least_opus(families)
        implementer = families[0] if families else "sonnet"
        return implementer if implementer in STRONG else "sonnet"
    if action in (Action.MERGE, Action.SYNC):
        return "sonnet"
    raise ValueError(f"no model runs a {action} step")
```

`src/taskmanager/engine/claims.py`:

```python
"""The claims engine: the only writer of a node's position in the dispatch cycle.

Every verb reads the stored cycle, asks the pure lifecycle rules for the next one, and writes it
with its lease change and the parents' rollup in one state.db transaction.
"""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from subprocess import CalledProcessError
from typing import Any, Protocol

from taskmanager.core import lifecycle
from taskmanager.core.enums import NodeKind, RelationType
from taskmanager.core.lifecycle import Caps, Cycle, LifecycleError
from taskmanager.core.models import Condition, FileLock, Lease, LedgerEvent, Node, NodeRelation
from taskmanager.core.rollup import rollup
from taskmanager.core.status import (
    EXITS,
    IN_STEP,
    Action,
    ConditionStage,
    DecisionStatus,
    Event,
    JobKind,
    JobState,
    Merge,
    Outcome,
    Status,
)
from taskmanager.db.cache_repo import CacheRepository
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.job_repo import JobRepository
from taskmanager.di.container import create_container
from taskmanager.engine import git as gitops
from taskmanager.engine.chains import satisfied
from taskmanager.engine.conditions import ConditionRunner, is_executable
from taskmanager.engine.config import ConfigStore, ProjectConfig
from taskmanager.engine.decisions import open_failed_decision, open_stranded_decision
from taskmanager.engine.git import GitManager
from taskmanager.engine.operations import OperationError, Operations
from taskmanager.engine.routing import model_for
from taskmanager.engine.snapshot import SnapshotBuilder
from taskmanager.engine.stepgraph import Snapshot
from taskmanager.engine.validation import Refusal, validate

DEFAULT_TTL: dict[Action, int] = {
    Action.IMPLEMENT: 10800,
    Action.REVIEW: 3600,
    Action.FIX: 7200,
    Action.MERGE: 3600,
    Action.SYNC: 3600,
}
LIVE_JOBS = frozenset({JobState.RUNNING, JobState.NEEDS_AGENT})
CONTAINERS = frozenset({NodeKind.PLAN, NodeKind.SPEC})

_STALLED = "max_step_failures steps in a row ended without progress"
_FAILED_BECAUSE: dict[Event, str] = {
    Event.REJECT: "its review rejected it with no fix round left",
    Event.OWN_DEFECT: "its landing failed on its own defect with no merge fix left",
    Event.RELEASE: _STALLED,
    Event.EXPIRED: _STALLED,
}


@dataclass(frozen=True)
class DecisionSpec:
    question: str
    # Each option is "key|Label|description|effect", the `tm decision add --option` syntax.
    options: list[str] = field(default_factory=list)
    recommend: str | None = None


@dataclass(frozen=True)
class Blocker:
    depends: list[str] = field(default_factory=list)
    decision: DecisionSpec | None = None
    condition: Condition | None = None


@dataclass(frozen=True)
class ClaimResult:
    action: Action
    reason: str | None = None
    model: str | None = None
    job: str | None = None
    repos: list[str] = field(default_factory=list)
    branch: str | None = None
    base: str | None = None
    # A task's worktree; for a container, the directory holding one worktree per repository.
    worktree: str | None = None
    # Repository name to the worktree the step works in there.
    worktrees: dict[str, str] = field(default_factory=dict)


class LandingJobs(Protocol):
    def start_land(self, node_id: str) -> str: ...

    def start_sync(self, node_id: str, pairs: list[tuple[str, str]]) -> str: ...


class _UnmovedBranches:
    """Branch facts for writes that change neither `merge` nor the parent, where the
    branch-base rule cannot fire."""

    def branch_exists(self, node_id: str) -> bool:
        return False

    def base_matches(self, node_id: str, new_target: str) -> bool:
        return True


class Claims:
    def __init__(
        self,
        root: Path,
        config: ProjectConfig,
        ops: Operations,
        jobs: JobRepository,
        snapshots: SnapshotBuilder,
        conditions: ConditionRunner,
    ) -> None:
        self.root = root
        self.config = config
        self.ops = ops
        self.nodes = ops.node_repo
        self.runtime = ops.runtime_repo
        self.jobs = jobs
        self.snapshots = snapshots
        self.conditions = conditions
        # Set by the landing engine when it is built: it needs these claims to move nodes.
        self.landing: LandingJobs | None = None
        self.caps = Caps(
            fix_rounds_task=config.max_fix_rounds.task,
            fix_rounds_container=config.max_fix_rounds.container,
            merge_attempts=config.max_merge_attempts,
            step_failures=config.max_step_failures,
        )

    @classmethod
    def open(cls, root: Path, config: ProjectConfig | None = None) -> Claims:
        container = create_container(root)
        db = container.get(DatabaseManager)
        ops = container.get(Operations)
        cfg = config or ConfigStore(root).project()
        jobs = JobRepository(db)
        cache = CacheRepository(db)
        return cls(
            root,
            cfg,
            ops,
            jobs,
            SnapshotBuilder(ops.node_repo, ops.runtime_repo, jobs),
            ConditionRunner(root, ops.node_repo, cache, cfg.condition_ttl, cfg.condition_timeout),
        )

    # -- reading ------------------------------------------------------------------------------

    def node(self, node_id: str) -> Node:
        found = self.nodes.get_node(node_id)
        if found is None:
            raise OperationError(f"node '{node_id}' not found", 404)
        if found.kind == NodeKind.DECISION:
            raise OperationError(f"'{node_id}' is a decision; use `tm decision`", 400)
        return found

    @staticmethod
    def is_container(node: Node) -> bool:
        return node.kind in CONTAINERS

    def branch_of(self, node_id: str) -> str:
        return self.node(node_id).branch or f"tm/{node_id}"

    def _parent(self, node_id: str) -> str | None:
        parents = self.nodes.get_parent_ids(node_id)
        return parents[0] if parents else None

    def target_of(self, node_id: str) -> str:
        """The branch `node_id` lands on: `main`, or its parent's branch."""
        parent = self._parent(node_id)
        if self.node(node_id).merge == Merge.PARENT and parent is not None:
            return self.branch_of(parent)
        return "main"

    @staticmethod
    def target_ref(target: str) -> str:
        """The ref a landing target is read at: `main` only through the fetched `origin/main`;
        container branches are local refs in the shared clones."""
        return "origin/main" if target == "main" else target

    def _descendants(self, node_id: str) -> list[str]:
        found: list[str] = []
        frontier = self.nodes.get_children(node_id)
        while frontier:
            child = frontier.pop(0)
            found.append(child)
            frontier.extend(self.nodes.get_children(child))
        return found

    def repos_of(self, node_id: str) -> list[str]:
        """A task's target repository; a container's, the repositories of its descendant tasks
        in landing order (`land_order`, then config `repo_order`, then by name)."""
        node = self.node(node_id)
        if not self.is_container(node):
            return [node.target_repo] if node.target_repo else []
        found: set[str] = set()
        for descendant in self._descendants(node_id):
            child = self.nodes.get_node(descendant)
            if child is not None and child.target_repo:
                found.add(child.target_repo)
        order = [*node.land_order, *self.config.repo_order]
        return sorted(found, key=lambda r: (order.index(r) if r in order else len(order), r))

    def ttl_for(self, action: Action) -> int:
        configured = self.config.lease_ttl
        if isinstance(configured, int):
            # A scalar is the lease_ttl earlier versions read: the implement lease.
            return configured if action == Action.IMPLEMENT else DEFAULT_TTL[action]
        return configured.get(action.value, DEFAULT_TTL[action])

    @staticmethod
    def _live(lease: Lease) -> bool:
        if lease.ttl_seconds is None:
            # Parked for an agent: never expires until someone takes it.
            return True
        age = (datetime.now(tz=UTC) - lease.last_heartbeat).total_seconds()
        return age <= lease.ttl_seconds

    def _locked_files(self, node: Node, action: Action | None) -> list[str]:
        if action not in (Action.IMPLEMENT, Action.FIX):
            return []
        own = self.nodes.declared_files(node.id)
        if own or not self.is_container(node):
            return own
        return list(
            dict.fromkeys(
                f for d in self._descendants(node.id) for f in self.nodes.declared_files(d)
            )
        )

    def blocked_reason(self, node: Node, snap: Snapshot, action: Action | None) -> str | None:
        """Why `node` cannot be claimed now, the first reason in claimability order; None when
        it can. Reads only: discovery asks it of every node."""
        live = [j for j in self.jobs.for_node(node.id) if j.state in LIVE_JOBS]
        if live:
            job = live[0]
            if job.kind == JobKind.SYNC:
                return f"syncing {job.target}"
            return f"landing job {job.id} is {job.state}"
        lease = self.runtime.get_lease(node.id)
        if lease is not None and self._live(lease):
            return f"held by {lease.agent_id}"
        edges = snap.inherited_edges(node.id)
        decisions = [d for d in edges if snap.status(d) == DecisionStatus.OPEN]
        if decisions:
            return f"awaiting decision {', '.join(decisions)}"
        waiting = [
            d
            for d in edges
            if isinstance(snap.status(d), Status) and not satisfied(snap, node.id, d)
        ]
        if waiting:
            return f"waits on {', '.join(waiting)}"
        stages = [
            ConditionStage.CLAIM,
            *([ConditionStage.LANDING] if action == Action.MERGE else []),
        ]
        for stage in stages:
            unmet = self.conditions.unmet(node.id, stage)
            if unmet:
                return f"condition unmet: {unmet[0].needs}"
        if action is None:
            return f"{node.status} has no next action"
        if action == Action.IMPLEMENT and not node.target_repo:
            return "no target_repo: a task is cut and landed in its target repository"
        if action == Action.MERGE and not self.repos_of(node.id):
            return "nothing to land: no task under it names a target_repo"
        conflicts = self.runtime.get_conflicting_tasks(self._locked_files(node, action))
        if conflicts:
            return f"declared files locked: {', '.join(sorted(conflicts))}"
        return None

    def next_step(self, node: Node) -> tuple[Action | None, str | None]:
        """The action a claim would take now, and the model it would name."""
        cycle = self.snapshots.cycle(node)
        action = lifecycle.next_action(cycle)
        if action is None:
            return None, None
        claimed = lifecycle.claim(cycle)
        return action, model_for(
            action, self._with_cycle(node, claimed), lifecycle.fix_round(claimed)
        )

    def verify(self, node_id: str, ref: str, repo: str | None = None) -> tuple[bool, str]:
        """The node's verifications at `ref` (a container's: every descendant task's), limited to
        `repo` when given. An empty set passes and says so."""
        node = self.node(node_id)
        ids = (
            [d for d in self._descendants(node_id) if self.node(d).kind == NodeKind.TASK]
            if self.is_container(node)
            else [node_id]
        )
        repo_for: dict[str, str | None] = {i: self.node(i).target_repo for i in ids}
        checks = [
            v
            for i in ids
            if repo is None or repo_for[i] == repo
            for v in self.nodes.get_verifications(i)
        ]
        if not checks:
            return True, f"no verifications to run at {ref}: an empty set passes"
        results = self.ops.verification_engine.verify_all(checks, repo_for, ref)
        report = "\n".join(
            f"{'PASS' if r.passed else 'FAIL'} {r.verification_type} {r.target_path}: {r.message}"
            for r in results
        )
        return all(r.passed for r in results), report

    # -- claiming -----------------------------------------------------------------------------

    def start(
        self,
        node_id: str,
        agent: str,
        session: str,
        ttl: int | None = None,
        worktree_dir: Path | None = None,
    ) -> ClaimResult:
        self.node(node_id)
        self.sweep()
        node = self.node(node_id)
        handed = self._hand_over(node, agent, session, ttl)
        if handed is not None:
            return handed
        snap = self.snapshots.build()
        cycle = self.snapshots.cycle(node)
        action = lifecycle.next_action(cycle)
        reason = self.blocked_reason(node, snap, action)
        if reason is not None or action is None:
            return ClaimResult(Action.BLOCKED, reason)
        return self._claim(node, cycle, action, agent, session, ttl, worktree_dir)

    def _hand_over(
        self, node: Node, agent: str, session: str, ttl: int | None
    ) -> ClaimResult | None:
        """A job stopped for an agent is handed, with its lease, to whoever claims its node."""
        job = next(
            (j for j in self.jobs.for_node(node.id) if j.state == JobState.NEEDS_AGENT), None
        )
        lease = self.runtime.get_lease(node.id)
        if job is None or lease is None or lease.ttl_seconds is not None:
            return None
        action = Action.MERGE if job.kind == JobKind.LAND else Action.SYNC
        model = model_for(action, node, 0)
        if not self.runtime.take_over(node.id, agent, session, ttl or self.ttl_for(action), model):
            return ClaimResult(Action.BLOCKED, f"job {job.id} was handed to another agent first")
        self._ledger("job handover", node.id, {"job": job.id, "agent": agent})
        if action == Action.SYNC:
            branch, base = job.target, str(job.result.get("source", job.target))
        else:
            branch, base = self.branch_of(node.id), job.target
        return ClaimResult(
            action,
            None,
            model,
            job.id,
            [job.repo],
            branch,
            base,
            job.worktree,
            {job.repo: job.worktree} if job.worktree else {},
        )

    def _claim(
        self,
        node: Node,
        cycle: Cycle,
        action: Action,
        agent: str,
        session: str,
        ttl: int | None,
        worktree_dir: Path | None,
    ) -> ClaimResult:
        claimed = lifecycle.claim(cycle)
        after = self._with_cycle(node, claimed)
        model = model_for(action, after, lifecycle.fix_round(claimed))
        lease = Lease(
            task_id=node.id,
            agent_id=agent,
            session_id=session,
            branch_name=self.branch_of(node.id),
            ttl_seconds=ttl or self.ttl_for(action),
            action=action,
            review_hash=self._review_hash(node.id) if action == Action.REVIEW else None,
            model=model,
        )
        locks = [FileLock(file_path=f, task_id=node.id) for f in self._locked_files(node, action)]
        if not self.runtime.claim(lease, locks, after):
            return ClaimResult(Action.BLOCKED, "claimed by another session at the same instant")
        self._ledger(
            "task start", node.id, {"action": action.value, "agent": agent, "from": node.status}
        )
        try:
            return self._begin(after, action, model, worktree_dir)
        except (OperationError, CalledProcessError, OSError) as exc:
            self._unclaim(node)
            detail = getattr(exc, "stderr", None) or str(exc)
            raise OperationError(f"claim of {node.id} undone: {detail}".strip(), 409) from exc

    def _begin(
        self, node: Node, action: Action, model: str, worktree_dir: Path | None
    ) -> ClaimResult:
        repos = self.repos_of(node.id)
        branch = self.branch_of(node.id)
        if self.is_container(node):
            repos = [r for r in repos if gitops.rev_parse(self.root / r, f"refs/heads/{branch}")]
        worktree: str | None = None
        worktrees: dict[str, str] = {}
        if action in (Action.IMPLEMENT, Action.FIX):
            worktree, worktrees = self._cut(node, repos, worktree_dir)
        job = self._landing().start_land(node.id) if action == Action.MERGE else None
        return ClaimResult(
            action,
            None,
            model,
            job,
            repos,
            branch,
            self.target_of(node.id),
            worktree,
            worktrees,
        )

    def _landing(self) -> LandingJobs:
        if self.landing is None:
            raise OperationError("no landing engine is attached to these claims", 500)
        return self.landing

    def _cut(
        self, node: Node, repos: list[str], worktree_dir: Path | None
    ) -> tuple[str, dict[str, str]]:
        """A worktree of the node's branch in each repository. A branch that already exists is
        checked out as it stands, never cut again: a fix continues its implement's commits, and a
        reopened or re-imported node resumes its branch. Write-time validation refuses a change
        of target once the branch exists, so an existing branch is cut from this node's target."""
        # An absolute worktree_dir replaces the root: `Path / absolute` is the absolute path.
        base_dir = self.root / (worktree_dir or self.config.worktree_dir)
        branch = self.branch_of(node.id)
        worktrees: dict[str, str] = {}
        if not self.is_container(node):
            repo = repos[0]
            path = GitManager(self.root / repo).create_worktree(
                branch, base_dir / f"{repo}-{node.id}", self._base_ref(node.id, repo)
            )
            worktrees[repo] = str(path)
            return str(path), worktrees
        for repo in repos:
            path = GitManager(self.root / repo).create_worktree(
                branch, base_dir / node.id / repo, self._base_ref(node.id, repo)
            )
            worktrees[repo] = str(path)
        return str(base_dir / node.id), worktrees

    def _base_ref(self, node_id: str, repo: str) -> str:
        """The ref `node_id`'s branch is cut from in `repo`, creating each ancestor container
        branch on the way: a node builds on its landing target, never on `main` past a parent
        that has not landed."""
        repo_dir = self.root / repo
        parent = self._parent(node_id)
        if self.node(node_id).merge != Merge.PARENT or parent is None:
            gitops.fetch(repo_dir)
            return GitManager(repo_dir).default_base_ref()
        parent_branch = self.branch_of(parent)
        if not gitops.rev_parse(repo_dir, f"refs/heads/{parent_branch}"):
            gitops.ensure_branch(repo_dir, parent_branch, self._base_ref(parent, repo))
        return parent_branch

    def _unclaim(self, original: Node) -> None:
        with self.nodes.transaction():
            self.nodes.save_node(original)
            self.runtime.release_lease(original.id)
        self._ledger("task start undone", original.id, {})

    # -- closing a step -----------------------------------------------------------------------

    def _held(
        self, node_id: str, statuses: tuple[Status, ...], agent: str | None
    ) -> tuple[Node, Lease]:
        node = self.node(node_id)
        if Status(node.status) not in statuses:
            wanted = " or ".join(statuses)
            raise OperationError(f"{node_id} is {node.status}; this closes {wanted}", 409)
        lease = self.runtime.get_lease(node_id)
        if lease is None:
            raise OperationError(f"{node_id} holds no lease: it was swept or released", 409)
        self._own(node_id, lease, agent)
        return node, lease

    def _own(self, node_id: str, lease: Lease, agent: str | None) -> None:
        """A named agent closes only its own live step: an agent whose lease expired and was
        claimed again must not close its successor's."""
        if agent is not None and (lease.agent_id != agent or not self._live(lease)):
            raise OperationError(
                f"{agent} holds no live lease on {node_id}; {lease.agent_id} does", 409
            )

    def complete(self, node_id: str, agent: str | None = None) -> Status:
        node, _ = self._held(node_id, (Status.IMPLEMENTING, Status.FIXING), agent)
        return self._advance(node, Event.COMPLETE, "task complete")

    def review(
        self,
        node_id: str,
        approve: bool,
        verdict: str | None = None,
        agent: str | None = None,
    ) -> Status:
        node, lease = self._held(node_id, (Status.REVIEWING,), agent)
        if self._review_hash(node_id) == lease.review_hash:
            raise OperationError(
                f"{node_id}:review is unchanged since the claim: write the findings with "
                f"`tm section set {node_id}:review` first",
                409,
            )
        event = Event.APPROVE if approve else Event.REJECT
        evidence = self._section_text(node_id, "review")
        return self._advance(node, event, "task review", verdict=verdict, evidence=evidence)

    def release(
        self, node_id: str, blocked: Blocker | None = None, agent: str | None = None
    ) -> Status:
        node = self.node(node_id)
        lease = self.runtime.get_lease(node_id)
        if lease is None:
            raise OperationError(f"{node_id} holds no lease to release", 409)
        self._own(node_id, lease, agent)
        if blocked is not None:
            if not (blocked.depends or blocked.decision or blocked.condition):
                raise OperationError(
                    "--blocked names what the node now waits on: --depends, --decision or "
                    "--condition",
                    400,
                )
            if blocked.condition is not None and not is_executable(blocked.condition.command):
                raise OperationError(
                    "a condition needs a command that exits 0 once it holds; a question for a "
                    "person is a --decision",
                    400,
                )
            self._check_edges(node_id, blocked.depends)
        has_job = any(j.state in LIVE_JOBS for j in self.jobs.for_node(node_id))
        if Status(node.status) not in IN_STEP and not has_job:
            raise OperationError(f"{node_id} is {node.status}, not mid-step", 409)
        self._expire_jobs(node_id)
        event = Event.RELEASE_BLOCKED if blocked is not None else Event.RELEASE
        return self._advance(node, event, "task release", blocker=blocked)

    def heartbeat(self, node_id: str) -> bool:
        return self.runtime.heartbeat(node_id)

    def sweep(self) -> list[str]:
        swept = self.runtime.sweep_expired_leases()
        for node in self.nodes.list_nodes():
            if (
                node.id not in swept
                and node.kind != NodeKind.DECISION
                and Status(node.status) in IN_STEP
                and self.runtime.get_lease(node.id) is None
            ):
                swept.append(node.id)
        for node_id in swept:
            node = self.nodes.get_node(node_id)
            if node is None or node.kind == NodeKind.DECISION:
                continue
            expired = self._expire_jobs(node_id)
            if Status(node.status) in IN_STEP or expired:
                self._advance(node, Event.EXPIRED, "lease sweep")
        return swept

    def _expire_jobs(self, node_id: str) -> int:
        live = [j for j in self.jobs.for_node(node_id) if j.state in LIVE_JOBS]
        for job in live:
            self.jobs.update(job.model_copy(update={"state": JobState.EXPIRED}))
            self.jobs.release_branch(job.repo, job.target, job.id)
        return len(live)

    def _advance(
        self,
        node: Node,
        event: Event,
        command: str,
        *,
        verdict: str | None = None,
        note: tuple[str, str] | None = None,
        blocker: Blocker | None = None,
        evidence: str = "",
    ) -> Status:
        try:
            nxt = lifecycle.advance(self.snapshots.cycle(node), event, self.caps)
        except LifecycleError as exc:
            raise OperationError(str(exc), 409) from exc
        after = self._with_cycle(node, nxt)
        if verdict is not None:
            after = after.model_copy(update={"verdict": verdict})
        with self.nodes.transaction():
            self.nodes.save_node(after)
            # Every event that moves a node ends the step its lease was for.
            self.runtime.release_lease(node.id)
            if note is not None:
                self.note(node.id, *note)
            if blocker is not None:
                self._write_blocker(node.id, blocker)
            if nxt.status == Status.FAILED and node.status != Status.FAILED:
                reason = _FAILED_BECAUSE.get(event, "its step failed")
                open_failed_decision(self.ops, node.id, reason, evidence)
            self._propagate(node.id)
        self._ledger(
            command, node.id, {"event": event.value, "from": node.status, "to": nxt.status}
        )
        return nxt.status

    def _write_blocker(self, node_id: str, blocker: Blocker) -> None:
        for dep in blocker.depends:
            self.nodes.add_relation(
                NodeRelation(
                    source_id=node_id, target_id=dep, relation_type=RelationType.DEPENDS_ON
                )
            )
        if blocker.decision is not None:
            spec = blocker.decision
            self.ops.add_decision(
                spec.question,
                options=spec.options,
                recommend=spec.recommend,
                raised_by=node_id,
                blocks=[node_id],
            )
        if blocker.condition is not None:
            self.nodes.add_condition(blocker.condition.model_copy(update={"node_id": node_id}))

    def _check_edges(self, node_id: str, depends: list[str]) -> None:
        missing = [d for d in depends if self.nodes.get_node(d) is None]
        if missing:
            raise OperationError(f"no node {', '.join(missing)}", 404)
        before = self.snapshots.build()
        after = replace(before, edges=[*before.edges, *((node_id, d) for d in depends)])
        self._refuse(validate(before, after, {node_id}, _UnmovedBranches()))

    @staticmethod
    def _refuse(refusals: list[Refusal]) -> None:
        if refusals:
            raise OperationError("; ".join(r.message for r in refusals), 409)

    # -- repair verbs -------------------------------------------------------------------------

    def _idle(self, node_id: str) -> None:
        lease = self.runtime.get_lease(node_id)
        if lease is not None and self._live(lease):
            raise OperationError(
                f"{node_id} is mid-step, held by {lease.agent_id}: wait for the step to end, "
                "or release it",
                409,
            )
        job = next((j for j in self.jobs.for_node(node_id) if j.state in LIVE_JOBS), None)
        if job is not None:
            raise OperationError(f"{node_id} has job {job.id} {job.state}: wait for it", 409)

    def reopen(self, node_id: str, note: str, new_branch: bool = False) -> Status:
        node = self.node(node_id)
        self._idle(node_id)
        open_decisions = [
            d
            for d in self.nodes.get_dependencies(node_id)
            if (dep := self.nodes.get_node(d)) is not None and dep.status == DecisionStatus.OPEN
        ]
        if open_decisions:
            raise OperationError(
                f"an open decision blocks {node_id} ({', '.join(open_decisions)}): answer it first",
                409,
            )
        counted = [s for s in self._child_statuses(node_id) if s not in EXITS]
        all_done = bool(counted) and all(s == Status.COMPLETED for s in counted)
        try:
            nxt = lifecycle.reopen(self.snapshots.cycle(node), all_done)
        except LifecycleError as exc:
            raise OperationError(str(exc), 409) from exc
        if new_branch:
            self._retire_branch(node_id)
        return self._rewrite(node, nxt, ("reopen", note), "task reopen", clear_verdict=True)

    def reset(self, node_id: str, to: Status, note: str, outcome: Outcome | None = None) -> Status:
        node = self.node(node_id)
        self._idle(node_id)
        if to == Status.COMPLETED:
            self._prove_landed(node_id)
        try:
            nxt = lifecycle.reset(self.snapshots.cycle(node), to, outcome)
        except LifecycleError as exc:
            raise OperationError(str(exc), 400) from exc
        return self._rewrite(node, nxt, ("reset", note), "task reset")

    def defer(self, node_id: str, note: str) -> Status:
        return self._set_aside(node_id, note, lifecycle.defer, "deferral", "task defer")

    def abandon(self, node_id: str, note: str) -> Status:
        return self._set_aside(node_id, note, lifecycle.abandon, "abandonment", "task abandon")

    def _set_aside(
        self,
        node_id: str,
        note: str,
        rule: Callable[[Cycle], Cycle],
        key: str,
        command: str,
    ) -> Status:
        node = self.node(node_id)
        self._idle(node_id)
        try:
            nxt = rule(self.snapshots.cycle(node))
        except LifecycleError as exc:
            raise OperationError(str(exc), 409) from exc
        return self._rewrite(node, nxt, (key, note), command)

    def _rewrite(
        self,
        node: Node,
        nxt: Cycle,
        note: tuple[str, str],
        command: str,
        *,
        clear_verdict: bool = False,
    ) -> Status:
        after = self._with_cycle(node, nxt)
        if clear_verdict:
            after = after.model_copy(update={"verdict": None})
        snap = self.snapshots.build()
        moved = replace(
            snap, nodes={**snap.nodes, node.id: replace(snap.nodes[node.id], status=nxt.status)}
        )
        self._refuse(validate(snap, moved, {node.id}, _UnmovedBranches()))
        with self.nodes.transaction():
            self.nodes.save_node(after)
            self.note(node.id, *note)
            if nxt.status in (Status.DEFERRED, Status.ABANDONED):
                self._strand(node.id, nxt.status)
            self._propagate(node.id)
        self._ledger(command, node.id, {"from": node.status, "to": nxt.status})
        return nxt.status

    def _prove_landed(self, node_id: str) -> None:
        branch, target = self.branch_of(node_id), self.target_of(node_id)
        ref = self.target_ref(target)
        container = self.is_container(self.node(node_id))
        for repo in self.repos_of(node_id):
            repo_dir = self.root / repo
            if target == "main":
                gitops.fetch(repo_dir)
            if not gitops.rev_parse(repo_dir, f"refs/heads/{branch}"):
                if container:
                    continue
                raise OperationError(f"{branch} does not exist in {repo}", 409)
            if not gitops.is_ancestor(repo_dir, branch, ref):
                raise OperationError(f"{branch} is not on {target} in {repo}: land it first", 409)
        passed, report = self.verify(node_id, ref)
        if not passed:
            raise OperationError(f"verifications red on {target}:\n{report}", 409)

    def _retire_branch(self, node_id: str) -> None:
        """Keeps the old branch as `<branch>@<n>` so the reopened node starts clean."""
        branch = self.branch_of(node_id)
        for repo in self.repos_of(node_id):
            repo_dir = self.root / repo
            if not gitops.rev_parse(repo_dir, f"refs/heads/{branch}"):
                continue
            n = 1
            while gitops.rev_parse(repo_dir, f"refs/heads/{branch}@{n}"):
                n += 1
            gitops.rename_branch(repo_dir, branch, f"{branch}@{n}")

    def _strand(self, node_id: str, status: Status) -> None:
        dependents = [
            d
            for d in self.nodes.get_blocked_by(node_id)
            if (dep := self.nodes.get_node(d)) is not None and dep.kind != NodeKind.DECISION
        ]
        if dependents:
            open_stranded_decision(self.ops, node_id, status, dependents)

    # -- containers ---------------------------------------------------------------------------

    def _child_statuses(self, node_id: str) -> list[Status]:
        return [
            Status(kid.status)
            for kid_id in self.nodes.get_children(node_id)
            if (kid := self.nodes.get_node(kid_id)) is not None and kid.kind != NodeKind.DECISION
        ]

    def _propagate(self, node_id: str) -> None:
        """Re-derives every ancestor container's status from its children, inside the caller's
        transaction."""
        child = node_id
        while (parent_id := self._parent(child)) is not None:
            parent = self.node(parent_id)
            current = Status(parent.status)
            derived = rollup(current, self._child_statuses(parent_id))
            if derived == Status.IMPLEMENTED and self._nothing_to_land(parent_id):
                # Code already on its target is never reviewed again.
                derived = Status.COMPLETED
            if derived == current:
                return
            self.nodes.save_node(
                parent.model_copy(update={"status": derived, "updated_at": datetime.now(tz=UTC)})
            )
            if derived in (Status.DEFERRED, Status.ABANDONED):
                self._strand(parent_id, derived)
            self._ledger("rollup", parent_id, {"from": current, "to": derived})
            child = parent_id

    def _nothing_to_land(self, container_id: str) -> bool:
        branch = self.branch_of(container_id)
        ref = self.target_ref(self.target_of(container_id))
        for repo in self.repos_of(container_id):
            repo_dir = self.root / repo
            if gitops.rev_parse(repo_dir, f"refs/heads/{branch}") and not gitops.diff_quiet(
                repo_dir, ref, branch
            ):
                return False
        return True

    # -- writing helpers ----------------------------------------------------------------------

    @staticmethod
    def _with_cycle(node: Node, cycle: Cycle) -> Node:
        return node.model_copy(
            update={
                "status": cycle.status,
                "claimed_from": cycle.claimed_from,
                "outcome": cycle.outcome,
                "fix_for": cycle.fix_for,
                "review_cycles": cycle.review_cycles,
                "merge_attempts": cycle.merge_attempts,
                "step_failures": cycle.step_failures,
                "updated_at": datetime.now(tz=UTC),
            }
        )

    def _section_text(self, node_id: str, key: str) -> str:
        found = self.nodes.get_section(node_id, key)
        return found.content if found else ""

    def _review_hash(self, node_id: str) -> str:
        return hashlib.sha256(self._section_text(node_id, "review").encode()).hexdigest()

    def note(self, node_id: str, key: str, text: str) -> None:
        """Appends a timestamped entry to `node_id:key`, keeping what earlier entries said."""
        existing = self._section_text(node_id, key)
        entry = f"{datetime.now(tz=UTC):%Y-%m-%d %H:%M} {text}"
        self.ops.set_section(node_id, key, f"{existing}\n\n{entry}" if existing else entry)

    def _ledger(self, command: str, node_id: str, payload: dict[str, Any]) -> None:
        self.ops.ledger_repo.append(
            LedgerEvent(
                actor_id=self.ops.actor, command=command, target_id=node_id, payload=payload
            )
        )
```

- [ ] **Step 4: Run the tests and the gates**

```
uv run --directory <worktree> ruff format src/taskmanager/engine/routing.py src/taskmanager/engine/claims.py src/taskmanager/engine/git.py tests/unit/lifecycle_estate.py tests/unit/test_routing.py tests/unit/test_claims.py; echo $?
uv run --directory <worktree> pytest tests/unit/test_routing.py tests/unit/test_claims.py -q; echo $?
uv run --directory <worktree> pytest -q; echo $?
uv run --directory <worktree> ruff check; echo $?
uv run --directory <worktree> ruff format --check; echo $?
uv run --directory <worktree> mypy; echo $?
```

Expected: every command prints `0` last. The first formats only this task's files, so the `--check` after it gates them at the repository's line length.

- [ ] **Step 5: Commit**

```
git -C <worktree> add src/taskmanager/engine/routing.py src/taskmanager/engine/claims.py src/taskmanager/engine/git.py tests/unit/lifecycle_estate.py tests/unit/test_routing.py tests/unit/test_claims.py && git -C <worktree> commit -m "feat(claims): claim, close and repair nodes through one engine that names the next step and its model"
```

### Task 14: Gates, baselines and landing jobs

**Spec:** §3.3, §6.1, §6.2, §6.3, §6.6, §8, §11 (Landing)

**Files:**
- Create: `src/taskmanager/engine/gates.py`
- Create: `src/taskmanager/engine/landing.py`
- Modify: `src/taskmanager/engine/git.py` (adds `merge_no_ff`, `update_ref_cas`, `ls_remote`, `push`, `add_detached_worktree`, `settled`)
- Modify: `src/taskmanager/engine/claims.py` (adds `landed`, `landing_failed`, `landing_blocked`, `park`, `_escalate_red_targets`; `sweep` replaced to escalate red targets; imports `RED_TARGET`)
- Modify: `tests/unit/lifecycle_estate.py` (adds `GATE_SCRIPT`, `junit_gate`, `gate_runs`, `attach_landing`)
- Test: `tests/unit/test_gates.py`
- Test: `tests/unit/test_landing.py`

**Interfaces:**
- Consumes: everything Task 13 produces; `CacheRepository.get_baseline`/`put_baseline`, the frozen dataclass `GateRun` in `taskmanager/core/models.py` (Task 9); `JobRepository.create` (assigns `f"{kind}-{uuid4().hex[:12]}"`)/`acquire_branch`/`release_branch` (Task 9); `ProjectConfig.repos`, `RepoConfig.gates`, `Gate(command, junit, timeout)`, `red_target_decision_after`, `worktree_dir` (Task 11); `NodeRepository.get_conditions`/`add_condition`/`remove_condition` (Task 8).
- Produces: `gates.render`, `gates.template_hash`, `gates.GateRun` (re-export of `core.models.GateRun`), `gates.run_gate`, `gates.attribute`, `gates.red_target_cleared`, `gates.red_target_command`, `gates.RED_TARGET`, `gates.main`; `landing.Landing(root, config, claims, cache, jobs, detach=True)` with `open(root)`, `start_land`, `run`, `resume`; `landing.RESUME_AT`; entry point `python -m taskmanager.engine.landing run <job_id> --root <root>`; `Claims.landed`, `Claims.landing_failed`, `Claims.landing_blocked`, `Claims.park`; `git.merge_no_ff`, `git.update_ref_cas`, `git.ls_remote`, `git.push`, `git.add_detached_worktree`, `git.settled`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/lifecycle_estate.py`, and add `import sys` to its stdlib imports and these to its package imports:

```python
from taskmanager.db.cache_repo import CacheRepository
from taskmanager.engine.config import Gate
from taskmanager.engine.landing import Landing
```

```python
GATE_SCRIPT = """
import pathlib, sys
worktree, log = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
listed = worktree / "failing.txt"
names = listed.read_text().split() if listed.exists() else []
cases = "".join(f'<testcase classname="suite" name="{n}"><failure/></testcase>' for n in names)
(worktree / "report.xml").write_text(
    f'<testsuite><testcase classname="suite" name="ok"/>{cases}</testsuite>'
)
with log.open("a") as out:
    out.write(worktree.name + "\\n")
sys.exit(1 if names else 0)
"""


def junit_gate(tmp_path: Path) -> Gate:
    """A gate that fails the tests named in the worktree's failing.txt, reports them as JUnit
    and logs the worktree it ran in (a baseline's is named `<job>-base`)."""
    script = tmp_path / "gate.py"
    script.write_text(GATE_SCRIPT)
    log = tmp_path / "gate.log"
    return Gate(
        command=f"{sys.executable} {script} {{worktree}} {log}", junit="report.xml", timeout=60
    )


def gate_runs(tmp_path: Path) -> list[str]:
    log = tmp_path / "gate.log"
    return log.read_text().split() if log.exists() else []


def attach_landing(claims: Claims, detach: bool = False) -> Landing:
    """detach=False records jobs without spawning, so a test runs each one in-process."""
    return Landing(
        claims.root, claims.config, claims, CacheRepository(claims.nodes.db), claims.jobs, detach
    )
```

`tests/unit/test_gates.py`:

```python
from pathlib import Path

import pytest
from lifecycle_estate import git, make_repo, push_main

from taskmanager.core import models
from taskmanager.db.cache_repo import CacheRepository
from taskmanager.db.connection import DatabaseManager
from taskmanager.engine import gates
from taskmanager.engine.gates import GateRun

JUNIT = (
    '<testsuite><testcase classname="s" name="a"><failure/></testcase>'
    '<testcase classname="s" name="b"><error/></testcase>'
    '<testcase classname="s" name="c"/></testsuite>'
)


def test_a_gate_run_is_the_type_the_baseline_cache_stores() -> None:
    assert gates.GateRun is models.GateRun


def test_render_quotes_every_value_spliced_into_the_command() -> None:
    rendered = gates.render("run {worktree} --task-id {node}", worktree="/w t", node="T1")
    assert rendered == "run '/w t' --task-id T1"


def test_the_template_hash_names_the_template_not_its_rendering() -> None:
    assert gates.template_hash("make {worktree}") == gates.template_hash("make {worktree}")
    assert gates.template_hash("make {worktree}") != gates.template_hash("make test {worktree}")


def test_a_green_gate_with_no_report_has_no_failing_set(tmp_path: Path) -> None:
    run = gates.run_gate("echo ok", tmp_path, 10, None)
    assert (run.exit_code, run.failing) == (0, None)
    assert "ok" in run.tail


def test_a_red_gate_reads_its_failing_set_from_junit(tmp_path: Path) -> None:
    command = f"mkdir -p out && printf '%s' '{JUNIT}' > out/report.xml; exit 1"
    run = gates.run_gate(command, tmp_path, 10, "out/*.xml")
    assert run.exit_code == 1
    assert run.failing == frozenset({"s::a", "s::b"})


def test_a_report_left_by_an_earlier_run_is_not_read_as_this_one(tmp_path: Path) -> None:
    (tmp_path / "report.xml").write_text(JUNIT)
    run = gates.run_gate("exit 1", tmp_path, 10, "report.xml")
    assert run.failing is None
    assert not (tmp_path / "report.xml").exists()


def test_a_red_gate_whose_report_names_no_failure_is_unattributable(tmp_path: Path) -> None:
    passing = '<testsuite><testcase classname="s" name="a"/></testsuite>'
    command = f"printf '%s' '{passing}' > report.xml; exit 2"
    assert gates.run_gate(command, tmp_path, 10, "report.xml").failing is None


def test_a_gate_past_its_timeout_is_killed_with_its_children_and_exits_124(tmp_path: Path) -> None:
    run = gates.run_gate("sleep 30 & sleep 30; wait", tmp_path, 1, None)
    assert run.exit_code == 124
    assert "timed out" in run.tail


def _run(code: int, failing: str | None = None) -> GateRun:
    return GateRun(code, None if failing is None else frozenset(failing.split()), "")


@pytest.mark.parametrize(
    ("tip", "base", "verdict"),
    [
        (_run(0), _run(1, "a"), "push"),
        (_run(1, "a"), _run(0, ""), "own_defect"),
        (_run(1), _run(1), "unattributed"),
        (_run(1, "a"), _run(1), "unattributed"),
        (_run(1), _run(1, "a"), "unattributed"),
        (_run(1, "a"), _run(1, "a b"), "push"),
        (_run(1, "a"), _run(1, "a"), "red_target"),
        (_run(1, "a c"), _run(1, "a"), "own_defect"),
        (_run(1, "c"), _run(1, "a"), "own_defect"),
    ],
)
def test_a_red_tip_is_attributed_against_the_baseline(
    tip: GateRun, base: GateRun, verdict: str
) -> None:
    assert gates.attribute(tip, base) == verdict


@pytest.fixture
def red(tmp_path: Path) -> tuple[CacheRepository, Path, str]:
    root = tmp_path / "estate"
    root.mkdir()
    api = make_repo(root, "api")
    db = DatabaseManager(root / ".taskmanager")
    db.init_all()
    return CacheRepository(db), api, git(api, "rev-parse", "origin/main")


PARKED = GateRun(1, frozenset({"s::a"}), "")


@pytest.mark.parametrize(
    ("moves", "later", "cleared"),
    [
        (False, None, False),
        (True, None, True),
        (True, GateRun(0, frozenset(), ""), True),
        (True, GateRun(1, frozenset({"s::a", "s::b"}), ""), False),
        (True, GateRun(1, frozenset({"s::b"}), ""), True),
    ],
)
def test_a_red_target_clears_once_main_moves_past_the_parked_failures(
    red: tuple[CacheRepository, Path, str], moves: bool, later: GateRun | None, cleared: bool
) -> None:
    cache, api, sha = red
    cache.put_baseline("api", sha, "h", PARKED)
    if moves:
        current = push_main(api, "next.txt", "n\n")
        if later is not None:
            cache.put_baseline("api", current, "h", later)
    assert gates.red_target_cleared(cache, api, "api", sha, "h") is cleared


def test_the_red_target_entry_point_exits_zero_only_once_cleared(
    red: tuple[CacheRepository, Path, str], tmp_path: Path
) -> None:
    cache, api, sha = red
    cache.put_baseline("api", sha, "h", PARKED)
    args = ["red-target", "--root", str(tmp_path / "estate"), "--repo", "api", "--sha", sha]
    args += ["--template-hash", "h"]
    assert gates.main(args) == 1
    push_main(api, "next.txt", "n\n")
    assert gates.main(args) == 0
```

`tests/unit/test_landing.py`:

```python
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from lifecycle_estate import (
    add,
    attach_landing,
    branch_at,
    gate_runs,
    git,
    junit_gate,
    make_estate,
    on_branch,
    push_main,
    section,
    stored,
)

from taskmanager.core.enums import NodeKind, VerificationType
from taskmanager.core.models import Condition, NodeVerification
from taskmanager.core.status import Action, ConditionStage, JobState, Merge, Outcome, Status
from taskmanager.engine.claims import Claims
from taskmanager.engine.config import Gate, ProjectConfig, RepoConfig
from taskmanager.engine.landing import Landing
from taskmanager.engine.operations import OperationError

TRUE = Gate(command="true", junit=None, timeout=60)


def estate_with(tmp_path: Path, gate: Gate | None, **config: object) -> tuple[Claims, Landing]:
    repos = {"api": RepoConfig(gates={"main": gate})} if gate is not None else {}
    claims = make_estate(tmp_path, config=ProjectConfig(repos=repos, **config))
    return claims, attach_landing(claims)


def reviewed_task(
    claims: Claims, node_id: str = "T1", path: str = "feature.py", content: str = "x = 1\n"
) -> None:
    add(claims, node_id, status=Status.REVIEWED, outcome=Outcome.APPROVE, review_cycles=1)
    on_branch(claims.root / "api", f"tm/{node_id}", path, content)


def verification(claims: Claims, node_id: str, path: str) -> None:
    claims.nodes.add_verification(
        NodeVerification(
            node_id=node_id, verification_type=VerificationType.FILE_EXISTS, target_path=path
        )
    )


def land(claims: Claims, landing: Landing, node_id: str = "T1") -> tuple[str, JobState]:
    result = claims.start(node_id, "merger", "s1")
    assert result.action == Action.MERGE, result.reason
    assert result.job is not None
    return result.job, landing.run(result.job)


def merges_of(api: Path, node_id: str) -> int:
    subjects = git(api, "log", "--first-parent", "--format=%s", "origin/main").splitlines()
    return subjects.count(f"merge({node_id}): land tm/{node_id} on main")


def test_a_clean_landing_merges_gates_pushes_verifies_and_completes(tmp_path: Path) -> None:
    claims, landing = estate_with(tmp_path, TRUE)
    api = claims.root / "api"
    reviewed_task(claims)
    verification(claims, "T1", "api/feature.py")

    job_id, state = land(claims, landing)

    assert job_id.startswith("land-")
    assert state == JobState.SUCCEEDED
    assert stored(claims, "T1").status == Status.COMPLETED
    assert claims.runtime.get_lease("T1") is None
    assert git(api, "log", "-1", "--format=%s", "origin/main") == "merge(T1): land tm/T1 on main"
    assert git(api, "show", "origin/main:feature.py") == "x = 1"
    job = claims.jobs.get(job_id)
    assert job is not None and job.worktree is None
    assert "PASS" in section(claims, "T1", "merge")


def test_a_branch_already_on_its_target_completes_without_a_new_commit(tmp_path: Path) -> None:
    claims, landing = estate_with(tmp_path, TRUE)
    api = claims.root / "api"
    add(claims, "T1", status=Status.REVIEWED, outcome=Outcome.APPROVE)
    branch_at(api, "tm/T1")
    before = git(api, "ls-remote", "origin", "refs/heads/main")

    assert land(claims, landing)[1] == JobState.SUCCEEDED
    assert stored(claims, "T1").status == Status.COMPLETED
    assert git(api, "ls-remote", "origin", "refs/heads/main") == before


def test_a_conflict_is_handed_to_an_agent_who_resolves_commits_and_resumes(tmp_path: Path) -> None:
    claims, landing = estate_with(tmp_path, TRUE)
    api = claims.root / "api"
    reviewed_task(claims, path="app.py", content="branch\n")
    push_main(api, "app.py", "main\n")

    job_id, state = land(claims, landing)

    assert state == JobState.NEEDS_AGENT
    job = claims.jobs.get(job_id)
    assert job is not None and job.result["reason"] == "conflict"
    lease = claims.runtime.get_lease("T1")
    assert lease is not None and lease.ttl_seconds is None
    assert stored(claims, "T1").status == Status.MERGING

    handed = claims.start("T1", "resolver", "s2")
    assert (handed.action, handed.job, handed.worktree) == (Action.MERGE, job_id, job.worktree)
    assert handed.worktree is not None
    worktree = Path(handed.worktree)
    with pytest.raises(OperationError, match="not committed"):
        landing.resume(job_id)
    (worktree / "app.py").write_text("both\n")
    git(worktree, "add", "app.py")
    git(worktree, "commit", "-q", "--no-edit")

    assert landing.resume(job_id) == JobState.SUCCEEDED
    assert stored(claims, "T1").status == Status.COMPLETED
    assert git(api, "show", "origin/main:app.py") == "both"


MOVER = """
import pathlib, subprocess, sys
api, flag, log = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3])
with log.open("a") as out:
    out.write("gate\\n")
if not flag.exists():
    flag.touch()
    subprocess.run(["git", "-C", str(api), "push", "-q", "origin", "side:main"], check=True)
"""


def test_a_target_that_moves_during_the_gate_is_merged_in_and_gated_again(tmp_path: Path) -> None:
    script = tmp_path / "mover.py"
    script.write_text(MOVER)
    api_path = tmp_path / "estate" / "api"
    command = f"{sys.executable} {script} {api_path} {tmp_path / 'moved'} {tmp_path / 'gate.log'}"
    claims, landing = estate_with(tmp_path, Gate(command=command, junit=None, timeout=60))
    api = claims.root / "api"
    reviewed_task(claims)
    side = on_branch(api, "side", "other.py", "y = 2\n")

    assert land(claims, landing)[1] == JobState.SUCCEEDED
    assert gate_runs(tmp_path) == ["gate", "gate"]
    git(api, "merge-base", "--is-ancestor", side, "origin/main")
    assert git(api, "show", "origin/main:feature.py") == "x = 1"


@pytest.mark.parametrize(
    ("on_main", "on_tip", "state", "status", "attempts"),
    [
        ("a b", "a", JobState.SUCCEEDED, Status.COMPLETED, 0),
        ("a", "a", JobState.CONDITION_UNMET, Status.REVIEWED, 0),
        ("a", "a c", JobState.OWN_DEFECT, Status.REVIEWED, 1),
        ("", "c", JobState.OWN_DEFECT, Status.REVIEWED, 1),
    ],
)
def test_a_red_tip_is_pushed_parked_or_charged_by_its_junit_set_against_the_baseline(
    tmp_path: Path, on_main: str, on_tip: str, state: JobState, status: Status, attempts: int
) -> None:
    claims, landing = estate_with(tmp_path, junit_gate(tmp_path))
    api = claims.root / "api"
    if on_main:
        push_main(api, "failing.txt", on_main + "\n")
    reviewed_task(claims)
    if on_tip != on_main:
        on_branch(api, "tm/T1", "failing.txt", on_tip + "\n")

    _, result = land(claims, landing)

    node = stored(claims, "T1")
    assert (result, node.status, node.merge_attempts, node.step_failures) == (
        state,
        status,
        attempts,
        0,
    )
    if attempts:
        assert node.outcome == Outcome.MERGE_FAILED
    parked = [c for c in claims.nodes.get_conditions("T1") if c.needs.startswith("red-target")]
    assert len(parked) == (1 if state == JobState.CONDITION_UNMET else 0)


def test_one_baseline_run_serves_every_landing_on_the_same_target_sha(tmp_path: Path) -> None:
    claims, landing = estate_with(tmp_path, junit_gate(tmp_path))
    push_main(claims.root / "api", "failing.txt", "a\n")
    reviewed_task(claims, "T1")
    reviewed_task(claims, "T2", path="other.py")

    assert land(claims, landing, "T1")[1] == JobState.CONDITION_UNMET
    assert land(claims, landing, "T2")[1] == JobState.CONDITION_UNMET

    runs = gate_runs(tmp_path)
    assert len(runs) == 3
    assert sum(run.endswith("-base") for run in runs) == 1


def test_a_landing_parked_on_a_red_main_waits_until_main_moves_then_lands(tmp_path: Path) -> None:
    claims, landing = estate_with(tmp_path, junit_gate(tmp_path), condition_ttl=1)
    api = claims.root / "api"
    push_main(api, "failing.txt", "a\n")
    reviewed_task(claims)
    assert land(claims, landing)[1] == JobState.CONDITION_UNMET

    blocked = claims.start("T1", "merger", "s1")
    assert blocked.action == Action.BLOCKED
    assert "red-target" in (blocked.reason or "")

    push_main(api, "failing.txt", None)
    time.sleep(1.1)
    assert land(claims, landing)[1] == JobState.SUCCEEDED
    assert claims.nodes.get_conditions("T1") == []


def test_landings_parked_on_one_red_main_past_the_threshold_reach_the_owner_as_one_decision(
    tmp_path: Path,
) -> None:
    claims, landing = estate_with(tmp_path, junit_gate(tmp_path))
    push_main(claims.root / "api", "failing.txt", "a\n")
    reviewed_task(claims, "T1")
    reviewed_task(claims, "T2", path="other.py")
    land(claims, landing, "T1")
    land(claims, landing, "T2")
    two_hours_ago = (datetime.now(tz=UTC) - timedelta(hours=2)).isoformat()
    for node_id in ("T1", "T2"):
        for job in claims.jobs.for_node(node_id):
            job.result["red_target"]["since"] = two_hours_ago
            claims.jobs.update(job)

    claims.sweep()
    claims.sweep()

    decisions = {
        dep
        for node_id in ("T1", "T2")
        for dep in claims.nodes.get_dependencies(node_id)
        if stored(claims, dep).kind == NodeKind.DECISION
    }
    assert len(decisions) == 1
    assert all(next(iter(decisions)) in claims.nodes.get_dependencies(n) for n in ("T1", "T2"))


def test_an_unattributed_red_is_handed_to_an_agent_who_may_push_it(tmp_path: Path) -> None:
    claims, landing = estate_with(tmp_path, Gate(command="exit 1", junit=None, timeout=60))
    reviewed_task(claims)

    job_id, state = land(claims, landing)

    assert state == JobState.NEEDS_AGENT
    job = claims.jobs.get(job_id)
    assert job is not None
    assert job.result["reason"] == "unattributed"
    assert "tip" in job.result and "base" in job.result
    assert claims.start("T1", "judge", "s2").action == Action.MERGE
    assert landing.resume(job_id, push=True) == JobState.SUCCEEDED
    assert stored(claims, "T1").status == Status.COMPLETED


def test_a_verification_red_after_landing_is_an_own_defect(tmp_path: Path) -> None:
    claims, landing = estate_with(tmp_path, TRUE)
    api = claims.root / "api"
    reviewed_task(claims)
    verification(claims, "T1", "api/missing.py")

    assert land(claims, landing)[1] == JobState.OWN_DEFECT

    node = stored(claims, "T1")
    assert (node.status, node.outcome, node.merge_attempts) == (
        Status.REVIEWED,
        Outcome.MERGE_FAILED,
        1,
    )
    assert git(api, "show", "origin/main:feature.py") == "x = 1"
    assert "FAIL" in section(claims, "T1", "merge")


def test_a_landing_killed_after_its_push_is_swept_back_and_the_next_completes_without_a_second_merge(
    tmp_path: Path,
) -> None:
    claims, landing = estate_with(tmp_path, TRUE)
    api = claims.root / "api"
    reviewed_task(claims)
    first = claims.start("T1", "merger", "s1", ttl=1)
    assert first.job is not None
    killed = tmp_path / "killed"
    git(api, "worktree", "add", "-q", "--detach", str(killed), "origin/main")
    git(killed, "merge", "-q", "--no-ff", "-m", "merge(T1): land tm/T1 on main", "tm/T1")
    git(killed, "push", "-q", "origin", "HEAD:main")
    time.sleep(1.5)

    assert claims.sweep() == ["T1"]
    dead = claims.jobs.get(first.job)
    assert dead is not None and dead.state == JobState.EXPIRED
    node = stored(claims, "T1")
    assert (node.status, node.step_failures) == (Status.REVIEWED, 1)

    second = claims.start("T1", "merger", "s1")
    assert second.job is not None
    assert landing.run(second.job) == JobState.SUCCEEDED
    assert stored(claims, "T1").status == Status.COMPLETED
    git(api, "fetch", "-q", "origin")
    assert merges_of(api, "T1") == 1


def test_the_module_entry_point_runs_a_landing_as_a_detached_process(tmp_path: Path) -> None:
    claims = make_estate(
        tmp_path, config=ProjectConfig(repos={"api": RepoConfig(gates={"main": TRUE})})
    )
    attach_landing(claims, detach=True)
    reviewed_task(claims)

    result = claims.start("T1", "merger", "s1")
    assert result.job is not None
    deadline = time.monotonic() + 60
    job = claims.jobs.get(result.job)
    while job is not None and job.state == JobState.RUNNING and time.monotonic() < deadline:
        time.sleep(0.2)
        job = claims.jobs.get(result.job)

    log = claims.root / ".taskmanager" / "jobs" / f"{result.job}.log"
    assert job is not None and job.state == JobState.SUCCEEDED, log.read_text()
    assert stored(claims, "T1").status == Status.COMPLETED


def test_a_landing_condition_unmet_when_the_job_runs_returns_the_node_without_counting(
    tmp_path: Path,
) -> None:
    claims, landing = estate_with(tmp_path, TRUE)
    reviewed_task(claims)
    result = claims.start("T1", "merger", "s1")
    assert result.job is not None
    claims.nodes.add_condition(
        Condition(
            node_id="T1",
            idx=0,
            needs="release window open",
            command=f"test -f {tmp_path / 'window'}",
            stage=ConditionStage.LANDING,
        )
    )

    assert landing.run(result.job) == JobState.CONDITION_UNMET

    node = stored(claims, "T1")
    assert (node.status, node.step_failures, node.merge_attempts) == (Status.REVIEWED, 0, 0)
    again = claims.start("T1", "merger", "s1")
    assert again.action == Action.BLOCKED
    assert "release window open" in (again.reason or "")


def test_a_repository_with_no_main_gate_stops_for_an_agent(tmp_path: Path) -> None:
    claims, landing = estate_with(tmp_path, None)
    reviewed_task(claims)

    job_id, state = land(claims, landing)

    job = claims.jobs.get(job_id)
    assert state == JobState.NEEDS_AGENT
    assert job is not None and job.result["reason"] == "no gate"


def parent_landing(claims: Claims, **columns: object) -> Path:
    api = claims.root / "api"
    add(claims, "P", NodeKind.PLAN, review=True, fix=True)
    add(
        claims,
        "T1",
        parent="P",
        merge=Merge.PARENT,
        status=Status.REVIEWED,
        review_cycles=1,
        **{"outcome": Outcome.APPROVE, **columns},
    )
    branch_at(api, "tm/P")
    on_branch(api, "tm/T1", "feature.py", "x = 1\n", base="tm/P")
    return api


def test_a_child_lands_on_its_parent_branch_after_its_own_verifications(tmp_path: Path) -> None:
    claims, landing = estate_with(tmp_path, None)
    api = parent_landing(claims)
    verification(claims, "T1", "api/feature.py")
    main_before = git(api, "ls-remote", "origin", "refs/heads/main")

    assert land(claims, landing)[1] == JobState.SUCCEEDED

    assert git(api, "show", "tm/P:feature.py") == "x = 1"
    assert git(api, "ls-remote", "origin", "refs/heads/main") == main_before
    assert stored(claims, "T1").status == Status.COMPLETED
    assert stored(claims, "P").status == Status.IMPLEMENTED


@pytest.mark.parametrize(
    ("fix", "outcome", "state"),
    [
        (True, Outcome.APPROVE, JobState.OWN_DEFECT),
        (False, Outcome.REJECT, JobState.SUCCEEDED),
    ],
)
def test_own_verifications_stop_a_parent_landing_unless_nobody_below_fixes_a_rejection(
    tmp_path: Path, fix: bool, outcome: Outcome, state: JobState
) -> None:
    claims, landing = estate_with(tmp_path, None)
    parent_landing(claims, fix=fix, outcome=outcome)
    verification(claims, "T1", "api/missing.py")

    assert land(claims, landing)[1] == state
    assert "FAIL" in section(claims, "T1", "merge")


def test_a_parent_branch_that_moves_before_the_swap_is_merged_in_and_gated_again(
    tmp_path: Path,
) -> None:
    claims, landing = estate_with(tmp_path, None)
    api = parent_landing(claims)
    side = on_branch(api, "side", "side.py", "s = 1\n", base="tm/P")
    flag = tmp_path / "moved"
    move = f"test -f {flag} || {{ touch {flag}; git -C {api} update-ref refs/heads/tm/P {side}; }}"
    claims.nodes.add_verification(
        NodeVerification(
            node_id="T1",
            verification_type=VerificationType.TEST_COMMAND,
            target_path="moves tm/P once",
            expected_pattern=move,
        )
    )

    assert land(claims, landing)[1] == JobState.SUCCEEDED

    tip = git(api, "rev-parse", "tm/P")
    git(api, "merge-base", "--is-ancestor", side, tip)
    git(api, "merge-base", "--is-ancestor", "tm/T1", tip)
```

- [ ] **Step 2: Run them and watch them fail**

```
uv run --directory <worktree> pytest tests/unit/test_gates.py tests/unit/test_landing.py -q; echo $?
```

Expected: collection errors `ModuleNotFoundError: No module named 'taskmanager.engine.gates'` and `ModuleNotFoundError: No module named 'taskmanager.engine.landing'` (through `lifecycle_estate`), exit code 2. `tests/unit/test_claims.py` fails to collect for the same reason until Step 3.

- [ ] **Step 3: Implement**

Append to `src/taskmanager/engine/git.py`:

```python
def merge_no_ff(worktree: Path, ref: str, subject: str) -> bool:
    """False on a conflict, leaving the merge in progress for an agent to resolve."""
    return _git(worktree, "merge", "--no-ff", "--no-edit", "-m", subject, ref).returncode == 0


def update_ref_cas(repo: Path, ref: str, new: str, old: str) -> bool:
    """Moves `ref` to `new` only if it still points at `old`."""
    return _git(repo, "update-ref", ref, new, old).returncode == 0


def ls_remote(repo: Path, ref: str) -> str:
    """The sha `origin` holds for `ref`, or "" when it cannot be read."""
    res = _git(repo, "ls-remote", "origin", ref)
    fields = res.stdout.split()
    return fields[0] if res.returncode == 0 and fields else ""


def push(worktree: Path, target: str) -> bool:
    """Never forced: a refused push means the target moved, and the caller merges it in."""
    return _git(worktree, "push", "-q", "origin", f"HEAD:refs/heads/{target}").returncode == 0


def add_detached_worktree(repo: Path, path: Path, commit: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _run(repo, "worktree", "add", "--detach", str(path), commit)


def settled(worktree: Path, ref: str) -> bool:
    """A handed-over merge is resolved: none in progress, nothing uncommitted, `ref` merged."""
    return (
        not rev_parse(worktree, "MERGE_HEAD")
        and _git(worktree, "status", "--porcelain").stdout.strip() == ""
        and is_ancestor(worktree, ref, "HEAD")
    )
```

`src/taskmanager/engine/gates.py`:

```python
"""Gate commands, the failing set a JUnit report names, and the attribution of a red tip."""

import argparse
import glob
import hashlib
import os
import shlex
import signal
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Literal

from taskmanager.core.models import GateRun as GateRun
from taskmanager.db.cache_repo import CacheRepository
from taskmanager.db.connection import DatabaseManager
from taskmanager.engine import git as gitops

RED_TARGET = "red-target"
TAIL_CHARS = 4000
TIMEOUT_EXIT = 124

Attribution = Literal["push", "own_defect", "red_target", "unattributed"]


def render(template: str, **values: str) -> str:
    # The values are paths and ids spliced into a shell command line.
    return template.format(**{key: shlex.quote(value) for key, value in values.items()})


def template_hash(template: str) -> str:
    """The baseline cache key: the template, so every node shares one baseline per sha."""
    return hashlib.sha256(template.encode()).hexdigest()[:16]


def _reports(cwd: Path, junit_glob: str | None) -> list[Path]:
    if not junit_glob:
        return []
    return sorted(Path(p) for p in glob.glob(str(cwd / junit_glob), recursive=True))


def _failing(reports: list[Path]) -> frozenset[str]:
    failing: set[str] = set()
    for report in reports:
        for case in ET.parse(report).getroot().iter("testcase"):
            if case.find("failure") is not None or case.find("error") is not None:
                failing.add(f"{case.get('classname', '')}::{case.get('name', '')}")
    return frozenset(failing)


def run_gate(command: str, cwd: Path, timeout: int, junit_glob: str | None) -> GateRun:
    # A report an earlier run left in this worktree would be read as this run's.
    for stale in _reports(cwd, junit_glob):
        stale.unlink()
    proc = subprocess.Popen(
        command,
        shell=True,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        output, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # The gate's own children would outlive a kill of the shell alone and hold its pipe.
        os.killpg(proc.pid, signal.SIGKILL)
        output, _ = proc.communicate()
        return GateRun(TIMEOUT_EXIT, None, f"{output}\ntimed out after {timeout}s"[-TAIL_CHARS:])
    reports = _reports(cwd, junit_glob)
    failing: frozenset[str] | None
    try:
        failing = _failing(reports) if reports else None
    except ET.ParseError:
        failing = None
    if proc.returncode != 0 and not failing:
        # A red run whose report names no failure failed where the report does not look (a
        # build, a crash), so its set says nothing about which tests broke.
        failing = None
    return GateRun(proc.returncode, failing, output[-TAIL_CHARS:])


def attribute(tip: GateRun, base: GateRun) -> Attribution:
    if tip.exit_code == 0:
        return "push"
    if base.exit_code == 0:
        return "own_defect"
    if tip.failing is None or base.failing is None:
        return "unattributed"
    if tip.failing - base.failing:
        return "own_defect"
    if tip.failing < base.failing:
        return "push"
    return "red_target"


def red_target_cleared(
    cache: CacheRepository, repo_dir: Path, repo: str, sha: str, template_hash: str
) -> bool:
    """What a landing parked on a red `main` waits on: `main` moved past `sha`, and the baseline
    at the new sha, if one ran, no longer fails the parked set. An unreadable remote is not
    cleared."""
    current = gitops.ls_remote(repo_dir, "refs/heads/main")
    if not current or current == sha:
        return False
    parked = cache.get_baseline(repo, sha, template_hash)
    later = cache.get_baseline(repo, current, template_hash)
    if parked is None or parked.failing is None or later is None or later.failing is None:
        return True
    if later.exit_code == 0:
        return True
    return not parked.failing <= later.failing


def red_target_command(root: Path, repo: str, sha: str, template_hash: str) -> str:
    """The condition command tm stores for a parked landing: tm itself evaluating the rule."""
    parts = [sys.executable, "-m", "taskmanager.engine.gates", RED_TARGET, "--root", str(root)]
    parts += ["--repo", repo, "--sha", sha, "--template-hash", template_hash]
    return " ".join(shlex.quote(part) for part in parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m taskmanager.engine.gates")
    commands = parser.add_subparsers(dest="command", required=True)
    red = commands.add_parser(RED_TARGET, help="exit 0 once a parked red main has cleared")
    red.add_argument("--root", type=Path, required=True)
    red.add_argument("--repo", required=True)
    red.add_argument("--sha", required=True)
    red.add_argument("--template-hash", required=True)
    args = parser.parse_args(argv)
    cache = CacheRepository(DatabaseManager(args.root / ".taskmanager"))
    cleared = red_target_cleared(
        cache, args.root / args.repo, args.repo, args.sha, args.template_hash
    )
    return 0 if cleared else 1


if __name__ == "__main__":
    sys.exit(main())
```

`src/taskmanager/engine/landing.py`:

```python
"""Landing jobs: tm merges, gates, pushes and verifies a node, as a detached process.

A landing can outlast the command runner's limit, so `tm task start` only records the job and
spawns `python -m taskmanager.engine.landing run <job> --root <root>`. An agent is handed the job
only when it stops at `needs_agent`.
"""

import argparse
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from taskmanager.core.models import Condition, Job
from taskmanager.core.status import ConditionStage, JobKind, JobState, Outcome
from taskmanager.db.cache_repo import CacheRepository
from taskmanager.db.job_repo import JobRepository
from taskmanager.engine import gates
from taskmanager.engine import git as gitops
from taskmanager.engine.claims import Claims
from taskmanager.engine.config import Gate, ProjectConfig
from taskmanager.engine.gates import GateRun
from taskmanager.engine.git import GitManager
from taskmanager.engine.operations import OperationError

HEARTBEAT_SECONDS = 30
PUSH_TRIES = 3
LOCK_WAIT_SECONDS = 120
LIVE = frozenset({JobState.RUNNING, JobState.NEEDS_AGENT})

# Where a resumed job picks up, by the reason it stopped. A resolved conflict is gated like any
# merge; an `error` starts over, since nothing after it can be trusted.
RESUME_AT = {
    "conflict": "gate",
    "unattributed": "gate",
    "no gate": "gate",
    "red": "gate",
    "push_failed": "push",
    "branch_locked": "push",
    "error": "start",
}


class Landing:
    def __init__(
        self,
        root: Path,
        config: ProjectConfig,
        claims: Claims,
        cache: CacheRepository,
        jobs: JobRepository,
        detach: bool = True,
    ) -> None:
        self.root = root
        self.config = config
        self.claims = claims
        self.cache = cache
        self.jobs = jobs
        self.detach = detach
        claims.landing = self

    @classmethod
    def open(cls, root: Path) -> Landing:
        claims = Claims.open(root)
        return cls(root, claims.config, claims, CacheRepository(claims.nodes.db), claims.jobs)

    # -- entry points -------------------------------------------------------------------------

    def start_land(self, node_id: str) -> str:
        repos = self.claims.repos_of(node_id)
        job = self._new_job(JobKind.LAND, node_id, repos[0], self.claims.target_of(node_id), {})
        self._launch(job)
        return job.id

    def run(self, job_id: str) -> JobState:
        job = self._job(job_id)
        if job.state != JobState.RUNNING:
            return job.state
        job.pid = os.getpid()
        self.jobs.update(job)
        with self._beating(job.node_id):
            try:
                return self._land(job)
            except (subprocess.CalledProcessError, OSError, OperationError) as exc:
                detail = f"{exc}\n{getattr(exc, 'stderr', '') or ''}".strip()
                return self._needs_agent(job, "error", error=detail)

    def resume(self, job_id: str, own_defect: str | None = None, push: bool = False) -> JobState:
        job = self._job(job_id)
        if job.state != JobState.NEEDS_AGENT:
            raise OperationError(
                f"job {job_id} is {job.state}; only a job waiting for an agent resumes", 409
            )
        lease = self.claims.runtime.get_lease(job.node_id)
        if lease is None or lease.ttl_seconds is None:
            raise OperationError(
                f"job {job_id} is not handed to an agent: take it with "
                f"`tm task start {job.node_id}`",
                409,
            )
        reason = str(job.result.get("reason"))
        if own_defect is not None:
            if job.kind == JobKind.SYNC:
                raise OperationError(
                    "a sync has no own defect: resolve it and resume, or release the node", 400
                )
            return self._own_defect(job, own_defect)
        if push and reason != "unattributed":
            raise OperationError("--push answers an unattributed red only", 400)
        if (
            reason == "conflict"
            and job.worktree
            and not gitops.settled(Path(job.worktree), self._merging(job))
        ):
            raise OperationError(
                f"the merge in {job.worktree} is not committed: resolve every path and commit, "
                "then resume",
                409,
            )
        step = "push" if push else RESUME_AT.get(reason, "start")
        if step == "start":
            self._remove_worktree(job)
        job.step = step
        job.state = JobState.RUNNING
        job.result.pop("push_tries", None)
        self.jobs.update(job)
        self.claims.heartbeat(job.node_id)
        if self.detach:
            self._launch(job)
            return JobState.RUNNING
        return self.run(job.id)

    # -- the landing steps --------------------------------------------------------------------

    def _land(self, job: Job) -> JobState:
        steps = {
            "start": self._build,
            "gate": self._gate,
            "push": self._push,
            "verify": self._verify,
        }
        while True:
            nxt = steps[job.step](job)
            if isinstance(nxt, JobState):
                return nxt
            job.step = nxt
            job.heartbeat = datetime.now(tz=UTC)
            self.jobs.update(job)

    def _build(self, job: Job) -> str | JobState:
        repo_dir, branch = self._dir(job), self.claims.branch_of(job.node_id)
        target = self._target_ref(job)
        if (
            not gitops.rev_parse(repo_dir, f"refs/heads/{branch}")
            or gitops.is_ancestor(repo_dir, branch, target)
            or gitops.diff_quiet(repo_dir, target, branch)
        ):
            # A landing killed after its push, a repository with nothing to land, and a partial
            # container landing retried all resume here.
            job.result["already_landed"] = True
            return "verify"
        unmet = self.claims.conditions.unmet(job.node_id, ConditionStage.LANDING)
        self._drop_cleared_red_targets(job.node_id, unmet)
        if unmet:
            return self._blocked(job, "landing waits on " + "; ".join(c.needs for c in unmet))
        base = gitops.rev_parse(repo_dir, target)
        worktree = self._worktree_path(job)
        gitops.add_detached_worktree(repo_dir, worktree, base)
        job.worktree = str(worktree)
        job.result["base_sha"] = base
        if not gitops.merge_no_ff(worktree, branch, self._subject(job)):
            return self._needs_agent(job, "conflict")
        return "gate"

    def _gate(self, job: Job) -> str | JobState:
        worktree = Path(self._require_worktree(job))
        gate: Gate | None
        if job.target == "main":
            gate = self._gate_config(job.repo, "main")
            if gate is None:
                return self._needs_agent(
                    job,
                    "no gate",
                    detail=f"repos.{job.repo}.gates.main is not configured: a repository with "
                    "no main gate cannot land on main",
                )
        else:
            # A node's own verification is red on its target by construction, so it has no
            # baseline: red is an own defect.
            passed, report = self.claims.verify(
                job.node_id, gitops.rev_parse(worktree, "HEAD"), job.repo
            )
            job.result["own_verify"] = report
            if not passed and not self._advisory(job.node_id):
                return self._own_defect(job, f"own verifications red at the merge:\n{report}")
            gate = self._gate_config(job.repo, "parent")
            if gate is None:
                return "push"
        tip = gates.run_gate(self._render(gate, job, worktree), worktree, gate.timeout, gate.junit)
        if tip.exit_code == 0:
            return "push"
        base = self._baseline(job, gate)
        verdict = gates.attribute(tip, base)
        if verdict == "push":
            return "push"
        if verdict == "own_defect":
            added = sorted((tip.failing or frozenset()) - (base.failing or frozenset()))
            why = f"adding {', '.join(added)}" if added else f"green on {job.target}"
            return self._own_defect(job, f"gate red at the tip, {why}:\n{tip.tail}")
        if verdict == "red_target":
            return self._park_red_target(job, gate, base)
        return self._needs_agent(job, "unattributed", tip=tip.tail, base=base.tail)

    def _push(self, job: Job) -> str | JobState:
        worktree = Path(self._require_worktree(job))
        if job.target != "main":
            return self._move_branch(job, worktree, "verify")
        repo_dir = self._dir(job)
        while int(job.result.get("push_tries", 0)) < PUSH_TRIES:
            remote = gitops.ls_remote(repo_dir, "refs/heads/main")
            if remote and remote != job.result["base_sha"]:
                # The full gate runs at the tip that is pushed, so a moved main is merged in and
                # gated again.
                gitops.fetch(repo_dir)
                subject = f"merge({job.node_id}): origin/main into its landing"
                if not gitops.merge_no_ff(worktree, "origin/main", subject):
                    return self._needs_agent(job, "conflict")
                job.result["base_sha"] = gitops.rev_parse(repo_dir, "origin/main")
                return "gate"
            if remote and gitops.push(worktree, "main"):
                return "verify"
            job.result["push_tries"] = int(job.result.get("push_tries", 0)) + 1
        return self._needs_agent(job, "push_failed")

    def _move_branch(self, job: Job, worktree: Path, done: str) -> str | JobState:
        """Moves a local container branch to the worktree's HEAD by compare-and-swap, under the
        branch's lock; a branch that moved meanwhile is merged in and gated again."""
        tries = int(job.result.get("push_tries", 0))
        if tries >= PUSH_TRIES:
            return self._needs_agent(job, "push_failed")
        if not self._lock(job):
            return self._needs_agent(job, "branch_locked")
        ref = f"refs/heads/{job.target}"
        repo_dir = self._dir(job)
        try:
            head = gitops.rev_parse(worktree, "HEAD")
            if gitops.update_ref_cas(repo_dir, ref, head, str(job.result["base_sha"])):
                return done
            moved = gitops.rev_parse(repo_dir, ref)
        finally:
            self.jobs.release_branch(job.repo, job.target, job.id)
        job.result["push_tries"] = tries + 1
        if not gitops.merge_no_ff(worktree, moved, f"merge({job.node_id}): {job.target} moved"):
            return self._needs_agent(job, "conflict")
        job.result["base_sha"] = moved
        return "gate"

    def _verify(self, job: Job) -> JobState:
        passed, report = self.claims.verify(job.node_id, self._target_ref(job), job.repo)
        if not passed and not self._advisory(job.node_id):
            # The code is on the target but the node's assertion does not hold there.
            return self._own_defect(
                job, f"verifications red on {job.target} after landing:\n{report}"
            )
        self._remove_worktree(job)
        return self._succeed(job, report)

    def _succeed(self, job: Job, report: str) -> JobState:
        if not self._end(job, JobState.SUCCEEDED, verify=report):
            return self._state(job)
        self.claims.landed(job.node_id, f"{job.repo}: landed on {job.target}\n{report}")
        return JobState.SUCCEEDED

    # -- how a job ends -----------------------------------------------------------------------

    def _end(self, job: Job, state: JobState, **result: Any) -> bool:
        """False when the job was expired under us (its lease swept or released): the node is no
        longer this job's to move."""
        current = self.jobs.get(job.id)
        if current is None or current.state not in LIVE:
            return False
        job.state = state
        job.result.update(result)
        job.heartbeat = datetime.now(tz=UTC)
        self.jobs.update(job)
        return True

    def _state(self, job: Job) -> JobState:
        current = self.jobs.get(job.id)
        return current.state if current is not None else job.state

    def _needs_agent(self, job: Job, reason: str, **detail: str) -> JobState:
        if not self._end(job, JobState.NEEDS_AGENT, reason=reason, **detail):
            return self._state(job)
        self.claims.park(job.node_id)
        return JobState.NEEDS_AGENT

    def _own_defect(self, job: Job, finding: str) -> JobState:
        self._remove_worktree(job)
        if not self._end(job, JobState.OWN_DEFECT, finding=finding):
            return self._state(job)
        self.claims.landing_failed(job.node_id, f"{job.repo}: {finding}")
        return JobState.OWN_DEFECT

    def _blocked(self, job: Job, why: str, **extra: Any) -> JobState:
        self._remove_worktree(job)
        if not self._end(job, JobState.CONDITION_UNMET, why=why, **extra):
            return self._state(job)
        self.claims.landing_blocked(job.node_id, f"{job.repo}: {why}")
        return JobState.CONDITION_UNMET

    def _park_red_target(self, job: Job, gate: Gate, base: GateRun) -> JobState:
        sha = str(job.result["base_sha"])
        failing = sorted(base.failing or frozenset())
        for cond in self.claims.nodes.get_conditions(job.node_id):
            if cond.needs.startswith(gates.RED_TARGET):
                self.claims.nodes.remove_condition(job.node_id, cond.idx)
        self.claims.nodes.add_condition(
            Condition(
                node_id=job.node_id,
                idx=0,
                needs=f"{gates.RED_TARGET}: {job.repo} main at {sha[:12]} fails the "
                f"{len(failing)} test(s) this landing fails",
                command=gates.red_target_command(
                    self.root, job.repo, sha, gates.template_hash(gate.command)
                ),
                stage=ConditionStage.LANDING,
            )
        )
        mark = {
            "repo": job.repo,
            "sha": sha,
            "failing": failing,
            "since": datetime.now(tz=UTC).isoformat(),
        }
        return self._blocked(
            job, f"main is red at {sha[:12]} with the same failures", red_target=mark
        )

    def _drop_cleared_red_targets(self, node_id: str, unmet: list[Condition]) -> None:
        still = {c.idx for c in unmet}
        for cond in self.claims.nodes.get_conditions(node_id):
            if cond.needs.startswith(gates.RED_TARGET) and cond.idx not in still:
                self.claims.nodes.remove_condition(node_id, cond.idx)

    # -- helpers ------------------------------------------------------------------------------

    def _new_job(
        self, kind: JobKind, node_id: str, repo: str, target: str, result: dict[str, Any]
    ) -> Job:
        return self.jobs.create(
            Job(
                kind=kind,
                node_id=node_id,
                repo=repo,
                target=target,
                state=JobState.RUNNING,
                step="start",
                result=result,
            )
        )

    def _launch(self, job: Job) -> None:
        if not self.detach:
            return
        log = self.root / ".taskmanager" / "jobs" / f"{job.id}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("ab") as out:
            # The child records its own pid: a write from here could overwrite its first step.
            subprocess.Popen(
                [sys.executable, "-m", "taskmanager.engine.landing", "run", job.id]
                + ["--root", str(self.root)],
                cwd=self.root,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

    def _job(self, job_id: str) -> Job:
        job = self.jobs.get(job_id)
        if job is None:
            raise OperationError(f"job '{job_id}' not found", 404)
        return job

    @contextmanager
    def _beating(self, node_id: str) -> Iterator[None]:
        stop = threading.Event()

        def beat() -> None:
            while not stop.wait(HEARTBEAT_SECONDS):
                self.claims.heartbeat(node_id)

        thread = threading.Thread(target=beat, daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join()

    def _lock(self, job: Job) -> bool:
        deadline = time.monotonic() + LOCK_WAIT_SECONDS
        while not self.jobs.acquire_branch(job.repo, job.target, job.id):
            if time.monotonic() > deadline:
                return False
            time.sleep(1)
        return True

    def _baseline(self, job: Job, gate: Gate) -> GateRun:
        sha = str(job.result["base_sha"])
        key = gates.template_hash(gate.command)
        cached = self.cache.get_baseline(job.repo, sha, key)
        if cached is not None:
            return cached
        worktree = self._worktree_path(job, "base")
        gitops.add_detached_worktree(self._dir(job), worktree, sha)
        try:
            run = gates.run_gate(
                self._render(gate, job, worktree), worktree, gate.timeout, gate.junit
            )
        finally:
            GitManager(self._dir(job)).remove_worktree(worktree, force=True)
        self.cache.put_baseline(job.repo, sha, key, run)
        return run

    def _advisory(self, node_id: str) -> bool:
        """A rejection nobody below fixes lands where the parent's review sees it, so its own
        verifications are reported, not enforced."""
        node = self.claims.node(node_id)
        return node.review and not node.fix and node.outcome == Outcome.REJECT

    def _merging(self, job: Job) -> str:
        if job.kind == JobKind.SYNC:
            return str(job.result["source"])
        return self.claims.branch_of(job.node_id)

    def _target_ref(self, job: Job) -> str:
        if job.target == "main":
            gitops.fetch(self._dir(job))
        return self.claims.target_ref(job.target)

    def _gate_config(self, repo: str, which: str) -> Gate | None:
        repo_config = self.config.repos.get(repo)
        return repo_config.gates.get(which) if repo_config is not None else None

    def _render(self, gate: Gate, job: Job, worktree: Path) -> str:
        return gates.render(
            gate.command, worktree=str(worktree), node=job.node_id, repo=job.repo, target=job.target
        )

    def _subject(self, job: Job) -> str:
        return f"merge({job.node_id}): land {self.claims.branch_of(job.node_id)} on {job.target}"

    def _dir(self, job: Job) -> Path:
        return self.root / job.repo

    def _worktree_path(self, job: Job, suffix: str = "") -> Path:
        name = f"{job.id}-{suffix}" if suffix else job.id
        return self.root / self.config.worktree_dir / "land" / name

    def _require_worktree(self, job: Job) -> str:
        if not job.worktree:
            raise OperationError(f"job {job.id} has no worktree at step {job.step}", 409)
        return job.worktree

    def _remove_worktree(self, job: Job) -> None:
        if job.worktree and Path(job.worktree).exists():
            GitManager(self._dir(job)).remove_worktree(Path(job.worktree), force=True)
        job.worktree = None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m taskmanager.engine.landing")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="run a landing or sync job to its end or next stop")
    run.add_argument("job_id")
    run.add_argument("--root", type=Path, required=True)
    args = parser.parse_args(argv)
    print(Landing.open(args.root.resolve()).run(args.job_id))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

`ponytail:` a job that expires (its process killed) leaves its merge worktree under `<worktree_dir>/land/`; the next landing cuts a fresh one. Add a sweep of `land/` worktrees whose job is not live if they accumulate.

In `src/taskmanager/engine/claims.py`, add `from taskmanager.engine.gates import RED_TARGET` to the imports, replace `Claims.sweep` in full, and add these methods to `Claims`:

```python
def sweep(self) -> list[str]:
    swept = self.runtime.sweep_expired_leases()
    for node in self.nodes.list_nodes():
        if (
            node.id not in swept
            and node.kind != NodeKind.DECISION
            and Status(node.status) in IN_STEP
            and self.runtime.get_lease(node.id) is None
        ):
            swept.append(node.id)
    for node_id in swept:
        node = self.nodes.get_node(node_id)
        if node is None or node.kind == NodeKind.DECISION:
            continue
        expired = self._expire_jobs(node_id)
        if Status(node.status) in IN_STEP or expired:
            self._advance(node, Event.EXPIRED, "lease sweep")
    self._escalate_red_targets()
    return swept


def landed(self, node_id: str, report: str) -> Status:
    return self._advance(self.node(node_id), Event.LANDED, "job land", note=("merge", report))


def landing_failed(self, node_id: str, finding: str) -> Status:
    return self._advance(
        self.node(node_id), Event.OWN_DEFECT, "job land", note=("merge", finding), evidence=finding
    )


def landing_blocked(self, node_id: str, why: str) -> Status:
    """The node goes back to where its merge was claimed from; a condition is not a failure."""
    return self._advance(self.node(node_id), Event.RELEASE_BLOCKED, "job land", note=("merge", why))


def park(self, node_id: str) -> None:
    """A job stopped for an agent keeps its lease with no ttl, so sweep leaves it alone."""
    self.runtime.park(node_id)


def _escalate_red_targets(self) -> list[str]:
    """One decision per red `main` that has held landings longer than
    red_target_decision_after, blocking every landing it holds."""
    parked: dict[tuple[str, str], list[tuple[str, datetime, list[str]]]] = {}
    for node in self.nodes.list_nodes():
        if node.kind == NodeKind.DECISION:
            continue
        if not any(c.needs.startswith(RED_TARGET) for c in self.nodes.get_conditions(node.id)):
            continue
        marks = [
            j.result["red_target"] for j in self.jobs.for_node(node.id) if "red_target" in j.result
        ]
        if not marks:
            continue
        mark = marks[-1]
        since = datetime.fromisoformat(str(mark["since"]))
        key = (str(mark["repo"]), str(mark["sha"]))
        parked.setdefault(key, []).append((node.id, since, list(mark["failing"])))
    opened: list[str] = []
    now = datetime.now(tz=UTC)
    for (repo, sha), entries in parked.items():
        oldest = min(since for _, since, _ in entries)
        if (now - oldest).total_seconds() < self.config.red_target_decision_after:
            continue
        slug = f"red-target-{repo}-{sha[:12]}"
        held = [node_id for node_id, _, _ in entries]
        existing = self.nodes.get_node(f"decision-{slug}")
        if existing is None:
            opened.append(
                self.ops.add_decision(
                    f"main of {repo} is red at {sha[:12]} and {len(held)} landing(s) wait on "
                    "it: who fixes main?",
                    slug=slug,
                    context="Failing on main and at every parked landing:\n"
                    + "\n".join(entries[0][2]),
                    options=[
                        "fixed|main is fixed; the parked landings retry on their own",
                        "investigate|Someone investigates the red main",
                    ],
                    blocks=held,
                )
            )
        elif existing.status == DecisionStatus.OPEN:
            unlinked = [n for n in held if existing.id not in self.nodes.get_dependencies(n)]
            if unlinked:
                self.ops.link_decision(existing.id, add=unlinked)
    return opened
```

- [ ] **Step 4: Run the tests and the gates**

```
uv run --directory <worktree> ruff format src/taskmanager/engine/gates.py src/taskmanager/engine/landing.py src/taskmanager/engine/git.py src/taskmanager/engine/claims.py tests/unit/lifecycle_estate.py tests/unit/test_gates.py tests/unit/test_landing.py; echo $?
uv run --directory <worktree> pytest tests/unit/test_gates.py tests/unit/test_landing.py tests/unit/test_claims.py -q; echo $?
uv run --directory <worktree> pytest -q; echo $?
uv run --directory <worktree> ruff check; echo $?
uv run --directory <worktree> ruff format --check; echo $?
uv run --directory <worktree> mypy; echo $?
```

Expected: every command prints `0` last.

- [ ] **Step 5: Commit**

```
git -C <worktree> add src/taskmanager/engine/gates.py src/taskmanager/engine/landing.py src/taskmanager/engine/git.py src/taskmanager/engine/claims.py tests/unit/lifecycle_estate.py tests/unit/test_gates.py tests/unit/test_landing.py && git -C <worktree> commit -m "feat(landing): land a node as a detached job that merges, gates against a cached baseline, pushes and verifies"
```

### Task 15: Sync jobs and container landings across repositories

**Spec:** §4.1 (the claim-time sync), §5.2 (`sync`), §5.4 row 5, §6.4, §6.5

**Files:**
- Modify: `src/taskmanager/engine/claims.py` (`Claims.start` replaced to trigger a sync, keeping `worktree_dir`; `Claims._with_cycle` replaced so a stable status never keeps a `claimed_from`; adds `known_repos`, `sync_units`, `hold_for_sync`, `sync_done`, `_sync_pairs`; imports `MAIN`, `sync_pairs` from `chains`)
- Modify: `src/taskmanager/engine/landing.py` (adds `start_sync`, `_sync`, `_sync_unit`; `run` and `_succeed` replaced)
- Test: `tests/unit/test_sync_landing.py`

**Interfaces:**
- Consumes: Tasks 13–14; `lifecycle.advance` taking `RELEASE`/`EXPIRED` on a stable status to one more step failure (Task 2); `chains.MAIN`, `chains.sync_pairs` (Task 5); `Snapshot.inherited_edges` (Task 6); `RuntimeRepository.claim` (which requires the stored status to equal the node's `claimed_from`)/`release_lease`, `JobRepository.create` assigning `sync-<hex>` ids (Task 9).
- Produces: `Landing.start_sync(node_id, pairs) -> str` (the contract name); `Claims.known_repos() -> list[str]`, `Claims.sync_units(pairs) -> list[tuple[str, str, str]]` (source ref, base branch, repository), `Claims.hold_for_sync(node_id) -> bool`, `Claims.sync_done(node_id)`; a claim answering `blocked` with reason `syncing <branch>` and the sync job's id in `ClaimResult.job`; a container landing that chains one job per repository, each finished job naming the next in `result["next"]`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_sync_landing.py`:

```python
import time
from pathlib import Path

from lifecycle_estate import (
    add,
    attach_landing,
    branch_at,
    commit,
    git,
    junit_gate,
    make_estate,
    on_branch,
    push_main,
    stored,
)

from taskmanager.core.enums import NodeKind
from taskmanager.core.status import Action, JobState, Merge, Outcome, Status
from taskmanager.engine.claims import Claims
from taskmanager.engine.config import Gate, ProjectConfig, RepoConfig
from taskmanager.engine.landing import Landing

TRUE = Gate(command="true", junit=None, timeout=60)


def lagging_parent(tmp_path: Path, config: ProjectConfig | None = None) -> tuple[Claims, Landing]:
    """X builds on tm/P and depends on Y, which landed on main after tm/P was cut."""
    claims = make_estate(tmp_path, config=config)
    api = claims.root / "api"
    add(claims, "P", NodeKind.PLAN, review=True, fix=True)
    add(claims, "Y", status=Status.COMPLETED)
    add(claims, "X", parent="P", merge=Merge.PARENT, depends=("Y",))
    branch_at(api, "tm/P")
    push_main(api, "y.py", "y = 1\n")
    return claims, attach_landing(claims)


def test_a_claim_on_a_lagging_parent_branch_syncs_main_in_before_implement_starts(
    tmp_path: Path,
) -> None:
    claims, landing = lagging_parent(tmp_path)
    api = claims.root / "api"

    first = claims.start("X", "implementer", "s1")

    assert first.action == Action.BLOCKED
    assert first.reason == "syncing tm/P"
    assert first.job is not None and first.job.startswith("sync-")
    assert stored(claims, "X").status == Status.READY
    lease = claims.runtime.get_lease("X")
    assert lease is not None and lease.action == Action.SYNC

    assert landing.run(first.job) == JobState.SUCCEEDED
    git(api, "merge-base", "--is-ancestor", "origin/main", "tm/P")
    assert claims.runtime.get_lease("X") is None
    assert stored(claims, "X").claimed_from is None

    second = claims.start("X", "implementer", "s1")
    assert second.action == Action.IMPLEMENT
    assert second.worktree is not None
    assert (Path(second.worktree) / "y.py").exists()


def test_a_sync_conflict_is_handed_to_an_agent_who_resolves_and_resumes(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)
    api = claims.root / "api"
    add(claims, "P", NodeKind.PLAN, review=True, fix=True)
    add(claims, "Y", status=Status.COMPLETED)
    add(claims, "X", parent="P", merge=Merge.PARENT, depends=("Y",))
    on_branch(api, "tm/P", "app.py", "parent\n")
    push_main(api, "app.py", "main\n")
    landing = attach_landing(claims)
    first = claims.start("X", "implementer", "s1")
    assert first.job is not None
    assert landing.run(first.job) == JobState.NEEDS_AGENT

    handed = claims.start("X", "resolver", "s2")

    assert (handed.action, handed.job, handed.branch) == (Action.SYNC, first.job, "tm/P")
    assert handed.worktree is not None
    worktree = Path(handed.worktree)
    (worktree / "app.py").write_text("both\n")
    git(worktree, "add", "app.py")
    git(worktree, "commit", "-q", "--no-edit")
    assert landing.resume(first.job) == JobState.SUCCEEDED
    git(api, "merge-base", "--is-ancestor", "origin/main", "tm/P")
    assert git(api, "show", "tm/P:app.py") == "both"
    assert claims.start("X", "implementer", "s1").action == Action.IMPLEMENT


def test_a_red_parent_gate_stops_a_sync_for_an_agent(tmp_path: Path) -> None:
    red = ProjectConfig(
        repos={"api": RepoConfig(gates={"parent": Gate(command="exit 1", junit=None, timeout=60)})}
    )
    claims, landing = lagging_parent(tmp_path, red)
    first = claims.start("X", "implementer", "s1")
    assert first.job is not None

    assert landing.run(first.job) == JobState.NEEDS_AGENT
    job = claims.jobs.get(first.job)
    assert job is not None and job.result["reason"] == "red"


def test_a_sync_its_agent_abandons_counts_one_step_failure(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)
    api = claims.root / "api"
    add(claims, "P", NodeKind.PLAN, review=True, fix=True)
    add(claims, "Y", status=Status.COMPLETED)
    add(claims, "X", parent="P", merge=Merge.PARENT, depends=("Y",))
    on_branch(api, "tm/P", "app.py", "parent\n")
    push_main(api, "app.py", "main\n")
    landing = attach_landing(claims)
    first = claims.start("X", "implementer", "s1")
    assert first.job is not None
    landing.run(first.job)
    assert claims.start("X", "resolver", "s2", ttl=1).action == Action.SYNC
    time.sleep(1.5)

    assert claims.sweep() == ["X"]

    job = claims.jobs.get(first.job)
    assert job is not None and job.state == JobState.EXPIRED
    node = stored(claims, "X")
    assert (node.status, node.step_failures, node.claimed_from) == (Status.READY, 1, None)
    assert claims.runtime.get_lease("X") is None


def two_repo_container(tmp_path: Path, **columns: object) -> tuple[Claims, Landing]:
    config = ProjectConfig(
        repo_order=["api", "web"],
        repos={
            "api": RepoConfig(gates={"main": TRUE}),
            "web": RepoConfig(gates={"main": junit_gate(tmp_path)}),
        },
    )
    claims = make_estate(tmp_path, repos=("api", "web"), config=config)
    add(
        claims,
        "P",
        NodeKind.PLAN,
        review=True,
        fix=True,
        status=Status.REVIEWED,
        outcome=Outcome.APPROVE,
        review_cycles=1,
        **columns,
    )
    add(claims, "A", parent="P", repo="api", merge=Merge.PARENT, status=Status.COMPLETED)
    add(claims, "B", parent="P", repo="web", merge=Merge.PARENT, status=Status.COMPLETED)
    return claims, attach_landing(claims)


def test_a_container_lands_each_repository_in_order_and_only_what_is_left_after_a_failure(
    tmp_path: Path,
) -> None:
    claims, landing = two_repo_container(tmp_path)
    api, web = claims.root / "api", claims.root / "web"
    on_branch(api, "tm/P", "a.py", "a = 1\n")
    on_branch(web, "tm/P", "b.py", "b = 1\n")
    on_branch(web, "tm/P", "failing.txt", "broken\n")

    merge = claims.start("P", "merger", "s1")
    assert merge.job is not None
    assert landing.run(merge.job) == JobState.OWN_DEFECT

    node = stored(claims, "P")
    assert (node.status, node.outcome, node.merge_attempts) == (
        Status.REVIEWED,
        Outcome.MERGE_FAILED,
        1,
    )
    assert git(api, "show", "origin/main:a.py") == "a = 1"
    assert "b.py" not in git(web, "ls-tree", "--name-only", "origin/main").split()

    fix = claims.start("P", "fixer", "s1")
    assert fix.action == Action.FIX
    assert fix.worktree is not None
    assert fix.worktrees == {
        "api": str(Path(fix.worktree) / "api"),
        "web": str(Path(fix.worktree) / "web"),
    }
    commit(Path(fix.worktrees["web"]), "failing.txt", None, "web: gate green again")
    assert claims.complete("P") == Status.FIXED
    assert claims.start("P", "reviewer", "s1").action == Action.REVIEW
    claims.ops.set_section("P", "review", "the web gate is green")
    assert claims.review("P", approve=True) == Status.REVIEWED
    assert stored(claims, "P").review_cycles == 1

    again = claims.start("P", "merger", "s1")
    assert again.job is not None
    assert landing.run(again.job) == JobState.SUCCEEDED

    assert stored(claims, "P").status == Status.COMPLETED
    git(api, "fetch", "-q", "origin")
    subjects = git(api, "log", "--first-parent", "--format=%s", "origin/main").splitlines()
    assert subjects.count("merge(P): land tm/P on main") == 1
    assert git(web, "show", "origin/main:b.py") == "b = 1"
    first_job = claims.jobs.get(again.job)
    assert first_job is not None and first_job.result["next"]


def test_land_order_overrides_the_configured_repository_order(tmp_path: Path) -> None:
    claims, _ = two_repo_container(tmp_path, land_order=["web", "api"])

    merge = claims.start("P", "merger", "s1")

    assert merge.job is not None
    job = claims.jobs.get(merge.job)
    assert job is not None and job.repo == "web"
```

- [ ] **Step 2: Run them and watch them fail**

```
uv run --directory <worktree> pytest tests/unit/test_sync_landing.py -q; echo $?
```

Expected: the four sync tests fail with `assert <Action.IMPLEMENT: 'implement'> == <Action.BLOCKED: 'blocked'>` (the claim implements from the lagging `tm/P` without syncing) or, for the handoff tests, a failed `first.job is not None`; the container test fails with `JobState.SUCCEEDED == JobState.OWN_DEFECT` false (only `api` lands, and `P` completes with `web` unlanded). `test_land_order_overrides_the_configured_repository_order` passes already (Task 13 orders the repositories). Exit code 1.

- [ ] **Step 3: Implement**

In `src/taskmanager/engine/claims.py`, add `from taskmanager.engine.chains import MAIN, satisfied, sync_pairs` (replacing the import of `satisfied` alone), replace `Claims.start` and `Claims._with_cycle` in full, and add the new methods to `Claims`:

```python
def start(
    self,
    node_id: str,
    agent: str,
    session: str,
    ttl: int | None = None,
    worktree_dir: Path | None = None,
) -> ClaimResult:
    self.node(node_id)
    self.sweep()
    node = self.node(node_id)
    handed = self._hand_over(node, agent, session, ttl)
    if handed is not None:
        return handed
    snap = self.snapshots.build()
    cycle = self.snapshots.cycle(node)
    action = lifecycle.next_action(cycle)
    reason = self.blocked_reason(node, snap, action)
    if reason is not None or action is None:
        return ClaimResult(Action.BLOCKED, reason)
    pairs = self._sync_pairs(node.id, snap)
    units = self.sync_units(pairs) if pairs else []
    if units:
        # A dependency landed below a branch this node builds on: that branch catches up
        # before any step starts on it, never after.
        job = self._landing().start_sync(node.id, pairs)
        branches = ", ".join(dict.fromkeys(base for _, base, _ in units))
        return ClaimResult(Action.BLOCKED, f"syncing {branches}", job=job)
    return self._claim(node, cycle, action, agent, session, ttl, worktree_dir)


@staticmethod
def _with_cycle(node: Node, cycle: Cycle) -> Node:
    return node.model_copy(
        update={
            "status": cycle.status,
            # A sync hold claims a node without moving it, so a step failure counted on a
            # stable status must still clear what the hold set.
            "claimed_from": cycle.claimed_from if cycle.status in IN_STEP else None,
            "outcome": cycle.outcome,
            "fix_for": cycle.fix_for,
            "review_cycles": cycle.review_cycles,
            "merge_attempts": cycle.merge_attempts,
            "step_failures": cycle.step_failures,
            "updated_at": datetime.now(tz=UTC),
        }
    )


def known_repos(self) -> list[str]:
    """Every repository the estate names that is cloned under the root."""
    names = [
        *self.config.repo_order,
        *self.config.repos,
        *(n.target_repo for n in self.nodes.list_nodes() if n.target_repo),
    ]
    return [r for r in dict.fromkeys(names) if (self.root / r / ".git").exists()]


def _sync_pairs(self, node_id: str, snap: Snapshot) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for dep in snap.inherited_edges(node_id):
        if not isinstance(snap.status(dep), Status):
            continue
        for pair in sync_pairs(snap, node_id, dep):
            if pair not in pairs:
                pairs.append(pair)
    return pairs


def sync_units(self, pairs: list[tuple[str, str]]) -> list[tuple[str, str, str]]:
    """(source ref, base branch, repository) for each pair and repository where both exist
    and the base lacks the source. Across repositories there is nothing to sync."""
    units: list[tuple[str, str, str]] = []
    fetched: set[str] = set()
    for source, base in pairs:
        source_ref = self.target_ref("main" if source == MAIN else self.branch_of(source))
        base_branch = self.branch_of(base)
        for repo in self.known_repos():
            repo_dir = self.root / repo
            if source == MAIN and repo not in fetched:
                gitops.fetch(repo_dir)
                fetched.add(repo)
            if not gitops.rev_parse(repo_dir, f"refs/heads/{base_branch}"):
                continue
            if not gitops.rev_parse(repo_dir, source_ref):
                continue
            if not gitops.is_ancestor(repo_dir, source_ref, base_branch):
                units.append((source_ref, base_branch, repo))
    return units


def hold_for_sync(self, node_id: str) -> bool:
    """tm's own lease on the node while its sync runs: the node stays at its status and
    unclaimed, and the sync's agent, if one is needed, takes this lease over.

    The claim names the node's own status as the one it is claimed from, so a racing claim
    that moved the node, or already holds it, makes this one write nothing."""
    node = self.node(node_id)
    lease = Lease(
        task_id=node_id,
        agent_id="tm",
        session_id="tm",
        branch_name=self.branch_of(node_id),
        ttl_seconds=self.ttl_for(Action.SYNC),
        action=Action.SYNC,
    )
    return self.runtime.claim(lease, [], node.model_copy(update={"claimed_from": node.status}))


def sync_done(self, node_id: str) -> None:
    node = self.node(node_id)
    with self.nodes.transaction():
        self.nodes.save_node(node.model_copy(update={"claimed_from": None}))
        self.runtime.release_lease(node_id)
    self._ledger("job sync", node_id, {"state": JobState.SUCCEEDED.value})
```

In `src/taskmanager/engine/landing.py`, replace `Landing.run` and `Landing._succeed` in full and add `start_sync`, `_sync` and `_sync_unit` to `Landing`:

```python
def start_sync(self, node_id: str, pairs: list[tuple[str, str]]) -> str:
    units = self.claims.sync_units(pairs)
    if not units:
        raise OperationError(f"{node_id} needs no sync", 409)
    if not self.claims.hold_for_sync(node_id):
        raise OperationError(f"{node_id} is held: its sync was not started", 409)
    source, target, repo = units[0]
    job = self._new_job(
        JobKind.SYNC,
        node_id,
        repo,
        target,
        {"units": [list(unit) for unit in units], "done": 0, "source": source},
    )
    self._launch(job)
    return job.id


def run(self, job_id: str) -> JobState:
    job = self._job(job_id)
    if job.state != JobState.RUNNING:
        return job.state
    job.pid = os.getpid()
    self.jobs.update(job)
    with self._beating(job.node_id):
        try:
            return self._sync(job) if job.kind == JobKind.SYNC else self._land(job)
        except (subprocess.CalledProcessError, OSError, OperationError) as exc:
            detail = f"{exc}\n{getattr(exc, 'stderr', '') or ''}".strip()
            return self._needs_agent(job, "error", error=detail)


def _succeed(self, job: Job, report: str) -> JobState:
    if not self._end(job, JobState.SUCCEEDED, verify=report):
        return self._state(job)
    summary = f"{job.repo}: landed on {job.target}\n{report}"
    repos = self.claims.repos_of(job.node_id)
    later = repos[repos.index(job.repo) + 1 :] if job.repo in repos else []
    if not later:
        self.claims.landed(job.node_id, summary)
        return JobState.SUCCEEDED
    # One landing job per repository, in order, under the same lease; a pushed main is
    # never rolled back, so a later repository's failure leaves this one landed.
    following = self._new_job(JobKind.LAND, job.node_id, later[0], job.target, {})
    job.result["next"] = following.id
    self.jobs.update(job)
    self.claims.note(job.node_id, "merge", summary)
    following.pid = os.getpid()
    self.jobs.update(following)
    return self._land(following)


def _sync(self, job: Job) -> JobState:
    units = job.result["units"]
    while int(job.result["done"]) < len(units):
        source, target, repo = units[int(job.result["done"])]
        job.repo, job.target, job.result["source"] = repo, target, source
        stopped = self._sync_unit(job)
        if stopped is not None:
            return stopped
        job.result["done"] = int(job.result["done"]) + 1
        job.result.pop("push_tries", None)
        job.step = "start"
        self.jobs.update(job)
    if not self._end(job, JobState.SUCCEEDED):
        return self._state(job)
    self.claims.sync_done(job.node_id)
    return JobState.SUCCEEDED


def _sync_unit(self, job: Job) -> JobState | None:
    """Merges the source into one container branch in one repository, gates it with the
    repository's `parent` gate when one is configured, and moves the branch by
    compare-and-swap. None when the unit is done."""
    repo_dir, source = self._dir(job), str(job.result["source"])
    while True:
        if job.step == "start":
            if gitops.is_ancestor(repo_dir, source, job.target):
                return None
            base = gitops.rev_parse(repo_dir, f"refs/heads/{job.target}")
            worktree = self._worktree_path(job)
            gitops.add_detached_worktree(repo_dir, worktree, base)
            job.worktree = str(worktree)
            job.result["base_sha"] = base
            subject = f"merge({job.node_id}): sync {source} into {job.target}"
            if not gitops.merge_no_ff(worktree, source, subject):
                return self._needs_agent(job, "conflict")
            job.step = "gate"
        elif job.step == "gate":
            gate = self._gate_config(job.repo, "parent")
            if gate is not None:
                worktree = Path(self._require_worktree(job))
                run = gates.run_gate(
                    self._render(gate, job, worktree), worktree, gate.timeout, gate.junit
                )
                if run.exit_code != 0:
                    return self._needs_agent(job, "red", tip=run.tail)
            job.step = "push"
        else:
            moved = self._move_branch(job, Path(self._require_worktree(job)), "done")
            if isinstance(moved, JobState):
                return moved
            if moved == "done":
                self._remove_worktree(job)
                return None
            job.step = moved
        job.heartbeat = datetime.now(tz=UTC)
        self.jobs.update(job)
```

- [ ] **Step 4: Run the tests and the gates**

```
uv run --directory <worktree> ruff format src/taskmanager/engine/claims.py src/taskmanager/engine/landing.py tests/unit/test_sync_landing.py; echo $?
uv run --directory <worktree> pytest tests/unit/test_sync_landing.py tests/unit/test_landing.py tests/unit/test_claims.py -q; echo $?
uv run --directory <worktree> pytest -q; echo $?
uv run --directory <worktree> ruff check; echo $?
uv run --directory <worktree> ruff format --check; echo $?
uv run --directory <worktree> mypy; echo $?
```

Expected: every command prints `0` last.

- [ ] **Step 5: Commit**

```
git -C <worktree> add src/taskmanager/engine/claims.py src/taskmanager/engine/landing.py tests/unit/test_sync_landing.py && git -C <worktree> commit -m "feat(landing): sync lagging container branches at claim and land containers repository by repository"
```

### Task 16: Discovery

**Spec:** §4.4 (migration chain), §5.4, §5.5, §5.6

**Files:**
- Create: `src/taskmanager/engine/discovery.py`
- Modify: `src/taskmanager/engine/wave.py` (`djb2` moves to `discovery.py`; `wave.py` re-exports it)
- Test: `tests/unit/test_discovery.py`

`tests/unit/test_wave.py`, `tests/integration/test_wave_cli.py` and `cli/main.py` keep importing `djb2` from `wave`, which re-exports it: `tm wave discover` keeps calling `wave.discover_batch` until Task 18 points it at `discovery.discover`, and Task 21 deletes `wave.py` with its tests.

**Interfaces:**
- Consumes: `Claims` with `sweep`, `snapshots`, `nodes`, `runtime`, `jobs`, `blocked_reason`, `next_step`, `repos_of`, `hold_for_sync` (Tasks 13–15); `RuntimeRepository.list_leases`/`claim`/`park`, `Lease.model` (the routed family), `JobRepository.create` assigning the id (Task 9); `routing.STRONG` (Task 13); `chains.base_chain`, `landing_chain`, `landing_target` (Task 5); `SnapNode.writes_migration`, `SnapNode.repo` (Tasks 6, 10).
- Produces: `discovery.djb2(payload) -> int` (re-exported by `wave.djb2` until Task 21); `discovery.discover(claims, specs, session, slots, max_strong, exclude=None) -> tuple[str, int]`, counting a session's strong slots by `Lease.model`: the JSON payload `{"chosen": [...], "held": [...], "waiting_for_slot": n, "mine": n}` (compact separators, sorted keys) and the count of `chosen`, the `__CHECK` contract of today's `wave.discover_batch`. A chosen entry is `{"id", "kind", "action", "model", "repos", "requires", "migration", "job"}`.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_discovery.py`:

```python
import json
from pathlib import Path
from typing import Any

import pytest
from lifecycle_estate import add, make_estate, stored

from taskmanager.core.enums import NodeKind
from taskmanager.core.models import Job, Lease
from taskmanager.core.status import Action, JobKind, JobState, Merge, Outcome, Status
from taskmanager.engine import wave
from taskmanager.engine.claims import Claims
from taskmanager.engine.discovery import discover, djb2

MIGRATION = ["api/migrations/versions/001_add.py"]


def batch(claims: Claims, **args: Any) -> dict[str, Any]:
    options: dict[str, Any] = {"specs": None, "session": "s1", "slots": 10, "max_strong": 10}
    options.update(args)
    payload, count = discover(claims, **options)
    data: dict[str, Any] = json.loads(payload)
    assert count == len(data["chosen"])
    return data


def chosen(data: dict[str, Any]) -> list[tuple[str, str]]:
    return [(entry["id"], entry["action"]) for entry in data["chosen"]]


def hold(
    claims: Claims,
    node_id: str,
    session: str,
    step: Status,
    action: Action,
    model: str | None = None,
) -> None:
    """Claims `node_id` into `step` from its stored status, as a dispatcher's claim would."""
    node = stored(claims, node_id)
    lease = Lease(
        task_id=node_id,
        agent_id=f"agent-{node_id}",
        session_id=session,
        branch_name=f"tm/{node_id}",
        ttl_seconds=3600,
        action=action,
        model=model,
    )
    moved = node.model_copy(update={"status": step, "claimed_from": node.status})
    assert claims.runtime.claim(lease, [], moved)


def waiting_job(claims: Claims, node_id: str, kind: JobKind) -> str:
    job = claims.jobs.create(
        Job(
            kind=kind,
            node_id=node_id,
            repo="api",
            target="main" if kind == JobKind.LAND else "tm/P",
            state=JobState.NEEDS_AGENT,
            step="gate",
            worktree=f"/tmp/waiting-{node_id}",
            result={"reason": "conflict", "source": "origin/main"},
        )
    )
    claims.runtime.park(node_id)
    return job.id


def test_the_batch_checksum_is_djb2_over_the_payload_bytes() -> None:
    assert djb2("") == 5381
    assert djb2("a") == (5381 * 33 + ord("a")) & 0xFFFFFFFF
    assert wave.djb2 is djb2


def test_every_kind_of_node_is_offered_with_its_next_step_model_and_requirements(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path)
    add(claims, "S", NodeKind.SPEC)
    add(claims, "P1", NodeKind.PLAN, parent="S", review=True, fix=True, status=Status.IMPLEMENTED)
    add(claims, "C1", parent="P1", status=Status.COMPLETED)
    add(claims, "P2", NodeKind.PLAN, parent="S")
    add(claims, "T1", parent="P2", models=["claude-haiku-4"], requires=["figma"])
    add(claims, "T2", parent="P2", status=Status.REVIEWED, outcome=Outcome.APPROVE)

    data = batch(claims)

    assert chosen(data) == [("T2", "merge"), ("P1", "review"), ("T1", "implement")]
    by_id = {entry["id"]: entry for entry in data["chosen"]}
    assert (by_id["P1"]["model"], by_id["T1"]["model"], by_id["T2"]["model"]) == (
        "opus",
        "haiku",
        "sonnet",
    )
    assert by_id["T1"]["requires"] == ["figma"]
    assert by_id["T1"]["repos"] == ["api"]
    assert by_id["P1"]["kind"] == "plan"


def test_a_landing_or_a_sync_waiting_for_an_agent_is_offered_with_its_job(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)
    add(claims, "T1", status=Status.REVIEWED, outcome=Outcome.APPROVE)
    hold(claims, "T1", "other", Status.MERGING, Action.MERGE)
    land = waiting_job(claims, "T1", JobKind.LAND)
    add(claims, "X")
    assert claims.hold_for_sync("X")
    sync = waiting_job(claims, "X", JobKind.SYNC)

    data = batch(claims)

    jobs = {entry["id"]: (entry["action"], entry["job"]) for entry in data["chosen"]}
    assert jobs == {"T1": ("merge", land), "X": ("sync", sync)}


def test_a_node_that_cannot_be_claimed_is_held_with_its_reason(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)
    add(claims, "D")
    add(claims, "T1", depends=("D",))

    data = batch(claims)

    assert chosen(data) == [("D", "implement")]
    assert "T1: waits on D" in data["held"]


@pytest.mark.parametrize(
    ("specs", "expected"),
    [
        (None, {"T1", "T2", "T3"}),
        (["S1"], {"T1"}),
        (["S1", "S2"], {"T1", "T2"}),
        (["none"], {"T3"}),
    ],
)
def test_specs_scope_the_batch_and_none_names_the_nodes_under_no_spec(
    tmp_path: Path, specs: list[str] | None, expected: set[str]
) -> None:
    claims = make_estate(tmp_path)
    for spec, plan, task in (("S1", "P1", "T1"), ("S2", "P2", "T2")):
        add(claims, spec, NodeKind.SPEC)
        add(claims, plan, NodeKind.PLAN, parent=spec)
        add(claims, task, parent=plan)
    add(claims, "T3")

    data = batch(claims, specs=specs)

    assert {entry["id"] for entry in data["chosen"]} == expected


def test_slots_strong_slots_exclusions_and_file_overlap_shape_the_batch(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)
    add(claims, "T1", files=["api/a.py"], priority=90)
    add(claims, "T2", files=["api/a.py"], priority=80)
    add(claims, "T3", models=["claude-opus-4"], priority=70)
    add(claims, "T4", priority=60)
    add(claims, "T5", priority=50)
    add(claims, "T6", priority=40)

    data = batch(claims, slots=2, max_strong=0, exclude=["T4"])

    assert chosen(data) == [("T1", "implement"), ("T5", "implement")]
    assert "T2: declared_files overlap a node chosen this wave" in data["held"]
    assert "T3: no free opus/fable slot" in data["held"]
    assert "T4: excluded by args" in data["held"]
    assert data["waiting_for_slot"] == 1


def test_the_session_leases_take_its_slots(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)
    add(claims, "T0")
    hold(claims, "T0", "s1", Status.IMPLEMENTING, Action.IMPLEMENT)
    add(claims, "T1")

    data = batch(claims, slots=1)

    assert (chosen(data), data["waiting_for_slot"], data["mine"]) == ([], 1, 1)


def test_a_session_lease_routed_to_a_strong_model_takes_a_strong_slot(tmp_path: Path) -> None:
    claims = make_estate(tmp_path)
    add(claims, "T0")
    hold(claims, "T0", "s1", Status.IMPLEMENTING, Action.IMPLEMENT, model="opus")
    add(claims, "T1", models=["claude-opus-4"], priority=90)
    add(claims, "T2", priority=80)

    data = batch(claims, max_strong=1)

    assert chosen(data) == [("T2", "implement")]
    assert "T1: no free opus/fable slot" in data["held"]


def test_a_migration_writer_holds_its_repository_chain_until_it_lands_on_main(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path)
    add(claims, "A", files=MIGRATION, status=Status.IMPLEMENTED)
    add(claims, "B", files=["api/migrations/versions/002_more.py"])
    add(claims, "C", files=["api/app.py"])

    data = batch(claims)

    assert chosen(data) == [("A", "review"), ("C", "implement")]
    assert "B: api migration chain held by A" in data["held"]


def test_a_sibling_building_on_a_landed_migration_proceeds_while_others_wait(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path)
    add(claims, "P", NodeKind.PLAN)
    add(claims, "A", parent="P", merge=Merge.PARENT, files=MIGRATION, status=Status.COMPLETED)
    add(claims, "B", parent="P", merge=Merge.PARENT, files=["api/migrations/versions/002_b.py"])
    add(claims, "C", files=["api/migrations/versions/003_c.py"])

    data = batch(claims)

    assert chosen(data) == [("B", "implement")]
    assert "C: api migration chain held by A" in data["held"]


def test_two_ready_migration_writers_in_one_repository_are_never_chosen_together(
    tmp_path: Path,
) -> None:
    claims = make_estate(tmp_path)
    add(claims, "B1", files=["api/migrations/versions/001_b1.py"], priority=90)
    add(claims, "B2", files=["api/migrations/versions/002_b2.py"], priority=80)

    data = batch(claims)

    assert chosen(data) == [("B1", "implement")]
    assert "B2: api migration chain held by B1" in data["held"]
    assert next(e for e in data["chosen"] if e["id"] == "B1")["migration"] is True
```

- [ ] **Step 2: Run them and watch them fail**

```
uv run --directory <worktree> pytest tests/unit/test_discovery.py -q; echo $?
```

Expected: collection error `ModuleNotFoundError: No module named 'taskmanager.engine.discovery'`, exit code 2.

- [ ] **Step 3: Implement**

`src/taskmanager/engine/discovery.py`:

```python
"""One dispatch wave's batch: every claimable node with the step it would take next.

Discovery reads and never claims; each chosen node is claimed by `tm task start`, which runs the
same checks again inside its transaction.
"""

import json
from dataclasses import dataclass

from taskmanager.core.enums import NodeKind
from taskmanager.core.models import Node
from taskmanager.core.status import IN_STEP, Action, JobKind, JobState, Status
from taskmanager.engine.chains import base_chain, landing_chain, landing_target
from taskmanager.engine.claims import Claims
from taskmanager.engine.routing import STRONG
from taskmanager.engine.stepgraph import Snapshot

# Later steps first, so a wave drains work already under way before it starts more.
_STAGE = {Action.MERGE: 0, Action.SYNC: 0, Action.FIX: 1, Action.REVIEW: 2, Action.IMPLEMENT: 3}
# A migration writer in one of these has started, and may not have reached main yet.
_HOLDS_CHAIN = IN_STEP | {
    Status.IMPLEMENTED,
    Status.REVIEWED,
    Status.FIXED,
    Status.FAILED,
    Status.COMPLETED,
}


@dataclass(frozen=True)
class _Candidate:
    node: Node
    action: Action
    model: str
    job: str | None
    repos: list[str]


def djb2(payload: str) -> int:
    """The `__CHECK h=` checksum `tm wave discover` prints after its payload: a caller that
    echoes the payload back through a model can reject a transcription that is not byte-exact."""
    checksum = 5381
    for byte in payload.encode("utf-8"):
        checksum = (checksum * 33 + byte) & 0xFFFFFFFF
    return checksum


def _in_scope(claims: Claims, node: Node, specs: list[str] | None) -> bool:
    if specs is None:
        return True
    spec = (
        node.id
        if node.kind == NodeKind.SPEC
        else claims.nodes.get_ancestor_of_kind(node.id, NodeKind.SPEC)
    )
    return (spec or "none") in specs


def _candidates(
    claims: Claims, snap: Snapshot, specs: list[str] | None, held: list[str]
) -> list[_Candidate]:
    found: list[_Candidate] = []
    for node in claims.nodes.list_nodes():
        if node.kind == NodeKind.DECISION or not _in_scope(claims, node, specs):
            continue
        waiting = next(
            (j for j in claims.jobs.for_node(node.id) if j.state == JobState.NEEDS_AGENT), None
        )
        if waiting is not None:
            lease = claims.runtime.get_lease(node.id)
            if lease is not None and lease.ttl_seconds is None:
                action = Action.MERGE if waiting.kind == JobKind.LAND else Action.SYNC
                found.append(_Candidate(node, action, "sonnet", waiting.id, [waiting.repo]))
            continue
        if Status(node.status) in IN_STEP:
            continue
        action, model = claims.next_step(node)
        if action is None or model is None:
            continue
        reason = claims.blocked_reason(node, snap, action)
        if reason is not None:
            held.append(f"{node.id}: {reason}")
            continue
        found.append(_Candidate(node, action, model, None, claims.repos_of(node.id)))
    return sorted(found, key=lambda c: (_STAGE[c.action], -c.node.priority, c.node.id))


def _landed_on_main(snap: Snapshot, node_id: str) -> bool:
    return snap.status(landing_chain(snap, node_id)[-1]) == Status.COMPLETED


def _migration_holders(snap: Snapshot) -> dict[str, list[str]]:
    holders: dict[str, list[str]] = {}
    for node in snap.nodes.values():
        if (
            node.writes_migration
            and node.repo
            and node.status in _HOLDS_CHAIN
            and not _landed_on_main(snap, node.id)
        ):
            holders.setdefault(node.repo, []).append(node.id)
    return holders


def _chain_holder(snap: Snapshot, holders: dict[str, list[str]], node_id: str) -> str | None:
    """The migration writer that holds `node_id`'s repository chain, if any. A node whose base
    chain holds a writer's landing target builds on that writer's landed migration."""
    base = base_chain(snap, node_id)
    for holder in holders.get(snap.nodes[node_id].repo or "", []):
        if holder == node_id:
            continue
        if snap.status(holder) == Status.COMPLETED and landing_target(snap, holder) in base:
            continue
        return holder
    return None


def discover(
    claims: Claims,
    specs: list[str] | None,
    session: str,
    slots: int,
    max_strong: int,
    exclude: list[str] | None = None,
) -> tuple[str, int]:
    claims.sweep()
    snap = claims.snapshots.build()
    excluded = set(exclude or [])
    mine = [lease for lease in claims.runtime.list_leases() if lease.session_id == session]
    free = slots - len(mine)
    strong_free = max_strong - sum(lease.model in STRONG for lease in mine)
    holders = _migration_holders(snap)
    held: list[str] = []
    chosen: list[dict[str, object]] = []
    taken: set[str] = set()
    waiting = 0
    for cand in _candidates(claims, snap, specs, held):
        node = cand.node
        if node.id in excluded:
            held.append(f"{node.id}: excluded by args")
            continue
        repo = node.target_repo or ""
        migration = snap.nodes[node.id].writes_migration
        files = (
            claims.nodes.declared_files(node.id)
            if cand.action in (Action.IMPLEMENT, Action.FIX)
            else []
        )
        why: list[str] = []
        if cand.action == Action.IMPLEMENT and migration:
            holder = _chain_holder(snap, holders, node.id)
            if holder is not None:
                why.append(f"{repo} migration chain held by {holder}")
        if taken.intersection(files):
            why.append("declared_files overlap a node chosen this wave")
        if cand.model in STRONG and strong_free <= 0:
            why.append("no free opus/fable slot")
        if not why and len(chosen) >= free:
            waiting += 1
            continue
        if why:
            held.append(f"{node.id}: {'; '.join(why)}")
            continue
        chosen.append(
            {
                "id": node.id,
                "kind": node.kind,
                "action": cand.action,
                "model": cand.model,
                "repos": cand.repos,
                "requires": node.requires,
                "migration": migration,
                "job": cand.job,
            }
        )
        taken.update(files)
        if cand.model in STRONG:
            strong_free -= 1
        if cand.action == Action.IMPLEMENT and migration:
            holders.setdefault(repo, []).append(node.id)
    payload = json.dumps(
        {"chosen": chosen, "held": held, "waiting_for_slot": waiting, "mine": len(mine)},
        separators=(",", ":"),
        sort_keys=True,
    )
    return payload, len(chosen)
```

In `src/taskmanager/engine/wave.py`, delete `def djb2` and its body, and add to the imports, after `from taskmanager.db.runtime_repo import RuntimeRepository`:

```python
from taskmanager.engine.discovery import djb2 as djb2
```

`ponytail:` candidates are ordered by step, then priority, then id; the scoring of `heuristics.get_next_tasks` (unblocking count, plan closure) is not ported. Port `_score_task` into the sort key if waves start starving nodes that unblock many others.

- [ ] **Step 4: Run the tests and the gates**

```
uv run --directory <worktree> ruff format src/taskmanager/engine/discovery.py src/taskmanager/engine/wave.py tests/unit/test_discovery.py; echo $?
uv run --directory <worktree> pytest tests/unit/test_discovery.py tests/unit/test_wave.py tests/integration/test_wave_cli.py -q; echo $?
uv run --directory <worktree> pytest -q; echo $?
uv run --directory <worktree> ruff check; echo $?
uv run --directory <worktree> ruff format --check; echo $?
uv run --directory <worktree> mypy; echo $?
```

Expected: every command prints `0` last.

- [ ] **Step 5: Commit**

```
git -C <worktree> add src/taskmanager/engine/discovery.py src/taskmanager/engine/wave.py tests/unit/test_discovery.py && git -C <worktree> commit -m "feat(discovery): offer every claimable node with its next step, holding the per-repository migration chain"
```

---

### Task 17: Operations and import/export on the new model, validation on every write

**Spec:** §2.1, §2.3, §3.1 (Containers), §3.3 (the parent's review brief), §4.3, §5.7, §9.1 (the `tm restore` bullet)
**Files:**
- Modify: `src/taskmanager/core/enums.py` (`LedgerCommand` gains `CONDITION_ADD`, `CONDITION_REMOVE`)
- Modify: `src/taskmanager/engine/operations.py` (new module symbols `GitBranchFacts`, `validated_write`, `_roll_up_container`; `Operations.__init__`, `add_spec`, `add_plan`, `add_task`, `update_node`, `set_dependencies`, `supersede`, `move_task`; new `Operations.add_condition`, `Operations.remove_condition`; the decision writers `add_decision`, `answer_decision`, `reopen_decision`, `withdraw_decision` store `DecisionStatus`)
- Modify: `src/taskmanager/engine/decisions.py` (`DECISION_STATUS_LABELS` gains the `DecisionStatus` keys)
- Modify: `src/taskmanager/engine/wave.py` (`_awaiting_decisions` reads an open decision through `stored_status`)
- Modify: `src/taskmanager/renderers/markdown.py` (the withdrawn-decision branch reads `stored_status`; the `subagent` view of a plan or spec lists its children whose stored outcome is `reject`)
- Modify: `src/taskmanager/renderers/importers.py` (`BulkImporter` rewritten: new keys, kind-aware statuses, conditions, one validated transaction)
- Modify: `src/taskmanager/engine/graph.py` (`_UNSTARTED`; `GraphEngine.resolve_task_state`, `GraphEngine.resolve_plan_status` read `READY` as not yet claimed; `GraphEngine._has_open_decision` reads `stored_status`; the whole module goes in Task 21)
- Modify: `src/taskmanager/cli/main.py` (`EXPORT_FORMAT`, `_export_node`, `export_cmd`, `restore_cmd`, `plan_add`, `task_depends`; `decision_list`'s status map and `task_get`'s open-decision test read `DecisionStatus`)
- Modify: `src/taskmanager/web/app.py` (`DependencyAdd`, `post_dependencies`, `create_plan`; `_dependency_met`, `_DECISION_TAB_STATUS` and `list_decisions`' filter read `DecisionStatus`)
- Create: `tests/unit/test_operations_lifecycle.py`
- Create: `tests/unit/test_import_lifecycle.py`
- Create: `tests/integration/test_export_restore.py`
- Modify: `tests/unit/test_operations.py`, `tests/unit/test_renderers.py`, `tests/integration/test_cli.py`, `tests/unit/test_estate_workflow.py`, `tests/integration/test_web_api.py`, `tests/unit/test_search_engine.py`, `tests/unit/test_decision_effects.py` (old tests rewritten or deleted, listed in Step 3g)

**Interfaces:**
- Consumes: `Status`, `DecisionStatus`, `Merge`, `Outcome`, `ConditionStage`, `Action` (Task 1); `rollup` (Task 3); `MAIN`, `landing_target` (Task 5); `Snapshot` (Task 6); `validate`, `Refusal`, `BranchFacts` (Task 7); `Node` lifecycle columns, `Condition`, `NodeRepository.get_conditions/add_condition/remove_condition` (Task 8); `RuntimeRepository.claim`, `JobRepository` (Task 9); `SnapshotBuilder`, `CONTAINERS`, `stored_status`, `cycle_of`, `apply_cycle` (Task 10); `is_executable` (Task 11); `roll_up_ancestors`, `Operations(..., job_repo=...)` (Task 12)
- Produces: `validated_write(node_repo, snapshots, touched, prefix=...)` in `taskmanager.engine.operations`; decisions stored as `DecisionStatus` by every writer; a container's `tm render <id> --view subagent` brief listing each child whose review rejected, with the command that prints its `:review`; `Operations.add_plan(...) -> str` (no review gate); `Operations.set_dependencies(node_id, add: list[str], remove: list[str]) -> list[str]`; `Operations.update_node(..., review, fix, merge, requires, land_order)`; `Operations.add_condition(node_id, needs, command, stage) -> Condition`; `Operations.remove_condition(node_id, idx) -> None`; `EXPORT_FORMAT` in `taskmanager.cli.main`; the export document shape every later task reads

Every write in `Operations` and `BulkImporter` runs inside `validated_write`: the writes happen in one `node_repo.transaction()`, the tree they produce is snapshotted and checked with `validate`, and a refusal raises inside the transaction so SQLite rolls every write back. Every write that adds, moves or sets aside a child re-derives its containers with Task 12's `roll_up_ancestors` in the same transaction; `rollup` itself sends a container waiting to land back to `READY` when a child comes back into play (§2.3 rule 6). Decisions move to their own vocabulary here: the writers store `OPEN`, `ANSWERED` and `WITHDRAWN`, and every reader that compared a decision's raw status goes through `stored_status`, which also reads the old names still on disk. Old task nodes still carry `NodeStatus` values until Task 21; `GraphEngine` reads the new `READY` exactly as it read `NOT_STARTED`, so the still-live old claim path keeps working until Task 18 removes it.

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_operations_lifecycle.py`:

```python
"""Operations on the lifecycle model: nodes start READY with their kind's flags, containers
roll up from their children, and every write passes the write rules before it commits."""

import subprocess
from pathlib import Path

import pytest

from taskmanager.core.enums import NodeKind, RenderView, VerificationType
from taskmanager.core.models import Lease, Node
from taskmanager.core.status import Action, ConditionStage, DecisionStatus, Merge, Outcome, Status
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.job_repo import JobRepository
from taskmanager.db.ledger_repo import LedgerRepository
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.engine.graph import GraphEngine
from taskmanager.engine.operations import OperationError, Operations
from taskmanager.engine.runtime import ExecutionCoordinator
from taskmanager.engine.verification import VerificationEngine
from taskmanager.renderers.markdown import MarkdownRenderer

Env = tuple[NodeRepository, RuntimeRepository, LedgerRepository, Operations]


def make_ops(root: Path) -> Env:
    db = DatabaseManager(root / ".taskmanager")
    db.init_all()
    node_repo = NodeRepository(db)
    runtime_repo = RuntimeRepository(db)
    ledger_repo = LedgerRepository(db)
    graph = GraphEngine(node_repo, runtime_repo)
    ops = Operations(
        node_repo,
        runtime_repo,
        graph,
        ExecutionCoordinator(node_repo, runtime_repo, graph),
        ledger_repo,
        VerificationEngine(root),
        actor="tester",
        job_repo=JobRepository(db),
    )
    return node_repo, runtime_repo, ledger_repo, ops


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return make_ops(tmp_path)


def get(repo: NodeRepository, node_id: str) -> Node:
    node = repo.get_node(node_id)
    assert node is not None
    return node


def events(ledger: LedgerRepository) -> int:
    return len(ledger.list_events(limit=10_000))


def set_status(repo: NodeRepository, node_id: str, status: Status) -> None:
    node = get(repo, node_id)
    node.status = status
    repo.save_node(node)


def tree(ops: Operations, **plan_flags: object) -> tuple[str, str, str]:
    spec = ops.add_spec("S", slug="S1")
    plan = ops.add_plan("P", spec, slug="P1", **plan_flags)  # type: ignore[arg-type]
    task = ops.add_task("T", plan, slug="T1")
    return spec, plan, task


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def repo_with_origin(root: Path, name: str) -> Path:
    origin = root / f"{name}.git"
    git(root, "init", "--bare", "-b", "main", str(origin))
    work = root / name
    git(root, "clone", str(origin), str(work))
    git(work, "config", "user.email", "ci@example.com")
    git(work, "config", "user.name", "CI")
    git(work, "commit", "--allow-empty", "-m", "init")
    git(work, "push", "origin", "HEAD:main")
    git(work, "fetch", "origin")
    return work


def test_new_nodes_start_ready_with_the_flags_of_their_kind(env: Env) -> None:
    node_repo, _runtime, _ledger, ops = env
    spec, plan, task = tree(ops)
    s, p, t = get(node_repo, spec), get(node_repo, plan), get(node_repo, task)
    assert (s.status, s.review, s.fix, s.merge) == (Status.READY, False, False, Merge.MAIN)
    assert (p.status, p.review, p.fix, p.merge) == (Status.READY, False, False, Merge.MAIN)
    assert (t.status, t.review, t.fix, t.merge) == (Status.READY, True, True, Merge.MAIN)


def test_add_plan_with_review_stores_the_flags_and_creates_no_gate_node(env: Env) -> None:
    node_repo, _runtime, _ledger, ops = env
    spec = ops.add_spec("S", slug="S1")
    plan = ops.add_plan("P", spec, slug="P1", review=True, fix=True)
    stored = get(node_repo, plan)
    assert (stored.review, stored.fix) == (True, True)
    assert node_repo.get_children(plan) == []
    assert node_repo.get_node(f"{plan}-REV") is None


@pytest.mark.parametrize(
    ("target", "changes"),
    [
        ("task", {"review": False}),
        ("task", {"fix": False}),
        ("spec", {"merge": Merge.PARENT}),
    ],
    ids=["fix-without-review", "review-without-fix-landing-on-main", "spec-on-a-parent"],
)
def test_update_refuses_a_flag_combination_the_write_rules_forbid(
    env: Env, target: str, changes: dict[str, object]
) -> None:
    node_repo, _runtime, ledger, ops = env
    spec, _plan, task = tree(ops)
    node_id = {"spec": spec, "task": task}[target]
    before = get(node_repo, node_id)
    count = events(ledger)
    with pytest.raises(OperationError) as exc:
        ops.update_node(node_id, **changes)  # type: ignore[arg-type]
    assert exc.value.status_code == 400
    after = get(node_repo, node_id)
    assert (after.review, after.fix, after.merge) == (before.review, before.fix, before.merge)
    assert events(ledger) == count


def test_landing_on_a_parent_is_refused_for_a_node_with_no_parent(env: Env) -> None:
    node_repo, _runtime, _ledger, ops = env
    node_repo.save_node(Node(id="LONE", kind=NodeKind.TASK, title="alone", status=Status.READY))
    with pytest.raises(OperationError) as exc:
        ops.update_node("LONE", merge=Merge.PARENT)
    assert exc.value.status_code == 400
    assert get(node_repo, "LONE").merge == Merge.MAIN


def test_review_without_fix_is_allowed_landing_where_the_parent_reviews_and_fixes(
    env: Env,
) -> None:
    node_repo, _runtime, _ledger, ops = env
    _spec, _plan, task = tree(ops, review=True, fix=True)
    ops.update_node(task, merge=Merge.PARENT, fix=False)
    stored = get(node_repo, task)
    assert (stored.review, stored.fix, stored.merge) == (True, False, Merge.PARENT)


def test_a_parent_write_that_leaves_a_child_rejection_unfixed_is_refused(env: Env) -> None:
    node_repo, _runtime, _ledger, ops = env
    _spec, plan, task = tree(ops, review=True, fix=True)
    ops.update_node(task, merge=Merge.PARENT, fix=False)
    with pytest.raises(OperationError) as exc:
        ops.update_node(plan, fix=False)
    assert exc.value.status_code == 400
    assert get(node_repo, plan).fix is True


def test_landing_on_a_parent_is_refused_for_a_test_command_naming_origin_main(env: Env) -> None:
    node_repo, _runtime, _ledger, ops = env
    _spec, _plan, task = tree(ops)
    ops.add_verification(
        task, VerificationType.TEST_COMMAND, "reads main", "git show origin/main:README.md"
    )
    with pytest.raises(OperationError) as exc:
        ops.update_node(task, merge=Merge.PARENT)
    assert exc.value.status_code == 400
    assert get(node_repo, task).merge == Merge.MAIN


def test_a_new_child_under_a_completed_plan_is_refused(env: Env) -> None:
    node_repo, _runtime, ledger, ops = env
    _spec, plan, _task = tree(ops)
    set_status(node_repo, plan, Status.COMPLETED)
    count = events(ledger)
    with pytest.raises(OperationError) as exc:
        ops.add_task("Late", plan, slug="LATE")
    assert exc.value.status_code == 409
    assert node_repo.get_node(f"{plan}-LATE") is None
    assert events(ledger) == count


def test_a_new_child_under_a_plan_waiting_to_land_sends_the_plan_back_to_ready(
    env: Env,
) -> None:
    node_repo, _runtime, _ledger, ops = env
    _spec, plan, task = tree(ops)
    set_status(node_repo, task, Status.COMPLETED)
    set_status(node_repo, plan, Status.IMPLEMENTED)
    ops.add_task("Late", plan, slug="LATE")
    assert get(node_repo, plan).status == Status.READY


def test_supersede_is_refused_while_the_node_holds_a_live_lease(env: Env) -> None:
    node_repo, runtime_repo, ledger, ops = env
    _spec, plan, task = tree(ops)
    other = ops.add_task("Other", plan, slug="T2")
    claimed = get(node_repo, task)
    claimed.status = Status.IMPLEMENTING
    claimed.claimed_from = Status.READY
    lease = Lease(
        task_id=task,
        agent_id="a",
        session_id="s",
        branch_name=f"tm/{task}",
        action=Action.IMPLEMENT,
        ttl_seconds=3600,
    )
    assert runtime_repo.claim(lease, [], claimed)
    count = events(ledger)
    with pytest.raises(OperationError) as exc:
        ops.supersede(task, other)
    assert exc.value.status_code == 409
    assert get(node_repo, task).status == Status.IMPLEMENTING
    assert runtime_repo.get_lease(task) is not None
    assert events(ledger) == count


def test_a_dependency_that_closes_a_cycle_is_refused_with_the_cycle_path(env: Env) -> None:
    node_repo, _runtime, ledger, ops = env
    _spec, plan, first = tree(ops)
    second = ops.add_task("Second", plan, slug="T2", depends_on=[first])
    count = events(ledger)
    with pytest.raises(OperationError) as exc:
        ops.set_dependencies(first, [second], [])
    assert exc.value.status_code == 409
    message = str(exc.value)
    assert "←" in message and f"{first}.start" in message
    assert node_repo.get_dependencies(first) == []
    assert events(ledger) == count


def test_a_task_depending_on_its_own_plan_is_refused_as_a_cycle(env: Env) -> None:
    node_repo, _runtime, _ledger, ops = env
    _spec, plan, _task = tree(ops)
    with pytest.raises(OperationError) as exc:
        ops.add_task("Loop", plan, slug="LOOP", depends_on=[plan])
    assert exc.value.status_code == 409
    assert node_repo.get_node(f"{plan}-LOOP") is None


def test_set_dependencies_takes_bare_ids_and_returns_the_edges_left(env: Env) -> None:
    node_repo, _runtime, _ledger, ops = env
    _spec, plan, first = tree(ops)
    second = ops.add_task("Second", plan, slug="T2")
    assert ops.set_dependencies(second, [first], []) == [first]
    assert ops.set_dependencies(second, [], [first]) == []
    assert node_repo.get_dependencies(second) == []


def test_moving_a_branch_cut_from_its_parent_onto_main_is_refused(env: Env, tmp_path: Path) -> None:
    node_repo, _runtime, _ledger, ops = env
    work = repo_with_origin(tmp_path, "core")
    _spec, plan, task = tree(ops, review=True, fix=True)
    ops.update_node(task, repo="core", merge=Merge.PARENT)
    git(work, "branch", "--no-track", f"tm/{plan}", "origin/main")
    git(work, "checkout", f"tm/{plan}")
    git(work, "commit", "--allow-empty", "-m", "container work")
    git(work, "branch", "--no-track", f"tm/{task}", f"tm/{plan}")
    git(work, "checkout", "--detach")
    with pytest.raises(OperationError) as exc:
        ops.update_node(task, merge=Merge.MAIN)
    assert exc.value.status_code == 409
    assert get(node_repo, task).merge == Merge.PARENT


def test_moving_a_branch_onto_a_parent_forked_from_the_same_commit_is_allowed(
    env: Env, tmp_path: Path
) -> None:
    node_repo, _runtime, _ledger, ops = env
    work = repo_with_origin(tmp_path, "core")
    _spec, plan, task = tree(ops, review=True, fix=True)
    ops.update_node(task, repo="core")
    git(work, "branch", "--no-track", f"tm/{task}", "origin/main")
    git(work, "branch", "--no-track", f"tm/{plan}", "origin/main")
    ops.update_node(task, merge=Merge.PARENT)
    assert get(node_repo, task).merge == Merge.PARENT


def test_superseding_the_last_unfinished_child_rolls_the_plan_up_to_implemented(
    env: Env,
) -> None:
    node_repo, _runtime, _ledger, ops = env
    _spec, plan, done = tree(ops)
    extra = ops.add_task("Extra", plan, slug="T2")
    set_status(node_repo, done, Status.COMPLETED)
    ops.supersede(extra, done, "none")
    assert get(node_repo, extra).status == Status.SUPERSEDED
    assert get(node_repo, plan).status == Status.IMPLEMENTED


def test_moving_a_task_rolls_up_the_plan_it_leaves_and_reopens_the_one_it_joins(
    env: Env,
) -> None:
    node_repo, _runtime, _ledger, ops = env
    spec, plan, done = tree(ops)
    moving = ops.add_task("Moving", plan, slug="T2")
    other = ops.add_plan("Other", spec, slug="P2")
    finished = ops.add_task("Finished", other, slug="T3")
    set_status(node_repo, done, Status.COMPLETED)
    set_status(node_repo, finished, Status.COMPLETED)
    set_status(node_repo, other, Status.IMPLEMENTED)
    ops.move_task(moving, other)
    assert get(node_repo, plan).status == Status.IMPLEMENTED
    assert get(node_repo, other).status == Status.READY


def test_land_order_is_refused_on_a_task(env: Env) -> None:
    _node_repo, _runtime, _ledger, ops = env
    _spec, _plan, task = tree(ops)
    with pytest.raises(OperationError) as exc:
        ops.update_node(task, land_order=["api"])
    assert exc.value.status_code == 400


def test_add_condition_stores_an_executable_check_and_remove_drops_it(env: Env) -> None:
    node_repo, _runtime, ledger, ops = env
    _spec, _plan, task = tree(ops)
    added = ops.add_condition(
        task, "staging is up", "curl -fsS https://staging.example/health", ConditionStage.LANDING
    )
    assert [(c.needs, c.stage) for c in node_repo.get_conditions(task)] == [
        ("staging is up", ConditionStage.LANDING)
    ]
    count = events(ledger)
    ops.remove_condition(task, added.idx)
    assert node_repo.get_conditions(task) == []
    assert events(ledger) == count + 1


@pytest.mark.parametrize("command", ["", "the design is signed off"], ids=["empty", "prose"])
def test_a_condition_without_an_executable_command_is_refused(env: Env, command: str) -> None:
    node_repo, _runtime, _ledger, ops = env
    _spec, _plan, task = tree(ops)
    with pytest.raises(OperationError) as exc:
        ops.add_condition(task, "sign-off", command)
    assert exc.value.status_code == 400
    assert "decision" in str(exc.value)
    assert node_repo.get_conditions(task) == []


def test_removing_a_condition_that_is_not_there_is_refused(env: Env) -> None:
    _node_repo, _runtime, _ledger, ops = env
    _spec, _plan, task = tree(ops)
    with pytest.raises(OperationError) as exc:
        ops.remove_condition(task, 7)
    assert exc.value.status_code == 404


def test_decisions_are_stored_in_their_own_vocabulary(env: Env) -> None:
    node_repo, _runtime, _ledger, ops = env
    _spec, _plan, task = tree(ops)
    decision = ops.add_decision("Which way?", slug="way", options=["a|A"], blocks=[task])
    assert get(node_repo, decision).status == DecisionStatus.OPEN
    ops.answer_decision(decision, option="a")
    assert get(node_repo, decision).status == DecisionStatus.ANSWERED
    ops.reopen_decision(decision)
    assert get(node_repo, decision).status == DecisionStatus.OPEN
    ops.withdraw_decision(decision, "moot")
    assert get(node_repo, decision).status == DecisionStatus.WITHDRAWN


def test_a_plans_brief_lists_the_children_whose_review_rejected(env: Env) -> None:
    node_repo, _runtime, _ledger, ops = env
    _spec, plan, rejected = tree(ops, review=True, fix=True)
    approved = ops.add_task("Approved", plan, slug="T2")
    ops.update_node(rejected, merge=Merge.PARENT, fix=False)
    for node_id, outcome in ((rejected, Outcome.REJECT), (approved, Outcome.APPROVE)):
        node = get(node_repo, node_id)
        node.status, node.outcome = Status.COMPLETED, outcome
        node_repo.save_node(node)
    renderer = MarkdownRenderer(node_repo)

    brief = renderer.render(plan, RenderView.SUBAGENT)
    assert "### Children whose review rejected" in brief
    assert f"- `{rejected}` (T): `tm section get {rejected}:review`" in brief
    assert f"`{approved}`" not in brief
    assert "Children whose review rejected" not in renderer.render(rejected, RenderView.SUBAGENT)
    assert "Children whose review rejected" not in renderer.render(plan, RenderView.FULL)
```

`tests/unit/test_import_lifecycle.py`:

```python
"""Import on the lifecycle model: flags, conditions and kind-aware statuses, and a refused
document writes nothing at all."""

from pathlib import Path
from typing import Any

import pytest

from taskmanager.core.status import ConditionStage, DecisionStatus, Merge, Status
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.node_repo import NodeRepository
from taskmanager.renderers.importers import BulkImporter


@pytest.fixture
def repo(tmp_path: Path) -> NodeRepository:
    db = DatabaseManager(tmp_path / ".taskmanager")
    db.init_all()
    return NodeRepository(db)


def doc(*tasks: dict[str, Any], plan: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "spec": {"id": "S", "title": "S"},
        "plans": [{"id": "S-P", "title": "P", **(plan or {}), "tasks": list(tasks)}],
    }


CHECK = "curl -fsS https://staging.example/health"


def test_import_stores_flags_merge_requires_land_order_and_conditions(
    repo: NodeRepository,
) -> None:
    BulkImporter(repo).import_dict(
        doc(
            {
                "id": "S-P-a",
                "title": "a",
                "merge": "parent",
                "fix": False,
                "requires": ["figma"],
                "conditions": [{"needs": "staging up", "command": CHECK, "stage": "landing"}],
            },
            plan={"review": True, "fix": True, "land_order": ["api", "web"]},
        )
    )
    task = repo.get_node("S-P-a")
    plan = repo.get_node("S-P")
    assert task is not None and plan is not None
    assert (task.status, task.review, task.fix, task.merge, task.requires) == (
        Status.READY,
        True,
        False,
        Merge.PARENT,
        ["figma"],
    )
    assert (plan.review, plan.fix, plan.land_order) == (True, True, ["api", "web"])
    assert [(c.needs, c.command, c.stage) for c in repo.get_conditions("S-P-a")] == [
        ("staging up", CHECK, ConditionStage.LANDING)
    ]


REFUSED = [
    pytest.param(doc({"id": "S-P-a", "title": "a", "review": False}), "fix", id="fix-no-review"),
    pytest.param(doc({"id": "S-P-a", "title": "a", "fix": False}), None, id="unfixed-on-main"),
    pytest.param({"spec": {"id": "S", "title": "S", "merge": "parent"}}, None, id="spec-on-parent"),
    pytest.param(
        doc(
            {"id": "S-P-a", "title": "a", "depends_on": ["S-P-b"]},
            {"id": "S-P-b", "title": "b", "depends_on": ["S-P-a"]},
        ),
        "←",
        id="cycle",
    ),
    pytest.param(
        doc({"id": "S-P-a", "title": "a", "depends_on": [{"id": "S-P", "gate": "REVIEWED"}]}),
        "gate",
        id="gated-edge",
    ),
    pytest.param(
        doc({"id": "S-P-a", "title": "a", "status": "NOT_STARTED"}), "NOT_STARTED", id="old-status"
    ),
    pytest.param(
        {"decisions": [{"id": "decision-x", "title": "Q", "status": "READY"}]},
        "READY",
        id="decision-with-a-cycle-status",
    ),
    pytest.param(
        doc(
            {
                "id": "S-P-a",
                "title": "a",
                "conditions": [{"needs": "sign-off", "command": "the design is signed off"}],
            }
        ),
        "decision",
        id="prose-condition",
    ),
]


@pytest.mark.parametrize(("document", "words"), REFUSED)
def test_a_refused_import_writes_nothing_and_says_why(
    repo: NodeRepository, document: dict[str, Any], words: str | None
) -> None:
    with pytest.raises(ValueError, match="nothing written") as exc:
        BulkImporter(repo).import_dict(document)
    if words is not None:
        assert words in str(exc.value)
    assert repo.list_nodes() == []


def test_importing_a_new_child_under_a_completed_plan_is_refused(repo: NodeRepository) -> None:
    importer = BulkImporter(repo)
    importer.import_dict(
        doc({"id": "S-P-a", "title": "a", "status": "COMPLETED"}, plan={"status": "COMPLETED"})
    )
    with pytest.raises(ValueError, match="nothing written"):
        importer.import_dict(doc({"id": "S-P-b", "title": "b"}))
    assert repo.get_node("S-P-b") is None


def test_an_imported_plan_whose_tasks_are_all_completed_rolls_up_to_implemented(
    repo: NodeRepository,
) -> None:
    BulkImporter(repo).import_dict(doc({"id": "S-P-a", "title": "a", "status": "COMPLETED"}))
    plan = repo.get_node("S-P")
    assert plan is not None and plan.status == Status.IMPLEMENTED


def test_a_document_that_states_conditions_replaces_the_set(repo: NodeRepository) -> None:
    importer = BulkImporter(repo)
    importer.import_dict(
        doc({"id": "S-P-a", "title": "a", "conditions": [{"needs": "a", "command": "true"}]})
    )
    importer.import_dict(
        doc({"id": "S-P-a", "title": "a", "conditions": [{"needs": "b", "command": "true"}]})
    )
    assert [c.needs for c in repo.get_conditions("S-P-a")] == ["b"]
    importer.import_dict(doc({"id": "S-P-a", "title": "a"}))
    assert [c.needs for c in repo.get_conditions("S-P-a")] == ["b"]


def test_a_decision_imports_with_its_own_status_and_defaults_to_open(
    repo: NodeRepository,
) -> None:
    BulkImporter(repo).import_dict(
        {
            "decisions": [
                {"id": "decision-x", "title": "Q", "status": "ANSWERED"},
                {"id": "decision-y", "title": "Q2"},
            ]
        }
    )
    x, y = repo.get_node("decision-x"), repo.get_node("decision-y")
    assert x is not None and y is not None
    assert (x.status, y.status) == (DecisionStatus.ANSWERED, DecisionStatus.OPEN)
```

`tests/integration/test_export_restore.py`:

```python
"""Export writes the lifecycle format with its marker; restore reads only that format."""

import json
from pathlib import Path

from typer.testing import CliRunner

from taskmanager.cli.main import app
from taskmanager.core.status import ConditionStage, Merge
from taskmanager.di.container import create_container
from taskmanager.engine.operations import Operations

runner = CliRunner()
CHECK = "curl -fsS https://staging.example/health"


def seeded(root: Path) -> None:
    assert runner.invoke(app, ["init", "-C", str(root)]).exit_code == 0
    ops = create_container(root).get(Operations)
    spec = ops.add_spec("S", slug="S1")
    plan = ops.add_plan("P", spec, slug="P1", review=True, fix=True)
    a = ops.add_task("a", plan, slug="a", merge=Merge.PARENT, requires=["figma"])
    ops.add_task("b", plan, slug="b", depends_on=[a])
    ops.update_node(a, fix=False)
    ops.update_node(plan, land_order=["api", "web"])
    ops.add_condition(a, "staging up", CHECK, ConditionStage.LANDING)


def test_export_writes_the_format_marker_flags_conditions_and_bare_dependencies(
    tmp_path: Path,
) -> None:
    seeded(tmp_path)
    out = tmp_path / "e"
    assert runner.invoke(app, ["export", str(out), "-C", str(tmp_path)]).exit_code == 0
    assert json.loads((out / "_format.json").read_text()) == {
        "format": "tm-lifecycle",
        "version": 1,
    }
    plan = json.loads((out / "S1-P1.json").read_text())["plans"][0]
    a, b = plan["tasks"]
    assert (plan["status"], plan["review"], plan["fix"], plan["land_order"]) == (
        "READY",
        True,
        True,
        ["api", "web"],
    )
    assert (a["status"], a["review"], a["fix"], a["merge"], a["requires"]) == (
        "READY",
        True,
        False,
        "parent",
        ["figma"],
    )
    assert a["conditions"] == [{"needs": "staging up", "command": CHECK, "stage": "landing"}]
    assert b["depends_on"] == ["S1-P1-a"]


def test_restore_rebuilds_an_export_that_exports_byte_identically(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    seeded(source)
    assert runner.invoke(app, ["export", str(tmp_path / "e1"), "-C", str(source)]).exit_code == 0
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    res = runner.invoke(app, ["restore", str(tmp_path / "e1"), "-C", str(fresh)])
    assert res.exit_code == 0, res.output
    assert runner.invoke(app, ["export", str(tmp_path / "e2"), "-C", str(fresh)]).exit_code == 0
    for f in sorted((tmp_path / "e1").glob("*.json")):
        assert f.read_bytes() == (tmp_path / "e2" / f.name).read_bytes(), f.name


def test_restore_refuses_a_pre_lifecycle_export_and_writes_nothing(tmp_path: Path) -> None:
    old = tmp_path / "old"
    old.mkdir()
    (old / "S1-P1.json").write_text(
        json.dumps(
            {"spec": None, "plans": [{"id": "S1-P1", "title": "P", "status": "NOT_STARTED"}]}
        ),
        encoding="utf-8",
    )
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    res = runner.invoke(app, ["restore", str(old), "-C", str(fresh)])
    assert res.exit_code == 1
    assert "v0.2.0" in res.output and "tm import" in res.output
    assert not (fresh / ".taskmanager").exists()
```

- [ ] **Step 2: Run them and watch them fail**

```
uv run --directory <worktree> pytest tests/unit/test_operations_lifecycle.py tests/unit/test_import_lifecycle.py tests/integration/test_export_restore.py -q; echo $?
```

Expected: exit 1. `add_decision` storing `NOT_STARTED` where `OPEN` is expected, `add_plan() got an unexpected keyword argument 'review'`, `update_node() got an unexpected keyword argument 'merge'`, `'Operations' object has no attribute 'add_condition'`, the importer's `unknown keys (… review …)` refusals where a flag is written, `set_dependencies` failing on bare ids, the plan's brief with no `### Children whose review rejected` heading, and the export test's missing `_format.json`.

- [ ] **Step 3: Implement**

**3a.** `src/taskmanager/core/enums.py`, inside `class LedgerCommand`, after `ATTACHMENT_CHECK`:

```python
    CONDITION_ADD = "condition_add"
    CONDITION_REMOVE = "condition_remove"
```

**3b.** `src/taskmanager/engine/operations.py`. The import block becomes (Task 12's own imports — `DecisionEffect`, `apply_effect`, `chosen_effect`, `node_busy` and the rest — stay in it):

```python
import hashlib
import logging
import sqlite3
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from taskmanager.core.enums import (
    LedgerCommand,
    NodeKind,
    NodeStatus,
    RelationType,
    TransferMode,
    VerificationType,
)
from taskmanager.core.models import (
    Condition,
    LedgerEvent,
    Node,
    NodeRelation,
    NodeSection,
    NodeVerification,
)
from taskmanager.core.rollup import rollup
from taskmanager.core.status import ConditionStage, DecisionStatus, Merge, Status
from taskmanager.db.job_repo import JobRepository
from taskmanager.db.ledger_repo import LedgerRepository
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.engine.assets import (
    AssetError,
    AttachmentSource,
    is_project_relative,
    store_asset,
)
from taskmanager.engine.chains import MAIN, landing_target
from taskmanager.engine.conditions import is_executable
from taskmanager.engine.decisions import (
    DecisionAnswer,
    DecisionData,
    DecisionOption,
    read_decision,
    write_decision,
)
from taskmanager.engine.graph import GraphEngine
from taskmanager.engine.runtime import ExecutionCoordinator
from taskmanager.engine.snapshot import (
    CONTAINERS,
    SnapshotBuilder,
    apply_cycle,
    cycle_of,
    roll_up_ancestors,
    stored_status,
)
from taskmanager.engine.stepgraph import Snapshot
from taskmanager.engine.validation import validate
from taskmanager.engine.verification import VerificationEngine, VerificationResult
```

Below `class OperationError`, add the module-level helpers:

```python
# Refusals that conflict with the tree's current state rather than with the request itself.
_CONFLICT_RULES = frozenset({4, 6, 7, 8})


class GitBranchFacts:
    """What the write rules need to know about a node's branches, read from git.

    A branch's recorded base is where it forks from its current landing target, so a new
    target keeps the base exactly when the branch forks from the new target at the same commit.
    """

    def __init__(self, root: Path, node_repo: NodeRepository, tree: Snapshot) -> None:
        self.root = root
        self.node_repo = node_repo
        self.tree = tree

    def _branch(self, node_id: str) -> str:
        node = self.node_repo.get_node(node_id)
        return node.branch if node is not None and node.branch else f"tm/{node_id}"

    def _repos(self, node_id: str) -> list[Path]:
        ids = [node_id, *self.tree.descendants(node_id)] if node_id in self.tree.nodes else []
        names = sorted(
            {repo for i in ids if (snap := self.tree.nodes.get(i)) and (repo := snap.repo)}
        )
        dirs = [self.root / name for name in names] or [self.root]
        return [d for d in dirs if (d / ".git").exists()]

    @staticmethod
    def _git(repo: Path, *args: str) -> str | None:
        res = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False)
        return res.stdout.strip() if res.returncode == 0 else None

    def _has(self, repo: Path, ref: str) -> bool:
        return self._git(repo, "rev-parse", "--verify", "--quiet", ref) is not None

    def _ref(self, repo: Path, target: str) -> str:
        # A container branch not yet cut in this repository would be cut from its own base.
        while target != MAIN:
            branch = self._branch(target)
            if self._has(repo, f"refs/heads/{branch}"):
                return branch
            target = landing_target(self.tree, target)
        return "origin/main" if self._has(repo, "origin/main") else "main"

    def branch_exists(self, node_id: str) -> bool:
        ref = f"refs/heads/{self._branch(node_id)}"
        return any(self._has(repo, ref) for repo in self._repos(node_id))

    def base_matches(self, node_id: str, new_target: str) -> bool:
        branch = self._branch(node_id)
        current = landing_target(self.tree, node_id)
        for repo in self._repos(node_id):
            if not self._has(repo, f"refs/heads/{branch}"):
                continue
            recorded = self._git(repo, "merge-base", branch, self._ref(repo, current))
            proposed = self._git(repo, "merge-base", branch, self._ref(repo, new_target))
            if recorded is None or recorded != proposed:
                return False
        return True


@contextmanager
def validated_write(
    node_repo: NodeRepository,
    snapshots: SnapshotBuilder,
    touched: set[str],
    prefix: str = "Nothing changed: ",
) -> Iterator[None]:
    """One transaction whose result is checked against every write rule before it commits.

    The writes run first so the check reads the tree they produce; a refusal raises inside the
    transaction, which rolls every write back.
    """
    root = node_repo.db.taskmanager_dir.parent
    with node_repo.transaction():
        before = snapshots.build()
        yield
        after = snapshots.build()
        scope = {n for n in touched if n in after.nodes}
        for node_id in list(scope):
            scope.update(after.children(node_id))
        refusals = validate(before, after, scope, GitBranchFacts(root, node_repo, before))
        if refusals:
            code = 409 if any(r.rule in _CONFLICT_RULES for r in refusals) else 400
            raise OperationError(prefix + "; ".join(r.message for r in refusals), code)


def _roll_up_container(node_repo: NodeRepository, container_id: str) -> None:
    """Re-derive a container that lost a child, then its ancestors."""
    children = node_repo.get_children(container_id)
    if children:
        roll_up_ancestors(node_repo, children[0])
        return
    node = node_repo.get_node(container_id)
    if node is None or node.kind not in CONTAINERS:
        return
    current = cycle_of(node)
    derived = rollup(current.status, [])
    if derived != current.status:
        node_repo.save_node(apply_cycle(node, replace(current, status=derived)))
    roll_up_ancestors(node_repo, container_id)
```

In `class Operations`, Task 12's `__init__` gains one line after `self.job_repo = job_repo`, and a helper method follows `with_actor`:

```python
self.snapshots = SnapshotBuilder(node_repo, runtime_repo, job_repo or JobRepository(node_repo.db))
```

```python
def _checked(self, touched: set[str]) -> Any:
    return validated_write(self.node_repo, self.snapshots, touched)


@staticmethod
def _refuse_fix_without_review(node: Node) -> None:
    # The column CHECK would fire before the snapshot check could explain it.
    if node.fix and not node.review:
        raise OperationError(
            f"{node.id}: fix needs review: a rejection is fixed by the node that was "
            "reviewed; turn review on or fix off",
            400,
        )
```

`add_spec`, `add_plan`, `add_task` are replaced in full:

```python
def add_spec(
    self,
    title: str,
    slug: str | None = None,
    priority: int = 50,
    order: int = 0,
    review: bool = False,
    fix: bool = False,
) -> str:
    self._validate_priority(priority)
    if slug:
        spec_id = slug
        if self.node_repo.get_node(spec_id) is not None:
            raise OperationError(f"'{spec_id}' already exists", 409)
    else:
        existing = {n.id for n in self.node_repo.list_nodes(kind=NodeKind.SPEC)}
        counter = 1
        while f"S{counter}" in existing:
            counter += 1
        spec_id = f"S{counter}"

    node = Node(
        id=spec_id,
        kind=NodeKind.SPEC,
        title=title,
        priority=priority,
        ordinal=order,
        status=Status.READY,
        review=review,
        fix=fix,
        merge=Merge.MAIN,
    )
    self._refuse_fix_without_review(node)
    with self._checked({spec_id}):
        self.node_repo.save_node(node)
    self._ledger(
        LedgerCommand.SPEC_ADD,
        target_id=spec_id,
        payload={
            "title": title,
            "priority": priority,
            "ordinal": order,
            "review": review,
            "fix": fix,
        },
    )
    return spec_id


def add_plan(
    self,
    title: str,
    spec: str,
    slug: str | None = None,
    priority: int = 50,
    order: int = 0,
    review: bool = False,
    fix: bool = False,
    merge: Merge = Merge.MAIN,
) -> str:
    self._validate_priority(priority)
    if self.node_repo.get_node(spec) is None:
        raise OperationError(f"spec '{spec}' not found", 404)
    if slug:
        plan_id = f"{spec}-{slug}"
        if self.node_repo.get_node(plan_id) is not None:
            raise OperationError(f"'{plan_id}' already exists", 409)
    else:
        children = set(self.node_repo.get_children(spec))
        counter = 1
        while f"{spec}-P{counter}" in children:
            counter += 1
        plan_id = f"{spec}-P{counter}"

    plan_node = Node(
        id=plan_id,
        kind=NodeKind.PLAN,
        title=title,
        priority=priority,
        ordinal=order,
        status=Status.READY,
        review=review,
        fix=fix,
        merge=merge,
    )
    self._refuse_fix_without_review(plan_node)
    with self._checked({plan_id}):
        self.node_repo.save_node(plan_node)
        self.node_repo.add_relation(
            NodeRelation(source_id=spec, target_id=plan_id, relation_type=RelationType.CONTAINS)
        )
        roll_up_ancestors(self.node_repo, plan_id)
    self._ledger(
        LedgerCommand.PLAN_ADD,
        target_id=plan_id,
        payload={
            "title": title,
            "spec": spec,
            "ordinal": order,
            "review": review,
            "fix": fix,
            "merge": merge.value,
        },
    )
    return plan_id


def add_task(
    self,
    title: str,
    plan: str,
    slug: str | None = None,
    priority: int = 50,
    order: int = 0,
    depends_on: list[str] | None = None,
    models: list[str] | None = None,
    review: bool = True,
    fix: bool = True,
    merge: Merge = Merge.MAIN,
    requires: list[str] | None = None,
) -> str:
    self._validate_priority(priority)
    if self.node_repo.get_node(plan) is None:
        raise OperationError(f"plan '{plan}' not found", 404)
    missing_deps = [d for d in (depends_on or []) if self.node_repo.get_node(d) is None]
    if missing_deps:
        raise OperationError(f"dependency not found: {', '.join(missing_deps)}", 404)
    if slug:
        task_id = f"{plan}-{slug}"
        if self.node_repo.get_node(task_id) is not None:
            raise OperationError(f"'{task_id}' already exists", 409)
    else:
        children = set(self.node_repo.get_children(plan))
        counter = 1
        while f"{plan}-T{counter}" in children:
            counter += 1
        task_id = f"{plan}-T{counter}"

    task_node = Node(
        id=task_id,
        kind=NodeKind.TASK,
        title=title,
        priority=priority,
        ordinal=order,
        acceptable_models=models or [],
        status=Status.READY,
        review=review,
        fix=fix,
        merge=merge,
        requires=requires or [],
    )
    self._refuse_fix_without_review(task_node)
    with self._checked({task_id}):
        self.node_repo.save_node(task_node)
        self.node_repo.add_relation(
            NodeRelation(source_id=plan, target_id=task_id, relation_type=RelationType.CONTAINS)
        )
        for dep in depends_on or []:
            self.node_repo.add_relation(
                NodeRelation(
                    source_id=task_id, target_id=dep, relation_type=RelationType.DEPENDS_ON
                )
            )
        roll_up_ancestors(self.node_repo, task_id)

    self._ledger(LedgerCommand.TASK_ADD, target_id=task_id, payload={"title": title, "plan": plan})
    return task_id
```

`update_node`, `set_dependencies`, `supersede`, `move_task` are replaced in full:

```python
def update_node(
    self,
    node_id: str,
    title: str | None = None,
    priority: int | None = None,
    models: list[str] | None = None,
    repo: str | None = None,
    frontmatter_set: dict[str, Any] | None = None,
    frontmatter_unset: list[str] | None = None,
    review: bool | None = None,
    fix: bool | None = None,
    merge: Merge | None = None,
    requires: list[str] | None = None,
    land_order: list[str] | None = None,
) -> dict[str, Any]:
    node = self.node_repo.get_node(node_id)
    if node is None:
        raise OperationError(f"task '{node_id}' not found", 404)
    changed: dict[str, Any] = {}
    if title is not None:
        node.title = title
        changed["title"] = title
    if priority is not None:
        if not 1 <= priority <= 100:
            raise OperationError("priority is 1-100", 400)
        node.priority = priority
        changed["priority"] = priority
    if models is not None:
        node.acceptable_models = models
        changed["acceptable_models"] = node.acceptable_models
    if repo is not None:
        node.target_repo = repo
        changed["target_repo"] = repo
    for key, value in (frontmatter_set or {}).items():
        node.frontmatter[key] = value
        changed[f"frontmatter.{key}"] = value
    for key in frontmatter_unset or []:
        node.frontmatter.pop(key, None)
        changed[f"frontmatter.{key}"] = None
    if review is not None:
        node.review = review
        changed["review"] = review
    if fix is not None:
        node.fix = fix
        changed["fix"] = fix
    if merge is not None:
        node.merge = merge
        changed["merge"] = merge.value
    if requires is not None:
        node.requires = requires
        changed["requires"] = requires
    if land_order is not None:
        if node.kind not in CONTAINERS:
            raise OperationError(
                "land_order orders a plan's or a spec's repositories; a task lands in its "
                "one target_repo",
                400,
            )
        node.land_order = land_order
        changed["land_order"] = land_order
    if not changed:
        raise OperationError("nothing to update", 400)
    self._refuse_fix_without_review(node)
    node.updated_at = datetime.now(tz=UTC)
    with self._checked({node_id}):
        self.node_repo.save_node(node)
    self._ledger(LedgerCommand.TASK_UPDATE, target_id=node_id, payload=changed)
    return changed


def set_dependencies(self, node_id: str, add: list[str], remove: list[str]) -> list[str]:
    if self.node_repo.get_node(node_id) is None:
        raise OperationError(f"Task '{node_id}' not found", 404)
    current = set(self.node_repo.get_dependencies(node_id))
    problems = [f"'{dep}' does not exist" for dep in add if self.node_repo.get_node(dep) is None]
    problems += [f"'{dep}' is not a dependency" for dep in remove if dep not in current]
    if problems:
        raise OperationError(f"Nothing changed: {'; '.join(problems)}", 409)
    with self._checked({node_id}):
        for dep in add:
            self.node_repo.add_relation(
                NodeRelation(
                    source_id=node_id, target_id=dep, relation_type=RelationType.DEPENDS_ON
                )
            )
        for dep in remove:
            self.node_repo.remove_relation(node_id, dep, RelationType.DEPENDS_ON)
    self._ledger(
        LedgerCommand.TASK_DEPENDS,
        target_id=node_id,
        payload={"add": add, "remove": remove},
    )
    return self.node_repo.get_dependencies(node_id)


def supersede(
    self, old_id: str, new_id: str, transfer_blocks: str = TransferMode.ALL.value
) -> None:
    old_node = self.node_repo.get_node(old_id)
    if not old_node:
        raise OperationError(f"Task '{old_id}' not found", 404)
    if new_id == old_id or self.node_repo.get_node(new_id) is None:
        raise OperationError(f"Replacement task '{new_id}' not found; nothing was changed", 400)

    old_node.status = Status.SUPERSEDED
    old_node.claimed_from = None
    old_node.updated_at = datetime.now(tz=UTC)
    touched = {old_id, new_id, *self.node_repo.get_blocked_by(old_id)}
    with self._checked(touched):
        self.node_repo.save_node(old_node)
        self.node_repo.add_relation(
            NodeRelation(source_id=new_id, target_id=old_id, relation_type=RelationType.SUPERSEDES)
        )
        tb_val = transfer_blocks.strip().lower()
        if tb_val == TransferMode.ALL.value:
            self.node_repo.transfer_blocks(old_id, new_id, TransferMode.ALL)
        elif tb_val == TransferMode.NONE.value:
            self.node_repo.transfer_blocks(old_id, new_id, TransferMode.NONE)
        else:
            custom_ids = [x.strip() for x in transfer_blocks.split(",") if x.strip()]
            self.node_repo.transfer_blocks(
                old_id, new_id, TransferMode.CUSTOM, custom_ids=custom_ids
            )
        roll_up_ancestors(self.node_repo, old_id)

    self._ledger(
        LedgerCommand.TASK_SUPERSEDE,
        target_id=old_id,
        payload={"superseded_by": new_id, "transfer_blocks": transfer_blocks},
    )


def move_task(self, task_id: str, plan_id: str) -> None:
    task_node = self.node_repo.get_node(task_id)
    if task_node is None:
        raise OperationError(f"Task '{task_id}' not found", 404)
    if task_node.kind != NodeKind.TASK:
        raise OperationError(f"'{task_id}' is not a task", 400)
    plan_node = self.node_repo.get_node(plan_id)
    if plan_node is None:
        raise OperationError(f"Plan '{plan_id}' not found", 404)
    if plan_node.kind != NodeKind.PLAN:
        raise OperationError(f"'{plan_id}' is not a plan", 400)
    parents = self.node_repo.get_parent_ids(task_id)
    old_plan = parents[0] if parents else None
    with self._checked({task_id}):
        if old_plan is not None:
            self.node_repo.remove_relation(old_plan, task_id, RelationType.CONTAINS)
        self.node_repo.add_relation(
            NodeRelation(source_id=plan_id, target_id=task_id, relation_type=RelationType.CONTAINS)
        )
        roll_up_ancestors(self.node_repo, task_id)
        if old_plan is not None:
            _roll_up_container(self.node_repo, old_plan)
    self._ledger(
        LedgerCommand.TASK_MOVE, target_id=task_id, payload={"from": old_plan, "to": plan_id}
    )
```

The conditions follow the verifications section of the class:

```python
# -- conditions ---------------------------------------------------------------------


def add_condition(
    self,
    node_id: str,
    needs: str,
    command: str,
    stage: ConditionStage = ConditionStage.CLAIM,
) -> Condition:
    node = self.node_repo.get_node(node_id)
    if node is None:
        raise OperationError(f"node '{node_id}' not found", 404)
    if node.kind == NodeKind.DECISION:
        raise OperationError(
            f"'{node_id}' is a decision; a condition holds a task, plan or spec", 400
        )
    if not needs.strip():
        raise OperationError("a condition names the state it waits for in --needs", 400)
    if not command.strip() or not is_executable(command):
        raise OperationError(
            f"'{command}' is not a command that exits 0 once '{needs}' holds; a wait nobody "
            f'can check is a decision: `tm decision add "..." --blocks {node_id}`',
            400,
        )
    stored = self.node_repo.add_condition(
        Condition(node_id=node_id, idx=0, needs=needs, command=command, stage=stage)
    )
    self._ledger(
        LedgerCommand.CONDITION_ADD,
        target_id=node_id,
        payload={"idx": stored.idx, "needs": needs, "stage": stage.value},
    )
    return stored


def remove_condition(self, node_id: str, idx: int) -> None:
    if not self.node_repo.remove_condition(node_id, idx):
        raise OperationError(f"'{node_id}' has no condition {idx}", 404)
    self._ledger(LedgerCommand.CONDITION_REMOVE, target_id=node_id, payload={"idx": idx})
```

Decisions are written in their own vocabulary. In Task 12's `add_decision`, the `Node(...)` it saves gains `status=DecisionStatus.OPEN,` after `kind=NodeKind.DECISION,`. In `answer_decision`, `if node.status != NodeStatus.NOT_STARTED:` becomes `if stored_status(node) != DecisionStatus.OPEN:` and `node.status = NodeStatus.COMPLETED` becomes `node.status = DecisionStatus.ANSWERED`. `reopen_decision` and `withdraw_decision` are replaced in full:

```python
def reopen_decision(self, decision_id: str) -> None:
    node = self._get_decision(decision_id)
    if stored_status(node) == DecisionStatus.OPEN:
        raise OperationError(f"decision '{decision_id}' is already open", 409)
    data = read_decision(node)
    data.answer = None
    data.withdrawn_reason = ""
    write_decision(node, data)
    node.status = DecisionStatus.OPEN
    node.updated_at = datetime.now(tz=UTC)
    self.node_repo.save_node(node)
    self._ledger(LedgerCommand.DECISION_REOPEN, target_id=decision_id)


def withdraw_decision(self, decision_id: str, reason: str = "") -> None:
    node = self._get_decision(decision_id)
    data = read_decision(node)
    data.withdrawn_reason = reason
    write_decision(node, data)
    node.status = DecisionStatus.WITHDRAWN
    node.updated_at = datetime.now(tz=UTC)
    self.node_repo.save_node(node)
    self._ledger(LedgerCommand.DECISION_WITHDRAW, target_id=decision_id, payload={"reason": reason})
```

`add_plan` no longer calls `self.graph.inject_plan_review_gate`; `LedgerCommand.PLAN_REVIEW_GATE` stays in the enum because written ledgers name it.

**3c.** `src/taskmanager/renderers/importers.py` is replaced in full:

```python
from enum import StrEnum
from typing import Any

from taskmanager.core.enums import NodeKind, RelationType, VerificationType
from taskmanager.core.models import (
    Condition,
    Node,
    NodeRelation,
    NodeSection,
    NodeVerification,
)
from taskmanager.core.status import ConditionStage, DecisionStatus, Merge, Outcome, Status
from taskmanager.db.job_repo import JobRepository
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.engine.conditions import is_executable
from taskmanager.engine.operations import validated_write
from taskmanager.engine.snapshot import SnapshotBuilder, roll_up_ancestors

REFUSED = "import refused, nothing written: "


def _optional[E: StrEnum](kind: type[E], value: Any) -> E | None:
    return None if value is None else kind(value)


class BulkImporter:
    def __init__(self, node_repo: NodeRepository) -> None:
        self.node_repo = node_repo
        self.snapshots = SnapshotBuilder(
            node_repo, RuntimeRepository(node_repo.db), JobRepository(node_repo.db)
        )

    NODE_KEYS = frozenset(
        {
            "id",
            "kind",
            "title",
            "status",
            "priority",
            "ordinal",
            "target_repo",
            "acceptable_models",
            "frontmatter",
            "sections",
            "depends_on",
            "verifications",
            "tasks",
            "review",
            "fix",
            "merge",
            "requires",
            "conditions",
            "land_order",
            "branch",
            "outcome",
            "verdict",
            "fix_for",
            "claimed_from",
            "review_cycles",
            "merge_attempts",
            "step_failures",
        }
    )
    DOCUMENT_KEYS = frozenset({"spec", "plans", "tasks", "decisions"})
    VERIFICATION_KEYS = frozenset(
        {"type", "verification_type", "target_path", "expected_pattern", "codegraph_query_json"}
    )
    CONDITION_KEYS = frozenset({"needs", "command", "stage"})

    def _refuse_unknown_keys(self, data: dict[str, Any]) -> None:
        """A key nothing reads would be dropped silently, and its content with it."""
        problems: list[str] = []

        def check(where: str, doc: dict[str, Any], allowed: frozenset[str]) -> None:
            extra = sorted(set(doc) - allowed)
            if extra:
                problems.append(f"{where}: {', '.join(extra)}")

        check("document", data, self.DOCUMENT_KEYS)
        nodes: list[tuple[str, dict[str, Any]]] = []
        if isinstance(data.get("spec"), dict):
            nodes.append(("spec", data["spec"]))
        for plan in data.get("plans") or []:
            nodes.append((f"plan {plan.get('id')}", plan))
            nodes.extend((f"task {t.get('id')}", t) for t in plan.get("tasks") or [])
        nodes.extend((f"task {t.get('id')}", t) for t in data.get("tasks") or [])
        nodes.extend((f"decision {d.get('id')}", d) for d in data.get("decisions") or [])
        for where, node in nodes:
            check(where, node, self.NODE_KEYS)
            for v in node.get("verifications") or []:
                check(f"{where} verification", v, self.VERIFICATION_KEYS)
            for c in node.get("conditions") or []:
                check(f"{where} condition", c, self.CONDITION_KEYS)
        if problems:
            raise ValueError(
                "import refused, nothing written: unknown keys ("
                + "; ".join(problems)
                + "). Section text belongs under `sections:`, other data under `frontmatter:`."
            )

    def import_dict(self, data: dict[str, Any]) -> None:
        self._refuse_unknown_keys(data)
        nodes: list[Node] = []
        sections: list[NodeSection] = []
        relations: list[NodeRelation] = []
        verifications: dict[str, list[NodeVerification]] = {}
        conditions: dict[str, list[Condition]] = {}

        def take(raw: dict[str, Any], kind: NodeKind, parent: str | None) -> None:
            node = self._parse_node(raw, kind, self.node_repo.get_node(raw["id"]))
            nodes.append(node)
            sections.extend(self._parse_sections(node.id, raw.get("sections")))
            if parent is not None:
                relations.append(
                    NodeRelation(
                        source_id=parent, target_id=node.id, relation_type=RelationType.CONTAINS
                    )
                )
            relations.extend(
                NodeRelation(
                    source_id=node.id,
                    target_id=self._parse_dep(dep),
                    relation_type=RelationType.DEPENDS_ON,
                )
                for dep in raw.get("depends_on", [])
            )
            if "verifications" in raw:
                verifications[node.id] = [
                    self._parse_verification(node.id, v) for v in raw["verifications"]
                ]
            if "conditions" in raw:
                conditions[node.id] = [self._parse_condition(node.id, c) for c in raw["conditions"]]

        spec_data = data.get("spec")
        spec_id = spec_data["id"] if spec_data else None
        if spec_data:
            take(spec_data, NodeKind.SPEC, None)
        for p_idx, plan_data in enumerate(data.get("plans", []), start=1):
            plan_data.setdefault("ordinal", p_idx)
            take(plan_data, NodeKind.PLAN, spec_id)
            for t_idx, task_data in enumerate(plan_data.get("tasks", []), start=1):
                task_data.setdefault("ordinal", t_idx)
                take(task_data, NodeKind.TASK, plan_data["id"])
        for task_data in data.get("tasks", []):
            take(task_data, NodeKind.TASK, spec_id)
        for dec_data in data.get("decisions", []):
            take(dec_data, NodeKind.DECISION, None)

        known = {n.id for n in nodes}
        unknown = sorted(
            {
                i
                for r in relations
                for i in (r.source_id, r.target_id)
                if i not in known and self.node_repo.get_node(i) is None
            }
        )
        if unknown:
            raise ValueError(f"{REFUSED}unknown ids {unknown}")

        with validated_write(self.node_repo, self.snapshots, known, prefix=REFUSED):
            for node in nodes:
                self.node_repo.save_node(node)
            for section in sections:
                self.node_repo.save_section(section)
            for rel in relations:
                self.node_repo.add_relation(rel)
            # A document that states a node's checks or conditions replaces the set it had.
            for node_id, vers in verifications.items():
                self.node_repo.clear_verifications(node_id)
                for ver in vers:
                    self.node_repo.add_verification(ver)
            for node_id, conds in conditions.items():
                for old in self.node_repo.get_conditions(node_id):
                    self.node_repo.remove_condition(node_id, old.idx)
                for cond in conds:
                    self.node_repo.add_condition(cond)
            for node in nodes:
                roll_up_ancestors(self.node_repo, node.id)

    @staticmethod
    def _parse_dep(dep: Any) -> str:
        if isinstance(dep, dict):
            raise ValueError(
                f"{REFUSED}a dependency is a bare id, not {dep!r}: an edge no longer carries a "
                "gate, it waits for the dependency's code to land where this node builds"
            )
        return str(dep)

    @staticmethod
    def _parse_verification(node_id: str, raw: dict[str, Any]) -> NodeVerification:
        return NodeVerification(
            node_id=node_id,
            verification_type=VerificationType(raw.get("verification_type") or raw.get("type")),
            target_path=raw["target_path"],
            expected_pattern=raw.get("expected_pattern"),
            codegraph_query_json=raw.get("codegraph_query_json"),
        )

    @staticmethod
    def _parse_condition(node_id: str, raw: dict[str, Any]) -> Condition:
        needs = str(raw.get("needs") or "")
        command = str(raw.get("command") or "")
        if not command.strip() or not is_executable(command):
            raise ValueError(
                f"{REFUSED}condition {needs!r} on {node_id!r} has no command that exits 0 once "
                "it holds; a wait nobody can check is a decision"
            )
        return Condition(
            node_id=node_id,
            idx=0,
            needs=needs,
            command=command,
            stage=ConditionStage(raw.get("stage", ConditionStage.CLAIM.value)),
        )

    @staticmethod
    def _parse_status(node_id: str, kind: NodeKind, value: Any) -> Status | DecisionStatus:
        allowed: type[Status] | type[DecisionStatus] = (
            DecisionStatus if kind == NodeKind.DECISION else Status
        )
        try:
            return allowed(value)
        except ValueError:
            names = ", ".join(s.value for s in allowed)
            raise ValueError(
                f"{REFUSED}node {node_id!r} has status {value!r}, which a {kind.value} cannot "
                f"hold; one of: {names}"
            ) from None

    @staticmethod
    def _parse_node(
        data: dict[str, Any], default_kind: NodeKind, existing: Node | None = None
    ) -> Node:
        """A key the document omits keeps the value the node already has, so importing a document
        again never resets the progress recorded since; a key it states wins."""

        def pick(key: str, default: Any) -> Any:
            if key in data:
                return data[key]
            return getattr(existing, key) if existing is not None else default

        node_id = data["id"]
        kind = NodeKind(data.get("kind", default_kind))
        title = pick("title", None)
        if title is None:
            raise ValueError(
                f"{REFUSED}node {node_id!r} has no title and none exists to fall back to"
            )
        if "status" in data:
            status = BulkImporter._parse_status(node_id, kind, data["status"])
        elif existing is not None:
            status = existing.status
        else:
            status = DecisionStatus.OPEN if kind == NodeKind.DECISION else Status.READY
        review = bool(pick("review", kind == NodeKind.TASK))
        fix = bool(pick("fix", kind == NodeKind.TASK))
        if fix and not review:
            raise ValueError(
                f"{REFUSED}node {node_id!r} sets fix without review: a rejection is fixed by "
                "the node that was reviewed; set review or drop fix"
            )
        return Node(
            id=node_id,
            kind=kind,
            title=title,
            status=status,
            priority=pick("priority", 50),
            ordinal=data.get("ordinal", existing.ordinal if existing is not None else 0),
            target_repo=pick("target_repo", None),
            acceptable_models=pick("acceptable_models", []),
            frontmatter=pick("frontmatter", {}),
            review=review,
            fix=fix,
            merge=Merge(pick("merge", Merge.MAIN)),
            requires=list(pick("requires", [])),
            land_order=list(pick("land_order", [])),
            branch=pick("branch", None),
            outcome=_optional(Outcome, pick("outcome", None)),
            verdict=pick("verdict", None),
            fix_for=_optional(Outcome, pick("fix_for", None)),
            claimed_from=_optional(Status, pick("claimed_from", None)),
            review_cycles=int(pick("review_cycles", 0)),
            merge_attempts=int(pick("merge_attempts", 0)),
            step_failures=int(pick("step_failures", 0)),
        )

    @staticmethod
    def _parse_sections(node_id: str, sections_data: Any) -> list[NodeSection]:
        parsed: list[NodeSection] = []
        if not sections_data:
            return parsed

        if isinstance(sections_data, list):
            for idx, item in enumerate(sections_data, start=1):
                if isinstance(item, NodeSection):
                    parsed.append(item)
                elif isinstance(item, dict):
                    sec_key = str(item.get("section_key") or item.get("key") or f"section_{idx}")
                    header = str(item.get("header") or f"## {sec_key.capitalize()}")
                    content = str(item.get("content", ""))
                    ordinal = int(item.get("ordinal", idx))
                    parsed.append(
                        NodeSection(
                            node_id=node_id,
                            section_key=sec_key,
                            ordinal=ordinal,
                            header=header,
                            content=content,
                        )
                    )
        elif isinstance(sections_data, dict):
            for idx, (key, val) in enumerate(sections_data.items(), start=1):
                if isinstance(val, dict):
                    sec_key = str(val.get("section_key") or val.get("key") or key)
                    header = str(val.get("header") or f"## {sec_key.capitalize()}")
                    content = str(val.get("content", ""))
                    ordinal = int(val.get("ordinal", idx))
                else:
                    sec_key = str(key)
                    header = f"## {sec_key.capitalize()}"
                    content = str(val)
                    ordinal = idx
                parsed.append(
                    NodeSection(
                        node_id=node_id,
                        section_key=sec_key,
                        ordinal=ordinal,
                        header=header,
                        content=content,
                    )
                )

        return parsed
```

**3d.** `src/taskmanager/engine/graph.py` (Task 8 already imports `Status` and `DecisionStatus` here). Add `from taskmanager.engine.snapshot import stored_status` and, below `_BLOCKED_STATES`:

```python
# Nodes written through Operations or import start at READY; the old readers treat it as the
# unclaimed start their NOT_STARTED was.
_UNSTARTED = frozenset({NodeStatus.NOT_STARTED.value, Status.READY.value})
```

In `GraphEngine.resolve_task_state`, the line `if node.status != NodeStatus.NOT_STARTED:` becomes `if node.status not in _UNSTARTED:`. In `GraphEngine.resolve_plan_status`, `cn.status == NodeStatus.NOT_STARTED and not self._is_task_in_flight(cn.id)` becomes `cn.status in _UNSTARTED and not self._is_task_in_flight(cn.id)`. In `GraphEngine._has_open_decision`, `and dep_node.status not in (NodeStatus.COMPLETED, NodeStatus.ABANDONED)` becomes `and stored_status(dep_node) == DecisionStatus.OPEN`, and the comment above it becomes `# A decision holds its dependents only while it is open; stored_status also reads the old names.`

The other readers of a decision's raw status move the same way:
- `src/taskmanager/engine/wave.py`, `_awaiting_decisions`: `and dep.status not in (NodeStatus.COMPLETED, NodeStatus.ABANDONED)` becomes `and stored_status(dep) == DecisionStatus.OPEN` (imports: `from taskmanager.core.status import DecisionStatus`, and `stored_status` joins the existing `from taskmanager.engine.snapshot import writes_migration`).
- `src/taskmanager/renderers/markdown.py`: `elif node.status == NodeStatus.ABANDONED:` becomes `elif stored_status(node) == DecisionStatus.WITHDRAWN:` (imports: `from taskmanager.core.status import DecisionStatus, Outcome`, `from taskmanager.engine.snapshot import CONTAINERS, stored_status`). The `subagent` view is the brief a container's reviewer reads, so it lists the children that landed on the container with a rejection nobody below fixed (§3.3). In `render`, inside `if v == RenderView.SUBAGENT:`, between the verifications block and `return "\n\n".join(out) + "\n"`:

```python
            rejected = self._rejected_children(node)
            if rejected:
                out.append(
                    "\n".join(
                        [
                            "### Children whose review rejected",
                            "Each landed on this branch with its findings unfixed; they are this "
                            "container's to fix:",
                            *rejected,
                        ]
                    )
                )
```

and a method after `render_recursive`:

```python
    def _rejected_children(self, node: Any) -> list[str]:
        # Read from each child's stored outcome on every render, never copied into a section.
        if node.kind not in CONTAINERS:
            return []
        lines: list[str] = []
        for child_id in self.node_repo.get_children(node.id):
            child = self.node_repo.get_node(child_id)
            if child is not None and child.outcome == Outcome.REJECT:
                lines.append(f"- `{child.id}` ({child.title}): `tm section get {child.id}:review`")
        return lines
```

- `src/taskmanager/engine/decisions.py`: `DECISION_STATUS_LABELS` gains the three new keys, so a label reads either vocabulary until Task 21:

```python
DECISION_STATUS_LABELS: dict[str, str] = {
    "NOT_STARTED": "Open",
    "COMPLETED": "Answered",
    "ABANDONED": "Withdrawn",
    DecisionStatus.OPEN.value: "Open",
    DecisionStatus.ANSWERED.value: "Answered",
    DecisionStatus.WITHDRAWN.value: "Withdrawn",
}
```

**3e.** `src/taskmanager/cli/main.py`. Add, below `_emit`:

```python
# A restore reads only exports carrying this marker; an export without it came from a
# pre-lifecycle tm, whose statuses and gated edges this version does not store.
EXPORT_FORMAT: dict[str, Any] = {"format": "tm-lifecycle", "version": 1}
```

`plan_add`'s body after `ops = container.get(Operations)` becomes:

```python
    try:
        plan_id = ops.add_plan(
            title, spec, slug, priority, order, review=require_review, fix=require_review
        )
    except OperationError as exc:
        print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=1) from exc
    print(f"[green]Added plan {plan_id}[/green]")
```

`task_depends` is replaced in full:

```python
@task_app.command("depends")
def task_depends(
    task_id: str,
    add: Annotated[
        str | None,
        typer.Option(
            "--add",
            help=(
                "Comma-separated ids this node now depends on; an edge is satisfied once the "
                "dependency's code lands where this node builds"
            ),
        ),
    ] = None,
    remove: Annotated[
        str | None, typer.Option("--remove", help="Comma-separated ids to stop depending on")
    ] = None,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    """Add or remove dependency edges; nothing is written if any is refused."""
    root = _get_root(path)
    container = _get_container(root)
    ops = container.get(Operations)

    def ids(raw: str | None) -> list[str]:
        return [x.strip() for x in (raw or "").split(",") if x.strip()]

    to_add, to_remove = ids(add), ids(remove)
    if not to_add and not to_remove:
        raise typer.BadParameter("give --add and/or --remove")
    gated = [x for x in to_add if ":" in x]
    if gated:
        raise typer.BadParameter(
            f"{', '.join(gated)}: a dependency is a bare id; an edge waits for the "
            "dependency's code to land, so it carries no status gate"
        )
    try:
        deps = ops.set_dependencies(task_id, to_add, to_remove)
    except OperationError as exc:
        print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=1) from exc
    print(f"[green]{task_id} depends on: {escape(', '.join(deps)) or '-'}[/green]")
```

`_export_node`, `export_cmd` and `restore_cmd` are replaced in full:

```python
def _export_node(node_repo: NodeRepository, node: Any) -> dict[str, Any]:
    return {
        "id": node.id,
        "kind": node.kind.value,
        "title": node.title,
        "status": node.status.value,
        "priority": node.priority,
        "target_repo": node.target_repo,
        "acceptable_models": node.acceptable_models,
        "frontmatter": node.frontmatter,
        "review": node.review,
        "fix": node.fix,
        "merge": node.merge.value,
        "requires": node.requires,
        "land_order": node.land_order,
        "branch": node.branch,
        "outcome": node.outcome.value if node.outcome else None,
        "verdict": node.verdict,
        "fix_for": node.fix_for.value if node.fix_for else None,
        "claimed_from": node.claimed_from.value if node.claimed_from else None,
        "review_cycles": node.review_cycles,
        "merge_attempts": node.merge_attempts,
        "step_failures": node.step_failures,
        "depends_on": sorted(node_repo.get_dependencies(node.id)),
        "conditions": [
            {"needs": c.needs, "command": c.command, "stage": c.stage.value}
            for c in node_repo.get_conditions(node.id)
        ],
        "sections": [
            {"key": s.section_key, "ordinal": s.ordinal, "header": s.header, "content": s.content}
            for s in node_repo.get_all_sections(node.id)
        ],
        "verifications": [
            {
                "type": v.verification_type.value,
                "target_path": v.target_path,
                "expected_pattern": v.expected_pattern,
            }
            for v in node_repo.get_verifications(node.id)
        ],
    }


@app.command("export")
def export_cmd(
    directory: Path,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    """Write the whole database as importable JSON: one file per plan and one per spec.

    No timestamps, so two exports of the same state are byte-identical: commit the directory and
    its history is a diff of what changed. `tm restore <dir>` rebuilds a database from it.
    """
    root = _get_root(path)
    container = _get_container(root)
    node_repo = container.get(NodeRepository)

    def dump(name: str, doc: dict[str, Any]) -> None:
        (directory / name).write_text(
            json.dumps(doc, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    directory.mkdir(parents=True, exist_ok=True)
    dump("_format.json", EXPORT_FORMAT)
    specs = {n.id: n for n in node_repo.list_nodes(kind=NodeKind.SPEC)}
    tasks = node_repo.list_nodes(kind=NodeKind.TASK)
    plans = node_repo.list_nodes(kind=NodeKind.PLAN)
    decisions = sorted(node_repo.list_nodes(kind=NodeKind.DECISION), key=lambda n: n.id)
    if decisions:
        dump("_decisions.json", {"decisions": [_export_node(node_repo, d) for d in decisions]})
    assets_src = root / ".taskmanager" / "assets"
    if assets_src.is_dir():
        assets_dst = directory / "assets"
        assets_dst.mkdir(parents=True, exist_ok=True)
        for f in sorted(assets_src.iterdir()):
            if f.is_file():
                (assets_dst / f.name).write_bytes(f.read_bytes())
    for plan in plans:
        children = set(node_repo.get_children(plan.id))
        plan_doc = _export_node(node_repo, plan)
        plan_doc["tasks"] = [_export_node(node_repo, t) for t in tasks if t.id in children]
        owner = next((sid for sid in sorted(specs) if plan.id in node_repo.get_children(sid)), None)
        dump(
            f"{plan.id}.json",
            {
                "spec": {"id": owner, "title": specs[owner].title} if owner else None,
                "plans": [plan_doc],
            },
        )
    for spec_id in sorted(specs):
        dump(f"_spec-{spec_id}.json", {"spec": _export_node(node_repo, specs[spec_id])})
    with _user_errors():
        settings = ConfigStore(root).document()
    if settings is not None:
        dump("_config.json", settings)
    print(f"[green]Exported {len(plans)} plans and {len(specs)} specs to {directory}[/green]")


@app.command("restore")
def restore_cmd(
    directory: Path,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    """Rebuild a database from a `tm export` directory (into a root that may be new)."""
    import copy

    root = _get_root(path, must_exist=False)
    marker = directory / "_format.json"
    if not marker.is_file() or json.loads(marker.read_text(encoding="utf-8")) != EXPORT_FORMAT:
        print(
            f"[red]{escape(str(directory))} is a pre-lifecycle export: tm v0.2.0 is the last "
            "release that restores it. Re-import the ongoing work into this version with "
            "`tm import`.[/red]"
        )
        raise typer.Exit(code=1)
    container = _get_container(root)
    container.get(DatabaseManager).init_all()
    importer = _RefusingImporter(container.get(BulkImporter))

    assets_dir = directory / "assets"
    if assets_dir.is_dir():
        assets_dest = root / ".taskmanager" / "assets"
        assets_dest.mkdir(parents=True, exist_ok=True)
        for f in sorted(assets_dir.iterdir()):
            if f.is_file():
                (assets_dest / f.name).write_bytes(f.read_bytes())

    files = [
        f
        for f in sorted(directory.glob("*.json"))
        if f.name not in ("_config.json", "_decisions.json", "_format.json")
    ]
    docs = [json.loads(f.read_text(encoding="utf-8")) for f in files]
    decisions_file = directory / "_decisions.json"
    decisions_doc = (
        json.loads(decisions_file.read_text(encoding="utf-8")) if decisions_file.exists() else None
    )
    if not docs and decisions_doc is None:
        print(f"[red]No export files in {directory}[/red]")
        raise typer.Exit(code=1)
    # Plans depend on each other, so the first pass keeps only the edges a document can satisfy
    # by itself and the second adds the rest; decisions go in between so a task's depends_on edge
    # onto one resolves in the second pass, then specs go last so their full data wins.
    plan_docs = [d for d in docs if d.get("plans")]
    spec_docs = [d for d in docs if not d.get("plans")]

    for doc in plan_docs:
        first = copy.deepcopy(doc)
        own = {n["id"] for p in first["plans"] for n in [p, *p.get("tasks", [])]}
        for p in first["plans"]:
            for n in [p, *p.get("tasks", [])]:
                n["depends_on"] = [d for d in n.get("depends_on", []) if d in own]
        importer.import_dict(first)
    if decisions_doc is not None:
        importer.import_dict(decisions_doc)
    for doc in [*plan_docs, *spec_docs]:
        importer.import_dict(doc)
    settings_file = directory / "_config.json"
    if settings_file.exists():
        with _user_errors():
            ConfigStore(root).replace(json.loads(settings_file.read_text(encoding="utf-8")))
    print(f"[green]Restored {len(plan_docs)} plans and {len(spec_docs)} specs into {root}[/green]")
```

Decisions in the CLI read their own vocabulary (imports: `DecisionStatus` from `taskmanager.core.status`, `stored_status` from `taskmanager.engine.snapshot`). In `decision_list`, the map and the filter become

```python
    status_map = {
        "open": DecisionStatus.OPEN,
        "answered": DecisionStatus.ANSWERED,
        "withdrawn": DecisionStatus.WITHDRAWN,
    }
    if status:
        wanted = status_map.get(status.lower())
        if wanted is None:
            raise typer.BadParameter("--status is one of: open, answered, withdrawn")
        decisions = [d for d in decisions if stored_status(d) == wanted]
```

and in `task_get`'s `awaiting_decisions`, `and dn.status not in (NodeStatus.COMPLETED, NodeStatus.ABANDONED)` becomes `and stored_status(dn) == DecisionStatus.OPEN`.

**3f.** `src/taskmanager/web/app.py`. `DependencyAdd` loses its `gate` field (`class DependencyAdd(BaseModel): id: str`). `create_plan` and `post_dependencies` become:

```python
    @app.post("/api/plans", status_code=201)
    def create_plan(body: PlanCreate, actor: Actor) -> dict[str, Any]:
        with _refusals():
            plan_id = operations.with_actor(actor).add_plan(
                body.title,
                body.spec,
                body.slug,
                body.priority,
                body.order,
                review=body.require_review,
                fix=body.require_review,
            )
        return {"id": plan_id}
```

```python
@app.post("/api/nodes/{node_id}/dependencies")
def post_dependencies(node_id: str, body: DependenciesUpdate, actor: Actor) -> list[dict[str, Any]]:
    with _refusals():
        deps = operations.with_actor(actor).set_dependencies(
            node_id, [d.id for d in body.add], body.remove
        )
    return [{"id": dep_id} for dep_id in deps]
```

The web's decision reads move too (imports: `DecisionStatus` from `taskmanager.core.status`, `stored_status` from `taskmanager.engine.snapshot`): in `_dependency_met`, `return bool(rel.status in (NodeStatus.COMPLETED, NodeStatus.ABANDONED))` becomes `return stored_status(rel) != DecisionStatus.OPEN`; `_DECISION_TAB_STATUS` maps `"open"`, `"answered"`, `"withdrawn"` to `DecisionStatus.OPEN`, `DecisionStatus.ANSWERED`, `DecisionStatus.WITHDRAWN`; and `list_decisions` filters with `stored_status(d) == wanted` and reports `"status": stored_status(d).value`.

**3g.** Old tests, rewritten or deleted in this task:

`tests/unit/test_operations.py`:
- Add `from taskmanager.core.status import Status` to the imports.
- `_seed_task`, `test_move_task_reparents`, `test_move_task_target_not_a_plan_refuses` and `test_move_task_second_write_failure_leaves_old_parent_intact`: every `x, _ = ops.add_plan(` becomes `x = ops.add_plan(` (lines 46, 217, 910, 911, 1085 at the current `origin/main`).
- Delete `test_add_plan_with_review_gate_injects_gate_and_records_it`; `test_add_plan_with_review_stores_the_flags_and_creates_no_gate_node` replaces it.
- Replace `test_set_dependencies_adds_and_removes_with_gate` with:

```python
def test_set_dependencies_adds_and_removes_bare_ids(ops_setup: tuple) -> None:
    node_repo, _runtime_repo, ledger_repo, ops = ops_setup
    _spec_id, plan_id, first = _seed_task(ops)
    second = ops.add_task("Second", plan_id, slug="T2")
    assert ops.set_dependencies(second, [first], []) == [first]
    assert _last_event_actor(ledger_repo) == "tester"
    assert ops.set_dependencies(second, [], [first]) == []
    assert node_repo.get_dependencies(second) == []
```

- `test_set_dependencies_cycle_refuses_and_writes_nothing`: `ops.set_dependencies(first, [(second, None)], [])` becomes `ops.set_dependencies(first, [second], [])`.
- `test_set_dependencies_second_add_failure_leaves_first_unadded`: `add=[(dep_a, None), (dep_b, None)]` becomes `add=[dep_a, dep_b]`.
- `test_supersede_missing_new_refuses_and_writes_nothing`, `test_supersede_second_write_failure_leaves_old_status_unchanged`, `test_release_lease_drops_lease_without_changing_status`: `== NodeStatus.NOT_STARTED` becomes `== Status.READY` (the node was created through `Operations`).

- Decisions now store their own statuses (import `DecisionStatus` alongside `Status`): in `test_add_decision_creates_node_with_options_and_context`, `test_reopen_decision_clears_answer_and_returns_to_open`, `test_set_status_refuses_a_decision_node` and `test_answer_decision_write_failure_leaves_decision_unanswered`, `== NodeStatus.NOT_STARTED` becomes `== DecisionStatus.OPEN`; in `test_answer_decision_with_option_completes_it`, `== NodeStatus.COMPLETED` becomes `== DecisionStatus.ANSWERED`; `test_withdraw_decision_sets_abandoned_with_reason` is renamed `test_withdraw_decision_sets_withdrawn_with_reason` and its `== NodeStatus.ABANDONED` becomes `== DecisionStatus.WITHDRAWN`.

`tests/unit/test_decision_effects.py` (Task 12's): in `test_an_effect_the_node_cannot_take_refuses_the_whole_answer` and `test_an_effect_waits_for_a_step_that_started_after_the_link`, `kit.node(decision_id).status == NodeStatus.NOT_STARTED` becomes `kit.node(decision_id).status == DecisionStatus.OPEN`, with the import to match.

`tests/unit/test_renderers.py`:
- `test_bulk_importer_json`: delete its last line, `assert repo.get_dependency_edges("AUTH-T2") == [("AUTH-T1", NodeStatus.COMPLETED)]`.
- Delete `test_bulk_importer_gated_dependency`; the `gated-edge` row of `test_a_refused_import_writes_nothing_and_says_why` replaces it.

`tests/integration/test_cli.py`:
- `test_cli_lifecycle_spec_plan_task_render_next` step 3: `assert "review gate" in res.stdout.lower()` becomes `assert "Added plan AUTH-REVPLAN" in res.stdout`.
- Replace `test_cli_task_depends_gated_edge` with:

```python
def test_cli_task_depends_takes_bare_ids_and_refuses_a_status_gate(tmp_path: Path) -> None:
    runner.invoke(app, ["init", "--path", str(tmp_path)])
    runner.invoke(app, ["spec", "add", "Gate Spec", "--slug", "GAT", "--path", str(tmp_path)])
    runner.invoke(
        app, ["plan", "add", "Gate Plan", "--spec", "GAT", "--slug", "P1", "--path", str(tmp_path)]
    )
    for slug in ("IMPL", "REV"):
        runner.invoke(
            app, ["task", "add", slug, "--plan", "GAT-P1", "--slug", slug, "--path", str(tmp_path)]
        )

    gated = runner.invoke(
        app,
        ["task", "depends", "GAT-P1-REV", "--add", "GAT-P1-IMPL:REVIEWED", "--path", str(tmp_path)],
    )
    assert gated.exit_code != 0
    assert "bare id" in gated.output

    res = runner.invoke(
        app, ["task", "depends", "GAT-P1-REV", "--add", "GAT-P1-IMPL", "--path", str(tmp_path)]
    )
    assert res.exit_code == 0, res.output
    assert "GAT-P1-REV depends on: GAT-P1-IMPL" in res.stdout
```

- `test_spec_and_plan_list_and_get_show_derived_state_not_stored_status`: `plan_stored.status == NodeStatus.NOT_STARTED` and `spec_stored.status == NodeStatus.NOT_STARTED` become `.status == Status.READY` (import `Status` from `taskmanager.core.status` in the function's local imports next to `NodeStatus`); both `assert "Status: NOT_STARTED" in res.stdout` become `assert "Status: READY" in res.stdout`.

`tests/unit/test_estate_workflow.py`:
- Delete `test_restore_handles_a_gated_dependency_without_crashing`; `test_restore_refuses_a_pre_lifecycle_export_and_writes_nothing` and the `gated-edge` import row cover what replaced it.
- `test_task_get_json_names_blockers_and_the_lease`: `[{"id": "S1-P1-a", "status": "NOT_STARTED"}]` becomes `[{"id": "S1-P1-a", "status": "READY"}]`.
- `test_plan_list_reports_the_state_its_tasks_add_up_to`: both `rows[0]["status"] == "NOT_STARTED"` become `rows[0]["status"] == "READY"`.
- `test_a_specs_state_rolls_up_from_its_plans_the_way_a_plans_does_from_its_tasks`: the three `row["status"] == "NOT_STARTED"` become `row["status"] == "READY"`, and `"Status: NOT_STARTED" in get_out` becomes `"Status: READY" in get_out`.
- Replace `test_supersede_refuses_a_missing_replacement_and_releases_the_old_lease` with:

```python
def test_supersede_refuses_a_missing_replacement_and_a_node_under_a_live_lease(
    tmp_path: Path,
) -> None:
    from taskmanager.core.models import Lease
    from taskmanager.core.status import Action, Status

    _seed_estate(tmp_path)
    root = str(tmp_path)
    bad = runner.invoke(app, ["task", "supersede", "S1-P1-a", "NOTHING", "-C", root])
    assert bad.exit_code == 1 and "nothing was changed" in bad.output

    db = DatabaseManager(tmp_path / ".taskmanager")
    node_repo, runtime_repo = NodeRepository(db), RuntimeRepository(db)
    node = node_repo.get_node("S1-P1-a")
    assert node is not None
    node.status, node.claimed_from = Status.IMPLEMENTING, Status.READY
    lease = Lease(
        task_id="S1-P1-a",
        agent_id="x",
        session_id="y",
        branch_name="tm/S1-P1-a",
        action=Action.IMPLEMENT,
        ttl_seconds=3600,
    )
    assert runtime_repo.claim(lease, [], node)
    held = runner.invoke(app, ["task", "supersede", "S1-P1-a", "S1-P1-b", "-C", root])
    assert held.exit_code == 1
    still = json.loads(runner.invoke(app, ["task", "get", "S1-P1-a", "--json", "-C", root]).stdout)
    assert still["status"] == "IMPLEMENTING" and still["lease"] is not None
```

`tests/integration/test_web_api.py`:
- Decision nodes read their own statuses (import `DecisionStatus` from `taskmanager.core.status`): in `test_answer_decision_unblocks_dependent_task` `NodeStatus.COMPLETED` becomes `DecisionStatus.ANSWERED`; in `test_answer_decision_unknown_option_refused_and_writes_nothing` `== NodeStatus.NOT_STARTED` becomes `stored_status(node) == DecisionStatus.OPEN` (the node was saved directly with the old default; import `stored_status` from `taskmanager.engine.snapshot`); in `test_reopen_and_withdraw_decision` `NodeStatus.NOT_STARTED` becomes `DecisionStatus.OPEN` and `NodeStatus.ABANDONED` becomes `DecisionStatus.WITHDRAWN`.
- Replace `test_post_dependencies_add_and_remove` with:

```python
def test_post_dependencies_add_and_remove(
    api: tuple[TestClient, NodeRepository, LedgerRepository],
) -> None:
    client, node_repo, _ledger_repo = api
    res = client.post("/api/nodes/SPEC-P1-T2/dependencies", json={"add": [{"id": "SPEC-P1-T1"}]})
    assert res.status_code == 200
    assert res.json() == [{"id": "SPEC-P1-T1"}]
    assert node_repo.get_dependencies("SPEC-P1-T2") == ["SPEC-P1-T1"]

    res2 = client.post("/api/nodes/SPEC-P1-T2/dependencies", json={"remove": ["SPEC-P1-T1"]})
    assert res2.status_code == 200
    assert node_repo.get_dependencies("SPEC-P1-T2") == []
```

`tests/unit/test_search_engine.py::test_search_prints_text_by_default_and_names_the_mode_on_the_last_line`: `"S1-P1-keys  task  NOT_STARTED plan S1-P1"` becomes `"S1-P1-keys  task  READY plan S1-P1"` (the task was added through the CLI).

Beyond these, one mechanical rule holds for the whole suite: a test that reads the stored `status` of a node created through `Operations`, the CLI's `add` commands or `tm import` and expects `NOT_STARTED` now expects `READY`; nothing else about such a test changes. Nodes saved directly with `NodeRepository.save_node(Node(...))` keep the model default until Task 21 and are untouched.

- [ ] **Step 4: Run the tests and the gates**

```
uv run --directory <worktree> pytest tests/unit/test_operations_lifecycle.py tests/unit/test_import_lifecycle.py tests/integration/test_export_restore.py -q; echo $?
uv run --directory <worktree> pytest -q; echo $?
uv run --directory <worktree> ruff check; echo $?
uv run --directory <worktree> ruff format; echo $?
uv run --directory <worktree> ruff format --check; echo $?
uv run --directory <worktree> mypy; echo $?
```

Expected: every command prints `0` (the `ruff format` run rewrites only files this task touched).

- [ ] **Step 5: Commit**

```
git -C <worktree> add src/taskmanager/core/enums.py src/taskmanager/engine/operations.py src/taskmanager/renderers/importers.py src/taskmanager/engine/graph.py src/taskmanager/cli/main.py src/taskmanager/web/app.py tests/unit/test_operations_lifecycle.py tests/unit/test_import_lifecycle.py tests/integration/test_export_restore.py tests/unit/test_operations.py tests/unit/test_renderers.py tests/integration/test_cli.py tests/unit/test_estate_workflow.py tests/integration/test_web_api.py tests/unit/test_search_engine.py tests/unit/test_decision_effects.py src/taskmanager/engine/decisions.py src/taskmanager/engine/wave.py src/taskmanager/renderers/markdown.py && git -C <worktree> commit -m "feat(operations): validate every write and store decisions and imports on the lifecycle model"
```

### Task 18: CLI verbs

**Spec:** §3.4, §4.3, §4.5, §5.2, §5.3, §5.6, §5.7, §6.2, §7.1, §7.2, §9.1 (`tm init --archive`), §10.1 (what `tm-wave` reads and calls)
**Files:**
- Modify: `src/taskmanager/engine/snapshot.py` (new `DisplayView`, `phase_of`)
- Modify: `src/taskmanager/di/container.py` (new `snapshots` provider)
- Modify: `src/taskmanager/guides/dispatch.md`, `fix.md`, `implement.md`, `merge.md`, `overview.md`, `review.md` (every line naming `tm run start|stop|release|heartbeat` deleted; `plan.md` names none)
- Modify: `commands/task.md` (the bullets for the removed verbs deleted)
- Modify: `src/taskmanager/cli/main.py` (import block; `_get_container` and the web commands refuse a pre-lifecycle estate; `_node_row`, new `_view`, `_claims`, `_refusing`, `_refuse_pre_lifecycle`, `_resolve_task_id`; `init`; `spec_add`, `spec_list`, `spec_get`, `plan_add`, `plan_list`, `plan_get`, `task_add`, `task_list`, `task_update`, `task_get` (with `next_action`), new `_next_action`; new `task_start` (with `--worktree-dir`), `task_complete`, `task_review`, `task_release` (each with `--agent`), `task_heartbeat`, `task_reopen`, `task_reset`, `task_defer`, `task_abandon`, `condition_add`, `condition_remove`, `job_status` (with `--wait`), `job_resume`, `land_start`; `run_list`, `run_sweep`, `wave_discover`, `decision_list`, `search_cmd`; delete `run_start`, `run_stop`, `run_release`, `run_heartbeat`)
- Create: `tests/integration/test_cli_lifecycle.py`
- Modify: `tests/integration/test_cli.py`, `tests/integration/test_decisions_cli.py`, `tests/integration/test_wave_cli.py`, `tests/unit/test_config.py`, `tests/unit/test_estate_workflow.py` (listed in Step 3d)

**Interfaces:**
- Consumes: `Status`, `DecisionStatus`, `Merge`, `Outcome`, `Action`, `ConditionStage`, `JobKind`, `JobState` (Task 1); `next_action` (Task 2); `phase`, `display_status` (Task 4); `landing_chain`, `satisfied` (Task 5); `Snapshot` (Task 6); `DatabaseManager.archive_pre_lifecycle`, `DatabaseManager.is_pre_lifecycle`, `Condition`, `Job` (Task 8); `JobRepository.get/for_node`, `CacheRepository.get_condition`, `RuntimeRepository.get_lease/list_leases/list_locks` (Task 9); `SnapshotBuilder.build/cycle/facts`, `stored_status`, `roll_up_ancestors` (Tasks 10, 12); `Claims` and its verbs returning the new `Status`, `Claims.start(..., worktree_dir=...)`, `Claims.complete/review/release(..., agent=...)`, `Claims.heartbeat -> bool`, `ClaimResult` with `worktrees`, `Blocker`, `DecisionSpec` (Task 13); `Landing.open`, `Landing.start_land/resume` (Task 14); `discover(claims, specs, session, slots, max_strong, exclude)` and `djb2` in `taskmanager.engine.discovery` (Task 16); `Operations.add_condition/remove_condition/update_node(..., review, fix, merge, requires, land_order)` (Task 17)
- Produces: `DisplayView(builder, cache=None, max_age=0)` with `.snapshot` and `.display(node) -> str`, and `phase_of(node) -> str | None`, in `taskmanager.engine.snapshot` (Task 19 reuses them); the CLI surface `tm-wave.js` (Task 22) drives: `tm task start … --worktree-dir <dir>`, `tm task get` printing `next_action`, `tm task complete|review|release … --agent <agent>`, `tm task heartbeat|reopen|reset|defer|abandon`, `tm task condition add|remove`, `tm job status <job> [--wait <seconds>]`, `tm job resume`, `tm land start`, `tm wave discover` with optional `--spec`, `tm init --archive`; guides and `commands/task.md` that name no removed verb

- [ ] **Step 1: Write the failing test**

`tests/integration/test_cli_lifecycle.py`:

```python
"""The lifecycle verbs of the CLI: claiming the next step, closing it, repair verbs,
conditions, jobs, discovery and the fresh-start archive."""

import json
import re
import sqlite3
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import Result
from typer.testing import CliRunner

from taskmanager.cli.main import app
from taskmanager.core.models import Job
from taskmanager.core.status import JobKind, JobState, Status
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.job_repo import JobRepository
from taskmanager.db.node_repo import NodeRepository
from taskmanager.engine.discovery import djb2

runner = CliRunner()


def tm(root: Path, *args: str) -> Result:
    return runner.invoke(app, [*args, "-C", str(root)])


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def estate(root: Path) -> None:
    """A spec, a plan and two tasks in the repository `core`, which has an `origin/main`;
    `S1-P1-b` depends on `S1-P1-a`."""
    origin = root / "core.git"
    git(root, "init", "--bare", "-b", "main", str(origin))
    git(root, "clone", str(origin), str(root / "core"))
    work = root / "core"
    git(work, "config", "user.email", "ci@example.com")
    git(work, "config", "user.name", "CI")
    git(work, "commit", "--allow-empty", "-m", "init")
    git(work, "push", "origin", "HEAD:main")
    git(work, "fetch", "origin")
    assert tm(root, "init").exit_code == 0
    tm(root, "spec", "add", "S", "--slug", "S1")
    tm(root, "plan", "add", "P", "--spec", "S1", "--slug", "P1")
    tm(root, "task", "add", "a", "--plan", "S1-P1", "--slug", "a")
    tm(root, "task", "add", "b", "--plan", "S1-P1", "--slug", "b", "--depends-on", "S1-P1-a")
    for task in ("S1-P1-a", "S1-P1-b"):
        assert tm(root, "task", "update", task, "--repo", "core").exit_code == 0


def get(root: Path, node_id: str) -> dict[str, Any]:
    res = tm(root, "task", "get", node_id, "--json")
    assert res.exit_code == 0, res.output
    doc: dict[str, Any] = json.loads(res.stdout)
    return doc


def start(root: Path, node_id: str, *extra: str) -> tuple[int, dict[str, Any]]:
    res = tm(root, "task", "start", node_id, "--agent", "agent-a", "--session", "sess-1", *extra)
    return res.exit_code, yaml.safe_load(res.stdout)


def test_start_claims_implement_then_complete_and_a_review_that_approves(tmp_path: Path) -> None:
    estate(tmp_path)
    code, step = start(tmp_path, "S1-P1-a")
    assert code == 0
    assert set(step) == {
        "action",
        "reason",
        "model",
        "job",
        "repo",
        "branch",
        "base",
        "worktree",
        "worktrees",
    }
    assert (step["action"], step["repo"], step["branch"], step["base"]) == (
        "implement",
        "core",
        "tm/S1-P1-a",
        "main",
    )
    assert step["worktree"] and Path(step["worktree"]).is_dir()
    assert tm(tmp_path, "task", "heartbeat", "S1-P1-a").exit_code == 0
    leases = json.loads(tm(tmp_path, "run", "list", "--json").stdout)["leases"]
    assert [(lease["task_id"], lease["action"]) for lease in leases] == [("S1-P1-a", "implement")]

    assert tm(tmp_path, "task", "complete", "S1-P1-a").exit_code == 0
    doc = get(tmp_path, "S1-P1-a")
    assert (doc["status"], doc["state"], doc["phase"], doc["lease"]) == (
        "IMPLEMENTED",
        "WAITING_REVIEW",
        "DISPATCHED",
        None,
    )

    code, step = start(tmp_path, "S1-P1-a")
    assert (code, step["action"]) == (0, "review")
    tm(tmp_path, "section", "set", "S1-P1-a:review", "no findings")
    assert (
        tm(tmp_path, "task", "review", "S1-P1-a", "--approve", "--verdict", "clean").exit_code == 0
    )
    doc = get(tmp_path, "S1-P1-a")
    assert (doc["status"], doc["outcome"], doc["verdict"], doc["state"]) == (
        "REVIEWED",
        "approve",
        "clean",
        "WAITING_MERGE",
    )


def test_a_blocked_start_exits_3_and_writes_nothing(tmp_path: Path) -> None:
    estate(tmp_path)
    code, step = start(tmp_path, "S1-P1-b")
    assert code == 3
    assert step["action"] == "blocked" and step["reason"]
    doc = get(tmp_path, "S1-P1-b")
    assert (doc["status"], doc["state"], doc["lease"], doc["blocked_by"]) == (
        "READY",
        "BLOCKED_BY_TASK",
        None,
        ["S1-P1-a"],
    )


def test_review_needs_exactly_one_of_approve_and_reject(tmp_path: Path) -> None:
    estate(tmp_path)
    for flags in ([], ["--approve", "--reject"]):
        res = tm(tmp_path, "task", "review", "S1-P1-a", *flags)
        assert res.exit_code == 2 and "--approve or --reject" in res.output


def test_release_blocked_writes_what_the_node_waits_on_and_returns_it(tmp_path: Path) -> None:
    estate(tmp_path)
    tm(tmp_path, "task", "add", "c", "--plan", "S1-P1", "--slug", "c")
    tm(tmp_path, "task", "update", "S1-P1-c", "--repo", "core")
    assert start(tmp_path, "S1-P1-c")[0] == 0
    bare = tm(tmp_path, "task", "release", "S1-P1-c", "--blocked")
    assert bare.exit_code == 2 and "--depends" in bare.output
    named = tm(tmp_path, "task", "release", "S1-P1-c", "--blocked", "--depends", "S1-P1-a")
    assert named.exit_code == 0, named.output
    doc = get(tmp_path, "S1-P1-c")
    assert (doc["status"], doc["lease"], doc["step_failures"]) == ("READY", None, 0)
    assert [d["id"] for d in doc["depends_on"]] == ["S1-P1-a"]


def test_a_transient_release_counts_a_step_failure(tmp_path: Path) -> None:
    estate(tmp_path)
    assert start(tmp_path, "S1-P1-a")[0] == 0
    assert tm(tmp_path, "task", "release", "S1-P1-a").exit_code == 0
    doc = get(tmp_path, "S1-P1-a")
    assert (doc["status"], doc["lease"], doc["step_failures"]) == ("READY", None, 1)


def test_an_expired_lease_is_swept_back_to_the_status_it_was_claimed_from(tmp_path: Path) -> None:
    estate(tmp_path)
    assert start(tmp_path, "S1-P1-a", "--ttl", "1")[0] == 0
    time.sleep(1.2)
    swept = tm(tmp_path, "run", "sweep")
    assert swept.exit_code == 0 and "S1-P1-a" in swept.output
    doc = get(tmp_path, "S1-P1-a")
    assert (doc["status"], doc["lease"], doc["step_failures"]) == ("READY", None, 1)


def test_defer_reopen_and_abandon_each_record_their_note(tmp_path: Path) -> None:
    estate(tmp_path)
    assert tm(tmp_path, "task", "defer", "S1-P1-a", "--note", "after the launch").exit_code == 0
    doc = get(tmp_path, "S1-P1-a")
    assert doc["status"] == "DEFERRED" and "deferral" in doc["sections"]
    assert tm(tmp_path, "task", "reopen", "S1-P1-a", "--note", "launch done").exit_code == 0
    doc = get(tmp_path, "S1-P1-a")
    assert doc["status"] == "READY" and "reopen" in doc["sections"]
    assert tm(tmp_path, "task", "abandon", "S1-P1-a", "--note", "dropped").exit_code == 0
    doc = get(tmp_path, "S1-P1-a")
    assert doc["status"] == "ABANDONED" and "abandonment" in doc["sections"]
    assert tm(tmp_path, "task", "defer", "S1-P1-b").exit_code == 2


def test_reset_moves_a_node_to_a_stable_status_and_refuses_a_step_status(tmp_path: Path) -> None:
    estate(tmp_path)
    ok = tm(tmp_path, "task", "reset", "S1-P1-a", "--to", "IMPLEMENTED", "--note", "repair")
    assert ok.exit_code == 0, ok.output
    doc = get(tmp_path, "S1-P1-a")
    assert (doc["status"], doc["state"]) == ("IMPLEMENTED", "WAITING_REVIEW")
    refused = tm(tmp_path, "task", "reset", "S1-P1-a", "--to", "REVIEWING", "--note", "x")
    assert refused.exit_code == 1
    assert get(tmp_path, "S1-P1-a")["status"] == "IMPLEMENTED"


def test_condition_add_and_remove_and_prose_is_refused(tmp_path: Path) -> None:
    estate(tmp_path)
    added = tm(
        tmp_path,
        "task",
        "condition",
        "add",
        "S1-P1-a",
        "--needs",
        "staging up",
        "--command",
        "true",
        "--stage",
        "landing",
    )
    assert added.exit_code == 0, added.output
    conditions = get(tmp_path, "S1-P1-a")["conditions"]
    assert [(c["needs"], c["command"], c["stage"]) for c in conditions] == [
        ("staging up", "true", "landing")
    ]
    idx = str(conditions[0]["idx"])
    assert tm(tmp_path, "task", "condition", "remove", "S1-P1-a", idx).exit_code == 0
    assert get(tmp_path, "S1-P1-a")["conditions"] == []
    prose = tm(
        tmp_path,
        "task",
        "condition",
        "add",
        "S1-P1-a",
        "--needs",
        "sign-off",
        "--command",
        "the design is signed off",
    )
    assert prose.exit_code == 1 and "decision" in prose.output


def test_task_update_sets_flags_merge_requires_and_land_order(tmp_path: Path) -> None:
    estate(tmp_path)
    plan = tm(tmp_path, "task", "update", "S1-P1", "--review", "--fix", "--land-order", "core,web")
    assert plan.exit_code == 0, plan.output
    task = tm(
        tmp_path,
        "task",
        "update",
        "S1-P1-a",
        "--merge",
        "parent",
        "--no-fix",
        "--requires",
        "figma,browser",
    )
    assert task.exit_code == 0, task.output
    a = get(tmp_path, "S1-P1-a")
    assert (a["review"], a["fix"], a["merge"], a["requires"]) == (
        True,
        False,
        "parent",
        ["figma", "browser"],
    )
    assert get(tmp_path, "S1-P1")["land_order"] == ["core", "web"]
    assert tm(tmp_path, "task", "update", "S1-P1-b", "--no-review").exit_code != 0
    assert get(tmp_path, "S1-P1-b")["review"] is True


@pytest.mark.parametrize("verb", ["start", "stop", "release", "heartbeat"])
def test_the_old_run_verbs_are_gone(tmp_path: Path, verb: str) -> None:
    assert tm(tmp_path, "init").exit_code == 0
    res = tm(tmp_path, "run", verb, "X")
    assert res.exit_code == 2 and "No such command" in res.output


def test_wave_discover_takes_an_optional_spec_and_has_no_release_flag(tmp_path: Path) -> None:
    estate(tmp_path)
    base = ["wave", "discover", "--session", "s", "--slots", "4", "--max-strong", "1"]
    for extra in ([], ["--spec", "S1"]):
        res = tm(tmp_path, *base, *extra)
        assert res.exit_code == 0, res.output
        payload, check = res.stdout.rstrip("\n").split("\n")
        data = json.loads(payload)
        assert "S1-P1-a" in [c["id"] for c in data["chosen"]]
        assert check == f"__CHECK n={len(data['chosen'])} h={djb2(payload)}"
    assert tm(tmp_path, *base, "--release", "S1-P1-a").exit_code == 2


def test_job_status_of_an_unknown_job_is_refused(tmp_path: Path) -> None:
    assert tm(tmp_path, "init").exit_code == 0
    res = tm(tmp_path, "job", "status", "nope")
    assert res.exit_code == 1 and "nope" in res.output


def test_land_start_refuses_an_unknown_node(tmp_path: Path) -> None:
    estate(tmp_path)
    res = tm(tmp_path, "land", "start", "NOPE")
    assert res.exit_code == 1 and "NOPE" in res.output


def test_init_archive_moves_a_pre_lifecycle_estate_aside_and_starts_fresh(tmp_path: Path) -> None:
    old = tmp_path / ".taskmanager"
    old.mkdir()
    conn = sqlite3.connect(old / "spec.db")
    conn.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY)")
    conn.commit()
    conn.close()
    refused = tm(tmp_path, "spec", "list")
    assert refused.exit_code == 1 and "tm init --archive" in refused.output
    assert "Traceback" not in refused.output
    res = tm(tmp_path, "init", "--archive")
    assert res.exit_code == 0, res.output
    archives = sorted(old.glob("archive-*"))
    assert len(archives) == 1 and (archives[0] / "spec.db").is_file()
    assert (old / "state.db").is_file()
    assert tm(tmp_path, "spec", "list").exit_code == 0


def test_lists_show_the_stored_status_the_display_and_the_phase(tmp_path: Path) -> None:
    estate(tmp_path)
    rows = json.loads(tm(tmp_path, "task", "list", "--json").stdout)
    assert {r["id"]: (r["status"], r["state"], r["phase"]) for r in rows} == {
        "S1-P1-a": ("READY", "READY", "QUEUED"),
        "S1-P1-b": ("READY", "BLOCKED_BY_TASK", "QUEUED"),
    }


def test_start_cuts_its_worktree_under_the_directory_it_is_given(tmp_path: Path) -> None:
    estate(tmp_path)
    wt_dir = (tmp_path / "scratch" / "worktrees").resolve()
    code, step = start(tmp_path, "S1-P1-a", "--worktree-dir", str(wt_dir))
    assert (code, step["action"]) == (0, "implement")
    worktree = Path(step["worktree"]).resolve()
    assert worktree.is_dir() and worktree.is_relative_to(wt_dir)
    assert isinstance(step["worktrees"], dict)
    assert all(Path(p).resolve().is_relative_to(wt_dir) for p in step["worktrees"].values())


def test_task_get_names_the_next_action_the_lifecycle_gives(tmp_path: Path) -> None:
    estate(tmp_path)
    assert [get(tmp_path, n)["next_action"] for n in ("S1-P1-a", "S1-P1-b", "S1-P1")] == [
        "implement",
        "implement",
        None,
    ]
    assert start(tmp_path, "S1-P1-a")[0] == 0
    assert get(tmp_path, "S1-P1-a")["next_action"] is None
    assert tm(tmp_path, "task", "complete", "S1-P1-a").exit_code == 0
    assert get(tmp_path, "S1-P1-a")["next_action"] == "review"

    db = DatabaseManager(tmp_path / ".taskmanager")
    node_repo, jobs = NodeRepository(db), JobRepository(db)
    node = node_repo.get_node("S1-P1-b")
    assert node is not None
    node.status, node.claimed_from = Status.MERGING, Status.IMPLEMENTED
    node_repo.save_node(node)
    job = jobs.create(Job(kind=JobKind.LAND, node_id="S1-P1-b", repo="core", target="main"))
    assert get(tmp_path, "S1-P1-b")["next_action"] is None
    jobs.update(job.model_copy(update={"state": JobState.NEEDS_AGENT}))
    assert get(tmp_path, "S1-P1-b")["next_action"] == "merge"


def test_closing_or_releasing_a_step_in_another_agents_name_is_refused(tmp_path: Path) -> None:
    estate(tmp_path)
    assert start(tmp_path, "S1-P1-a")[0] == 0
    for verb in ("complete", "release"):
        res = tm(tmp_path, "task", verb, "S1-P1-a", "--agent", "agent-b")
        assert res.exit_code == 1, res.output
    doc = get(tmp_path, "S1-P1-a")
    assert (doc["status"], doc["lease"]["agent_id"], doc["step_failures"]) == (
        "IMPLEMENTING",
        "agent-a",
        0,
    )
    assert tm(tmp_path, "task", "complete", "S1-P1-a", "--agent", "agent-a").exit_code == 0

    assert start(tmp_path, "S1-P1-a")[1]["action"] == "review"
    tm(tmp_path, "section", "set", "S1-P1-a:review", "no findings")
    wrong = tm(tmp_path, "task", "review", "S1-P1-a", "--approve", "--agent", "agent-b")
    assert wrong.exit_code == 1, wrong.output
    assert get(tmp_path, "S1-P1-a")["status"] == "REVIEWING"
    assert tm(tmp_path, "task", "release", "S1-P1-a", "--agent", "agent-a").exit_code == 0
    doc = get(tmp_path, "S1-P1-a")
    assert (doc["status"], doc["lease"]) == ("IMPLEMENTED", None)


def test_job_status_waits_while_the_job_runs_and_returns_once_it_leaves_running(
    tmp_path: Path,
) -> None:
    estate(tmp_path)
    jobs = JobRepository(DatabaseManager(tmp_path / ".taskmanager"))
    job = jobs.create(Job(kind=JobKind.LAND, node_id="S1-P1-a", repo="core", target="main"))

    began = time.monotonic()
    res = tm(tmp_path, "job", "status", job.id, "--wait", "1")
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout)["state"] == "running"
    assert time.monotonic() - began >= 1

    def finish() -> None:
        time.sleep(0.5)
        own = JobRepository(DatabaseManager(tmp_path / ".taskmanager"))
        own.update(job.model_copy(update={"state": JobState.SUCCEEDED}))

    worker = threading.Thread(target=finish)
    worker.start()
    began = time.monotonic()
    res = tm(tmp_path, "job", "status", job.id, "--wait", "60")
    worker.join()
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout)["state"] == "succeeded"
    assert time.monotonic() - began < 10


REMOVED_RUN_VERBS = re.compile(r"\btm run (start|stop|release|heartbeat)\b")


def test_no_guide_or_command_page_shows_a_removed_run_verb() -> None:
    repo = Path(__file__).resolve().parents[2]
    guides = sorted((repo / "src" / "taskmanager" / "guides").glob("*.md"))
    pages = [*guides, repo / "commands" / "task.md"]
    assert len(pages) == 8, pages
    shown = [
        f"{page.name}: {line}"
        for page in pages
        for line in page.read_text(encoding="utf-8").splitlines()
        if REMOVED_RUN_VERBS.search(line)
    ]
    assert shown == []
```

- [ ] **Step 2: Run it and watch it fail**

```
uv run --directory <worktree> pytest tests/integration/test_cli_lifecycle.py -q; echo $?
```

Expected: exit 1. `No such command 'start'` under `task`, `No such option: --archive`, `No such command 'condition'`, `No such command 'job'`, `No such command 'land'`, `wave discover` failing on a missing `--spec`, `run start` still existing (exit 0 or 1, not 2), the list rows lacking `phase`, and `test_no_guide_or_command_page_shows_a_removed_run_verb` listing the guides' `tm run start` and `tm run stop` lines. The `--worktree-dir`, `--agent`, `next_action` and `--wait` tests fail on the same missing `start` command and `job` group.

- [ ] **Step 3: Implement**

**3a.** `src/taskmanager/engine/snapshot.py`, appended below `roll_up_ancestors` (imports it needs, merged into the module's own: `replace` from `dataclasses`, `NodeKind` from `taskmanager.core.enums`, `ConditionStage` from `taskmanager.core.status`, `display_status` and `phase` from `taskmanager.core.display`, `next_action` from `taskmanager.core.lifecycle`, `CacheRepository` from `taskmanager.db.cache_repo`):

```python
class DisplayView:
    """One snapshot, and the cached condition results, that every display in one read shares.

    Conditions are read from the cache only: a reader must never wait on a condition's command.
    """

    def __init__(
        self, builder: SnapshotBuilder, cache: CacheRepository | None = None, max_age: int = 0
    ) -> None:
        self.builder = builder
        self.snapshot = builder.build()
        self.cache = cache
        self.max_age = max_age

    def _unmet(self, node: Node) -> bool:
        if self.cache is None:
            return False
        stages = {ConditionStage.CLAIM}
        if next_action(self.builder.cycle(node)) == Action.MERGE:
            stages.add(ConditionStage.LANDING)
        for condition in self.builder.node_repo.get_conditions(node.id):
            if condition.stage not in stages:
                continue
            code = self.cache.get_condition(node.id, condition.idx, condition.command, self.max_age)
            if code is not None and code != 0:
                return True
        return False

    def display(self, node: Node) -> str:
        status = stored_status(node)
        if not isinstance(status, Status):
            return status.value
        facts = replace(
            self.builder.facts(node.id, self.snapshot), unmet_condition=self._unmet(node)
        )
        return display_status(self.builder.cycle(node), facts).value


def phase_of(node: Node) -> str | None:
    status = stored_status(node)
    return phase(status).value if isinstance(status, Status) else None
```

The CLI and the web resolve the builder from the container. In `src/taskmanager/di/container.py`, add `from taskmanager.engine.snapshot import SnapshotBuilder`, a provider after `cache_repo`, and its alias beside the others (`get_snapshots = snapshots`):

```python
    @provide(scope=Scope.APP)
    def snapshots(
        self, node_repo: NodeRepository, runtime_repo: RuntimeRepository, job_repo: JobRepository
    ) -> SnapshotBuilder:
        return SnapshotBuilder(node_repo, runtime_repo, job_repo)
```

**3b.** The guides and `commands/task.md` show `tm run start`, `stop`, `release` and `heartbeat`, which this task deletes, and `test_guide_command_exists` resolves every `tm` command a guide shows against the CLI. Every guide line naming a removed verb goes now; Tasks 23 and 24 rewrite these files whole, so a guide paragraph left without its opening line stays until then. Run:

```
uv run --directory <worktree> python - <<'EOF'
import re
from pathlib import Path

removed = re.compile(r"\btm run (start|stop|release|heartbeat)\b")
guides = sorted(Path("src/taskmanager/guides").glob("*.md"))
assert len(guides) == 7, guides
dropped = 0
for guide in guides:
    lines = guide.read_text(encoding="utf-8").splitlines(keepends=True)
    kept = [line for line in lines if not removed.search(line)]
    dropped += len(lines) - len(kept)
    guide.write_text("".join(kept), encoding="utf-8")
assert dropped > 0, "no guide line named a removed verb"
print(f"dropped {dropped} lines")
EOF
```

Expected: `dropped <n> lines` with `n` above 0. Every guide still shows at least one `tm` command, and the remaining total (about 100) stays above the floor of 40 that `test_the_guides_show_enough_commands_to_be_worth_checking` holds.

`commands/task.md` is replaced in full, the bullets for the removed verbs gone with their lead-in lines:

```markdown
---
description: Claim, inspect, heartbeat, or complete the active task in the current worktree.
argument-hint: "[verify]"
---

Run `tm guide implement` (or `review`, `fix`, `merge` for your role) before the first command of a
task: it names what each command refuses and the handoff you owe. `tm` finds the project and, inside
a task worktree, the task, so the id is optional there.

- `verify`: run the task's declared verifications, exit 1 on a failure:
  `tm verify run`
- no arguments: show what is claimed and which files are locked:
  `tm run list --yaml`
```

**3c.** `src/taskmanager/cli/main.py`.

The import block becomes:

```python
import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any

import typer
from dishka import Container, make_container
from rich import print
from rich.markup import escape
from rich.table import Table

from taskmanager.core.enums import (
    ImportFormat,
    LedgerCommand,
    NodeKind,
    RecommendationStrategy,
    RenderView,
    SearchMode,
    TransferMode,
    VerificationType,
)
from taskmanager.core.lifecycle import next_action
from taskmanager.core.models import Condition, LedgerEvent, Node
from taskmanager.core.naming import QualifiedPath
from taskmanager.core.status import (
    Action,
    ConditionStage,
    DecisionStatus,
    JobKind,
    JobState,
    Merge,
    Outcome,
    Status,
)
from taskmanager.db.cache_repo import CacheRepository
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.job_repo import JobRepository
from taskmanager.db.ledger_repo import LedgerRepository
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.di.container import TaskManagerProvider
from taskmanager.engine.chains import landing_chain, satisfied
from taskmanager.engine.claims import Blocker, Claims, DecisionSpec
from taskmanager.engine.config import ConfigError, ConfigStore
from taskmanager.engine.decisions import DECISION_STATUS_LABELS, read_decision
from taskmanager.engine.discovery import discover, djb2
from taskmanager.engine.heuristics import RecommendationEngine
from taskmanager.engine.landing import Landing
from taskmanager.engine.operations import GUIDE_NODE, OperationError, Operations
from taskmanager.engine.search import SearchEngine, SearchError
from taskmanager.engine.snapshot import DisplayView, SnapshotBuilder, phase_of, stored_status
from taskmanager.renderers.importers import BulkImporter
from taskmanager.renderers.markdown import MarkdownRenderer
```

The sub-apps gain three groups; `condition_app` hangs under `task`:

```python
job_app = typer.Typer(name="job", help="Landing and sync jobs")
land_app = typer.Typer(name="land", help="Start a node's landing")
condition_app = typer.Typer(name="condition", help="States outside the corpus a node waits on")

app.add_typer(job_app)
app.add_typer(land_app)
task_app.add_typer(condition_app)
```

`_node_row` is replaced and three helpers follow it:

```python
def _node_row(node: Node, state: str | None = None) -> dict[str, Any]:
    return {
        "id": node.id,
        "kind": node.kind.value,
        "title": node.title,
        "status": node.status.value,
        "state": state or node.status.value,
        "phase": phase_of(node),
        "priority": node.priority,
        "target_repo": node.target_repo,
        "acceptable_models": node.acceptable_models,
        "review": node.review,
        "fix": node.fix,
        "merge": node.merge.value,
    }


def _view(container: Container) -> DisplayView:
    root = container.get(TaskManagerProvider).root
    return DisplayView(
        container.get(SnapshotBuilder),
        container.get(CacheRepository),
        ConfigStore(root).project().condition_ttl,
    )


def _list_rows(container: Container, kind: NodeKind, status: Status | None) -> list[dict[str, Any]]:
    view = _view(container)
    nodes = [
        n
        for n in container.get(NodeRepository).list_nodes(kind=kind)
        if status is None or stored_status(n) == status
    ]
    return [_node_row(n, view.display(n)) for n in nodes]


def _next_action(container: Container, node: Node) -> str | None:
    """The step a claim of this node would take, before claimability is asked.

    A landing stopped for an agent is handed over by a merge claim, so a `MERGING` node whose
    landing job waits for one reads `merge`; every other step in progress reads null.
    """
    status = stored_status(node)
    if not isinstance(status, Status):
        return None
    if status == Status.MERGING:
        jobs = container.get(JobRepository).for_node(node.id)
        waiting = any(j.kind == JobKind.LAND and j.state == JobState.NEEDS_AGENT for j in jobs)
        return Action.MERGE.value if waiting else None
    action = next_action(container.get(SnapshotBuilder).cycle(node))
    return action.value if action is not None else None


@contextmanager
def _refusing() -> Iterator[None]:
    """A refusal is its message and exit 1, never a traceback."""
    try:
        yield
    except OperationError as exc:
        print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=1) from exc


_PRE_LIFECYCLE = (
    "this directory holds a pre-lifecycle estate: run `tm init --archive` to move it to "
    "`.taskmanager/archive-<timestamp>/` and start fresh, then re-import the ongoing work"
)


def _refuse_pre_lifecycle(root: Path) -> None:
    # Checked before any connection opens: opening one on an old estate raises mid-command.
    if DatabaseManager(root / ".taskmanager").is_pre_lifecycle():
        print(f"[red]{escape(_PRE_LIFECYCLE)}[/red]")
        raise typer.Exit(code=1)


def _claims(root: Path) -> Claims:
    """Claims wired to the landing engine, which a merge claim starts its job through."""
    _refuse_pre_lifecycle(root)
    return Landing.open(root).claims


def _landing(root: Path) -> Landing:
    _refuse_pre_lifecycle(root)
    return Landing.open(root)
```

`_get_container` refuses an old estate first:

```python
def _get_container(path: Path | None) -> Container:
    root = _get_root(path, must_exist=False)
    _refuse_pre_lifecycle(root)
    return make_container(TaskManagerProvider(root))
```

and `_run_web_server` and `web_export` each call `_refuse_pre_lifecycle(root)` on the line after they resolve `root`, ahead of their `is_initialized()` check.

`_resolve_task_id`'s lease lookup reads through the repository:

```python
def _resolve_task_id(runtime_repo: RuntimeRepository, task_id: str | None) -> str:
    if task_id:
        return task_id

    cwd = Path.cwd().resolve()
    for lease in runtime_repo.list_leases():
        if lease.worktree_path:
            wt = Path(lease.worktree_path).resolve()
            if cwd == wt or wt in cwd.parents:
                return lease.task_id

    try:
        res = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True,
            text=True,
            check=False,
        )
        branch = res.stdout.strip()
        if branch.startswith("tm/"):
            return branch[len("tm/") :]
    except (subprocess.SubprocessError, OSError) as exc:
        logging.getLogger(__name__).debug("Failed to resolve task from branch: %s", exc)

    raise typer.BadParameter(
        "task_id is required or current working directory must be within a task worktree"
    )
```

`init`:

```python
@app.command("init")
def init(
    archive: Annotated[
        bool,
        typer.Option(
            "--archive",
            help="Move a pre-lifecycle estate to .taskmanager/archive-<timestamp>/ first",
        ),
    ] = False,
    path: Annotated[
        Path | None, typer.Option("--path", "-C", help="Target project root directory")
    ] = None,
) -> None:
    root = _get_root(path, must_exist=False)
    if archive:
        try:
            moved = DatabaseManager.archive_pre_lifecycle(root)
        except ValueError as exc:
            print(f"[red]{escape(str(exc))}[/red]")
            raise typer.Exit(code=1) from exc
        print(f"[yellow]Moved the pre-lifecycle estate to {moved}[/yellow]")
    container = _get_container(root)
    db = container.get(DatabaseManager)
    db.init_all()
    _record_ledger(container, command=LedgerCommand.INIT, target_id=str(root))
    print(f"[green]Initialized .taskmanager in {root}[/green]")
```

Specs:

```python
@spec_app.command("add")
def spec_add(
    title: str,
    slug: Annotated[str | None, typer.Option("--slug", "-s", help="Specification slug/id")] = None,
    priority: Annotated[int, typer.Option("--priority", "-p", help="Priority (1-100)")] = 50,
    order: Annotated[int, typer.Option("--order", "-o", help="Display order")] = 0,
    review: Annotated[
        bool, typer.Option("--review/--no-review", help="A review step follows the children")
    ] = False,
    fix: Annotated[
        bool, typer.Option("--fix/--no-fix", help="A rejection is fixed on this node")
    ] = False,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    root = _get_root(path)
    ops = _get_container(root).get(Operations)
    with _refusing():
        spec_id = ops.add_spec(title, slug, priority, order, review=review, fix=fix)
    print(f"[green]Added spec {spec_id}[/green]")


def _print_rows(title: str, rows: list[dict[str, Any]]) -> None:
    table = Table(title=title)
    table.add_column("ID", style="cyan")
    table.add_column("Title")
    table.add_column("State", style="yellow")
    table.add_column("Priority", justify="right")
    for r in rows:
        table.add_row(escape(r["id"]), escape(r["title"]), r["state"], str(r["priority"]))
    print(table)


@spec_app.command("list")
def spec_list(
    status: Annotated[
        Status | None, typer.Option("--status", help="Filter by stored status")
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
    yaml_output: Annotated[
        bool, typer.Option("--yaml", help="Output as YAML (fewer tokens than JSON)")
    ] = False,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    rows = _list_rows(_get_container(_get_root(path)), NodeKind.SPEC, status)
    if json_output or yaml_output:
        _emit(rows, yaml_output)
        return
    _print_rows("Specifications", rows)


def _print_container(label: str, container: Container, node_id: str, children_label: str) -> None:
    node_repo = container.get(NodeRepository)
    node = node_repo.get_node(node_id)
    if node is None:
        print(f"[red]{label} '{node_id}' not found[/red]")
        raise typer.Exit(code=1)
    view = _view(container)
    children = node_repo.get_children(node_id)
    print(f"[bold cyan]{label}:[/] {node.id}")
    print(f"[bold]Title:[/] {escape(node.title)}")
    print(f"[bold]Status:[/] {node.status.value}")
    print(f"[bold]State:[/] {view.display(node)}")
    print(f"[bold]Priority:[/] {node.priority}")
    if children:
        print(f"[bold]{children_label}:[/] {escape(', '.join(children))}")


@spec_app.command("get")
def spec_get(
    spec_id: str,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    _print_container("Spec", _get_container(_get_root(path)), spec_id, "Plans")
```

Plans:

```python
@plan_app.command("add")
def plan_add(
    title: str,
    spec: Annotated[str, typer.Option("--spec", help="Parent spec ID")],
    slug: Annotated[str | None, typer.Option("--slug", "-s", help="Plan slug")] = None,
    priority: Annotated[int, typer.Option("--priority", "-p", help="Priority")] = 50,
    order: Annotated[int, typer.Option("--order", "-o", help="Display order")] = 0,
    review: Annotated[
        bool, typer.Option("--review/--no-review", help="A review step follows the children")
    ] = False,
    fix: Annotated[
        bool, typer.Option("--fix/--no-fix", help="A rejection is fixed on this plan")
    ] = False,
    merge: Annotated[
        Merge, typer.Option("--merge", help="Land on the parent's branch or on main")
    ] = Merge.MAIN,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    root = _get_root(path)
    ops = _get_container(root).get(Operations)
    with _refusing():
        plan_id = ops.add_plan(
            title, spec, slug, priority, order, review=review, fix=fix, merge=merge
        )
    print(f"[green]Added plan {plan_id}[/green]")


@plan_app.command("list")
def plan_list(
    spec: Annotated[str | None, typer.Option("--spec", help="Filter by spec ID")] = None,
    status: Annotated[
        Status | None, typer.Option("--status", help="Filter by stored status")
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
    yaml_output: Annotated[
        bool, typer.Option("--yaml", help="Output as YAML (fewer tokens than JSON)")
    ] = False,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    container = _get_container(_get_root(path))
    rows = _list_rows(container, NodeKind.PLAN, status)
    if spec:
        children = set(container.get(NodeRepository).get_children(spec))
        rows = [r for r in rows if r["id"] in children]
    if json_output or yaml_output:
        _emit(rows, yaml_output)
        return
    _print_rows("Plans", rows)


@plan_app.command("get")
def plan_get(
    plan_id: str,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    _print_container("Plan", _get_container(_get_root(path)), plan_id, "Tasks")
```

Tasks (`task_add`, `task_list`, `task_update`, `task_get` replaced; `task_supersede`, `task_depends`, `task_move` unchanged):

```python
def _csv(raw: str | None) -> list[str]:
    return [x.strip() for x in (raw or "").split(",") if x.strip()]


@task_app.command("add")
def task_add(
    title: str,
    plan: Annotated[str, typer.Option("--plan", help="Parent plan ID")],
    slug: Annotated[str | None, typer.Option("--slug", "-s", help="Task slug")] = None,
    priority: Annotated[int, typer.Option("--priority", "-p", help="Priority")] = 50,
    order: Annotated[int, typer.Option("--order", "-o", help="Display order")] = 0,
    depends_on: Annotated[
        str | None, typer.Option("--depends-on", help="Comma-separated dependency IDs")
    ] = None,
    models: Annotated[
        str | None, typer.Option("--models", help="Comma-separated acceptable models")
    ] = None,
    review: Annotated[
        bool, typer.Option("--review/--no-review", help="A review step follows implement")
    ] = True,
    fix: Annotated[
        bool, typer.Option("--fix/--no-fix", help="A rejection is fixed by this task")
    ] = True,
    merge: Annotated[
        Merge, typer.Option("--merge", help="Land on the parent's branch or on main")
    ] = Merge.MAIN,
    requires: Annotated[
        str | None, typer.Option("--requires", help="Comma-separated agent capabilities")
    ] = None,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    root = _get_root(path)
    ops = _get_container(root).get(Operations)
    with _refusing():
        task_id = ops.add_task(
            title,
            plan,
            slug,
            priority,
            order,
            _csv(depends_on),
            _csv(models),
            review=review,
            fix=fix,
            merge=merge,
            requires=_csv(requires),
        )
    print(f"[green]Added task {task_id}[/green]")


@task_app.command("list")
def task_list(
    plan: Annotated[str | None, typer.Option("--plan", help="Filter by plan ID")] = None,
    spec: Annotated[
        str | None,
        typer.Option("--spec", help='Filter by spec ID ("none" for tasks whose plan has no spec)'),
    ] = None,
    status: Annotated[
        Status | None, typer.Option("--status", help="Filter by stored status")
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
    yaml_output: Annotated[
        bool, typer.Option("--yaml", help="Output as YAML (fewer tokens than JSON)")
    ] = False,
    render_view: Annotated[
        RenderView | None,
        typer.Option("--render", help="Render every listed task in this view instead of a table"),
    ] = None,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    container = _get_container(_get_root(path))
    node_repo = container.get(NodeRepository)
    rows = _list_rows(container, NodeKind.TASK, status)
    if plan:
        children = set(node_repo.get_children(plan))
        rows = [r for r in rows if r["id"] in children]
    if spec:
        wanted = None if spec == "none" else spec
        rows = [r for r in rows if _task_spec_id(node_repo, r["id"]) == wanted]
    if render_view is not None:
        renderer = container.get(MarkdownRenderer)
        sys.stdout.write(
            "\n\n---\n\n".join(renderer.render(r["id"], view=render_view) for r in rows) + "\n"
        )
        return
    if json_output or yaml_output:
        _emit(rows, yaml_output)
        return

    table = Table(title="Tasks")
    table.add_column("ID", style="cyan")
    table.add_column("Title")
    table.add_column("State", style="yellow")
    table.add_column("Priority", justify="right")
    table.add_column("Models")
    for r in rows:
        table.add_row(
            escape(r["id"]),
            escape(r["title"]),
            r["state"],
            str(r["priority"]),
            escape(", ".join(r["acceptable_models"])),
        )
    print(table)


@task_app.command("update")
def task_update(
    task_id: str,
    title: Annotated[str | None, typer.Option("--title")] = None,
    priority: Annotated[int | None, typer.Option("--priority", "-p")] = None,
    models: Annotated[
        str | None, typer.Option("--models", help="Comma-separated acceptable models")
    ] = None,
    repo: Annotated[str | None, typer.Option("--repo", help="Target repository directory")] = None,
    set_frontmatter: Annotated[
        list[str] | None,
        typer.Option(
            "--set",
            help="Frontmatter key=value, repeatable; the value is JSON when it parses "
            '(declared_files=\'["a","b"]\'), else text',
        ),
    ] = None,
    unset_frontmatter: Annotated[
        list[str] | None,
        typer.Option("--unset", help="Frontmatter key to remove, repeatable"),
    ] = None,
    review: Annotated[
        bool | None, typer.Option("--review/--no-review", help="A review step follows implement")
    ] = None,
    fix: Annotated[
        bool | None, typer.Option("--fix/--no-fix", help="A rejection is fixed by this node")
    ] = None,
    merge: Annotated[
        Merge | None, typer.Option("--merge", help="Land on the parent's branch or on main")
    ] = None,
    requires: Annotated[
        str | None,
        typer.Option("--requires", help="Comma-separated agent capabilities; '' clears them"),
    ] = None,
    land_order: Annotated[
        str | None,
        typer.Option("--land-order", help="Comma-separated repositories, for a plan or a spec"),
    ] = None,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    root = _get_root(path)
    ops = _get_container(root).get(Operations)
    frontmatter_set: dict[str, Any] = {}
    for pair in set_frontmatter or []:
        key, sep, raw = pair.partition("=")
        if not sep or not key:
            raise typer.BadParameter(f"--set takes key=value, got '{pair}'")
        try:
            frontmatter_set[key] = json.loads(raw)
        except ValueError:
            frontmatter_set[key] = raw
    try:
        changed = ops.update_node(
            task_id,
            title=title,
            priority=priority,
            models=_csv(models) if models is not None else None,
            repo=repo,
            frontmatter_set=frontmatter_set or None,
            frontmatter_unset=unset_frontmatter,
            review=review,
            fix=fix,
            merge=merge,
            requires=_csv(requires) if requires is not None else None,
            land_order=_csv(land_order) if land_order is not None else None,
        )
    except OperationError as exc:
        raise typer.BadParameter(str(exc)) from exc
    print(f"[green]Updated {task_id}: {', '.join(changed)}[/green]")


@task_app.command("get")
def task_get(
    task_id: str,
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
    yaml_output: Annotated[
        bool, typer.Option("--yaml", help="Output as YAML (fewer tokens than JSON)")
    ] = False,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    container = _get_container(_get_root(path))
    node_repo = container.get(NodeRepository)
    task = node_repo.get_node(task_id)
    if not task:
        print(f"[red]Task '{task_id}' not found[/red]")
        raise typer.Exit(code=1)
    view = _view(container)
    snapshot = view.snapshot
    state = view.display(task)
    deps = {d: node_repo.get_node(d) for d in node_repo.get_dependencies(task_id)}
    verifications = node_repo.get_verifications(task_id)
    if json_output or yaml_output:
        lease = container.get(RuntimeRepository).get_lease(task_id)
        is_decision = task.kind == NodeKind.DECISION
        doc = _node_row(task, state)
        doc.update(
            {
                "spec_id": _task_spec_id(node_repo, task_id),
                "next_action": _next_action(container, task),
                "frontmatter": task.frontmatter,
                "outcome": task.outcome.value if task.outcome else None,
                "verdict": task.verdict,
                "fix_for": task.fix_for.value if task.fix_for else None,
                "claimed_from": task.claimed_from.value if task.claimed_from else None,
                "review_cycles": task.review_cycles,
                "merge_attempts": task.merge_attempts,
                "step_failures": task.step_failures,
                "branch": task.branch or f"tm/{task_id}",
                "requires": task.requires,
                "land_order": task.land_order,
                "landing_chain": [] if is_decision else landing_chain(snapshot, task_id),
                "depends_on": [
                    {"id": d, "status": n.status.value} if n else {"id": d} for d, n in deps.items()
                ],
                "blocked_by": [
                    d
                    for d, n in deps.items()
                    if n is None
                    or (
                        n.kind != NodeKind.DECISION
                        and not is_decision
                        and not satisfied(snapshot, task_id, d)
                    )
                ],
                "awaiting_decisions": [
                    d
                    for d, n in deps.items()
                    if n is not None
                    and n.kind == NodeKind.DECISION
                    and stored_status(n) == DecisionStatus.OPEN
                ],
                "conditions": [
                    c.model_dump(mode="json", exclude={"node_id"})
                    for c in node_repo.get_conditions(task_id)
                ],
                "declared_files": node_repo.declared_files(task_id),
                "sections": [s.section_key for s in node_repo.get_all_sections(task_id)],
                "verifications": [
                    {
                        "type": v.verification_type.value,
                        "target_path": v.target_path,
                        "expected_pattern": v.expected_pattern,
                    }
                    for v in verifications
                ],
                "lease": lease.model_dump(mode="json", exclude={"task_id"}) if lease else None,
                "jobs": [
                    j.model_dump(mode="json")
                    for j in container.get(JobRepository).for_node(task_id)
                ],
            }
        )
        _emit(doc, yaml_output)
        return
    print(f"[bold cyan]Task:[/] {task.id}")
    print(f"[bold]Title:[/] {escape(task.title)}")
    print(f"[bold]Status:[/] {task.status.value}")
    print(f"[bold]State:[/] {state}")
    print(f"[bold]Priority:[/] {task.priority}")
    print(f"[bold]Models:[/] {escape(', '.join(task.acceptable_models))}")
    if deps:
        print(f"[bold]Depends On:[/] {escape(', '.join(deps))}")
    if verifications:
        v_str = ", ".join(f"{v.verification_type.value}:{v.target_path}" for v in verifications)
        print(f"[bold]Verifications:[/] {escape(v_str)}")
```

The lifecycle verbs, added after `task_get`:

```python
@task_app.command("start")
def task_start(
    node_id: str,
    agent: Annotated[str, typer.Option("--agent", help="Agent identifier")],
    session: Annotated[str, typer.Option("--session", help="Dispatching session identifier")],
    ttl: Annotated[
        int | None, typer.Option("--ttl", min=1, help="Lease seconds (default: lease_ttl.<action>)")
    ] = None,
    worktree_dir: Annotated[
        Path | None,
        typer.Option(
            "--worktree-dir",
            help="Where an implement or fix step cuts its worktree (default: config worktree_dir)",
        ),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
    yaml_output: Annotated[bool, typer.Option("--yaml", help="Output as YAML (default)")] = False,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    """Claim the node's next step and print it; `blocked` exits 3 and writes nothing."""
    claims = _claims(_get_root(path))
    with _refusing():
        result = claims.start(node_id, agent, session, ttl, worktree_dir=worktree_dir)
    repos = list(result.repos)
    _emit(
        {
            "action": result.action.value,
            "reason": result.reason,
            "model": result.model,
            "job": result.job,
            "repo": repos[0] if len(repos) == 1 else (repos or None),
            "branch": result.branch,
            "base": result.base,
            "worktree": result.worktree,
            "worktrees": result.worktrees,
        },
        as_yaml=yaml_output or not json_output,
    )
    if result.action == Action.BLOCKED:
        raise typer.Exit(code=3)


@task_app.command("complete")
def task_complete(
    node_id: str,
    agent: Annotated[
        str | None,
        typer.Option("--agent", help="Refused unless the node's live lease is this agent's"),
    ] = None,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    """Close an implement or a fix step."""
    with _refusing():
        status = _claims(_get_root(path)).complete(node_id, agent=agent)
    print(f"[green]{node_id} is {status.value}[/green]")


@task_app.command("review")
def task_review(
    node_id: str,
    approve: Annotated[bool, typer.Option("--approve")] = False,
    reject: Annotated[bool, typer.Option("--reject")] = False,
    verdict: Annotated[
        str | None, typer.Option("--verdict", help="Free text; it never routes")
    ] = None,
    agent: Annotated[
        str | None,
        typer.Option("--agent", help="Refused unless the node's live lease is this agent's"),
    ] = None,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    """Close a review step; the node's :review section must have changed since the claim."""
    if approve == reject:
        raise typer.BadParameter("give exactly one of --approve or --reject")
    with _refusing():
        status = _claims(_get_root(path)).review(node_id, approve, verdict, agent=agent)
    print(f"[green]{node_id} is {status.value}[/green]")


@task_app.command("release")
def task_release(
    node_id: str,
    blocked: Annotated[
        bool, typer.Option("--blocked", help="The step stopped on what the flags below name")
    ] = False,
    depends: Annotated[
        str | None, typer.Option("--depends", help="Comma-separated ids it now waits on")
    ] = None,
    decision: Annotated[
        str | None, typer.Option("--decision", help="The question it now waits on")
    ] = None,
    option: Annotated[
        list[str] | None,
        typer.Option("--option", help="'key|Label|description|effect' for --decision, repeatable"),
    ] = None,
    recommend: Annotated[str | None, typer.Option("--recommend")] = None,
    needs: Annotated[
        str | None, typer.Option("--needs", help="The state a --command condition checks")
    ] = None,
    command: Annotated[
        str | None, typer.Option("--command", help="Exits 0 once --needs holds")
    ] = None,
    stage: Annotated[
        ConditionStage, typer.Option("--stage", help="claim or landing")
    ] = ConditionStage.CLAIM,
    agent: Annotated[
        str | None,
        typer.Option("--agent", help="Refused unless the node's live lease is this agent's"),
    ] = None,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    """Give a step back. Alone it is a transient failure, counted; with --blocked it names what
    the node now waits on and writes it in the same call."""
    named = bool(_csv(depends) or decision or needs or command)
    if named and not blocked:
        raise typer.BadParameter("--depends, --decision and --needs/--command go with --blocked")
    if blocked and not named:
        raise typer.BadParameter(
            "--blocked names what the node waits on: --depends <ids>, --decision <question> "
            "or --needs <state> --command <check>"
        )
    if (needs is None) != (command is None):
        raise typer.BadParameter("--needs and --command go together")
    blocker = (
        Blocker(
            depends=_csv(depends),
            decision=(
                DecisionSpec(question=decision, options=option or [], recommend=recommend)
                if decision
                else None
            ),
            condition=(
                Condition(node_id=node_id, idx=0, needs=needs, command=command, stage=stage)
                if needs is not None and command is not None
                else None
            ),
        )
        if blocked
        else None
    )
    with _refusing():
        status = _claims(_get_root(path)).release(node_id, blocked=blocker, agent=agent)
    print(f"[green]{node_id} is {status.value}[/green]")


@task_app.command("heartbeat")
def task_heartbeat(
    node_id: Annotated[
        str | None, typer.Argument(help="Node ID (optional inside its worktree)")
    ] = None,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    root = _get_root(path)
    tid = _resolve_task_id(_get_container(root).get(RuntimeRepository), node_id)
    with _refusing():
        alive = _claims(root).heartbeat(tid)
    if not alive:
        print(f"[red]No live lease on {escape(tid)}[/red]")
        raise typer.Exit(code=1)
    print(f"[green]Heartbeat recorded for {tid}[/green]")


@task_app.command("reopen")
def task_reopen(
    node_id: str,
    note: Annotated[str, typer.Option("--note", help="Why, and what to do differently")],
    new_branch: Annotated[
        bool, typer.Option("--new-branch", help="Rename the old branch to <branch>@<n>")
    ] = False,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    """From FAILED, DEFERRED or ABANDONED back into the cycle, keeping the branch."""
    with _refusing():
        status = _claims(_get_root(path)).reopen(node_id, note, new_branch=new_branch)
    print(f"[green]{node_id} is {status.value}[/green]")


@task_app.command("reset")
def task_reset(
    node_id: str,
    to: Annotated[
        Status, typer.Option("--to", help="READY, IMPLEMENTED, REVIEWED, FIXED or COMPLETED")
    ],
    note: Annotated[str, typer.Option("--note", help="Why the stored state was wrong")],
    outcome: Annotated[
        Outcome | None,
        typer.Option("--outcome", help="For REVIEWED: approve, reject or merge_failed"),
    ] = None,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    """A ledgered repair of a node with no live lease or job."""
    with _refusing():
        status = _claims(_get_root(path)).reset(node_id, to, note, outcome=outcome)
    print(f"[green]{node_id} is {status.value}[/green]")


@task_app.command("defer")
def task_defer(
    node_id: str,
    note: Annotated[str, typer.Option("--note", help="Why, and until when")],
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    with _refusing():
        status = _claims(_get_root(path)).defer(node_id, note)
    print(f"[green]{node_id} is {status.value}[/green]")


@task_app.command("abandon")
def task_abandon(
    node_id: str,
    note: Annotated[str, typer.Option("--note", help="Why it is dropped")],
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    with _refusing():
        status = _claims(_get_root(path)).abandon(node_id, note)
    print(f"[green]{node_id} is {status.value}[/green]")


@condition_app.command("add")
def condition_add(
    node_id: str,
    needs: Annotated[str, typer.Option("--needs", help="The state outside the corpus")],
    command: Annotated[str, typer.Option("--command", help="Exits 0 once the state holds")],
    stage: Annotated[
        ConditionStage, typer.Option("--stage", help="claim or landing")
    ] = ConditionStage.CLAIM,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    ops = _get_container(_get_root(path)).get(Operations)
    with _refusing():
        added = ops.add_condition(node_id, needs, command, stage)
    print(f"[green]Added condition {added.idx} to {node_id}[/green]")


@condition_app.command("remove")
def condition_remove(
    node_id: str,
    idx: int,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    ops = _get_container(_get_root(path)).get(Operations)
    with _refusing():
        ops.remove_condition(node_id, idx)
    print(f"[green]Removed condition {idx} from {node_id}[/green]")


@job_app.command("status")
def job_status(
    job_id: str,
    wait: Annotated[
        int,
        typer.Option("--wait", min=0, help="Block up to this many seconds while the job runs"),
    ] = 0,
    yaml_output: Annotated[bool, typer.Option("--yaml", help="Output as YAML")] = False,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    """Print a landing or sync job; with --wait, once it leaves `running` or the time is up."""
    jobs = _get_container(_get_root(path)).get(JobRepository)
    deadline = time.monotonic() + wait
    job = jobs.get(job_id)
    while job is not None and job.state == JobState.RUNNING:
        left = deadline - time.monotonic()
        if left <= 0:
            break
        time.sleep(min(1.0, left))
        job = jobs.get(job_id)
    if job is None:
        print(f"[red]No job '{escape(job_id)}'[/red]")
        raise typer.Exit(code=1)
    _emit(job.model_dump(mode="json"), yaml_output)


@job_app.command("resume")
def job_resume(
    job_id: str,
    own_defect: Annotated[
        str | None, typer.Option("--own-defect", help="The node's own defect, as a finding")
    ] = None,
    push: Annotated[
        bool, typer.Option("--push", help="An unattributed red is not this node's: push")
    ] = False,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    """How an agent finishes a landing or sync job that stopped for it."""
    if own_defect is not None and push:
        raise typer.BadParameter("--own-defect and --push contradict each other")
    landing = _landing(_get_root(path))
    with _refusing():
        state = landing.resume(job_id, own_defect=own_defect, push=push)
    print(f"[green]Job {job_id}: {state.value}[/green]")


@land_app.command("start")
def land_start(
    node_id: str,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    """Start the node's landing as a detached job and print its id."""
    landing = _landing(_get_root(path))
    with _refusing():
        job_id = landing.start_land(node_id)
    sys.stdout.write(f"{job_id}\n")
```

`run_start`, `run_heartbeat`, `run_stop` and `run_release` are deleted. `run_list`, `run_sweep` and `wave_discover` are replaced:

```python
@run_app.command("list")
def run_list(
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
    yaml_output: Annotated[
        bool, typer.Option("--yaml", help="Output as YAML (fewer tokens than JSON)")
    ] = False,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    runtime_repo = _get_container(_get_root(path)).get(RuntimeRepository)
    leases = runtime_repo.list_leases()
    locks = runtime_repo.list_locks()
    if json_output or yaml_output:
        _emit(
            {
                "leases": [lease.model_dump(mode="json") for lease in leases],
                "locks": [lock.model_dump(mode="json") for lock in locks],
            },
            yaml_output,
        )
        return

    table_leases = Table(title="Active Leases")
    for column in ("Node ID", "Action", "Agent ID", "Session ID", "Worktree", "Last Heartbeat"):
        table_leases.add_column(column)
    for lease in leases:
        table_leases.add_row(
            lease.task_id,
            lease.action.value if lease.action else "-",
            lease.agent_id,
            lease.session_id,
            lease.worktree_path or "-",
            lease.last_heartbeat.isoformat(),
        )
    print(table_leases)

    table_locks = Table(title="Locked Files")
    table_locks.add_column("File Path", style="green")
    table_locks.add_column("Node ID", style="cyan")
    table_locks.add_column("Lock Type")
    for lock in locks:
        table_locks.add_row(lock.file_path, lock.task_id, lock.lock_type.value)
    print(table_locks)


@run_app.command("sweep")
def run_sweep(
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    """Return every step whose lease expired to the status it was claimed from."""
    swept = _claims(_get_root(path)).sweep()
    if swept:
        print(f"[yellow]Swept {len(swept)} expired lease(s): {', '.join(swept)}[/yellow]")
    else:
        print("[green]No expired leases found.[/green]")


@wave_app.command("discover")
def wave_discover(
    session: Annotated[str, typer.Option("--session", help="Dispatching session id")],
    slots: Annotated[
        int, typer.Option("--slots", help="Total concurrent slots this session may hold")
    ],
    max_strong: Annotated[
        int, typer.Option("--max-strong", help="Cap on opus/fable leases for this session")
    ],
    spec: Annotated[
        list[str] | None,
        typer.Option("--spec", help="Spec id to search, repeatable; omitted means every node"),
    ] = None,
    exclude: Annotated[
        list[str] | None, typer.Option("--exclude", help="Node id to never choose this run")
    ] = None,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    """One dispatch wave's batch: a JSON payload line, then `__CHECK n=<chosen> h=<djb2>`.

    A caller with no shell of its own (a Workflow script) echoes the two lines back verbatim;
    the checksum lets the caller reject a transcription that is not byte-exact.
    """
    claims = _claims(_get_root(path))
    payload, chosen_count = discover(claims, spec or None, session, slots, max_strong, exclude)
    sys.stdout.write(f"{payload}\n__CHECK n={chosen_count} h={djb2(payload)}\n")
```

`decision_list` keeps Task 17's `DecisionStatus` map and `stored_status` filter.

In `search_cmd`, the `status` option becomes `status: Annotated[str | None, typer.Option("--status", help="Filter by stored status")] = None`.

**3d.** Old tests, rewritten or deleted in this task:

`tests/integration/test_cli.py`:
- Delete `test_cli_execution_leases_and_runtime` and `test_cli_run_release_drops_the_lease_without_changing_status`; `test_start_claims_implement_then_complete_and_a_review_that_approves`, `test_a_transient_release_counts_a_step_failure`, `test_an_expired_lease_is_swept_back_to_the_status_it_was_claimed_from` and `test_the_old_run_verbs_are_gone` cover what they did on the new verbs.
- `test_cli_lifecycle_spec_plan_task_render_next` step 3: `"--require-review",` becomes the two items `"--review", "--fix",`.
- Replace `test_spec_and_plan_list_and_get_show_derived_state_not_stored_status` with:

```python
def test_spec_and_plan_list_and_get_show_the_display_beside_the_stored_status(
    tmp_path: Path,
) -> None:
    from taskmanager.core.status import Status
    from taskmanager.db.connection import DatabaseManager
    from taskmanager.db.node_repo import NodeRepository
    from taskmanager.engine.snapshot import roll_up_ancestors

    runner.invoke(app, ["init", "--path", str(tmp_path)])
    runner.invoke(app, ["spec", "add", "S", "--slug", "S1", "--path", str(tmp_path)])
    runner.invoke(
        app, ["plan", "add", "P", "--spec", "S1", "--slug", "P1", "--path", str(tmp_path)]
    )
    runner.invoke(
        app, ["task", "add", "T", "--plan", "S1-P1", "--slug", "t1", "--path", str(tmp_path)]
    )
    node_repo = NodeRepository(DatabaseManager(tmp_path / ".taskmanager"))
    task = node_repo.get_node("S1-P1-t1")
    assert task is not None
    task.status = Status.COMPLETED
    node_repo.save_node(task)
    roll_up_ancestors(node_repo, "S1-P1-t1")

    res = runner.invoke(app, ["plan", "list", "--spec", "S1", "--path", str(tmp_path)])
    assert res.exit_code == 0 and "WAITING_MERGE" in res.stdout
    res = runner.invoke(app, ["plan", "get", "S1-P1", "--path", str(tmp_path)])
    assert "Status: IMPLEMENTED" in res.stdout and "State: WAITING_MERGE" in res.stdout
    res = runner.invoke(app, ["spec", "list", "--path", str(tmp_path)])
    assert res.exit_code == 0 and "IMPLEMENTING" in res.stdout
    res = runner.invoke(app, ["spec", "get", "S1", "--path", str(tmp_path)])
    assert "Status: READY" in res.stdout and "State: IMPLEMENTING" in res.stdout
```

`tests/integration/test_decisions_cli.py`: delete `test_run_stop_with_section_writes_both` and `test_run_stop_section_needs_section_file` — `tm run stop` and its same-transaction section write are gone; a step closes through `tm task complete|review` and sections are written with `tm section set` before it.

`tests/integration/test_wave_cli.py`: `from taskmanager.engine.wave import djb2` becomes `from taskmanager.engine.discovery import djb2`; the test body is unchanged.

`tests/unit/test_config.py`: delete `test_run_start_takes_its_worktree_directory_and_ttl_through_the_precedence`, the helpers `_git` and `_repo`, and the imports only they used (`subprocess`, `NodeKind`, `Node`, `DatabaseManager`, `NodeRepository`). Worktree placement and the per-action lease TTL are the claims engine's, tested in Task 13.

`tests/unit/test_estate_workflow.py`:
- `test_an_export_restores_into_a_fresh_root_and_exports_identically`: `["run", "stop", "S1-P1-a", "--status", "DEFERRED", "-C", str(source)]` becomes `["task", "defer", "S1-P1-a", "--note", "later", "-C", str(source)]`.
- `test_reads_are_json_or_yaml_and_the_two_agree`: `{"S1-P1-a": "READY", "S1-P1-b": "BLOCKED"}` becomes `{"S1-P1-a": "READY", "S1-P1-b": "BLOCKED_BY_TASK"}`.
- Delete `test_a_swept_merge_lease_returns_the_task_to_waiting_merge` and `test_a_swept_lease_returns_the_task_to_the_state_before_its_claim`; sweeping back to `claimed_from` is the claims engine's (Task 13) and `test_an_expired_lease_is_swept_back_to_the_status_it_was_claimed_from` covers the CLI.
- Replace `test_task_get_json_names_blockers_and_the_lease` with:

```python
def test_task_get_json_names_blockers_and_the_lease(tmp_path: Path) -> None:
    from taskmanager.core.models import Lease
    from taskmanager.core.status import Action, Status

    _seed_estate(tmp_path)
    root = str(tmp_path)
    doc = json.loads(runner.invoke(app, ["task", "get", "S1-P1-b", "--json", "-C", root]).stdout)
    assert doc["blocked_by"] == ["S1-P1-a"]
    assert doc["depends_on"] == [{"id": "S1-P1-a", "status": "READY"}]
    assert doc["lease"] is None
    db = DatabaseManager(tmp_path / ".taskmanager")
    node_repo = NodeRepository(db)
    node = node_repo.get_node("S1-P1-a")
    assert node is not None
    node.status, node.claimed_from = Status.IMPLEMENTING, Status.READY
    lease = Lease(
        task_id="S1-P1-a",
        agent_id="x",
        session_id="y",
        branch_name="tm/S1-P1-a",
        action=Action.IMPLEMENT,
        ttl_seconds=3600,
    )
    assert RuntimeRepository(db).claim(lease, [], node)
    leased = json.loads(runner.invoke(app, ["task", "get", "S1-P1-a", "--json", "-C", root]).stdout)
    assert leased["state"] == "IMPLEMENTING" and leased["lease"]["agent_id"] == "x"
    assert leased["lease"]["action"] == "implement"
    assert "body" in leased["sections"]
```

- Replace `test_plan_list_reports_the_state_its_tasks_add_up_to` with:

```python
def test_plan_list_reports_the_state_its_tasks_add_up_to(tmp_path: Path) -> None:
    from taskmanager.core.status import Status
    from taskmanager.engine.snapshot import roll_up_ancestors

    _seed_estate(tmp_path)
    rows = json.loads(runner.invoke(app, ["plan", "list", "--json", "-C", str(tmp_path)]).stdout)
    assert rows[0]["status"] == "READY" and rows[0]["state"] == "READY"
    node_repo = NodeRepository(DatabaseManager(tmp_path / ".taskmanager"))
    for task_id in ("S1-P1-a", "S1-P1-b"):
        node = node_repo.get_node(task_id)
        assert node is not None
        node.status = Status.COMPLETED
        node_repo.save_node(node)
    roll_up_ancestors(node_repo, "S1-P1-a")
    rows = json.loads(runner.invoke(app, ["plan", "list", "--json", "-C", str(tmp_path)]).stdout)
    assert rows[0]["status"] == "IMPLEMENTED" and rows[0]["state"] == "WAITING_MERGE"
```

- Replace `test_a_specs_state_rolls_up_from_its_plans_the_way_a_plans_does_from_its_tasks` with:

```python
def test_a_specs_state_rolls_up_from_its_plans_the_way_a_plans_does_from_its_tasks(
    tmp_path: Path,
) -> None:
    from taskmanager.core.status import Status
    from taskmanager.engine.snapshot import roll_up_ancestors

    root = str(tmp_path)
    runner.invoke(app, ["init", "-C", root])
    runner.invoke(app, ["spec", "add", "S", "--slug", "S1", "-C", root])
    runner.invoke(app, ["plan", "add", "P1", "--spec", "S1", "--slug", "P1", "-C", root])
    runner.invoke(app, ["plan", "add", "P2", "--spec", "S1", "--slug", "P2", "-C", root])
    runner.invoke(app, ["task", "add", "a", "--plan", "S1-P1", "--slug", "a", "-C", root])
    runner.invoke(app, ["task", "add", "b", "--plan", "S1-P2", "--slug", "b", "-C", root])
    node_repo = NodeRepository(DatabaseManager(tmp_path / ".taskmanager"))

    def spec_row() -> dict[str, str]:
        rows = json.loads(runner.invoke(app, ["spec", "list", "--json", "-C", root]).stdout)
        return next(r for r in rows if r["id"] == "S1")

    def set_status(node_id: str, status: Status) -> None:
        node = node_repo.get_node(node_id)
        assert node is not None
        node.status = status
        node_repo.save_node(node)

    assert (spec_row()["status"], spec_row()["state"]) == ("READY", "READY")
    set_status("S1-P1-a", Status.IMPLEMENTED)
    assert (spec_row()["status"], spec_row()["state"]) == ("READY", "IMPLEMENTING")
    for task_id in ("S1-P1-a", "S1-P2-b"):
        set_status(task_id, Status.COMPLETED)
        roll_up_ancestors(node_repo, task_id)
    assert (spec_row()["status"], spec_row()["state"]) == ("READY", "IMPLEMENTING")
    for plan_id in ("S1-P1", "S1-P2"):
        set_status(plan_id, Status.COMPLETED)
    roll_up_ancestors(node_repo, "S1-P1")
    assert (spec_row()["status"], spec_row()["state"]) == ("IMPLEMENTED", "WAITING_MERGE")
    get_out = runner.invoke(app, ["spec", "get", "S1", "-C", root]).stdout
    assert "Status: IMPLEMENTED" in get_out and "State: WAITING_MERGE" in get_out
```

The coordinator-driven tests in `test_estate_workflow.py` (`test_a_lease_locks_frontmatter_files_and_a_second_claim_is_refused`, `test_the_lease_ttl_is_the_one_asked_for`, the worktree tests, `test_each_stage_of_the_lifecycle_is_claimed_by_its_own_lease`, `test_a_merge_claim_refuses_a_worktree`, `test_a_fix_round_reuses_the_branch_and_worktree_its_first_round_cut`, `test_completing_a_task_removes_the_worktree_it_no_longer_holds_a_lease_on`, `test_reimporting_a_document_keeps_the_progress_it_does_not_state`) call `ExecutionCoordinator` directly, not the CLI; they stay green here and go with the coordinator in Task 21.

- [ ] **Step 4: Run the tests and the gates**

```
uv run --directory <worktree> pytest tests/integration/test_cli_lifecycle.py -q; echo $?
uv run --directory <worktree> pytest -q; echo $?
uv run --directory <worktree> ruff check; echo $?
uv run --directory <worktree> ruff format; echo $?
uv run --directory <worktree> ruff format --check; echo $?
uv run --directory <worktree> mypy; echo $?
```

Expected: every command prints `0` (the `ruff format` run rewrites only files this task touched).

- [ ] **Step 5: Commit**

```
git -C <worktree> add src/taskmanager/engine/snapshot.py src/taskmanager/di/container.py src/taskmanager/cli/main.py src/taskmanager/guides/dispatch.md src/taskmanager/guides/fix.md src/taskmanager/guides/implement.md src/taskmanager/guides/merge.md src/taskmanager/guides/overview.md src/taskmanager/guides/review.md commands/task.md tests/integration/test_cli_lifecycle.py tests/integration/test_cli.py tests/integration/test_decisions_cli.py tests/integration/test_wave_cli.py tests/unit/test_config.py tests/unit/test_estate_workflow.py && git -C <worktree> commit -m "feat(cli): claim and close steps through tm task verbs, jobs and land"
```

### Task 19: Web API

**Spec:** §5.3 (no status-setting route), §7.1, §7.2, §10.2
**Files:**
- Modify: `src/taskmanager/web/app.py` (request models `SpecCreate`, `PlanCreate`, `TaskCreate`, `NodeUpdate`, `DecisionOptionIn`; delete `StatusUpdate`; new `NoteRequest`, `ReopenRequest`, `ResetRequest`, `ConditionCreate`; `_SET_ASIDE_STATUSES`, `add_progress`; inside `create_app`: construction through the DI container and `Landing.open`, `new_view`, `lifecycle_fields`, `dependency_details`, `dependent_details`, routes `get_tree`, `get_graph`, `get_node_detail`, `get_stats`, `get_meta`, `create_spec`, `create_plan`, `create_task`, `patch_node`, `create_decision`, `delete_lease`, `post_sweep`, `list_decisions`, `_DECISION_TAB_STATUS`; delete `post_status`; new routes `post_reopen`, `post_reset`, `post_defer`, `post_abandon`, `post_condition`, `delete_condition`, `get_job`)
- Create: `tests/integration/test_web_lifecycle.py`
- Modify: `tests/integration/test_web_api.py`, `tests/integration/test_web.py`, `tests/unit/test_web_ui.py` (listed in Step 3b)

**Interfaces:**
- Consumes: `Status`, `DecisionStatus`, `DisplayStatus`, `Phase`, `Merge`, `Outcome`, `ConditionStage`, `DecisionEffect`, `Action` (Task 1); `base_chain`, `landing_chain`, `satisfied` (Task 5); `Job`, `Condition` (Task 8); `JobRepository.get/for_node`, `CacheRepository.get_condition/put_condition` taking the command (Task 9); `SnapshotBuilder`, `stored_status` (Task 10); `ConfigStore.project().condition_ttl` (Task 11); `DecisionOption.effect` (Task 12); `Claims.reopen/reset/defer/abandon/release/sweep` (Task 13); `Landing.open` (Task 14); `Operations.add_condition/remove_condition/update_node(...flags)`, `add_plan(...) -> str` (Task 17); `DisplayView`, `phase_of` (Task 18)
- Produces: the JSON the web UI (Task 20) reads — every tree, graph and detail node carries `status` (stored), `display`, `phase`, `review`, `fix`, `merge`, `outcome`, `verdict`, `fix_for`, `claimed_from`, `review_cycles`, `merge_attempts`, `step_failures`, `branch`, `requires`, `land_order`, `landing_chain`, `base_chain`; a detail adds `conditions` (with `last_result`) and `jobs`; `/api/stats` is `{total, display: {code: n}, phase: {code: n}}`; `/api/meta` lists `statuses`, `display_statuses`, `phases`, `decision_statuses`, `decision_effects`, `reset_targets`, `merge_targets`, `condition_stages`, `outcomes`; verb routes `POST /api/nodes/{id}/reopen|reset|defer|abandon`, condition routes, `GET /api/jobs/{job_id}`; decision rows carry `blocks`

- [ ] **Step 1: Write the failing test**

`tests/integration/test_web_lifecycle.py`:

```python
"""The web API on the lifecycle model: stored status beside the derived display and phase,
flags and conditions, verbs instead of a status setter, and job state."""

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from taskmanager.core.models import Lease
from taskmanager.core.status import Action, DecisionStatus, DisplayStatus, Phase, Status
from taskmanager.db.cache_repo import CacheRepository
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.di.container import create_container
from taskmanager.engine.operations import Operations
from taskmanager.web.app import create_app

JSON = {"content-type": "application/json"}
Web = tuple[TestClient, Path]


@pytest.fixture
def web(tmp_path: Path) -> Web:
    DatabaseManager(tmp_path / ".taskmanager").init_all()
    ops = create_container(tmp_path).get(Operations)
    spec = ops.add_spec("S", slug="S1")
    plan = ops.add_plan("P", spec, slug="P1", review=True, fix=True)
    a = ops.add_task("a", plan, slug="a")
    ops.add_task("b", plan, slug="b", depends_on=[a])
    return TestClient(create_app(tmp_path)), tmp_path


def repo(root: Path) -> NodeRepository:
    return NodeRepository(DatabaseManager(root / ".taskmanager"))


def plan_children(client: TestClient) -> dict[str, dict[str, Any]]:
    spec = next(n for n in client.get("/api/tree").json() if n["id"] == "S1")
    return {t["id"]: t for t in spec["children"][0]["children"]}


def test_tree_nodes_carry_the_stored_status_display_phase_and_flags(web: Web) -> None:
    client, _root = web
    tasks = plan_children(client)
    a, b = tasks["S1-P1-a"], tasks["S1-P1-b"]
    assert (a["status"], a["display"], a["phase"]) == ("READY", "READY", "QUEUED")
    assert (b["status"], b["display"], b["phase"]) == ("READY", "BLOCKED_BY_TASK", "QUEUED")
    assert (a["review"], a["fix"], a["merge"], a["branch"]) == (True, True, "main", "tm/S1-P1-a")
    assert "virtual_status" not in a
    spec = next(n for n in client.get("/api/tree").json() if n["id"] == "S1")
    plan = spec["children"][0]
    assert (plan["review"], plan["fix"], plan["display"]) == (True, True, "READY")


def test_node_detail_carries_chains_dependencies_conditions_and_jobs(web: Web) -> None:
    client, _root = web
    detail = client.get("/api/nodes/S1-P1-b").json()
    assert (detail["display"], detail["phase"]) == ("BLOCKED_BY_TASK", "QUEUED")
    assert detail["node"]["landing_chain"] == ["S1-P1-b"]
    assert detail["node"]["base_chain"] == ["MAIN"]
    assert [(d["id"], d["status"], d["finished"]) for d in detail["dependency_details"]] == [
        ("S1-P1-a", "READY", False)
    ]
    assert (detail["conditions"], detail["jobs"]) == ([], [])


def test_detail_lists_conditions_with_their_stage_and_last_result(web: Web) -> None:
    client, root = web
    res = client.post(
        "/api/nodes/S1-P1-a/conditions",
        json={"needs": "staging up", "command": "true", "stage": "landing"},
    )
    assert res.status_code == 201
    idx = res.json()["idx"]
    detail = client.get("/api/nodes/S1-P1-a").json()
    assert [(c["needs"], c["stage"], c["last_result"]) for c in detail["conditions"]] == [
        ("staging up", "landing", None)
    ]
    create_container(root).get(CacheRepository).put_condition("S1-P1-a", idx, "true", 0)
    detail = client.get("/api/nodes/S1-P1-a").json()
    assert detail["conditions"][0]["last_result"] == 0
    assert client.delete(f"/api/nodes/S1-P1-a/conditions/{idx}", headers=JSON).status_code == 200
    assert client.get("/api/nodes/S1-P1-a").json()["conditions"] == []


def test_a_prose_condition_is_refused(web: Web) -> None:
    client, _root = web
    res = client.post(
        "/api/nodes/S1-P1-a/conditions",
        json={"needs": "sign-off", "command": "the design is signed off"},
    )
    assert res.status_code == 400 and "decision" in res.json()["detail"]


def test_stats_count_every_display_status_and_phase_including_zeros(web: Web) -> None:
    client, _root = web
    stats = client.get("/api/stats").json()
    assert stats["total"] == 2
    assert set(stats["display"]) == {d.value for d in DisplayStatus}
    assert set(stats["phase"]) == {p.value for p in Phase}
    assert {k: v for k, v in stats["display"].items() if v} == {"READY": 1, "BLOCKED_BY_TASK": 1}
    assert {k: v for k, v in stats["phase"].items() if v} == {"QUEUED": 2}


def test_meta_lists_the_lifecycle_vocabularies(web: Web) -> None:
    client, _root = web
    meta = client.get("/api/meta").json()
    assert meta["statuses"] == [s.value for s in Status]
    assert meta["display_statuses"] == [d.value for d in DisplayStatus]
    assert meta["phases"] == [p.value for p in Phase]
    assert meta["decision_statuses"] == ["OPEN", "ANSWERED", "WITHDRAWN"]
    assert meta["reset_targets"] == ["READY", "IMPLEMENTED", "REVIEWED", "FIXED", "COMPLETED"]
    assert meta["merge_targets"] == ["parent", "main"]
    assert "none" in meta["decision_effects"]


def test_there_is_no_route_that_sets_a_status(web: Web) -> None:
    client, _root = web
    res = client.post("/api/nodes/S1-P1-a/status", json={"status": "COMPLETED"})
    assert res.status_code in (404, 405)
    assert repo(web[1]).get_node("S1-P1-a").status == Status.READY  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("verb", "body", "status", "section"),
    [
        ("defer", {"note": "after launch"}, Status.DEFERRED, "deferral"),
        ("abandon", {"note": "dropped"}, Status.ABANDONED, "abandonment"),
        ("reset", {"note": "repair", "to": "IMPLEMENTED"}, Status.IMPLEMENTED, None),
    ],
)
def test_a_verb_moves_the_node_and_records_its_note(
    web: Web, verb: str, body: dict[str, str], status: Status, section: str | None
) -> None:
    client, root = web
    res = client.post(f"/api/nodes/S1-P1-a/{verb}", json=body)
    assert res.status_code == 200, res.text
    assert res.json()["status"] == status.value
    node_repo = repo(root)
    assert node_repo.get_node("S1-P1-a").status == status  # type: ignore[union-attr]
    if section is not None:
        assert node_repo.get_section("S1-P1-a", section) is not None


def test_reopen_returns_a_deferred_node_to_ready(web: Web) -> None:
    client, root = web
    client.post("/api/nodes/S1-P1-a/defer", json={"note": "later"})
    res = client.post("/api/nodes/S1-P1-a/reopen", json={"note": "now", "new_branch": False})
    assert res.status_code == 200 and res.json()["status"] == "READY"


def test_a_verb_needs_a_note_and_reset_refuses_a_step_status(web: Web) -> None:
    client, root = web
    assert client.post("/api/nodes/S1-P1-a/defer", json={}).status_code == 422
    refused = client.post("/api/nodes/S1-P1-a/reset", json={"note": "x", "to": "REVIEWING"})
    assert refused.status_code in (400, 409)
    assert repo(root).get_node("S1-P1-a").status == Status.READY  # type: ignore[union-attr]


def test_patch_sets_flags_merge_requires_and_land_order_through_validation(web: Web) -> None:
    client, root = web
    ok = client.patch(
        "/api/nodes/S1-P1-a", json={"merge": "parent", "fix": False, "requires": ["figma"]}
    )
    assert ok.status_code == 200, ok.text
    assert client.patch("/api/nodes/S1-P1", json={"land_order": ["api", "web"]}).status_code == 200
    refused = client.patch("/api/nodes/S1-P1-b", json={"review": False})
    assert refused.status_code == 400
    node_repo = repo(root)
    a = node_repo.get_node("S1-P1-a")
    assert a is not None and (a.merge.value, a.fix, a.requires) == ("parent", False, ["figma"])
    assert node_repo.get_node("S1-P1").land_order == ["api", "web"]  # type: ignore[union-attr]
    assert node_repo.get_node("S1-P1-b").review is True  # type: ignore[union-attr]


def test_create_routes_take_the_flags(web: Web) -> None:
    client, root = web
    plan = client.post(
        "/api/plans", json={"title": "Q", "spec": "S1", "slug": "P2", "review": True, "fix": True}
    )
    assert plan.status_code == 201 and plan.json() == {"id": "S1-P2"}
    task = client.post(
        "/api/tasks",
        json={"title": "c", "plan": "S1-P2", "slug": "c", "merge": "parent", "requires": ["x"]},
    )
    assert task.status_code == 201
    node_repo = repo(root)
    p, c = node_repo.get_node("S1-P2"), node_repo.get_node("S1-P2-c")
    assert p is not None and (p.review, p.fix) == (True, True)
    assert c is not None and (c.merge.value, c.requires) == ("parent", ["x"])


def test_deleting_a_lease_gives_the_step_back_as_a_transient_release(web: Web) -> None:
    client, root = web
    node_repo = repo(root)
    node = node_repo.get_node("S1-P1-a")
    assert node is not None
    node.status, node.claimed_from = Status.IMPLEMENTING, Status.READY
    lease = Lease(
        task_id="S1-P1-a",
        agent_id="a",
        session_id="s",
        branch_name="tm/S1-P1-a",
        action=Action.IMPLEMENT,
        ttl_seconds=3600,
    )
    assert RuntimeRepository(node_repo.db).claim(lease, [], node)
    assert client.get("/api/nodes/S1-P1-a").json()["lease"]["action"] == "implement"
    assert client.delete("/api/nodes/S1-P1-a/lease", headers=JSON).status_code == 200
    after = node_repo.get_node("S1-P1-a")
    assert after is not None and (after.status, after.step_failures) == (Status.READY, 1)


def test_an_unknown_job_is_404(web: Web) -> None:
    client, _root = web
    assert client.get("/api/jobs/nope").status_code == 404


def test_decision_rows_use_decision_statuses_and_list_the_nodes_they_block(web: Web) -> None:
    client, _root = web
    res = client.post(
        "/api/decisions",
        json={
            "question": "Which way?",
            "slug": "way",
            "options": [{"key": "drop", "label": "Drop it", "effect": "abandon"}],
            "blocks": ["S1-P1-a"],
        },
    )
    assert res.status_code == 201
    rows = client.get("/api/decisions", params={"status": "open"}).json()
    assert [r["id"] for r in rows] == ["decision-way"]
    assert rows[0]["status"] == DecisionStatus.OPEN.value
    assert [(b["id"], b["display"]) for b in rows[0]["blocks"]] == [
        ("S1-P1-a", "AWAITING_DECISION")
    ]
    assert rows[0]["decision"]["options"][0]["effect"] == "abandon"
```

- [ ] **Step 2: Run it and watch it fail**

```
uv run --directory <worktree> pytest tests/integration/test_web_lifecycle.py -q; echo $?
```

Expected: exit 1. `KeyError: 'display'` on tree and detail nodes, `stats["display"]` missing, `/api/meta` lacking `display_statuses`, the status route still answering 200, 404 on the verb, condition and job routes, and `PATCH` rejecting `merge` as an unknown field only after `Operations` ignores it (the flags never reach it).

- [ ] **Step 3: Implement**

**3a.** `src/taskmanager/web/app.py`.

The import block becomes:

```python
"""FastAPI application for the TaskManager interactive web visualizer."""

import asyncio
import base64
import mimetypes
import sqlite3
import tempfile
from collections import Counter
from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

from dishka import make_container
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from taskmanager.core.enums import NodeKind, RenderView, TransferMode, VerificationType
from taskmanager.core.models import Node
from taskmanager.core.status import (
    ConditionStage,
    DecisionEffect,
    DecisionStatus,
    DisplayStatus,
    Merge,
    Outcome,
    Phase,
    Status,
)
from taskmanager.db.cache_repo import CacheRepository
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.job_repo import JobRepository
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.di.container import TaskManagerProvider
from taskmanager.engine.assets import ASSET_NAME_RE
from taskmanager.engine.chains import base_chain, landing_chain, satisfied
from taskmanager.engine.config import ConfigStore
from taskmanager.engine.heuristics import score_every_task
from taskmanager.engine.landing import Landing
from taskmanager.engine.operations import OperationError, Operations
from taskmanager.engine.snapshot import DisplayView, SnapshotBuilder, phase_of, stored_status
from taskmanager.renderers.markdown import MarkdownRenderer
from taskmanager.web.ui import get_web_html
```

The request models `SpecCreate`, `PlanCreate`, `TaskCreate`, `NodeUpdate` are replaced, `StatusUpdate` is deleted, `DependencyAdd` stays `id` only (Task 17), and four models are added:

```python
class SpecCreate(BaseModel):
    title: str
    slug: str | None = None
    priority: int = 50
    review: bool = False
    fix: bool = False


class PlanCreate(BaseModel):
    title: str
    spec: str
    slug: str | None = None
    priority: int = 50
    order: int = 0
    review: bool = False
    fix: bool = False
    merge: Merge = Merge.MAIN


class TaskCreate(BaseModel):
    title: str
    plan: str
    slug: str | None = None
    priority: int = 50
    order: int = 0
    depends_on: list[str] = Field(default_factory=list)
    models: list[str] = Field(default_factory=list)
    review: bool = True
    fix: bool = True
    merge: Merge = Merge.MAIN
    requires: list[str] = Field(default_factory=list)


class NodeUpdate(BaseModel):
    title: str | None = None
    priority: int | None = None
    acceptable_models: list[str] | None = None
    target_repo: str | None = None
    frontmatter_set: dict[str, Any] | None = None
    frontmatter_unset: list[str] | None = None
    review: bool | None = None
    fix: bool | None = None
    merge: Merge | None = None
    requires: list[str] | None = None
    land_order: list[str] | None = None


class NoteRequest(BaseModel):
    note: str


class ReopenRequest(NoteRequest):
    new_branch: bool = False


class ResetRequest(NoteRequest):
    to: Status
    outcome: Outcome | None = None


class ConditionCreate(BaseModel):
    needs: str
    command: str
    stage: ConditionStage = ConditionStage.CLAIM
```

`_SET_ASIDE_STATUSES` and `add_progress`:

```python
# A task in one of these cannot reach completion, so it is excluded from both the rollup and
# the progress denominator until its status changes back.
_SET_ASIDE_STATUSES = {Status.SUPERSEDED.value, Status.ABANDONED.value, Status.DEFERRED.value}
_RESET_TARGETS = (Status.READY, Status.IMPLEMENTED, Status.REVIEWED, Status.FIXED, Status.COMPLETED)


def add_progress(node: dict[str, Any]) -> tuple[Counter[str], int, int]:
    """Returns (display counts, done, set_aside) for the subtree rooted at `node`, and -- on
    every non-task node -- sets `node["progress"] = {done, total, set_aside, counts}`, where
    `total` is `done + (non-set-aside, non-done)` and `counts` keeps every display, set-aside
    included, so the caller can still render a full breakdown."""
    counts: Counter[str] = Counter()
    done = 0
    set_aside = 0
    if node["kind"] == NodeKind.TASK.value:
        display = node["display"]
        counts[display] += 1
        if display in _SET_ASIDE_STATUSES:
            set_aside += 1
        elif display == Status.COMPLETED.value:
            done += 1
    for child in node["children"]:
        child_counts, child_done, child_set_aside = add_progress(child)
        counts += child_counts
        done += child_done
        set_aside += child_set_aside
    if node["kind"] != NodeKind.TASK.value:
        total = sum(counts.values()) - set_aside
        node["progress"] = {
            "done": done,
            "total": total,
            "set_aside": set_aside,
            "counts": dict(counts),
        }
    return counts, done, set_aside
```

In `create_app`, everything from `db_dir = project_root / ".taskmanager"` down to `ws_manager = ConnectionManager()` is replaced by construction through the container the CLI uses:

```python
    db_dir = project_root / ".taskmanager"
    container = make_container(TaskManagerProvider(project_root))
    db_mgr = container.get(DatabaseManager)
    node_repo = container.get(NodeRepository)
    runtime_repo = container.get(RuntimeRepository)
    renderer = container.get(MarkdownRenderer)
    operations = container.get(Operations).with_actor("web")
    snapshots = container.get(SnapshotBuilder)
    # A verb or a lease release may start or stop a landing, so the claims carry the engine.
    claims = Landing.open(project_root).claims
    jobs = container.get(JobRepository)
    cache = container.get(CacheRepository)
    ws_manager = ConnectionManager()
```

`effective_status`, `_dependency_met`, `_relation_details`, `dependency_details` and `dependent_details` are replaced by:

```python
def _condition_ttl() -> int:
    return ConfigStore(project_root).project().condition_ttl


def new_view() -> DisplayView:
    """One snapshot per request, so every display in one response reads the same tree."""
    return DisplayView(snapshots, cache, _condition_ttl())


def _finished(view: DisplayView, source_id: str, target: Node) -> bool:
    if target.kind == NodeKind.DECISION:
        return stored_status(target) != DecisionStatus.OPEN
    nodes = view.snapshot.nodes
    return (
        source_id in nodes and target.id in nodes and satisfied(view.snapshot, source_id, target.id)
    )


def _relation_row(view: DisplayView, rel_id: str, finished: bool) -> dict[str, Any]:
    rel = node_repo.get_node(rel_id)
    return {
        "id": rel_id,
        "title": rel.title if rel else None,
        "kind": rel.kind.value if rel else None,
        "status": view.display(rel) if rel else None,
        # A missing node blocks.
        "finished": rel is not None and finished,
    }


def dependency_details(
    node_id: str, view: DisplayView, deps: list[str] | None = None
) -> list[dict[str, Any]]:
    rows = []
    for dep_id in deps if deps is not None else node_repo.get_dependencies(node_id):
        dep = node_repo.get_node(dep_id)
        rows.append(_relation_row(view, dep_id, dep is not None and _finished(view, node_id, dep)))
    return rows


def dependent_details(
    node_id: str, view: DisplayView, blocked_by: list[str] | None = None
) -> list[dict[str, Any]]:
    node = node_repo.get_node(node_id)
    return [
        _relation_row(view, src_id, node is not None and _finished(view, src_id, node))
        for src_id in (blocked_by if blocked_by is not None else node_repo.get_blocked_by(node_id))
    ]


def lifecycle_fields(n: Node, view: DisplayView) -> dict[str, Any]:
    is_decision = n.kind == NodeKind.DECISION
    return {
        # Read through stored_status so a node saved under an old name shows its new one.
        "status": stored_status(n).value,
        "display": view.display(n),
        "phase": phase_of(n),
        "review": n.review,
        "fix": n.fix,
        "merge": n.merge.value,
        "outcome": n.outcome.value if n.outcome else None,
        "verdict": n.verdict,
        "fix_for": n.fix_for.value if n.fix_for else None,
        "claimed_from": n.claimed_from.value if n.claimed_from else None,
        "review_cycles": n.review_cycles,
        "merge_attempts": n.merge_attempts,
        "step_failures": n.step_failures,
        "branch": n.branch or f"tm/{n.id}",
        "requires": n.requires,
        "land_order": n.land_order,
        "landing_chain": [] if is_decision else landing_chain(view.snapshot, n.id),
        "base_chain": [] if is_decision else base_chain(view.snapshot, n.id),
    }


def lease_dict(node_id: str) -> dict[str, Any] | None:
    lease = runtime_repo.get_lease(node_id)
    if lease is None:
        return None
    return {
        "agent_id": lease.agent_id,
        "session_id": lease.session_id,
        "branch_name": lease.branch_name,
        "worktree_path": lease.worktree_path,
        "action": lease.action.value if lease.action else None,
        "ttl_seconds": lease.ttl_seconds,
    }
```

`get_tree`'s inner `node_to_dict` and the lines creating `status_cache` are replaced (the rest of `get_tree` — the spec, standalone-plan and orphan-task walk and `add_progress` — is unchanged):

```python
    @app.get("/api/tree")
    def get_tree() -> list[dict[str, Any]]:
        specs = node_repo.list_nodes(kind=NodeKind.SPEC)
        plans = node_repo.list_nodes(kind=NodeKind.PLAN)
        tasks = node_repo.list_nodes(kind=NodeKind.TASK)
        task_scores = score_every_task(node_repo)
        view = new_view()

        def node_to_dict(n: Any) -> dict[str, Any]:
            sections = node_repo.get_all_sections(n.id)
            verifications = node_repo.get_verifications(n.id) if n.kind == NodeKind.TASK else []
            deps = node_repo.get_dependencies(n.id)
            blocked_by = node_repo.get_blocked_by(n.id)

            return {
                "id": n.id,
                "kind": n.kind.value,
                "title": n.title,
                **lifecycle_fields(n, view),
                "priority": n.priority,
                "score": task_scores.get(n.id) if n.kind == NodeKind.TASK else None,
                "ordinal": n.ordinal,
                "target_repo": n.target_repo,
                "acceptable_models": n.acceptable_models,
                "frontmatter": _frontmatter_with_attachment_sizes(n.frontmatter),
                "dependencies": deps,
                "dependency_details": dependency_details(n.id, view, deps),
                "blocked_by": blocked_by,
                "dependent_details": dependent_details(n.id, view, blocked_by),
                "sections": [
                    {
                        "key": s.section_key,
                        "header": s.header,
                        "content": s.content,
                        "ordinal": s.ordinal,
                    }
                    for s in sections
                ],
                "verifications": [
                    {
                        "id": v.id,
                        "type": v.verification_type.value,
                        "target": v.target_path,
                        "pattern": v.expected_pattern,
                    }
                    for v in verifications
                ],
                "lease": lease_dict(n.id) if n.kind == NodeKind.TASK else None,
                "children": [],
            }
```

`get_graph`, `get_node_detail`, `get_stats`, `get_meta`:

```python
@app.get("/api/graph")
def get_graph() -> dict[str, Any]:
    view = new_view()
    task_scores = score_every_task(node_repo)
    nodes_out = [
        {
            "id": n.id,
            "title": n.title,
            "kind": n.kind.value,
            "status": stored_status(n).value,
            "display": view.display(n),
            "phase": phase_of(n),
            "priority": n.priority,
            "score": task_scores.get(n.id) if n.kind == NodeKind.TASK else None,
            "ordinal": n.ordinal,
            "target_repo": n.target_repo,
            "acceptable_models": n.acceptable_models,
        }
        for n in node_repo.list_nodes()
    ]
    with db_mgr.get_state_connection() as conn:
        rows = conn.execute(
            "SELECT source_id, target_id, relation_type FROM node_relations"
        ).fetchall()
    edges_out = [{"source": r[0], "target": r[1], "type": r[2]} for r in rows]
    return {"nodes": nodes_out, "edges": edges_out}


@app.get("/api/nodes/{node_id}")
def get_node_detail(node_id: str) -> dict[str, Any]:
    node = node_repo.get_node(node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")

    view = new_view()
    dependencies = node_repo.get_dependencies(node_id)
    blocked_by = node_repo.get_blocked_by(node_id)
    ttl = _condition_ttl()
    return {
        "node": {
            "id": node.id,
            "kind": node.kind.value,
            "title": node.title,
            **lifecycle_fields(node, view),
            "priority": node.priority,
            "ordinal": node.ordinal,
            "target_repo": node.target_repo,
            "acceptable_models": node.acceptable_models,
            "frontmatter": _frontmatter_with_attachment_sizes(node.frontmatter),
        },
        "display": view.display(node),
        "phase": phase_of(node),
        "rendered_markdown": renderer.render(node_id, view=RenderView.FULL),
        "dependencies": dependencies,
        "dependency_details": dependency_details(node_id, view, dependencies),
        "blocked_by": blocked_by,
        "dependent_details": dependent_details(node_id, view, blocked_by),
        "sections": [
            {
                "key": s.section_key,
                "header": s.header,
                "content": s.content,
                "ordinal": s.ordinal,
            }
            for s in node_repo.get_all_sections(node_id)
        ],
        "verifications": [
            {
                "id": v.id,
                "verification_type": v.verification_type.value,
                "target_path": v.target_path,
                "expected_pattern": v.expected_pattern,
            }
            for v in node_repo.get_verifications(node_id)
        ],
        "conditions": [
            {
                "idx": c.idx,
                "needs": c.needs,
                "command": c.command,
                "stage": c.stage.value,
                "last_result": cache.get_condition(node_id, c.idx, c.command, ttl),
            }
            for c in node_repo.get_conditions(node_id)
        ],
        "jobs": [j.model_dump(mode="json") for j in jobs.for_node(node_id)],
        "lease": lease_dict(node_id),
    }


@app.get("/api/stats")
def get_stats() -> dict[str, Any]:
    view = new_view()
    tasks = node_repo.list_nodes(kind=NodeKind.TASK)
    display: dict[str, int] = {d.value: 0 for d in DisplayStatus}
    phases: dict[str, int] = {p.value: 0 for p in Phase}
    for t in tasks:
        code = view.display(t)
        display[code] = display.get(code, 0) + 1
        phase_code = phase_of(t)
        if phase_code is not None:
            phases[phase_code] += 1
    return {"total": len(tasks), "display": display, "phase": phases}


_DECISION_TAB_STATUS = {
    "open": DecisionStatus.OPEN,
    "answered": DecisionStatus.ANSWERED,
    "withdrawn": DecisionStatus.WITHDRAWN,
}


@app.get("/api/meta")
def get_meta() -> dict[str, Any]:
    all_nodes = node_repo.list_nodes()
    return {
        "statuses": [s.value for s in Status],
        "display_statuses": [d.value for d in DisplayStatus],
        "phases": [p.value for p in Phase],
        "decision_statuses": [d.value for d in DecisionStatus],
        "decision_effects": [e.value for e in DecisionEffect],
        "reset_targets": [s.value for s in _RESET_TARGETS],
        "merge_targets": [m.value for m in Merge],
        "condition_stages": [s.value for s in ConditionStage],
        "outcomes": [o.value for o in Outcome],
        "decision_states": list(_DECISION_TAB_STATUS),
        "verification_types": [t.value for t in VerificationType],
        "models": sorted({m for n in all_nodes for m in n.acceptable_models}),
        "repos": sorted({n.target_repo for n in all_nodes if n.target_repo}),
        "specs": [{"id": s.id, "title": s.title} for s in node_repo.list_nodes(kind=NodeKind.SPEC)],
        "plans": [{"id": p.id, "title": p.title} for p in node_repo.list_nodes(kind=NodeKind.PLAN)],
    }
```

The later definition of `_DECISION_TAB_STATUS` under `# -- decisions` is deleted (it now sits above `get_meta`).

The create and patch routes:

```python
@app.post("/api/specs", status_code=201)
def create_spec(body: SpecCreate, actor: Actor) -> dict[str, str]:
    with _refusals():
        spec_id = operations.with_actor(actor).add_spec(
            body.title, body.slug, body.priority, review=body.review, fix=body.fix
        )
    return {"id": spec_id}


@app.post("/api/plans", status_code=201)
def create_plan(body: PlanCreate, actor: Actor) -> dict[str, Any]:
    with _refusals():
        plan_id = operations.with_actor(actor).add_plan(
            body.title,
            body.spec,
            body.slug,
            body.priority,
            body.order,
            review=body.review,
            fix=body.fix,
            merge=body.merge,
        )
    return {"id": plan_id}


@app.post("/api/tasks", status_code=201)
def create_task(body: TaskCreate, actor: Actor) -> dict[str, str]:
    with _refusals():
        task_id = operations.with_actor(actor).add_task(
            body.title,
            body.plan,
            body.slug,
            body.priority,
            body.order,
            body.depends_on,
            body.models,
            review=body.review,
            fix=body.fix,
            merge=body.merge,
            requires=body.requires,
        )
    return {"id": task_id}


@app.patch("/api/nodes/{node_id}")
def patch_node(node_id: str, body: NodeUpdate, actor: Actor) -> dict[str, Any]:
    with _refusals():
        changed = operations.with_actor(actor).update_node(
            node_id,
            title=body.title,
            priority=body.priority,
            models=body.acceptable_models,
            repo=body.target_repo,
            frontmatter_set=body.frontmatter_set,
            frontmatter_unset=body.frontmatter_unset,
            review=body.review,
            fix=body.fix,
            merge=body.merge,
            requires=body.requires,
            land_order=body.land_order,
        )
    return changed
```

`post_status` is deleted. The verbs, conditions, lease and job routes (in place of `post_status`, `delete_lease`, `post_sweep`):

```python
def _moved(node_id: str) -> dict[str, str]:
    node = node_repo.get_node(node_id)
    return {"id": node_id, "status": node.status.value if node else ""}


@app.post("/api/nodes/{node_id}/reopen")
def post_reopen(node_id: str, body: ReopenRequest, actor: Actor) -> dict[str, str]:
    with _refusals():
        claims.reopen(node_id, body.note, new_branch=body.new_branch)
    return _moved(node_id)


@app.post("/api/nodes/{node_id}/reset")
def post_reset(node_id: str, body: ResetRequest, actor: Actor) -> dict[str, str]:
    with _refusals():
        claims.reset(node_id, body.to, body.note, outcome=body.outcome)
    return _moved(node_id)


@app.post("/api/nodes/{node_id}/defer")
def post_defer(node_id: str, body: NoteRequest, actor: Actor) -> dict[str, str]:
    with _refusals():
        claims.defer(node_id, body.note)
    return _moved(node_id)


@app.post("/api/nodes/{node_id}/abandon")
def post_abandon(node_id: str, body: NoteRequest, actor: Actor) -> dict[str, str]:
    with _refusals():
        claims.abandon(node_id, body.note)
    return _moved(node_id)


@app.post("/api/nodes/{node_id}/conditions", status_code=201)
def post_condition(node_id: str, body: ConditionCreate, actor: Actor) -> dict[str, Any]:
    with _refusals():
        added = operations.with_actor(actor).add_condition(
            node_id, body.needs, body.command, body.stage
        )
    return added.model_dump(mode="json")


@app.delete("/api/nodes/{node_id}/conditions/{idx}")
def delete_condition(node_id: str, idx: int, actor: Actor) -> dict[str, int]:
    with _refusals():
        operations.with_actor(actor).remove_condition(node_id, idx)
    return {"idx": idx}


@app.delete("/api/nodes/{node_id}/lease")
def delete_lease(node_id: str, actor: Actor) -> dict[str, str]:
    with _refusals():
        claims.release(node_id)
    return _moved(node_id)


@app.post("/api/leases/sweep")
def post_sweep(actor: Actor) -> dict[str, list[str]]:
    with _refusals():
        swept = claims.sweep()
    return {"swept": swept}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, f"no job '{job_id}'")
    return job.model_dump(mode="json")
```

A decision option carries its effect from the form to `Operations.add_decision`, which takes the four-field option string (`key|Label|description|effect`, Task 12). `DecisionOptionIn` gains `effect: DecisionEffect = DecisionEffect.NONE`, and in `create_decision` the join becomes:

```python
        options = [f"{o.key}|{o.label}|{o.description}|{o.effect.value}" for o in body.options]
```

`list_decisions`:

```python
    @app.get("/api/decisions")
    def list_decisions(status: str | None = None) -> list[dict[str, Any]]:
        decisions = node_repo.list_nodes(kind=NodeKind.DECISION)
        if status is not None:
            wanted = _DECISION_TAB_STATUS.get(status.lower())
            if wanted is None:
                raise HTTPException(400, "status is one of: open, answered, withdrawn")
            decisions = [d for d in decisions if stored_status(d) == wanted]
        view = new_view()

        def blocks(decision_id: str) -> list[dict[str, Any]]:
            rows = []
            for node_id in node_repo.get_blocked_by(decision_id):
                node = node_repo.get_node(node_id)
                if node is not None:
                    rows.append(
                        {
                            "id": node.id,
                            "title": node.title,
                            "kind": node.kind.value,
                            "display": view.display(node),
                        }
                    )
            return rows

        return [
            {
                "id": d.id,
                "title": d.title,
                "status": stored_status(d).value,
                "priority": d.priority,
                "created_at": d.created_at.isoformat(),
                "waiting_count": len(node_repo.get_blocked_by(d.id)),
                "blocks": blocks(d.id),
                "decision": d.frontmatter.get("decision") or {},
                "attachments": _attachments_with_size(d.frontmatter.get("attachments") or []),
            }
            for d in decisions
        ]
```

Every other route (`index`, the websocket, sections, verifications, `post_verify`, supersede, move, dependencies, the decision create/answer/reopen/withdraw/blocks routes, attachments and file serving) is unchanged. The decision answer route keeps passing through `Operations.answer_decision`, which applies the chosen option's effect (Task 12).

**3b.** Old tests, rewritten or deleted in this task:

`tests/integration/test_web_api.py`:
- Imports: `from taskmanager.core.enums import NodeKind, NodeStatus, RelationType, VerificationType` becomes `from taskmanager.core.enums import NodeKind, RelationType, VerificationType`, plus `from taskmanager.core.status import Action, DecisionStatus, Status` and `from taskmanager.core.models import Lease, Node, NodeRelation, NodeVerification`.
- The `api` fixture passes `status=Status.READY` to each of its four `Node(...)` calls (`SPEC`, `SPEC-P1`, `SPEC-P1-T1`, `SPEC-P1-T2`).
- `test_get_meta_lists_pickers`: `assert "NOT_STARTED" in body["statuses"]` becomes `assert "NOT_STARTED" not in body["statuses"]`.
- Delete `test_post_status_updates_node` and `test_post_status_unknown_node_is_404_and_writes_nothing`; `test_there_is_no_route_that_sets_a_status` and the verb tests replace them.
- `test_post_supersede`: `NodeStatus.SUPERSEDED` becomes `Status.SUPERSEDED`. `test_post_supersede_unknown_replacement_refused_and_writes_nothing`: `NodeStatus.NOT_STARTED` becomes `Status.READY`.
- Replace `test_delete_lease_releases_it` with:

```python
def test_delete_lease_gives_the_claimed_step_back(
    api: tuple[TestClient, NodeRepository, LedgerRepository],
) -> None:
    client, node_repo, _ledger_repo = api
    node = node_repo.get_node("SPEC-P1-T1")
    assert node is not None
    node.status, node.claimed_from = Status.IMPLEMENTING, Status.READY
    lease = Lease(
        task_id="SPEC-P1-T1",
        agent_id="a",
        session_id="s",
        branch_name="tm/SPEC-P1-T1",
        action=Action.IMPLEMENT,
        ttl_seconds=3600,
    )
    from taskmanager.db.runtime_repo import RuntimeRepository

    assert RuntimeRepository(node_repo.db).claim(lease, [], node)
    res = client.delete("/api/nodes/SPEC-P1-T1/lease", headers=JSON)
    assert res.status_code == 200
    assert res.json() == {"id": "SPEC-P1-T1", "status": "READY"}
```


`tests/integration/test_web.py`:
- Imports: drop `NodeStatus` and `VirtualStatus`; add `from taskmanager.core.status import Action, DecisionStatus, DisplayStatus, Outcome, Phase, Status`.
- `test_web_api_endpoints_and_ui`: `status=NodeStatus.NOT_STARTED` becomes `status=Status.READY`; `node_detail["virtual_status"] == "READY"` becomes `node_detail["display"] == "READY"`; `stats["READY"] >= 1` becomes `stats["display"]["READY"] >= 1`.
- `ALL_STATUS_CODES` and the `every_status_project` fixture are replaced by:

```python
SEEDED_DISPLAY = {
    "T-READY": "READY",
    "T-IMPLEMENTING": "IMPLEMENTING",
    "T-STALE": "STALE",
    "T-WAITING-REVIEW": "WAITING_REVIEW",
    "T-WAITING-FIX": "WAITING_FIX",
    "T-WAITING-MERGE": "WAITING_MERGE",
    "T-COMPLETED": "COMPLETED",
    "T-FAILED": "FAILED",
    "T-DEFERRED": "DEFERRED",
    "T-ABANDONED": "ABANDONED",
    "T-SUPERSEDED": "SUPERSEDED",
    "T-AWAITING-DECISION": "AWAITING_DECISION",
    "T-BLOCKED": "BLOCKED_BY_TASK",
    "T-BLOCKED-BY-LEASE": "BLOCKED_BY_LEASE",
}
DISPLAY_CODES = {d.value for d in DisplayStatus}


@pytest.fixture
def every_display_project(tmp_path: Path) -> Path:
    db_mgr = DatabaseManager(tmp_path / ".taskmanager")
    db_mgr.init_all()
    node_repo = NodeRepository(db_mgr)
    for node_id, kind in (("SPEC", NodeKind.SPEC), ("PLAN", NodeKind.PLAN)):
        node_repo.save_node(
            Node(
                id=node_id,
                kind=kind,
                title=node_id.title(),
                status=Status.READY,
                review=False,
                fix=False,
            )
        )
    node_repo.add_relation(
        NodeRelation(source_id="SPEC", target_id="PLAN", relation_type=RelationType.CONTAINS)
    )
    shared_path = "src/shared/module.py"

    def add_task(task_id: str, status: Status, repo: str = "core", **fields: Any) -> None:
        node_repo.save_node(
            Node(
                id=task_id,
                kind=NodeKind.TASK,
                title=f"Task {task_id}",
                status=status,
                review=True,
                fix=True,
                target_repo=repo,
                acceptable_models=["claude-opus-5"] if repo == "core" else ["gemini-flash"],
                **fields,
            )
        )
        node_repo.add_relation(
            NodeRelation(source_id="PLAN", target_id=task_id, relation_type=RelationType.CONTAINS)
        )

    add_task("T-READY", Status.READY)
    add_task(
        "T-IMPLEMENTING", Status.READY, repo="web", frontmatter={"declared_files": [shared_path]}
    )
    add_task("T-STALE", Status.IMPLEMENTING, claimed_from=Status.READY)
    add_task("T-WAITING-REVIEW", Status.IMPLEMENTED)
    add_task("T-WAITING-FIX", Status.REVIEWED, outcome=Outcome.REJECT)
    add_task("T-WAITING-MERGE", Status.REVIEWED, outcome=Outcome.APPROVE)
    add_task("T-COMPLETED", Status.COMPLETED)
    add_task("T-FAILED", Status.FAILED)
    add_task("T-DEFERRED", Status.DEFERRED)
    add_task("T-ABANDONED", Status.ABANDONED)
    add_task("T-SUPERSEDED", Status.SUPERSEDED)
    add_task("T-AWAITING-DECISION", Status.READY)
    add_task("T-BLOCKED", Status.READY)
    add_task("T-BLOCKED-BY-LEASE", Status.READY, frontmatter={"declared_files": [shared_path]})
    node_repo.save_node(
        Node(id="DECISION", kind=NodeKind.DECISION, title="Which way?", status=DecisionStatus.OPEN)
    )
    for source, target in (
        ("T-AWAITING-DECISION", "DECISION"),
        ("T-BLOCKED", "T-IMPLEMENTING"),
        ("T-BLOCKED", "T-SUPERSEDED"),
    ):
        node_repo.add_relation(
            NodeRelation(source_id=source, target_id=target, relation_type=RelationType.DEPENDS_ON)
        )
    node_repo.save_section(
        NodeSection(
            node_id="T-DEFERRED",
            section_key="deferral",
            ordinal=1,
            header="## Deferral",
            content="line one\nline two",
        )
    )
    claimed = node_repo.get_node("T-IMPLEMENTING")
    assert claimed is not None
    claimed.status, claimed.claimed_from = Status.IMPLEMENTING, Status.READY
    lease = Lease(
        task_id="T-IMPLEMENTING",
        agent_id="agent",
        session_id="session",
        branch_name="tm/T-IMPLEMENTING",
        action=Action.IMPLEMENT,
        acquired_at=datetime.now(tz=UTC),
        last_heartbeat=datetime.now(tz=UTC),
        ttl_seconds=300,
    )
    assert RuntimeRepository(db_mgr).claim(
        lease, [FileLock(file_path=shared_path, task_id="T-IMPLEMENTING")], claimed
    )
    return tmp_path
```

(add `from typing import Any` to the module imports). Every test that took `every_status_project` takes `every_display_project`. Then:

- Replace `test_stats_reports_every_status_including_zeros` with:

```python
def test_stats_report_every_display_status_and_phase_including_zeros(
    every_display_project: Path,
) -> None:
    stats = TestClient(create_app(every_display_project)).get("/api/stats").json()
    assert stats["total"] == 14
    assert set(stats["display"]) == DISPLAY_CODES
    assert {k: v for k, v in stats["display"].items() if v} == {
        code: 1 for code in SEEDED_DISPLAY.values()
    }
    assert set(stats["phase"]) == {p.value for p in Phase}
    assert stats["phase"] == {
        "QUEUED": 4,
        "DISPATCHED": 5,
        "COMPLETED": 1,
        "FAILED": 1,
        "DEFERRED": 1,
        "ABANDONED": 1,
        "SUPERSEDED": 1,
    }


@pytest.mark.parametrize(("task_id", "display"), sorted(SEEDED_DISPLAY.items()))
def test_every_seeded_task_reads_its_display_status(
    every_display_project: Path, task_id: str, display: str
) -> None:
    detail = TestClient(create_app(every_display_project)).get(f"/api/nodes/{task_id}").json()
    assert detail["display"] == display
```

- Replace `test_tree_progress_counts_each_status_separately` with:

```python
def test_tree_progress_counts_each_display_status_separately(every_display_project: Path) -> None:
    tree = TestClient(create_app(every_display_project)).get("/api/tree").json()
    spec = tree[0]
    plan = spec["children"][0]
    expected = {code: 1 for code in SEEDED_DISPLAY.values()}
    # DEFERRED, ABANDONED and SUPERSEDED can never finish, so they leave both `total` and its
    # `done` count rather than diluting them.
    assert plan["progress"] == {"done": 1, "total": 11, "set_aside": 3, "counts": expected}
    assert spec["progress"] == plan["progress"]
    assert (plan["status"], plan["display"]) == ("READY", "IMPLEMENTING")
```

- Replace `test_spec_status_in_tree_is_a_rollup_not_its_stored_status` with:

```python
def test_a_container_in_the_tree_shows_its_stored_status_beside_its_display(
    tmp_path: Path,
) -> None:
    db_mgr = DatabaseManager(tmp_path / ".taskmanager")
    db_mgr.init_all()
    node_repo = NodeRepository(db_mgr)
    node_repo.save_node(Node(id="S", kind=NodeKind.SPEC, title="Spec", status=Status.READY))
    node_repo.save_node(Node(id="S-P1", kind=NodeKind.PLAN, title="Plan", status=Status.READY))
    node_repo.add_relation(
        NodeRelation(source_id="S", target_id="S-P1", relation_type=RelationType.CONTAINS)
    )
    node_repo.save_node(
        Node(id="S-P1-T1", kind=NodeKind.TASK, title="Task", status=Status.COMPLETED)
    )
    node_repo.add_relation(
        NodeRelation(source_id="S-P1", target_id="S-P1-T1", relation_type=RelationType.CONTAINS)
    )

    tree = TestClient(create_app(tmp_path)).get("/api/tree").json()
    spec = next(n for n in tree if n["id"] == "S")
    # Nothing re-derived the stored rollup, so the display reads the started descendant.
    assert (spec["status"], spec["display"]) == ("READY", "IMPLEMENTING")
```

- `test_tree_and_graph_carry_task_score_and_dependents`: the fixture name only.
- `test_tree_task_lists_dependencies_with_their_own_status`: `blocked["virtual_status"] == "BLOCKED"` becomes `blocked["display"] == "BLOCKED_BY_TASK"`; in the expected `dependency_details`, `"status": "IMPLEMENTING"` stays (the live lease reads IMPLEMENTING) and `"status": "SUPERSEDED"` stays.
- `test_dependency_details_carry_kind_so_the_page_can_tell_a_decision_apart`: save the task with `status=Status.READY` and the decision with `status=DecisionStatus.OPEN`; the expected `"status": "NOT_STARTED"` becomes `"status": "OPEN"`.
- `test_node_detail_lists_dependencies_and_every_section`: `deferred["virtual_status"] == "DEFERRED"` becomes `deferred["display"] == "DEFERRED"`.
- `test_graph_nodes_carry_repo_and_models_for_filtering`: `by_id["T-INFLIGHT"]["status"] == "IN_FLIGHT"` becomes `by_id["T-IMPLEMENTING"]["display"] == "IMPLEMENTING"`.
- `test_static_export_embeds_every_status_and_the_filter_ui`: delete the `themes_match` search, its assert, `themes = ...` and `assert set(themes) == ALL_STATUS_CODES` (the themes change in Task 20, which asserts them); `assert ALL_STATUS_CODES <= static["stats"].keys()` becomes `assert set(static["stats"]["display"]) == DISPLAY_CODES`; `progress["total"] == 13` becomes `== 11`.

`tests/unit/test_web_ui.py`: the helper `_task(status)` returns `{"kind": "task", "display": status, "children": []}`; both `add_progress` tests are otherwise unchanged.

- [ ] **Step 4: Run the tests and the gates**

```
uv run --directory <worktree> pytest tests/integration/test_web_lifecycle.py tests/integration/test_web_api.py tests/integration/test_web.py tests/unit/test_web_ui.py -q; echo $?
uv run --directory <worktree> pytest -q; echo $?
uv run --directory <worktree> ruff check; echo $?
uv run --directory <worktree> ruff format; echo $?
uv run --directory <worktree> ruff format --check; echo $?
uv run --directory <worktree> mypy; echo $?
```

Expected: every command prints `0` (the `ruff format` run rewrites only files this task touched).

- [ ] **Step 5: Commit**

```
git -C <worktree> add src/taskmanager/web/app.py tests/integration/test_web_lifecycle.py tests/integration/test_web_api.py tests/integration/test_web.py tests/unit/test_web_ui.py && git -C <worktree> commit -m "feat(web): serve stored status, display and phase, and verbs instead of a status setter"
```

### Task 20: Web UI

**Spec:** §7.1, §7.2, §10.2
**Files:**
- Modify: `src/taskmanager/web/enums.py` (`AppIcon` gains `GIT_MERGE`, `OCTAGON_X`; `StatusGroup`, `StatusVisual` rebuilt over `DisplayStatus`; new `PhaseTheme`, `PhaseVisual`)
- Modify: `src/taskmanager/web/ui.py` (`get_web_html` injects `window.PHASE_THEMES` and the phase CSS)
- Modify: `src/taskmanager/web/static/index.html` (a `phase-filter` container in `#filter-controls-group`)
- Modify: `src/taskmanager/web/static/js/core.js` (`phaseFilterEl`, `getTheme`, new `displayOf`, new `phaseChip`)
- Modify: `src/taskmanager/web/static/js/filters.js` (`NO_PHASE`, `filters`, `structuralFilterActive`, `taskPasses`, `readHash`, `writeHash`, `passesOtherDimensions`, `updateStatsDigest`, new `phaseTriState`, `activeFilterCount`, `renderFilterControls`, the clear handler, `renderLegend`)
- Modify: `src/taskmanager/web/static/js/tree.js` (four `virtual_status` reads)
- Modify: `src/taskmanager/web/static/js/graph.js` (`renderGraph`'s theme read)
- Modify: `src/taskmanager/web/static/js/detail.js` (`renderDependencies`, `renderActionBar`, `wireActionBar`, `showGraphInspector`, `decorateAwaitingDecisionBanners`; new `landingChainText`, `renderLifecycle`, `wireLifecycleControls`)
- Modify: `src/taskmanager/web/static/js/edit.js` (delete `REAL_NODE_STATUSES`, `setNodeStatus`, `changeStatus`, `openOtherStatusDialog`; new `postVerb`, `VERB_COPY`, `openVerbDialog`, `RESET_TARGETS`, `OUTCOMES`, `openResetDialog`, `openFlagsDialog`, `openAddConditionDialog`, `removeCondition`; `openNewPlanDialog`, `openAddDependencyDialog`, `releaseLease`)
- Modify: `src/taskmanager/web/static/js/decisions.js` (`DECISION_TABS`, `DECISION_STATUS_ICON`, `updateDecisionsBadge`, `optionCardHtml`, `renderDecisionDetail`'s status reads)
- Modify: `tests/unit/test_web_enums.py` (rewritten), `tests/unit/test_web_ui.py` (listed in Step 3c), `tests/integration/test_web.py` (the static export's themes)

**Interfaces:**
- Consumes: `DisplayStatus`, `Phase`, `DecisionStatus` (Task 1); the web JSON of Task 19 (`display`, `phase`, flags, `landing_chain`, `base_chain`, `conditions`, `jobs`, `blocks`, verb and condition routes)
- Produces: `StatusVisual` (one theme per `DisplayStatus`), `PhaseVisual` (one per `Phase`), `window.STATUS_THEMES`, `window.PHASE_THEMES`; the page no longer names a pre-lifecycle status

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_web_enums.py` is replaced in full:

```python
"""The web visualizer's status and phase registries."""

import re

import pytest

from taskmanager.core.status import DisplayStatus, Phase
from taskmanager.web.enums import AppIcon, PhaseVisual, StatusGroup, StatusVisual, WebViewMode

DISPLAY_CODES = {d.value for d in DisplayStatus}
PHASE_CODES = {p.value for p in Phase}


def _luminance(hex_colour: str) -> float:
    channels = [int(hex_colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(foreground: str, background: str) -> float:
    lighter, darker = sorted((_luminance(foreground), _luminance(background)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def test_every_display_status_has_exactly_one_theme() -> None:
    assert {m.value.code for m in StatusVisual} == DISPLAY_CODES
    assert set(StatusVisual.all_themes_dict()) == DISPLAY_CODES
    assert all(m.name == m.value.code for m in StatusVisual)
    for code in DISPLAY_CODES:
        d = StatusVisual.from_status(code).to_dict()
        assert d["code"] == code and d["label"] and d["icon"]


def test_an_unknown_status_falls_back_to_the_stale_theme() -> None:
    assert StatusVisual.from_status("NOT_STARTED") == StatusVisual.STALE


def test_every_phase_has_exactly_one_theme() -> None:
    assert {m.value.code for m in PhaseVisual} == PHASE_CODES
    assert set(PhaseVisual.all_themes_dict()) == PHASE_CODES


def test_status_labels_icons_and_colours_are_distinct() -> None:
    themes = [m.value for m in StatusVisual]
    for field in ("label", "icon", "dark_fg", "light_fg"):
        values = [getattr(t, field) for t in themes]
        assert len(set(values)) == len(values), field
    assert len({(t.dark_fg, t.dark_bg) for t in themes}) == len(themes)


@pytest.mark.parametrize(
    "theme",
    [m.value for m in StatusVisual] + [m.value for m in PhaseVisual],
    ids=[f"status-{m.name}" for m in StatusVisual] + [f"phase-{m.name}" for m in PhaseVisual],
)
def test_text_contrast_is_at_least_aa_in_both_themes(theme: object) -> None:
    assert _contrast(theme.dark_fg, theme.dark_bg) >= 4.5  # type: ignore[attr-defined]
    assert _contrast(theme.light_fg, theme.light_bg) >= 4.5  # type: ignore[attr-defined]
    assert _contrast("#f3f4f6", theme.dark_bg) >= 4.5  # type: ignore[attr-defined]


def test_descriptions_are_one_sentence_and_every_group_is_used() -> None:
    for theme in [m.value for m in StatusVisual] + [m.value for m in PhaseVisual]:
        assert theme.description.endswith(".")
        assert theme.description.count(". ") == 0
    assert {m.value.group for m in StatusVisual} == set(StatusGroup)


def test_css_defines_both_theme_variables_for_every_status_and_phase() -> None:
    css = StatusVisual.css() + PhaseVisual.css()
    for prefix, codes in (("st", DISPLAY_CODES), ("ph", PHASE_CODES)):
        for code in codes:
            assert re.search(rf"(?<!\.dark )\.{prefix}-{code}\{{--st-fg:#\w+;--st-bg:#\w+\}}", css)
            assert re.search(rf"\.dark \.{prefix}-{code}\{{--st-fg:#\w+;--st-bg:#\w+\}}", css)


def test_app_icon_sprite_carries_the_new_status_icons() -> None:
    sprite = AppIcon.generate_svg_sprite()
    assert 'id="icon-git-merge"' in sprite and 'id="icon-octagon-x"' in sprite


def test_webview_mode_values() -> None:
    assert WebViewMode.DOCUMENT.value == "document"
    assert WebViewMode.GRAPH.value == "graph"
```

Appended to `tests/unit/test_web_ui.py`:

```python
def test_the_page_names_no_pre_lifecycle_status_or_status_setter() -> None:
    html = get_web_html()
    for word in (
        "NOT_STARTED",
        "WAITING_FIXES",
        "IN_FLIGHT",
        "virtual_status",
        "REAL_NODE_STATUSES",
        "/status`",
        "dep-gate",
    ):
        assert word not in html, word


def test_the_page_carries_phase_themes_and_a_phase_filter() -> None:
    html = get_web_html()
    assert "window.PHASE_THEMES = " in html
    assert '<div id="phase-filter" class="relative"></div>' in html
    assert "createTriStatePopover(phaseFilterEl" in html
    read_hash = _function_body(html, "readHash")
    write_hash = _function_body(html, "writeHash")
    for key in ("phase", "xphase"):
        assert f"p.get('{key}')" in read_hash and f"p.set('{key}'" in write_hash


def test_action_bar_offers_verbs_by_stored_status() -> None:
    body = _function_body(get_web_html(), "renderActionBar")
    assert "REOPENABLE.includes(node.status)" in body
    assert "SETTABLE_ASIDE.includes(node.status)" in body
    assert "ab-reset" in body and "ab-flags" in body
    wire = _function_body(get_web_html(), "wireActionBar")
    for verb in ("'reopen'", "'defer'", "'abandon'"):
        assert f"openVerbDialog(node, {verb})" in wire
    assert "openResetDialog(node)" in wire and "openFlagsDialog(node)" in wire


def test_every_verb_collects_a_note_and_abandon_is_destructive() -> None:
    body = _function_body(get_web_html(), "openVerbDialog")
    assert "vb-note" in body and "A note is required." in body
    assert "destructive: verb === 'abandon'" in body
    assert "vb-new-branch" in body


def test_the_inspector_shows_the_lifecycle_panel() -> None:
    html = get_web_html()
    panel = _function_body(html, "renderLifecycle")
    for field in (
        "n.status",
        "n.outcome",
        "n.verdict",
        "n.review_cycles",
        "n.merge_attempts",
        "n.step_failures",
        "n.requires",
        "detail.conditions",
        "detail.jobs",
        "c.last_result",
        "c.stage",
        "landingChainText(n)",
    ):
        assert field in panel, field
    assert "renderLifecycle(detail, editable)" in _function_body(html, "showGraphInspector")


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed to exercise the JS")
def test_landing_chain_text_reads_the_base_chain(tmp_path: Path) -> None:
    fn = _function_body(get_web_html(), "landingChainText")
    script = tmp_path / "check.js"
    script.write_text(
        "function esc(s) { return String(s); }\n"
        f"function landingChainText(node) {{{fn}\n}}\n"
        "const assert = require('node:assert');\n"
        "assert.strictEqual(landingChainText({base_chain: ['MAIN']}), 'lands on main');\n"
        "assert.strictEqual(landingChainText({base_chain: ['P', 'MAIN']}),"
        " 'on tm/P; waits for P → main');\n"
        "assert.strictEqual(landingChainText({base_chain: []}), '');\n"
        "console.log('OK');\n",
        encoding="utf-8",
    )
    result = subprocess.run(["node", str(script)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_decisions_read_open_answered_and_withdrawn_and_show_option_effects() -> None:
    html = get_web_html()
    assert "{ key: 'open', label: 'Open', status: 'OPEN' }" in html
    assert "OPEN: 'help-circle'" in html
    card = _function_body(html, "optionCardHtml")
    assert "opt.effect && opt.effect !== 'none'" in card


def test_static_export_themes_every_display_status(tmp_path: Path) -> None:
    import json

    from taskmanager.core.status import DisplayStatus, Phase

    html = get_web_html(initial_data={"tree": []})
    themes = json.loads(re.search(r"window.STATUS_THEMES = (\{.*?\});\n", html).group(1))  # type: ignore[union-attr]
    phases = json.loads(re.search(r"window.PHASE_THEMES = (\{.*?\});\n", html).group(1))  # type: ignore[union-attr]
    assert set(themes) == {d.value for d in DisplayStatus}
    assert set(phases) == {p.value for p in Phase}
```

- [ ] **Step 2: Run them and watch them fail**

```
uv run --directory <worktree> pytest tests/unit/test_web_enums.py tests/unit/test_web_ui.py -q; echo $?
```

Expected: exit 1. `ImportError: cannot import name 'PhaseVisual'`, and in `test_web_ui.py` the new tests fail on `NOT_STARTED` still in the page, a missing `window.PHASE_THEMES`, and missing `renderLifecycle`, `openVerbDialog`, `landingChainText`.

- [ ] **Step 3: Implement**

**3a.** `src/taskmanager/web/enums.py`. Two icons join `AppIcon`, after `MINUS`:

```python
    # Status icons with no other meaning on the page: a sync merge and a failed node.
    GIT_MERGE = IconData(
        "git-merge",
        '<circle cx="18" cy="18" r="3"/><circle cx="6" cy="6" r="3"/><path d="M6 21V9a9 9 0 0 0 9 9"/>',
    )
    OCTAGON_X = IconData(
        "octagon-x",
        '<path d="m15 9-6 6"/><path d="M2.586 16.726A2 2 0 0 1 2 15.312V8.688a2 2 0 0 1 .586-1.414l4.688-4.688A2 2 0 0 1 8.688 2h6.624a2 2 0 0 1 1.414.586l4.688 4.688A2 2 0 0 1 22 8.688v6.624a2 2 0 0 1-.586 1.414l-4.688 4.688a2 2 0 0 1-1.414.586H8.688a2 2 0 0 1-1.414-.586z"/><path d="m9 9 6 6"/>',
    )
```

`StatusGroup`, `StatusTheme`, `StatusVisual` are replaced, and the phase registry follows:

```python
class StatusGroup(Enum):
    READY = "Ready"
    BLOCKED = "Blocked"
    IN_PROGRESS = "In progress"
    WAITING = "Waiting"
    FINISHED = "Finished"
    SET_ASIDE = "Set aside"


class StatusTheme(NamedTuple):
    code: str
    label: str
    icon: AppIcon
    group: StatusGroup
    description: str
    dark_fg: str
    dark_bg: str
    light_fg: str
    light_bg: str


def _theme_dict(theme: Any) -> dict[str, Any]:
    return {
        "code": theme.code,
        "label": theme.label,
        "icon": theme.icon.value.name,
        "description": theme.description,
        "dark_fg": theme.dark_fg,
        "dark_bg": theme.dark_bg,
        "light_fg": theme.light_fg,
        "light_bg": theme.light_bg,
        "graph_bg": theme.dark_bg,
        "graph_border": theme.dark_fg,
    }


def _css(prefix: str, themes: list[Any]) -> str:
    light = "".join(
        f".{prefix}-{t.code}{{--st-fg:{t.light_fg};--st-bg:{t.light_bg}}}" for t in themes
    )
    dark = "".join(
        f".dark .{prefix}-{t.code}{{--st-fg:{t.dark_fg};--st-bg:{t.dark_bg}}}" for t in themes
    )
    return light + dark


class StatusVisual(Enum):
    """One theme per DisplayStatus, in its order."""

    READY = StatusTheme(
        "READY",
        "Ready",
        AppIcon.PLAY_CIRCLE,
        StatusGroup.READY,
        "Nothing it waits on is open, so its next step can be claimed now.",
        "#bef264",
        "#365314",
        "#3f6212",
        "#ecfccb",
    )
    IMPLEMENTING = StatusTheme(
        "IMPLEMENTING",
        "Implementing",
        AppIcon.PLAY,
        StatusGroup.IN_PROGRESS,
        "An agent holds the implement lease, or a container's children are under way.",
        "#a5b4fc",
        "#312e81",
        "#4338ca",
        "#e0e7ff",
    )
    REVIEWING = StatusTheme(
        "REVIEWING",
        "Reviewing",
        AppIcon.EYE,
        StatusGroup.IN_PROGRESS,
        "A reviewer holds the review lease.",
        "#c4b5fd",
        "#4c1d95",
        "#6d28d9",
        "#ede9fe",
    )
    FIXING = StatusTheme(
        "FIXING",
        "Fixing",
        AppIcon.WRENCH,
        StatusGroup.IN_PROGRESS,
        "An agent holds the fix lease for a rejection or a failed landing.",
        "#f9a8d4",
        "#831843",
        "#be185d",
        "#fce7f3",
    )
    MERGING = StatusTheme(
        "MERGING",
        "Merging",
        AppIcon.GIT_BRANCH,
        StatusGroup.IN_PROGRESS,
        "A landing job is merging, gating and pushing the branch.",
        "#93c5fd",
        "#1e3a8a",
        "#1d4ed8",
        "#dbeafe",
    )
    COMPLETED = StatusTheme(
        "COMPLETED",
        "Completed",
        AppIcon.CHECK_CIRCLE_2,
        StatusGroup.FINISHED,
        "Landed on its target and verified there; the only status that counts as done.",
        "#86efac",
        "#14532d",
        "#166534",
        "#dcfce7",
    )
    FAILED = StatusTheme(
        "FAILED",
        "Failed",
        AppIcon.OCTAGON_X,
        StatusGroup.FINISHED,
        "A cap was reached; a decision asks whether to abandon or investigate.",
        "#f87171",
        "#450a0a",
        "#991b1b",
        "#fef2f2",
    )
    DEFERRED = StatusTheme(
        "DEFERRED",
        "Deferred",
        AppIcon.PAUSE_CIRCLE,
        StatusGroup.SET_ASIDE,
        "Postponed with a note and never counted as done until reopened.",
        "#94a3b8",
        "#1e293b",
        "#475569",
        "#e2e8f0",
    )
    ABANDONED = StatusTheme(
        "ABANDONED",
        "Abandoned",
        AppIcon.X_CIRCLE,
        StatusGroup.SET_ASIDE,
        "Dropped for good with a note and never counted as done.",
        "#d6d3d1",
        "#44403c",
        "#57534e",
        "#e7e5e4",
    )
    SUPERSEDED = StatusTheme(
        "SUPERSEDED",
        "Superseded",
        AppIcon.ARCHIVE,
        StatusGroup.SET_ASIDE,
        "Replaced by another node; it satisfies dependents but is not counted as done.",
        "#f0abfc",
        "#701a75",
        "#a21caf",
        "#fae8ff",
    )
    WAITING_REVIEW = StatusTheme(
        "WAITING_REVIEW",
        "Waiting Review",
        AppIcon.CLOCK,
        StatusGroup.WAITING,
        "Implemented or fixed, and its review waits for a reviewer.",
        "#fcd34d",
        "#78350f",
        "#92400e",
        "#fef3c7",
    )
    WAITING_FIX = StatusTheme(
        "WAITING_FIX",
        "Waiting Fix",
        AppIcon.ALERT_TRIANGLE,
        StatusGroup.WAITING,
        "Its review rejected it or its landing found its own defect, and the fix waits for an agent.",
        "#fdba74",
        "#7c2d12",
        "#9a3412",
        "#ffedd5",
    )
    WAITING_MERGE = StatusTheme(
        "WAITING_MERGE",
        "Waiting Merge",
        AppIcon.GIT_PULL_REQUEST,
        StatusGroup.WAITING,
        "Ready to land, and waits for a landing job.",
        "#5eead4",
        "#134e4a",
        "#115e59",
        "#ccfbf1",
    )
    WAITING_MERGE_AGENT = StatusTheme(
        "WAITING_MERGE_AGENT",
        "Waiting Merge Agent",
        AppIcon.BOT,
        StatusGroup.WAITING,
        "Its landing job stopped on a conflict or an unattributed red, and waits for an agent.",
        "#7dd3fc",
        "#0c4a6e",
        "#0369a1",
        "#e0f2fe",
    )
    STALE = StatusTheme(
        "STALE",
        "Stale",
        AppIcon.CIRCLE_DASHED,
        StatusGroup.WAITING,
        "A step was claimed but its lease expired or is missing; a sweep returns it.",
        "#a1a1aa",
        "#27272a",
        "#52525b",
        "#f4f4f5",
    )
    AWAITING_DECISION = StatusTheme(
        "AWAITING_DECISION",
        "Awaiting Decision",
        AppIcon.HELP_CIRCLE,
        StatusGroup.BLOCKED,
        "An edge points at an open decision; answering or withdrawing it unblocks the node.",
        "#fbbf24",
        "#451a03",
        "#b45309",
        "#fffbeb",
    )
    BLOCKED_BY_TASK = StatusTheme(
        "BLOCKED_BY_TASK",
        "Blocked by Task",
        AppIcon.LOCK,
        StatusGroup.BLOCKED,
        "A dependency's code has not landed where this node builds yet.",
        "#fca5a5",
        "#7f1d1d",
        "#b91c1c",
        "#fee2e2",
    )
    BLOCKED_BY_CONDITION = StatusTheme(
        "BLOCKED_BY_CONDITION",
        "Blocked by Condition",
        AppIcon.TERMINAL,
        StatusGroup.BLOCKED,
        "A condition's command has not exited 0 yet.",
        "#fde047",
        "#422006",
        "#854d0e",
        "#fefce8",
    )
    BLOCKED_BY_SYNC = StatusTheme(
        "BLOCKED_BY_SYNC",
        "Blocked by Sync",
        AppIcon.GIT_MERGE,
        StatusGroup.BLOCKED,
        "A sync of the branch it builds on is running or waits for an agent.",
        "#67e8f9",
        "#164e63",
        "#0e7490",
        "#ecfeff",
    )
    BLOCKED_BY_LEASE = StatusTheme(
        "BLOCKED_BY_LEASE",
        "Blocked by Lease",
        AppIcon.HOURGLASS,
        StatusGroup.BLOCKED,
        "A file its next step would lock is held by another node's lease.",
        "#fda4af",
        "#881337",
        "#be123c",
        "#ffe4e6",
    )

    @classmethod
    def from_status(cls, status: str) -> StatusVisual:
        try:
            return cls[status.upper()]
        except KeyError:
            return cls.STALE

    def to_dict(self) -> dict[str, Any]:
        return {**_theme_dict(self.value), "group": self.value.group.name}

    @classmethod
    def all_themes_dict(cls) -> dict[str, dict[str, Any]]:
        return {item.value.code: item.to_dict() for item in cls}

    @classmethod
    def groups_list(cls) -> list[dict[str, str]]:
        return [{"code": g.name, "label": g.value} for g in StatusGroup]

    @classmethod
    def css(cls) -> str:
        return _css("st", [m.value for m in cls])


class PhaseTheme(NamedTuple):
    code: str
    label: str
    icon: AppIcon
    description: str
    dark_fg: str
    dark_bg: str
    light_fg: str
    light_bg: str


class PhaseVisual(Enum):
    """One theme per Phase, in its order."""

    QUEUED = PhaseTheme(
        "QUEUED",
        "Queued",
        AppIcon.PLAY_CIRCLE,
        "Waiting for its first claim.",
        "#bef264",
        "#365314",
        "#3f6212",
        "#ecfccb",
    )
    DISPATCHED = PhaseTheme(
        "DISPATCHED",
        "Dispatched",
        AppIcon.PLAY,
        "Claimed at least once and not yet landed.",
        "#a5b4fc",
        "#312e81",
        "#4338ca",
        "#e0e7ff",
    )
    COMPLETED = PhaseTheme(
        "COMPLETED",
        "Completed",
        AppIcon.CHECK_CIRCLE_2,
        "Landed and verified.",
        "#86efac",
        "#14532d",
        "#166534",
        "#dcfce7",
    )
    FAILED = PhaseTheme(
        "FAILED",
        "Failed",
        AppIcon.OCTAGON_X,
        "Stopped at a cap, waiting on its decision.",
        "#f87171",
        "#450a0a",
        "#991b1b",
        "#fef2f2",
    )
    DEFERRED = PhaseTheme(
        "DEFERRED",
        "Deferred",
        AppIcon.PAUSE_CIRCLE,
        "Postponed.",
        "#94a3b8",
        "#1e293b",
        "#475569",
        "#e2e8f0",
    )
    ABANDONED = PhaseTheme(
        "ABANDONED",
        "Abandoned",
        AppIcon.X_CIRCLE,
        "Dropped.",
        "#d6d3d1",
        "#44403c",
        "#57534e",
        "#e7e5e4",
    )
    SUPERSEDED = PhaseTheme(
        "SUPERSEDED",
        "Superseded",
        AppIcon.ARCHIVE,
        "Replaced by another node.",
        "#f0abfc",
        "#701a75",
        "#a21caf",
        "#fae8ff",
    )

    @classmethod
    def all_themes_dict(cls) -> dict[str, dict[str, Any]]:
        return {item.value.code: _theme_dict(item.value) for item in cls}

    @classmethod
    def css(cls) -> str:
        return _css("ph", [m.value for m in cls])
```

(`ruff format` expands each theme's positional arguments one per line; the values are the ones above.) `web/__init__.py` also exports `PhaseVisual` in its import and `__all__`.

**3b.** `src/taskmanager/web/ui.py`: the import becomes `from taskmanager.web.enums import AppIcon, PhaseVisual, StatusVisual, WebViewMode`; in `get_web_html`, the `runtime_data` string gains the line

```python
        f"    window.PHASE_THEMES = {json.dumps(PhaseVisual.all_themes_dict())};\n"
```

after the `window.STATUS_GROUPS` line, and the CSS slot is filled with both registries:

```python
    css = _read_static("app.css").replace(
        "<!--slot:status-css-->", StatusVisual.css() + PhaseVisual.css()
    )
```

**3c.** `src/taskmanager/web/static/index.html`: in `#filter-controls-group`, after `<div id="spec-filter" class="relative"></div>`, add

```html
        <div id="phase-filter" class="relative"></div>
```

**3d.** `core.js`. After `const specFilterEl = ...` add `const phaseFilterEl = document.getElementById('phase-filter');`. `getTheme` is replaced and two helpers follow it:

```js
function getTheme(status) {
  return window.STATUS_THEMES[status] || { ...window.STATUS_THEMES.STALE, label: String(status) };
}

// What a reader sees for a node: the derived display, else (a decision) its own status.
function displayOf(n) {
  return n.display || n.status;
}

function phaseChip(code, size = 'text-[10px]') {
  const t = window.PHASE_THEMES[code];
  if (!t) return '';
  return `<span class="st-chip ph-${t.code} inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full font-medium ${size}" title="${esc(t.description)}">${renderIcon(t.icon, 'w-3 h-3')}<span>${esc(t.label)}</span></span>`;
}
```

**3e.** `filters.js`. Below `const NO_SPEC = '(none)';` add `const NO_PHASE = '(none)';`. The replaced symbols:

```js
const filters = {
  statusMode: new Map(), phaseMode: new Map(), repoMode: new Map(), modelMode: new Map(),
  specMode: new Map(), scoreMin: null, scoreMax: null, q: ''
};
```

```js
function structuralFilterActive() {
  return filters.statusMode.size > 0 || filters.phaseMode.size > 0 || filters.repoMode.size > 0 ||
    filters.modelMode.size > 0 || filters.specMode.size > 0 ||
    filters.scoreMin !== null || filters.scoreMax !== null;
}

function taskPasses(t) {
  if (!dimensionPasses(filters.statusMode, [displayOf(t)])) return false;
  if (!dimensionPasses(filters.phaseMode, [t.phase || NO_PHASE])) return false;
  if (!dimensionPasses(filters.repoMode, [t.target_repo || NO_REPO])) return false;
  if (!dimensionPasses(filters.modelMode, t.acceptable_models || [])) return false;
  if (!dimensionPasses(filters.specMode, [t._specId])) return false;
  if (filters.scoreMin !== null && typeof t.score === 'number' && t.score < filters.scoreMin) return false;
  if (filters.scoreMax !== null && typeof t.score === 'number' && t.score > filters.scoreMax) return false;
  return true;
}
```

```js
function readHash() {
  const p = new URLSearchParams(location.hash.slice(1));
  filters.statusMode = new Map();
  (p.get('status') || '').split(',').filter(c => window.STATUS_THEMES[c]).forEach(c => filters.statusMode.set(c, 'include'));
  (p.get('xstatus') || '').split(',').filter(c => window.STATUS_THEMES[c]).forEach(c => filters.statusMode.set(c, 'exclude'));
  filters.phaseMode.clear();
  (p.get('phase') || '').split(',').filter(c => window.PHASE_THEMES[c]).forEach(c => filters.phaseMode.set(c, 'include'));
  (p.get('xphase') || '').split(',').filter(c => window.PHASE_THEMES[c]).forEach(c => filters.phaseMode.set(c, 'exclude'));
  filters.repoMode.clear();
  (p.get('repo') || '').split(',').filter(Boolean).forEach(v => filters.repoMode.set(v, 'include'));
  (p.get('xrepo') || '').split(',').filter(Boolean).forEach(v => filters.repoMode.set(v, 'exclude'));
  filters.modelMode.clear();
  (p.get('model') || '').split(',').filter(Boolean).forEach(v => filters.modelMode.set(v, 'include'));
  (p.get('xmodel') || '').split(',').filter(Boolean).forEach(v => filters.modelMode.set(v, 'exclude'));
  filters.specMode.clear();
  (p.get('spec') || '').split(',').filter(Boolean).forEach(v => filters.specMode.set(v, 'include'));
  (p.get('xspec') || '').split(',').filter(Boolean).forEach(v => filters.specMode.set(v, 'exclude'));
  const smin = p.get('smin');
  const smax = p.get('smax');
  filters.scoreMin = smin !== null && smin !== '' ? Number(smin) : null;
  filters.scoreMax = smax !== null && smax !== '' ? Number(smax) : null;
  filters.q = (p.get('q') || '').toLowerCase();
  searchBox.value = filters.q;
}
```

```js
function writeHash() {
  const p = new URLSearchParams();
  const status = modeEntries(filters.statusMode);
  if (status.inc.length) p.set('status', status.inc.join(','));
  if (status.exc.length) p.set('xstatus', status.exc.join(','));
  const phase = modeEntries(filters.phaseMode);
  if (phase.inc.length) p.set('phase', phase.inc.join(','));
  if (phase.exc.length) p.set('xphase', phase.exc.join(','));
  const repo = modeEntries(filters.repoMode);
  if (repo.inc.length) p.set('repo', repo.inc.join(','));
  if (repo.exc.length) p.set('xrepo', repo.exc.join(','));
  const model = modeEntries(filters.modelMode);
  if (model.inc.length) p.set('model', model.inc.join(','));
  if (model.exc.length) p.set('xmodel', model.exc.join(','));
  const spec = modeEntries(filters.specMode);
  if (spec.inc.length) p.set('spec', spec.inc.join(','));
  if (spec.exc.length) p.set('xspec', spec.exc.join(','));
  if (filters.scoreMin !== null) p.set('smin', String(filters.scoreMin));
  if (filters.scoreMax !== null) p.set('smax', String(filters.scoreMax));
  if (filters.q) p.set('q', filters.q);
  try {
    history.replaceState(null, '', p.toString() ? '#' + p : location.pathname + location.search);
  } catch (e) {
    console.error('Could not update the URL hash:', e);
  }
}
```

```js
function passesOtherDimensions(t, exclude) {
  if (exclude !== 'status' && !dimensionPasses(filters.statusMode, [displayOf(t)])) return false;
  if (exclude !== 'phase' && !dimensionPasses(filters.phaseMode, [t.phase || NO_PHASE])) return false;
  if (exclude !== 'repo' && !dimensionPasses(filters.repoMode, [t.target_repo || NO_REPO])) return false;
  if (exclude !== 'model' && !dimensionPasses(filters.modelMode, t.acceptable_models || [])) return false;
  if (exclude !== 'spec' && !dimensionPasses(filters.specMode, [t._specId])) return false;
  if (filters.scoreMin !== null && typeof t.score === 'number' && t.score < filters.scoreMin) return false;
  if (filters.scoreMax !== null && typeof t.score === 'number' && t.score > filters.scoreMax) return false;
  if (filters.q !== '' && !textMatches(t)) return false;
  return true;
}
```

```js
function updateStatsDigest() {
  // A rebuild replaces every chip with a new DOM node, so the one that had focus (Enter on a
  // status chip is a normal way to apply a filter) would otherwise drop to BODY and a
  // following Shift+Enter would land on nothing. Re-find and refocus its replacement by the
  // status code it carries, __all__ standing in for the "All tasks" chip.
  const focusedCode = statsDigest.contains(document.activeElement)
    ? document.activeElement.dataset.statusCode
    : null;

  statsDigest.innerHTML = '';
  const counts = computeDimensionCounts('status', t => [displayOf(t)]);
  const total = collectTasks(treeData).filter(t => passesOtherDimensions(t, 'status')).length;
  const allActive = filters.statusMode.size === 0;
  const totalChip = document.createElement('button');
  totalChip.className = `flex items-center gap-1 px-2 py-1 rounded-md border text-xs transition focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-500 ${allActive ? 'bg-zinc-800 text-white border-zinc-700' : 'bg-zinc-900/60 text-zinc-400 border-zinc-800 hover:bg-zinc-800'}`;
  totalChip.title = 'All tasks';
  totalChip.dataset.statusCode = '__all__';
  totalChip.setAttribute('aria-label', `All tasks: ${total}`);
  totalChip.setAttribute('aria-pressed', String(allActive));
  totalChip.innerHTML = `${renderIcon('layers', 'w-3.5 h-3.5')}<strong>${total}</strong>`;
  totalChip.onclick = () => {
    filters.statusMode.clear();
    renderAll();
  };
  statsDigest.appendChild(totalChip);

  window.STATUS_GROUPS.forEach((group, groupIndex) => {
    if (groupIndex > 0) {
      const divider = document.createElement('div');
      divider.className = 'w-px h-4 bg-zinc-800 mx-0.5 flex-shrink-0';
      statsDigest.appendChild(divider);
    }
    Object.keys(window.STATUS_THEMES).filter(code => window.STATUS_THEMES[code].group === group.code).forEach(code => {
      const theme = getTheme(code);
      const count = counts[code] || 0;
      const mode = filters.statusMode.get(code);
      const chip = document.createElement('button');
      const modeClass = mode === 'include' ? 'st-mode-include' : mode === 'exclude' ? 'st-mode-exclude' : '';
      chip.className = `st-toggle st-${code} ${modeClass} flex items-center gap-1 px-1.5 py-1 rounded-md text-xs transition hover:brightness-125 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-400 ${count === 0 && !mode ? 'opacity-50' : ''}`;
      chip.title = `${theme.label} · ${triModeLabel(mode)}`;
      chip.dataset.statusCode = code;
      chip.setAttribute('aria-label', `Status ${theme.label}: ${triModeLabel(mode)}`);
      chip.innerHTML = `${renderIcon(theme.icon, 'w-3.5 h-3.5')}<strong>${count}</strong>`;
      triStateHandlers(chip, () => filters.statusMode.get(code), (mode) => {
        if (mode === null) filters.statusMode.delete(code); else filters.statusMode.set(code, mode);
        renderAll();
      });
      statsDigest.appendChild(chip);
    });
  });

  if (focusedCode) {
    const toFocus = statsDigest.querySelector(`[data-status-code="${CSS.escape(focusedCode)}"]`);
    if (toFocus) toFocus.focus();
  }
}
```

After `const specTriState = createTriStatePopover(specFilterEl, {...});`:

```js
const phaseTriState = createTriStatePopover(phaseFilterEl, {
  label: 'Phase',
  dimension: 'Phase',
  modeMap: filters.phaseMode,
  onChange: renderAll,
  getOptions: () => {
    const counts = computeDimensionCounts('phase', t => [t.phase || NO_PHASE]);
    return Object.keys(window.PHASE_THEMES).map(code => ({
      value: code, label: window.PHASE_THEMES[code].label, count: counts[code] || 0,
    }));
  },
});
```

```js
function activeFilterCount() {
  return filters.phaseMode.size + filters.repoMode.size + filters.modelMode.size +
    filters.specMode.size + (filters.scoreMin !== null || filters.scoreMax !== null ? 1 : 0);
}
```

```js
function renderFilterControls() {
  repoTriState.render();
  modelTriState.render();
  specTriState.render();
  phaseTriState.render();
  scoreFilter.render();
  clearFiltersBtn.classList.toggle('hidden', !(structuralFilterActive() || filters.q !== ''));
  renderFiltersToggle();
}

clearFiltersBtn.addEventListener('click', () => {
  filters.statusMode.clear();
  filters.phaseMode.clear();
  filters.repoMode.clear();
  filters.modelMode.clear();
  filters.specMode.clear();
  filters.scoreMin = null;
  filters.scoreMax = null;
  filters.q = '';
  searchBox.value = '';
  renderAll();
});
```

```js
function renderLegend() {
  const statusRows = window.STATUS_GROUPS.map(group => {
    const rows = Object.values(window.STATUS_THEMES).filter(t => t.group === group.code).map(t => `
      <div class="flex items-start gap-2 py-1">
        <div class="w-36 flex-shrink-0">${statusChip(t.code)}</div>
        <p class="text-xs text-zinc-300">${esc(t.description)}</p>
      </div>
    `).join('');
    return `<div><div class="text-[10px] uppercase tracking-wider text-zinc-400 mt-2">${esc(group.label)}</div>${rows}</div>`;
  }).join('');
  const phaseRows = Object.values(window.PHASE_THEMES).map(t => `
    <div class="flex items-start gap-2 py-1">
      <div class="w-36 flex-shrink-0">${phaseChip(t.code)}</div>
      <p class="text-xs text-zinc-300">${esc(t.description)}</p>
    </div>
  `).join('');
  legendBody.innerHTML = `${statusRows}<div><div class="text-[10px] uppercase tracking-wider text-zinc-400 mt-2">Phases</div>${phaseRows}</div>`;
}
```

**3f.** `tree.js`: the four reads `node.virtual_status || node.status`, `spec.virtual_status || spec.status`, `plan.virtual_status || plan.status`, `task.virtual_status || task.status` become `displayOf(node)`, `displayOf(spec)`, `displayOf(plan)`, `displayOf(task)`.

**3g.** `graph.js`: in `renderGraph`, `const theme = getTheme(n.status);` becomes `const theme = getTheme(displayOf(n));`.

**3h.** `detail.js`.

```js
function renderDependencies(details, status, node, editable) {
  const addControl = editable && node.kind === 'task'
    ? `
      <div class="flex items-center gap-2 mt-1">
        <button type="button" class="add-dep-btn h-7 px-2 rounded-md text-[11px] font-medium text-emerald-400 hover:text-emerald-300 hover:bg-zinc-800 border border-dashed border-zinc-700 transition">+ Add dependency</button>
        <button type="button" class="add-decision-dep-btn h-7 px-2 rounded-md text-[11px] font-medium text-amber-400 hover:text-amber-300 hover:bg-zinc-800 border border-dashed border-zinc-700 transition">+ Wait on decision</button>
      </div>`
    : '';
  if (!details || details.length === 0) {
    return addControl ? `<div class="pt-2">${addControl}</div>` : '';
  }
  const unfinished = details.filter(d => !d.finished);
  const why = status === 'BLOCKED_BY_TASK' && unfinished.length > 0
    ? `<div class="st-chip st-BLOCKED_BY_TASK rounded-lg px-2.5 py-1.5 text-xs">Waits for ${unfinished.map(d => esc(d.id)).join(', ')} to land where this node builds.</div>`
    : '';
  const rows = details.map(d => `
    <div class="flex items-center gap-2 px-2 py-1.5 bg-zinc-950/60">
      ${d.status ? statusIcon(d.status) : '<span class="text-[10px] font-mono text-red-400">missing</span>'}
      <span class="font-mono text-[11px] text-zinc-300">${esc(d.id)}</span>
      <span class="truncate text-[11px] text-zinc-400 flex-1">${esc(d.title || '')}</span>
      ${editable ? `<button type="button" class="dep-remove-btn p-1 rounded text-zinc-500 hover:text-red-400 hover:bg-zinc-800 flex-shrink-0" data-dep-id="${esc(d.id)}" aria-label="Remove dependency ${esc(d.id)}">${renderIcon('x', 'w-3 h-3')}</button>` : ''}
    </div>
  `).join('');
  return `
    <div class="space-y-1.5 pt-2">
      <div class="font-semibold text-zinc-400 uppercase tracking-wider text-[10px]">Depends on (${details.length})</div>
      ${why}
      <div class="divide-y divide-zinc-800 rounded border border-zinc-800">${rows}</div>
      ${addControl}
    </div>
  `;
}
```

The action bar, replacing `renderActionBar` and `wireActionBar` and the comment above them:

```js
// Verbs, never a status picker: each button is a transition the stored status allows, and
// every one of them asks for the note it records.
const REOPENABLE = ['FAILED', 'DEFERRED', 'ABANDONED'];
const SETTABLE_ASIDE = ['READY', 'IMPLEMENTED', 'REVIEWED', 'FIXED', 'FAILED'];

function renderActionBar(node, hasLease) {
  const btnCls = 'h-7 px-2.5 rounded-md text-[11px] font-medium bg-zinc-800 hover:bg-zinc-700 text-zinc-200 border border-zinc-700 transition';
  const dangerCls = 'h-7 px-2.5 rounded-md text-[11px] font-medium bg-zinc-900 hover:bg-red-950 text-red-300 border border-red-900/60 transition';
  const buttons = [
    `<button type="button" class="ab-edit ${btnCls}">Edit</button>`,
  ];
  if (node.kind !== 'decision') {
    buttons.push(`<button type="button" class="ab-flags ${btnCls}">Flags&hellip;</button>`);
  }
  if (REOPENABLE.includes(node.status)) {
    buttons.push(`<button type="button" class="ab-reopen ${btnCls}">Reopen&hellip;</button>`);
  }
  if (!hasLease) {
    buttons.push(`<button type="button" class="ab-reset ${btnCls}">Reset&hellip;</button>`);
  }
  if (!hasLease && SETTABLE_ASIDE.includes(node.status)) {
    buttons.push(`<button type="button" class="ab-defer ${btnCls}">Defer&hellip;</button>`);
    buttons.push(`<button type="button" class="ab-abandon ${dangerCls}">Abandon&hellip;</button>`);
  }
  if (node.kind === 'task') {
    buttons.push(`<button type="button" class="ab-supersede ${dangerCls}">Supersede&hellip;</button>`);
    buttons.push(`<button type="button" class="ab-move ${btnCls}">Move to plan&hellip;</button>`);
  }
  if (hasLease) {
    buttons.push(`<button type="button" class="ab-release ${dangerCls}">Release lease</button>`);
  }
  return `<div class="ab-bar flex flex-wrap gap-1.5 pb-2 border-b border-zinc-800/80">${buttons.join('')}</div>`;
}

function wireActionBar(root, node) {
  const bar = root.querySelector('.ab-bar');
  if (!bar) return;
  const on = (selector, fn) => {
    const el = bar.querySelector(selector);
    if (el) el.addEventListener('click', fn);
  };
  on('.ab-edit', () => openEditNodeDialog(node));
  on('.ab-flags', () => openFlagsDialog(node));
  on('.ab-reopen', () => openVerbDialog(node, 'reopen'));
  on('.ab-reset', () => openResetDialog(node));
  on('.ab-defer', () => openVerbDialog(node, 'defer'));
  on('.ab-abandon', () => openVerbDialog(node, 'abandon'));
  on('.ab-supersede', () => openSupersedeDialog(node));
  on('.ab-move', () => openMoveDialog(node));
  on('.ab-release', () => releaseLease(node));
}


// Where a node lands, read from its base chain: "on tm/P; waits for P → main".
function landingChainText(node) {
  const base = (node.base_chain || []).map(id => (id === 'MAIN' ? 'main' : id));
  if (base.length === 0) return '';
  if (base[0] === 'main') return 'lands on main';
  return `on tm/${esc(base[0])}; waits for ${base.map(esc).join(' → ')}`;
}

function renderLifecycle(detail, editable) {
  const n = detail.node;
  if (n.kind === 'decision') return '';
  const row = (label, value) => `
    <div class="flex gap-2"><dt class="w-28 flex-shrink-0 text-zinc-500">${esc(label)}</dt><dd class="text-zinc-300 min-w-0 break-words">${value}</dd></div>`;
  const rows = [
    row('Stored status', `${esc(n.status)} ${phaseChip(detail.phase)}`),
    row('Outcome', esc(n.outcome || '-')),
    row('Verdict', esc(n.verdict || '-')),
    row('Flags', `review ${n.review ? 'on' : 'off'} · fix ${n.fix ? 'on' : 'off'}`),
    row('Lands', `${esc(n.merge)} · ${landingChainText(n)}`),
    row('Counters', `reviews ${n.review_cycles} · merge attempts ${n.merge_attempts} · step failures ${n.step_failures}`),
    row('Requires', esc((n.requires || []).join(', ') || '-')),
  ];
  if (n.land_order && n.land_order.length) rows.push(row('Land order', esc(n.land_order.join(' → '))));
  const conditions = (detail.conditions || []).map(c => `
    <div class="flex items-center gap-2 px-2 py-1.5 bg-zinc-950/60">
      <span class="text-[11px] text-zinc-300 flex-1 min-w-0 truncate">${esc(c.needs)}</span>
      <span class="text-[10px] uppercase text-zinc-500">${esc(c.stage)}</span>
      <code class="text-[10px] text-zinc-400 truncate max-w-[40%]">${esc(c.command)}</code>
      <span class="text-[10px] ${c.last_result === 0 ? 'text-emerald-400' : 'text-amber-400'}">${c.last_result === null || c.last_result === undefined ? 'not run' : (c.last_result === 0 ? 'holds' : `exit ${c.last_result}`)}</span>
      ${editable ? `<button type="button" class="cond-remove-btn p-1 rounded text-zinc-500 hover:text-red-400 hover:bg-zinc-800" data-idx="${c.idx}" data-needs="${esc(c.needs)}" aria-label="Remove condition ${esc(c.needs)}">${renderIcon('x', 'w-3 h-3')}</button>` : ''}
    </div>`).join('');
  const jobs = (detail.jobs || []).map(j => `
    <div class="px-2 py-1.5 bg-zinc-950/60 text-[11px] text-zinc-300">${esc(j.kind)} ${esc(j.repo || '')} → ${esc(j.target || '')}: <strong>${esc(j.state)}</strong>${j.step ? ` at ${esc(j.step)}` : ''}</div>`).join('');
  return `
    <div class="lc-panel space-y-2 pt-2 text-xs">
      <dl class="space-y-1">${rows.join('')}</dl>
      <div class="font-semibold text-zinc-400 uppercase tracking-wider text-[10px]">Conditions (${(detail.conditions || []).length})</div>
      ${conditions ? `<div class="divide-y divide-zinc-800 rounded border border-zinc-800">${conditions}</div>` : ''}
      ${editable ? '<button type="button" class="cond-add-btn h-7 px-2 rounded-md text-[11px] font-medium text-emerald-400 hover:text-emerald-300 hover:bg-zinc-800 border border-dashed border-zinc-700 transition">+ Add condition</button>' : ''}
      ${jobs ? `<div class="font-semibold text-zinc-400 uppercase tracking-wider text-[10px]">Jobs</div><div class="divide-y divide-zinc-800 rounded border border-zinc-800">${jobs}</div>` : ''}
    </div>
  `;
}

function wireLifecycleControls(root, node) {
  root.querySelectorAll('.cond-remove-btn').forEach(btn => {
    btn.addEventListener('click', () => removeCondition(node, Number(btn.dataset.idx), btn.dataset.needs));
  });
  const add = root.querySelector('.cond-add-btn');
  if (add) add.addEventListener('click', () => openAddConditionDialog(node));
}
```

`showGraphInspector`:

```js
async function showGraphInspector(nodeId) {
  selectedNodeId = nodeId;
  graphInspector.classList.remove('hidden');

  let detail = null;
  if (isStaticMode) {
    detail = (window.STATIC_DATA.details || {})[nodeId];
  } else {
    try {
      const res = await fetch(`/api/nodes/${nodeId}`);
      if (res.ok) detail = await res.json();
    } catch (e) {
      console.error('Failed to load node detail:', e);
    }
  }

  if (!detail) return;

  const n = detail.node;
  const status = detail.display || n.status;
  const editable = canEdit();

  document.getElementById('inspector-kind').textContent = n.kind;
  document.getElementById('inspector-id').textContent = n.id;
  document.getElementById('inspector-title').textContent = n.title;

  const body = document.getElementById('inspector-body');
  let leaseBanner = '';
  if (detail.lease) {
    leaseBanner = `
      <div class="p-2.5 bg-blue-950/40 border border-blue-800/80 rounded-lg text-xs space-y-1">
        <div class="text-blue-300 font-semibold flex items-center gap-1.5">
          ${renderIcon('bot', 'w-3.5 h-3.5')}
          <span>Live lease: ${esc(detail.lease.action || 'step')}</span>
        </div>
        <div class="text-zinc-400 font-mono">Agent: ${esc(detail.lease.agent_id)}</div>
        <div class="text-zinc-400 font-mono text-[11px]">${esc(detail.lease.branch_name)}</div>
      </div>
    `;
  }

  const attachments = (n.frontmatter && n.frontmatter.attachments) || [];

  body.innerHTML = `
    ${editable ? renderActionBar(n, !!detail.lease) : ''}
    <div class="flex items-center gap-2">
      ${statusIcon(status, 'w-4 h-4')}
      <span class="px-2 py-0.5 rounded bg-zinc-950 border border-zinc-800 font-mono text-zinc-400 text-xs">Prio: ${n.priority || 50}</span>
    </div>
    ${leaseBanner}
    ${renderLifecycle(detail, editable)}
    ${renderVerifications(n, detail.verifications, editable)}
    ${renderDependencies(detail.dependency_details, status, n, editable)}
    ${renderAttachments(n, attachments, editable)}
    ${renderSections(detail.sections, n.id)}
  `;
  attachSectionToggleHandlers(body);
  attachInspectorGroupToggleHandlers(body, nodeId);
  wireAttachmentControls(body, n, attachments, editable, () => showGraphInspector(nodeId));
  if (editable) {
    wireActionBar(body, n);
    wireLifecycleControls(body, n);
    wireVerificationControls(body, n);
    wireDependencyControls(body, n);
    attachSectionEditControls(body, n, detail.sections);
  }
}
```

In `decorateAwaitingDecisionBanners`, `(task.virtual_status || task.status) === 'AWAITING_DECISION'` becomes `displayOf(task) === 'AWAITING_DECISION'`.

**3i.** `edit.js`. Delete the `REAL_NODE_STATUSES` comment and constant, and the whole `// Status ---` block (`setNodeStatus`, `changeStatus`, `openOtherStatusDialog`). In its place:

```js
// Verbs -------------------------------------------------------------------------------------

async function postVerb(node, verb, body) {
  const res = await api('POST', `/api/nodes/${node.id}/${verb}`, body);
  toast(`${node.id} is now ${res.status}.`, 'success');
  await afterWrite(node.id);
}

const VERB_COPY = {
  reopen: { title: 'Reopen', label: 'What should the next attempt do differently?' },
  defer: { title: 'Defer', label: 'Why, and until when?' },
  abandon: { title: 'Abandon', label: 'Why is it dropped for good?' },
};

function openVerbDialog(node, verb) {
  const copy = VERB_COPY[verb];
  openDialog({
    title: `${copy.title} ${node.id}`,
    submitLabel: copy.title,
    destructive: verb === 'abandon',
    bodyHtml: `
      ${fieldRow(copy.label, `<textarea required class="vb-note ${TEXTAREA_CLS}" rows="3"></textarea>`)}
      ${verb === 'reopen' ? `<label class="flex items-center gap-2 text-xs text-zinc-300"><input type="checkbox" class="vb-new-branch rounded border-zinc-600 bg-zinc-950">Start on a new branch; the old one is kept as ${esc(node.branch || `tm/${node.id}`)}@n</label>` : ''}
    `,
    onSubmit: async (panel, close) => {
      const note = panel.querySelector('.vb-note').value.trim();
      if (!note) throw new Error('A note is required.');
      const body = { note };
      const newBranch = panel.querySelector('.vb-new-branch');
      if (newBranch) body.new_branch = newBranch.checked;
      await postVerb(node, verb, body);
      close();
    }
  });
}

const RESET_TARGETS = ['READY', 'IMPLEMENTED', 'REVIEWED', 'FIXED', 'COMPLETED'];
const OUTCOMES = ['approve', 'reject', 'merge_failed'];

function openResetDialog(node) {
  openDialog({
    title: `Reset ${node.id}`,
    submitLabel: 'Reset',
    bodyHtml: `
      ${fieldRow('To', `<select class="rs-to ${SELECT_CLS}">${RESET_TARGETS.map(s => `<option value="${s}">${esc(s)}</option>`).join('')}</select>`)}
      ${fieldRow('Outcome (for REVIEWED)', `<select class="rs-outcome ${SELECT_CLS}"><option value="">(none)</option>${OUTCOMES.map(o => `<option value="${o}">${esc(o)}</option>`).join('')}</select>`)}
      ${fieldRow('Why the stored state was wrong', `<textarea required class="rs-note ${TEXTAREA_CLS}" rows="3"></textarea>`)}
    `,
    onSubmit: async (panel, close) => {
      const note = panel.querySelector('.rs-note').value.trim();
      if (!note) throw new Error('A note is required.');
      const body = { to: panel.querySelector('.rs-to').value, note };
      const outcome = panel.querySelector('.rs-outcome').value;
      if (outcome) body.outcome = outcome;
      await postVerb(node, 'reset', body);
      close();
    }
  });
}

// Flags, merge, requires and land_order go through the same write rules as the CLI; a
// refusal stays in the dialog to correct.
function openFlagsDialog(node) {
  const isContainer = node.kind === 'plan' || node.kind === 'spec';
  const checkbox = (cls, checked, label) => `<label class="flex items-center gap-2 text-xs text-zinc-300"><input type="checkbox" class="${cls} rounded border-zinc-600 bg-zinc-950 text-emerald-500 focus:ring-emerald-500" ${checked ? 'checked' : ''}>${esc(label)}</label>`;
  openDialog({
    title: `Flags of ${node.id}`,
    submitLabel: 'Save',
    bodyHtml: `
      ${checkbox('fl-review', node.review, 'A review step follows implement')}
      ${checkbox('fl-fix', node.fix, 'This node fixes what its review rejects')}
      ${fieldRow('Lands on', `<select class="fl-merge ${SELECT_CLS}"><option value="main" ${node.merge === 'main' ? 'selected' : ''}>main</option><option value="parent" ${node.merge === 'parent' ? 'selected' : ''}>the parent's branch</option></select>`)}
      ${fieldRow('Requires (comma separated capabilities)', `<input type="text" class="fl-requires ${INPUT_CLS}" value="${esc((node.requires || []).join(', '))}">`)}
      ${isContainer ? fieldRow('Land order (comma separated repositories)', `<input type="text" class="fl-land-order ${INPUT_CLS}" value="${esc((node.land_order || []).join(', '))}">`) : ''}
    `,
    onSubmit: async (panel, close) => {
      const list = (selector) => panel.querySelector(selector).value.split(',').map(s => s.trim()).filter(Boolean);
      const body = {
        review: panel.querySelector('.fl-review').checked,
        fix: panel.querySelector('.fl-fix').checked,
        merge: panel.querySelector('.fl-merge').value,
        requires: list('.fl-requires'),
      };
      if (isContainer) body.land_order = list('.fl-land-order');
      await api('PATCH', `/api/nodes/${node.id}`, body);
      toast(`${node.id} flags saved.`, 'success');
      close();
      await afterWrite(node.id);
    }
  });
}


// Conditions ---------------------------------------------------------------------------------

function openAddConditionDialog(node) {
  openDialog({
    title: `Add a condition to ${node.id}`,
    submitLabel: 'Add',
    bodyHtml: `
      ${fieldRow('Waits for (a state outside the corpus)', `<input type="text" required class="cd-needs ${INPUT_CLS}" placeholder="staging is up">`)}
      ${fieldRow('Command that exits 0 once it holds', `<input type="text" required class="cd-command ${INPUT_CLS} font-mono" placeholder="curl -fsS https://staging.example/health">`)}
      ${fieldRow('Holds', `<select class="cd-stage ${SELECT_CLS}"><option value="claim">every claim</option><option value="landing">only the landing</option></select>`)}
    `,
    onSubmit: async (panel, close) => {
      const needs = panel.querySelector('.cd-needs').value.trim();
      const command = panel.querySelector('.cd-command').value.trim();
      if (!needs || !command) throw new Error('Name what it waits for and the command that checks it.');
      await api('POST', `/api/nodes/${node.id}/conditions`, { needs, command, stage: panel.querySelector('.cd-stage').value });
      toast(`Condition added to ${node.id}.`, 'success');
      close();
      await afterWrite(node.id);
    }
  });
}

function removeCondition(node, idx, needs) {
  confirmDialog({
    title: `Remove the condition "${needs}"?`,
    message: `${node.id} will no longer wait for it.`,
    confirmLabel: 'Remove',
    onConfirm: async () => {
      await api('DELETE', `/api/nodes/${node.id}/conditions/${idx}`);
      toast(`Condition removed from ${node.id}.`, 'success');
      await afterWrite(node.id);
    }
  });
}
```

In `openNewPlanDialog`, the `Require review` checkbox line becomes two checkboxes, and the POST body sends the flags:

```js
      <label class="flex items-center gap-2 text-xs text-zinc-300"><input type="checkbox" class="np-review rounded border-zinc-600 bg-zinc-950 text-emerald-500 focus:ring-emerald-500">A review step follows its tasks</label>
      <label class="flex items-center gap-2 text-xs text-zinc-300"><input type="checkbox" class="np-fix rounded border-zinc-600 bg-zinc-950 text-emerald-500 focus:ring-emerald-500">The plan fixes what its review rejects</label>
```

```js
      const res = await api('POST', '/api/plans', {
        title,
        spec: panel.querySelector('.np-spec').value,
        slug: panel.querySelector('.np-slug').value.trim() || undefined,
        priority: Number(panel.querySelector('.np-priority').value) || 50,
        order: Number(panel.querySelector('.np-order').value) || 0,
        review: panel.querySelector('.np-review').checked,
        fix: panel.querySelector('.np-fix').checked,
      });
```

`openAddDependencyDialog` loses its gate: the `${decisionsOnly ? '' : fieldRow('Gate status', ...)}` line is deleted, and in `onSubmit` the three lines from `const gateEl = ...` to the POST become

```js
      await api('POST', `/api/nodes/${node.id}/dependencies`, { add: [{ id }] });
```

`releaseLease`'s confirm message becomes `'The step is given back: the node returns to the status it was claimed from and counts one step failure.'`.

**3j.** `decisions.js`.

```js
const DECISION_TABS = [
  { key: 'open', label: 'Open', status: 'OPEN' },
  { key: 'answered', label: 'Answered', status: 'ANSWERED' },
  { key: 'withdrawn', label: 'Withdrawn', status: 'WITHDRAWN' },
];
```

```js
// A decision dependency row (blockers/dependencies/dependents) reads its own status, not a
// display theme: none of the display themes means Open, Answered or Withdrawn.
const DECISION_STATUS_ICON = { OPEN: 'help-circle', ANSWERED: 'check-circle-2', WITHDRAWN: 'x-circle' };
```

In `updateDecisionsBadge`, `d.status === 'NOT_STARTED'` becomes `d.status === 'OPEN'`.

`optionCardHtml` shows an option's effect beside its label:

```js
function optionCardHtml(opt, isChosen, selectable) {
  const base = 'w-full text-left p-3 rounded-lg border transition space-y-1';
  const cls = isChosen
    ? `${base} bg-emerald-950/40 border-emerald-600`
    : `${base} bg-zinc-900/60 border-zinc-800 ${selectable ? 'hover:border-zinc-600 cursor-pointer' : ''}`;
  const tag = selectable ? 'button' : 'div';
  // At most one option is ever chosen at a time, so a selectable card is a radio, not a
  // plain toggle button -- a screen reader otherwise never announces which one is selected.
  const roleAttrs = selectable ? `type="button" role="radio" aria-checked="${isChosen}"` : '';
  const effect = opt.effect && opt.effect !== 'none'
    ? `<span class="px-1.5 py-0.5 rounded-full bg-amber-950/60 text-amber-300 border border-amber-800/60 text-[10px] font-medium">Then: ${esc(opt.effect.replace('_', ' '))} the blocked nodes</span>`
    : '';
  return `
    <${tag} ${roleAttrs} class="dec-option-card ${cls}" data-option-key="${esc(opt.key)}">
      <div class="flex items-center gap-2">
        <span class="text-sm font-medium text-zinc-100">${esc(opt.label)}</span>
        ${opt.recommended ? '<span class="px-1.5 py-0.5 rounded-full bg-emerald-950/60 text-emerald-300 border border-emerald-800/60 text-[10px] font-medium">Recommended</span>' : ''}
        ${effect}
        ${isChosen ? `<span class="ml-auto">${renderIcon('check-circle-2', 'w-4 h-4 text-emerald-400')}</span>` : ''}
      </div>
      ${opt.description ? `<div class="prose prose-invert prose-sm max-w-none text-xs text-zinc-400">${renderSectionBody(opt.description)}</div>` : ''}
    </${tag}>
  `;
}
```

In `renderDecisionDetail`: `node.status === 'NOT_STARTED'` becomes `node.status === 'OPEN'`; `node.status === 'COMPLETED'` becomes `node.status === 'ANSWERED'`; both `node.status === 'ABANDONED'` become `node.status === 'WITHDRAWN'`; `${statusIcon(detail.virtual_status || node.status, 'w-4 h-4')}` becomes `${decisionStatusIcon(node.status, 'w-4 h-4')}`. The "Waiting on this" chips already read each blocked node's display through `t.status`, which is how a FAILED or stranded decision links its nodes.

**3k.** Old tests, rewritten or deleted in this task (`tests/unit/test_web_ui.py` unless named):
- `test_destructive_actions_confirm_before_writing`: the tuple gains `"removeCondition"`; the two lines reading `_function_body(html, "changeStatus")` and asserting on it are replaced by `assert "destructive: verb === 'abandon'" in _function_body(html, "openVerbDialog")`.
- Delete `test_status_digest_and_legend_never_render_not_started`, `test_action_bar_hides_a_transition_already_at_its_own_target` and `test_other_status_and_gate_pickers_omit_not_started`; `test_the_page_names_no_pre_lifecycle_status_or_status_setter` and `test_action_bar_offers_verbs_by_stored_status` replace them.
- `test_decision_dependency_row_uses_open_answered_withdrawn_not_node_status`: `"NOT_STARTED: 'help-circle'" in html` becomes `"OPEN: 'help-circle'" in html`.
- `test_page_inlines_every_static_js_file` and `test_static_files_are_packaged_and_resolve_at_runtime` are unchanged.
- `tests/integration/test_web.py::test_static_export_embeds_every_status_and_the_filter_ui`: the element-id tuple gains `"phase-filter"`.

- [ ] **Step 4: Run the tests and the gates**

```
uv run --directory <worktree> pytest tests/unit/test_web_enums.py tests/unit/test_web_ui.py tests/integration/test_web.py tests/unit/test_static_export.py -q; echo $?
uv run --directory <worktree> pytest -q; echo $?
uv run --directory <worktree> ruff check; echo $?
uv run --directory <worktree> ruff format; echo $?
uv run --directory <worktree> ruff format --check; echo $?
uv run --directory <worktree> mypy; echo $?
```

Expected: every command prints `0` (the `ruff format` run rewrites only files this task touched); `test_page_scripts_parse` runs `node --check` over the whole page, so a JS syntax slip fails the suite.

- [ ] **Step 5: Commit**

```
git -C <worktree> add src/taskmanager/web/enums.py src/taskmanager/web/__init__.py src/taskmanager/web/ui.py src/taskmanager/web/static/index.html src/taskmanager/web/static/js/core.js src/taskmanager/web/static/js/filters.js src/taskmanager/web/static/js/tree.js src/taskmanager/web/static/js/graph.js src/taskmanager/web/static/js/detail.js src/taskmanager/web/static/js/edit.js src/taskmanager/web/static/js/decisions.js tests/unit/test_web_enums.py tests/unit/test_web_ui.py tests/integration/test_web.py && git -C <worktree> commit -m "feat(web): show display and phase, filter by both, and edit through verbs and flags"
```

### Task 21: Delete the old vocabulary and add the status CHECK

**Spec:** §2.2 (the kind-aware `CHECK`), §5.3 (no path sets an arbitrary status), §7.2 (last paragraph), §11 (Storage: "the kind-aware `CHECK` refusing an old status on write")
**Files:**
- Delete: `src/taskmanager/engine/graph.py`, `src/taskmanager/engine/runtime.py`, `src/taskmanager/engine/wave.py`
- Delete: `tests/unit/test_graph_engine.py`, `tests/unit/test_runtime_engine.py`, `tests/unit/test_wave.py`
- Modify: `src/taskmanager/core/enums.py` (delete `NodeStatus`, `VirtualStatus`, `NodeKind.REVIEW_GATE`)
- Modify: `src/taskmanager/core/models.py` (`Node.status` typed `Status | DecisionStatus`, default `READY`, new validator `_status_fits_kind`)
- Modify: `src/taskmanager/db/schema.py` (`NODE_STATUS_CHECK`; the `nodes` statement gains it and `DEFAULT 'READY'`)
- Modify: `src/taskmanager/db/connection.py` (delete the `get_spec_connection` and `get_runtime_connection` aliases)
- Modify: `src/taskmanager/db/node_repo.py` (`list_nodes`, `_row_to_node`; delete `_status` and `get_dependency_edges`; `get_spec_connection` → `get_state_connection`)
- Modify: `src/taskmanager/engine/operations.py` (`Operations.__init__`, `with_actor`; delete `_SWEEP_BACK`, `set_status`, `release_lease`, `sweep_leases`; `_refuse_unlinkable` loses the `graph.would_cause_cycle` check; import block)
- Modify: `src/taskmanager/engine/heuristics.py` (`_score_task`, `RecommendationEngine`)
- Modify: `src/taskmanager/engine/snapshot.py` (delete `_LEGACY`, `_LEGACY_DECISION`; `stored_status`, `cycle_of` read the stored status as it is)
- Modify: `src/taskmanager/engine/search.py` (status type hints; `get_spec_connection` → `get_state_connection`)
- Modify: `src/taskmanager/engine/decisions.py` (`DECISION_STATUS_LABELS`)
- Modify: `src/taskmanager/renderers/markdown.py` (the withdrawn-decision branch; `get_spec_connection` → `get_state_connection`)
- Modify: `src/taskmanager/di/container.py` (delete `graph_engine`, `coordinator` providers and aliases; `heuristics`, `operations` providers)
- Modify: `src/taskmanager/web/static/js/graph.js` (`GRAPH_SHAPE_BY_KIND` loses `review_gate`)
- Create: `tests/unit/test_old_vocabulary.py`, `tests/unit/test_status_check.py`
- Modify: `tests/unit/test_operations.py`, `tests/unit/test_operations_lifecycle.py`, `tests/unit/test_heuristics.py`, `tests/unit/test_estate_workflow.py`, `tests/unit/test_models.py`, `tests/unit/test_repos.py`, `tests/unit/test_search.py`, `tests/unit/test_search_engine.py`, `tests/unit/test_renderers.py`, `tests/unit/test_db_schema.py` (Step 3c)

**Interfaces:**
- Consumes: `Status`, `DecisionStatus`, `DisplayStatus` (Task 1); `SnapshotBuilder`, `stored_status`, `cycle_of` (Task 10); `Operations(..., job_repo=...)` (Task 12); `DisplayView` (Task 18); every earlier task's removal of its old callers
- Produces: `Node.status: Status | DecisionStatus` (kind-checked in the model and in SQLite); `Operations(node_repo, runtime_repo, ledger_repo, verification_engine, job_repo, actor="cli")`; `RecommendationEngine(node_repo, runtime_repo, snapshots)`; a package in which no module names `NodeStatus`, `VirtualStatus`, `GraphEngine`, `ExecutionCoordinator`, `discover_batch`, `REVIEW_GATE`, `get_spec_connection`, `get_runtime_connection` or `get_dependency_edges`

- [ ] **Step 1: Write the failing tests**

`tests/unit/test_old_vocabulary.py`:

```python
"""Nothing in the package names the pre-lifecycle vocabulary once the switch is done."""

import re
from pathlib import Path

import taskmanager

FORBIDDEN = re.compile(
    r"\b("
    + "|".join(
        [
            "NodeStatus",
            "VirtualStatus",
            "NOT_STARTED",
            "WAITING_FIXES",
            "IN_FLIGHT",
            "GraphEngine",
            "ExecutionCoordinator",
            "discover_batch",
            "gate_satisfied",
            "REVIEW_GATE",
            "review_gate",
            "virtual_status",
            "get_spec_connection",
            "get_runtime_connection",
            "get_dependency_edges",
            "_LEGACY",
            "_LEGACY_DECISION",
        ]
    )
    + r")\b"
)


def test_the_package_names_no_pre_lifecycle_vocabulary() -> None:
    root = Path(taskmanager.__file__).parent
    hits = [
        f"{path.relative_to(root)}:{number}: {match.group(1)}"
        for path in sorted(root.rglob("*"))
        if path.suffix in {".py", ".js", ".html", ".css"}
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        for match in FORBIDDEN.finditer(line)
    ]
    assert hits == []


def test_the_old_engine_modules_are_gone() -> None:
    root = Path(taskmanager.__file__).parent / "engine"
    assert [name for name in ("graph.py", "runtime.py", "wave.py") if (root / name).exists()] == []
```

`tests/unit/test_status_check.py`:

```python
"""A stored status the node's kind cannot hold is refused by SQLite and by the model."""

import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from taskmanager.core.enums import NodeKind
from taskmanager.core.models import Node
from taskmanager.core.status import DecisionStatus, Status
from taskmanager.db.connection import DatabaseManager


@pytest.fixture
def db(tmp_path: Path) -> DatabaseManager:
    manager = DatabaseManager(tmp_path / ".taskmanager")
    manager.init_all()
    return manager


def insert(db: DatabaseManager, kind: str, status: str) -> None:
    with db.get_state_connection() as conn:
        conn.execute(
            "INSERT INTO nodes (id, kind, title, status) VALUES (?, ?, ?, ?)",
            (f"{kind}-{status}", kind, "x", status),
        )
        conn.commit()


@pytest.mark.parametrize(
    ("kind", "status"),
    [
        ("task", "NOT_STARTED"),
        ("task", "WAITING_REVIEW"),
        ("plan", "IN_FLIGHT"),
        ("spec", "BLOCKED"),
        ("task", "OPEN"),
        ("decision", "READY"),
        ("decision", "NOT_STARTED"),
        ("decision", "COMPLETED"),
    ],
)
def test_the_status_check_refuses_a_status_the_kind_cannot_hold(
    db: DatabaseManager, kind: str, status: str
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        insert(db, kind, status)


@pytest.mark.parametrize(
    ("kind", "status"),
    [
        ("task", "READY"),
        ("plan", "IMPLEMENTED"),
        ("spec", "COMPLETED"),
        ("task", "SUPERSEDED"),
        ("decision", "OPEN"),
        ("decision", "WITHDRAWN"),
    ],
)
def test_the_status_check_accepts_the_statuses_of_the_kind(
    db: DatabaseManager, kind: str, status: str
) -> None:
    insert(db, kind, status)


def test_a_node_defaults_to_ready_and_a_decision_to_open() -> None:
    assert Node(id="T", kind=NodeKind.TASK, title="t").status == Status.READY
    assert Node(id="D", kind=NodeKind.DECISION, title="d").status == DecisionStatus.OPEN


@pytest.mark.parametrize(
    ("kind", "status"),
    [
        (NodeKind.TASK, DecisionStatus.OPEN),
        (NodeKind.DECISION, Status.READY),
        (NodeKind.PLAN, "NOT_STARTED"),
    ],
)
def test_the_model_refuses_a_status_its_kind_cannot_hold(kind: NodeKind, status: object) -> None:
    with pytest.raises(ValidationError):
        Node(id="X", kind=kind, title="x", status=status)  # type: ignore[arg-type]
```

- [ ] **Step 2: Run them and watch them fail**

```
uv run --directory <worktree> pytest tests/unit/test_old_vocabulary.py tests/unit/test_status_check.py -q; echo $?
```

Expected: exit 1. The vocabulary test lists every `NodeStatus`, `GraphEngine`, `ExecutionCoordinator`, `get_spec_connection` and `review_gate` hit still in the package, and the three old engine modules; the CHECK tests fail with `DID NOT RAISE` because `nodes.status` accepts any text; the model tests fail on `NOT_STARTED` being the default and a task accepting `DecisionStatus.OPEN`.

- [ ] **Step 3: Implement**

**3a.** Source changes.

`src/taskmanager/core/enums.py`: delete `class NodeStatus` and `class VirtualStatus` whole, and the `REVIEW_GATE = "review_gate"` member of `NodeKind`. `LedgerCommand.PLAN_REVIEW_GATE` stays: written ledgers carry that string.

`src/taskmanager/core/models.py`: the enums import drops `NodeStatus`; add `from typing import Self`, `from pydantic import model_validator` and `from taskmanager.core.status import DecisionStatus, Status` (merged into the existing imports). In `Node`, the `status` field and a validator after the last field:

```python
    status: Status | DecisionStatus = Status.READY
```

```python
    @model_validator(mode="after")
    def _status_fits_kind(self) -> Self:
        is_decision = self.kind == NodeKind.DECISION
        if is_decision and "status" not in self.model_fields_set:
            self.status = DecisionStatus.OPEN
        if isinstance(self.status, DecisionStatus) != is_decision:
            raise ValueError(f"a {self.kind.value} cannot hold status {self.status.value}")
        return self
```

`src/taskmanager/db/schema.py`: add, above the state schema constant (import `DecisionStatus`, `Status` from `taskmanager.core.status`):

```python
# Built from the enums so the vocabulary SQLite enforces and the one the code writes cannot drift.
_CYCLE_STATUSES = ", ".join(f"'{s.value}'" for s in Status)
_DECISION_STATUSES = ", ".join(f"'{s.value}'" for s in DecisionStatus)
NODE_STATUS_CHECK = (
    f"CHECK ((kind = 'decision' AND status IN ({_DECISION_STATUSES})) "
    f"OR (kind <> 'decision' AND status IN ({_CYCLE_STATUSES})))"
)
```

In the `CREATE TABLE IF NOT EXISTS nodes (...)` statement of that constant, the `status` column becomes `status TEXT NOT NULL DEFAULT 'READY',` and the statement's last table constraint (after the `CHECK (fix <= review)` Task 8 wrote) is `NODE_STATUS_CHECK`; the constant is assembled by concatenation so the SQL text stays one literal plus this interpolation:

```python
    ...
    CHECK (fix <= review),
    """
    + NODE_STATUS_CHECK
    + """
);
```

No stored estate predates this schema (the fresh-start cutover creates `state.db` with it), so `PRAGMA user_version` is not bumped.

`src/taskmanager/db/connection.py`: delete the `get_spec_connection` and `get_runtime_connection` aliases; every caller now says `get_state_connection` (the runtime repository, the snapshot builder and any other reader Tasks 8–16 left on the old names included).

`src/taskmanager/db/node_repo.py`: every `self.db.get_spec_connection()` becomes `self.db.get_state_connection()`; the enums import drops `NodeStatus` and `from taskmanager.core.status import DecisionStatus, Status` is added; `get_dependency_edges` is deleted; `list_nodes`'s signature becomes

```python
    def list_nodes(
        self, kind: NodeKind | None = None, status: Status | DecisionStatus | None = None
    ) -> list[Node]:
```

and in `_row_to_node` the status argument passes the stored text through, so `Node` parses it by kind: `status=row[3],` (the index of `status` in the row Task 8's query selects); Task 8's `_status` helper, which parsed three vocabularies, is deleted.

`src/taskmanager/engine/operations.py`: the import block loses `NodeStatus`, `GraphEngine`, `ExecutionCoordinator`; `_SWEEP_BACK` and the `set_status`, `release_lease`, `sweep_leases` methods are deleted (claims, releases and sweeps are `Claims`'); the constructor and `with_actor` become (a `job_repo` is now required, so every `Operations` counts a running job as busy)

```python
def __init__(
    self,
    node_repo: NodeRepository,
    runtime_repo: RuntimeRepository,
    ledger_repo: LedgerRepository,
    verification_engine: VerificationEngine,
    job_repo: JobRepository,
    actor: str = "cli",
) -> None:
    self.node_repo = node_repo
    self.runtime_repo = runtime_repo
    self.ledger_repo = ledger_repo
    self.verification_engine = verification_engine
    self.job_repo = job_repo
    self.actor = actor
    self.snapshots = SnapshotBuilder(node_repo, runtime_repo, job_repo)


def with_actor(self, actor: str) -> Operations:
    return Operations(
        self.node_repo,
        self.runtime_repo,
        self.ledger_repo,
        self.verification_engine,
        self.job_repo,
        actor=actor,
    )
```

In Task 12's `_refuse_unlinkable`, the two lines

```python
        if self.graph.would_cause_cycle(node_id, decision_id):
            raise OperationError(f"'{node_id}' -> '{decision_id}' would make a cycle", 409)
```

are deleted: a decision has no out-edges, so an edge onto one cannot close a cycle.

`src/taskmanager/engine/heuristics.py`: the imports become

```python
from taskmanager.core.enums import NodeKind, RecommendationStrategy
from taskmanager.core.status import DisplayStatus, Status
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.engine.snapshot import DisplayView, SnapshotBuilder
```

in `_score_task`, `node.status in (NodeStatus.COMPLETED, NodeStatus.SUPERSEDED)` becomes `node.status in (Status.COMPLETED, Status.SUPERSEDED)`; `RecommendationEngine` is replaced by:

```python
class RecommendationEngine:
    def __init__(
        self,
        node_repo: NodeRepository,
        runtime_repo: RuntimeRepository,
        snapshots: SnapshotBuilder,
    ) -> None:
        self.node_repo = node_repo
        self.runtime_repo = runtime_repo
        self.snapshots = snapshots

    def get_next_tasks(
        self,
        plan_id: str | None = None,
        spec_id: str | None = None,
        model_filter: str | None = None,
        strategy: RecommendationStrategy | str = RecommendationStrategy.BALANCED,
        limit: int = 5,
    ) -> list[ScoredTask]:
        weights = _STRATEGY_WEIGHTS[_resolve_strategy(strategy)]

        view = DisplayView(self.snapshots)
        all_tasks = self.node_repo.list_nodes(kind=NodeKind.TASK)
        plans = self.node_repo.list_nodes(kind=NodeKind.PLAN)
        plan_children = {p.id: set(self.node_repo.get_children(p.id)) for p in plans}
        # "none" reads as the sentinel for "no spec", so a task with no plan (parent_plan_id
        # None) matches it the same way a plan with no spec ancestor does: .get(None) is None.
        wanted_spec = None if spec_id in (None, "none") else spec_id
        plan_spec = (
            {p.id: self.node_repo.get_ancestor_of_kind(p.id, NodeKind.SPEC) for p in plans}
            if spec_id is not None
            else {}
        )

        scored: list[ScoredTask] = []

        for task in all_tasks:
            if view.display(task) != DisplayStatus.READY.value:
                continue

            if (
                model_filter
                and task.acceptable_models
                and model_filter not in task.acceptable_models
            ):
                continue

            # A file-lock conflict already reads as BLOCKED_BY_LEASE, not READY, so the check
            # above already excludes it; declared_files is still needed below, for the batch's
            # own same-file collision guard.
            declared_files = self.node_repo.declared_files(task.id)

            parent_plan_id: str | None = None
            for p in plans:
                if task.id in plan_children[p.id]:
                    parent_plan_id = p.id
                    break

            if plan_id is not None and parent_plan_id != plan_id:
                continue

            if spec_id is not None and plan_spec.get(parent_plan_id) != wanted_spec:
                continue

            total_score, unblocking_count = _score_task(
                task.id, task.priority, parent_plan_id, plan_children, self.node_repo, weights
            )

            scored.append(
                ScoredTask(
                    task_id=task.id,
                    title=task.title,
                    plan_id=parent_plan_id,
                    score=total_score,
                    priority=task.priority,
                    acceptable_models=task.acceptable_models,
                    unblocking_count=unblocking_count,
                    declared_files=declared_files,
                )
            )

        scored.sort(key=lambda x: x.score, reverse=True)
        # A batch is started together, so no two of its tasks may claim one file.
        chosen: list[ScoredTask] = []
        taken: set[str] = set()
        for candidate in scored:
            if taken.intersection(candidate.declared_files):
                continue
            chosen.append(candidate)
            taken.update(candidate.declared_files)
            if len(chosen) >= limit:
                break
        return chosen
```

`src/taskmanager/engine/snapshot.py`: `_LEGACY` and `_LEGACY_DECISION` are deleted with the comment above them, and the two readers become:

```python
def stored_status(node: Node) -> Status | DecisionStatus:
    return node.status


def cycle_of(node: Node) -> Cycle:
    if not isinstance(node.status, Status):
        raise ValueError(f"decision {node.id!r} has no cycle")
    return Cycle(
        status=node.status,
        container=node.kind in CONTAINERS,
        review=node.review,
        fix=node.fix,
        outcome=node.outcome,
        fix_for=node.fix_for,
        claimed_from=node.claimed_from,
        review_cycles=node.review_cycles,
        merge_attempts=node.merge_attempts,
        step_failures=node.step_failures,
    )
```

`src/taskmanager/engine/search.py`: the enums import drops `NodeStatus` and `from taskmanager.core.status import DecisionStatus, Status` is added; the five annotations `NodeStatus | str | None` become `Status | DecisionStatus | str | None`, and `list[NodeStatus | str] | None` becomes `list[Status | DecisionStatus | str] | None`; every `get_spec_connection()` becomes `get_state_connection()`.

`src/taskmanager/engine/decisions.py`: the three old-name keys Task 17 kept go:

```python
# How a decision's status reads everywhere a human sees it: the CLI table, `tm decision get`,
# the JSON and YAML rows.
DECISION_STATUS_LABELS: dict[DecisionStatus, str] = {
    DecisionStatus.OPEN: "Open",
    DecisionStatus.ANSWERED: "Answered",
    DecisionStatus.WITHDRAWN: "Withdrawn",
}
```

(with `from taskmanager.core.status import DecisionStatus` replacing the `NodeStatus` import).

`src/taskmanager/renderers/markdown.py`: the enums import drops `NodeStatus` (the withdrawn branch reads `stored_status` since Task 17), and `get_spec_connection()` in `_get_parent_ids` becomes `get_state_connection()`.

`src/taskmanager/di/container.py`: delete the imports of `GraphEngine` and `ExecutionCoordinator`, the `graph_engine` and `coordinator` providers and the `get_graph_engine` and `get_coordinator` aliases; add `from taskmanager.engine.snapshot import SnapshotBuilder` if the file does not import it yet; `heuristics` and `operations` become:

```python
    @provide(scope=Scope.APP)
    def heuristics(
        self,
        node_repo: NodeRepository,
        runtime_repo: RuntimeRepository,
        snapshots: SnapshotBuilder,
    ) -> RecommendationEngine:
        return RecommendationEngine(node_repo, runtime_repo, snapshots)
```

```python
    @provide(scope=Scope.APP)
    def operations(
        self,
        node_repo: NodeRepository,
        runtime_repo: RuntimeRepository,
        ledger_repo: LedgerRepository,
        verification_engine: VerificationEngine,
        job_repo: JobRepository,
    ) -> Operations:
        return Operations(node_repo, runtime_repo, ledger_repo, verification_engine, job_repo)
```

`src/taskmanager/web/static/js/graph.js`:

```js
// Vis Network DAG Graph
// Shape per node kind so spec/plan/task read as distinct at a glance, independent of the
// status colouring the fill and border already carry.
const GRAPH_SHAPE_BY_KIND = {
  spec: { shape: 'hexagon' },
  plan: { shape: 'box', shapeProperties: { borderRadius: 14 } },
  task: { shape: 'box', shapeProperties: { borderRadius: 3 } },
};
```

Delete `src/taskmanager/engine/graph.py`, `src/taskmanager/engine/runtime.py` and `src/taskmanager/engine/wave.py` with `git -C <worktree> rm`. Any hit `test_the_package_names_no_pre_lifecycle_vocabulary` still prints after these edits is a line an earlier task left on the old vocabulary; it is rewritten with the table in 3c, never excluded from the test.

**3b.** Deleted tests: `tests/unit/test_graph_engine.py` (old graph resolution; its replacements are Tasks 3–7's rollup, display, chains and step-graph tests), `tests/unit/test_runtime_engine.py` (the old coordinator; Task 13's claims tests replace it), `tests/unit/test_wave.py` (`discover_batch`; Task 16's discovery tests replace it). Delete them with `git -C <worktree> rm`.

**3c.** Old tests rewritten in this task. One table converts every remaining reference to the old vocabulary in `tests/`:

| Old | New |
|---|---|
| `NodeStatus.NOT_STARTED` on a task, plan or spec | `Status.READY` |
| `NodeStatus.NOT_STARTED` on a decision | `DecisionStatus.OPEN` |
| `NodeStatus.COMPLETED` on a decision | `DecisionStatus.ANSWERED` |
| `NodeStatus.ABANDONED` on a decision | `DecisionStatus.WITHDRAWN` |
| `NodeStatus.WAITING_REVIEW` | `Status.IMPLEMENTED` |
| `NodeStatus.WAITING_FIXES` | `Status.REVIEWED`, with `outcome=Outcome.REJECT` where the test builds the node |
| `NodeStatus.WAITING_MERGE` | `Status.REVIEWED`, with `outcome=Outcome.APPROVE` where the test builds the node |
| `NodeStatus.<X>` for `IMPLEMENTING`, `REVIEWING`, `FIXING`, `MERGING`, `COMPLETED`, `SUPERSEDED`, `ABANDONED`, `DEFERRED` | `Status.<X>` |
| the text `"NOT_STARTED"` as a stored status in raw SQL or in expected CLI output | `"READY"` |
| `db.get_spec_connection()` / `db_mgr.get_spec_connection()` | `...get_state_connection()` |
| `...get_runtime_connection()` | `...get_state_connection()` |

It applies to `test_models.py`, `test_repos.py`, `test_search.py`, `test_search_engine.py`, `test_renderers.py`, `test_db_schema.py`, `test_heuristics.py`, `test_estate_workflow.py` and `test_operations.py`, whose imports swap `NodeStatus` for `Status`, `DecisionStatus` or `Outcome` from `taskmanager.core.status` as used. Beyond the table:

`tests/unit/test_models.py`: in `test_enums_values`, the `REVIEW_GATE` line and the fourteen `NodeStatus`/`VirtualStatus` lines are replaced by

```python
    assert [s.value for s in Status][:3] == ["READY", "IMPLEMENTING", "IMPLEMENTED"]
    assert [s.value for s in DecisionStatus] == ["OPEN", "ANSWERED", "WITHDRAWN"]
```

(the `NodeStatus` and `VirtualStatus` imports go; `from taskmanager.core.status import DecisionStatus, Status` joins); `test_node_instantiation_defaults` asserts `node.status == Status.READY`; `test_node_instantiation_explicit` uses `Status.IMPLEMENTING` twice.

`tests/unit/test_repos.py`: in `test_node_repo_relations`, delete everything from `edges = repo.get_dependency_edges("AUTH-T02")` to the end of the test (the gated-edge metadata it read is gone).

`tests/unit/test_operations.py`: the fixture becomes

```python
@pytest.fixture
def ops_setup(
    tmp_path: Path,
) -> tuple[NodeRepository, RuntimeRepository, LedgerRepository, Operations]:
    db = DatabaseManager(tmp_path / "db")
    db.init_all()
    node_repo = NodeRepository(db)
    runtime_repo = RuntimeRepository(db)
    ledger_repo = LedgerRepository(db)
    ops = Operations(
        node_repo,
        runtime_repo,
        ledger_repo,
        VerificationEngine(tmp_path),
        JobRepository(db),
        actor="tester",
    )
    return node_repo, runtime_repo, ledger_repo, ops
```

(with `from taskmanager.db.job_repo import JobRepository`), and these tests of the deleted methods are deleted: `test_set_status_stops_task_and_releases_lease`, `test_release_lease_drops_lease_without_changing_status`, `test_sweep_leases_rolls_back_status_of_expired_claims`, `test_sweep_leases_with_nothing_expired_writes_no_ledger`, `test_set_status_with_section_writes_status_and_section_together`, `test_set_status_failure_leaves_the_section_unwritten`, `test_release_lease_missing_node_refuses`, `test_set_status_refuses_a_decision_node`, `test_sweep_leases_second_write_failure_leaves_first_task_unrolled_back`, `test_set_status_with_section_second_write_failure_leaves_status_unchanged`. The imports of `GraphEngine`, `ExecutionCoordinator` and whatever `ruff check` then reports unused (`datetime`, `timedelta`, `Lease`) go.

`tests/unit/test_operations_lifecycle.py`: `make_ops` becomes

```python
def make_ops(root: Path) -> Env:
    db = DatabaseManager(root / ".taskmanager")
    db.init_all()
    node_repo = NodeRepository(db)
    runtime_repo = RuntimeRepository(db)
    ledger_repo = LedgerRepository(db)
    ops = Operations(
        node_repo,
        runtime_repo,
        ledger_repo,
        VerificationEngine(root),
        JobRepository(db),
        actor="tester",
    )
    return node_repo, runtime_repo, ledger_repo, ops
```

and its `GraphEngine` and `ExecutionCoordinator` imports go.

Every other test that builds `Operations(...)` itself (`tests/unit/test_decision_effects.py`'s kit among them; `git -C <worktree> grep -n "Operations(" tests` lists them) takes the same shape: the `GraphEngine` and `ExecutionCoordinator` arguments go, and a `JobRepository` over the same `DatabaseManager` is passed fifth.

`tests/unit/test_heuristics.py`: the imports swap `NodeStatus` and `GraphEngine` for `from taskmanager.core.status import Action, Status`, `from taskmanager.db.job_repo import JobRepository` and `from taskmanager.engine.snapshot import SnapshotBuilder`; every `GraphEngine` in a type annotation becomes `SnapshotBuilder`; the fixture and two tests become:

```python
@pytest.fixture
def env(
    tmp_path: Path,
) -> tuple[NodeRepository, RuntimeRepository, SnapshotBuilder, RecommendationEngine]:
    db = DatabaseManager(tmp_path)
    db.init_all()
    node_repo = NodeRepository(db)
    runtime_repo = RuntimeRepository(db)
    snapshots = SnapshotBuilder(node_repo, runtime_repo, JobRepository(db))
    engine = RecommendationEngine(node_repo, runtime_repo, snapshots)
    return node_repo, runtime_repo, snapshots, engine


def _claim(
    node_repo: NodeRepository, runtime_repo: RuntimeRepository, node_id: str, *files: str
) -> None:
    node = node_repo.get_node(node_id)
    assert node is not None
    node.status, node.claimed_from = Status.IMPLEMENTING, Status.READY
    lease = Lease(
        task_id=node_id,
        agent_id="agent-1",
        session_id="sess-1",
        branch_name=f"tm/{node_id}",
        action=Action.IMPLEMENT,
        acquired_at=datetime.now(tz=UTC),
        last_heartbeat=datetime.now(tz=UTC),
        ttl_seconds=300,
    )
    locks = [FileLock(file_path=f, task_id=node_id) for f in files]
    assert runtime_repo.claim(lease, locks, node)


def test_exclusion_of_blocked_claimed_and_already_implemented_tasks(
    env: tuple[NodeRepository, RuntimeRepository, SnapshotBuilder, RecommendationEngine],
) -> None:
    node_repo, runtime_repo, _, engine = env
    node_repo.save_node(Node(id="T-01", kind=NodeKind.TASK, title="Prerequisite", priority=80))
    node_repo.save_node(Node(id="T-02", kind=NodeKind.TASK, title="Blocked", priority=90))
    node_repo.save_node(Node(id="T-03", kind=NodeKind.TASK, title="Claimed", priority=85))
    node_repo.save_node(
        Node(
            id="T-04",
            kind=NodeKind.TASK,
            title="Already implemented",
            priority=70,
            status=Status.IMPLEMENTED,
        )
    )
    node_repo.add_relation(
        NodeRelation(source_id="T-02", target_id="T-01", relation_type=RelationType.DEPENDS_ON)
    )
    _claim(node_repo, runtime_repo, "T-03")

    assert [t.task_id for t in engine.get_next_tasks(limit=10)] == ["T-01"]


def test_exclusion_of_file_colliding_tasks(
    env: tuple[NodeRepository, RuntimeRepository, SnapshotBuilder, RecommendationEngine],
) -> None:
    node_repo, runtime_repo, _, engine = env
    node_repo.save_node(
        Node(id="TASK-VER", kind=NodeKind.TASK, title="Has Verification File", priority=80)
    )
    node_repo.save_node(
        Node(
            id="TASK-FM",
            kind=NodeKind.TASK,
            title="Has Frontmatter File",
            priority=75,
            frontmatter={"declared_files": ["src/db.py"]},
        )
    )
    node_repo.save_node(
        Node(id="TASK-FREE", kind=NodeKind.TASK, title="Free of locks", priority=70)
    )
    node_repo.save_node(Node(id="OTHER-TASK", kind=NodeKind.TASK, title="Holds files", priority=1))
    node_repo.add_verification(
        NodeVerification(
            node_id="TASK-VER",
            verification_type=VerificationType.FILE_EXISTS,
            target_path="src/auth/jwt.py",
        )
    )

    assert {t.task_id for t in engine.get_next_tasks(limit=5)} == {
        "TASK-VER",
        "TASK-FM",
        "TASK-FREE",
        "OTHER-TASK",
    }

    _claim(node_repo, runtime_repo, "OTHER-TASK", "src/auth/jwt.py", "src/db.py")
    assert [t.task_id for t in engine.get_next_tasks(limit=5)] == ["TASK-FREE"]

    runtime_repo.release_lease("OTHER-TASK")
    assert {t.task_id for t in engine.get_next_tasks(limit=5)} == {
        "TASK-VER",
        "TASK-FM",
        "TASK-FREE",
    }
```

(`OTHER-TASK` stays `IMPLEMENTING` with no lease after the release, which reads `STALE`, so it is not offered.) The remaining tests' unpacking `node_repo, _runtime_repo, _graph, engine = env` keeps its shape.

`tests/unit/test_estate_workflow.py`: delete the tests that drive `ExecutionCoordinator` directly — `test_a_lease_locks_frontmatter_files_and_a_second_claim_is_refused`, `test_the_lease_ttl_is_the_one_asked_for`, `test_a_worktree_is_cut_in_the_tasks_own_repository_from_origin_main_without_upstream`, `test_a_worktree_needs_a_git_repository_for_the_task`, `test_each_stage_of_the_lifecycle_is_claimed_by_its_own_lease`, `test_a_merge_claim_refuses_a_worktree`, `test_a_fix_round_reuses_the_branch_and_worktree_its_first_round_cut`, `test_completing_a_task_removes_the_worktree_it_no_longer_holds_a_lease_on` — whose behaviour Task 13's claims tests own (file locks, per-action TTL, the worktree in the task's own repository without upstream, a fix reusing the implement branch); delete the `coordinator()` helper and the `GraphEngine`, `ExecutionCoordinator`, `GitManager` imports; and replace `test_reimporting_a_document_keeps_the_progress_it_does_not_state` with:

```python
def test_reimporting_a_document_keeps_the_progress_it_does_not_state(db: DatabaseManager) -> None:
    from taskmanager.core.status import Outcome, Status

    repo = NodeRepository(db)
    importer = BulkImporter(repo)
    importer.import_dict(
        {
            "plans": [
                {"id": "P", "title": "P", "tasks": [{"id": "P-1", "title": "a", "priority": 70}]}
            ]
        }
    )
    node = repo.get_node("P-1")
    assert node is not None
    node.status, node.outcome = Status.REVIEWED, Outcome.APPROVE
    node.acceptable_models = ["claude-sonnet-5"]
    node.frontmatter = {"declared_files": ["web/a"]}
    repo.save_node(node)

    importer.import_dict(
        {"plans": [{"id": "P", "title": "P", "tasks": [{"id": "P-1", "title": "a renamed"}]}]}
    )
    kept = repo.get_node("P-1")
    assert kept is not None
    assert kept.title == "a renamed"
    assert (kept.status, kept.outcome, kept.priority) == (Status.REVIEWED, Outcome.APPROVE, 70)
    assert kept.acceptable_models == ["claude-sonnet-5"]
    assert kept.frontmatter == {"declared_files": ["web/a"]}

    importer.import_dict(
        {
            "plans": [
                {
                    "id": "P",
                    "title": "P",
                    "tasks": [{"id": "P-1", "title": "a", "status": "COMPLETED", "priority": 20}],
                }
            ]
        }
    )
    stated = repo.get_node("P-1")
    assert stated is not None and stated.status == Status.COMPLETED and stated.priority == 20
```

`tests/unit/test_db_schema.py`: the raw insert's `("AUTH-01", "task", "Auth Task", "NOT_STARTED")` becomes `("AUTH-01", "task", "Auth Task", "READY")`, and its `get_spec_connection()` calls follow the table.

- [ ] **Step 4: Run the tests and the gates**

```
uv run --directory <worktree> pytest tests/unit/test_old_vocabulary.py tests/unit/test_status_check.py -q; echo $?
uv run --directory <worktree> pytest -q; echo $?
uv run --directory <worktree> ruff check; echo $?
uv run --directory <worktree> ruff format; echo $?
uv run --directory <worktree> ruff format --check; echo $?
uv run --directory <worktree> mypy; echo $?
```

Expected: every command prints `0` (the `ruff format` run rewrites only files this task touched).

- [ ] **Step 5: Commit**

```
git -C <worktree> rm -q src/taskmanager/engine/graph.py src/taskmanager/engine/runtime.py src/taskmanager/engine/wave.py tests/unit/test_graph_engine.py tests/unit/test_runtime_engine.py tests/unit/test_wave.py && git -C <worktree> add src/taskmanager/core/enums.py src/taskmanager/core/models.py src/taskmanager/db/schema.py src/taskmanager/db/connection.py src/taskmanager/db/node_repo.py src/taskmanager/engine/operations.py src/taskmanager/engine/heuristics.py src/taskmanager/engine/snapshot.py src/taskmanager/engine/search.py src/taskmanager/engine/decisions.py src/taskmanager/renderers/markdown.py src/taskmanager/di/container.py src/taskmanager/web/static/js/graph.js tests/unit/test_old_vocabulary.py tests/unit/test_status_check.py tests/unit/test_operations.py tests/unit/test_operations_lifecycle.py tests/unit/test_heuristics.py tests/unit/test_estate_workflow.py tests/unit/test_models.py tests/unit/test_repos.py tests/unit/test_search.py tests/unit/test_search_engine.py tests/unit/test_renderers.py tests/unit/test_db_schema.py && git -C <worktree> commit -m "refactor(core): delete the pre-lifecycle status vocabulary and enforce statuses by kind in SQLite"
```

---

### Task 22: `tm-wave.js` as one loop per node

**Spec:** §10.1, §4.5, §5.2, §5.3, §5.5, §5.6, §6.2
**Files:**
- Modify: `workflows/tm-wave.js` (rewritten whole: `meta`, argument parsing, `op`, `djb2`, `opJson`, `discover`, `read`, `start`, `release`, `job`, `head`, `close`, `agentName`, `pickType`, `clip`, `work`, `land`, `run`, the discovery prologue; removed: `release(t, status)`, `claim`, `hold`, `reviewHash`, `verifyMerged`, `implement`, `review`, `fix`, `merge`, `family` and the `WORK`/`REVIEWED`/`MERGED` schemas)
- Create: `tests/workflow/harness.mjs`
- Create: `tests/workflow/tm-wave.test.mjs`
- Test: `tests/workflow/tm-wave.test.mjs`

**Interfaces:**
- Consumes: `tm wave discover` and its `chosen` entries `{id, kind, action, model, repos, requires, job, migration}` (Task 16); `tm task get --json` with `next_action` (Task 18); `tm task start --json` with `--worktree-dir`, printing `ClaimResult` `{action, reason, model, job, repos, branch, base, worktree, worktrees}` and exiting 3 on `blocked` (Tasks 13 and 18); `--agent` on `tm task complete`, `tm task review` and `tm task release`, refused unless the live lease is that agent's (Task 18); `tm job status <job> --wait <seconds>` (Task 18); the model families `haiku`, `sonnet`, `opus`, `fable` that `routing.model_for` returns (Task 13); `Action` values from `taskmanager/core/status.py`.
- Produces: the `tm-wave` workflow's argument set `{session, worktreeDir, specs, slots, maxStrong, maxBatch, exclude, holdMerge, root, tm, agentTypes, reviewerTypes, capabilities, preamble, rulesDir, gateLane, models}` (Tasks 23, 25 and 26 document and run it); the brief lines every dispatched agent receives (`tm-task:`, `Model:` with the id `models` maps tm's family to, the closing verb carrying `--agent wf-<session>-<id>`), which the guides of Task 23 assume; a trail line per step naming the family it ran on, which Task 26 reads.

The script keeps its three load-bearing mechanisms and drops the lifecycle it used to encode. It still has no shell: every `tm` call goes through a haiku runner, and every JSON reply is checked against a djb2 the same shell computed, now over UTF-8 bytes so non-ASCII text survives. It still claims every step itself, so no agent explores before its claim. What it no longer does: count fix rounds, hash `:review`, write `:hold`, choose statuses, choose models, build merges or verify them. tm names a model family on every claim and the script only maps it to an id through `models`; a family `models` does not map is released rather than guessed at. Each agent closes its own step with the verb its guide names (`tm task complete`, `tm task review`, `tm job resume`, or `tm task release --blocked`), passing the lease's agent name with `--agent` so tm refuses a close on a lease that is no longer this workflow's; the script reads the stored status afterwards and releases, with the same `--agent`, only a step still sitting at the `-ING` status its claim set.

- [ ] **Step 1: Write the failing test**

`tests/workflow/harness.mjs`:

```js
import { readFileSync } from 'node:fs'

const SOURCE = readFileSync(new URL('../../workflows/tm-wave.js', import.meta.url), 'utf8')
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor
const META_START = 'export const meta = '

// The Workflow tool reads meta without running the script, so it has to evaluate on its own.
export function meta() {
  const start = SOURCE.indexOf(META_START) + META_START.length
  const end = SOURCE.indexOf('\n}\n', start) + 2
  return Function(`"use strict"; return (${SOURCE.slice(start, end)})`)()
}

// Independent of the script's own djb2: it hashes Buffer bytes, the script a percent-decoded string.
export function djb2(text) {
  let h = 5381
  for (const byte of Buffer.from(text, 'utf8')) h = (Math.imul(h, 33) + byte) >>> 0
  return h
}

export const json = (value, exit = 0) => ({ text: JSON.stringify(value), exit })

export function discovery(chosen, extra = {}) {
  const payload = JSON.stringify({ chosen, held: [], waiting_for_slot: 0, mine: 0, ...extra })
  return `${payload}\n__CHECK n=${chosen.length} h=${djb2(payload)}`
}

// A scripted reply list is served in order and its last entry repeats, so a test writes only the
// replies that change; an entry that is a function runs when served, for replies with effects.
function queue(list) {
  const items = [...list]
  return () => {
    const entry = items.length > 1 ? items.shift() : items[0]
    return typeof entry === 'function' ? entry() : entry
  }
}

export function makeTm({ chosen = [], nodes = {}, start = {}, job = {}, releaseExit = 0, discover, corrupt = () => false } = {}) {
  const state = structuredClone(nodes)
  const starts = Object.fromEntries(Object.entries(start).map(([id, list]) => [id, queue(list)]))
  const jobs = Object.fromEntries(Object.entries(job).map(([id, list]) => [id, queue(list)]))
  function answer(inner) {
    let m = inner.match(/^\S+ task get (\S+) --json$/)
    if (m) return state[m[1]] ? json(state[m[1]]) : { text: `Task '${m[1]}' not found`, exit: 1 }
    m = inner.match(/^\S+ task start (\S+) --agent wf-\S+ --session \S+ --worktree-dir \S+ --json$/)
    if (m && starts[m[1]]) return starts[m[1]]()
    m = inner.match(/^\S+ job status (\S+)(?: --wait \d+)?$/)
    if (m && jobs[m[1]]) return jobs[m[1]]()
    throw new Error(`the fake tm has no reply for: ${inner}`)
  }
  return {
    set(id, patch) {
      state[id] = { ...state[id], ...patch }
    },
    reply(cmd) {
      if (/^\S+ wave discover\b/.test(cmd)) {
        return { stdout: `${discover ? discover(cmd) : discovery(chosen)}\n__EXIT:0\n` }
      }
      const m = cmd.match(/^out=\$\((.*) 2>&1\); rc=\$\?; /)
      if (m) {
        const { text, exit } = answer(m[1])
        const h = corrupt(m[1]) ? djb2(text) + 1 : djb2(text)
        return { stdout: `${text}\n__CHECK h=${h}\n__EXIT:${exit}\n` }
      }
      if (/^\S+ task release \S+ --agent wf-\S+ >\/dev\/null 2>&1$/.test(cmd)) {
        return { stdout: `__EXIT:${releaseExit}\n` }
      }
      throw new Error(`the fake tm has no reply for: ${cmd}`)
    },
  }
}

const RUNNER = /^\( (.*) \); echo "__EXIT:\$\?"$/m

export async function runWave({ args, tm, agents = () => 'done' }) {
  const calls = []
  const logs = []
  const errors = []
  const agent = async (prompt, opts = {}) => {
    const m = prompt.match(RUNNER)
    if (m) {
      calls.push({ kind: 'op', cmd: m[1], prompt, opts })
      return tm.reply(m[1])
    }
    calls.push({ kind: 'agent', prompt, opts })
    return agents(prompt, opts)
  }
  const pipeline = (items, ...stages) =>
    Promise.all(
      items.map(async (item, index) => {
        let value = item
        for (const stage of stages) {
          try {
            value = await stage(value, item, index)
          } catch (error) {
            errors.push(error)
            return null
          }
        }
        return value
      }),
    )
  const parallel = thunks => Promise.all(thunks.map(t => t().catch(error => (errors.push(error), null))))
  const body = SOURCE.replace(META_START, 'const meta = ')
  const script = new AsyncFunction('args', 'agent', 'pipeline', 'parallel', 'log', 'phase', body)
  const result = await script(args, agent, pipeline, parallel, message => logs.push(message), () => {})
  return {
    result,
    logs,
    errors,
    calls,
    ops: calls.filter(c => c.kind === 'op').map(c => c.cmd),
    work: calls.filter(c => c.kind === 'agent'),
  }
}
```

`tests/workflow/tm-wave.test.mjs`:

```js
import { test } from 'node:test'
import assert from 'node:assert/strict'

import { discovery, json, makeTm, meta, runWave } from './harness.mjs'

const ARGS = { session: 's1', worktreeDir: '/wt', root: '/est' }
const T1 = { id: 'T1', kind: 'task', action: 'implement', model: 'sonnet', repos: ['core'], requires: [], job: null, migration: false }

const node = (status, next_action, extra = {}) => ({ id: 'T1', kind: 'task', status, next_action, outcome: null, ...extra })
const claim = (action, extra = {}) =>
  json({ action, reason: null, model: 'sonnet', job: null, repos: ['core'], branch: 'tm/T1', base: 'main', worktree: null, worktrees: {}, ...extra })
const jobAt = (state, extra = {}) =>
  json({
    id: 'J1', kind: 'land', node_id: 'T1', repo: 'core', target: 'main', state, step: 'gate',
    worktree: '/est/.worktrees/land-T1', pid: 4242, heartbeat: null, result: null, ...extra,
  })
const starts = ops => ops.filter(c => / task start /.test(c))
const releases = ops => ops.filter(c => / task release /.test(c))
const RELEASE_T1 = 'tm task release T1 --agent wf-s1-T1 >/dev/null 2>&1'

test('session and worktreeDir are required', async () => {
  await assert.rejects(runWave({ args: { session: 's1' }, tm: makeTm() }), /args\.session and args\.worktreeDir are required/)
})

for (const key of ['release', 'maxFixRounds']) {
  test(`a caller passing ${key} is told what replaced it`, async () => {
    await assert.rejects(runWave({ args: { ...ARGS, [key]: [] }, tm: makeTm() }), new RegExp(`args\\.${key} is no longer read`))
  })
}

test('discovery asks about every spec unless specs names some', async () => {
  const all = await runWave({ args: ARGS, tm: makeTm() })
  const some = await runWave({ args: { ...ARGS, specs: ['S1', 'S2'] }, tm: makeTm() })
  assert.equal(all.ops[0], 'tm wave discover --session s1 --slots 9 --max-strong 5')
  assert.equal(some.ops[0], 'tm wave discover --spec S1 --spec S2 --session s1 --slots 9 --max-strong 5')
})

test('a discovery payload whose checksum never matches claims nothing', async () => {
  const tm = makeTm({ discover: () => '{"chosen":[],"held":[],"waiting_for_slot":0,"mine":0}\n__CHECK n=0 h=1' })
  await assert.rejects(runWave({ args: ARGS, tm }), /discovery failed three times/)
})

test('a payload carrying non-ASCII text passes its checksum', async () => {
  const tm = makeTm({ discover: () => discovery([], { held: ['T9: aguardando a decisão D1'] }) })
  const { logs, errors } = await runWave({ args: ARGS, tm })
  assert.deepEqual(errors, [])
  assert.ok(logs.includes('held: T9: aguardando a decisão D1'))
})

test('a READY task is implemented, reviewed and landed in one loop, one claim per step', async () => {
  const tm = makeTm({
    chosen: [T1],
    nodes: { T1: node('READY', 'implement') },
    start: {
      T1: [
        () => (tm.set('T1', { status: 'IMPLEMENTING', next_action: null }), claim('implement', { worktree: '/wt/core-T1' })),
        () => (tm.set('T1', { status: 'REVIEWING', next_action: null }), claim('review')),
        () => (tm.set('T1', { status: 'MERGING', next_action: null }), claim('merge', { job: 'J1' })),
      ],
    },
    job: { J1: [() => (tm.set('T1', { status: 'COMPLETED' }), jobAt('succeeded', { step: 'complete' }))] },
  })
  const agents = (prompt, opts) => {
    if (opts.label === 'implement:T1') tm.set('T1', { status: 'IMPLEMENTED', next_action: 'review' })
    if (opts.label === 'review:T1') tm.set('T1', { status: 'REVIEWED', next_action: 'merge', outcome: 'approve' })
    return 'done'
  }
  const { result, ops, work, errors } = await runWave({ args: ARGS, tm, agents })
  assert.deepEqual(errors, [])
  assert.deepEqual(result.results.map(r => [r.id, r.status]), [['T1', 'COMPLETED']])
  assert.deepEqual(work.map(w => w.opts.label), ['implement:T1', 'review:T1'])
  assert.equal(starts(ops).length, 3)
  assert.deepEqual(releases(ops), [])
})

test('every agent runs under a phase the meta declares', async () => {
  const tm = makeTm({
    chosen: [T1],
    nodes: { T1: node('READY', 'implement') },
    start: {
      T1: [
        () => (tm.set('T1', { status: 'IMPLEMENTING', next_action: null }), claim('implement', { worktree: '/wt/core-T1' })),
        () => (tm.set('T1', { status: 'REVIEWING', next_action: null }), claim('review')),
        () => (tm.set('T1', { status: 'FIXING', next_action: null }), claim('fix', { worktree: '/wt/core-T1' })),
        () => (tm.set('T1', { status: 'REVIEWING', next_action: null }), claim('review')),
        () => (tm.set('T1', { status: 'MERGING', next_action: null }), claim('merge', { job: 'J1' })),
      ],
    },
    job: {
      J1: [jobAt('needs_agent', { step: 'build', result: 'conflict' }), jobAt('running'), () => (tm.set('T1', { status: 'COMPLETED' }), jobAt('succeeded'))],
    },
  })
  let reviews = 0
  const agents = (prompt, opts) => {
    if (opts.label === 'implement:T1') tm.set('T1', { status: 'IMPLEMENTED', next_action: 'review' })
    if (opts.label === 'review:T1') {
      reviews += 1
      tm.set('T1', reviews === 1 ? { status: 'REVIEWED', next_action: 'fix', outcome: 'reject' } : { status: 'REVIEWED', next_action: 'merge', outcome: 'approve' })
    }
    if (opts.label === 'fix:T1') tm.set('T1', { status: 'FIXED', next_action: 'review' })
    return 'done'
  }
  const { calls, work, errors } = await runWave({ args: ARGS, tm, agents })
  const titles = meta().phases.map(p => p.title)
  assert.deepEqual(errors, [])
  assert.deepEqual(work.map(w => w.opts.label), ['implement:T1', 'review:T1', 'fix:T1', 'review:T1', 'merge-agent:T1'])
  for (const call of calls) assert.ok(titles.includes(call.opts.phase), `${call.opts.label} runs under ${call.opts.phase}`)
})

for (const [fam, models, id] of [
  ['opus', undefined, 'claude-opus-5'],
  ['fable', undefined, 'claude-fable-5-1'],
  ['opus', { opus: 'claude-opus-6' }, 'claude-opus-6'],
]) {
  test(`a step tm routes to the ${fam} family runs on it and names ${id}`, async () => {
    const tm = makeTm({
      chosen: [T1],
      nodes: { T1: node('READY', 'implement') },
      start: { T1: [() => (tm.set('T1', { status: 'IMPLEMENTING', next_action: null }), claim('implement', { model: fam, worktree: '/wt/core-T1' }))] },
    })
    const agents = () => (tm.set('T1', { status: 'IMPLEMENTED', next_action: null }), 'done')
    const { work, result } = await runWave({ args: { ...ARGS, ...(models ? { models } : {}) }, tm, agents })
    assert.equal(work[0].opts.model, fam)
    assert.match(work[0].prompt, new RegExp(`^Model: ${id}$`, 'm'))
    assert.ok(result.results[0].trail.includes(`implement on ${fam}: done`))
  })
}

test('a claim naming a family args.models does not map is released and never dispatched', async () => {
  const tm = makeTm({
    chosen: [T1],
    nodes: { T1: node('READY', 'implement') },
    start: { T1: [() => (tm.set('T1', { status: 'IMPLEMENTING', next_action: null }), claim('implement', { model: 'claude-opus-5', worktree: '/wt/core-T1' }))] },
  })
  const { ops, work, result } = await runWave({ args: ARGS, tm })
  assert.deepEqual(work, [])
  assert.deepEqual(releases(ops), [RELEASE_T1])
  assert.ok(result.results[0].trail.includes('claim names the model family claude-opus-5, which args.models does not map'))
})

test('an implement brief names the worktree, the branch, its base and the verb that closes it', async () => {
  const tm = makeTm({
    chosen: [T1],
    nodes: { T1: node('READY', 'implement') },
    start: { T1: [() => (tm.set('T1', { status: 'IMPLEMENTING', next_action: null }), claim('implement', { worktree: '/wt/core-T1' }))] },
  })
  const agents = () => (tm.set('T1', { status: 'IMPLEMENTED', next_action: null }), 'done')
  const { work } = await runWave({ args: { ...ARGS, agentTypes: { core: 'python-dev' } }, tm, agents })
  for (const text of ['tm-task: T1', 'tm guide implement', 'Worktree: /wt/core-T1', 'branch tm/T1', 'based on main', 'tm task complete T1 --agent wf-s1-T1', 'tm task release T1 --agent wf-s1-T1 --blocked', 'tm render T1 --view subagent']) {
    assert.ok(work[0].prompt.includes(text), text)
  }
  assert.equal(work[0].opts.agentType, 'python-dev')
})

test('a step the agent leaves open is released as a counted failure', async () => {
  const tm = makeTm({
    chosen: [T1],
    nodes: { T1: node('READY', 'implement') },
    start: { T1: [() => (tm.set('T1', { status: 'IMPLEMENTING', next_action: null }), claim('implement', { worktree: '/wt/core-T1' }))] },
  })
  const { ops, result } = await runWave({ args: ARGS, tm, agents: () => 'I stopped before committing' })
  assert.deepEqual(releases(ops), [RELEASE_T1])
  assert.ok(result.results[0].trail.includes('released, the implement step was left open'))
})

test("a dead agent's step is released", async () => {
  const tm = makeTm({
    chosen: [T1],
    nodes: { T1: node('READY', 'implement') },
    start: { T1: [() => (tm.set('T1', { status: 'IMPLEMENTING', next_action: null }), claim('implement', { worktree: '/wt/core-T1' }))] },
  })
  const { ops, result } = await runWave({ args: ARGS, tm, agents: () => null })
  assert.deepEqual(releases(ops), [RELEASE_T1])
  assert.ok(result.results[0].trail.includes('implement on sonnet: the agent died'))
})

test('a step the agent closed is never released by the script', async () => {
  const tm = makeTm({
    chosen: [T1],
    nodes: { T1: node('READY', 'implement') },
    start: { T1: [() => (tm.set('T1', { status: 'IMPLEMENTING', next_action: null }), claim('implement', { worktree: '/wt/core-T1' }))] },
  })
  const agents = () => (tm.set('T1', { status: 'READY', next_action: null }), 'released --blocked on T2')
  const { ops } = await runWave({ args: ARGS, tm, agents })
  assert.deepEqual(releases(ops), [])
})

test("a step already claimed by another dispatcher's next claim is not released", async () => {
  const tm = makeTm({
    chosen: [T1],
    nodes: { T1: node('READY', 'implement') },
    start: { T1: [() => (tm.set('T1', { status: 'IMPLEMENTING', next_action: null }), claim('implement', { worktree: '/wt/core-T1' }))] },
  })
  const agents = () => (tm.set('T1', { status: 'REVIEWING', next_action: null }), 'done')
  const { ops } = await runWave({ args: ARGS, tm, agents })
  assert.deepEqual(releases(ops), [])
})

test("a blocked claim ends the loop with tm's reason and dispatches nobody", async () => {
  const tm = makeTm({
    chosen: [T1],
    nodes: { T1: node('READY', 'implement') },
    start: { T1: [json({ action: 'blocked', reason: 'waits on the decisão D1', job: null }, 3)] },
  })
  const { result, work } = await runWave({ args: ARGS, tm })
  assert.deepEqual(work, [])
  assert.equal(result.results[0].status, 'blocked')
  assert.ok(result.results[0].trail.includes('blocked: waits on the decisão D1'))
})

test('a claim whose transcription fails its checksum is released and never acted on', async () => {
  const tm = makeTm({
    chosen: [T1],
    nodes: { T1: node('READY', 'implement') },
    start: { T1: [claim('implement', { worktree: '/wt/core-T1' })] },
    corrupt: inner => / task start /.test(inner),
  })
  const { ops, work } = await runWave({ args: ARGS, tm })
  assert.equal(starts(ops).length, 1)
  assert.deepEqual(work, [])
  assert.deepEqual(releases(ops), [RELEASE_T1])
})

test('holdMerge stops a node before its merge claim', async () => {
  const tm = makeTm({ chosen: [{ ...T1, action: 'merge' }], nodes: { T1: node('REVIEWED', 'merge', { outcome: 'approve' }) } })
  const { ops, result } = await runWave({ args: { ...ARGS, holdMerge: ['T1'] }, tm })
  assert.deepEqual(starts(ops), [])
  assert.ok(result.results[0].trail.includes('merge held by args.holdMerge'))
})

test('holdMerge does not stop the steps before the merge', async () => {
  const tm = makeTm({
    chosen: [T1],
    nodes: { T1: node('READY', 'implement') },
    start: { T1: [() => (tm.set('T1', { status: 'IMPLEMENTING', next_action: null }), claim('implement', { worktree: '/wt/core-T1' }))] },
  })
  const agents = () => (tm.set('T1', { status: 'IMPLEMENTED', next_action: 'merge' }), 'done')
  const { ops, work } = await runWave({ args: { ...ARGS, holdMerge: ['T1'] }, tm, agents })
  assert.deepEqual(work.map(w => w.opts.label), ['implement:T1'])
  assert.equal(starts(ops).length, 1)
})

test('a node at FAILED is not claimed', async () => {
  const tm = makeTm({ chosen: [T1], nodes: { T1: node('FAILED', null) } })
  const { ops, result } = await runWave({ args: ARGS, tm })
  assert.deepEqual(starts(ops), [])
  assert.equal(result.results[0].status, 'FAILED')
})

test('a landing stopped for an agent hands it the job, its state and its worktree, then polling resumes', async () => {
  const tm = makeTm({
    chosen: [{ ...T1, action: 'merge' }],
    nodes: { T1: node('REVIEWED', 'merge', { outcome: 'approve' }) },
    start: { T1: [() => (tm.set('T1', { status: 'MERGING', next_action: null }), claim('merge', { job: 'J1' }))] },
    job: {
      J1: [
        jobAt('needs_agent', { step: 'build', result: 'conflict in src/a.py' }),
        jobAt('running'),
        () => (tm.set('T1', { status: 'COMPLETED' }), jobAt('succeeded')),
      ],
    },
  })
  const { work, ops, result } = await runWave({ args: ARGS, tm })
  assert.equal(work.length, 1)
  assert.equal(work[0].opts.label, 'merge-agent:T1')
  assert.equal(work[0].opts.model, 'sonnet')
  for (const text of ['tm guide merge', 'Job: J1', 'stopped at build: conflict in src/a.py', 'Worktree: /est/.worktrees/land-T1', 'tm job resume J1', '--own-defect']) {
    assert.ok(work[0].prompt.includes(text), text)
  }
  assert.equal(result.results[0].status, 'COMPLETED')
  assert.deepEqual(releases(ops), [])
})

test('a landing the agent left stopped is released and counted', async () => {
  const tm = makeTm({
    chosen: [{ ...T1, action: 'merge' }],
    nodes: { T1: node('REVIEWED', 'merge', { outcome: 'approve' }) },
    start: { T1: [() => (tm.set('T1', { status: 'MERGING', next_action: null }), claim('merge', { job: 'J1' }))] },
    job: { J1: [jobAt('needs_agent', { step: 'push', result: 'push_failed' })] },
  })
  const { ops, result } = await runWave({ args: ARGS, tm })
  assert.deepEqual(releases(ops), [RELEASE_T1])
  assert.ok(result.results[0].trail.includes('released, the landing agent left the job stopped'))
})

test('a running job is polled with --wait, under the long runner timeout, until it leaves running', async () => {
  const tm = makeTm({
    chosen: [{ ...T1, action: 'merge' }],
    nodes: { T1: node('REVIEWED', 'merge', { outcome: 'approve' }) },
    start: { T1: [() => (tm.set('T1', { status: 'MERGING', next_action: null }), claim('merge', { job: 'J1' }))] },
    job: { J1: [jobAt('running'), jobAt('running'), () => (tm.set('T1', { status: 'COMPLETED' }), jobAt('succeeded'))] },
  })
  const { calls, work } = await runWave({ args: ARGS, tm })
  const waits = calls.filter(c => c.kind === 'op' && / job status J1 --wait 540 2>&1\)/.test(c.cmd))
  assert.equal(waits.length, 3)
  for (const w of waits) assert.ok(w.prompt.includes('600000'))
  assert.deepEqual(work, [])
})

test('a sync the claim started is followed before the claim is retried', async () => {
  const tm = makeTm({
    chosen: [T1],
    nodes: { T1: node('READY', 'implement') },
    start: {
      T1: [
        json({ action: 'blocked', reason: 'syncing tm/P1', job: 'S1' }, 3),
        () => (tm.set('T1', { status: 'IMPLEMENTING', next_action: null }), claim('implement', { worktree: '/wt/core-T1', base: 'tm/P1' })),
      ],
    },
    job: { S1: [jobAt('succeeded', { id: 'S1', kind: 'sync' })] },
  })
  const agents = () => (tm.set('T1', { status: 'IMPLEMENTED', next_action: null }), 'done')
  const { ops, work } = await runWave({ args: ARGS, tm, agents })
  const order = ops.filter(c => / task start | job status /.test(c)).map(c => (/ job status /.test(c) ? 'job' : 'start'))
  assert.deepEqual(order, ['start', 'job', 'start'])
  assert.deepEqual(work.map(w => w.opts.label), ['implement:T1'])
})

test('a sync stopped for an agent is handed over by the next claim, on the family it names', async () => {
  const tm = makeTm({
    chosen: [T1],
    nodes: { T1: node('READY', 'implement') },
    start: {
      T1: [
        json({ action: 'blocked', reason: 'syncing tm/P1', job: 'S1' }, 3),
        claim('sync', { job: 'S1', base: 'tm/P1' }),
        () => (tm.set('T1', { status: 'IMPLEMENTING', next_action: null }), claim('implement', { worktree: '/wt/core-T1', base: 'tm/P1' })),
      ],
    },
    job: {
      S1: [
        jobAt('needs_agent', { id: 'S1', kind: 'sync', target: 'tm/P1', step: 'build', result: 'conflict' }),
        jobAt('needs_agent', { id: 'S1', kind: 'sync', target: 'tm/P1', step: 'build', result: 'conflict' }),
        jobAt('running', { id: 'S1', kind: 'sync' }),
        jobAt('succeeded', { id: 'S1', kind: 'sync' }),
      ],
    },
  })
  const agents = (prompt, opts) => {
    if (opts.label === 'implement:T1') tm.set('T1', { status: 'IMPLEMENTED', next_action: null })
    return 'done'
  }
  const { work, ops, result, errors } = await runWave({ args: ARGS, tm, agents })
  assert.deepEqual(errors, [])
  assert.deepEqual(work.map(w => [w.opts.label, w.opts.model]), [['sync-agent:T1', 'sonnet'], ['implement:T1', 'sonnet']])
  assert.ok(work[0].prompt.includes('tm job resume S1'))
  assert.ok(result.results[0].trail.includes('sync stopped for an agent; the next claim hands it over'))
  assert.equal(starts(ops).length, 3)
  assert.deepEqual(releases(ops), [])
})

for (const [base, from] of [['main', 'origin/main'], ['tm/S1', 'tm/S1']]) {
  test(`a container review based on ${base} reads every repository it touched from ${from}, on the container reviewer`, async () => {
    const P1 = { id: 'P1', kind: 'plan', action: 'review', model: 'opus', repos: ['core', 'web'], requires: [], job: null, migration: false }
    const tm = makeTm({
      chosen: [P1],
      nodes: { P1: { ...node('IMPLEMENTED', 'review'), id: 'P1', kind: 'plan' } },
      start: { P1: [() => (tm.set('P1', { status: 'REVIEWING', next_action: null }), claim('review', { model: 'opus', repos: ['core', 'web'], branch: 'tm/P1', base }))] },
    })
    const agents = () => (tm.set('P1', { status: 'REVIEWED', next_action: null }), 'done')
    const reviewerTypes = { task: 'task-reviewer', rereview: 'scoped-re-reviewer', container: 'branch-reviewer' }
    const { work } = await runWave({ args: { ...ARGS, reviewerTypes }, tm, agents })
    assert.equal(work[0].opts.agentType, 'branch-reviewer')
    assert.equal(work[0].opts.model, 'opus')
    assert.ok(work[0].prompt.includes(`git -C /est/core diff ${from}...tm/P1`))
    assert.ok(work[0].prompt.includes(`git -C /est/web diff ${from}...tm/P1`))
    assert.ok(work[0].prompt.includes('container review'))
  })
}

test('a review after a fix is scoped to the open findings, on the re-reviewer', async () => {
  const tm = makeTm({
    chosen: [{ ...T1, action: 'review' }],
    nodes: { T1: node('FIXED', 'review', { outcome: 'reject' }) },
    start: { T1: [() => (tm.set('T1', { status: 'REVIEWING', next_action: null }), claim('review'))] },
  })
  const agents = () => (tm.set('T1', { status: 'REVIEWED', next_action: null }), 'done')
  const reviewerTypes = { task: 'task-reviewer', rereview: 'scoped-re-reviewer', container: 'branch-reviewer' }
  const { work } = await runWave({ args: { ...ARGS, reviewerTypes }, tm, agents })
  assert.equal(work[0].opts.agentType, 'scoped-re-reviewer')
  assert.ok(work[0].prompt.includes('not yet recorded as closed'))
  assert.ok(!work[0].prompt.includes('diff origin/main'))
  assert.ok(work[0].prompt.includes('tm task review T1 --agent wf-s1-T1 --approve or --reject'))
})

test('a container fix across repositories names the worktree tm cut in each', async () => {
  const P1 = { id: 'P1', kind: 'plan', action: 'fix', model: 'opus', repos: ['core', 'web'], requires: [], job: null, migration: false }
  const worktrees = { core: '/wt/core-P1', web: '/wt/web-P1' }
  const tm = makeTm({
    chosen: [P1],
    nodes: { P1: { ...node('REVIEWED', 'fix', { outcome: 'reject' }), id: 'P1', kind: 'plan' } },
    start: { P1: [() => (tm.set('P1', { status: 'FIXING', next_action: null }), claim('fix', { model: 'opus', repos: ['core', 'web'], branch: 'tm/P1', worktree: '/wt/P1', worktrees }))] },
  })
  const agents = () => (tm.set('P1', { status: 'FIXED', next_action: null }), 'done')
  const { work } = await runWave({ args: ARGS, tm, agents })
  for (const text of ['core at /wt/core-P1', 'web at /wt/web-P1', 'branch tm/P1', 'tm task complete P1 --agent wf-s1-P1']) {
    assert.ok(work[0].prompt.includes(text), text)
  }
  assert.ok(!work[0].prompt.includes('worktree add'))
})

for (const [outcome, pointer] of [['merge_failed', 'tm section get T1:merge'], ['reject', 'tm section get T1:review not recorded as closed']]) {
  test(`a fix answering ${outcome} is pointed at ${pointer.split(' ')[3]}`, async () => {
    const tm = makeTm({
      chosen: [{ ...T1, action: 'fix' }],
      nodes: { T1: node('REVIEWED', 'fix', { outcome }) },
      start: { T1: [() => (tm.set('T1', { status: 'FIXING', next_action: null }), claim('fix', { worktree: '/wt/core-T1' }))] },
    })
    const agents = () => (tm.set('T1', { status: 'FIXED', next_action: null }), 'done')
    const { work } = await runWave({ args: ARGS, tm, agents })
    assert.ok(work[0].prompt.includes(pointer), pointer)
    assert.ok(work[0].prompt.includes('tm guide fix'))
  })
}

for (const [capabilities, expected] of [[{}, undefined], [{ 'python-dev': ['figma'] }, 'python-dev']]) {
  test(`a node requiring figma goes to ${expected ?? 'the default agent'} when python-dev serves ${JSON.stringify(capabilities['python-dev'] ?? [])}`, async () => {
    const tm = makeTm({
      chosen: [{ ...T1, requires: ['figma'] }],
      nodes: { T1: node('READY', 'implement') },
      start: { T1: [() => (tm.set('T1', { status: 'IMPLEMENTING', next_action: null }), claim('implement', { worktree: '/wt/core-T1' }))] },
    })
    const agents = () => (tm.set('T1', { status: 'IMPLEMENTED', next_action: null }), 'done')
    const { work } = await runWave({ args: { ...ARGS, agentTypes: { core: 'python-dev' }, capabilities }, tm, agents })
    assert.equal(work[0].opts.agentType, expected)
    assert.ok(work[0].prompt.includes('Requires: figma'))
  })
}

test('every op runs the tm the caller names', async () => {
  const tm = makeTm({ chosen: [T1], nodes: { T1: node('FAILED', null) } })
  const { ops } = await runWave({ args: { ...ARGS, tm: '/opt/tm-new/bin/tm' }, tm })
  assert.ok(ops.length >= 2)
  for (const cmd of ops) assert.ok(cmd.startsWith('/opt/tm-new/bin/tm ') || cmd.startsWith('out=$(/opt/tm-new/bin/tm '), cmd)
})

test('a loop that never settles stops at the step cap and says so', async () => {
  const tm = makeTm({
    chosen: [T1],
    nodes: { T1: node('READY', 'implement') },
    start: { T1: [claim('implement', { worktree: '/wt/core-T1' })] },
  })
  const { work, logs } = await runWave({ args: ARGS, tm })
  assert.equal(work.length, 24)
  assert.ok(logs.some(l => l.startsWith('T1: 24 steps without reaching COMPLETED, FAILED or blocked')))
})
```

- [ ] **Step 2: Run it and watch it fail**

```
node --test <worktree>/tests/workflow/; echo $?
```

Expected: exit 1, `tests 37`, `pass 1`, `fail 36`. The current script requires `args.specs`, so every test that omits it fails with `Error: args.specs, args.session and args.worktreeDir are required`; the two retired-argument tests fail because that same error does not match `args.release is no longer read` / `args.maxFixRounds is no longer read`. The one pass is `session and worktreeDir are required`, whose message the old error also matches.

- [ ] **Step 3: Implement**

`workflows/tm-wave.js`, whole file:

```js
export const meta = {
  name: 'tm-wave',
  description: 'Choose claimable tm nodes and run each through its own loop: tm task start names the next step and its model, an agent does the step and closes it, and landings run as tm jobs',
  whenToUse: 'Dispatcher tick. args: {session, worktreeDir, specs, slots, maxStrong, maxBatch, exclude, holdMerge, root, tm, agentTypes, reviewerTypes, capabilities, preamble, rulesDir, gateLane, models}. session and worktreeDir are required; specs defaults to every spec; root defaults to the session cwd and tm to the tm on PATH; agentTypes (repo -> agent type for implement and fix), reviewerTypes ({task, rereview, container} -> agent type), capabilities (agent type -> the requires values it serves), preamble (repo -> a line prepended to its briefs, plus a "default" key) and rulesDir default to none; gateLane (a string, or repo -> text with a "default" key; `{task}` becomes the node id) defaults to none; models (the family tm names on a claim -> model id) overrides the current Claude ids family by family.',
  phases: [
    { title: 'Discover', detail: '`tm wave discover` chooses the batch', model: 'haiku' },
    { title: 'Claim', detail: 'tm task get and tm task start, and a release when a step is left open', model: 'haiku' },
    { title: 'Implement' },
    { title: 'Review' },
    { title: 'Fix' },
    { title: 'Land', detail: 'tm job status until a landing or sync leaves running', model: 'haiku' },
    { title: 'Land agent', detail: 'a landing or sync tm stopped for an agent' },
  ],
}

const A = args || {}
const SESSION = A.session
const WT = A.worktreeDir
if (!SESSION || !WT) throw new Error('args.session and args.worktreeDir are required')
const RETIRED = {
  release: 'a node waits only on what tm task release --blocked names, and the workflow holds nothing',
  maxFixRounds: 'tm counts fix rounds itself; set max_fix_rounds with tm config set',
}
for (const key of Object.keys(RETIRED)) {
  if (key in A) throw new Error(`args.${key} is no longer read: ${RETIRED[key]}`)
}
const SPECS = A.specs || []
const SLOTS = A.slots || 9
const MAX_STRONG = A.maxStrong || 5
const HOLD_MERGE = new Set(A.holdMerge || [])

// The tm root a bare command runs against; a caller with no fixed estate path leaves this out
// and every op runs from wherever the dispatching session already sits.
const ROOT = A.root || '.'
// tm names a family on every claim; this maps it to the id a brief's Model: line carries.
const MODEL_ID = { haiku: 'claude-haiku-4-5', sonnet: 'claude-sonnet-5', opus: 'claude-opus-5', fable: 'claude-fable-5-1', ...A.models }
// repo -> agent type for implement and fix; a repo missing here gets the harness default.
const AGENT_TYPE = A.agentTypes || {}
// {task, rereview, container} -> agent type for a first task review, a review after a fix, and a
// plan's or spec's review.
const REVIEWER = A.reviewerTypes || {}
// agent type -> the `requires` values it can serve; a type not listed serves none.
const CAPS = A.capabilities || {}
const PREAMBLE = A.preamble || {}
const RULES_DIR = A.rulesDir || null
// Many concurrent agents running suites on one machine starve each other, so a project with a
// remote runner names it here and every implement, review and fix brief carries it.
const GATE_LANE = typeof A.gateLane === 'string' ? { default: A.gateLane } : A.gateLane || {}

const SAFE = /^[A-Za-z0-9._\/-]+$/
const q = s => {
  if (!SAFE.test(String(s))) throw new Error(`refusing to interpolate ${JSON.stringify(s)}`)
  return s
}
const TM = q(A.tm || 'tm')

// tm's own caps end every real cycle long before this; a node reaching it means the script and
// tm disagree about what comes next, and tm task get is what explains it.
const MAX_STEPS = 24
// tm job status --wait returns inside the runner's 10-minute command limit; a job still running
// after MAX_POLLS waits keeps running on its own and a later tick picks it up.
const WAIT_SECONDS = 540
const MAX_POLLS = 8
const TERMINAL = new Set(['COMPLETED', 'FAILED', 'DEFERRED', 'ABANDONED', 'SUPERSEDED'])
const CLAIMED = { implement: 'IMPLEMENTING', review: 'REVIEWING', fix: 'FIXING' }
const ACTIONS = ['implement', 'review', 'fix', 'merge', 'sync']
const PHASE = { discover: 'Discover', read: 'Claim', start: 'Claim', release: 'Claim', job: 'Land' }
const WORK_PHASE = { implement: 'Implement', review: 'Review', fix: 'Fix' }

// A session started before .claude/agents/tm-op.md existed does not know the type, so the first
// failure hands this and every later command to the generic runner.
let opType = 'tm-op'
async function runner(prompt, opts) {
  if (opType) {
    const r = await agent(prompt, { ...opts, agentType: opType }).catch(() => null)
    if (r) return r
    if (opType) log(`${opts.label}: the tm-op runner returned nothing; the generic runner takes the rest of this run`)
    opType = undefined
  }
  return agent(prompt, opts)
}

// The script has no shell: a runner agent executes a command the script composed, and the
// __EXIT sentinel is parsed from its stdout rather than trusting the runner's own account.
// `shape` (last group: the exit code) doubles as the schema pattern that rejects a transcription
// of the wrong shape before the script sees it; `valid` rejects one of the right shape but wrong
// bytes, and either rejection is retried while attempts remain.
async function op(kind, id, cmd, shape, { attempts = 2, long = false, valid = () => true } = {}) {
  const schema = { type: 'object', properties: { stdout: { type: 'string', pattern: shape.source } }, required: ['stdout'] }
  const timeout = long ? ' Set the Bash tool timeout to 600000 ms: the command waits up to nine minutes.' : ''
  for (let attempt = 0; attempt < attempts; attempt++) {
    const r = await runner(`tm-task: none — command runner for the tm-wave workflow
Model: ${MODEL_ID.haiku}
Run this exact command once with the Bash tool, from ${ROOT}, changing nothing in it, and run no other command.${timeout}

( ${cmd} ); echo "__EXIT:$?"

Return its complete stdout, character for character, in the stdout field: every line, unparsed and unreformatted, even where a line is JSON.`,
      { label: `${kind}:${id}`, phase: PHASE[kind], model: 'haiku', effort: 'low', schema })
    const m = r && typeof r.stdout === 'string' && r.stdout.match(shape)
    if (m && valid(m)) return { exit: Number(m[m.length - 1]), m }
  }
  return null
}

// Over UTF-8 bytes, as tm and the shell compute it, so text carrying non-ASCII still checks.
function djb2(s) {
  const bytes = unescape(encodeURIComponent(s))
  let h = 5381
  for (let i = 0; i < bytes.length; i++) h = (Math.imul(h, 33) + bytes.charCodeAt(i)) >>> 0
  return h
}

const CHECKSUM = `python3 -c 'import sys,functools;print("__CHECK h=%d" % functools.reduce(lambda h,b:(h*33+b)&0xFFFFFFFF,sys.stdin.buffer.read(),5381))'`

// A tm command whose stdout is JSON, checked against a djb2 the same shell computed over the same
// bytes, so a paraphrased field never reaches a decision.
async function opJson(kind, id, cmd, opts = {}) {
  const r = await op(kind, id,
    `out=$(${cmd} 2>&1); rc=$?; printf '%s\\n' "$out"; printf '%s' "$out" | ${CHECKSUM}; exit $rc`,
    /^([\s\S]*)\n__CHECK h=(\d+)\n__EXIT:(\d+)\s*$/,
    { ...opts, valid: m => djb2(m[1]) === Number(m[2]) })
  if (!r) return null
  let data = null
  try {
    data = JSON.parse(r.m[1])
  } catch (e) {
    data = null
  }
  return { exit: r.exit, data, text: r.m[1] }
}

// Returns the batch, or null when the runner's transcription fails its exit code or checksum.
async function discover(attempt) {
  const cmd = [
    `${TM} wave discover`,
    ...SPECS.map(s => `--spec ${q(s)}`),
    `--session ${q(SESSION)}`,
    `--slots ${SLOTS | 0}`,
    `--max-strong ${MAX_STRONG | 0}`,
    ...(A.exclude || []).map(x => `--exclude ${q(x)}`),
  ].join(' ')
  const r = await op('discover', `attempt-${attempt}`, cmd, /^([\s\S]*?)__EXIT:(\d+)\s*$/)
  if (!r || r.exit !== 0) return null
  const lines = r.m[1].split('\n').map(l => l.trim()).filter(Boolean)
  const check = (lines.pop() || '').match(/^__CHECK n=(\d+) h=(\d+)$/)
  const payload = lines.pop() || ''
  try {
    const d = JSON.parse(payload)
    return check && djb2(payload) === Number(check[2]) && d.chosen.length === Number(check[1]) ? d : null
  } catch (e) {
    return null
  }
}

const agentName = n => `wf-${q(SESSION)}-${q(n.id)}`
const clip = v => String((typeof v === 'string' ? v : JSON.stringify(v)) ?? '').replace(/\s+/g, ' ').slice(0, 300)
// The harness's default agent reaches every connected tool, so a node needing a capability the
// preferred type is not known to serve goes to the default instead.
const pickType = (type, requires = []) => (requires.every(x => (CAPS[type] || []).includes(x)) ? type : undefined)

async function read(n) {
  const r = await opJson('read', n.id, `${TM} task get ${q(n.id)} --json`)
  return r && r.exit === 0 && r.data && typeof r.data.status === 'string' ? r.data : null
}

// Only this workflow's own lease is released: tm refuses the --agent form for anyone else's, so a
// release racing another dispatcher's claim changes nothing.
async function release(n, trail, why) {
  const r = await op('release', n.id, `${TM} task release ${q(n.id)} --agent ${agentName(n)} >/dev/null 2>&1`, /^__EXIT:(\d+)\s*$/)
  const refused = r && r.exit === 0 ? '' : ' (tm refused the release or the runner failed; tm run sweep returns the step once its lease expires)'
  trail.push(`released, ${why}${refused}`)
}

// A claim is a write, so it is never retried: a second attempt would find the first one's lease.
async function start(n, trail) {
  const r = await opJson('start', n.id,
    `${TM} task start ${q(n.id)} --agent ${agentName(n)} --session ${q(SESSION)} --worktree-dir ${q(WT)} --json`,
    { attempts: 1 })
  if (!r) {
    await release(n, trail, 'its claim could not be read')
    return null
  }
  const d = r.data || {}
  if (r.exit === 3 || d.action === 'blocked') return { ...d, action: 'blocked', reason: d.reason || 'tm task start exited 3' }
  if (r.exit !== 0 || !ACTIONS.includes(d.action)) {
    trail.push(`claim refused: ${clip(r.text)}`)
    return null
  }
  if (!Object.hasOwn(MODEL_ID, d.model)) {
    trail.push(`claim names the model family ${clip(d.model)}, which args.models does not map`)
    await release(n, trail, 'no model id was known for it')
    return null
  }
  return d
}

async function job(n, id, wait) {
  const r = await opJson('job', n.id, `${TM} job status ${q(id)}${wait ? ` --wait ${WAIT_SECONDS}` : ''}`, { long: wait })
  return r && r.exit === 0 && r.data && typeof r.data.state === 'string' ? r.data : null
}

// --agent names the lease this workflow took, so tm refuses the close once that lease is gone.
const close = (n, c) => ({
  implement: `${TM} task complete ${n.id} --agent ${agentName(n)}`,
  fix: `${TM} task complete ${n.id} --agent ${agentName(n)}`,
  review: `${TM} task review ${n.id} --agent ${agentName(n)} --approve or --reject (tm refuses either until your findings are in its :review section)`,
  merge: `${TM} job resume ${c.job}`,
  sync: `${TM} job resume ${c.job}`,
})[c.action]

const head = (n, c, fam, role) => {
  const repo = (c.repos || n.repos || [])[0]
  const preamble = PREAMBLE[repo] ?? PREAMBLE.default ?? ''
  const rules = RULES_DIR
    ? `\nRules: read every file in ${RULES_DIR} yourself before your first edit or probe; path-scoped rules do not load in a worktree.`
    : ''
  // `{task}` in a lane becomes this node's id, so a runner that tags its jobs can name them after it.
  const lane = (GATE_LANE[repo] ?? GATE_LANE.default ?? '').replaceAll('{task}', n.id)
  const gate = lane ? `\nGate lane: ${lane}` : ''
  const requires = n.requires || []
  const needs = requires.length ? `\nRequires: ${requires.join(', ')}; load the tools that provide it with ToolSearch before the first step that needs them.` : ''
  const blocked = c.action in CLAIMED
    ? `, or, when something outside this step must happen first, with ${TM} task release ${n.id} --agent ${agentName(n)} --blocked naming the edge, decision or condition it waits on`
    : ''
  return `${preamble ? preamble + '\n' : ''}tm-task: ${n.id}
Model: ${MODEL_ID[fam]}
The tm-wave workflow claimed this ${c.action} step for you: never run tm task start, and never claim or release any other node. Read tm guide ${role} and follow it from the step after its claim. Close the step with ${close(n, c)}${blocked}.
Brief: tm render ${n.id} --view subagent${rules}${gate}${needs}
Sections: before any tm section set, tm section get the same key and append to it. Code, comments, test names, log lines and fixtures never name a ruling, task, review or round.`
}

async function work(n, c, s, trail) {
  const fam = c.model
  const repos = c.repos || []
  let type = AGENT_TYPE[repos[0]]
  let body
  if (c.action === 'review') {
    const again = s.status === 'FIXED'
    const container = n.kind !== 'task'
    type = REVIEWER[container ? 'container' : again ? 'rereview' : 'task']
    const base = !c.base || c.base === 'main' ? 'origin/main' : c.base
    body = again
      ? `Scope: every finding in tm section ${n.id}:review not yet recorded as closed, against the fix commits on ${c.branch} and the fixer's latest :report entry, and, when the last landing failed, the failure its latest :merge entry names. Establish each closure by mutation.`
      : `Scope: the whole diff of ${c.branch} from its base, in each repository it touched: ${repos.map(r => `git -C ${ROOT}/${r} diff ${base}...${c.branch}`).join('; ')}.${container ? ' This is a container review: read what is true only between its children, and every child tm render lists as rejected by its own review.' : ''}`
    body += `\nFindings: append numbered findings to tm section ${n.id}:review, one line each; write it even when nothing is open, saying so.`
  } else {
    // A container's step spans repositories, and tm cuts one worktree of its branch in each.
    const trees = Object.entries(c.worktrees || {})
    const where = trees.length > 1
      ? `Worktrees, one per repository, each on branch ${c.branch}, based on ${c.base}: ${trees.map(([r, p]) => `${r} at ${p}`).join('; ')}. Work only there, and never cd in a Bash command.`
      : `Worktree: ${c.worktree} — branch ${c.branch}, based on ${c.base}. Work only there, and never cd in a Bash command.`
    body = c.action === 'fix'
      ? `${where}\nFindings: ${s.outcome === 'merge_failed' ? `the landing failure the latest entry of tm section get ${n.id}:merge records` : `every finding in tm section get ${n.id}:review not recorded as closed`}. Fix each one, commit on the branch, and answer each by number in an appended :report entry.`
      : `${where}\nReport: append to tm section ${n.id}:report before your last commit.`
  }
  const r = await agent(`${head(n, c, fam, c.action)}\n${body}`,
    { label: `${c.action}:${n.id}`, phase: WORK_PHASE[c.action], model: fam, agentType: pickType(type, n.requires) })
  trail.push(`${c.action} on ${fam}: ${r === null ? 'the agent died' : clip(r)}`)
  const after = await read(n)
  if (!after || after.status === CLAIMED[c.action]) await release(n, trail, `the ${c.action} step was left open`)
}

async function land(n, c, trail) {
  for (let poll = 0; poll < MAX_POLLS; poll++) {
    const j = await job(n, c.job, true)
    if (!j) return trail.push(`${c.action}: job ${c.job} status could not be read`)
    if (j.state === 'running') continue
    if (j.state !== 'needs_agent') return trail.push(`${c.action}: ${j.state}${j.result ? ` — ${clip(j.result)}` : ''}`)
    // A blocked claim that started a sync names no model; the next claim hands the stopped job over
    // with the family tm routes it to.
    if (!c.model) return trail.push(`${c.action} stopped for an agent; the next claim hands it over`)
    const fam = c.model
    const r = await agent(`${head(n, c, fam, 'merge')}
Job: ${c.job}, a ${j.kind} of ${j.repo} onto ${j.target}, stopped at ${j.step}: ${clip(j.result)}
Worktree: ${j.worktree} — the one tm built for this job. Work only there, and never cd in a Bash command.
Output: ${TM} job status ${c.job} prints what stopped it.
When this node's own change is at fault, close with ${TM} job resume ${c.job} --own-defect "<the finding, one line>" instead.`,
      { label: `${c.action}-agent:${n.id}`, phase: 'Land agent', model: fam, agentType: pickType(undefined, n.requires) })
    trail.push(`${c.action} agent: ${r === null ? 'died' : clip(r)}`)
    const k = await job(n, c.job, false)
    if (!k || k.state === 'needs_agent') {
      if (c.action === 'merge') await release(n, trail, 'the landing agent left the job stopped')
      else trail.push(`sync ${c.job} left stopped; discovery offers it again`)
      return
    }
  }
  trail.push(`${c.action}: job ${c.job} still running after ${MAX_POLLS} waits; a later tick picks it up`)
}

async function run(n) {
  const trail = []
  const end = status => ({ id: n.id, status, trail })
  let s = await read(n)
  for (let step = 0; step < MAX_STEPS; step++) {
    if (!s) return end('unreadable')
    if (TERMINAL.has(s.status) || !s.next_action) return end(s.status)
    if (s.next_action === 'merge' && HOLD_MERGE.has(n.id)) return trail.push('merge held by args.holdMerge'), end(s.status)
    const c = await start(n, trail)
    if (!c) return end(s.status)
    if (c.action === 'blocked' && !c.job) return trail.push(`blocked: ${c.reason}`), end('blocked')
    if (c.action === 'blocked') await land(n, { ...c, action: 'sync' }, trail)
    else if (c.action === 'merge' || c.action === 'sync') await land(n, c, trail)
    else await work(n, c, s, trail)
    s = await read(n)
  }
  log(`${n.id}: ${MAX_STEPS} steps without reaching COMPLETED, FAILED or blocked; tm task get ${n.id} shows where it stands`)
  return end(s ? s.status : 'unreadable')
}

phase('Discover')
let plan = null
for (let i = 1; i <= 3 && !plan; i++) {
  plan = await discover(i)
  if (!plan) log(`discovery attempt ${i} failed its exit code or checksum`)
}
if (!plan) throw new Error('discovery failed three times; nothing was claimed')
// A run holds at most min(16, CPUs - 2) agents at once, so a batch past that sits queued and unclaimed.
if (A.maxBatch && plan.chosen.length > A.maxBatch) {
  log(`maxBatch ${A.maxBatch}: left for the next run: ${plan.chosen.slice(A.maxBatch).map(n => n.id).join(', ')}`)
  plan.chosen = plan.chosen.slice(0, A.maxBatch)
}
plan.held.forEach(h => log(`held: ${h}`))
log(`wave: ${plan.chosen.map(n => `${n.id}@${n.action}/${n.model}`).join(', ') || 'nothing claimable'}; ${plan.waiting_for_slot} waiting for a slot`)

const results = await pipeline(plan.chosen, n => run(n))
return { results: results.filter(Boolean), held: plan.held, waitingForSlot: plan.waiting_for_slot }
```

- [ ] **Step 4: Run the tests and the gates**

```
node --test <worktree>/tests/workflow/; echo $?
uv run --directory <worktree> pytest -q; echo $?
uv run --directory <worktree> ruff check; echo $?
uv run --directory <worktree> ruff format --check; echo $?
uv run --directory <worktree> mypy; echo $?
```

Expected: every command exits 0; `node --test` reports 37 tests, 37 pass. No Python file changed, so the Python gates only confirm nothing else moved.

- [ ] **Step 5: Commit**

```
git -C <worktree> add workflows/tm-wave.js tests/workflow/harness.mjs tests/workflow/tm-wave.test.mjs && git -C <worktree> commit -m "feat(tm-wave): run each node through one loop of tm task start and the step it names"
```


### Task 23: The seven guides rewritten for the lifecycle

**Spec:** §10.3, §2, §3, §4, §5, §6, §7, §9.2
**Files:**
- Modify: `src/taskmanager/guides/overview.md` (rewritten whole)
- Modify: `src/taskmanager/guides/implement.md` (rewritten whole)
- Modify: `src/taskmanager/guides/review.md` (rewritten whole)
- Modify: `src/taskmanager/guides/fix.md` (rewritten whole)
- Modify: `src/taskmanager/guides/merge.md` (rewritten whole)
- Modify: `src/taskmanager/guides/dispatch.md` (rewritten whole)
- Modify: `src/taskmanager/guides/plan.md` (rewritten whole, worked example included)
- Modify: `tests/unit/test_guides.py` (adds `RETIRED`, `CLOSING_VERBS`, `WORKFLOW` and four tests)
- Modify: `tests/unit/test_guides_examples.py` (replaces `test_next_offers_the_task_the_guide_says_it_offers_first` with `test_discovery_offers_the_tasks_the_guide_says_it_offers_first`; adds `test_the_example_imports_the_documented_flags`)
- Test: `tests/unit/test_guides.py`, `tests/unit/test_guides_examples.py`

**Interfaces:**
- Consumes: every CLI verb and flag of Task 18, `tm task start --worktree-dir`, `--agent` on `tm task complete`, `review` and `release`, and `tm job status --wait` among them; the claim printout `{action, reason, model, job, repos, branch, base, worktree, worktrees}` with `model` a family (Task 13); `tm task get` fields with `next_action` (Task 18); the `tm wave discover` payload (Task 16); the `tm-wave` argument set of Task 22; import fields `review`, `fix`, `merge`, `requires`, `conditions`, `land_order` of Task 17.
- Produces: the guide text `tm guide <role>` prints, which Task 24's skills route to, Task 25's addendum is appended to, and Task 26's agents follow; the cutover runbook in `tm guide overview`.

`test_guide_command_exists` already resolves every `tm ...` the guides show against the typer app, flags included, so a guide naming a verb Task 18 did not build reddens here. The new tests pin what that check cannot: that no retired vocabulary survives, that each role guide shows the verb closing its step, that the overview carries the cycle and the runbook, and that the dispatch guide documents every argument `tm-wave` reads.

What changes, guide by guide and section by section:

**overview.md**

| Current section | Becomes |
|:--|:--|
| title, blurb, topics paragraph | kept; the blurb adds "landed" |
| Lifecycle (`NOT_STARTED`→`READY` table of `tm run start`/`stop`, `IN_FLIGHT`, rolled-up `state`, `BLOCKED_BY_LEASE`, `AWAITING_DECISION`, 300 s leases and sweep) | **The cycle**: the stored-status diagram, the `tm task start` action table with the verb closing each step, the three flags; **Plans and specs**: rollup, empty-diff completion, container branches never pushed, container review, `land_order`; **What a node waits on**: edges by landing level, decisions with effects, conditions, and the display-status table; **Counters and FAILED**; **Repairs** (`reopen`, `reset`, `defer`, `abandon`, `supersede`) |
| Reading | kept, `tm next` replaced by `tm wave discover`, `tm run list` gains jobs, `tm job status` added, `tm task get` accepts containers |
| Files and worktrees (`--worktree` on `tm run start`, id inferred in a worktree) | folded into The cycle: the claim prints `branch`, `base` and `worktree`; the section goes |
| Verification | kept; adds the landing's `--ref <target>`, `"$TM_VERIFY_REF"` and the literal-`origin/main` refusal on `merge: parent` |
| Writing | kept; decisions raised by `tm task release --blocked --decision` inside a step; imports refuse flag-rule breaks and cycles |
| Messages you will meet | rewritten: `tm run start` refusals go, `action: blocked` (exit 3), the pre-lifecycle refusal and the validation refusal come in |
| (new) Moving an estate to this version | the nine-step runbook of spec §9.2 |

**implement.md**

| Current section | Becomes |
|:--|:--|
| 1. Read the brief / 2. Claim it (`tm run start --worktree`, refusal table) | 1. The claim (`tm task start`, the printed step, `blocked`; skipped when the workflow claimed) then 2. Read the brief, which adds the `:reopen` note |
| 3. Work in that worktree | kept; "tm lands the branch"; rewriting allowed only before the first review |
| 4. Keep the lease alive (`tm run heartbeat`) | `tm task heartbeat <task-id>`, `lease_ttl.implement` |
| 5. Verify | kept; `--ref tm/<task-id>` first, `"$TM_VERIFY_REF"` |
| Waiting on something that takes time | kept; "over 30 minutes" becomes a condition release instead of `--status NOT_STARTED` |
| Something only the owner can answer (`tm decision add` then release `NOT_STARTED`) | Something outside this step must happen first: the three `--blocked` forms, and plain release as a counted failure |
| 6. Hand it off (`tm run stop --status WAITING_REVIEW`) / 7. Report | 6. Report (written to `:report` first) then 7. Close the step, `tm task complete` |
| Never | the status list goes; "never hold two steps", "tm lands the branch" |

**review.md**

| Current section | Becomes |
|:--|:--|
| 1. Take the task (`tm task list --status WAITING_REVIEW`, `tm run start`, `--worktree` refusal) | 1. The claim: `action: review`, locks nothing, cuts nothing; a node may be a container; a non-empty `:review` means a re-review |
| 2. Read what was asked for | kept; a container's brief lists children whose own review rejected |
| 3. Read the branch (`main..tm/<id>`) | diff from the printed `base` in each repository; first review, container review and re-review scopes |
| 4. Run the checks | kept, `--ref <branch>`; the empty set exits 2 |
| 5. Write the findings | kept; written to `:review` with the section commands, earlier findings marked closed or open |
| 6. Release it (`WAITING_FIXES` / `WAITING_MERGE`) | 6. Close the step: `tm task review` with `--approve` or `--reject` and an optional `--verdict`, the unchanged-`:review` refusal, the three outcomes, a decision via `--blocked --decision` |
| 7. Report, Never | kept; "never leave the task in `REVIEWING`" becomes "never close without `:review`" |

**fix.md**

| Current section | Becomes |
|:--|:--|
| 1. Take the task and the findings (`WAITING_FIXES`, `tm run start --worktree`) | 1. The claim, and what it answers: `outcome` `reject` reads `:review`, `merge_failed` reads `:merge` |
| 2. Fix exactly the findings | kept; adds the not-my-files landing red (release `--blocked --depends`) and the owner-decision release |
| 3. Verify and keep the lease alive | `tm task heartbeat`, `--ref tm/<node-id>` |
| 4. Hand it back (`tm run stop --status WAITING_REVIEW`) / 5. Report | 4. Report, then 5. Close the step with `tm task complete`; a merge fix is re-reviewed without spending a round |
| Never | the status list goes; "never approve, land or complete a node" |

**merge.md**

| Current section | Becomes |
|:--|:--|
| the whole procedure (claim `WAITING_MERGE`, build the merge worktree, push by refspec, `cat-file -e`, `tm verify run`, `tm run stop --status COMPLETED --remove-worktree`) | **What tm already did**: the seven landing steps tm runs itself, and the table of the five `needs_agent` reasons with what each needs |
| — | 1. Take the job (`tm task start` on the stopped node, `tm job status`) |
| — | 2. Work in the job's worktree (resolve and commit there, push nothing) |
| — | 3. Resume: `tm job resume`, `--own-defect`, `--push`; append to `:merge` |
| Waiting on something that takes time | kept; "over 30 minutes" leaves the job stopped |
| 7. Report / Never | 4. Report; Never keeps no-force, no local-`main`, adds no `--push` on an unattributed red |

**dispatch.md**

| Current section | Becomes |
|:--|:--|
| 1. Ask what can start (`tm next`) / 2. Read the wave's files | 2. What is claimable: `tm wave discover`, the claimability order, disjointness and the migration chain |
| 3. Route to a model | 3. Models are tm's: the routing table |
| 4. Write the prompt | folded into 1: the workflow writes every brief |
| 5. Watch, do not poll | 5. What is in flight: `tm run list`, sweep, `STALE`, `WAITING_MERGE_AGENT` |
| 6. Move it through the cadence (per-status table) / 7. Chain the stages as one workflow | 1. The mechanism is the `tm-wave` workflow: the per-node loop, the argument table, the three rules still binding the caller; the fix cap, review-hash and hold rules go because tm enforces them |
| 8. When the plan changes (`tm run stop --status DEFERRED/ABANDONED/COMPLETED`) | 7. When the plan changes: `defer`, `abandon`, `supersede`, `reopen`, `reset` |
| Decisions are the owner's queue | 6. When something fails: `FAILED`, red-`main` and stranded-dependent decisions |
| 9. Write rulings down / 10. Escalate rather than repeat | 8 kept; escalation becomes "change what made it fail before `investigate`" |
| (new) 4. Holds are edges, decisions and conditions | `holdMerge` is the only dispatcher hold |

**plan.md**

| Current section | Becomes |
|:--|:--|
| 1. Write the document | the field table gains `review`, `fix`, `merge`, `requires`, `conditions`, `land_order`; `status` is never written |
| 2. Sections and frontmatter | `deferral` leaves the author's keys (tm writes it); `external_blockers` goes; `review_models` is now read by tm |
| 3. Models | adds `requires` |
| (new) 4. Where a node lands | the flags, the two common shapes, the `review`-without-`fix` rule, `land_order` |
| 4. Dependencies | 5. What a node waits on: edges by landing, decisions, conditions, the cycle refusal |
| 5. Verifications | 6.; `"$TM_VERIFY_REF"` and the literal-`origin/main` refusal |
| 6. Size a task / 7. Provision the review and the merge | 7. kept / 8. Write the review into the node: a landing precondition is an edge or a condition |
| 8. Import it, then read it back / 9. Amend | 9. / 10.; `tm wave discover` replaces `tm next`; the flag, condition and `merge`-change amendments |
| Worked example | the plan reviews once with `review`/`fix` on, tasks land on `parent`, `NOTIFY-EMAIL-TEMPLATES` keeps a review with `fix: false`, and the API task's `external_blockers` becomes a `landing` condition |

- [ ] **Step 1: Write the failing test**

In `tests/unit/test_guides.py`, add `from pathlib import Path` to the imports, and append:

```python
RETIRED = (
    "tm run start",
    "tm run stop",
    "tm run heartbeat",
    "NOT_STARTED",
    "WAITING_FIXES",
    "IN_FLIGHT",
    ":hold",
    "external_blockers",
    "--release",
    "maxFixRounds",
)

CLOSING_VERBS = {
    "implement": "tm task complete <task-id> --agent <name>",
    "fix": "tm task complete <node-id> --agent <name>",
    "review": "tm task review <node-id> --agent <name> --approve",
    "merge": "tm job resume <job>",
}

WORKFLOW = Path(__file__).resolve().parents[2] / "workflows" / "tm-wave.js"


@pytest.mark.parametrize("topic", _topics())
def test_guide_carries_no_retired_lifecycle_vocabulary(topic: str) -> None:
    text = _guide_text(topic)
    assert [word for word in RETIRED if word in text] == []


@pytest.mark.parametrize("topic,verb", sorted(CLOSING_VERBS.items()))
def test_role_guide_shows_the_verb_that_closes_its_step(topic: str, verb: str) -> None:
    assert verb in _guide_text(topic)


def test_overview_carries_the_cycle_and_the_cutover_runbook() -> None:
    text = _guide_text("overview")
    for needle in (
        "READY ──claim──▶ IMPLEMENTING",
        "MERGING ──landed and verified──▶ COMPLETED",
        "## Moving an estate to this version",
        "`tm init --archive`",
        "`tm export <export dir>`",
    ):
        assert needle in text, needle


def test_dispatch_guide_names_every_argument_tm_wave_reads() -> None:
    read = set(re.findall(r"\bA\.([A-Za-z]+)", WORKFLOW.read_text(encoding="utf-8")))
    assert read, "no argument found in the workflow script"
    text = _guide_text("dispatch")
    assert sorted(arg for arg in read if f"`{arg}`" not in text) == []
```

In `tests/unit/test_guides_examples.py`, add `import json` to the imports, delete `test_next_offers_the_task_the_guide_says_it_offers_first`, and add:

```python
def test_the_example_imports_the_documented_flags(tmp_path: Path) -> None:
    root = _imported_root(tmp_path)

    def flags(node_id: str) -> tuple[object, object, object]:
        doc = _yaml("task", "get", node_id, "--yaml", "-C", str(root))
        return doc["review"], doc["fix"], doc["merge"]

    assert flags("NOTIFY-EMAIL") == (True, True, "main")
    assert flags("NOTIFY-EMAIL-SENDER") == (True, True, "parent")
    assert flags("NOTIFY-EMAIL-TEMPLATES") == (True, False, "parent")
    assert flags("NOTIFY-EMAIL-API") == (True, True, "parent")


def test_discovery_offers_the_tasks_the_guide_says_it_offers_first(tmp_path: Path) -> None:
    root = _imported_root(tmp_path)

    output = _run(
        "wave",
        "discover",
        "--session",
        "guide-example",
        "--slots",
        "5",
        "--max-strong",
        "5",
        "-C",
        str(root),
    )

    payload = json.loads(output.splitlines()[0])
    assert sorted((node["id"], node["action"]) for node in payload["chosen"]) == [
        ("NOTIFY-EMAIL-SENDER", "implement"),
        ("NOTIFY-EMAIL-TEMPLATES", "implement"),
    ]
```

- [ ] **Step 2: Run it and watch it fail**

```
uv run --directory <worktree> pytest tests/unit/test_guides.py tests/unit/test_guides_examples.py -q; echo $?
```

Expected: exit 1. `test_guide_carries_no_retired_lifecycle_vocabulary` fails for all seven topics: Task 18 deleted the lines naming the verbs it removed, but every guide still says `NOT_STARTED`, `WAITING_FIXES`, `IN_FLIGHT` or (in `plan`) `external_blockers`; all four `test_role_guide_shows_the_verb_that_closes_its_step` cases, `test_overview_carries_the_cycle_and_the_cutover_runbook` and `test_dispatch_guide_names_every_argument_tm_wave_reads` (missing at least `capabilities`, `reviewerTypes`) fail; `test_the_example_imports_the_documented_flags` fails with `(True, True, 'main') != (True, False, 'parent')` on `NOTIFY-EMAIL-TEMPLATES` or the plan's own `(False, False, 'main')`, because the current example sets no flags; `test_discovery_offers_the_tasks_the_guide_says_it_offers_first` passes already: it asserts the same first pair the deleted `tm next` test did, through the command a dispatcher now uses, and must stay green once the example changes.

- [ ] **Step 3: Implement**

Replace each guide whole with the text below.

`src/taskmanager/guides/overview.md`:

````markdown
# How TaskManager works

The task database is the only record of what is planned, claimed, built, landed and finished; read it with `tm`, change it with `tm`, and never open the sqlite files.

Run `tm guide` for the topics and `tm guide <topic>` for the one that matches your role. Each prints the built-in guidance, then this project's own addendum when it has one.

## The cycle

Every task, plan and spec stores one status: its position in this cycle. Everything else a reader sees (blocked, waiting, stale) is worked out from the graph, the leases and the jobs, and is never stored.

```
READY ──claim──▶ IMPLEMENTING ──complete──▶ IMPLEMENTED
IMPLEMENTED ──claim, review on──▶ REVIEWING ──review──▶ REVIEWED
IMPLEMENTED ──claim, review off──▶ MERGING
REVIEWED ──claim: approved, or rejected with fix off──▶ MERGING
REVIEWED ──claim: rejected with fix on, or a failed landing──▶ FIXING ──complete──▶ FIXED
FIXED ──claim──▶ REVIEWING
MERGING ──landed and verified──▶ COMPLETED
MERGING ──own defect, attempts left, fix on──▶ REVIEWED, outcome merge_failed
a cap reached ──▶ FAILED ──the owner answers investigate──▶ reopened
set by a verb, from any stable status but COMPLETED: DEFERRED, ABANDONED, SUPERSEDED
```

`tm task start <id> --agent <name> --session <id> --worktree-dir <dir> --yaml` is the only claim. It reads the stored status and the node's flags, decides the next step, takes the lease under `<name>` and prints the step; `--worktree-dir` is where an implement or fix worktree is cut, in place of the estate's `worktree_dir`:

| `action` | Claimed from | Locks the declared files | Closed with |
|:--|:--|:--|:--|
| `implement` | `READY` | yes | `tm task complete <id> --agent <name>` |
| `review` | `IMPLEMENTED`, `FIXED` | no | `tm task review <id> --agent <name> --approve`, or `--reject` |
| `fix` | `REVIEWED` | yes | `tm task complete <id> --agent <name>` |
| `merge` | `IMPLEMENTED`, `REVIEWED` | no | nothing: tm lands it as a job, and hands the job to an agent only when it stops |
| `sync` | a container branch behind its base | no | nothing: tm merges it as a job, and hands the job to an agent only when it stops |
| `blocked` | nothing is claimed; exit 3 | | the printed `reason` says what it waits on |

The printout also names the `model` family the step runs on (`haiku`, `sonnet`, `opus` or `fable`), the `repos` it touches, the `branch`, its `base` (`main`, or the container branch it lands on), and for `implement` and `fix` the `worktree`, with `worktrees` naming one per repository when a plan or spec spans several. `--agent <name>` on a closing verb is refused unless the live lease is that agent's, so a step closes only for whoever holds it. A step that cannot go on ends with `tm task release <id> --agent <name> --blocked` naming what it now waits on; `tm task release <id> --agent <name>` alone ends it as a failed step. `tm task heartbeat <id>` renews the lease, which lasts `lease_ttl.<action>` seconds.

Three flags on every node decide the path through the cycle: `review` (a review follows implement), `fix` (this node fixes its own rejections; it needs `review`) and `merge` (`main`, or `parent` to land on the branch of the plan or spec above it). A task has `review` and `fix` on and lands on `main` unless its plan says otherwise; a plan or spec has both off.

## Plans and specs

A plan or spec is a container: it is never implemented itself, and its status follows its children in the same write that moves any of them. Once every counted child (not `DEFERRED`, `ABANDONED` or `SUPERSEDED`) is `COMPLETED`, the container is `IMPLEMENTED`, and from there it is reviewed, fixed and landed like a task when its flags say so. A container whose branches hold nothing their base lacks completes at once: there is nothing to review.

Children with `merge: parent` land on the container's branch, `tm/<container-id>`, a local branch in each repository they touch that is never pushed. Their code reaches `main` only when the container lands. A container review reads the whole branch, so it sees what is true only between children; its fix works on that branch. A container that touched several repositories lands them one at a time, in its `land_order`.

## What a node waits on

- **An edge.** `depends_on` points at another node or at a decision. An edge to a node is satisfied once that node's code has landed on a branch this node builds on: the dependency itself when both land on the same container, the container above it when it lands there, and so on up to `main`. An edge on a container holds every node under it. When a dependency has landed further up than this node's base, the claim first syncs it down into each container branch in between.
- **A decision.** A question only the owner answers, raised with `tm decision add` or opened by tm itself. The node waits until it is answered or withdrawn, and an answer may carry an effect on every node it blocks: abandon, defer, reopen, or drop the edge.
- **A condition.** A state outside the corpus plus a shell command that exits 0 once it holds (`tm task condition add`). A `claim` condition holds every claim; a `landing` condition holds only the landing. tm runs it before the claim and caches the result for `condition_ttl` seconds.

`tm task get <id> --yaml` prints the stored `status`, the `next_action` a claim would take now (null while the node is mid-step, or has no next step), and the `state` a reader sees, first match wins:

| Display | When |
|:--|:--|
| the status itself | an exit, `COMPLETED` or `FAILED`, or an `-ING` status with a live lease |
| `WAITING_MERGE_AGENT` | `MERGING` with a landing job stopped for an agent |
| `STALE` | an `-ING` status whose lease expired or is gone; `tm run sweep` returns it |
| `AWAITING_DECISION` | an edge to an open decision |
| `BLOCKED_BY_TASK` | an unsatisfied edge |
| `BLOCKED_BY_CONDITION` | an unmet condition for its next step |
| `BLOCKED_BY_SYNC` | a sync its claim needs is running or waiting |
| `BLOCKED_BY_LEASE` | its next step's files are locked by another lease |
| `IMPLEMENTING` | a container at `READY` with a child already past `READY` |
| `WAITING_REVIEW` | `IMPLEMENTED` with `review` on, or `FIXED` |
| `WAITING_FIX` | `REVIEWED` after a rejection it fixes, or after a failed landing |
| `WAITING_MERGE` | `IMPLEMENTED` with `review` off, or `REVIEWED` to be landed |
| `READY` | `READY` |

## Counters and FAILED

Nothing loops. Three counters end every repeated failure at `FAILED`:

- **Fix rounds**, `max_fix_rounds` (2 for a task, 3 for a container). A rejection with no round left fails the node.
- **Own-defect landing failures**, `max_merge_attempts` (3): a red on the node's own verifications, failures the merged tip adds, a red verification after landing, or a defect an agent recorded. A conflict, a race or a red `main` never counts here.
- **Failed steps**, `max_step_failures` (3): a plain release, an expired lease, a landing job an agent left unresolved.

Entering `FAILED` opens a decision on the node, "abandon, or investigate?", carrying the reason and the relevant `:review` or `:merge` excerpt. `investigate` reopens the node with the answer as its note. When a node that others depend on is deferred, abandoned or failed, one decision asks what to do with those dependents: drop the edge, defer them, or abandon them.

## Repairs

- `tm task reopen <id> --note "<why>"`: `FAILED`, `DEFERRED` or `ABANDONED` back into the cycle, counters and outcome cleared, the note kept in `:reopen`. The branch is kept for the next implementer; `--new-branch` starts clean instead. Refused while an open decision blocks the node.
- `tm task reset <id> --to IMPLEMENTED --note "<why>"`: a ledgered repair to `READY`, `IMPLEMENTED`, `REVIEWED`, `FIXED` or `COMPLETED` (with `--outcome` for `REVIEWED`). A reset to `COMPLETED` is refused unless the branch is already on its target and the node's verifications pass there.
- `tm task defer <id> --note "<why>"` and `tm task abandon <id> --note "<why>"`: from any stable status but `COMPLETED`; the note goes to `:deferral` or `:abandonment`.
- `tm task supersede <old-id> <new-id> --transfer-blocks all`: the old node is `SUPERSEDED` and every dependent points at the new one.

None of these touches a node mid-step: wait for the step to end, or stop it.

## Reading

- `tm wave discover --session <id> --slots <n> --max-strong <n>`: every claimable node with its next action and model, a JSON line then a `__CHECK` line.
- `tm task list --yaml` and `tm task get <id> --yaml` are the compact reads (`--json` is the same data); `tm task get` accepts a plan or spec id too.
- `tm render <id> --view subagent` is a node's full brief; `--recursive` (`-r`) adds every child. `tm section get <id>:<key>` reads one section.
- `tm run list --yaml` is every lease, locked file and job in flight; `tm job status <job>` is one job, and `tm job status <job> --wait 540` blocks until it leaves `running` or the seconds pass.
- `tm verify run <id> --ref <ref>` runs the node's checks and exits 1 if one fails.
- `tm audit list --target <id>` is the event log of everything done to a node.
- `tm search <words>` finds nodes by text, or by meaning once `tm index` has run.
- `tm config list` shows every setting with its effective value and source; `tm config set <key> <value>` changes one.
- `tm decision list --status open` is the owner's queue; `tm decision get <id> --yaml` is one decision with its options.

## Verification

`tm verify run` reads `file_exists`, `file_absent`, `symbol_signature` and `ast_export` from a ref of the node's `target_repo` — `origin/main` by default, fetched first, never a working tree — and runs every `test_command` from the project root with that ref exported as `TM_VERIFY_REF`. `--ref tm/<id>` reads the node's own branch, with no fetch. A landing runs the node's verifications with `--ref` set to its target, so a `test_command` reads `"$TM_VERIFY_REF"` rather than naming a branch; tm refuses a `test_command` that names `origin/main` itself on a node landing on its parent.

`No verifications to run` exits 2: nothing was checked, and that is not a pass. A landing with nothing to verify lands and says so in `:merge`.

## Writing

Add new work with `tm import` (an import that names an unknown id, breaks a flag rule or closes a cycle writes nothing and exits 1, printing the cycle as a path). Change a node with `tm task update`, `tm section set`, `tm verify add`, `tm task depends` and `tm task condition add`. `tm export <dir>` writes the whole database as sorted text for version control.

A question nobody in the loop can answer is not a reason to stop and ask: release the step with `tm task release <id> --agent <name> --blocked --decision "<question>" --option "a|Do X|why" --option "b|Do Y|why" --recommend a`, or, outside a step, `tm decision add "<question>" --option "a|Label" --recommend a --blocks <id>`. `tm decision answer <id> --option a` or `tm decision withdraw <id>` lets the node move again.

## Messages you will meet

| Message | Exit | What to do |
|:--|:--|:--|
| `Invalid value: no .taskmanager at <dir>: pass -C, set TM_ROOT, or run tm init there` | 2 | you are outside the project; pass `-C <project root>` |
| `this directory holds a pre-lifecycle estate: run tm init --archive ...` | 1 | the owner's cutover has not run here; stop and report, never run `tm init` yourself |
| `action: blocked` with a `reason` | 3 | nothing was claimed; the reason names the edge, decision, condition, sync or lease |
| `import refused, nothing written: unknown ids [...]` | 1 | the document depends on ids that do not exist yet |
| a refusal naming a field and a fix | 1 | a flag rule or the cycle check refused the write; nothing was written |
| `Section '<key>' not found on node '<id>'` | 1 | list the node's sections with `tm section get <id>` |

## Moving an estate to this version

An estate written by 0.2 or earlier is not migrated. It is archived, and only the work still in flight is re-imported. Each step runs on the owner's go-ahead:

1. Stop every dispatcher, and wait until `tm run list --yaml` shows no lease.
2. With the old version still installed, `tm export <export dir>` and commit the export: that snapshot is the archive of record.
3. Author the re-import: `tm import` documents holding only the specs, plans, tasks and open decisions still in flight, each with its sections, verifications, edges, flags, conditions and `requires` in this version's shape. Completed work is not re-imported, and edges to it are dropped, because it is on `main`. A task whose branch `tm/<id>` already exists resumes on it at its next implement.
4. Install this version of the plugin and of `tm`, and replace any copy of the old `tm-wave` script a session keeps.
5. `tm init --archive` moves the old files to `.taskmanager/archive-<timestamp>/` and creates the new estate. Configure the landing gates as one YAML value, `tm config set repos '{<repo>: {gates: {main: {command: <template>, junit: <glob>, timeout: <seconds>}}}}'`, and the landing order with `tm config set repo_order '[<repo>, ...]'`.
6. `tm import --format yaml -f <document>` for each document; `tm wave discover --session <id> --slots <n> --max-strong <n>` shows what is claimable, for the owner to check.
7. Apply the project's prepared guide addendum and rules.
8. `tm export <export dir>` and commit.
9. Resume dispatching with the new `tm-wave`.
````

`src/taskmanager/guides/implement.md`:

````markdown
# Implementing a task

For the agent that builds a task `tm task start` claimed for `implement`, and closes the step with the work committed on the task's own branch.

## 1. The claim

A dispatcher's workflow usually claims the step for you and says so in the prompt: then skip to step 2, and never run `tm task start` yourself. On your own, claim it:

```
tm task start <task-id> --agent <name> --session <id> --worktree-dir <dir> --yaml
```

```yaml
action: implement
model: sonnet
repos: [backend]
branch: tm/<task-id>
base: main
worktree: <dir>/backend-<task-id>
worktrees: {backend: <dir>/backend-<task-id>}
```

`model` is the family the step runs on; `<name>` is the agent the lease is held under, and every verb that closes or releases the step passes it back with `--agent <name>`. When the workflow claimed for you, its prompt names both. The claim locks every path the task declares until the step closes. The branch is cut from `base` in the task's own `target_repo`, with no upstream: `main` means that repository's `origin/main`, and a branch name means the container branch the task lands on. A task with no `target_repo` is refused. Any other `action` is another role's step: read that role's guide instead.

`action: blocked` exits 3 and writes nothing. Its `reason` names what the task waits on — an edge, a decision, a condition, a sync, or a lease holding one of its files. Report it and start nothing.

## 2. Read the brief

```
tm render <task-id> --view subagent
```

That is the whole assignment: the task's frontmatter, its parent's context, its sections and its verifications. Then, only as needed:

- `tm task get <task-id> --yaml` — status, `next_action`, flags, `target_repo`, `depends_on`, `declared_files`, verifications, conditions, lease.
- `tm section get <task-id>:<key>` — one section; `tm section get <task-id>` prints them all.

A task that was reopened carries a `:reopen` note and the earlier `:review`, and its branch still holds the earlier work: read both, and decide what to keep.

## 3. Work in that worktree and nowhere else

Every read, edit, command and commit happens under the printed worktree path. Check the prefix of each path you edit, not just its basename: the same file exists in the project's own checkout. Commit on the branch with an explicit pathspec. Do not merge and do not push: tm lands the branch. You may rewrite your own branch until its first review, never after.

## 4. Keep the lease alive

```
tm task heartbeat <task-id>
```

An implement lease lasts `lease_ttl.implement` seconds (three hours by default). A refused heartbeat means the lease is gone and the step was swept back to `READY`: stop editing and report.

## 5. Verify

```
tm verify run <task-id> --ref tm/<task-id>
```

A table of the task's checks against your branch, exit 1 if any failed. What each type asserts:

- `file_exists` / `file_absent` — the path is there, or is not.
- `symbol_signature` — a `def`, `async def` or `class` of that name parses in that file.
- `ast_export` — that name is in the file's `__all__`, or is a public top-level `def`/`class`.
- `test_command` — the command runs in a shell from the project root; exit 0 passes.
- `codegraph_query` — passes with `codegraph CLI not installed; skipped` where that tool is absent.

The path checks read the ref, never your worktree, so without `--ref` they read `origin/main` and stay red until the task lands. A `test_command` reads the same ref from `TM_VERIFY_REF`; when tm lands the task it sets that to the landing target, so write `"$TM_VERIFY_REF"` into the command rather than a branch name. `No verifications to run.` exits 2: a task with no checks has not passed anything, and that is worth a line in your report.

## Waiting on something that takes time

A gate, a build, an external state change — pick by duration, because duration is what you actually know:

| The wait is | Do |
|:--|:--|
| under 10 minutes | a single foreground call to completion: the tool's own blocking `wait` where one exists, else `timeout 540 bash -c 'until <cond>; do sleep 15; done'; echo $?` |
| 10–30 minutes | a Monitor with a filter matching every terminal state, not only success |
| over 30 minutes | it is a condition, not a wait: release the step naming it (below) |

Never end your turn to wait on a background run "until notified." A background command's completion notification reaches you only while you are still working — ending your turn is what loses it, and nothing resumes you afterward.

## Something outside this step must happen first

Release the step and name what it waits on, in one call; the task returns to `READY` with its branch and worktree intact, and becomes claimable again the moment the named thing clears:

- Another node must land first: `tm task release <task-id> --agent <name> --blocked --depends <other-id>`.
- Only the owner can answer: `tm task release <task-id> --agent <name> --blocked --decision "<question>" --option "a|Do X|why" --option "b|Do Y|why" --recommend a`. Name the options you considered and the one you recommend.
- A state outside the corpus: `tm task release <task-id> --agent <name> --blocked --needs "<what must hold>" --command "<a command that exits 0 once it holds>"`.

`--blocked` with nothing named is refused. `tm task release <task-id> --agent <name>` alone is a failed step, counted towards `FAILED`: use it only when you cannot go on and nothing names why, and say why in the report. A release or close refused for `--agent` means the lease is no longer yours: stop and report.

## 6. Report

Write the report before closing the step, appending to what is there:

```
tm section get <task-id>:report
tm section set <task-id>:report --file <path> --header "## Report"
```

The branch, the commits you made, the `tm verify run` exit code and which rows failed, and anything you could not do. Where the brief contradicts the tree — a file that does not exist, an interface that already differs — record the discrepancy, implement against the tree, and keep going.

## 7. Close the step

```
tm task complete <task-id> --agent <name>
```

After the last commit and the report, on every path that finished the work. The task moves to `IMPLEMENTED`, and what follows — a review, or the landing — is tm's to choose. Leave the worktree in place: a fix round reuses it.

## Never

- Never open, copy or edit anything under `.taskmanager/`; the CLI is the only writer.
- Never claim, close or release a step you do not hold, and never hold two.
- Never edit outside your worktree, and never merge or push anything: tm lands the branch.
- Never change a task's definition to match what you built.
- Never end your turn with the step open: close it, or release it naming why.
````

`src/taskmanager/guides/review.md`:

````markdown
# Reviewing a node

For the agent that reads a node's branch after `tm task start` claimed its `review`, writes the findings on the node, and approves or rejects it.

## 1. The claim

A dispatcher's workflow usually claims the step for you and says so in the prompt: then skip to step 2. On your own:

```
tm task start <node-id> --agent <name> --session <id> --yaml
```

`action: review` sets the node to `REVIEWING`, names the `model` family, the `repos` it touched (several for a plan or spec), its `branch` and `base`, and locks nothing: a review writes no code, so it never holds a sibling out. It cuts no worktree. The lease is held under `<name>`, which the verbs closing the step pass back with `--agent`; a workflow's prompt names it. `action: blocked` (exit 3) claimed nothing; report its `reason`.

The node may be a task, or a plan or spec whose children have all landed on its branch. If `tm section get <node-id>:review` already holds findings, this review checks a fix (step 3).

## 2. Read what was asked for

```
tm render <node-id> --view subagent
```

That is the brief the implementer was given, and the only standard you review against. `tm task get <node-id> --yaml` names the node's `target_repo`, `declared_files` and flags. For a plan or spec the brief also lists every child whose own review rejected, with its `:review`: those children landed on this branch unfixed, and this review is where their findings get fixed.

## 3. Read the branch

In each repository the claim's `repos` names, `<base>` is `origin/main` when `base` is `main`, and the container branch otherwise:

```
git -C <repo> log --oneline <base>..<branch>
git -C <repo> diff <base>...<branch>
git -C <repo> show <branch>:<path>
```

Read only. Do not check the branch out in the project's own checkout, do not edit a file, do not run a formatter. If you must execute the code, do it in a worktree of your own making, outside the project, and say so in the review.

- **A first review** reads the whole diff against the brief.
- **A plan's or spec's review** reads the whole branch too, for what is true only between its children: a producer nobody calls, a column only ever written as null, two halves that do not join.
- **A review after a fix** checks every finding in `:review` not yet recorded as closed against the fix commits and the fixer's latest `:report` entry, and, when the last landing failed, the failure the latest `:merge` entry names. Establish each closure by making it fail.

## 4. Run the checks

```
tm verify run <node-id> --ref <branch>
```

The path checks read that ref directly, with no fetch, so a check against the unmerged branch is real evidence. Each `test_command` sees the same ref as `TM_VERIFY_REF`. Exit 1 names each failing row; `No verifications to run.` exits 2 and proves nothing — a task with no checks is itself a finding.

## 5. Write the findings

Append them to the node before you close the step:

```
tm section get <node-id>:review
tm section set <node-id>:review --file <path> --header "## Review"
```

One line per defect: the file, the symbol or line, and what breaks. No summary, no praise, no restatement of the task, no severity essay. Number them, because the fix answers them by number, and record each earlier finding as closed or still open. Cite a symbol rather than a line number wherever you can. Nothing to say is a valid review: say it in one line.

## 6. Close the step

```
tm task review <node-id> --agent <name> --approve
tm task review <node-id> --agent <name> --reject --verdict "<one line>"
```

tm refuses either one while the `:review` section is unchanged since your claim: the findings are the record, and a verdict without them leaves a fixer nothing to fix. It refuses it too when the live lease is not `<name>`'s: the step is no longer yours, so stop and report. `--verdict` is a free-text line shown beside the status; it never decides anything.

- **Approved**: the node goes on to its landing.
- **Rejected, and the node fixes its own rejections**: it goes to a fix round while it has rounds left; with none left it is `FAILED`, and the owner decides.
- **Rejected, and the node does not fix** (`fix` off): it lands its branch on its parent unfixed, and the parent's review is where the findings are fixed.

A judgement call the brief itself cannot settle — not a defect, a genuine open question — is raised rather than left in prose: `tm task release <node-id> --agent <name> --blocked --decision "<question>" --option "a|Do X|why" --recommend a`. Run `tm task heartbeat <node-id>` if the read runs long.

## 7. Report

The verdict, the numbered findings, the `tm verify run` exit code with the rows that failed, and what you did not cover.

## Never

- Never edit code, tests, fixtures or configuration — not even a one-line fix you can see.
- Never approve with a finding still open, and never merge or push anything.
- Never close the step without writing `:review` first.
- Never file a finding you have not read in the branch's own content.
````

`src/taskmanager/guides/fix.md`:

````markdown
# Fixing a node

For the agent that closes a rejection's findings, or a failed landing's defect, on the node's own branch after `tm task start` claimed its `fix`.

## 1. The claim, and what it answers

A dispatcher's workflow usually claims the step for you and says so in the prompt: then skip to the next paragraph. On your own:

```
tm task start <node-id> --agent <name> --session <id> --worktree-dir <dir> --yaml
```

`action: fix` sets the node to `FIXING`, locks its declared files again, names the `model` family, and hands back a worktree of the node's existing branch with every earlier commit on it: `worktree` for a task, and `worktrees`, one per repository, for a plan or spec whose branch spans several. Nothing is cut from `origin/main` a second time. The lease is held under `<name>`, which the verbs closing the step pass back with `--agent`; a workflow's prompt names it.

What you answer is the node's `outcome`, printed by `tm task get <node-id> --yaml`:

- `reject` — every finding in `tm section get <node-id>:review` not yet recorded as closed, from any round.
- `merge_failed` — the landing's own defect, in the latest entry of `tm section get <node-id>:merge`: a red on the node's own verifications, failures the merged tip added to the gate, a red verification after landing, or what an agent recorded.

Read the brief again only where a finding disputes it: `tm render <node-id> --view subagent`.

## 2. Fix exactly the findings

One commit per finding, or one commit naming them all — either way on the node's branch, inside your worktree, with an explicit pathspec. Do not rewrite the branch's earlier commits: the review cites them.

- A finding you can close, close.
- A finding you judge wrong is answered in the report with the evidence that refutes it, and the code is left alone. It is never silently skipped.
- Anything else you notice goes in the report, not in the diff. Widening the scope is what spends the next round.
- A landing failure whose red lies in files this node does not declare belongs to the node that caused it: find or file that node, then `tm task release <node-id> --agent <name> --blocked --depends <that-node>`, and say so in the report.
- A finding whose fix needs a call only the owner can make: `tm task release <node-id> --agent <name> --blocked --decision "<question>" --option "a|Do X|why" --recommend a`, and answer it in the report as raised, not closed.

## 3. Verify and keep the lease alive

```
tm task heartbeat <node-id>
tm verify run <node-id> --ref tm/<node-id>
```

Exit 1 names the failing rows. `No verifications to run.` exits 2 and is no evidence at all.

## 4. Report

Answer each finding by its number, with the commit that closed it or the words "not done" and why, appended to the report:

```
tm section get <node-id>:report
tm section set <node-id>:report --file <path> --header "## Report"
```

Then the `tm verify run` exit code, and anything you found and did not touch.

## 5. Close the step

```
tm task complete <node-id> --agent <name>
```

On every path that finished the round, including one where a finding was contested rather than closed. The node moves to `FIXED` and is reviewed again. A fix answering a failed landing is checked by that review but does not spend a fix round. Leave the worktree in place.

## Never

- Never approve, land or complete a node: the reviewer approves, and tm lands.
- Never edit outside your worktree, and never merge or push anything.
- Never force-push, rebase or squash the node's branch.
- Never change a test so a finding stops firing; close the finding the test names.
- Never end your turn with the step open: close it, or release it naming why.
````

`src/taskmanager/guides/merge.md`:

````markdown
# Resolving a stopped landing

For the agent handed a landing or a sync that tm stopped because it needs judgement: a conflict, a red it cannot attribute, a refused push or a missing gate.

## What tm already did

tm lands every node itself, as a job, one repository at a time in the node's landing order:

1. **Already landed?** A branch that is already on its target, or adds nothing to it, skips straight to the verification.
2. **Landing conditions.** An unmet `landing` condition ends the job; the node waits on it.
3. **Build.** A fresh merge worktree cut from the target (`origin/main`, or the container branch), and `git merge --no-ff` of the node's branch with a subject naming the node.
4. **Gate.** On a container branch: the node's own verifications, then the repository's `parent` gate when one is configured. On `main`: the repository's `main` gate, and when it is red, the same gate on the untouched target, cached per target commit, to attribute the red.
5. **Push.** To `main`: re-read the remote, merge it in again and re-gate if it moved, push `HEAD:main`, never force. To a container branch: a compare-and-swap of the local ref.
6. **Verify.** The node's verifications at the target; a red here is the node's own defect.
7. **Complete.** The merge worktree is removed and the node is `COMPLETED`.

A sync merges a target into a container branch the same way, under that branch's lock. Either job stops at `needs_agent` only for something a rule cannot settle, and that is the one moment an agent is dispatched:

| Stopped for | What it needs |
|:--|:--|
| `conflict` | resolve the merge in the job's worktree and commit it, then resume |
| `unattributed` | the tip and the untouched target are both red and no report names the failures: read both outputs; if the tip adds a failure, record an own defect, and if it adds none, resume with `--push` |
| `push_failed` | three refused pushes: find out why (a permission, a protection rule, a hook) and report it; resume only once the cause is gone, and never force |
| `no gate` | the repository has no `main` gate configured: report it; the owner configures `repos.<repo>.gates.main` |
| a red sync | the container's `parent` gate went red after the target was merged in: fix it in the job's worktree, commit, resume |

## 1. Take the job

A dispatcher's workflow usually hands you the job and says so in the prompt. On your own, claim the stopped node, which hands you the job and its lease:

```
tm task start <node-id> --agent <name> --session <id> --yaml
tm job status <job>
```

The job's `step`, `result`, `repo`, `target` and `worktree` say where it stopped, why, and where the merge in progress is. Run `tm task heartbeat <node-id>` if the work runs long.

## 2. Work in the job's worktree

It is tm's own merge worktree, holding the merge in progress. Resolve there, commit there with an explicit pathspec, and push nothing: tm pushes when it resumes. Never rebase, never force, and never merge in the project's own checkout or on its local `main`.

## 3. Resume

```
tm job resume <job>
tm job resume <job> --own-defect "<the finding, one line>"
tm job resume <job> --push
```

- Plain `resume` continues from where the job stopped, gates the tip again and lands it.
- `--own-defect` records that the node's own change is at fault. The node goes back for a fix with your finding in `:merge`, or to `FAILED` when it has no landing attempt left or does not fix its own defects.
- `--push` is for `unattributed` only, after you proved the tip adds no failure the target lacks.

A resume runs on in the background. `tm job status <job> --wait 540` blocks until the job leaves `running` or nine minutes pass, and prints where it went; a job stopped again is still yours.

Append what you found and did to the node's `:merge` section (`tm section get <node-id>:merge` first, then `tm section set <node-id>:merge --file <path>`).

## Waiting on something that takes time

| The wait is | Do |
|:--|:--|
| under 10 minutes | a single foreground call to completion: `timeout 540 bash -c 'until <cond>; do sleep 15; done'; echo $?` |
| 10–30 minutes | a Monitor with a filter matching every terminal state, not only success |
| over 30 minutes | leave the job stopped and report what is pending |

A job you leave unresolved counts as a failed step for the node, and its lease returns to the job for the next agent. Never end your turn to wait on a background run "until notified."

## 4. Report

The job, where it stopped, what you changed and in which commit, which resume you ran and what `tm job status <job>` printed after it.

## Never

- Never push, force-push, rebase or reset anything; tm pushes.
- Never merge into, commit on, or push the project checkout's local `main`.
- Never set a node `COMPLETED` or resume with `--push` on a red you have not attributed.
- Never review or fix the node's own code while resolving its landing: that is an own defect, recorded with `--own-defect`.
- Never open or edit anything under `.taskmanager/`.
````

`src/taskmanager/guides/dispatch.md`:

````markdown
# Dispatching

For the session manager: run the `tm-wave` workflow, which asks tm what each node needs next and hands every step to the model tm names, and keep no record of what is in flight that tm does not already hold.

## 1. The mechanism is the `tm-wave` workflow

The plugin ships the dispatcher as a workflow script, `workflows/tm-wave.js`, run by name — `Workflow({name: 'tm-wave', args: {...}})` — or by path. One run is one tick:

1. `tm wave discover` chooses a batch: every claimable node, within the session's slots, file-disjoint within the batch.
2. Each chosen node runs its own loop, and no node waits for a sibling. `tm task get` reads where it stands and its `next_action`; `tm task start --worktree-dir <worktreeDir>` claims the next step and names its action and model family; the workflow dispatches the agent that action needs on the model id `models` maps that family to; the agent does the step and closes it with its guide's verb, passing the lease's agent name with `--agent`. For a `merge` or a `sync`, the workflow waits on `tm job status <job> --wait 540` instead, and dispatches an agent only when the job stops for one.
3. The loop repeats until the node is `COMPLETED`, `FAILED`, or blocked on something outside the step.

A step the agent leaves open, or an agent that dies, is released by the workflow with `tm task release <id> --agent <its lease's agent>` as a failed step, which tm counts; tm refuses that release once another claim holds the node. A claim naming a family `models` does not map is released the same way, never run on a guess. tm counts fix rounds and landing failures too, so the workflow keeps no counter and no hold of its own.

Arguments, of which `session` and `worktreeDir` are required:

| Argument | What it is |
|:--|:--|
| `session` | this dispatching session's name; every lease carries it |
| `worktreeDir` | where implement and fix worktrees are cut, passed to every claim as `tm task start --worktree-dir` |
| `specs` | spec ids to discover under; omitted means every spec and every node with no spec |
| `slots` | agents this session may hold at once (default 9) |
| `maxStrong` | of those, how many may run on `opus` or `fable` (default 5) |
| `maxBatch` | the most nodes one tick takes on; the rest wait for the next tick |
| `exclude` | node ids this tick never chooses |
| `holdMerge` | node ids whose landing this tick never starts: they are implemented, reviewed and fixed, and wait at their merge step for the owner |
| `root` | the tm root every command runs from; defaults to the session's own directory |
| `tm` | the `tm` executable every command runs; defaults to the one on `PATH` |
| `agentTypes` | repository → agent type for implement and fix |
| `reviewerTypes` | `task`, `rereview` and `container` → agent type for a first review, a review after a fix, and a plan's or spec's review |
| `capabilities` | agent type → the `requires` values it can serve; a node needing one no preferred type serves goes to the default agent |
| `preamble` | repository → a line prepended to every brief for it, plus a `default` key |
| `rulesDir` | a directory every agent reads before its first edit |
| `gateLane` | where suites and gates run, as text or repository → text with a `default` key; `{task}` becomes the node id |
| `models` | the family tm names on a claim → the model id every brief's `Model:` line carries; each family given overrides the current Claude id, and a family missing from both is released unrun |

A run holds at most `min(16, CPUs - 2)` agents at once, so a batch larger than that queues inside the run; `maxBatch` keeps it from sitting claimed but idle.

What still binds you when you run it:

- **Never hand-roll the loop.** Chaining single dispatches by hand appoints the session as the scheduler and rebuilds, a notification at a time, the barrier a per-node loop removes.
- **The workflow claims; agents close.** It runs every `tm task start` before it dispatches, so no agent explores before its claim and no second dispatcher sends a second agent. Each agent closes its own step with the verb its guide names.
- **Resume re-reads tm.** A resumed run replays cached agent results, but the loop re-reads each node with `tm task get` before claiming, so it enters at the node's real next step rather than where the cache left it.

## 2. What is claimable

```
tm wave discover --session <id> --slots <n> --max-strong <n>
tm wave discover --spec <spec-id> --session <id> --slots <n> --max-strong <n> --exclude <node-id>
```

Every node of every kind whose next step can be claimed now, with that step and its model, plus landings and syncs stopped for an agent; a JSON line, then `__CHECK n=<chosen> h=<djb2>`. A node is claimable when none of these holds, checked in this order: it is mid-step or its job is running; an edge (its own, or one on a container above it) points at an open decision; an edge is unsatisfied; a `claim` condition is unmet; a sync its claim needs is running or waiting; its next step would lock a file another lease holds; its status has no next step.

Within a batch no two nodes declare the same file. Across a repository, a node writing a migration holds every other migration writer back until it has landed on `main`, except siblings building on its own container branch. Two nodes touching one schema, one generated file or one shared table are not disjoint whatever their file lists say: give them an edge.

## 3. Models are tm's

`tm task start` names the model family; the workflow runs the step on the id `models` maps it to.

| Step | Family |
|:--|:--|
| implement | the cheapest family in `acceptable_models` |
| review of a task | the family of `review_models` when set, else `sonnet` |
| review of a plan or spec | the family of `review_models` when set, else the strongest in `acceptable_models`, never below `opus` |
| fix after a rejection, rounds 1 and 2 | the implement family when it is `opus` or `fable`, else `sonnet` |
| fix after a rejection, round 3 onward (containers only) | the strongest in `acceptable_models`, never below `opus` |
| fix after a failed landing; a landing or sync agent | `sonnet` |

A list you disagree with is a plan defect: fix it with `tm task update <id> --models a,b` and say so, never dispatch around it.

## 4. Holds are edges, decisions and conditions

A node waits only on something named: an edge (`tm task depends <id> --add <other-id>`), a decision (`tm decision add ... --blocks <id>`), or a condition (`tm task condition add <id> --needs "<what>" --command "<check>"`). `holdMerge` is the one hold the dispatcher keeps, and it is policy for this tick, not state on the node: a landing that is irreversible, deploys, or is the owner's call is listed there and reported, and the node's earlier steps still run.

## 5. What is in flight

```
tm run list --yaml
tm run sweep
tm job status <job>
tm job status <job> --wait 540
```

`tm run list` is every lease, locked file and job; nothing else needs writing down, and never a session's own agent list. A lease past its TTL reads `STALE` until `tm run sweep` returns the step to where it was claimed from and counts a failed step. A landing stopped for an agent reads `WAITING_MERGE_AGENT`, and discovery offers it.

## 6. When something fails

A node that spends its fix rounds, its landing attempts or its failed steps is `FAILED`, and tm opens a decision on it; a `main` that stays red under parked landings for an hour opens one too; and a node deferred, abandoned or failed while others depend on it opens one on those dependents. `tm decision list --status open` is the owner's queue, not yours: do not answer a decision on the owner's behalf, and do not chase an agent to withdraw one.

Re-running a failed step unchanged is not a fix. Before anyone answers `investigate`, change what made it fail: correct the brief with `tm section set`, widen `acceptable_models`, or split the node.

## 7. When the plan changes

- **Defer**: `tm task defer <id> --note "<why>"`. The note is kept in `:deferral`.
- **Abandon**: `tm task abandon <id> --note "<why>"`.
- **Supersede**: `tm task supersede <old-id> <new-id> --transfer-blocks all` sets the old node `SUPERSEDED` and re-points every dependent at the new one, which must already exist.
- **Reopen**: `tm task reopen <id> --note "<why>"` puts a failed, deferred or abandoned node back into the cycle.
- **Repair**: `tm task reset <id> --to READY --note "<why>"`, ledgered, for a stored status that is wrong.

A dependent of a deferred, abandoned or failed node is never stranded silently: the decision tm opens on it asks whether to drop the edge, defer it, or abandon it.

## 8. Write rulings down where the work is

A ruling, a constraint, a hazard or an answer the next agent will need goes on the node it applies to:

```
tm section set <task-id>:context --file <path> --header "## Context"
tm section set <plan-id>:context --file <path>
```

A plan's `context` reaches every task's brief. Anything a `tm` command can answer — what is claimed, what is ready, who holds a file — is not written down at all.

## Never

- Never dispatch a step `tm task start` did not claim, and never two agents on one node.
- Never paste, summarise or extend the rendered brief; `tm render <id> --view subagent` is the brief.
- Never keep a second record of what is in flight, a fix counter, or a hold outside `holdMerge`.
- Never answer a decision that is the owner's.
- Never re-run a failed node without changing what made it fail.
````

`src/taskmanager/guides/plan.md`:

````markdown
# Writing a plan

For the agent authoring new work: turn an intent into a document `tm import` accepts, so every node is claimable, lands where it should and is provable without asking its author anything.

## 1. Write the document

One YAML file (`--format json` and `markdown` parse the same shape). A `spec` is the standing intent, a `plan` is a shippable slice of it, a `task` is one agent's unit of work.

```
spec:     one, optional; omit it to add plans under a spec already in the database
plans:    a list; each carries its own tasks
  tasks:  a list; also allowed at top level, where the tasks hang off the spec
```

Every node takes the fields below; `verifications` and `target_repo` act only on tasks, and `land_order` only on plans and specs.

| Field | What it means |
|:--|:--|
| `id` | Yours to choose and permanent. Prefix a child with its parent (`NOTIFY`, `NOTIFY-EMAIL`, `NOTIFY-EMAIL-SENDER`): it reads as a path and sorts with its siblings. |
| `title` | One line, what the change is. |
| `priority` | 1-100, default 50. Raise it to break a tie in discovery, not to express importance. |
| `ordinal` | Display order; the position in the list when omitted. |
| `target_repo` | The directory, under the tm root, the task's branch is cut in. Per node and **not inherited**: set it on every task. A task without one cannot be implemented. |
| `acceptable_models` | Real model ids. See §3. |
| `review`, `fix`, `merge` | How the node reaches `main`. See §4. |
| `requires` | Capabilities the agent needs, such as `figma`. See §3. |
| `conditions` | States outside the corpus the node waits on, each with its command. See §5. |
| `land_order` | On a plan or spec: the order its repositories land in. See §4. |
| `frontmatter` | Free keys, rendered into the brief's frontmatter. See §2. |
| `sections` | A map of key to text, in the order they appear in the brief. See §2. |
| `depends_on` | Ids this node cannot start before. See §5. |
| `verifications` | Machine checks. See §6. |

Never write `kind`: position decides it. Never write `status`: a new node is `READY`, and only the claim and its verbs move it. Any key a document omits keeps the node's current value on a re-import.

## 2. Sections and frontmatter

Section keys, in this order where they apply: `objective` (what is true when it is done), `acceptance` (the checks a reviewer runs, one per line), `body` (how, where the how is not obvious), `context` (what the agent would otherwise have to go and read), `owed` (what this node deliberately leaves open), `attestation` (a signed claim for work no assertion can measure). The header is `## <Key>` unless a section is given as `{header, content, ordinal}` instead of plain text. tm writes `:report`, `:review`, `:merge`, `:reopen` and `:deferral` itself, or its agents do.

Put shared context on the **plan**. Only the direct parent's `context` and `overview` reach `tm render <id> --view subagent`; a spec's sections never do.

Frontmatter keys the estate reads:

- `declared_files`: every repo-relative path the task will create or modify. This is what an implement or fix claim locks and what discovery keeps disjoint, so an unlisted file is a collision nobody sees and a listed file nobody touches holds a task out of a wave for nothing. Tests count.
- `review_models`: who reviews this node, as model ids; tm routes the review to that family. A task that is cheap to write can be expensive to check, and a migration, a row-level security policy or a crypto boundary is reviewed on the strongest model whatever wrote it.
- `soft_depends_on`: ids this task builds against a stub until they land. It creates no edge and holds nothing back; it tells the implementer what the stub is for.
- `gate_lane`: where this task's own gate can run. It is a claim about this task's files, so its author owns it.

## 3. Models and capabilities

`acceptable_models` decides the implement route: tm takes the cheapest family listed. An **empty list means every model**, not the strongest one. Reviews, fixes and landings follow from it and from `review_models`, as `tm guide dispatch` lists; tm prints the model family with every claim.

`requires` names what the agent must be able to reach — `figma` for a node read against a design frame, say. The dispatcher routes the node to an agent type that serves it, or to the default agent, which reaches every connected tool.

## 4. Where a node lands: `review`, `fix`, `merge`

- `merge: main` (the default) cuts the node's branch from `origin/main` and lands it on `main`.
- `merge: parent` cuts it from the branch of the plan or spec above it, `tm/<parent-id>`, and lands it there. It reaches `main` only when that parent lands. A spec cannot land on a parent.
- `review` puts a review after implement; `fix` makes this node fix its own rejections, and needs `review`. A task has both on unless the document says otherwise; a plan or spec has both off.

Two shapes cover most work:

- **Each task reviewed and landed alone.** Tasks keep the defaults and land on `main`; the plan is a grouping only.
- **One review for the whole plan.** Tasks carry `merge: parent`, the plan carries `review: true` and `fix: true`, and the plan's review reads its whole branch once every task has landed on it. A task may keep its own review with `fix: false`: a rejection then lands on the plan's branch unfixed, and the plan's review is where it gets fixed. tm refuses `review` without `fix` anywhere else, because a rejection nobody below fixes must land where a review above will see it.

A plan or spec that touched several repositories lands them one at a time, in `land_order` (else the project's `repo_order`). A container whose tasks changed nothing completes without a review.

## 5. What a node waits on

`depends_on` holds real blockers as explicit ids, each a node or a decision. There is no wildcard and no status gate: an edge to a node is satisfied once that node's code has landed on a branch this node builds on, and an edge on a plan holds every task in it. An id that is neither in the document nor already in the database refuses the whole import, and nothing is written:

```
import refused, nothing written: unknown ids ['DOES-NOT-EXIST']
```

A question that gates several unrelated nodes at once is a decision (`tm decision add "<question>" --option "a|Do X" --recommend a --blocks t1,t2`) that each of them depends on, never a transcript of its terms in a section: an edge onto a decision lifts the moment it is answered, and every node waiting on it reads `AWAITING_DECISION` meanwhile.

A state outside the corpus — an approval, a credential, a tag another repository must cut — is a **condition**: `{needs, command, stage}`, where `command` exits 0 once the state holds and `stage` is `claim` (the default: nothing starts until it holds) or `landing` (the work proceeds, and only the landing waits). A condition with no runnable command is refused: that is a decision.

Every write is checked against a graph of each node's start, implement and landing, and refused when it would close a cycle, with the cycle printed as a path:

```
A.start ← P.landed ← P.implemented ← A.landed ← A.start
```

The usual cause is an edge from a container to its own child, or between two children that land on each other's parent.

## 6. Verifications

A verification is the node's own proof. `file_exists`, `file_absent`, `symbol_signature` and `ast_export` take a repo-relative path in `target_path` and count towards `declared_files`. `test_command` puts a label in `target_path` and the command in `expected_pattern`, and counts towards nothing. `codegraph_query` **passes when `codegraph` is not installed**, so it never proves anything on its own.

A good check exits 0 exactly when this task's own deliverable exists: content this change makes true, never a path another task creates and never the whole suite. Write it, then run it once against the open task and watch it fail:

```
$ tm verify run NOTIFY-EMAIL-SENDER --ref tm/NOTIFY-EMAIL-SENDER
symbol_signature  src/notify/email/sender.py  FAILED  File src/notify/email/sender.py missing
```

The path checks read a ref of the task's `target_repo` (`origin/main` by default, fetched first; `--ref` names another) and never a working tree. `test_command` runs from the tm root with that ref in `TM_VERIFY_REF`, and a landing sets it to the landing target — the parent's branch for `merge: parent` — so a command reads `"$TM_VERIFY_REF"` instead of naming `origin/main`; tm refuses one that names `origin/main` itself on a task landing on its parent.

## 7. Size a task to one agent

One agent, one sitting, one branch: an objective of a sentence, acceptance of a handful of lines, and a `declared_files` list short enough that no sibling wants any of it. Two objectives joined by "and" are two tasks.

## 8. Write the review into the node

- **`acceptance` is the review's brief.** One check per line, each one a reviewer can actually run, and each about *this* node's deliverable. "The suite is green" is not a check.
- **`review_models` is who runs it.** Set it wherever checking is harder than writing.
- **A landing precondition is an edge or a condition, never a sentence.** Written into `acceptance` it reads as a review check, passes review, and is found only when the landing is already under way.

## 9. Import it, then read it back

```
tm import --format yaml -f plan.yaml
tm task list --yaml
tm wave discover --session <id> --slots 5 --max-strong 2
tm render <task-id> --view subagent
tm export docs/tm/
```

Read the brief before dispatching anyone: a section you meant to write and did not is invisible in the database and obvious here.

## 10. Amend it

While you are still authoring, re-importing the same document is safe: it refuses before writing when an id is unknown, a flag rule breaks or a cycle closes, and adds no verification twice. A task whose `verifications` a document states has exactly those afterwards. Once work has started, change it in place:

```
tm task update <id> --title "<title>" --priority 60 --models a,b --repo <dir>
tm task update <id> --review --no-fix --merge parent --requires figma
tm task update <plan-id> --land-order core,web
tm task depends <id> --add a,b --remove c
tm task condition add <id> --needs "<what must hold>" --command "<check>" --stage landing
tm task condition remove <id> <idx>
tm section set <id>:<key> --file <path> --header "## Context"
tm verify add <id> --type test_command --target api-suite --pattern "pytest tests/notify/test_api.py -q"
tm verify list <id>
tm verify remove <id> <verification-id>
```

A change to `merge` once the node's branch exists is refused unless that branch was cut from the new target: code cut from a plan's branch must never land on `main` carrying the plan's unreviewed work. Reopen it with `tm task reopen <id> --note "<why>" --new-branch` instead.

## Worked example

Imports as written: every task lands on the plan's branch, the plan is reviewed once, and `tm wave discover` offers `NOTIFY-EMAIL-SENDER` and `NOTIFY-EMAIL-TEMPLATES` first.

<!-- tm:example -->

```yaml
spec:
  id: NOTIFY
  title: Outbound notifications
  sections:
    context: One service sends every outbound message; delivery and retries are its own concern.

plans:
  - id: NOTIFY-EMAIL
    title: Email channel
    priority: 60
    review: true
    fix: true
    sections:
      context: SMTP only. The provider client is injected, so no test opens a socket.
    tasks:
      - id: NOTIFY-EMAIL-SENDER
        title: SMTP sender with retry
        priority: 80
        target_repo: backend
        merge: parent
        acceptable_models: [claude-sonnet-5]
        frontmatter:
          declared_files: [src/notify/email/sender.py, tests/notify/test_sender.py]
        sections:
          objective: "`send(message)` delivers through the injected SMTP client."
          acceptance: |
            - A transient failure is retried three times, then raises `SendFailed`.
            - A permanent failure raises `SendFailed` without a retry.
          body: Take the clock as an argument so the retry test does not wait.
        verifications:
          - type: symbol_signature
            target_path: src/notify/email/sender.py
            expected_pattern: "def send(self, message: Message) -> SendResult"
          - type: test_command
            target_path: sender-suite
            expected_pattern: pytest tests/notify/test_sender.py -q

      - id: NOTIFY-EMAIL-TEMPLATES
        title: Template rendering
        target_repo: backend
        merge: parent
        fix: false
        acceptable_models: [claude-sonnet-5]
        frontmatter:
          declared_files: [src/notify/email/templates.py, tests/notify/test_templates.py]
          soft_depends_on: [NOTIFY-EMAIL-SENDER]
        sections:
          objective: "`render(template_id, payload)` returns a subject and a body."
          acceptance: An unknown template id raises `UnknownTemplate`, never renders an empty span.
          context: Until the sender lands, type the message against a local stub.
        verifications:
          - type: test_command
            target_path: templates-suite
            expected_pattern: pytest tests/notify/test_templates.py -q

      - id: NOTIFY-EMAIL-API
        title: POST /notifications/email
        target_repo: backend
        merge: parent
        depends_on: [NOTIFY-EMAIL-SENDER, NOTIFY-EMAIL-TEMPLATES]
        acceptable_models: [claude-sonnet-5]
        conditions:
          - needs: the provider account is approved for outbound SMTP
            command: test -f /etc/notify/smtp-approved
            stage: landing
        frontmatter:
          declared_files: [src/notify/api/routes.py, tests/notify/test_api.py]
        sections:
          objective: The route renders a template and hands the message to the sender.
          acceptance: A body missing `template_id` answers 422; an unknown template answers 404.
          owed: Rate limiting per recipient is not in this task.
        verifications:
          - type: test_command
            target_path: api-suite
            expected_pattern: pytest tests/notify/test_api.py -q
```

## Never

- Never import the same document twice to change a landed node; amend it in place.
- Never leave a task's `declared_files` unwritten, and never list a file two open tasks both claim.
- Never write a verification that passes before the work starts.
- Never state a dependency or a precondition in prose: an id in `depends_on`, a decision, or a condition with its command.
- Never write `status`.
- Never give a node `review` without `fix` unless it lands on a parent that reviews and fixes.
````

- [ ] **Step 4: Run the tests and the gates**

```
uv run --directory <worktree> pytest tests/unit/test_guides.py tests/unit/test_guides_examples.py -q; echo $?
uv run --directory <worktree> pytest -q; echo $?
uv run --directory <worktree> ruff check; echo $?
uv run --directory <worktree> ruff format --check; echo $?
uv run --directory <worktree> mypy; echo $?
node --test <worktree>/tests/workflow/; echo $?
```

Expected: every command exits 0. `test_guide_command_exists` now collects over a hundred `tm` commands; a red there names the guide, the command and the flag Task 18 does not have, and is closed by correcting the guide to the verb Task 18 built, never by adding a flag here.

- [ ] **Step 5: Commit**

```
git -C <worktree> add src/taskmanager/guides/overview.md src/taskmanager/guides/implement.md src/taskmanager/guides/review.md src/taskmanager/guides/fix.md src/taskmanager/guides/merge.md src/taskmanager/guides/dispatch.md src/taskmanager/guides/plan.md tests/unit/test_guides.py tests/unit/test_guides_examples.py && git -C <worktree> commit -m "docs(guides): teach every role the claim, the verb closing its step and tm's own landing"
```

### Task 24: Skills, commands, README and the 0.3.0 version

**Spec:** §10.3
**Files:**
- Modify: `skills/taskmanager/SKILL.md` (body rewritten: the claim tells the agent its step; the role table keyed by the printed `action`)
- Modify: `src/taskmanager/skills/taskmanager/SKILL.md` (identical to the plugin copy)
- Modify: `skills/dispatcher/SKILL.md` (workflow section and "The shape of it" rewritten for the per-node loop)
- Modify: `src/taskmanager/skills/dispatcher/SKILL.md` (identical to the plugin copy; today it lacks the workflow section)
- Modify: `commands/task.md` (every verb moves to `tm task start`, `heartbeat`, `complete`, `release`)
- Modify: `commands/tm.md` (the no-argument read no longer uses `tm next`)
- Modify: `commands/board.md` (the closing reads no longer use `tm next`)
- Modify: `README.md` (rewritten)
- Modify: `pyproject.toml` (`project.version` to `0.3.0`)
- Modify: `src/taskmanager/__init__.py` (`__version__` to `0.3.0`)
- Modify: `.claude-plugin/plugin.json` (`version`), `.claude-plugin/marketplace.json` (`plugins[0].version`), `gemini-extension.json` (`version`)
- Modify: `uv.lock` (regenerated by `uv lock`)
- Modify: `tests/unit/test_skills.py` (`test_the_bundled_skill_matches_the_plugin_skill` parametrized over both skills; `test_dispatcher_skill_cli_instructions` replaced by `test_dispatcher_skill_runs_waves_through_tm_wave`)
- Modify: `tests/unit/test_guides.py` (extracts `_assert_command_exists`; adds `REPO`, `DOCS`, `_DOC_CASES` and three tests over the shipped docs)
- Modify: `tests/unit/test_package.py` (adds `test_every_manifest_carries_the_release_version`)
- Test: `tests/unit/test_skills.py`, `tests/unit/test_guides.py`, `tests/unit/test_package.py`

**Interfaces:**
- Consumes: the guides of Task 23 (the skills route to them), `RETIRED`, `_tm_commands`, `_tokens`, `_resolve`, `_accepted_flags` in `tests/unit/test_guides.py`, the CLI of Task 18 (`tm task start --worktree-dir`, `--agent` on `tm task complete` and `tm task release`), the model family `tm task start` prints (Task 13).
- Produces: `skills/dispatcher/SKILL.md` at its final text, which Task 25 copies for the owner's global skill; version `0.3.0`, which Task 27 tags.

The doc checks reuse the guide checks: every `tm ...` a skill, command file or the README shows must resolve against the CLI with its flags, and none may carry retired vocabulary. The two skill copies are asserted byte-identical, which today fails for the dispatcher (the bundled copy never got the workflow section).

- [ ] **Step 1: Write the failing test**

`tests/unit/test_skills.py`: replace `test_the_bundled_skill_matches_the_plugin_skill` and `test_dispatcher_skill_cli_instructions` with:

```python
@pytest.mark.parametrize("skill", ["taskmanager", "dispatcher"])
def test_the_bundled_skill_matches_the_plugin_skill(skill: str) -> None:
    """Two copies ship: the plugin reads one and the package the other."""
    plugin = Path(f"skills/{skill}/SKILL.md").read_text(encoding="utf-8")
    bundled = Path(f"src/taskmanager/skills/{skill}/SKILL.md").read_text(encoding="utf-8")
    assert plugin == bundled


def test_dispatcher_skill_runs_waves_through_tm_wave() -> None:
    content = Path("src/taskmanager/skills/dispatcher/SKILL.md").read_text(encoding="utf-8")
    for needle in (
        "tm guide dispatch",
        "tm-wave",
        "tm wave discover",
        "tm task start",
        "holdMerge",
        "acceptable_models",
        "disjoint",
    ):
        assert needle in content, needle
```

and add `import pytest` to its imports.

`tests/unit/test_guides.py`: replace `test_guide_command_exists` with the shared helper and the test below, and append the doc checks:

```python
def _assert_command_exists(where: str, command: str) -> None:
    tokens = _tokens(command)
    if not tokens:
        return
    name, cmd, rest = _resolve(tokens)
    accepted = _accepted_flags(cmd)
    for token in rest:
        if not token.startswith("-"):
            continue
        flag = token.split("=", 1)[0]
        assert flag in accepted, f"`{name}` has no flag {flag} ({where} shows `{command}`)"


@pytest.mark.parametrize("topic,command", _CASES, ids=[f"{t}:{c}" for t, c in _CASES])
def test_guide_command_exists(topic: str, command: str) -> None:
    _assert_command_exists(f"{topic}.md", command)


REPO = Path(__file__).resolve().parents[2]
DOCS = (
    "README.md",
    "agents/tm-op.md",
    "commands/board.md",
    "commands/task.md",
    "commands/tm.md",
    "skills/dispatcher/SKILL.md",
    "skills/taskmanager/SKILL.md",
    "src/taskmanager/skills/dispatcher/SKILL.md",
    "src/taskmanager/skills/taskmanager/SKILL.md",
)


def _doc_text(doc: str) -> str:
    return (REPO / doc).read_text(encoding="utf-8")


_DOC_CASES = [
    (doc, cmd)
    for doc in DOCS
    for cmd in _tm_commands(_doc_text(doc))
    if not _tokens(cmd)[:1] or not _tokens(cmd)[0].startswith("$")
]


def test_the_docs_show_enough_commands_to_be_worth_checking() -> None:
    assert len(_DOC_CASES) >= 15, f"only {len(_DOC_CASES)} `tm` commands found in the shipped docs"


@pytest.mark.parametrize("doc,command", _DOC_CASES, ids=[f"{d}:{c}" for d, c in _DOC_CASES])
def test_doc_command_exists(doc: str, command: str) -> None:
    _assert_command_exists(doc, command)


@pytest.mark.parametrize("doc", DOCS)
def test_doc_carries_no_retired_lifecycle_vocabulary(doc: str) -> None:
    text = _doc_text(doc)
    assert [word for word in RETIRED if word in text] == []
```

`tests/unit/test_package.py`, whole file:

```python
import json
import tomllib
from pathlib import Path

import taskmanager

RELEASE = "0.3.0"


def test_version_defined() -> None:
    assert hasattr(taskmanager, "__version__")
    assert isinstance(taskmanager.__version__, str)


def test_every_manifest_carries_the_release_version() -> None:
    def read_json(path: str) -> dict[str, object]:
        loaded: dict[str, object] = json.loads(Path(path).read_text(encoding="utf-8"))
        return loaded

    marketplace = read_json(".claude-plugin/marketplace.json")["plugins"]
    assert isinstance(marketplace, list)
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]
    versions = {
        "pyproject": project["version"],
        "package": taskmanager.__version__,
        "plugin": read_json(".claude-plugin/plugin.json")["version"],
        "marketplace": [plugin["version"] for plugin in marketplace],
        "gemini": read_json("gemini-extension.json")["version"],
    }
    assert versions == {
        "pyproject": RELEASE,
        "package": RELEASE,
        "plugin": RELEASE,
        "marketplace": [RELEASE],
        "gemini": RELEASE,
    }
```

- [ ] **Step 2: Run it and watch it fail**

```
uv run --directory <worktree> pytest tests/unit/test_skills.py tests/unit/test_guides.py tests/unit/test_package.py -q; echo $?
```

Expected: exit 1, with `test_the_bundled_skill_matches_the_plugin_skill[dispatcher]` (the bundled copy is 31 lines, the plugin's 58), `test_dispatcher_skill_runs_waves_through_tm_wave` (`tm wave discover` missing), `test_doc_carries_no_retired_lifecycle_vocabulary` for the plugin's dispatcher skill (`tm run start`, `tm run stop`) and both taskmanager skills (`WAITING_FIXES`), `test_doc_command_exists` for the plugin dispatcher skill's `tm run start` and `tm run stop` (`` `tm run` has no subcommand 'start' ``, since Task 18 removed both; `commands/task.md` lost its lines for them in Task 18 and passes), and `test_every_manifest_carries_the_release_version` (`'0.2.0' != '0.3.0'` on every key) failing. The Task 23 guide tests stay green.

- [ ] **Step 3: Implement**

`skills/taskmanager/SKILL.md` and `src/taskmanager/skills/taskmanager/SKILL.md`, both exactly:

````markdown
---
name: taskmanager
description: Use when about to claim, build, review, fix, merge or hand off a task, when reading a task's brief or status, or when anyone asks to use TaskManager or the `tm` CLI. Run `tm guide <role>` before the first command.
---

# TaskManager

`tm` is a local CLI over a SQLite task graph: it holds the specs, plans and tasks, claims each step of one with a lease and tells you which step it is, cuts the worktree the work happens in, lands the branch on its parent's branch or on `main`, and verifies it there. It is the only record of what is planned, claimed, built and finished, and the only thing that writes it.

The instructions ship with the tool and are printed on demand, so nothing here repeats them.

## Read your role's guide first

```
tm guide            # the topics, each with one line
tm guide <topic>    # the built-in guidance, then this project's addendum
```

| Doing | Topic |
|:--|:--|
| a step `tm task start` printed as `action: implement` | `tm guide implement` |
| a step printed as `action: review` | `tm guide review` |
| a step printed as `action: fix` | `tm guide fix` |
| a landing or a sync tm stopped for an agent | `tm guide merge` |
| anything else, or first contact with `tm` | `tm guide overview` |

Run it before your first `tm` command, not after: it names the flags, what each refusal means, and the verb that closes your step. A project's own conventions are appended to the same output, so the guide you read is the one that applies here.
````

`skills/dispatcher/SKILL.md` and `src/taskmanager/skills/dispatcher/SKILL.md`, both exactly:

````markdown
---
name: dispatcher
description: Use when planning a wave, dispatching a subagent, routing a task to a model, ordering review, fixes or a merge across a plan's work, or writing new specs, plans and tasks into TaskManager. Run `tm guide dispatch` before the first dispatch of the session.
---

# Dispatching with TaskManager

`tm` holds the specs, plans and tasks, decides the next step of each, locks the files a claim covers, lands finished work and records every state change. It is the only record of what is planned, claimed, built and finished — there is no second tracker, and nothing it can answer is written down anywhere else.

The instructions ship with the tool and are printed on demand, so nothing here repeats them.

## Read the topic for what you are doing

```
tm guide            # the topics, each with one line
tm guide <topic>    # the built-in guidance, then this project's addendum
```

| Doing | Topic |
|:--|:--|
| running waves, routing models, holding landings, handling failures | `tm guide dispatch` |
| authoring new specs, plans and tasks, or amending landed ones | `tm guide plan` |
| first contact with `tm`, or a command you have not met | `tm guide overview` |

Run `tm guide dispatch` before the first dispatch of the session. It names the arguments, what each refusal means and which step tm chooses when; a project's own conventions are appended to the same output, so the guide you read is the one that applies here.

The subagents you dispatch read `tm guide implement`, `review`, `fix` or `merge` themselves. Do not carry their guidance into the prompt.

## The mechanism is the `tm-wave` workflow

Orchestrate the queue with the `Workflow` tool running the plugin's `tm-wave` workflow (`workflows/tm-wave.js`), by name — `Workflow({name: 'tm-wave', args: {session, worktreeDir, ...}})` — or by path. This is required rather than preferred: chaining one-off `Agent` calls by hand appoints the session as the scheduler — the job the script exists to do — and rebuilds, a notification at a time, the barrier a per-node loop removes.

Each tick, `tm wave discover` chooses a batch of claimable nodes, disjoint by their declared files, and each node runs its own loop: `tm task start` names the next step and the model it runs on, the workflow dispatches that agent, the agent closes its step with its guide's verb, and the loop repeats until the node has landed, failed or blocked. tm counts fix rounds, landing failures and failed steps into `FAILED` and opens a decision for the owner, so the workflow keeps no counter and no hold of its own. The one hold a dispatcher keeps is `holdMerge`: nodes whose landing is irreversible, deploys, or is the owner's call are implemented, reviewed and fixed, and wait at their merge step.

If the harness demands a permission for that mechanism which the session cannot grant itself, ask the user for it as a question, with the options and a recommendation, in the same response, and keep doing every part that does not depend on the answer. Falling back to sequential dispatches without saying so is the failure this paragraph exists to name: each dispatch looks correct on its own, so nothing in the transcript shows the mechanism was abandoned.

`session` and `worktreeDir` are required; every other argument is listed in `tm guide dispatch`. A run holds at most `min(16, CPUs - 2)` agents concurrently, so pass `maxBatch` at or under that to keep a large batch from sitting claimed but idle instead of waiting for the next tick.

## The shape of it

`tm wave discover --session <id> --slots <n> --max-strong <n>` offers what is claimable; `acceptable_models` and `review_models` route each step, and tm prints the model family with every claim, which the workflow maps to a model id through its `models` argument; the brief is `tm render <task-id> --view subagent`, with nothing added to it. `tm run list --yaml` is what is in flight. `tm task defer`, `tm task supersede` and `tm task reopen` are how a plan changes shape.
````

`commands/task.md`, whole file:

```markdown
---
description: Claim, inspect, heartbeat, close or release the step you hold on a task.
argument-hint: "[start <task-id> | heartbeat <task-id> | verify <task-id> | complete <task-id> | release <task-id>]"
---

Run `tm guide implement` (or `review`, `fix`, `merge` for your role) before the first command of a
task: it names what each command refuses and the verb that closes your step.

- `start <task-id>`: claim the step tm chooses next for the task under the agent name `<name>`, cut any worktree under `<dir>`, and print the step, its model family and its worktree:
  `tm task start <task-id> --agent <name> --session <id> --worktree-dir <dir> --yaml`
- `heartbeat <task-id>`: renew the lease before it runs out:
  `tm task heartbeat <task-id>`
- `verify <task-id>`: run the task's verifications against its own branch, exit 1 on a failure:
  `tm verify run <task-id> --ref tm/<task-id>`
- `complete <task-id>`: close an implement or fix step, refused unless the lease is `<name>`'s:
  `tm task complete <task-id> --agent <name>`
- `release <task-id>`: hand the step back naming what it waits on:
  `tm task release <task-id> --agent <name> --blocked --depends <other-id>`
- no arguments: show every lease, locked file and job in flight:
  `tm run list --yaml`
```

`commands/tm.md`: replace the line

```
  Run `tm next --limit 5 --yaml` followed by `tm task list --yaml` for the current state.
```

with

```
  Run `tm task list --yaml` followed by `tm run list --yaml` for the current state.
```

`commands/board.md`: replace the last line

```
`tm next -n 5 --yaml`, `tm task list --yaml`, `tm run list --yaml`.
```

with

```
`tm task list --yaml`, `tm run list --yaml`, `tm decision list --status open`.
```

`README.md`, whole file:

````markdown
# TaskManager

A local task tracker for agents: a SQLite graph of specs, plans and tasks that claims each step of a node, lands finished work on its parent's branch or on `main`, and verifies it there.

## Features

- One stored status per node, moved only by `tm task start` and the verb that closes each step; blocked, waiting and stale are worked out, never stored
- `review`, `fix` and `merge` flags on every node in place of separate review and fix tasks, on plans and specs too
- Landings as detached jobs: merge, gate against a cached baseline of the target, push, verify
- Edges, decisions and conditions as the only things a node waits on, with a cycle check on every write
- `state.db`, `cache.db` and `ledger.db` under `.taskmanager/`, SQLite in WAL mode, with `sqlite-vec` search
- The `tm-wave` workflow (`workflows/tm-wave.js`): one loop per node, every step on the model family tm names

## Upgrading from 0.2

0.3.0 does not open an estate written by 0.2: it refuses with the command to run. Export the old estate with the old version first; `tm init --archive` then moves the old files aside and starts fresh, and the work still in flight is re-imported. `tm guide overview` carries the whole runbook.

## Development

```bash
uv sync
uv run pytest
node --test tests/workflow/
```
````

`pyproject.toml`: `version = "0.2.0"` → `version = "0.3.0"` under `[project]`.

`src/taskmanager/__init__.py`, whole file:

```python
__version__ = "0.3.0"
```

`.claude-plugin/plugin.json`: `"version": "0.2.0"` → `"version": "0.3.0"`. `.claude-plugin/marketplace.json`: the plugin entry's `"version": "0.2.0"` → `"version": "0.3.0"`. `gemini-extension.json`: `"version": "0.2.0"` → `"version": "0.3.0"`.

Regenerate the lock so the editable project's recorded version matches:

```
uv lock --directory <worktree>; echo $?
git -C <worktree> diff --stat -- uv.lock
```

Expected: exit 0, and `uv.lock` changes only the `taskmanager` package's `version` line.

- [ ] **Step 4: Run the tests and the gates**

```
uv run --directory <worktree> pytest tests/unit/test_skills.py tests/unit/test_guides.py tests/unit/test_package.py -q; echo $?
uv run --directory <worktree> pytest -q; echo $?
uv run --directory <worktree> ruff check; echo $?
uv run --directory <worktree> ruff format --check; echo $?
uv run --directory <worktree> mypy; echo $?
node --test <worktree>/tests/workflow/; echo $?
```

Expected: every command exits 0.

- [ ] **Step 5: Commit**

```
git -C <worktree> add skills/taskmanager/SKILL.md src/taskmanager/skills/taskmanager/SKILL.md skills/dispatcher/SKILL.md src/taskmanager/skills/dispatcher/SKILL.md commands/task.md commands/tm.md commands/board.md README.md pyproject.toml src/taskmanager/__init__.py .claude-plugin/plugin.json .claude-plugin/marketplace.json gemini-extension.json uv.lock tests/unit/test_skills.py tests/unit/test_guides.py tests/unit/test_package.py && git -C <worktree> commit -m "docs(plugin): ship skills, commands and README for the lifecycle, released as 0.3.0"
```

### Task 25: SocialSrc cutover preparation

**Spec:** §9.2, §10.3
**Files:**

Paths: `<cutover>` is `<worktree>/../tm-cutover` (scratch beside the implementation worktree, never committed); `<rules-wt>` is `<worktree>/../socialsrc-tm-rules`, a worktree of the SocialSrc outer repository on branch `docs/tm-lifecycle-rules`, cut `--no-track` from its `origin/main`.

- Create: `<cutover>/check_prepared.py`
- Create: `<cutover>/prepare_guide.py`
- Create: `<cutover>/guide.yaml` (generated by `prepare_guide.py` from `git show origin/main:tm/_spec-guide.json` of `/Users/taigo.pedrosa/Documents/SocialSrc/spec`)
- Create: `<cutover>/dispatcher-SKILL.md` (copy of `<worktree>/skills/dispatcher/SKILL.md`, for `/Users/taigo.pedrosa/claude/shared/skills/dispatcher/SKILL.md`)
- Create: `<cutover>/tm-wave.js` (copy of `<worktree>/workflows/tm-wave.js`, for `/Users/taigo.pedrosa/claude/shared/workflows/tm-wave.js`)
- Modify: `<rules-wt>/.claude/rules/35-taskmanager.md` (the `status-moves-through-tm-run` bullet becomes `status-moves-through-tm-verbs`; the `deferral-is-a-section`, `shared-verification-blames-the-regressor` and `verification-reads-origin-main-not-the-checkout` bullets rewritten)
- Modify: `<rules-wt>/.claude/rules/85-reports-gates-copy.md` (the `review-findings-are-a-section`, `merge-outcome-is-its-own-section` and `wave-gate`/`branch-ci-gate` bullets rewritten)
- Modify: `<rules-wt>/.claude/conventions-ledger.md` (`### status-moves-through-tm-run` retired; `### status-moves-through-tm-verbs` added; a dated paragraph appended to `deferral-is-a-section`, `review-findings-are-a-section`, `merge-outcome-is-its-own-section`, `wave-gate`, `shared-verification-blames-the-regressor`, `verification-reads-origin-main-not-the-checkout`)
- Test: `<cutover>/check_prepared.py`, `<rules-wt>/.claude/check-rules.sh`, `<rules-wt>/.claude/hooks/guard.py --self-test`

**Interfaces:**
- Consumes: `RETIRED`, `_tm_commands`, `_tokens`, `_resolve`, `_accepted_flags` from `tests/unit/test_guides.py` (Tasks 23–24); the CLI of Task 18 (`tm task start --worktree-dir`, `--agent` on the closing verbs); `skills/dispatcher/SKILL.md` (Task 24); `workflows/tm-wave.js` and its argument set (Task 22).
- Produces: the four artefacts the runbook's step 7 applies — the rules branch, `guide.yaml`, and the two owner copies — and nothing applied yet.

Nothing here touches the live estate. `/Users/taigo.pedrosa/Documents/SocialSrc/.taskmanager/` is never opened, no `tm` command runs against SocialSrc, and nothing is pushed: the rules describe a tm that is not installed until the owner's cutover, so the branch waits. The addendum comes from the spec repository's committed export at `origin/main`, never from the live database. Because the user dropped in-place migration, the cutover re-imports only the ongoing work; the `guide` spec node is part of what gets re-imported, and `guide.yaml` is that node with its sections, so one `tm import` both creates it and applies the addendum (a `tm section set guide:<topic>` needs the node to exist first).

The check is written first and watched failing against the untouched rules; it passes only once every rule file, every addendum topic and both copies name no retired vocabulary and show only `tm` commands and flags the new CLI has.

- [ ] **Step 1: Write the failing test**

Cut the rules worktree, after proving the branch name is free:

```
git -C /Users/taigo.pedrosa/Documents/SocialSrc branch -a --list '*tm-lifecycle*'; echo $?
git -C /Users/taigo.pedrosa/Documents/SocialSrc fetch -q origin main && git -C /Users/taigo.pedrosa/Documents/SocialSrc worktree add --no-track -b docs/tm-lifecycle-rules <worktree>/../socialsrc-tm-rules origin/main; echo $?
git -C <worktree>/../socialsrc-tm-rules rev-parse --abbrev-ref HEAD
mkdir <worktree>/../tm-cutover
```

Expected: the branch listing prints nothing, the worktree add exits 0, and the branch prints `docs/tm-lifecycle-rules`.

`<cutover>/check_prepared.py`:

```python
"""Everything the SocialSrc cutover applies describes the tm about to be installed.

Run in the new tm's environment, so every `tm` command shown resolves against the new CLI:
uv run --directory <worktree> python <cutover>/check_prepared.py <worktree> <rules-wt>
"""

import sys
from pathlib import Path

import yaml

WORKTREE = Path(sys.argv[1]).resolve()
RULES_WT = Path(sys.argv[2]).resolve()
CUTOVER = Path(__file__).resolve().parent
sys.path.insert(0, str(WORKTREE / "tests" / "unit"))

from test_guides import RETIRED, _accepted_flags, _resolve, _tm_commands, _tokens  # noqa: E402

LEDGER_SLUGS = ("status-moves-through-tm-verbs",)
COPIES = (
    ("dispatcher-SKILL.md", "skills/dispatcher/SKILL.md"),
    ("tm-wave.js", "workflows/tm-wave.js"),
)
# CLAUDE.md, ten rule files, ten addendum topics and two copies; fewer means a glob found nothing.
FLOOR = 20


def problems_in(name: str, text: str) -> list[str]:
    found = [f"{name}: retired vocabulary {word!r}" for word in RETIRED if word in text]
    for command in _tm_commands(text):
        tokens = _tokens(command)
        if not tokens or tokens[0].startswith(("$", "<")):
            continue
        try:
            label, cmd, rest = _resolve(tokens)
        except AssertionError as exc:
            found.append(f"{name}: {exc}")
            continue
        accepted = _accepted_flags(cmd)
        for token in rest:
            flag = token.split("=", 1)[0]
            if token.startswith("-") and flag not in accepted:
                found.append(f"{name}: `{label}` has no flag {flag}")
    return found


def main() -> int:
    problems: list[str] = []
    checked = 0
    for path in [RULES_WT / "CLAUDE.md", *sorted((RULES_WT / ".claude" / "rules").glob("*.md"))]:
        problems += problems_in(str(path.relative_to(RULES_WT)), path.read_text(encoding="utf-8"))
        checked += 1
    ledger = (RULES_WT / ".claude" / "conventions-ledger.md").read_text(encoding="utf-8")
    problems += [
        f"conventions-ledger.md: no ### {s}" for s in LEDGER_SLUGS if f"\n### {s}\n" not in ledger
    ]
    guide = CUTOVER / "guide.yaml"
    if guide.exists():
        sections = yaml.safe_load(guide.read_text(encoding="utf-8"))["spec"]["sections"]
        for key, section in sections.items():
            problems += problems_in(f"guide:{key}", section["content"])
            checked += 1
    else:
        problems.append("guide.yaml: not prepared")
    for copy, source in COPIES:
        target = CUTOVER / copy
        if not target.exists() or target.read_bytes() != (WORKTREE / source).read_bytes():
            problems.append(f"{copy}: missing, or differs from {source}")
        checked += 1
    print("\n".join(problems) if problems else "prepared")
    print(f"checked {checked} files and sections")
    return 1 if problems or checked < FLOOR else 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Run it and watch it fail**

```
uv run --directory <worktree> python <worktree>/../tm-cutover/check_prepared.py <worktree> <worktree>/../socialsrc-tm-rules; echo $?
```

Expected: exit 1, printing at least:

```
.claude/rules/35-taskmanager.md: retired vocabulary 'tm run start'
.claude/rules/35-taskmanager.md: retired vocabulary 'tm run stop'
.claude/rules/35-taskmanager.md: `tm run` has no subcommand 'start'
.claude/rules/85-reports-gates-copy.md: retired vocabulary 'tm run stop'
conventions-ledger.md: no ### status-moves-through-tm-verbs
guide.yaml: not prepared
dispatcher-SKILL.md: missing, or differs from skills/dispatcher/SKILL.md
tm-wave.js: missing, or differs from workflows/tm-wave.js
checked 13 files and sections
```

- [ ] **Step 3: Implement**

**3a. `35-taskmanager.md`.** Four exact replacements (Edit, old → new):

Old:

```
- **A status moves only through `tm run start` / `tm run stop`.** `COMPLETED` is set by the holder of the merge lease (the merging agent, or the workflow that claimed for it) only after `tm verify run <id>` exits 0 against `origin/main`. `tm run stop` does not check lease ownership, so stop only a task you claimed. [status-moves-through-tm-run]
```

New:

```
- **A status moves only through `tm task start` and the verb that closes the step it claimed** (`tm task complete`, `tm task review`, `tm task release`, `tm job resume`), **or through a ledgered repair** (`tm task reopen`, `tm task reset`, `tm task defer`, `tm task abandon`, `tm task supersede`). `COMPLETED` is set only by tm's own landing, after `tm verify run <id> --ref <target>` passes on the landed target. A step that must wait is released with `tm task release <id> --blocked` naming the edge, decision or condition; a plain release is a counted failure. `tm task complete`, `tm task review` and `tm task release` given `--agent <agent>` are refused unless the live lease is that agent's, so a dispatcher passes its lease's agent name and never closes a step another claim now holds. [status-moves-through-tm-verbs]
```

Old:

```
- **Paused, deferred or blocked work carries its justification as a section** (`deferral`, `owed`) with the owner's ruling and date, and `DEFERRED` set through `tm run stop`. [deferral-is-a-section]
```

New:

```
- **Paused, deferred or blocked work carries its justification as a section** (`deferral`, `owed`) with the owner's ruling and date: `tm task defer <id> --note "<ruling, date>"` writes it to `:deferral` and sets `DEFERRED` in one call. [deferral-is-a-section]
```

Old:

```
- **A shared-aggregate verification's hold belongs to the task whose files caused the latest regression, not to whichever sibling task's merge gets re-dispatched next.** A budget/ratchet check spanning many tasks' files stays red for every task sharing it until the actual regressor is fixed, so re-running the identical measurement on an already-correct task's merge just repeats the same non-fix. A merge role hitting this diffs the check's per-file output against its own `declared_files`; if none of the red is its own, it names the owning task (filing one via `tm import` if none exists) and reports its own task as still `merged`/correct rather than re-measuring next round. [shared-verification-blames-the-regressor]
```

New:

```
- **A shared-aggregate verification's red belongs to the task whose files caused the latest regression, not to whichever sibling's landing verifies next.** A budget/ratchet check spanning many tasks' files stays red for every task sharing it until the actual regressor is fixed, and tm counts a red verification after landing against the node that landed. The fixer handed that `merge_failed` diffs the check's per-file output against its own `declared_files`; if none of the red is its own, it names the owning task (filing one via `tm import` if none exists) and releases with `tm task release <id> --agent <name> --blocked --depends <owner>` rather than committing a non-fix. [shared-verification-blames-the-regressor]
```

Old:

```
- **A verification reads the ref it names, never a bare path into a shared checkout**, whose working tree stands on whatever branch it happens to be on. Use `git -C <repo> fetch -q origin main && git show origin/main:<path>`, or `git worktree add --detach <dir> origin/main` when the check executes files. Keep the fetch inside the `&&` so a network failure fails closed. Fetch `--tags` before a tag check. [verification-reads-origin-main-not-the-checkout]
```

New:

```
- **A verification reads the ref tm names, never a bare path into a shared checkout**, whose working tree stands on whatever branch it happens to be on. A `test_command` reads `git -C <repo> show "${TM_VERIFY_REF:-origin/main}:<path>"`, fetching first only when that ref is `origin/main`, or `git worktree add --detach <dir> "${TM_VERIFY_REF:-origin/main}"` when the check executes files: a landing verifies at its own target, and tm refuses a literal `origin/main` on a node that lands on its parent. Keep the fetch inside the `&&` so a network failure fails closed. Fetch `--tags` before a tag check. [verification-reads-origin-main-not-the-checkout]
```

**3b. `85-reports-gates-copy.md`.** Three exact replacements:

Old:

```
- **A review's findings are `tm section set <id>:review`, written before `tm run stop`.** Findings sent only to the dispatcher leave a fixer nothing to fix. [review-findings-are-a-section]
```

New:

```
- **A review's findings are `tm section set <id>:review`, written before `tm task review`**, which refuses a verdict whose `:review` is unchanged since the claim. Findings sent only to the dispatcher leave a fixer nothing to fix. [review-findings-are-a-section]
```

Old:

```
- **A merge outcome goes in `:merge`, never `:report`.** [merge-outcome-is-its-own-section]
```

New:

```
- **A landing's outcome goes in `:merge`, never `:report`**: tm's landing writes it, and an agent resolving a stopped landing appends to it. [merge-outcome-is-its-own-section]
```

Old:

```
- **A merge runs the repo's full CI test jobs at the merged tip before it pushes, and never adds a failure to `main`.** On a red `main` it pushes only when its failing set is a strict subset of untouched `origin/main`'s, so a fix for one red is never blocked by another. A task brief scopes the implementer's gate to the task's files; with continuous dispatch no wave ever closes, so the merge is the only full gate a tip gets. A branch review runs the branch's own CI command at the tip and reports its exit code. [wave-gate] [branch-ci-gate]
```

New:

```
- **tm's landing runs the repository's `main` gate at the merged tip before it pushes, and never adds a failure to `main`.** That gate, `repos.<repo>.gates.main`, is the repository's full CI test jobs, copied from its CI workflow and run through `infra/agent-test.sh --task-id {node}`, with a JUnit report so a red `main` is attributed by failing set: a tip that removes a failure and adds none lands, one matching the red waits on it. A task brief scopes the implementer's gate to the task's files; with continuous dispatch no wave ever closes, so the landing is the only full gate a tip gets. A branch review runs the branch's own CI command at the tip and reports its exit code. [wave-gate] [branch-ci-gate]
```

**3c. `conventions-ledger.md`.** Replace the whole body of `### status-moves-through-tm-run` (the six lines under the heading, from `A first design derived` to `so it never frees a task.`) with its retirement line, and add the new entry directly after it, before `### story-emptiness-measured-on-rendered-text`:

```
### status-moves-through-tm-run
Retired 2026-09-24: `tm run start` and `tm run stop` are removed in tm 0.3.0. See [status-moves-through-tm-verbs].


### status-moves-through-tm-verbs
Product owner, 2026-09-24, the TaskManager lifecycle redesign. Under tm 0.2 any caller could hand-set
a status with `tm run stop --status`, which checked no lease ownership, and the `tm-wave` workflow
carried the lifecycle itself: an in-memory fix counter, a `:review` hash check, a `:hold` section, and a
`COMPLETED` written by whoever held the merge lease after a verification it ran on its own. Each of
those could disagree with tm's record. tm 0.3.0 stores one status per node, moves it only by the claim
and the verb closing the claimed step or a ledgered repair, runs landings as its own jobs, sets
`COMPLETED` only after its own verification at the landed target, and counts fix rounds, own-defect
landings and failed steps into `FAILED` with a decision for the owner.
```

Then append one dated paragraph to each of six entries, each inserted after the entry's current last line (Edit: old is that line, new is that line, a blank line, and the paragraph):

| Entry | Its current last line | Paragraph appended |
|:--|:--|:--|
| `deferral-is-a-section` | `name (29 named none, and are stored whole on the spec node), so a task's blocker and its reason arrive in one read.` | `2026-09-24: under tm 0.3.0, \`tm task defer <id> --note\` writes the note to \`:deferral\` and sets \`DEFERRED\` in one ledgered call; \`tm run stop\` no longer exists.` |
| `review-findings-are-a-section` | `review is precisely the judgement the re-measure section exempts: nothing regenerates it.` | `2026-09-24: tm 0.3.0 snapshots the \`:review\` section's hash at the review claim and refuses \`tm task review\` while it is unchanged, so a verdict whose findings went only to the dispatcher is refused by the tool rather than found by a fixer.` |
| `merge-outcome-is-its-own-section` | `targets. Proposed by the implementer whose report was destroyed, in the same message that` / `restored it.` (two lines) | `2026-09-24: tm 0.3.0's landing job writes \`:merge\` itself; an agent handed a stopped landing appends its resolution there.` |
| `wave-gate` | `and \`tsc\` over touched files, own test files green, repo-wide result reported as-is with failing` / `paths attributed.` (two lines) | `2026-09-24: tm 0.3.0 runs the full gate itself at every landing on \`main\` (\`repos.<repo>.gates.main\`), caches the untouched target's result per commit, and attributes a red by its JUnit failing set; no merge agent runs it.` |
| `shared-verification-blames-the-regressor` | `spending a seventh identical round establishing what the fifth already established.` | `2026-09-24: under tm 0.3.0 a red verification after landing counts against the node that landed as an own defect, so the same shared red would spend that node's landing attempts and reach \`FAILED\`; releasing \`--blocked --depends\` on the regressor is what stops the count.` |
| `verification-reads-origin-main-not-the-checkout` | `answers; this is the wrong *ref* in the right repository.` | `2026-09-24: tm 0.3.0 runs a node's verifications at its landing target with \`TM_VERIFY_REF\` set to it, and refuses a \`test_command\` naming \`origin/main\` literally on a node that lands on a parent branch, where \`origin/main\` is never the target.` |

The backslashes in the table only escape the backticks for this table; the paragraphs are written with plain backticks.

**3d. The guide addendum.** `<cutover>/prepare_guide.py`:

```python
"""Builds SocialSrc's guide addendum for tm 0.3.0 from the spec repository's exported `guide` node.

The source is the export committed on the spec repository's origin/main, never the live database.
Every replacement must match exactly once, whitespace-tolerantly, or nothing is written.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

SPEC_REPO = "/Users/taigo.pedrosa/Documents/SocialSrc/spec"
OUT = Path(__file__).with_name("guide.yaml")

REPLACEMENTS: list[tuple[str, str, str]] = [
    (
        "dispatch",
        "- **Schedule by readiness, never by wave.** A wave runs at the speed of its slowest member and "
        "leaves slots idle behind it. Ask `tm next -n <slots>` (`--strategy finish-plans` when the "
        "question is which plan to close, `--model <id>` to fill a tier) and dispatch straight from it: "
        "closing a plan outranks starting one. Do not pre-verify a plan's prose gates yourself — a "
        "blocker that is real is a dependency or an `external_blockers` entry on the task, and a task "
        "whose gate is only prose is a defect in the task, fixed in `tm` before dispatch.",
        "- **Schedule by readiness, never by wave.** A wave runs at the speed of its slowest member and\n"
        "  leaves slots idle behind it. `tm-wave` asks `tm wave discover` for every claimable node each\n"
        "  tick and runs each through its own loop, so a slot refills the moment a node lands or blocks;\n"
        "  closing a plan outranks starting one. Do not pre-verify a plan's prose gates yourself — a\n"
        "  blocker that is real is an edge, a decision or a condition on the node\n"
        '  (`tm task condition add <id> --needs "<what>" --command "<check>"`), and a node whose gate is\n'
        "  only prose is a defect in the node, fixed in `tm` before dispatch.",
    ),
    (
        "dispatch",
        "- **Name the worktree-creation step explicitly, in every dispatch.** An `agy` implementer once "
        "committed finished work directly onto the shared primary `core` checkout's local `main`, "
        "unpushed and on a stale base, because its brief never said where to work; nothing reds, and "
        "it sits there until an unrelated sibling notices `git status`. Name the exact command before "
        "naming any file to edit: `tm run start <id> --agent <n> --session <s> --worktree "
        "--worktree-dir <scratchpad>`, which cuts `tm/<id>` from the task's own `target_repo`'s "
        "`origin/main` with `--no-track`; or, cut by hand, `git -C <repo> worktree add --no-track -b "
        "<branch> <scratchpad>/<repo>-<branch> origin/main`. `--worktree` needs the task to declare "
        "`target_repo`, and a review claim refuses it.",
        "- **Name the worktree explicitly, in every dispatch.** An `agy` implementer once committed\n"
        "  finished work directly onto the shared primary `core` checkout's local `main`, unpushed and on\n"
        "  a stale base, because its brief never said where to work; nothing reds, and it sits there\n"
        "  until an unrelated sibling notices `git status`. The claim cuts it:\n"
        "  `tm task start <id> --agent <n> --session <s> --worktree-dir <scratchpad>` cuts `tm/<id>` for\n"
        "  an `implement` or `fix` step from its base with `--no-track`, in the task's own `target_repo`,\n"
        "  and prints the path; the brief names that path before naming any file to edit. A task with no\n"
        "  `target_repo` cannot be claimed for implement.",
    ),
    (
        "dispatch",
        "Whoever runs `tm run start` first holds the task.",
        "Whoever's `tm task start` takes the lease first holds the step; the other is told `blocked`.",
    ),
    (
        "dispatch",
        'specs: [<spec ids>, "none"], session: "<your session name>", worktreeDir: "<your scratchpad>", '
        'slots: 15, maxStrong: 5, maxFixRounds: 2, root: "/Users/taigo.pedrosa/Documents/SocialSrc", '
        'agentTypes: {web: "react-dev", core: "python-dev", api: "python-dev"},',
        'session: "<your session name>", worktreeDir: "<your scratchpad>",\n'
        '  slots: 15, maxStrong: 5, root: "/Users/taigo.pedrosa/Documents/SocialSrc",\n'
        '  agentTypes: {web: "react-dev", core: "python-dev", api: "python-dev"},\n'
        '  reviewerTypes: {task: "task-reviewer", rereview: "scoped-re-reviewer", container: "branch-reviewer"},',
    ),
    (
        "dispatch",
        "[dev-box-run-carries-its-task-id]. One run at a time unless the owner says otherwise.",
        "[dev-box-run-carries-its-task-id]. One run at a time unless the owner says otherwise.\n"
        "\n"
        "Omitting `specs` discovers across every spec and every node with no spec. tm counts fix rounds\n"
        "itself (`tm config set max_fix_rounds.task 2`), and a node waits only on the edge, decision or\n"
        "condition `tm task release --blocked` names, so the workflow takes no fix cap and no release\n"
        "list. The landing gate is tm's own, one per repository:\n"
        "`repos.<repo>.gates.main`, set as one YAML value with `tm config set repos '<yaml>'`, holding\n"
        "the repository's CI test command run through `infra/agent-test.sh --task-id {node}`.",
    ),
    (
        "implement",
        "2. **Claim and cut the worktree in one command**, as the brief names it: `tm run start "
        "<task-id> --agent <name> --session <id> --worktree --worktree-dir <scratchpad>`. It resolves "
        "the task's `target_repo`, cuts `tm/<task-id>` from **that repository's** `origin/main` with "
        "`--no-track`, and puts it at `<worktree-dir>/<repo>-<task-id>`; a task with no `target_repo` "
        "is refused rather than cut in the outer repo, and a review claim refuses `--worktree` because "
        "a review reads the branch.",
        "2. **The claim cuts the worktree**: `tm task start <task-id> --agent <name> --session <id>\n"
        "   --worktree-dir <scratchpad>`, which the workflow runs before dispatching you, prints\n"
        "   `worktree`, `branch` and `base`. The branch is cut from `base` — `origin/main`, or the\n"
        "   container branch the task lands on — with `--no-track`, in the task's own `target_repo`; a\n"
        "   task with no `target_repo` is refused rather than cut in the outer repo.",
    ),
    (
        "implement",
        "3. The claim refuses a task that is not ready or whose files another lease holds: **stop and "
        "report, never pick another task.**",
        "3. A claim that prints `action: blocked` names why — an edge, a decision, a condition, a sync\n"
        "   or a lease on one of its files: **stop and report, never pick another task.**",
    ),
    (
        "implement",
        "- **`tm run heartbeat <task-id>` before and after every gate.** The lease is 300 seconds; an "
        "expired one leaves the task claimed and `tm run sweep` names it.",
        "- **`tm task heartbeat <task-id>` before and after every gate.** An implement lease lasts\n"
        "  `lease_ttl.implement`; an expired one is swept back to `READY` and counted as a failed step.",
    ),
    (
        "implement",
        "5. `tm run stop <task-id> --status WAITING_REVIEW` when the work is done, `--status "
        "NOT_STARTED` when blocked or handed off, with the report saying why. It runs on every exit, "
        "so a lease never outlives its agent. **An implementer never sets `COMPLETED`.**",
        "5. `tm task complete <task-id> --agent <name>` when the work is done;\n"
        "   `tm task release <task-id> --agent <name> --blocked` naming the edge, decision or condition\n"
        "   when something else must happen first, with the report saying why. One of them runs on\n"
        "   every exit, so a lease never outlives its agent.\n"
        "   **Only tm's landing sets `COMPLETED`.**",
    ),
    (
        "review",
        "- **A plan's whole-branch review is unconditional, and no amount of per-task review "
        "substitutes for it.**",
        "- **A plan's whole-branch review is unconditional, and no amount of per-task review\n"
        "  substitutes for it**: every plan is imported with `review` and `fix` on, and tm dispatches\n"
        "  that review once its tasks have landed on the plan's branch.",
    ),
    (
        "review",
        "The findings are written to `tm section set <id>:review ...` before `tm run stop`, never only "
        "to the dispatcher.",
        "The findings are written to `tm section set <id>:review ...` before `tm task review`, which\n"
        "refuses a verdict whose `:review` is unchanged, never only to the dispatcher.",
    ),
    (
        "fix",
        "- **A third fix round on one task stops the task.** Re-read the brief against the tree, "
        "correct the brief, say what it got wrong, then dispatch again.",
        "- **tm stops a task at `FAILED` when its fix rounds run out** (`max_fix_rounds`, two for a\n"
        "  task), and the decision it opens is the owner's. Before anyone answers `investigate`,\n"
        "  re-read the brief against the tree, correct it, and say what it got wrong.",
    ),
    (
        "fix",
        "commit with an explicit pathspec, and release the claim with `tm run stop <task-id> --status "
        "WAITING_REVIEW`.",
        "commit with an explicit pathspec, and close the step with\n"
        "`tm task complete <task-id> --agent <name>`.",
    ),
    (
        "merge",
        "## A merge here is a deploy",
        "## A merge here is a deploy\n"
        "\n"
        "tm lands every node itself (`tm guide merge`): it builds the merge, runs\n"
        "`repos.<repo>.gates.main`, pushes `HEAD:main` and verifies. What follows is what an agent handed\n"
        "a stopped landing needs, and what a planner turns into a `landing` condition before the node\n"
        "is dispatched: a precondition on the push below is a condition with its command, never a\n"
        "sentence in a brief.",
    ),
    (
        "merge",
        "so neither task can reach `COMPLETED` through `tm run stop`.",
        "so neither task's landing can ever verify.",
    ),
    (
        "merge",
        "- **A merge refuses a task whose `:review` still records a finding carried forward, "
        "unresolved or open**: release it to `WAITING_FIXES` naming the findings.",
        "- **A review never approves a task whose `:review` still records a finding carried forward,\n"
        "  unresolved or open**: it rejects, naming the findings.",
    ),
    (
        "merge",
        "- **Set `COMPLETED` only after `tm verify run <task-id>` passes against `origin/main`**, and "
        'only after the push. A task carrying no verification exits 0 with "No verifications to '
        'run.", which is a defect in the task, not a pass.',
        "- **`COMPLETED` is set by tm's landing only, after `tm verify run <task-id> --ref <target>`\n"
        "  passes on the landed target.** A task carrying no verification lands with that said in its\n"
        "  `:merge`, which is a defect in the task, not a pass.",
    ),
    (
        "plan",
        "- **A blocker outside `tm` is content, and it is an `external_blockers` entry carrying its "
        "command** — a branch merged in another repository, a tag a consumer pins, a file a sibling "
        "must land first.",
        "- **A blocker outside `tm` is content, and it is a condition carrying its command**\n"
        "  (`conditions: [{needs, command, stage}]` in the import, `stage: landing` when only the push\n"
        "  must wait) — a branch merged in another repository, a tag a consumer pins, a file a sibling\n"
        "  must land first.",
    ),
    (
        "plan",
        "read against `origin/main`: `git -C <repo> show origin/main:<path> | grep -qF '<needle>'`,",
        'read at the ref tm names: `git -C <repo> show "${TM_VERIFY_REF:-origin/main}:<path>" |\n'
        "  grep -qF '<needle>'`, since a landing verifies at its own target and tm refuses a literal\n"
        "  `origin/main` on a task that lands on its parent;",
    ),
    (
        "plan",
        "owner's ruling and its date, and `DEFERRED` set through `tm run stop`.",
        'owner\'s ruling and its date, set with `tm task defer <id> --note "<ruling, date>"`.',
    ),
    (
        "plan",
        "- **Dependencies as explicit task ids.**",
        "- **`review`, `fix` and `merge` say how a task reaches `main`.** A task whose plan's branch\n"
        "  review is its review lands with `merge: parent`; a migration, RLS or crypto task keeps its own\n"
        "  review and fix whatever its plan does.\n"
        "- **Dependencies as explicit task ids.**",
    ),
]


def exported() -> dict:
    subprocess.run(["git", "-C", SPEC_REPO, "fetch", "-q", "origin", "main"], check=True)
    shown = subprocess.run(
        ["git", "-C", SPEC_REPO, "show", "origin/main:tm/_spec-guide.json"],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(shown.stdout)["spec"]


def pattern(old: str) -> re.Pattern[str]:
    return re.compile(r"\s+".join(re.escape(word) for word in old.split()))


def main() -> int:
    node = exported()
    sections = {section["key"]: section for section in node["sections"]}
    problems: list[str] = []
    for topic, old, new in REPLACEMENTS:
        content = sections[topic]["content"]
        count = len(pattern(old).findall(content))
        if count != 1:
            problems.append(f"{topic}: {count} matches for {old[:70]!r}")
            continue
        sections[topic]["content"] = pattern(old).sub(lambda _match: new, content)
    if problems:
        print("\n".join(problems))
        return 1
    document = {
        "spec": {
            "id": node["id"],
            "title": node["title"],
            "priority": node["priority"],
            "sections": {
                key: {
                    "header": section["header"],
                    "content": section["content"],
                    "ordinal": section["ordinal"],
                }
                for key, section in sections.items()
            },
        }
    }
    OUT.write_text(
        yaml.safe_dump(document, sort_keys=False, allow_unicode=True, width=100), "utf-8"
    )
    print(f"{OUT}: {len(sections)} sections, {len(REPLACEMENTS)} replacements applied")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Build it and read what changed:

```
uv run --directory <worktree> python <worktree>/../tm-cutover/prepare_guide.py; echo $?
```

Expected: exit 0 and `<cutover>/guide.yaml: 10 sections, 21 replacements applied`. A `0 matches` line means the export moved since this plan was written: re-read that topic's text with `git -C /Users/taigo.pedrosa/Documents/SocialSrc/spec show origin/main:tm/_spec-guide.json`, correct the replacement's old text to the committed wording, and run it again.

**3e. The owner's copies.**

```
cp <worktree>/skills/dispatcher/SKILL.md <worktree>/../tm-cutover/dispatcher-SKILL.md
cp <worktree>/workflows/tm-wave.js <worktree>/../tm-cutover/tm-wave.js
```

- [ ] **Step 4: Run the tests and the gates**

```
uv run --directory <worktree> python <worktree>/../tm-cutover/check_prepared.py <worktree> <worktree>/../socialsrc-tm-rules; echo $?
env PATH=/usr/bin:/bin sh <worktree>/../socialsrc-tm-rules/.claude/check-rules.sh; echo $?
python3 <worktree>/../socialsrc-tm-rules/.claude/hooks/guard.py --self-test; echo $?
git -C <worktree>/../socialsrc-tm-rules diff --stat
git -C <worktree> status --short
```

Expected: `prepared`, `checked 23 files and sections`, exit 0; `check-rules.sh` prints `slugs cited: <n>` with no `cited but not in ledger:` block and exits 0 (its `PATH` leaves out every `tm`, so it reads the rules and ledger only and never the live estate's addendum; the addendum slugs it would otherwise count are unchanged by this task); `guard.py --self-test` exits 0; the rules diff touches exactly the three files; the TaskManager worktree is clean.

- [ ] **Step 5: Commit**

```
git -C <worktree>/../socialsrc-tm-rules add .claude/rules/35-taskmanager.md .claude/rules/85-reports-gates-copy.md .claude/conventions-ledger.md && git -C <worktree>/../socialsrc-tm-rules commit -m "docs(rules): describe the claim, the closing verbs and tm's own landing for the tm 0.3.0 cutover"
git -C <worktree>/../socialsrc-tm-rules status --short
```

Expected: one commit on `docs/tm-lifecycle-rules`, a clean status. The branch is not pushed. What the owner's cutover does with these artefacts, at runbook step 7 and only on the owner's go-ahead:

1. Re-run `prepare_guide.py` and `check_prepared.py` (the export or the rules may have moved), then `tm import --format yaml -f <worktree>/../tm-cutover/guide.yaml -C /Users/taigo.pedrosa/Documents/SocialSrc`.
2. Land the rules: a worktree cut `--no-track` from a freshly fetched SocialSrc `origin/main`, `git merge --no-ff -m "merge: describe tm 0.3.0 in the rules at its cutover" docs/tm-lifecycle-rules` there, `git ls-remote origin refs/heads/main` in the same breath, `git push origin HEAD:main`, then fast-forward the primary checkout's local `main` and announce it.
3. `cp <worktree>/../tm-cutover/dispatcher-SKILL.md /Users/taigo.pedrosa/claude/shared/skills/dispatcher/SKILL.md` and `cp <worktree>/../tm-cutover/tm-wave.js /Users/taigo.pedrosa/claude/shared/workflows/tm-wave.js`, which the owner commits in that repository.

### Task 26: End-to-end run on a scratch estate

**Spec:** §11 (end to end), §6.1–§6.5, §4.4, §3.2, §3.4, §10.1
**Files:**

Paths: `<e2e>` is `<worktree>/../tm-e2e`, written resolved (no `..`) wherever it is passed to the workflow; `<tm>` is `<worktree>/.venv/bin/tm`, the code under test (created by `uv sync --directory <worktree>`).

- Create: `<e2e>/setup.sh`
- Create: `<e2e>/e2e.yaml`
- Create: `<e2e>/check.sh`
- Test: `<e2e>/check.sh`

**Interfaces:**
- Consumes: everything: `tm init`, `tm config set repos '<yaml>'` (Task 11), `tm import` with flags and `land_order`, `tm task depends`, `tm decision list/answer`, `tm verify list/remove/add`, `tm section get`, `tm run list`, the landing and sync jobs of Tasks 14–15, the `tm-wave` workflow of Task 22 with its `tm` argument and its trail naming the family each step ran on, the guides of Task 23.
- Produces: the run's evidence (each tick's workflow result and each check's output), quoted in the report and read by Task 27's branch review.

This task is run by the session that holds the `Workflow` tool, not by a dispatched subagent: the ticks are workflow runs with real, cheap agents. `acceptable_models` and `review_models` hold the agents at haiku wherever tm's routing allows; tm's own floors put the plan review on `opus` and fixes, landing agents and syncs on `sonnet`, and that routing is part of what the run proves. Every agent works in `<e2e>` against two toy repositories with local bare origins: nothing reaches the network, and neither the live SocialSrc estate nor any real repository is touched.

The hazard to brief against is the live estate: an agent that runs the `tm` on `PATH` from the session's own directory reaches SocialSrc's database with the old binary, and one that runs `<tm>` there is told to run `tm init --archive`. The preamble below forbids both, and every command in this task passes `-C <e2e>`.

What the run must show, each by a named check:

| Scenario | Shown by |
|:--|:--|
| a landing on a parent branch | `E2E-P1-A` and `E2E-P1-B` merge commits on the local `tm/E2E-P1` of `alpha` and `beta`, and nothing of them on either `main` until the plan lands; `tm/E2E-P1` never pushed |
| one container review | `E2E-P1` reaches `IMPLEMENTED` by rollup, is reviewed on the `opus` family with a `:review` section written, and lands `alpha` then `beta` in its `land_order` |
| one refused cycle | `tm task depends E2E-P1-A --add E2E-P1-B` and `tm task depends E2E-P1 --add E2E-P1-B` exit 1 printing a path through `←`, and write nothing |
| one `FAILED` node | `E2E-F1` (review and fix off) lands on `main`, its verification is red there, it goes to `FAILED`, and tm opens a decision on it |
| one reopen | answering that decision `investigate` reopens `E2E-F1` with a `:reopen` note and cleared counters; the next tick recognises the branch as already landed and completes it without a second merge commit |

- [ ] **Step 1: Write the failing test**

```
mkdir <worktree>/../tm-e2e
uv sync --directory <worktree>; echo $?
```

`<e2e>/setup.sh`:

```sh
#!/bin/sh
# Two toy repositories with local bare origins, a fresh estate, per-repository gates and the run's plan.
set -eu
E2E=$1
TM=$2
mkdir "$E2E/origin" "$E2E/wt"
for repo in alpha beta; do
  git init -q --bare -b main "$E2E/origin/$repo.git"
  git init -q -b main "$E2E/$repo"
  git -C "$E2E/$repo" remote add origin "$E2E/origin/$repo.git"
  cat > "$E2E/$repo/test.sh" <<'GATE'
#!/bin/sh
# The repository's whole gate: every tests/*_test.sh, and the first failure fails it.
dir=$(dirname "$0")
for t in "$dir"/tests/*_test.sh; do
  [ -e "$t" ] || continue
  sh "$t" || exit 1
done
GATE
  printf '# %s\n' "$repo" > "$E2E/$repo/README.md"
  git -C "$E2E/$repo" add test.sh README.md
  git -C "$E2E/$repo" -c user.name=e2e -c user.email=e2e@example.invalid commit -q -m "chore: seed $repo"
  git -C "$E2E/$repo" push -q origin HEAD:main
  git -C "$E2E/$repo" fetch -q origin
done
"$TM" init -C "$E2E"
"$TM" config set repos '{alpha: {gates: {main: {command: "sh {worktree}/test.sh", timeout: 120}}}, beta: {gates: {main: {command: "sh {worktree}/test.sh", timeout: 120}}}}' -C "$E2E"
"$TM" import --format yaml -f "$E2E/e2e.yaml" -C "$E2E"
```

`<e2e>/e2e.yaml`:

```yaml
spec:
  id: E2E
  title: Lifecycle end to end
plans:
  - id: E2E-P1
    title: A greeting in each of two repositories
    review: true
    fix: true
    land_order: [alpha, beta]
    sections:
      context: >-
        Each task adds one POSIX shell script printing a greeting, and a test under tests/ named
        greet_test.sh that exits 0 exactly when the script prints it. Nothing else changes.
    tasks:
      - id: E2E-P1-A
        title: alpha prints its greeting
        target_repo: alpha
        merge: parent
        acceptable_models: [claude-haiku-4-5]
        frontmatter:
          declared_files: [greet.sh, tests/greet_test.sh]
          review_models: [claude-haiku-4-5]
        sections:
          objective: "`sh greet.sh` prints `hello alpha`."
          acceptance: "`sh test.sh` exits 0, and exits 1 when greet.sh prints anything else."
        verifications:
          - type: test_command
            target_path: greet-alpha
            expected_pattern: 'test "$(git -C alpha show "$TM_VERIFY_REF:greet.sh" | sh)" = "hello alpha"'
      - id: E2E-P1-B
        title: beta prints its greeting
        target_repo: beta
        merge: parent
        depends_on: [E2E-P1-A]
        acceptable_models: [claude-haiku-4-5]
        frontmatter:
          declared_files: [greet.sh, tests/greet_test.sh]
          review_models: [claude-haiku-4-5]
        sections:
          objective: "`sh greet.sh` prints `hello beta`."
          acceptance: "`sh test.sh` exits 0, and exits 1 when greet.sh prints anything else."
        verifications:
          - type: test_command
            target_path: greet-beta
            expected_pattern: 'test "$(git -C beta show "$TM_VERIFY_REF:greet.sh" | sh)" = "hello beta"'
  - id: E2E-F
    title: A version file
    tasks:
      - id: E2E-F1
        title: alpha carries a VERSION file
        target_repo: alpha
        review: false
        fix: false
        acceptable_models: [claude-haiku-4-5]
        frontmatter:
          declared_files: [VERSION]
        sections:
          objective: "`VERSION` holds the single line `1`."
        verifications:
          - type: test_command
            target_path: version
            expected_pattern: 'test "$(git -C alpha show "${TM_VERIFY_REF:-origin/main}:VERSION")" = 2'
```

`E2E-F1`'s verification expects `2` while its objective says `1`: the post-landing verification is red by design, which is how the run reaches `FAILED`.

`<e2e>/check.sh`:

```sh
#!/bin/sh
# What the end-to-end run must have shown: ok or FAIL per claim, exit 1 on any FAIL or on no claims.
set -u
E2E=$1
TM=$2
fail=0
count=0
report() {
  count=$((count + 1))
  if [ "$2" -eq 0 ]; then echo "ok   $1"; else echo "FAIL $1"; fail=1; fi
}
status() {
  "$TM" task get "$1" --json -C "$E2E" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])'
}

git -C "$E2E/alpha" log --first-parent --format=%s tm/E2E-P1 | grep -q E2E-P1-A
report "E2E-P1-A landed on the plan's branch in alpha" $?
git -C "$E2E/beta" log --first-parent --format=%s tm/E2E-P1 | grep -q E2E-P1-B
report "E2E-P1-B landed on the plan's branch in beta" $?
git -C "$E2E/alpha" ls-remote --exit-code origin refs/heads/tm/E2E-P1 >/dev/null
[ $? -eq 2 ]
report "the plan's branch was never pushed" $?
"$TM" section get E2E-P1:review -C "$E2E" >/dev/null 2>&1
report "the plan was reviewed as a container" $?
[ "$(status E2E-P1)" = COMPLETED ]
report "E2E-P1 is COMPLETED" $?
git -C "$E2E/origin/alpha.git" log --first-parent --format=%s main | grep -q E2E-P1
report "alpha main carries the plan's landing" $?
git -C "$E2E/origin/beta.git" log --first-parent --format=%s main | grep -q E2E-P1
report "beta main carries the plan's landing" $?
[ "$(git -C "$E2E/origin/alpha.git" show main:greet.sh | sh)" = "hello alpha" ]
report "alpha main prints hello alpha" $?
"$TM" decision list --json -C "$E2E" | grep -q E2E-F1
report "a decision was opened when E2E-F1 failed" $?
"$TM" section get E2E-F1:reopen -C "$E2E" >/dev/null 2>&1
report "E2E-F1 was reopened with a note" $?
[ "$(status E2E-F1)" = COMPLETED ]
report "E2E-F1 is COMPLETED after its reopen" $?
[ "$(git -C "$E2E/origin/alpha.git" log --first-parent --format=%s main | grep -c E2E-F1)" = 1 ]
report "E2E-F1 landed exactly once" $?
[ "$("$TM" run list --json -C "$E2E" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["leases"]))')" = 0 ]
report "no lease is left" $?

echo "$count claims checked"
[ "$count" -gt 0 ] || exit 1
exit $fail
```

- [ ] **Step 2: Run it and watch it fail**

```
sh <worktree>/../tm-e2e/setup.sh <worktree>/../tm-e2e <worktree>/.venv/bin/tm; echo $?
sh <worktree>/../tm-e2e/check.sh <worktree>/../tm-e2e <worktree>/.venv/bin/tm; echo $?
<worktree>/.venv/bin/tm wave discover --session e2e --slots 4 --max-strong 2 -C <worktree>/../tm-e2e; echo $?
```

Expected: setup exits 0. The check prints `FAIL` for every claim except `the plan's branch was never pushed` and `no lease is left`, then `13 claims checked`, and exits 1. Discovery chooses exactly `E2E-P1-A` and `E2E-F1`, both `implement`: `E2E-P1-B` waits on its edge, and `E2E-P1` has no step of its own at `READY`.

Then the refused cycles, and that they wrote nothing:

```
<worktree>/.venv/bin/tm task depends E2E-P1-A --add E2E-P1-B -C <worktree>/../tm-e2e; echo $?
<worktree>/.venv/bin/tm task depends E2E-P1 --add E2E-P1-B -C <worktree>/../tm-e2e; echo $?
<worktree>/.venv/bin/tm task get E2E-P1-A --json -C <worktree>/../tm-e2e | python3 -c 'import json,sys; print([d["id"] for d in json.load(sys.stdin)["depends_on"]])'
<worktree>/.venv/bin/tm task get E2E-P1 --json -C <worktree>/../tm-e2e | python3 -c 'import json,sys; print([d["id"] for d in json.load(sys.stdin)["depends_on"]])'
```

Expected: each `depends` exits 1 printing a cycle through `←` that starts and ends at the same vertex (the first names `E2E-P1-A.start` and `E2E-P1-B.landed`; the second `E2E-P1-B.start` and `E2E-P1-B.landed`), and both reads print `[]`.

- [ ] **Step 3: Implement**

Nothing is written here: the implementation is Tasks 1–24, and this step runs it. Each tick is one call, with `<E2E>` and `<TM>` the resolved absolute forms of `<worktree>/../tm-e2e` and `<worktree>/.venv/bin/tm`:

```
Workflow({scriptPath: "<worktree>/workflows/tm-wave.js", args: {
  session: "e2e", worktreeDir: "<E2E>/wt", root: "<E2E>", tm: "<TM>", slots: 4, maxStrong: 2,
  preamble: {default: "Run tm only as <TM>, always with -C <E2E>: the tm on PATH is another install and another estate. If tm answers that a directory holds a pre-lifecycle estate, you ran it in the wrong place: stop and report, and never run tm init."}}})
```

After each tick, quote the workflow's returned `results` (every node's final status and trail) and run the tick's own checks:

**Tick 1.** Expected results: `E2E-P1-A` `COMPLETED` after an implement and a review on haiku (its trail's `implement on haiku:` and `review on haiku:` lines) and a landing on the plan's branch; `E2E-F1` `FAILED` after an implement on haiku and a landing on `main` whose verification was red.

```
git -C <worktree>/../tm-e2e/alpha log --first-parent --format=%s tm/E2E-P1
git -C <worktree>/../tm-e2e/origin/alpha.git show main:greet.sh; echo $?
<worktree>/.venv/bin/tm decision list --status open --json -C <worktree>/../tm-e2e
<worktree>/.venv/bin/tm task get E2E-F1 --yaml -C <worktree>/../tm-e2e
```

Expected: the first shows a merge commit naming `E2E-P1-A`; `show main:greet.sh` exits 128 (the task landed on the plan's branch, not on `main`); one open decision names `E2E-F1` with options `abandon` and `investigate`; `E2E-F1` reads `status: FAILED`.

**Tick 2.** Expected: `E2E-P1-B` `COMPLETED` on the plan's branch in `beta`; the plan rolls up to `IMPLEMENTED` in the same write.

```
<worktree>/.venv/bin/tm task get E2E-P1 --yaml -C <worktree>/../tm-e2e
```

Expected: `status: IMPLEMENTED`, `state: WAITING_REVIEW`, `next_action: review`.

**Tick 3.** Expected: `E2E-P1` reviewed on the `opus` family (the wave log's `E2E-P1@review/opus` and the trail's `review on opus:` line; the brief named both repositories' diffs from `origin/main`), approved, landed on `alpha` then `beta`, `COMPLETED`.

**Reopen.** Correct the verification, then answer the decision the way an owner would:

```
<worktree>/.venv/bin/tm verify list E2E-F1 -C <worktree>/../tm-e2e
<worktree>/.venv/bin/tm verify remove E2E-F1 <the verification id it printed> -C <worktree>/../tm-e2e
<worktree>/.venv/bin/tm verify add E2E-F1 --type test_command --target version --pattern 'test "$(git -C alpha show "${TM_VERIFY_REF:-origin/main}:VERSION")" = 1' -C <worktree>/../tm-e2e
<worktree>/.venv/bin/tm decision answer <the decision id tick 1 printed> --option investigate -C <worktree>/../tm-e2e
<worktree>/.venv/bin/tm task get E2E-F1 --yaml -C <worktree>/../tm-e2e
```

Expected: the answer exits 0; `E2E-F1` reads `status: READY`, `merge_attempts: 0`, `step_failures: 0`, `outcome: null`, with `reopen` among its sections.

**Tick 4.** Expected: `E2E-F1` claimed for implement on its existing branch (the agent finds the work done and closes the step), then landed: the landing recognises the branch as already on `main`, skips to the verification, which is green, and completes it.

A tick whose result differs from its expectation is a defect in the code under test: stop the ticks, fix it test-first in the task that owns that code (its own commit on `feat/lifecycle`, with the four gates), and repeat this task from Step 1 in a fresh `<worktree>/../tm-e2e-2`.

- [ ] **Step 4: Run the tests and the gates**

```
sh <worktree>/../tm-e2e/check.sh <worktree>/../tm-e2e <worktree>/.venv/bin/tm; echo $?
git -C <worktree>/../tm-e2e/alpha worktree list
git -C <worktree>/../tm-e2e/beta worktree list
node --test <worktree>/tests/workflow/; echo $?
uv run --directory <worktree> pytest -q; echo $?
uv run --directory <worktree> ruff check; echo $?
uv run --directory <worktree> ruff format --check; echo $?
uv run --directory <worktree> mypy; echo $?
```

Expected: the check prints `ok` for all 13 claims, `13 claims checked`, exit 0; each toy repository lists only its own checkout and the agents' implement worktrees under `<e2e>/wt` (tm removed every merge worktree it built); every gate exits 0.

- [ ] **Step 5: Commit**

The run changes no tracked file of the implementation worktree:

```
git -C <worktree> status --short
```

Expected: empty. There is nothing to commit; the report quotes, for each tick, the workflow's `results`, and the full output of `check.sh`. `<e2e>` stays in the scratchpad and is listed under "Left behind", since it holds git worktrees and removing it would need a recursive delete.

### Task 27: Branch review and publishing

**Spec:** §11, §9.1 (the `v0.2.0` tag), the plan's Review Focus
**Files:**
- Test: the published refs on `origin` (`refs/heads/feat/lifecycle`, `refs/tags/v0.2.0`, `refs/tags/v0.3.0`)
- Modify: whatever a finding's fix touches, each fix its own test-first commit on `feat/lifecycle`

**Interfaces:**
- Consumes: the whole branch; Task 26's evidence; version `0.3.0` (Task 24); the `v0.2.0` name the pre-lifecycle refusal of Task 8 prints.
- Produces: `feat/lifecycle` and the tags `v0.2.0` and `v0.3.0` on `origin`, never `main`; `main` moves only at the owner's cutover.

- [ ] **Step 1: Write the failing test**

The published state is the test:

```
git -C <worktree> fetch -q origin && git -C <worktree> ls-remote --exit-code origin refs/heads/feat/lifecycle refs/tags/v0.2.0 refs/tags/v0.3.0; echo $?
```

- [ ] **Step 2: Run it and watch it fail**

Expected: exit 2 (no ref matched): nothing is published yet.

- [ ] **Step 3: Implement**

**3a. The branch review.** Dispatch one reviewer, model `claude-opus-5` (a whole-branch review), with this prompt:

```
tm-task: none — branch review of feat/lifecycle, which is not a tm task
Model: claude-opus-5
Read every file in /Users/taigo.pedrosa/Documents/SocialSrc/.claude/rules/ yourself before your first probe.
Review the branch feat/lifecycle in <worktree> against docs/superpowers/specs/2026-09-24-lifecycle-redesign.md and the Global Constraints, Review Focus and Interface contract of docs/superpowers/plans/2026-09-24-lifecycle-redesign.md.
Scope: git -C <worktree> diff "$(git -C <worktree> merge-base origin/main HEAD)"...HEAD. Take the file list from --stat and --name-only, grep the changed paths for each shape below, and read only what a grep flags, in windows.
Gates at the tip, each in the foreground as cmd; echo $?, quoting command and exit code:
  uv run --directory <worktree> pytest -q
  uv run --directory <worktree> ruff check
  uv run --directory <worktree> ruff format --check
  uv run --directory <worktree> mypy
  node --test <worktree>/tests/workflow/
For each of the five Review Focus items, name the test that pins it and prove it load-bearing by a logic mutation in a worktree of your own making (git -C <worktree> worktree add --detach <your scratch>/review-<n> HEAD), restored by exact inverse edit and verified with git diff HEAD.
Shapes to grep for: a status written anywhere but through lifecycle.claim/advance or a ledgered verb; an UPDATE of nodes.status outside the claims engine; INSERT OR REPLACE on leases; a git push without HEAD:main or with --force; a rebase; a comment naming a task, spec section, plan or review; a test name stating history rather than behaviour; NodeStatus, VirtualStatus, IN_FLIGHT or tm run start surviving anywhere.
The end-to-end evidence is in the report of the run that preceded you; read it, and say where it does not show one of the five scenarios.
Findings: one line each — file, symbol, what breaks. Nothing to say is one line. Return the findings and the gate table.
```

**3b. Fix rounds.** Each finding is fixed test-first: the failing test that pins the defect, watched red, then the fix, then the four gates and `node --test`, then a commit naming the change (`fix(<area>): <what now holds>`). Rounds one and two go to `claude-sonnet-5`, a third round to `claude-opus-5`; each round's fix diff is re-reviewed by `claude-sonnet-5` against the findings it answers. A fix touching `src/taskmanager/engine/landing.py`, `claims.py`, `discovery.py`, `workflows/tm-wave.js` or a guide repeats Task 26 in a fresh `<worktree>/../tm-e2e-<n>` before the branch is published. The review is done when a re-review returns nothing open.

**3c. Publish.** The last pre-lifecycle release is the commit this branch was cut from; tag it, tag the tip, and push the branch and both tags by explicit refspec:

```
git -C <worktree> fetch -q origin && base=$(git -C <worktree> merge-base origin/main HEAD) && git -C <worktree> show "$base:pyproject.toml" | grep -qx 'version = "0.2.0"' && git -C <worktree> show HEAD:pyproject.toml | grep -qx 'version = "0.3.0"' && git -C <worktree> tag -a v0.2.0 -m "taskmanager 0.2.0, the last release before the lifecycle redesign" "$base" && git -C <worktree> tag -a v0.3.0 -m "taskmanager 0.3.0: one stored status, node-level landing, landings run by tm" HEAD && git -C <worktree> push origin refs/heads/feat/lifecycle:refs/heads/feat/lifecycle refs/tags/v0.2.0 refs/tags/v0.3.0; echo $?
```

Expected: exit 0. Never `main`, never `--force`: a refused push is reported with the worktree path and the tip's sha, and not retried.

- [ ] **Step 4: Run the tests and the gates**

```
git -C <worktree> ls-remote --exit-code origin refs/heads/feat/lifecycle 'refs/tags/v0.2.0^{}' 'refs/tags/v0.3.0^{}'; echo $?
git -C <worktree> rev-parse HEAD "$(git -C <worktree> merge-base origin/main HEAD)"
git -C <worktree> ls-remote origin refs/heads/main
git -C <worktree> rev-parse origin/main
node --test <worktree>/tests/workflow/; echo $?
uv run --directory <worktree> pytest -q; echo $?
uv run --directory <worktree> ruff check; echo $?
uv run --directory <worktree> ruff format --check; echo $?
uv run --directory <worktree> mypy; echo $?
```

Expected: `ls-remote` exits 0; `feat/lifecycle` and `v0.3.0^{}` both equal `HEAD`, and `v0.2.0^{}` equals the merge-base; the remote `main` equals the local `origin/main` measured before publishing (it did not move); every gate exits 0.

- [ ] **Step 5: Commit**

Every fix was committed in Step 3b; publishing adds tags, not commits.

```
git -C <worktree> status --short
git -C <worktree> log --oneline "$(git -C <worktree> merge-base origin/main HEAD)"..HEAD
```

Expected: an empty status, and the branch's commits, one per task and one per fix, each with a conventional subject and no AI-attribution trailer.
