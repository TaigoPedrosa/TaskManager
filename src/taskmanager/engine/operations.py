import hashlib
import logging
import subprocess
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import cached_property
from pathlib import Path
from typing import Any

from taskmanager.core.enums import (
    CONTAINERS,
    LedgerCommand,
    NodeKind,
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
from taskmanager.core.status import (
    SET_ASIDE,
    ConditionStage,
    DecisionEffect,
    DecisionStatus,
    Merge,
    Status,
)
from taskmanager.db.job_repo import JobRepository
from taskmanager.db.ledger_repo import LedgerRepository
from taskmanager.db.node_repo import NodeRepository, declared_files_of
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.engine import git as gitops
from taskmanager.engine.assets import (
    AssetError,
    AttachmentSource,
    is_project_relative,
    store_asset,
)
from taskmanager.engine.chains import TOP, landing_target
from taskmanager.engine.conditions import is_executable
from taskmanager.engine.config import ConfigStore, ProjectConfig, moved_defaults
from taskmanager.engine.decisions import (
    DecisionAnswer,
    DecisionData,
    DecisionOption,
    apply_effect,
    chosen_effect,
    read_decision,
    write_decision,
)
from taskmanager.engine.git import GitManager
from taskmanager.engine.selection import ordered_repos
from taskmanager.engine.snapshot import (
    SnapshotBuilder,
    node_busy,
    roll_up_ancestors,
    sensitive_areas,
    stored_status,
    with_tops,
    writes_migration,
)
from taskmanager.engine.stepgraph import Snapshot
from taskmanager.engine.validation import moved_tops, retargets, validate
from taskmanager.engine.verification import (
    VerificationEngine,
    VerificationResult,
    codegraph_flags,
)

_log = logging.getLogger(__name__)

# The section a project bootstraps once and every `tm guide` overlay hangs off; `section set`
# points a user here when they try to write to it before it exists.
GUIDE_NODE = "guide"


def is_owed_key(section_key: str) -> bool:
    return section_key.strip().lower() == "owed"


def owed_refusal(node_id: str) -> str:
    return (
        "`owed` is not a section tm keeps: register the owed work as its own spec, plan or "
        f"task (with `tm import`) whose `depends_on` names {node_id}, so it waits on {node_id} "
        "instead of hiding in its text. Nothing was written."
    )


class OperationError(ValueError):
    """A refusal a user can act on; its message is shown verbatim by the CLI and the web."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


MERGE_REFUSAL = "merge is parent or spec; main is now spec"


def _land_on(land_on: str | None) -> dict[str, Any]:
    # land_on lives in frontmatter, where the write's own check reads it and refuses it off a spec.
    return {} if land_on is None else {"land_on": land_on}


def parse_merge(value: str) -> Merge:
    """Every merge value a user writes, through import, the CLI or the web, is read here."""
    try:
        return Merge(value)
    except ValueError:
        raise OperationError(MERGE_REFUSAL) from None


def child_defaults(
    child: Node,
    parent: Node | None,
    verifications: Sequence[NodeVerification] = (),
    *,
    review: bool | None = None,
    fix: bool | None = None,
    merge: Merge | None = None,
) -> Node:
    """`child` carrying each review, fix and merge flag its write states, and a default for each
    one it leaves out. A reviewed parent's one review reads what lands on the parent's branch,
    so a child under it lands there with no review of its own unless it is sensitive; anywhere
    else a task reviews and fixes itself and lands on its spec's target, and a container does
    neither."""
    if parent is None or not parent.review:
        own, lands_on = child.kind == NodeKind.TASK, Merge.SPEC
    else:
        files = declared_files_of(child, list(verifications))
        own, lands_on = bool(sensitive_areas(child)) or writes_migration(files), Merge.PARENT
    return child.model_copy(
        update={
            "review": own if review is None else review,
            "fix": own if fix is None else fix,
            "merge": lands_on if merge is None else merge,
        }
    )


# Refusals that conflict with the tree's current state rather than with the request itself.
_CONFLICT_RULES = frozenset({4, 6, 7, 8, 13})


class GitBranchFacts:
    """What the write rules need to know about a node's branches, read from git.

    A branch's recorded base is where it forks from its current landing target, so a new
    target keeps the base exactly when the branch forks from the new target at the same commit.
    """

    def __init__(
        self, root: Path, node_repo: NodeRepository, tree: Snapshot, only: str | None = None
    ) -> None:
        self.root = root
        self.node_repo = node_repo
        self.tree = tree
        # One repository's clone alone, for a tree whose tops were read in that repository.
        self.only = only

    @cached_property
    def _branches(self) -> ProjectConfig:
        return ConfigStore(self.root).branches()

    def _branch(self, node_id: str) -> str:
        node = self.node_repo.get_node(node_id)
        return node.branch if node is not None and node.branch else f"tm/{node_id}"

    def _repos(self, node_id: str) -> list[tuple[str | None, Path]]:
        ids = [node_id, *self.tree.descendants(node_id)] if node_id in self.tree.nodes else []
        names = sorted(
            {repo for i in ids if (snap := self.tree.nodes.get(i)) and (repo := snap.repo)}
        )
        dirs: list[tuple[str | None, Path]] = [
            (name, self.root / name) for name in names if self.only in (None, name)
        ]
        if not names and self.only is None:
            dirs = [(None, self.root)]
        return [(name, d) for name, d in dirs if (d / ".git").exists()]

    @staticmethod
    def _git(repo: Path, *args: str) -> str | None:
        res = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False)
        return res.stdout.strip() if res.returncode == 0 else None

    def _has(self, repo: Path, ref: str) -> bool:
        return self._git(repo, "rev-parse", "--verify", "--quiet", ref) is not None

    def _ref(self, name: str | None, repo: Path, target: str, top: str) -> str:
        # A container branch not yet cut in this repository would be cut from its own base, and
        # a top branch not yet on origin from its repository's default branch.
        while not target.startswith(TOP):
            branch = self._branch(target)
            if self._has(repo, f"refs/heads/{branch}"):
                return branch
            target = landing_target(self.tree, target)
        tops = (f"origin/{top}", f"origin/{self._branches.default_branch(name)}")
        return next((ref for ref in tops if self._has(repo, ref)), top)

    def branch_exists(self, node_id: str) -> bool:
        ref = f"refs/heads/{self._branch(node_id)}"
        return any(self._has(repo, ref) for _name, repo in self._repos(node_id))

    def base_matches(self, node_id: str, new_target: str, new_top: str) -> bool:
        branch = self._branch(node_id)
        current = landing_target(self.tree, node_id)
        top = self.tree.nodes[node_id].top
        for name, repo in self._repos(node_id):
            if not self._has(repo, f"refs/heads/{branch}"):
                continue
            recorded = self._git(repo, "merge-base", branch, self._ref(name, repo, current, top))
            proposed = self._git(
                repo, "merge-base", branch, self._ref(name, repo, new_target, new_top)
            )
            if recorded is None or recorded != proposed:
                return False
        return True


@contextmanager
def validated_write(
    node_repo: NodeRepository,
    snapshots: SnapshotBuilder,
    touched: set[str],
    prefix: str = "Nothing changed: ",
    restoring: bool = False,
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
        refusals = validate(
            before, after, scope, GitBranchFacts(root, node_repo, before), root, restoring
        )
        if refusals:
            code = 409 if any(r.rule in _CONFLICT_RULES for r in refusals) else 400
            raise OperationError(prefix + "; ".join(r.message for r in refusals), code)


class Operations:
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

    def busy(self, node_id: str) -> bool:
        return node_busy(self.runtime_repo, self.job_repo, node_id)

    # Where a node lands, read the same way by claims, landing and the rollup's completion.

    def branch_of(self, node_id: str) -> str:
        node = self.node_repo.get_node(node_id)
        return (node.branch if node is not None else None) or f"tm/{node_id}"

    def landing_branch(self, node_id: str, repo: str | None = None) -> str:
        """The branch `node_id`'s chain lands on at the top: its spec's `land_on`, else
        `repos.<repo>.default_branch` for `repo`, or with none named, for the node's own
        repository: its target_repo, else a container's first repository by name."""
        root, seen = node_id, {node_id}
        while (parents := self.node_repo.get_parent_ids(root)) and parents[0] not in seen:
            root = parents[0]
            seen.add(root)
        spec = self.node_repo.get_node(root)
        land_on = spec.frontmatter.get("land_on") if spec is not None else None
        if land_on:
            return str(land_on)
        if repo is None:
            node = self.node_repo.get_node(node_id)
            own = node.target_repo if node is not None else None
            repo = own or min(self.repos_of(node_id), default=None)
        return self.default_branch(repo)

    def default_branch(self, repo: str | None) -> str:
        return ConfigStore(self._project_root()).branches().default_branch(repo)

    def check_default_branches(self, before: ProjectConfig, after: ProjectConfig) -> None:
        """Refuses moving a repository's default branch from `before` to `after` where the same
        move through a spec's `land_on` is refused: a branch cut there that would land on the new
        one from another base (rule 4, read per repository), a check naming `origin/main` moved
        off it (rule 5), or a wait drawn across targets (rule 13)."""
        root, tree = self._project_root(), self.snapshots.build()
        refusals = []
        for repo in moved_defaults(before, after):
            old = with_tops(tree, before, repo)
            facts = GitBranchFacts(root, self.node_repo, old, only=repo)
            refusals += retargets(old, with_tops(tree, after, repo), facts)
        refusals += moved_tops(with_tops(tree, before), with_tops(tree, after))
        if refusals:
            raise OperationError("Nothing changed: " + "; ".join(r.message for r in refusals), 409)

    def landing_parent(self, node_id: str) -> str | None:
        """The parent whose branch `node_id` lands on, or None when it lands at the top."""
        node = self.node_repo.get_node(node_id)
        parents = self.node_repo.get_parent_ids(node_id)
        return parents[0] if node is not None and node.merge == Merge.PARENT and parents else None

    def target_of(self, node_id: str, repo: str | None = None) -> str:
        """The branch `node_id` lands on: its parent's branch, or its chain's top branch."""
        parent = self.landing_parent(node_id)
        return self.branch_of(parent) if parent else self.landing_branch(node_id, repo)

    def target_ref(self, node_id: str, repo: str | None = None) -> str:
        """The ref `node_id`'s landing target is read at: a top branch only through its fetched
        `origin/` ref; container branches are local refs in the shared clones."""
        parent = self.landing_parent(node_id)
        return self.branch_of(parent) if parent else f"origin/{self.landing_branch(node_id, repo)}"

    def counted_descendants(self, node_id: str) -> list[str]:
        """The descendants a container still counts: a set-aside node never lands, so neither it
        nor anything under it adds a repository, a lock or a verification to the container's."""
        found: list[str] = []
        frontier = self.node_repo.get_children(node_id)
        while frontier:
            child = frontier.pop(0)
            node = self.node_repo.get_node(child)
            if node is None or node.status in SET_ASIDE:
                continue
            found.append(child)
            frontier.extend(self.node_repo.get_children(child))
        return found

    def repos_of(self, node_id: str, repo_order: Sequence[str] = ()) -> list[str]:
        """A task's target repository; a container's, the repositories of its counted
        descendants in landing order (`land_order`, then `repo_order`, then by name).

        Reads the node and its descendants straight from `node_repo` rather than through a built
        `Snapshot`: a single live claim or landing needs one node's repos, never the whole
        graph's. `engine.selection.repos_of` answers the same question in bulk, over a snapshot
        already built for a whole wave; the two share `ordered_repos` for the ordering itself."""
        node = self.node_repo.get_node(node_id)
        if node is None:
            return []
        if node.kind not in CONTAINERS:
            return [node.target_repo] if node.target_repo else []
        found = {
            child.target_repo
            for d in self.counted_descendants(node_id)
            if (child := self.node_repo.get_node(d)) is not None and child.target_repo
        }
        return ordered_repos(found, node.land_order, repo_order)

    def nothing_to_land(self, container_id: str) -> bool:
        """True when the container's branch changes nothing against its landing target in every
        repository its counted tasks name. No repository named, a git error, or a repository
        not cloned here reads as a change: nothing then proves the code is on its target."""
        repos = self.repos_of(container_id)
        branch = self.branch_of(container_id)
        root = self._project_root()
        return bool(repos) and all(
            (root / repo / ".git").exists()
            and (
                not gitops.rev_parse(root / repo, f"refs/heads/{branch}")
                or gitops.diff_quiet(root / repo, self.target_ref(container_id, repo), branch)
            )
            for repo in repos
        )

    def drop_worktrees(self, node_id: str) -> None:
        """A completed node's code is on its target, so the worktrees its steps worked in go.
        Its branch stays: a later sync reads it as the carrier of that code. A worktree git
        refuses to remove, one holding uncommitted work, stays and is logged."""
        # Set-aside nodes count here: a container whose children were all superseded still
        # holds the worktrees its steps cut in their repositories.
        repos: set[str] = set()
        frontier = [node_id]
        while frontier:
            current = frontier.pop()
            node = self.node_repo.get_node(current)
            if node is not None and node.target_repo:
                repos.add(node.target_repo)
            frontier.extend(self.node_repo.get_children(current))
        branch, root = self.branch_of(node_id), self._project_root()
        # ponytail: one `git worktree list` per completed node and repository; list each
        # repository once per commit if restoring a large estate into clones gets slow.
        for repo in sorted(repos):
            # A restored estate need not have every repository cloned.
            if not (root / repo / ".git").exists():
                continue
            manager = GitManager(root / repo)
            try:
                worktree = manager.find_worktree(branch)
                if worktree is not None:
                    manager.remove_worktree(worktree)
            except (subprocess.CalledProcessError, OSError) as exc:
                detail = getattr(exc, "stderr", None) or exc
                _log.warning("kept the worktree of %s in %s: %s", node_id, repo, detail)

    def _ledger(
        self,
        command: LedgerCommand | str,
        target_id: str | None = None,
        payload: dict[str, Any] | None = None,
        diff: dict[str, Any] | None = None,
    ) -> None:
        self.ledger_repo.append(
            LedgerEvent(
                actor_id=self.actor,
                command=command,
                target_id=target_id,
                payload=payload or {},
                diff=diff or {},
            )
        )

    # -- spec / plan / task creation -------------------------------------------------------

    @staticmethod
    def _validate_priority(priority: int) -> None:
        if not 1 <= priority <= 100:
            raise OperationError("priority is 1-100", 400)

    def add_spec(
        self,
        title: str,
        slug: str | None = None,
        priority: int = 50,
        order: int = 0,
        review: bool = False,
        fix: bool = False,
        land_on: str | None = None,
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
            merge=Merge.SPEC,
            frontmatter=_land_on(land_on),
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
                **_land_on(land_on),
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
        review: bool | None = None,
        fix: bool | None = None,
        merge: str | None = None,
        land_on: str | None = None,
    ) -> str:
        self._validate_priority(priority)
        parent = self.node_repo.get_node(spec)
        if parent is None:
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

        plan_node = child_defaults(
            Node(
                id=plan_id,
                kind=NodeKind.PLAN,
                title=title,
                priority=priority,
                ordinal=order,
                status=Status.READY,
                frontmatter=_land_on(land_on),
            ),
            parent,
            review=review,
            fix=fix,
            merge=None if merge is None else parse_merge(merge),
        )
        self._refuse_fix_without_review(plan_node)
        with self._checked({plan_id}):
            self.node_repo.save_node(plan_node)
            self.node_repo.add_relation(
                NodeRelation(source_id=spec, target_id=plan_id, relation_type=RelationType.CONTAINS)
            )
            roll_up_ancestors(self, plan_id)
        self._ledger(
            LedgerCommand.PLAN_ADD,
            target_id=plan_id,
            payload={
                "title": title,
                "spec": spec,
                "ordinal": order,
                "review": plan_node.review,
                "fix": plan_node.fix,
                "merge": plan_node.merge.value,
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
        review: bool | None = None,
        fix: bool | None = None,
        merge: str | None = None,
        requires: list[str] | None = None,
        frontmatter: dict[str, Any] | None = None,
        repo: str | None = None,
    ) -> str:
        self._validate_priority(priority)
        parent = self.node_repo.get_node(plan)
        if parent is None:
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

        task_node = child_defaults(
            Node(
                id=task_id,
                kind=NodeKind.TASK,
                title=title,
                priority=priority,
                ordinal=order,
                acceptable_models=models or [],
                frontmatter=frontmatter or {},
                status=Status.READY,
                requires=requires or [],
                target_repo=repo,
            ),
            parent,
            review=review,
            fix=fix,
            merge=None if merge is None else parse_merge(merge),
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
            roll_up_ancestors(self, task_id)

        self._ledger(
            LedgerCommand.TASK_ADD, target_id=task_id, payload={"title": title, "plan": plan}
        )
        return task_id

    # -- node update / dependencies / supersede / move --------------------------------------

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
        merge: str | None = None,
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
            node.merge = parse_merge(merge)
            changed["merge"] = node.merge.value
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
            self.node_repo.save_node(node, keep_cycle=True)
        self._ledger(LedgerCommand.TASK_UPDATE, target_id=node_id, payload=changed)
        return changed

    def set_dependencies(self, node_id: str, add: list[str], remove: list[str]) -> list[str]:
        if self.node_repo.get_node(node_id) is None:
            raise OperationError(f"Task '{node_id}' not found", 404)
        current = set(self.node_repo.get_dependencies(node_id))
        problems = [
            f"'{dep}' does not exist" for dep in add if self.node_repo.get_node(dep) is None
        ]
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
        if old_node.kind == NodeKind.DECISION:
            raise OperationError(f"'{old_id}' is a decision; use `tm decision` to close it", 400)
        if new_id == old_id or self.node_repo.get_node(new_id) is None:
            raise OperationError(f"Replacement task '{new_id}' not found; nothing was changed", 400)

        old_node.status = Status.SUPERSEDED
        old_node.claimed_from = None
        old_node.updated_at = datetime.now(tz=UTC)
        touched = {old_id, new_id, *self.node_repo.get_blocked_by(old_id)}
        with self._checked(touched):
            self.node_repo.save_node(old_node)
            self.node_repo.add_relation(
                NodeRelation(
                    source_id=new_id, target_id=old_id, relation_type=RelationType.SUPERSEDES
                )
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
            roll_up_ancestors(self, old_id)

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
                NodeRelation(
                    source_id=plan_id, target_id=task_id, relation_type=RelationType.CONTAINS
                )
            )
            roll_up_ancestors(self, task_id, arrived=old_plan != plan_id)
            if old_plan is not None:
                roll_up_ancestors(self, old_plan, include_self=True)
        self._ledger(
            LedgerCommand.TASK_MOVE, target_id=task_id, payload={"from": old_plan, "to": plan_id}
        )

    # -- sections -------------------------------------------------------------------------

    def _write_section(
        self, node_id: str, section_key: str, content: str, header: str | None
    ) -> None:
        if is_owed_key(section_key):
            raise OperationError(owed_refusal(node_id), 400)
        existing_secs = self.node_repo.get_all_sections(node_id)
        existing = next((s for s in existing_secs if s.section_key == section_key), None)
        stored = existing.header if existing else None
        sec_header = header or f"## {section_key.capitalize()}"
        ordinal = existing.ordinal if existing else len(existing_secs) + 1
        # The header renders above the content, so content opening with the section's own header
        # line would show it twice.
        for leaked_header in {sec_header, stored}:
            if leaked_header and content.startswith(f"{leaked_header}\n"):
                content = content[len(leaked_header) + 1 :]
                break
        self.node_repo.save_section(
            NodeSection(
                node_id=node_id,
                section_key=section_key,
                ordinal=ordinal,
                header=sec_header,
                content=content,
            )
        )

    def append_section(self, node_id: str, section_key: str, text: str) -> None:
        """`text` added as a new paragraph at the end of the section, which is created if absent.
        Joins the caller's transaction and writes no ledger entry of its own."""
        existing = self.node_repo.get_section(node_id, section_key)
        content = f"{existing.content}\n\n{text}" if existing and existing.content else text
        self._write_section(node_id, section_key, content, existing.header if existing else None)

    def set_section(
        self, node_id: str, section_key: str, content: str, header: str | None = None
    ) -> None:
        if self.node_repo.get_node(node_id) is None:
            hint = (
                " Create it once with `tm spec add 'Project guide' --slug guide`."
                if node_id == GUIDE_NODE
                else ""
            )
            raise OperationError(f"No node '{node_id}' to hold the section.{hint}", 404)
        self._write_section(node_id, section_key, content, header)
        self._ledger(LedgerCommand.SECTION_SET, target_id=f"{node_id}:{section_key}")

    def remove_section(self, node_id: str, section_key: str) -> None:
        if not self.node_repo.remove_section(node_id, section_key):
            raise OperationError(f"No section '{section_key}' on node '{node_id}'", 404)
        self._ledger(LedgerCommand.SECTION_REMOVE, target_id=f"{node_id}:{section_key}")

    # -- verifications ----------------------------------------------------------------------

    def add_verification(
        self,
        task_id: str,
        verification_type: VerificationType,
        target: str,
        pattern: str | None = None,
        query_json: str | None = None,
    ) -> NodeVerification:
        if self.node_repo.get_node(task_id) is None:
            raise OperationError(f"task '{task_id}' not found", 404)
        try:
            codegraph_flags(query_json)
        except ValueError as e:
            raise OperationError(f"{task_id}: codegraph_query {target!r} {e}", 400) from None
        ver = NodeVerification(
            node_id=task_id,
            verification_type=verification_type,
            target_path=target,
            expected_pattern=pattern,
            codegraph_query_json=query_json,
        )
        with self._checked({task_id}):
            self.node_repo.add_verification(ver)
            self._ledger(
                LedgerCommand.VERIFICATION_ADD,
                target_id=task_id,
                payload={"type": verification_type.value, "target": target},
            )
        return ver

    def remove_verification(self, task_id: str, verification_id: int) -> None:
        if not self.node_repo.remove_verification(task_id, verification_id):
            raise OperationError(f"Task '{task_id}' has no verification {verification_id}", 404)
        self._ledger(
            "verify remove", target_id=task_id, payload={"verification_id": verification_id}
        )

    def run_verifications(
        self, task_id: str | None, ref: str | None = None
    ) -> tuple[bool, list[VerificationResult]]:
        if ref is not None and not task_id:
            raise OperationError("--ref checks one task's repo; pass a task id", 400)

        repo_for_node: dict[str, str | None] = {}
        if task_id:
            node = self.node_repo.get_node(task_id)
            if node is None:
                raise OperationError(f"task '{task_id}' not found", 404)
            repo_for_node[task_id] = node.target_repo
            vers = self.node_repo.get_verifications(task_id)
        else:
            vers = []
            for t in self.node_repo.list_nodes(kind=NodeKind.TASK):
                repo_for_node[t.id] = t.target_repo
                vers.extend(self.node_repo.get_verifications(t.id))

        if not vers:
            raise OperationError(
                "No verifications to run: an empty check set proves nothing. Add one with "
                "`tm verify add`, or attest the task.",
                400,
            )

        # With no ref asked for, each task is read where its chain lands at the top.
        branch_for_node = (
            None
            if ref is not None
            else {n: self.landing_branch(n, repo_for_node[n]) for n in {v.node_id for v in vers}}
        )
        results = self.verification_engine.verify_all(vers, repo_for_node, ref, branch_for_node)
        all_passed = all(r.passed for r in results)
        self._ledger(
            LedgerCommand.VERIFICATION_RUN,
            target_id=task_id,
            payload={"passed": all_passed, "count": len(results)},
        )
        return all_passed, results

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

    # -- decisions ------------------------------------------------------------------------------

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

    def _get_decision(self, decision_id: str) -> Node:
        node = self.node_repo.get_node(decision_id)
        if node is None or node.kind != NodeKind.DECISION:
            raise OperationError(f"decision '{decision_id}' not found", 404)
        return node

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
            status=DecisionStatus.OPEN,
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
        self._ledger(
            LedgerCommand.DECISION_ADD, target_id=decision_id, payload={"question": question}
        )
        return decision_id

    def answer_decision(
        self,
        decision_id: str,
        option: str | None = None,
        text: str = "",
        rationale: str = "",
        by: str = "cli",
    ) -> None:
        node = self._get_decision(decision_id)
        if stored_status(node) != DecisionStatus.OPEN:
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
        node.status = DecisionStatus.ANSWERED
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

    def reopen_decision(self, decision_id: str) -> None:
        node = self._get_decision(decision_id)
        if stored_status(node) == DecisionStatus.OPEN:
            raise OperationError(f"decision '{decision_id}' is already open", 409)
        data = read_decision(node)
        data.answer = None
        data.withdrawn_reason = ""
        data.withdrawn_by = None
        data.withdrawn_at = None
        write_decision(node, data)
        node.status = DecisionStatus.OPEN
        node.updated_at = datetime.now(tz=UTC)
        self.node_repo.save_node(node)
        self._ledger(LedgerCommand.DECISION_REOPEN, target_id=decision_id)

    def withdraw_decision(self, decision_id: str, reason: str = "") -> None:
        node = self._get_decision(decision_id)
        data = read_decision(node)
        data.withdrawn_reason = reason
        data.withdrawn_by = self.actor
        data.withdrawn_at = datetime.now(tz=UTC)
        write_decision(node, data)
        node.status = DecisionStatus.WITHDRAWN
        node.updated_at = datetime.now(tz=UTC)
        self.node_repo.save_node(node)
        self._ledger(
            LedgerCommand.DECISION_WITHDRAW, target_id=decision_id, payload={"reason": reason}
        )

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

    # -- attachments ------------------------------------------------------------------------

    def _assets_dir(self) -> Path:
        return self.node_repo.db.taskmanager_dir / "assets"

    def _project_root(self) -> Path:
        return self.node_repo.db.taskmanager_dir.parent

    def attach(
        self,
        node_id: str,
        file_path: Path,
        caption: str = "",
        source: str | None = None,
        replace: str | None = None,
    ) -> dict[str, Any]:
        node = self.node_repo.get_node(node_id)
        if node is None:
            raise OperationError(f"node '{node_id}' not found", 404)
        if not file_path.is_file():
            raise OperationError(f"'{file_path}' does not exist", 400)

        attachments = list(node.frontmatter.get("attachments") or [])
        replace_idx: int | None = None
        if replace is not None:
            replace_idx = next(
                (i for i, a in enumerate(attachments) if a.get("asset") == replace), None
            )
            if replace_idx is None:
                raise OperationError(f"no attachment '{replace}' on '{node_id}'", 404)

        try:
            asset_name, mime = store_asset(self._assets_dir(), file_path)
        except AssetError as exc:
            raise OperationError(str(exc), 400) from exc

        prior = attachments[replace_idx] if replace_idx is not None else {}
        effective_caption = caption or prior.get("caption", "")
        effective_source = source or (prior.get("source") or {}).get("uri")
        if effective_source is None:
            try:
                effective_source = str(file_path.resolve().relative_to(self._project_root()))
            except ValueError:
                effective_source = None

        source_sha: str | None = None
        source_state = "unverifiable"
        if effective_source is not None and is_project_relative(effective_source):
            src_path = self._project_root() / effective_source
            if src_path.is_file():
                source_sha = hashlib.sha256(src_path.read_bytes()).hexdigest()
                source_state = "fresh"
            else:
                source_state = "missing"

        entry = {
            "asset": asset_name,
            "name": file_path.name,
            "caption": effective_caption,
            "mime": mime,
            "source": AttachmentSource(
                uri=effective_source,
                sha256=source_sha,
                captured_at=datetime.now(tz=UTC),
                state=source_state,  # type: ignore[arg-type]
            ).model_dump(mode="json"),
        }
        if replace_idx is not None:
            attachments[replace_idx] = entry
        else:
            attachments.append(entry)
        node.frontmatter["attachments"] = attachments
        node.updated_at = datetime.now(tz=UTC)
        self.node_repo.save_node(node, keep_cycle=True)
        self._ledger(LedgerCommand.ATTACH, target_id=node_id, payload={"asset": asset_name})
        return entry

    def _asset_referenced(self, asset: str) -> bool:
        return any(
            a.get("asset") == asset
            for n in self.node_repo.list_nodes()
            for a in n.frontmatter.get("attachments") or []
        )

    def detach(self, node_id: str, asset: str) -> None:
        node = self.node_repo.get_node(node_id)
        if node is None:
            raise OperationError(f"node '{node_id}' not found", 404)
        attachments = list(node.frontmatter.get("attachments") or [])
        idx = next((i for i, a in enumerate(attachments) if a.get("asset") == asset), None)
        if idx is None:
            raise OperationError(f"no attachment '{asset}' on '{node_id}'", 404)
        del attachments[idx]
        node.frontmatter["attachments"] = attachments
        node.updated_at = datetime.now(tz=UTC)
        self.node_repo.save_node(node, keep_cycle=True)
        self._ledger(LedgerCommand.DETACH, target_id=node_id, payload={"asset": asset})
        # `save_node` above already persisted `node_id`'s attachments with the entry removed,
        # so checking every node (this one included) is correct: a second entry on `node_id`
        # itself pointing at the same content-addressed asset is exactly the case a per-node
        # exclusion used to miss, deleting the file while that second entry still referenced it.
        if not self._asset_referenced(asset):
            (self._assets_dir() / asset).unlink(missing_ok=True)

    def list_attachments(self, node_id: str, check: bool = False) -> list[dict[str, Any]]:
        node = self.node_repo.get_node(node_id)
        if node is None:
            raise OperationError(f"node '{node_id}' not found", 404)
        attachments = list(node.frontmatter.get("attachments") or [])
        if not check:
            return attachments

        changed = False
        for entry in attachments:
            source = dict(entry.get("source") or {})
            uri = source.get("uri")
            if not uri or not is_project_relative(uri):
                continue
            path = self._project_root() / uri
            if not path.is_file():
                state = "missing"
            else:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                state = "fresh" if digest == source.get("sha256") else "stale"
            source["state"] = state
            source["checked_at"] = datetime.now(tz=UTC).isoformat()
            entry["source"] = source
            changed = True

        if changed:
            node.frontmatter["attachments"] = attachments
            node.updated_at = datetime.now(tz=UTC)
            self.node_repo.save_node(node, keep_cycle=True)
            self._ledger(LedgerCommand.ATTACHMENT_CHECK, target_id=node_id)
        return attachments
