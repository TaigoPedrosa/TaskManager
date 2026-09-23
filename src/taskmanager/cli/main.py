import json
import logging
import os
import sqlite3
import subprocess
import sys
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
    NodeStatus,
    RecommendationStrategy,
    RenderView,
    SearchMode,
    TransferMode,
    VerificationType,
)
from taskmanager.core.models import LedgerEvent
from taskmanager.core.naming import QualifiedPath
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.ledger_repo import LedgerRepository
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.di.container import TaskManagerProvider
from taskmanager.engine.config import ConfigError, ConfigStore
from taskmanager.engine.graph import GraphEngine, gate_satisfied
from taskmanager.engine.heuristics import RecommendationEngine
from taskmanager.engine.operations import GUIDE_NODE, OperationError, Operations
from taskmanager.engine.runtime import ExecutionCoordinator
from taskmanager.engine.search import SearchEngine, SearchError
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
verify_app = typer.Typer(name="verify", help="Static and AST verifications")
audit_app = typer.Typer(name="audit", help="Audit ledger event logs")
web_app = typer.Typer(name="web", help="Interactive web visualizer and exporter")
plugin_app = typer.Typer(name="plugin", help="Install and manage harness plugins")
config_app = typer.Typer(name="config", help="Project configuration (.taskmanager/config.yaml)")

app.add_typer(spec_app)
app.add_typer(plan_app)
app.add_typer(task_app)
app.add_typer(section_app)
app.add_typer(run_app)
app.add_typer(verify_app)
app.add_typer(audit_app)
app.add_typer(web_app)
app.add_typer(plugin_app)
app.add_typer(config_app)


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


def _node_row(node: Any, state: str | None = None) -> dict[str, Any]:
    return {
        "id": node.id,
        "kind": node.kind.value,
        "title": node.title,
        "status": node.status.value,
        "state": state or node.status.value,
        "priority": node.priority,
        "target_repo": node.target_repo,
        "acceptable_models": node.acceptable_models,
    }


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
    return make_container(TaskManagerProvider(root))


def _record_ledger(
    container: Container,
    command: LedgerCommand | str,
    target_id: str | None = None,
    actor_id: str = "cli",
    payload: dict[str, Any] | None = None,
    diff: dict[str, Any] | None = None,
) -> None:
    try:
        ledger_repo = container.get(LedgerRepository)
        ledger_repo.append(
            LedgerEvent(
                actor_id=actor_id,
                command=command,
                target_id=target_id,
                payload=payload or {},
                diff=diff or {},
            )
        )
    except (sqlite3.Error, OSError) as exc:
        logging.getLogger(__name__).debug("Failed to append ledger event: %s", exc)


def _resolve_task_id(runtime_repo: RuntimeRepository, task_id: str | None) -> str:
    if task_id:
        return task_id

    cwd = Path.cwd().resolve()
    with runtime_repo.db.get_runtime_connection() as conn:
        rows = conn.execute("SELECT task_id, worktree_path, branch_name FROM leases").fetchall()
        for tid, wt_path_str, _ in rows:
            if wt_path_str:
                wt = Path(wt_path_str).resolve()
                if cwd == wt or wt in cwd.parents:
                    return str(tid)

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
    path: Annotated[
        Path | None, typer.Option("--path", "-C", help="Target project root directory")
    ] = None,
) -> None:
    root = _get_root(path, must_exist=False)
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
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    root = _get_root(path)
    container = _get_container(root)
    ops = container.get(Operations)
    spec_id = ops.add_spec(title, slug, priority, order)
    print(f"[green]Added spec {spec_id}[/green]")


