import json
import logging
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any, cast

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
from taskmanager.db.connection import (
    DatabaseManager,
    StateNotInitialized,
    StateSchemaTooNew,
    StateSchemaTooOld,
)
from taskmanager.db.job_repo import JobRepository
from taskmanager.db.ledger_repo import LedgerRepository
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.db.schema import STATE_SCHEMA_VERSION
from taskmanager.di.container import TaskManagerProvider
from taskmanager.engine.chains import landing_chain
from taskmanager.engine.claims import Blocker, Claims, DecisionSpec
from taskmanager.engine.config import ConfigError, ConfigStore
from taskmanager.engine.decisions import DECISION_STATUS_LABELS, read_decision
from taskmanager.engine.discovery import discover, djb2
from taskmanager.engine.heuristics import RecommendationEngine
from taskmanager.engine.landing import Landing
from taskmanager.engine.operations import GUIDE_NODE, OperationError, Operations
from taskmanager.engine.search import SearchEngine, SearchError
from taskmanager.engine.snapshot import (
    DisplayView,
    SnapshotBuilder,
    phase_of,
    stored_status,
    waits_on,
)
from taskmanager.renderers.importers import BulkImporter
from taskmanager.renderers.markdown import MarkdownRenderer

app = typer.Typer(
    name="taskmanager",
    help="Local agentic task tracker system powered by SQLite and DAG heuristics",
)
spec_app = typer.Typer(name="spec", help="Manage specifications")
plan_app = typer.Typer(name="plan", help="Manage plans")
task_app = typer.Typer(name="task", help="Manage tasks")
section_app = typer.Typer(name="section", help="Manage node sections")
run_app = typer.Typer(name="run", help="Execution coordination and leases")
wave_app = typer.Typer(name="wave", help="Batch-choosing for a dispatch wave")
verify_app = typer.Typer(name="verify", help="Static and AST verifications")
audit_app = typer.Typer(name="audit", help="Audit ledger event logs")
web_app = typer.Typer(name="web", help="Interactive web visualizer and exporter")
plugin_app = typer.Typer(name="plugin", help="Install and manage harness plugins")
config_app = typer.Typer(name="config", help="Project configuration (.taskmanager/config.yaml)")
decision_app = typer.Typer(name="decision", help="Raise and answer decisions")
job_app = typer.Typer(name="job", help="Landing and sync jobs")
land_app = typer.Typer(name="land", help="Start a node's landing")
condition_app = typer.Typer(name="condition", help="States outside the corpus a node waits on")
db_app = typer.Typer(name="db", help="The estate's state.db schema")

app.add_typer(spec_app)
app.add_typer(plan_app)
app.add_typer(task_app)
app.add_typer(section_app)
app.add_typer(run_app)
app.add_typer(wave_app)
app.add_typer(verify_app)
app.add_typer(audit_app)
app.add_typer(web_app)
app.add_typer(plugin_app)
app.add_typer(config_app)
app.add_typer(decision_app)
app.add_typer(job_app)
app.add_typer(land_app)
app.add_typer(db_app)
task_app.add_typer(condition_app)


def _emit(data: Any, as_yaml: bool = False) -> None:
    """Raw to stdout: `rich.print` wraps long lines inside strings and the output stops parsing.

    YAML carries the same document in fewer tokens, which is what an agent reading it pays for.
    """
    if as_yaml:
        import yaml

        class _Dumper(yaml.SafeDumper):  # type: ignore[misc]
            pass

        def _text(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
            style = "|" if "\n" in value else None
            return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)

        _Dumper.add_representer(str, _text)
        sys.stdout.write(
            yaml.dump(
                json.loads(json.dumps(data, default=str)),
                Dumper=_Dumper,
                sort_keys=False,
                allow_unicode=True,
                width=10_000,
            )
        )
        return
    sys.stdout.write(json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n")


# A restore reads only exports carrying this marker; an export without it came from a
# pre-lifecycle tm, whose statuses and gated edges this version does not store.
EXPORT_FORMAT: dict[str, Any] = {"format": "tm-lifecycle", "version": 1}


def _task_spec_id(node_repo: NodeRepository, task_id: str) -> str | None:
    """The spec that owns a task, walking up through any number of nested plans, or None when
    no ancestor is a spec -- what `--spec none` filters for."""
    return node_repo.get_ancestor_of_kind(task_id, NodeKind.SPEC)


def _node_row(node: Node, state: str | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": node.id,
        "kind": node.kind.value,
        "title": node.title,
        "status": node.status.value,
        "state": state or node.status.value,
        "priority": node.priority,
        "target_repo": node.target_repo,
        "acceptable_models": node.acceptable_models,
    }
    if node.kind != NodeKind.DECISION:
        # A decision has no cycle: it is never claimed, reviewed, fixed or landed.
        row |= {
            "phase": phase_of(node),
            "review": node.review,
            "fix": node.fix,
            "merge": node.merge.value,
        }
    return row


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


def _find_root(start: Path) -> Path | None:
    for candidate in (start, *start.parents):
        if (candidate / ".taskmanager").is_dir():
            return candidate
    return None


def _get_root(path: Path | None, *, must_exist: bool = True) -> Path:
    """`-C`, then `$TM_ROOT`, then the nearest ancestor holding a `.taskmanager`, then the same
    search from the repository a worktree was cut from (a worktree can live outside the root).

    A root with no database is an error, never a fresh empty one: opening it used to create a
    `.taskmanager` wherever the command happened to run, and every claim made in it was invisible.
    """
    if path is not None:
        root = path.resolve()
    elif os.environ.get("TM_ROOT"):
        root = Path(os.environ["TM_ROOT"]).resolve()
    else:
        cwd = Path.cwd().resolve()
        found = _find_root(cwd)
        if found is None:
            res = subprocess.run(
                ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                cwd=cwd,
                capture_output=True,
                text=True,
                check=False,
            )
            if res.returncode == 0 and res.stdout.strip():
                found = _find_root(Path(res.stdout.strip()).resolve().parent)
        root = found if found is not None else cwd
    if must_exist and not (root / ".taskmanager").is_dir():
        raise typer.BadParameter(
            f"no .taskmanager at {root}: pass -C, set TM_ROOT, or run `tm init` there"
        )
    return root


@contextmanager
def _user_errors() -> Iterator[None]:
    """A bad configuration or a failing provider is one line and exit 1, never a traceback."""
    try:
        yield
    except (ConfigError, SearchError) as exc:
        sys.stdout.write(f"{exc}\n")
        raise typer.Exit(code=1) from exc


def _get_container(path: Path | None) -> Container:
    root = _get_root(path, must_exist=False)
    _refuse_pre_lifecycle(root)
    # Opened once here so a state.db at any schema but this tm's refuses every command, even one
    # that only reads audit.db or cache.db.
    with DatabaseManager(root / ".taskmanager").get_state_connection():
        pass
    return make_container(TaskManagerProvider(root))


def _record_ledger(
    container: Container,
    command: LedgerCommand | str,
    target_id: str | None = None,
    actor_id: str = "cli",
    payload: dict[str, Any] | None = None,
    diff: dict[str, Any] | None = None,
) -> None:
    container.get(LedgerRepository).append(
        LedgerEvent(
            actor_id=actor_id,
            command=command,
            target_id=target_id,
            payload=payload or {},
            diff=diff or {},
        )
    )


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


# A plain string, not a choice of the enum, so a refused value reaches `parse_merge` and its
# message.
_MERGE_OPTION = typer.Option(
    "--merge",
    metavar="|".join(Merge),
    help="parent lands on the parent's branch; spec lands where its spec lands",
)


@plan_app.command("add")
def plan_add(
    title: str,
    spec: Annotated[str, typer.Option("--spec", help="Parent spec ID")],
    slug: Annotated[str | None, typer.Option("--slug", "-s", help="Plan slug")] = None,
    priority: Annotated[int, typer.Option("--priority", "-p", help="Priority")] = 50,
    order: Annotated[int, typer.Option("--order", "-o", help="Display order")] = 0,
    review: Annotated[
        bool | None,
        typer.Option("--review/--no-review", help="A review step follows the children"),
    ] = None,
    fix: Annotated[
        bool | None, typer.Option("--fix/--no-fix", help="A rejection is fixed on this plan")
    ] = None,
    merge: Annotated[str | None, _MERGE_OPTION] = None,
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


def _csv(raw: str | None) -> list[str]:
    return [x.strip() for x in (raw or "").split(",") if x.strip()]


_SET_OPTION = typer.Option(
    "--set",
    help="Frontmatter key=value, repeatable; the value is JSON when it parses "
    '(declared_files=\'["a","b"]\'), else text',
)


def _frontmatter_pairs(pairs: list[str] | None) -> dict[str, Any]:
    frontmatter: dict[str, Any] = {}
    for pair in pairs or []:
        key, sep, raw = pair.partition("=")
        if not sep or not key:
            raise typer.BadParameter(f"--set takes key=value, got '{pair}'")
        try:
            frontmatter[key] = json.loads(raw)
        except ValueError:
            frontmatter[key] = raw
    return frontmatter


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
        bool | None,
        typer.Option("--review/--no-review", help="A review step follows implement"),
    ] = None,
    fix: Annotated[
        bool | None, typer.Option("--fix/--no-fix", help="A rejection is fixed by this task")
    ] = None,
    merge: Annotated[str | None, _MERGE_OPTION] = None,
    requires: Annotated[
        str | None, typer.Option("--requires", help="Comma-separated agent capabilities")
    ] = None,
    set_frontmatter: Annotated[list[str] | None, _SET_OPTION] = None,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    root = _get_root(path)
    ops = _get_container(root).get(Operations)
    frontmatter = _frontmatter_pairs(set_frontmatter)
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
            frontmatter=frontmatter,
        )
    print(f"[green]Added task {task_id}[/green]")


@task_app.command("supersede")
def task_supersede(
    old_id: str,
    new_id: str,
    transfer_blocks: Annotated[
        str,
        typer.Option(
            "--transfer-blocks", help="Transfer blocks: all, none, or comma-separated task IDs"
        ),
    ] = TransferMode.ALL.value,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    root = _get_root(path)
    container = _get_container(root)
    ops = container.get(Operations)
    try:
        ops.supersede(old_id, new_id, transfer_blocks)
    except OperationError as exc:
        print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    print(f"[green]Task {old_id} superseded by {new_id}[/green]")


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


@task_app.command("update")
def task_update(
    task_id: str,
    title: Annotated[str | None, typer.Option("--title")] = None,
    priority: Annotated[int | None, typer.Option("--priority", "-p")] = None,
    models: Annotated[
        str | None, typer.Option("--models", help="Comma-separated acceptable models")
    ] = None,
    repo: Annotated[str | None, typer.Option("--repo", help="Target repository directory")] = None,
    set_frontmatter: Annotated[list[str] | None, _SET_OPTION] = None,
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
    merge: Annotated[str | None, _MERGE_OPTION] = None,
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
    frontmatter_set = _frontmatter_pairs(set_frontmatter)
    with _refusing():
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
    print(f"[green]Updated {task_id}: {', '.join(changed)}[/green]")


@task_app.command("move")
def task_move(
    task_id: str,
    plan: Annotated[str, typer.Option("--plan", help="New parent plan ID")],
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    """Re-parent a task: drop its old plan's `contains` edge and add the new one."""
    root = _get_root(path)
    container = _get_container(root)
    ops = container.get(Operations)
    try:
        ops.move_task(task_id, plan)
    except OperationError as exc:
        print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    print(f"[green]Moved {task_id} to {plan}[/green]")


@task_app.command("get")
def task_get(
    task_id: str,
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
    yaml_output: Annotated[
        bool, typer.Option("--yaml", help="Output as YAML (fewer tokens than JSON)")
    ] = False,
    fields: Annotated[
        str | None,
        typer.Option("--fields", help="Comma-separated keys to print; requires --json"),
    ] = None,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    if fields is not None and not json_output:
        print("[red]--fields requires --json[/red]")
        raise typer.Exit(code=1)
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
        waiting, awaiting = waits_on(snapshot, task)
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
                "blocked_by": [d for d, n in deps.items() if n is None] + waiting,
                "awaiting_decisions": awaiting,
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
        if fields is not None:
            requested = [f.strip() for f in fields.split(",") if f.strip()]
            unknown = [f for f in requested if f not in doc]
            if unknown:
                valid = ", ".join(sorted(doc))
                print(f"[red]unknown field(s): {', '.join(unknown)} (valid: {valid})[/red]")
                raise typer.Exit(code=1)
            doc = {f: doc[f] for f in requested}
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
    _emit(
        {
            "action": result.action.value,
            "reason": result.reason,
            "model": result.model,
            "job": result.job,
            "repos": list(result.repos),
            "branch": result.branch,
            "base": result.base,
            "worktree": result.worktree,
            "worktrees": result.worktrees,
            "token": result.token,
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
    token: Annotated[
        str | None,
        typer.Option("--token", help="Refused unless the node's live lease is this claim's"),
    ] = None,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    """Close an implement or a fix step."""
    with _refusing():
        status = _claims(_get_root(path)).complete(node_id, agent=agent, token=token)
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
    token: Annotated[
        str | None,
        typer.Option("--token", help="Refused unless the node's live lease is this claim's"),
    ] = None,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    """Close a review step; the node's :review section must have changed since the claim."""
    if approve == reject:
        raise typer.BadParameter("give exactly one of --approve or --reject")
    with _refusing():
        status = _claims(_get_root(path)).review(
            node_id, approve, verdict, agent=agent, token=token
        )
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
    token: Annotated[
        str | None,
        typer.Option("--token", help="Refused unless the node's live lease is this claim's"),
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
        status = _claims(_get_root(path)).release(
            node_id, blocked=blocker, agent=agent, token=token
        )
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
        Status,
        typer.Option("--to", help="READY, IMPLEMENTED, REVIEWED, FIXED, LANDED or COMPLETED"),
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


def _dotted_get(doc: dict[str, Any], dotted: str) -> tuple[bool, Any]:
    """(True, value) once the top-level name is a real field; a nested name a run's `result` never
    populated is a known field with nothing there, not an unknown one, so it resolves to None."""
    head, _, rest = dotted.partition(".")
    if head not in doc:
        return False, None
    cur: Any = doc[head]
    for part in rest.split(".") if rest else []:
        if not isinstance(cur, dict) or part not in cur:
            return True, None
        cur = cur[part]
    return True, cur


@job_app.command("status")
def job_status(
    job_id: str,
    wait: Annotated[
        int,
        typer.Option("--wait", min=0, help="Block up to this many seconds while the job runs"),
    ] = 0,
    fields: Annotated[
        str | None,
        typer.Option("--fields", help="Comma-separated keys to print; dotted selects nested"),
    ] = None,
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
    doc = job.model_dump(mode="json")
    if fields is not None:
        requested = [f.strip() for f in fields.split(",") if f.strip()]
        picked: dict[str, Any] = {}
        unknown = []
        for f in requested:
            found, value = _dotted_get(doc, f)
            if not found:
                unknown.append(f)
            else:
                picked[f] = value
        if unknown:
            valid = ", ".join(sorted(doc))
            print(f"[red]unknown field(s): {', '.join(unknown)} (valid: {valid})[/red]")
            raise typer.Exit(code=1)
        doc = picked
    _emit(doc, yaml_output)


@job_app.command("resume")
def job_resume(
    job_id: str,
    own_defect: Annotated[
        str | None, typer.Option("--own-defect", help="The node's own defect, as a finding")
    ] = None,
    push: Annotated[
        bool, typer.Option("--push", help="An unattributed red is not this node's: push")
    ] = False,
    agent: Annotated[
        str | None,
        typer.Option("--agent", help="Refused unless the node's live lease is this agent's"),
    ] = None,
    token: Annotated[
        str | None,
        typer.Option("--token", help="Refused unless the node's live lease is this claim's"),
    ] = None,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    """How an agent finishes a landing or sync job that stopped for it."""
    if own_defect is not None and push:
        raise typer.BadParameter("--own-defect and --push contradict each other")
    landing = _landing(_get_root(path))
    with _refusing():
        state = landing.resume(job_id, own_defect=own_defect, push=push, agent=agent, token=token)
    print(f"[green]Job {job_id}: {state.value}[/green]")


@land_app.command("start")
def land_start(
    node_id: str,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    """Start the landing of a node a merge claim holds, as a detached job, and print its id."""
    landing = _landing(_get_root(path))
    with _refusing():
        job_id = landing.start_land(node_id)
    sys.stdout.write(f"{job_id}\n")


@section_app.command("get")
def section_get(
    qualified_path: str,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    root = _get_root(path)
    container = _get_container(root)
    node_repo = container.get(NodeRepository)
    qp = QualifiedPath.parse(qualified_path)

    if not qp.section_key:
        secs = node_repo.get_all_sections(qp.node_id)
        if not secs:
            print(f"[yellow]No sections found for node '{qp.node_id}'[/yellow]")
            return
        for s in secs:
            sys.stdout.write(f"{s.header}\n{s.content}\n\n")
        return

    sec = node_repo.get_section(qp.node_id, qp.section_key)
    if not sec:
        print(f"[red]Section '{qp.section_key}' not found on node '{qp.node_id}'[/red]")
        raise typer.Exit(code=1)
    # Header on stderr, content alone on stdout, no added newline: `tm section get id:key > f`
    # then `tm section set id:key -f f` round-trips byte-identical.
    if sec.header:
        sys.stderr.write(f"{sec.header}\n")
    sys.stdout.write(sec.content)


@section_app.command("set")
def section_set(
    qualified_path: str,
    content: Annotated[str | None, typer.Argument(help="Section content")] = None,
    content_opt: Annotated[
        str | None, typer.Option("--content", help="Section content text")
    ] = None,
    file: Annotated[
        typer.FileText | None,
        typer.Option("--file", "-f", encoding="utf-8", help="Read content from file, - for stdin"),
    ] = None,
    header: Annotated[
        str | None, typer.Option("--header", "-h", help="Section markdown header")
    ] = None,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    root = _get_root(path)
    container = _get_container(root)
    ops = container.get(Operations)
    qp = QualifiedPath.parse(qualified_path)

    if not qp.section_key:
        print(f"[red]Qualified path must include section key (e.g. {qp.node_id}:steps)[/red]")
        raise typer.Exit(code=1)

    text_content = ""
    if file:
        text_content = file.read()
    elif content_opt is not None:
        text_content = content_opt
    elif content is not None:
        text_content = content

    # Without --header the stored one stays, so `section get | section set --file -` round-trips.
    stored = container.get(NodeRepository).get_section(qp.node_id, qp.section_key)
    try:
        ops.set_section(
            qp.node_id, qp.section_key, text_content, header or (stored.header if stored else None)
        )
    except OperationError as exc:
        print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    print(f"[green]Saved section {qualified_path}[/green]")


@section_app.command("remove")
def section_remove(
    qualified_path: str,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    root = _get_root(path)
    container = _get_container(root)
    ops = container.get(Operations)
    qp = QualifiedPath.parse(qualified_path)

    if not qp.section_key:
        print(f"[red]Qualified path must include section key (e.g. {qp.node_id}:steps)[/red]")
        raise typer.Exit(code=1)
    try:
        ops.remove_section(qp.node_id, qp.section_key)
    except OperationError as exc:
        print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=1) from exc
    print(f"[green]Removed section {qualified_path}[/green]")


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
    hold_merge: Annotated[
        list[str] | None,
        typer.Option("--hold-merge", help="Node id whose merge is not offered, repeatable"),
    ] = None,
    lines: Annotated[
        bool,
        typer.Option(
            "--lines",
            help="Quote-free lines instead of JSON: `N id action model kind repos requires`, "
            "`H <held>`, `W <waiting>`; an empty list prints `-`",
        ),
    ] = False,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    """One dispatch wave's batch: a JSON payload line, then `__CHECK n=<chosen> h=<djb2>`.

    A caller with no shell of its own (a Workflow script) echoes the two lines back verbatim;
    the checksum lets the caller reject a transcription that is not byte-exact. `--lines` exists
    for a small-model runner, which drops keys when it retypes nested JSON into a string field.
    """
    claims = _claims(_get_root(path))
    payload, chosen_count = discover(
        claims, spec or None, session, slots, max_strong, exclude, hold_merge
    )
    if not lines:
        sys.stdout.write(f"{payload}\n__CHECK n={chosen_count} h={djb2(payload)}\n")
        return
    data = json.loads(payload)

    def flat(values: list[str] | None) -> str:
        return ",".join(values or []) or "-"

    out = [
        f"N {n['id']} {n['action']} {n['model']} {n['kind']} "
        f"{flat(n.get('repos'))} {flat(n.get('requires'))}"
        for n in data["chosen"]
    ]
    # A held reason can quote free text, such as a condition's --needs, and the caller reads one
    # record per line.
    out += [f"H {' '.join(held.split())}" for held in data.get("held", [])]
    out.append(f"W {data.get('waiting_for_slot', 0)}")
    sys.stdout.write("\n".join(out) + "\n")


@verify_app.command("add")
def verify_add(
    task_id: str,
    type: Annotated[
        VerificationType,
        typer.Option("--type", "-t", help="Verification type (e.g. file_exists, test_command)"),
    ],
    target: Annotated[str, typer.Option("--target", help="Target path or command")],
    pattern: Annotated[
        str | None, typer.Option("--pattern", help="Expected pattern or test command")
    ] = None,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    root = _get_root(path)
    container = _get_container(root)
    ops = container.get(Operations)
    with _refusing():
        ops.add_verification(task_id, type, target, pattern)
    print(f"[green]Added {type.value} verification to task {task_id}[/green]")


@verify_app.command("list")
def verify_list(
    task_id: str,
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
    yaml_output: Annotated[
        bool, typer.Option("--yaml", help="Output as YAML (fewer tokens than JSON)")
    ] = False,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    """List a task's verifications with the ids `tm verify remove` takes."""
    root = _get_root(path)
    node_repo = _get_container(root).get(NodeRepository)
    if node_repo.get_node(task_id) is None:
        print(f"[red]Task '{task_id}' not found[/red]")
        raise typer.Exit(code=1)
    rows = [
        {
            "id": v.id,
            "type": v.verification_type.value,
            "target_path": v.target_path,
            "expected_pattern": v.expected_pattern,
        }
        for v in node_repo.get_verifications(task_id)
    ]
    if json_output or yaml_output:
        _emit(rows, yaml_output)
        return
    table = Table(title=f"Verifications of {escape(task_id)}")
    for column in ("ID", "Type", "Target", "Pattern"):
        table.add_column(column)
    for r in rows:
        table.add_row(
            str(r["id"]),
            str(r["type"]),
            escape(str(r["target_path"])),
            escape(str(r["expected_pattern"] or "")),
        )
    print(table)


@verify_app.command("remove")
def verify_remove(
    task_id: str,
    verification_id: int,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    """Remove one verification by the id `tm verify list` shows."""
    root = _get_root(path)
    container = _get_container(root)
    ops = container.get(Operations)
    try:
        ops.remove_verification(task_id, verification_id)
    except OperationError as exc:
        print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=1) from exc
    print(f"[green]Removed verification {verification_id} from {escape(task_id)}[/green]")


@verify_app.command("run")
def verify_run(
    task_id: Annotated[str | None, typer.Argument(help="Task ID to verify")] = None,
    ref: Annotated[
        str | None,
        typer.Option(
            "--ref",
            help=(
                "Git ref to check the task's path verifications against (e.g. tm/<task-id>), "
                "read as-is with no fetch. Default: origin/main, fetched first. Also exported "
                "to a test_command as TM_VERIFY_REF, unset when --ref is omitted."
            ),
        ),
    ] = None,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    root = _get_root(path)
    container = _get_container(root)
    ops = container.get(Operations)
    runtime_repo = container.get(RuntimeRepository)

    target_tid = task_id
    if not target_tid:
        try:
            target_tid = _resolve_task_id(runtime_repo, None)
        except typer.BadParameter:
            target_tid = None

    try:
        all_passed, results = ops.run_verifications(target_tid, ref=ref)
    except OperationError as exc:
        print(f"[yellow]{exc}[/yellow]")
        raise typer.Exit(code=2) from exc

    table = Table(title="Verification Results")
    table.add_column("Target", style="cyan")
    table.add_column("Type")
    table.add_column("Status")
    table.add_column("Message")

    for r in results:
        status_str = "[green]PASSED[/green]" if r.passed else "[red]FAILED[/red]"
        table.add_row(
            escape(r.target_path), r.verification_type.value, status_str, escape(r.message)
        )
    print(table)

    if not all_passed:
        raise typer.Exit(code=1)


@decision_app.command("add")
def decision_add(
    question: str,
    slug: Annotated[str | None, typer.Option("--slug", "-s")] = None,
    priority: Annotated[int, typer.Option("--priority", "-p")] = 50,
    context: Annotated[str | None, typer.Option("--context")] = None,
    context_file: Annotated[Path | None, typer.Option("--context-file")] = None,
    option: Annotated[
        list[str] | None,
        typer.Option("--option", help="'key|Label|description|effect', repeatable"),
    ] = None,
    recommend: Annotated[str | None, typer.Option("--recommend")] = None,
    no_custom: Annotated[bool, typer.Option("--no-custom")] = False,
    raised_by: Annotated[str | None, typer.Option("--raised-by")] = None,
    blocks: Annotated[str | None, typer.Option("--blocks", help="Comma-separated task ids")] = None,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    root = _get_root(path)
    container = _get_container(root)
    ops = container.get(Operations)
    ctx = context_file.read_text(encoding="utf-8") if context_file else context
    blocked = [t.strip() for t in blocks.split(",") if t.strip()] if blocks else []
    try:
        decision_id = ops.add_decision(
            question, slug, priority, ctx, option, recommend, not no_custom, raised_by, blocked
        )
    except OperationError as exc:
        print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    print(f"[green]Raised decision {decision_id}[/green]")


@decision_app.command("list")
def decision_list(
    status: Annotated[
        str | None, typer.Option("--status", help="open, answered or withdrawn")
    ] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    yaml_output: Annotated[bool, typer.Option("--yaml")] = False,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    root = _get_root(path)
    container = _get_container(root)
    node_repo = container.get(NodeRepository)
    decisions = node_repo.list_nodes(kind=NodeKind.DECISION)
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
    if json_output or yaml_output:
        _emit([_node_row(d) for d in decisions], yaml_output)
        return
    table = Table(title="Decisions")
    table.add_column("ID", style="cyan")
    table.add_column("Question")
    table.add_column("Status", style="yellow")
    table.add_column("Priority", justify="right")
    for d in decisions:
        label = DECISION_STATUS_LABELS.get(cast("DecisionStatus", d.status), d.status.value)
        table.add_row(escape(d.id), escape(d.title), label, str(d.priority))
    print(table)


@decision_app.command("get")
def decision_get(
    decision_id: str,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    yaml_output: Annotated[bool, typer.Option("--yaml")] = False,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    root = _get_root(path)
    container = _get_container(root)
    node_repo = container.get(NodeRepository)
    node = node_repo.get_node(decision_id)
    if node is None or node.kind != NodeKind.DECISION:
        print(f"[red]Decision '{decision_id}' not found[/red]")
        raise typer.Exit(code=1)
    data = read_decision(node)
    label = DECISION_STATUS_LABELS.get(cast("DecisionStatus", node.status), node.status.value)
    if json_output or yaml_output:
        doc = _node_row(node)
        doc["decision"] = json.loads(data.model_dump_json())
        doc["blocked_tasks"] = node_repo.get_blocked_by(decision_id)
        _emit(doc, yaml_output)
        return
    print(f"[bold cyan]Decision:[/] {node.id}")
    print(f"[bold]Question:[/] {escape(node.title)}")
    print(f"[bold]Status:[/] {label}")
    for opt in data.options:
        mark = " (recommended)" if opt.recommended else ""
        print(f"  - {opt.key}: {escape(opt.label)}{mark}")
    if data.answer:
        print(f"[bold]Answer:[/] {escape(data.answer.option or data.answer.text)}")


@decision_app.command("answer")
def decision_answer(
    decision_id: str,
    option: Annotated[str | None, typer.Option("--option")] = None,
    note: Annotated[str | None, typer.Option("--note")] = None,
    custom: Annotated[str | None, typer.Option("--custom")] = None,
    rationale: Annotated[str, typer.Option("--rationale")] = "",
    by: Annotated[str, typer.Option("--by")] = "cli",
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    root = _get_root(path)
    container = _get_container(root)
    ops = container.get(Operations)
    if (option is None) == (custom is None):
        raise typer.BadParameter("give --option <key> or --custom <text>, not both or neither")
    text = custom if custom is not None else (note or "")
    try:
        ops.answer_decision(decision_id, option, text, rationale, by)
    except OperationError as exc:
        print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    print(f"[green]Answered {decision_id}[/green]")


@decision_app.command("reopen")
def decision_reopen(
    decision_id: str,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    root = _get_root(path)
    container = _get_container(root)
    ops = container.get(Operations)
    try:
        ops.reopen_decision(decision_id)
    except OperationError as exc:
        print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    print(f"[green]Reopened {decision_id}[/green]")


@decision_app.command("withdraw")
def decision_withdraw(
    decision_id: str,
    reason: Annotated[str, typer.Option("--reason")] = "",
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    root = _get_root(path)
    container = _get_container(root)
    ops = container.get(Operations)
    try:
        ops.withdraw_decision(decision_id, reason)
    except OperationError as exc:
        print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    print(f"[green]Withdrew {decision_id}[/green]")


@decision_app.command("block")
def decision_block(
    decision_id: str,
    tasks: Annotated[str, typer.Option("--tasks", help="Comma-separated task ids")],
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    root = _get_root(path)
    container = _get_container(root)
    ops = container.get(Operations)
    ids = [t.strip() for t in tasks.split(",") if t.strip()]
    try:
        ops.link_decision(decision_id, add=ids)
    except OperationError as exc:
        print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    print(f"[green]{', '.join(ids)} now wait on {decision_id}[/green]")


@decision_app.command("unblock")
def decision_unblock(
    decision_id: str,
    tasks: Annotated[str, typer.Option("--tasks", help="Comma-separated task ids")],
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    root = _get_root(path)
    container = _get_container(root)
    ops = container.get(Operations)
    ids = [t.strip() for t in tasks.split(",") if t.strip()]
    try:
        ops.link_decision(decision_id, remove=ids)
    except OperationError as exc:
        print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    print(f"[green]{', '.join(ids)} no longer wait on {decision_id}[/green]")


@app.command("attach")
def attach_cmd(
    node_id: str,
    file: Path,
    caption: Annotated[str, typer.Option("--caption")] = "",
    source: Annotated[str | None, typer.Option("--source")] = None,
    replace: Annotated[
        str | None, typer.Option("--replace", help="Asset name to re-capture")
    ] = None,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    root = _get_root(path)
    container = _get_container(root)
    ops = container.get(Operations)
    try:
        entry = ops.attach(node_id, file, caption, source, replace)
    except OperationError as exc:
        print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    print(f"[green]Attached {entry['asset']} to {node_id}[/green]")


@app.command("detach")
def detach_cmd(
    node_id: str,
    asset: str,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    root = _get_root(path)
    container = _get_container(root)
    ops = container.get(Operations)
    try:
        ops.detach(node_id, asset)
    except OperationError as exc:
        print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    print(f"[green]Detached {asset} from {node_id}[/green]")


@app.command("attachments")
def attachments_cmd(
    node_id: str,
    check: Annotated[bool, typer.Option("--check", help="Re-hash project-file sources")] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
    yaml_output: Annotated[bool, typer.Option("--yaml")] = False,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    root = _get_root(path)
    container = _get_container(root)
    ops = container.get(Operations)
    try:
        entries = ops.list_attachments(node_id, check)
    except OperationError as exc:
        print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    if json_output or yaml_output:
        _emit(entries, yaml_output)
        return
    table = Table(title=f"Attachments of {escape(node_id)}")
    for column in ("Asset", "Name", "Caption", "Source", "State"):
        table.add_column(column)
    for e in entries:
        source = e.get("source") or {}
        table.add_row(
            escape(str(e.get("asset"))),
            escape(str(e.get("name"))),
            escape(str(e.get("caption") or "")),
            escape(str(source.get("uri") or "-")),
            escape(str(source.get("state") or "-")),
        )
    print(table)


@app.command("next")
def next_tasks(
    limit: Annotated[int, typer.Option("--limit", "-n", help="Number of tasks")] = 5,
    strategy: Annotated[
        RecommendationStrategy, typer.Option("--strategy", help="Scoring strategy")
    ] = RecommendationStrategy.BALANCED,
    plan: Annotated[str | None, typer.Option("--plan", help="Filter by plan ID")] = None,
    spec: Annotated[
        str | None,
        typer.Option("--spec", help='Filter by spec ID ("none" for tasks whose plan has no spec)'),
    ] = None,
    model: Annotated[str | None, typer.Option("--model", help="Filter by acceptable model")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
    yaml_output: Annotated[
        bool, typer.Option("--yaml", help="Output as YAML (fewer tokens than JSON)")
    ] = False,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    """What can be worked on now, scored. The owner's own queue is `tm decision list --status open`,
    not this command: an open decision blocks its task out of `next` until it is answered."""
    root = _get_root(path)
    container = _get_container(root)
    heuristics = container.get(RecommendationEngine)

    ranked = heuristics.get_next_tasks(
        plan_id=plan, spec_id=spec, model_filter=model, strategy=strategy, limit=limit
    )

    if json_output or yaml_output:
        data = [
            {
                "task_id": t.task_id,
                "title": t.title,
                "plan_id": t.plan_id,
                "score": t.score,
                "priority": t.priority,
                "acceptable_models": t.acceptable_models,
                "unblocking_count": t.unblocking_count,
                "declared_files": t.declared_files,
            }
            for t in ranked
        ]
        _emit(data, yaml_output)
        return

    table = Table(title="Recommended Next Tasks")
    table.add_column("Task ID", style="cyan", no_wrap=True)
    table.add_column("Title")
    table.add_column("Plan", style="magenta", no_wrap=True)
    table.add_column("Score", justify="right", style="green")
    table.add_column("Priority", justify="right")
    table.add_column("Models")
    for t in ranked:
        table.add_row(
            escape(t.task_id),
            escape(t.title),
            escape(t.plan_id or "-"),
            f"{t.score:.2f}",
            str(t.priority),
            ", ".join(t.acceptable_models),
        )
    print(table)


@app.command("render")
def render(
    qualified_ids: Annotated[
        list[str], typer.Argument(help="Qualified node path(s) (e.g. AUTH-USER-LOGIN)")
    ],
    view: Annotated[
        RenderView, typer.Option("--view", "-v", help="View projection: summary, subagent, or full")
    ] = RenderView.FULL,
    recursive: Annotated[
        bool,
        typer.Option(
            "--recursive",
            "-r",
            help="Also render every child, depth-first (a spec's plans and their tasks)",
        ),
    ] = False,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    root = _get_root(path)
    container = _get_container(root)
    renderer = container.get(MarkdownRenderer)
    outputs = []
    for qualified_id in qualified_ids:
        qp = QualifiedPath.parse(qualified_id)
        if recursive and qp.section_key:
            print(
                "[red]--recursive renders a node, not one of its sections; "
                "drop the `:section` part[/red]"
            )
            raise typer.Exit(code=1)
        try:
            outputs.append(
                renderer.render_recursive(qp.node_id, view=view)
                if recursive
                else renderer.render(qp.node_id, view=view)
            )
        except ValueError as exc:
            print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
    sys.stdout.write("\n\n---\n\n".join(outputs) + "\n")


class _RefusingImporter:
    """A refused import prints why and exits 1 instead of a traceback."""

    def __init__(self, importer: BulkImporter) -> None:
        self._importer = importer

    def import_dict(self, data: dict[str, Any]) -> None:
        try:
            self._importer.import_dict(data)
        except ValueError as exc:
            print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc


def _dispatch_targets(text: str, root: Path) -> str:
    # str.format would choke on the guide's own literal braces (a code span like `{...}`), so
    # only these known `{{token}}` spots are substituted.
    values = {k: r.value for k, r in ConfigStore(root).effective().items()}
    for token in ("tick_min", "tick_max", "wave_size", "tick_budget"):
        text = text.replace(f"{{{{{token}}}}}", str(values[f"dispatch.{token}"]))
    return text


def _guide_topics() -> dict[str, str]:
    """Built-in topic -> one-line description (the first sentence after the title)."""
    from importlib.resources import files

    topics: dict[str, str] = {}
    for entry in sorted(files("taskmanager").joinpath("guides").iterdir(), key=lambda e: e.name):
        if entry.name.endswith(".md"):
            lines = [ln.strip() for ln in entry.read_text(encoding="utf-8").splitlines()]
            body = next((ln for ln in lines[1:] if ln), "")
            topics[entry.name[: -len(".md")]] = body.split(". ")[0].rstrip(".")
    return topics


@app.command("guide")
def guide(
    topic: Annotated[str | None, typer.Argument(help="Topic; omit to list them")] = None,
    builtin_only: Annotated[
        bool, typer.Option("--builtin", help="Only the guidance shipped with tm")
    ] = False,
    project_only: Annotated[
        bool, typer.Option("--project", help="Only this project's addendum")
    ] = False,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    """How to work with tm, by role. Built-in guidance first, then the project's addendum.

    The addendum is the section named after the topic on the node `guide`
    (`tm section set guide:<topic> --file ...`), so a project's conventions live and version with
    its tasks.
    """
    from importlib.resources import files

    topics = _guide_topics()
    try:
        overlay_repo: NodeRepository | None = _get_container(_get_root(path)).get(NodeRepository)
    except typer.BadParameter:
        overlay_repo = None
    if topic is None:
        overlay_keys = (
            {sec.section_key for sec in overlay_repo.get_all_sections(GUIDE_NODE)}
            if overlay_repo
            else set()
        )
        for name, blurb in topics.items():
            marker = " (+ project addendum)" if name in overlay_keys else ""
            sys.stdout.write(f"{name}: {blurb}{marker}\n")
        for name in sorted(overlay_keys - set(topics)):
            sys.stdout.write(f"{name}: (project topic)\n")
        return
    overlay = overlay_repo.get_section(GUIDE_NODE, topic) if overlay_repo else None
    if topic not in topics and overlay is None:
        raise typer.BadParameter(f"no guide '{topic}'; topics: {', '.join(topics)}")
    parts: list[str] = []
    if topic in topics and not project_only:
        text = files("taskmanager").joinpath(f"guides/{topic}.md").read_text(encoding="utf-8")
        if topic == "dispatch":
            text = _dispatch_targets(text, _get_root(path, must_exist=False))
        parts.append(text)
    if overlay is not None and not builtin_only:
        parts.append(overlay.content)
    sys.stdout.write("\n\n---\n\n".join(p.rstrip("\n") for p in parts) + "\n")


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
        spec_doc: dict[str, Any] = {"spec": _export_node(node_repo, specs[spec_id])}
        children = set(node_repo.get_children(spec_id))
        # Import accepts tasks straight under a spec, or under nothing; the archive keeps both.
        if spec_tasks := [_export_node(node_repo, t) for t in tasks if t.id in children]:
            spec_doc["tasks"] = spec_tasks
        dump(f"_spec-{spec_id}.json", spec_doc)
    parented = {c for n in [*specs.values(), *plans] for c in node_repo.get_children(n.id)}
    if lone := [_export_node(node_repo, t) for t in tasks if t.id not in parented]:
        dump("_tasks.json", {"tasks": lone})
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
    # Documents depend on each other, so the first pass keeps only the edges a document can
    # satisfy by itself and the second adds the rest; decisions go in between so a task's
    # depends_on edge onto one resolves in the second pass, then specs go last so their full
    # data wins over the stub a plan's document carries.
    plan_docs = [d for d in docs if d.get("plans")]
    spec_docs = [d for d in docs if not d.get("plans")]

    def nodes_of(doc: dict[str, Any]) -> list[dict[str, Any]]:
        plans = doc.get("plans", [])
        return [
            *([doc["spec"]] if doc.get("spec") else []),
            *(n for p in plans for n in [p, *p.get("tasks", [])]),
            *doc.get("tasks", []),
        ]

    for doc in [*plan_docs, *spec_docs]:
        first = copy.deepcopy(doc)
        own = {n["id"] for n in nodes_of(first)}
        for n in nodes_of(first):
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
    specs = sum(1 for d in spec_docs if d.get("spec"))
    print(f"[green]Restored {len(plan_docs)} plans and {specs} specs into {root}[/green]")


@config_app.command("list")
def config_list(
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
    yaml_output: Annotated[bool, typer.Option("--yaml", help="Output as YAML")] = False,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    """Every key with its effective value and where it came from (flag, env, config, default)."""
    root = _get_root(path)
    with _user_errors():
        effective = ConfigStore(root).effective()
    if json_output or yaml_output:
        _emit(
            [{"key": k, "value": r.value, "source": r.source} for k, r in effective.items()],
            yaml_output,
        )
        return
    for key, resolved in effective.items():
        sys.stdout.write(
            f"{key} = {resolved.value if resolved.value is not None else ''}  ({resolved.source})\n"
        )


@config_app.command("get")
def config_get(
    key: str,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    root = _get_root(path)
    with _user_errors():
        value = ConfigStore(root).resolve(key).value
    sys.stdout.write(f"{value if value is not None else ''}\n")


@config_app.command("set", context_settings={"ignore_unknown_options": True})
def config_set(
    key: str,
    value: str,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    root = _get_root(path)
    with _user_errors():
        ConfigStore(root).set(key, value)
    print(f"[green]Set {escape(key)}[/green]")


@config_app.command("unset")
def config_unset(
    key: str,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    root = _get_root(path)
    with _user_errors():
        ConfigStore(root).unset(key)
    print(f"[green]Unset {escape(key)}[/green]")


@app.command("index")
def index_cmd(
    rebuild: Annotated[
        bool, typer.Option("--rebuild", help="Drop the vector table and embed everything again")
    ] = False,
    kind: Annotated[NodeKind | None, typer.Option("--kind", help="task, plan or spec only")] = None,
    show_status: Annotated[
        bool, typer.Option("--status", help="Print provider, size and staleness instead")
    ] = False,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    """Embed each node's title and sections for `tm search`; unchanged text is skipped."""
    root = _get_root(path)
    engine = _get_container(root).get(SearchEngine)
    with _user_errors():
        if show_status:
            for key, value in engine.status().items():
                sys.stdout.write(f"{key}: {value}\n")
            return

        def progress(done: int, total: int) -> None:
            if done % 25 == 0 or done == total:
                sys.stdout.write(f"embedded {done}/{total}\n")

        report = engine.index([kind] if kind else [], rebuild, progress)
    print(
        f"[green]Indexed {report.embedded} items: {report.unchanged} unchanged, "
        f"{report.removed} removed[/green]"
    )


@app.command("search")
def search_cmd(
    query: Annotated[list[str], typer.Argument(help="Words to look for")],
    mode: Annotated[SearchMode, typer.Option("--mode", help="auto, fts, semantic or hybrid")] = (
        SearchMode.AUTO
    ),
    kind: Annotated[NodeKind | None, typer.Option("--kind", help="task, plan or spec")] = None,
    status: Annotated[
        Status | None, typer.Option("--status", help="Filter by stored status")
    ] = None,
    plan: Annotated[str | None, typer.Option("--plan", help="Only this plan and its tasks")] = None,
    limit: Annotated[int, typer.Option("--limit", min=1, help="Most results to print")] = 10,
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
    yaml_output: Annotated[bool, typer.Option("--yaml", help="Output as YAML")] = False,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    """Find tasks, plans and specs by words (fts), by meaning (semantic) or both (hybrid)."""
    root = _get_root(path)
    engine = _get_container(root).get(SearchEngine)
    with _user_errors():
        hits, used = engine.run(" ".join(query), mode, [kind] if kind else [], status, plan, limit)
        stale = engine.stale_nodes() if used is not SearchMode.FTS else set()
    if stale:
        sys.stderr.write(f"warning: {len(stale)} nodes changed since `tm index`: run it\n")
    if json_output or yaml_output:
        _emit({"mode": used.value, "results": [h.as_dict() for h in hits]}, yaml_output)
        return
    for hit in hits:
        where = f" plan {hit.plan}" if hit.plan and hit.plan != hit.id else ""
        section = f" section {hit.section}" if hit.section else ""
        sys.stdout.write(
            f"{hit.id}  {hit.kind}  {hit.status}{where}{section}  {hit.score}\n"
            f"  {hit.title}\n  {hit.snippet}\n"
        )
    sys.stdout.write(f"{len(hits)} results, mode: {used.value}\n")


@app.command("root")
def root_cmd(path: Annotated[Path | None, typer.Option("--path", "-C")] = None) -> None:
    """Print the project root `tm` resolved (a `target_repo` is `<root>/<target_repo>`)."""
    sys.stdout.write(f"{_get_root(path)}\n")


@app.command("import")
def import_cmd(
    format: Annotated[
        ImportFormat, typer.Option("--format", help="Input format: json, yaml, or markdown")
    ] = ImportFormat.JSON,
    file: Annotated[
        Path | None, typer.Option("--file", "-f", help="File to import (defaults to stdin)")
    ] = None,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    root = _get_root(path)
    container = _get_container(root)
    importer = _RefusingImporter(container.get(BulkImporter))

    if file:
        content = file.read_text(encoding="utf-8")
    else:
        content = sys.stdin.read()

    if format == ImportFormat.JSON:
        data = json.loads(content)
        importer.import_dict(data)
    elif format == ImportFormat.YAML:
        try:
            import importlib

            yaml_mod = importlib.import_module("yaml")
            data = yaml_mod.safe_load(content)
        except ImportError, ValueError, AttributeError:
            data = json.loads(content)
        importer.import_dict(data)
    elif format == ImportFormat.MARKDOWN:
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                fm = parts[1].strip()
                try:
                    import importlib

                    yaml_mod = importlib.import_module("yaml")
                    data = yaml_mod.safe_load(fm)
                except ImportError, ValueError, AttributeError:
                    data = json.loads(fm)
                importer.import_dict(data)
            else:
                raise typer.BadParameter("Invalid markdown frontmatter")
        else:
            raise typer.BadParameter("Markdown format requires frontmatter structure")

    _record_ledger(
        container,
        command=LedgerCommand.IMPORT,
        payload={"format": format.value, "file": str(file) if file else "stdin"},
    )
    print(f"[green]Successfully imported data from {file or 'stdin'}[/green]")


@audit_app.command("list")
def audit_list(
    target: Annotated[str | None, typer.Option("--target", help="Filter by target ID")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n", help="Limit results")] = 50,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    root = _get_root(path)
    container = _get_container(root)
    ledger_repo = container.get(LedgerRepository)
    events = ledger_repo.list_events(target_id=target, limit=limit)

    table = Table(title="Audit Ledger Events")
    table.add_column("ID", justify="right")
    table.add_column("Timestamp")
    table.add_column("Actor", style="cyan")
    table.add_column("Command")
    table.add_column("Target ID", style="magenta")
    for e in events:
        table.add_row(
            str(e.id or "-"),
            str(e.timestamp),
            escape(e.actor_id),
            escape(e.command),
            escape(e.target_id or "-"),
        )
    print(table)


def _find_available_port(host: str, starting_port: int, max_attempts: int = 20) -> int:
    import socket

    for p in range(starting_port, starting_port + max_attempts):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, p))
                return p
            except OSError:
                continue
    raise RuntimeError(
        f"Could not find an available port in range {starting_port}..{starting_port + max_attempts}"
    )


def _run_web_server(host: str, port: int, open_browser: bool, path: Path | None) -> None:
    import threading
    import time
    import webbrowser

    import uvicorn

    from taskmanager.web.app import create_app

    root = _get_root(path)
    _refuse_pre_lifecycle(root)
    db_mgr = DatabaseManager(root / ".taskmanager")
    if not db_mgr.is_initialized():
        print(f"[red]Error:[/red] TaskManager is not initialized in {root}. Run 'tm init' first.")
        raise typer.Exit(code=1)

    actual_port = _find_available_port(host, port)
    if actual_port != port:
        print(f"[yellow]Port {port} in use, auto-switched to port {actual_port}[/yellow]")

    server_url = f"http://{host}:{actual_port}"
    print(
        f"[green]Starting TaskManager Web Visualizer at[/green] [bold cyan]{server_url}[/bold cyan]"
    )
    print(f"[dim]Live WebSocket connected at ws://{host}:{actual_port}/ws[/dim]")

    if open_browser:

        def _open() -> None:
            time.sleep(0.6)
            webbrowser.open(server_url)

        threading.Thread(target=_open, daemon=True).start()

    fastapi_app = create_app(root, host=host, port=actual_port)
    uvicorn.run(fastapi_app, host=host, port=actual_port, log_level="warning")


@web_app.callback(invoke_without_command=True)
def web_callback(
    ctx: typer.Context,
    host: Annotated[str, typer.Option("--host", "-h", help="Host address")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", "-p", help="Starting port number")] = 6701,
    open_browser: Annotated[
        bool, typer.Option("--open/--no-open", help="Auto-open browser")
    ] = True,
    path: Annotated[
        Path | None, typer.Option("--path", "-C", help="Project root directory")
    ] = None,
) -> None:
    """Interactive web visualizer and dashboard."""
    if ctx.invoked_subcommand is None:
        _run_web_server(host=host, port=port, open_browser=open_browser, path=path)


@web_app.command("run")
def web_run(
    host: Annotated[str, typer.Option("--host", "-h", help="Host address")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", "-p", help="Starting port number")] = 6701,
    open_browser: Annotated[
        bool, typer.Option("--open/--no-open", help="Auto-open browser")
    ] = True,
    path: Annotated[
        Path | None, typer.Option("--path", "-C", help="Project root directory")
    ] = None,
) -> None:
    """Run interactive web server with real-time updates."""
    _run_web_server(host=host, port=port, open_browser=open_browser, path=path)


@web_app.command("export")
def web_export(
    output: Annotated[
        Path, typer.Option("--output", "-o", help="Output static HTML file path")
    ] = Path("spec-dashboard.html"),
    path: Annotated[
        Path | None, typer.Option("--path", "-C", help="Project root directory")
    ] = None,
) -> None:
    """Export standalone self-contained static HTML visualizer."""
    from taskmanager.web.static_export import export_static_html

    root = _get_root(path, must_exist=False)
    _refuse_pre_lifecycle(root)
    db_mgr = DatabaseManager(root / ".taskmanager")
    if not db_mgr.is_initialized():
        print(f"[red]Error:[/red] TaskManager is not initialized in {root}. Run 'tm init' first.")
        raise typer.Exit(code=1)

    exported = export_static_html(root, output)
    print(
        f"[green]Exported static HTML visualizer to[/green] [bold cyan]{exported.resolve()}[/bold cyan]"
    )


@app.command("install")
def cli_install(
    status: Annotated[
        bool, typer.Option("--status", "-s", help="Check installation status")
    ] = False,
    tool_only: Annotated[
        bool, typer.Option("--tool-only", help="Install CLI executable only")
    ] = False,
    claude_only: Annotated[
        bool, typer.Option("--claude-only", help="Register Claude Code plugin only")
    ] = False,
    path: Annotated[
        Path | None, typer.Option("--path", "-C", help="TaskManager repository directory")
    ] = None,
) -> None:
    """Install TaskManager globally as an executable CLI and harness plugin."""
    import shutil
    import subprocess

    root = _get_root(path, must_exist=False)
    install_script = root / "install.sh"
    if not install_script.exists():
        pkg_root = Path(__file__).resolve().parents[3]
        if (pkg_root / "install.sh").exists():
            install_script = pkg_root / "install.sh"
            root = pkg_root

    if status:
        if install_script.exists():
            subprocess.run([str(install_script), "status"], check=False)
        else:
            is_installed = shutil.which("tm") is not None
            print(f"tm in PATH: {is_installed}")
        return

    if tool_only:
        subprocess.run(["uv", "tool", "install", "--editable", str(root), "--force"], check=False)
        print("[green]Installed TaskManager executable tool[/green]")
        return

    if claude_only:
        subprocess.run(["claude", "plugin", "marketplace", "add", str(root)], check=False)
        subprocess.run(["claude", "plugin", "install", "taskmanager@taskmanager"], check=False)
        print("[green]Registered TaskManager plugin in Claude Code[/green]")
        return

    if install_script.exists():
        subprocess.run([str(install_script), "install"], check=False)
    else:
        subprocess.run(["uv", "tool", "install", "--editable", str(root), "--force"], check=False)
        subprocess.run(["claude", "plugin", "marketplace", "add", str(root)], check=False)
        subprocess.run(["claude", "plugin", "install", "taskmanager@taskmanager"], check=False)
        print("[green]TaskManager installed successfully[/green]")


@db_app.command("migrate")
def db_migrate(
    path: Annotated[
        Path | None, typer.Option("--path", "-C", help="Project root directory")
    ] = None,
) -> None:
    """Copy .taskmanager/state.db to .taskmanager/state.db.schema<n>.bak with SQLite's backup
    API, then migrate it from schema <n> to the schema this tm reads."""
    root = _get_root(path)
    _refuse_pre_lifecycle(root)
    db = DatabaseManager(root / ".taskmanager")
    try:
        found = db.migrate_state()
    except StateNotInitialized:
        print(f"[red]Error:[/red] TaskManager is not initialized in {root}. Run 'tm init' first.")
        raise typer.Exit(code=1) from None
    if found is None:
        print(f"state.db is current at schema {STATE_SCHEMA_VERSION}: nothing to migrate")
        return
    print(
        f"[green]Migrated state.db from schema {found} to {STATE_SCHEMA_VERSION}[/green]; "
        f"the schema-{found} copy is {escape(str(db.state_backup(found)))}"
    )


def main() -> None:
    """The console entry: a state.db at another schema than this tm's can surface from whichever
    command opens it first, so it is refused here, once, as its message and exit 1."""
    try:
        app()
    except (StateSchemaTooNew, StateSchemaTooOld) as exc:
        print(f"[red]{escape(str(exc))}[/red]")
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