@spec_app.command("list")
def spec_list(
    status: Annotated[NodeStatus | None, typer.Option("--status", help="Filter by status")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
    yaml_output: Annotated[
        bool, typer.Option("--yaml", help="Output as YAML (fewer tokens than JSON)")
    ] = False,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    root = _get_root(path)
    container = _get_container(root)
    node_repo = container.get(NodeRepository)
    specs = node_repo.list_nodes(kind=NodeKind.SPEC, status=status)
    if json_output or yaml_output:
        graph = container.get(GraphEngine)
        _emit([_node_row(s, graph.resolve_spec_status(s.id).value) for s in specs], yaml_output)
        return

    table = Table(title="Specifications")
    table.add_column("ID", style="cyan")
    table.add_column("Title")
    table.add_column("Status", style="yellow")
    table.add_column("Priority", justify="right")
    for s in specs:
        table.add_row(escape(s.id), escape(s.title), s.status.value, str(s.priority))
    print(table)


@spec_app.command("get")
def spec_get(
    spec_id: str,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    root = _get_root(path)
    container = _get_container(root)
    node_repo = container.get(NodeRepository)
    spec = node_repo.get_node(spec_id)
    if not spec:
        print(f"[red]Spec '{spec_id}' not found[/red]")
        raise typer.Exit(code=1)

    children = node_repo.get_children(spec_id)
    print(f"[bold cyan]Spec:[/] {spec.id}")
    print(f"[bold]Title:[/] {escape(spec.title)}")
    graph = container.get(GraphEngine)
    print(f"[bold]Status:[/] {spec.status.value}")
    print(f"[bold]State:[/] {graph.resolve_spec_status(spec.id).value}")
    print(f"[bold]Priority:[/] {spec.priority}")
    if children:
        print(f"[bold]Plans:[/] {escape(', '.join(children))}")


@plan_app.command("add")
def plan_add(
    title: str,
    spec: Annotated[str, typer.Option("--spec", help="Parent spec ID")],
    slug: Annotated[str | None, typer.Option("--slug", "-s", help="Plan slug")] = None,
    priority: Annotated[int, typer.Option("--priority", "-p", help="Priority")] = 50,
    order: Annotated[int, typer.Option("--order", "-o", help="Display order")] = 0,
    require_review: Annotated[
        bool, typer.Option("--require-review", help="Inject review gate")
    ] = False,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    root = _get_root(path)
    container = _get_container(root)
    ops = container.get(Operations)
    plan_id, gate_id = ops.add_plan(title, spec, slug, priority, order, require_review)
    if gate_id:
        print(f"[green]Added plan {plan_id} with review gate {gate_id}[/green]")
    else:
        print(f"[green]Added plan {plan_id}[/green]")


@plan_app.command("list")
def plan_list(
    spec: Annotated[str | None, typer.Option("--spec", help="Filter by spec ID")] = None,
    status: Annotated[NodeStatus | None, typer.Option("--status", help="Filter by status")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
    yaml_output: Annotated[
        bool, typer.Option("--yaml", help="Output as YAML (fewer tokens than JSON)")
    ] = False,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    root = _get_root(path)
    container = _get_container(root)
    node_repo = container.get(NodeRepository)
    plans = node_repo.list_nodes(kind=NodeKind.PLAN, status=status)
    if spec:
        children = set(node_repo.get_children(spec))
        plans = [p for p in plans if p.id in children or p.id.startswith(f"{spec}-")]
    if json_output or yaml_output:
        graph = container.get(GraphEngine)
        _emit([_node_row(p, graph.resolve_plan_status(p.id).value) for p in plans], yaml_output)
        return

    table = Table(title="Plans")
    table.add_column("ID", style="cyan")
    table.add_column("Title")
    table.add_column("Status", style="yellow")
    table.add_column("Priority", justify="right")
    for p in plans:
        table.add_row(escape(p.id), escape(p.title), p.status.value, str(p.priority))
    print(table)


@plan_app.command("get")
def plan_get(
    plan_id: str,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    root = _get_root(path)
    container = _get_container(root)
    node_repo = container.get(NodeRepository)
    plan = node_repo.get_node(plan_id)
    if not plan:
        print(f"[red]Plan '{plan_id}' not found[/red]")
        raise typer.Exit(code=1)

    children = node_repo.get_children(plan_id)
    print(f"[bold cyan]Plan:[/] {plan.id}")
    print(f"[bold]Title:[/] {escape(plan.title)}")
    print(f"[bold]Status:[/] {plan.status.value}")
    print(f"[bold]Priority:[/] {plan.priority}")
    if children:
        print(f"[bold]Tasks:[/] {escape(', '.join(children))}")


@task_app.command("add")
def task_add(
    title: str,
    plan: Annotated[str, typer.Option("--plan", help="Parent plan ID")],
    slug: Annotated[str | None, typer.Option("--slug", "-s", help="Task slug")] = None,
    priority: Annotated[int, typer.Option("--priority", "-p", help="Priority")] = 50,
    order: Annotated[int, typer.Option("--order", "-o", help="Display order")] = 0,
    depends_on: Annotated[
        str | None, typer.Option("--depends-on", help="Comma-separated dependency task IDs")
    ] = None,
    models: Annotated[
        str | None, typer.Option("--models", help="Comma-separated acceptable models")
    ] = None,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    root = _get_root(path)
    container = _get_container(root)
    ops = container.get(Operations)
    acceptable_models = [m.strip() for m in models.split(",") if m.strip()] if models else []
    deps = [d.strip() for d in depends_on.split(",") if d.strip()] if depends_on else []
    task_id = ops.add_task(title, plan, slug, priority, order, deps, acceptable_models)
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
    status: Annotated[NodeStatus | None, typer.Option("--status", help="Filter by status")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
    yaml_output: Annotated[
        bool, typer.Option("--yaml", help="Output as YAML (fewer tokens than JSON)")
    ] = False,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    root = _get_root(path)
    container = _get_container(root)
    node_repo = container.get(NodeRepository)
    tasks = node_repo.list_nodes(kind=NodeKind.TASK, status=status)
    if plan:
        children = set(node_repo.get_children(plan))
        tasks = [t for t in tasks if t.id in children or t.id.startswith(f"{plan}-")]
    if json_output or yaml_output:
        graph = container.get(GraphEngine)
        _emit([_node_row(t, graph.resolve_task_state(t.id).value) for t in tasks], yaml_output)
        return

    table = Table(title="Tasks")
    table.add_column("ID", style="cyan")
    table.add_column("Title")
    table.add_column("Status", style="yellow")
    table.add_column("Priority", justify="right")
    table.add_column("Models")
    for t in tasks:
        table.add_row(
            escape(t.id),
            escape(t.title),
            t.status.value,
            str(t.priority),
            escape(", ".join(t.acceptable_models)),
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
                "Comma-separated ids this task now depends on. An id alone gates on the "
                "dependency reaching COMPLETED, same as always; 'id:STATUS' (e.g. "
                "'AUTH-T01:WAITING_REVIEW') gates on it reaching STATUS or later in the "
                "lifecycle instead."
            ),
        ),
    ] = None,
    remove: Annotated[
        str | None, typer.Option("--remove", help="Comma-separated ids to stop depending on")
    ] = None,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    """Add or remove dependency edges on an existing task; nothing is written if any is refused."""
    root = _get_root(path)
    container = _get_container(root)
    ops = container.get(Operations)

    def ids(raw: str | None) -> list[str]:
        return [x.strip() for x in (raw or "").split(",") if x.strip()]

    def parse_add(raw: str | None) -> list[tuple[str, NodeStatus | None]]:
        parsed: list[tuple[str, NodeStatus | None]] = []
        for item in ids(raw):
            dep_id, sep, gate_raw = item.partition(":")
            if not sep:
                parsed.append((dep_id, None))
                continue
            try:
                parsed.append((dep_id, NodeStatus(gate_raw)))
            except ValueError:
                valid = ", ".join(s.value for s in NodeStatus)
                raise typer.BadParameter(f"'{gate_raw}' is not a status; one of: {valid}") from None
        return parsed

    to_add, to_remove = parse_add(add), ids(remove)
    if not to_add and not to_remove:
        raise typer.BadParameter("give --add and/or --remove")
    try:
        edges = ops.set_dependencies(task_id, to_add, to_remove)
    except OperationError as exc:
        print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    shown = [
        f"{dep_id}:{gate.value}" if gate != NodeStatus.COMPLETED else dep_id
        for dep_id, gate in edges
    ]
    print(f"[green]{task_id} depends on: {', '.join(shown) or '-'}[/green]")


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
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    root = _get_root(path)
    container = _get_container(root)
    ops = container.get(Operations)
    frontmatter_set: dict[str, Any] = {}
    for pair in set_frontmatter or []:
        key, sep, raw = pair.partition("=")
        if not sep or not key:
            raise typer.BadParameter(f"--set takes key=value, got '{pair}'")
        try:
            frontmatter_set[key] = json.loads(raw)
        except ValueError:
            frontmatter_set[key] = raw
    model_list = [m.strip() for m in models.split(",") if m.strip()] if models is not None else None
    try:
        changed = ops.update_node(
            task_id,
            title=title,
            priority=priority,
            models=model_list,
            repo=repo,
            frontmatter_set=frontmatter_set or None,
            frontmatter_unset=unset_frontmatter,
        )
    except OperationError as exc:
        raise typer.BadParameter(str(exc)) from exc
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
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    root = _get_root(path)
    container = _get_container(root)
    node_repo = container.get(NodeRepository)
    task = node_repo.get_node(task_id)
    if not task:
        print(f"[red]Task '{task_id}' not found[/red]")
        raise typer.Exit(code=1)

    deps = node_repo.get_dependencies(task_id)
    verifications = node_repo.get_verifications(task_id)
    if json_output or yaml_output:
        graph = container.get(GraphEngine)
        runtime_repo = container.get(RuntimeRepository)
        lease = runtime_repo.get_lease(task_id)
        dep_edges = node_repo.get_dependency_edges(task_id)
        doc = _node_row(task, graph.resolve_task_state(task_id).value)
        doc["frontmatter"] = task.frontmatter
        doc["depends_on"] = [
            (
                {"id": dep_id, "status": dn.status.value}
                | ({"gate": gate.value} if gate != NodeStatus.COMPLETED else {})
                if (dn := node_repo.get_node(dep_id))
                else {"id": dep_id}
            )
            for dep_id, gate in dep_edges
        ]
        doc["blocked_by"] = [
            dep_id
            for dep_id, gate in dep_edges
            if (dn := node_repo.get_node(dep_id)) is None or not gate_satisfied(dn.status, gate)
        ]
        doc["declared_files"] = node_repo.declared_files(task_id)
        doc["sections"] = [s.section_key for s in node_repo.get_all_sections(task_id)]
        doc["verifications"] = [
            {
                "type": v.verification_type.value,
                "target_path": v.target_path,
                "expected_pattern": v.expected_pattern,
            }
            for v in verifications
        ]
        doc["lease"] = (
            {
                "agent_id": lease.agent_id,
                "session_id": lease.session_id,
                "worktree_path": lease.worktree_path,
                "branch_name": lease.branch_name,
                "last_heartbeat": lease.last_heartbeat.isoformat(),
                "ttl_seconds": lease.ttl_seconds,
            }
            if lease
            else None
        )
        _emit(doc, yaml_output)
        return
    print(f"[bold cyan]Task:[/] {task.id}")
    print(f"[bold]Title:[/] {escape(task.title)}")
    print(f"[bold]Status:[/] {task.status.value}")
    print(f"[bold]Priority:[/] {task.priority}")
    print(f"[bold]Models:[/] {escape(', '.join(task.acceptable_models))}")
    if deps:
        print(f"[bold]Depends On:[/] {escape(', '.join(deps))}")
    if verifications:
        v_str = ", ".join(f"{v.verification_type.value}:{v.target_path}" for v in verifications)
        print(f"[bold]Verifications:[/] {escape(v_str)}")


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
    output = f"{sec.header}\n{sec.content}" if sec.header else sec.content
    sys.stdout.write(output + "\n")


@section_app.command("set")
def section_set(
    qualified_path: str,
    content: Annotated[str | None, typer.Argument(help="Section content")] = None,
    content_opt: Annotated[
        str | None, typer.Option("--content", help="Section content text")
    ] = None,
    file: Annotated[
        Path | None, typer.Option("--file", "-f", help="Read content from file")
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
        text_content = file.read_text(encoding="utf-8")
    elif content_opt is not None:
        text_content = content_opt
    elif content is not None:
        text_content = content

    try:
        ops.set_section(qp.node_id, qp.section_key, text_content, header)
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


@run_app.command("start")
def run_start(
    task_id: str,
    worktree: Annotated[
        bool, typer.Option("--worktree", help="Create isolated git worktree")
    ] = False,
    agent: Annotated[str, typer.Option("--agent", help="Agent identifier")] = "agent-1",
    session: Annotated[str, typer.Option("--session", help="Session identifier")] = "session-1",
    account: Annotated[str | None, typer.Option("--account", help="Account identifier")] = None,
    worktree_dir: Annotated[
        Path | None,
        typer.Option(
            "--worktree-dir",
            help="Where worktrees go (env TM_WORKTREES, then config worktree_dir, else .worktrees)",
        ),
    ] = None,
    ttl: Annotated[
        int | None,
        typer.Option(
            "--ttl",
            help="Lease seconds before it reads as abandoned (env TM_LEASE_TTL, then config lease_ttl)",
        ),
    ] = None,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    root = _get_root(path)
    container = _get_container(root)
    coordinator = container.get(ExecutionCoordinator)

    worktree_base: Path | None = None
    with _user_errors():
        config = ConfigStore(root)
        lease_ttl = config.resolve("lease_ttl", ttl).value
        if worktree:
            chosen = config.resolve("worktree_dir", str(worktree_dir) if worktree_dir else None)
            worktree_base = (
                Path(chosen.value) if chosen.source in ("flag", "env") else root / chosen.value
            )
    try:
        lease = coordinator.start_task(
            task_id=task_id,
            agent_id=agent,
            session_id=session,
            account_id=account,
            create_worktree=worktree,
            worktree_base=worktree_base,
            ttl_seconds=lease_ttl,
        )
    except ValueError as exc:
        print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    _record_ledger(
        container,
        command=LedgerCommand.TASK_START,
        target_id=task_id,
        payload={"agent_id": agent, "session_id": session, "worktree": worktree},
    )
    print(f"[green]Started task {task_id}[/green]")
    print(f"[bold]Lease Agent:[/] {lease.agent_id}")
    print(f"[bold]Session:[/] {lease.session_id}")
    if lease.worktree_path:
        print(f"[bold]Worktree:[/] {lease.worktree_path}")


@run_app.command("heartbeat")
def run_heartbeat(
    task_id: Annotated[
        str | None, typer.Argument(help="Task ID (optional if inside worktree)")
    ] = None,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    root = _get_root(path)
    container = _get_container(root)
    runtime_repo = container.get(RuntimeRepository)
    coordinator = container.get(ExecutionCoordinator)

    tid = _resolve_task_id(runtime_repo, task_id)
    success = coordinator.heartbeat(tid)
    if success:
        _record_ledger(container, command=LedgerCommand.TASK_HEARTBEAT, target_id=tid)
        print(f"[green]Heartbeat recorded for {tid}[/green]")
    else:
        print(f"[red]No active lease found for {tid}[/red]")
        raise typer.Exit(code=1)


@run_app.command("stop")
def run_stop(
    task_id: Annotated[
        str | None, typer.Argument(help="Task ID (optional if inside worktree)")
    ] = None,
    status: Annotated[
        NodeStatus, typer.Option("--status", help="Target status")
    ] = NodeStatus.WAITING_REVIEW,
    remove_worktree: Annotated[
        bool, typer.Option("--remove-worktree", help="Remove worktree if created")
    ] = False,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    root = _get_root(path)
    container = _get_container(root)
    runtime_repo = container.get(RuntimeRepository)
    ops = container.get(Operations)

    tid = _resolve_task_id(runtime_repo, task_id)
    ops.set_status(tid, status, remove_worktree)
    print(f"[green]Stopped task {tid} with status {status.value}[/green]")


@run_app.command("release")
def run_release(
    task_id: str,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    """Drop a task's lease and file locks without touching its status."""
    root = _get_root(path)
    container = _get_container(root)
    ops = container.get(Operations)
    ops.release_lease(task_id)
    print(f"[green]Released lease for {task_id}[/green]")


@run_app.command("list")
def run_list(
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
    yaml_output: Annotated[
        bool, typer.Option("--yaml", help="Output as YAML (fewer tokens than JSON)")
    ] = False,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    root = _get_root(path)
    container = _get_container(root)
    runtime_repo = container.get(RuntimeRepository)

    with runtime_repo.db.get_runtime_connection() as conn:
        leases = conn.execute(
            "SELECT task_id, agent_id, session_id, account_id, worktree_path, branch_name, acquired_at, last_heartbeat, ttl_seconds FROM leases"
        ).fetchall()
        locks = conn.execute("SELECT file_path, task_id, lock_type FROM file_locks").fetchall()

    if json_output or yaml_output:
        data = {
            "leases": [
                {
                    "task_id": r[0],
                    "agent_id": r[1],
                    "session_id": r[2],
                    "account_id": r[3],
                    "worktree_path": r[4],
                    "branch_name": r[5],
                    "acquired_at": r[6],
                    "last_heartbeat": r[7],
                    "ttl_seconds": r[8],
                }
                for r in leases
            ],
            "locks": [{"file_path": r[0], "task_id": r[1], "lock_type": r[2]} for r in locks],
        }
        _emit(data, yaml_output)
        return

    table_leases = Table(title="Active Leases")
    table_leases.add_column("Task ID", style="cyan")
    table_leases.add_column("Agent ID")
    table_leases.add_column("Session ID")
    table_leases.add_column("Worktree")
    table_leases.add_column("Last Heartbeat")
    for l in leases:
        table_leases.add_row(str(l[0]), str(l[1]), str(l[2]), str(l[4] or "-"), str(l[7]))
    print(table_leases)

    table_locks = Table(title="Locked Files")
    table_locks.add_column("File Path", style="green")
    table_locks.add_column("Task ID", style="cyan")
    table_locks.add_column("Lock Type")
    for lk in locks:
        table_locks.add_row(str(lk[0]), str(lk[1]), str(lk[2]))
    print(table_locks)


@run_app.command("sweep")
def run_sweep(
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    root = _get_root(path)
    container = _get_container(root)
    ops = container.get(Operations)
    swept = ops.sweep_leases()
    if swept:
        print(f"[yellow]Swept {len(swept)} expired lease(s): {', '.join(swept)}[/yellow]")
    else:
        print("[green]No expired leases found.[/green]")


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
        all_passed, results = ops.run_verifications(target_tid)
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


@app.command("next")
def next_tasks(
    limit: Annotated[int, typer.Option("--limit", "-n", help="Number of tasks")] = 5,
    strategy: Annotated[
        RecommendationStrategy, typer.Option("--strategy", help="Scoring strategy")
    ] = RecommendationStrategy.BALANCED,
    plan: Annotated[str | None, typer.Option("--plan", help="Filter by plan ID")] = None,
    model: Annotated[str | None, typer.Option("--model", help="Filter by acceptable model")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
    yaml_output: Annotated[
        bool, typer.Option("--yaml", help="Output as YAML (fewer tokens than JSON)")
    ] = False,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    root = _get_root(path)
    container = _get_container(root)
    heuristics = container.get(RecommendationEngine)

    ranked = heuristics.get_next_tasks(
        plan_id=plan, model_filter=model, strategy=strategy, limit=limit
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
    qualified_id: Annotated[str, typer.Argument(help="Qualified node path (e.g. AUTH-USER-LOGIN)")],
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
    qp = QualifiedPath.parse(qualified_id)
    if recursive and qp.section_key:
        print(
            "[red]--recursive renders a node, not one of its sections; drop the `:section` part[/red]"
        )
        raise typer.Exit(code=1)
    try:
        output = (
            renderer.render_recursive(qp.node_id, view=view)
            if recursive
            else renderer.render(qp.node_id, view=view)
        )
    except ValueError as exc:
        print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    sys.stdout.write(output + "\n")


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
        parts.append(
            files("taskmanager").joinpath(f"guides/{topic}.md").read_text(encoding="utf-8")
        )
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
        "depends_on": [
            {"id": dep_id, "gate": gate.value} if gate != NodeStatus.COMPLETED else dep_id
            for dep_id, gate in sorted(node_repo.get_dependency_edges(node.id))
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
    specs = {n.id: n for n in node_repo.list_nodes(kind=NodeKind.SPEC)}
    tasks = node_repo.list_nodes(kind=NodeKind.TASK)
    plans = node_repo.list_nodes(kind=NodeKind.PLAN)
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
    container = _get_container(root)
    container.get(DatabaseManager).init_all()
    importer = _RefusingImporter(container.get(BulkImporter))

    files = [f for f in sorted(directory.glob("*.json")) if f.name != "_config.json"]
    docs = [json.loads(f.read_text(encoding="utf-8")) for f in files]
    if not docs:
        print(f"[red]No export files in {directory}[/red]")
        raise typer.Exit(code=1)
    # Plans depend on each other, so the first pass keeps only the edges a document can satisfy
    # by itself and the second adds the rest; specs go last so their full data wins.
    plan_docs = [d for d in docs if d.get("plans")]
    spec_docs = [d for d in docs if not d.get("plans")]
    for doc in plan_docs:
        first = copy.deepcopy(doc)
        own = {n["id"] for p in first["plans"] for n in [p, *p.get("tasks", [])]}
        for p in first["plans"]:
            for n in [p, *p.get("tasks", [])]:
                n["depends_on"] = [d for d in n.get("depends_on", []) if d in own]
        importer.import_dict(first)
    for doc in [*plan_docs, *spec_docs]:
        importer.import_dict(doc)
    settings_file = directory / "_config.json"
    if settings_file.exists():
        with _user_errors():
            ConfigStore(root).replace(json.loads(settings_file.read_text(encoding="utf-8")))
    print(f"[green]Restored {len(plan_docs)} plans and {len(spec_docs)} specs into {root}[/green]")


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
    status: Annotated[NodeStatus | None, typer.Option("--status", help="Filter by status")] = None,
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

    fastapi_app = create_app(root)
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


if __name__ == "__main__":
    app()
