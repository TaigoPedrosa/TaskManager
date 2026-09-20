import json
import logging
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Annotated, Any

import typer
from dishka import Container, make_container
from rich import print
from rich.table import Table

from taskmanager.core.enums import NodeKind, NodeStatus, RelationType, VerificationType
from taskmanager.core.models import (
    LedgerEvent,
    Node,
    NodeRelation,
    NodeSection,
    NodeVerification,
)
from taskmanager.core.naming import QualifiedPath
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.ledger_repo import LedgerRepository
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.di.container import TaskManagerProvider
from taskmanager.engine.graph import GraphEngine
from taskmanager.engine.heuristics import RecommendationEngine
from taskmanager.engine.runtime import ExecutionCoordinator
from taskmanager.engine.verification import VerificationEngine
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
run_app = typer.Typer(name="run", help="Manage execution leases")
verify_app = typer.Typer(name="verify", help="Static verification and assertions")
audit_app = typer.Typer(name="audit", help="Audit ledger events")
web_app = typer.Typer(name="web", help="Interactive web visualizer and exporter")

app.add_typer(spec_app)
app.add_typer(plan_app)
app.add_typer(task_app)
app.add_typer(section_app)
app.add_typer(run_app)
app.add_typer(verify_app)
app.add_typer(audit_app)
app.add_typer(web_app)


def _get_root(path: Path | None) -> Path:
    return path.resolve() if path else Path.cwd().resolve()


def _get_container(path: Path | None) -> Container:
    root = _get_root(path)
    return make_container(TaskManagerProvider(root))


def _record_ledger(
    container: Container,
    command: str,
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
    root = _get_root(path)
    container = _get_container(root)
    db = container.get(DatabaseManager)
    db.init_all()
    _record_ledger(container, command="init", target_id=str(root))
    print(f"[green]Initialized .taskmanager in {root}[/green]")


@spec_app.command("add")
def spec_add(
    title: str,
    slug: Annotated[str | None, typer.Option("--slug", "-s", help="Specification slug/id")] = None,
    priority: Annotated[int, typer.Option("--priority", "-p", help="Priority (1-100)")] = 50,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    root = _get_root(path)
    container = _get_container(root)
    node_repo = container.get(NodeRepository)

    if slug:
        spec_id = slug
    else:
        existing = {n.id for n in node_repo.list_nodes(kind=NodeKind.SPEC)}
        counter = 1
        while f"S{counter}" in existing:
            counter += 1
        spec_id = f"S{counter}"

    node = Node(id=spec_id, kind=NodeKind.SPEC, title=title, priority=priority)
    node_repo.save_node(node)
    _record_ledger(
        container,
        command="spec add",
        target_id=spec_id,
        payload={"title": title, "priority": priority},
    )
    print(f"[green]Added spec {spec_id}[/green]")


@spec_app.command("list")
def spec_list(
    status: Annotated[str | None, typer.Option("--status", help="Filter by status")] = None,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    root = _get_root(path)
    container = _get_container(root)
    node_repo = container.get(NodeRepository)
    status_filter = NodeStatus(status.upper()) if status else None
    specs = node_repo.list_nodes(kind=NodeKind.SPEC, status=status_filter)

    table = Table(title="Specifications")
    table.add_column("ID", style="cyan")
    table.add_column("Title")
    table.add_column("Status", style="yellow")
    table.add_column("Priority", justify="right")
    for s in specs:
        table.add_row(s.id, s.title, s.status.value, str(s.priority))
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
    print(f"[bold]Title:[/] {spec.title}")
    print(f"[bold]Status:[/] {spec.status.value}")
    print(f"[bold]Priority:[/] {spec.priority}")
    if children:
        print(f"[bold]Plans:[/] {', '.join(children)}")


@plan_app.command("add")
def plan_add(
    title: str,
    spec: Annotated[str, typer.Option("--spec", help="Parent spec ID")],
    slug: Annotated[str | None, typer.Option("--slug", "-s", help="Plan slug")] = None,
    priority: Annotated[int, typer.Option("--priority", "-p", help="Priority")] = 50,
    require_review: Annotated[
        bool, typer.Option("--require-review", help="Inject review gate")
    ] = False,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    root = _get_root(path)
    container = _get_container(root)
    node_repo = container.get(NodeRepository)
    graph_engine = container.get(GraphEngine)

    if slug:
        plan_id = f"{spec}-{slug}"
    else:
        children = set(node_repo.get_children(spec))
        counter = 1
        while f"{spec}-P{counter}" in children:
            counter += 1
        plan_id = f"{spec}-P{counter}"

    plan_node = Node(id=plan_id, kind=NodeKind.PLAN, title=title, priority=priority)
    node_repo.save_node(plan_node)
    node_repo.add_relation(
        NodeRelation(source_id=spec, target_id=plan_id, relation_type=RelationType.CONTAINS)
    )

    if require_review:
        gate_id = graph_engine.inject_plan_review_gate(plan_id)
        _record_ledger(
            container,
            command="plan add",
            target_id=plan_id,
            payload={"title": title, "spec": spec, "review_gate": gate_id},
        )
        print(f"[green]Added plan {plan_id} with review gate {gate_id}[/green]")
    else:
        _record_ledger(
            container, command="plan add", target_id=plan_id, payload={"title": title, "spec": spec}
        )
        print(f"[green]Added plan {plan_id}[/green]")


@plan_app.command("list")
def plan_list(
    spec: Annotated[str | None, typer.Option("--spec", help="Filter by spec ID")] = None,
    status: Annotated[str | None, typer.Option("--status", help="Filter by status")] = None,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    root = _get_root(path)
    container = _get_container(root)
    node_repo = container.get(NodeRepository)
    status_filter = NodeStatus(status.upper()) if status else None
    plans = node_repo.list_nodes(kind=NodeKind.PLAN, status=status_filter)
    if spec:
        children = set(node_repo.get_children(spec))
        plans = [p for p in plans if p.id in children or p.id.startswith(f"{spec}-")]

    table = Table(title="Plans")
    table.add_column("ID", style="cyan")
    table.add_column("Title")
    table.add_column("Status", style="yellow")
    table.add_column("Priority", justify="right")
    for p in plans:
        table.add_row(p.id, p.title, p.status.value, str(p.priority))
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
    print(f"[bold]Title:[/] {plan.title}")
    print(f"[bold]Status:[/] {plan.status.value}")
    print(f"[bold]Priority:[/] {plan.priority}")
    if children:
        print(f"[bold]Tasks:[/] {', '.join(children)}")


@task_app.command("add")
def task_add(
    title: str,
    plan: Annotated[str, typer.Option("--plan", help="Parent plan ID")],
    slug: Annotated[str | None, typer.Option("--slug", "-s", help="Task slug")] = None,
    priority: Annotated[int, typer.Option("--priority", "-p", help="Priority")] = 50,
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
    node_repo = container.get(NodeRepository)

    if slug:
        task_id = f"{plan}-{slug}"
    else:
        children = set(node_repo.get_children(plan))
        counter = 1
        while f"{plan}-T{counter}" in children:
            counter += 1
        task_id = f"{plan}-T{counter}"

    acceptable_models = [m.strip() for m in models.split(",") if m.strip()] if models else []
    task_node = Node(
        id=task_id,
        kind=NodeKind.TASK,
        title=title,
        priority=priority,
        acceptable_models=acceptable_models,
    )
    node_repo.save_node(task_node)
    node_repo.add_relation(
        NodeRelation(source_id=plan, target_id=task_id, relation_type=RelationType.CONTAINS)
    )

    if depends_on:
        deps = [d.strip() for d in depends_on.split(",") if d.strip()]
        for dep in deps:
            node_repo.add_relation(
                NodeRelation(
                    source_id=task_id, target_id=dep, relation_type=RelationType.DEPENDS_ON
                )
            )

    _record_ledger(
        container, command="task add", target_id=task_id, payload={"title": title, "plan": plan}
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
    ] = "all",
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    root = _get_root(path)
    container = _get_container(root)
    node_repo = container.get(NodeRepository)

    old_node = node_repo.get_node(old_id)
    if not old_node:
        print(f"[red]Task '{old_id}' not found[/red]")
        raise typer.Exit(code=1)

    old_node.status = NodeStatus.SUPERSEDED
    node_repo.save_node(old_node)

    node_repo.add_relation(
        NodeRelation(source_id=new_id, target_id=old_id, relation_type=RelationType.SUPERSEDES)
    )

    tb_val = transfer_blocks.strip().lower()
    if tb_val == "all":
        node_repo.transfer_blocks(old_id, new_id, "all")
    elif tb_val == "none":
        node_repo.transfer_blocks(old_id, new_id, "none")
    else:
        custom_ids = [x.strip() for x in transfer_blocks.split(",") if x.strip()]
        node_repo.transfer_blocks(old_id, new_id, "custom", custom_ids=custom_ids)

    _record_ledger(
        container,
        command="task supersede",
        target_id=old_id,
        payload={"superseded_by": new_id, "transfer_blocks": transfer_blocks},
    )
    print(f"[green]Task {old_id} superseded by {new_id}[/green]")


@task_app.command("list")
def task_list(
    plan: Annotated[str | None, typer.Option("--plan", help="Filter by plan ID")] = None,
    status: Annotated[str | None, typer.Option("--status", help="Filter by status")] = None,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    root = _get_root(path)
    container = _get_container(root)
    node_repo = container.get(NodeRepository)
    status_filter = NodeStatus(status.upper()) if status else None
    tasks = node_repo.list_nodes(kind=NodeKind.TASK, status=status_filter)
    if plan:
        children = set(node_repo.get_children(plan))
        tasks = [t for t in tasks if t.id in children or t.id.startswith(f"{plan}-")]

    table = Table(title="Tasks")
    table.add_column("ID", style="cyan")
    table.add_column("Title")
    table.add_column("Status", style="yellow")
    table.add_column("Priority", justify="right")
    table.add_column("Models")
    for t in tasks:
        table.add_row(
            t.id, t.title, t.status.value, str(t.priority), ", ".join(t.acceptable_models)
        )
    print(table)


@task_app.command("get")
def task_get(
    task_id: str,
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
    print(f"[bold cyan]Task:[/] {task.id}")
    print(f"[bold]Title:[/] {task.title}")
    print(f"[bold]Status:[/] {task.status.value}")
    print(f"[bold]Priority:[/] {task.priority}")
    print(f"[bold]Models:[/] {', '.join(task.acceptable_models)}")
    if deps:
        print(f"[bold]Depends On:[/] {', '.join(deps)}")
    if verifications:
        v_str = ", ".join(f"{v.verification_type.value}:{v.target_path}" for v in verifications)
        print(f"[bold]Verifications:[/] {v_str}")


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
            print(f"{s.header}\n{s.content}\n")
        return

    sec = node_repo.get_section(qp.node_id, qp.section_key)
    if not sec:
        print(f"[red]Section '{qp.section_key}' not found on node '{qp.node_id}'[/red]")
        raise typer.Exit(code=1)
    output = f"{sec.header}\n{sec.content}" if sec.header else sec.content
    print(output)


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
    node_repo = container.get(NodeRepository)
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

    sec_header = header or f"## {qp.section_key.capitalize()}"
    existing_secs = node_repo.get_all_sections(qp.node_id)
    existing = next((s for s in existing_secs if s.section_key == qp.section_key), None)
    ordinal = existing.ordinal if existing else len(existing_secs) + 1

    node_section = NodeSection(
        node_id=qp.node_id,
        section_key=qp.section_key,
        ordinal=ordinal,
        header=sec_header,
        content=text_content,
    )
    node_repo.save_section(node_section)
    _record_ledger(container, command="section set", target_id=qualified_path)
    print(f"[green]Saved section {qualified_path}[/green]")


@run_app.command("start")
def run_start(
    task_id: str,
    worktree: Annotated[
        bool, typer.Option("--worktree", help="Create isolated git worktree")
    ] = False,
    agent: Annotated[str, typer.Option("--agent", help="Agent identifier")] = "agent-1",
    session: Annotated[str, typer.Option("--session", help="Session identifier")] = "session-1",
    account: Annotated[str | None, typer.Option("--account", help="Account identifier")] = None,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    root = _get_root(path)
    container = _get_container(root)
    coordinator = container.get(ExecutionCoordinator)

    worktree_base = root / ".worktrees" if worktree else None
    lease = coordinator.start_task(
        task_id=task_id,
        agent_id=agent,
        session_id=session,
        account_id=account,
        create_worktree=worktree,
        worktree_base=worktree_base,
    )
    _record_ledger(
        container,
        command="run start",
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
        _record_ledger(container, command="run heartbeat", target_id=tid)
        print(f"[green]Heartbeat recorded for {tid}[/green]")
    else:
        print(f"[red]No active lease found for {tid}[/red]")
        raise typer.Exit(code=1)


@run_app.command("stop")
def run_stop(
    task_id: Annotated[
        str | None, typer.Argument(help="Task ID (optional if inside worktree)")
    ] = None,
    status: Annotated[str, typer.Option("--status", help="Target status")] = "WAITING_REVIEW",
    remove_worktree: Annotated[
        bool, typer.Option("--remove-worktree", help="Remove worktree if created")
    ] = False,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    root = _get_root(path)
    container = _get_container(root)
    runtime_repo = container.get(RuntimeRepository)
    coordinator = container.get(ExecutionCoordinator)

    tid = _resolve_task_id(runtime_repo, task_id)
    status_enum = NodeStatus(status.upper())
    coordinator.stop_task(task_id=tid, new_status=status_enum, remove_worktree=remove_worktree)
    _record_ledger(
        container,
        command="run stop",
        target_id=tid,
        payload={"status": status_enum.value, "remove_worktree": remove_worktree},
    )
    print(f"[green]Stopped task {tid} with status {status_enum.value}[/green]")


@run_app.command("list")
def run_list(
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
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

    if json_output:
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
        print(json.dumps(data, indent=2))
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
    runtime_repo = container.get(RuntimeRepository)
    swept = runtime_repo.sweep_expired_leases()
    if swept:
        _record_ledger(container, command="run sweep", payload={"swept_tasks": swept})
        print(f"[yellow]Swept {len(swept)} expired lease(s): {', '.join(swept)}[/yellow]")
    else:
        print("[green]No expired leases found.[/green]")


@verify_app.command("add")
def verify_add(
    task_id: str,
    type: Annotated[
        str, typer.Option("--type", "-t", help="Verification type (e.g. file_exists, test_command)")
    ],
    target: Annotated[str, typer.Option("--target", help="Target path or command")],
    pattern: Annotated[
        str | None, typer.Option("--pattern", help="Expected pattern or test command")
    ] = None,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    root = _get_root(path)
    container = _get_container(root)
    node_repo = container.get(NodeRepository)
    v_type = VerificationType(type.lower())
    ver = NodeVerification(
        node_id=task_id,
        verification_type=v_type,
        target_path=target,
        expected_pattern=pattern,
    )
    node_repo.add_verification(ver)
    _record_ledger(
        container,
        command="verify add",
        target_id=task_id,
        payload={"type": v_type.value, "target": target},
    )
    print(f"[green]Added {v_type.value} verification to task {task_id}[/green]")


@verify_app.command("run")
def verify_run(
    task_id: Annotated[str | None, typer.Argument(help="Task ID to verify")] = None,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    root = _get_root(path)
    container = _get_container(root)
    node_repo = container.get(NodeRepository)
    verification_engine = container.get(VerificationEngine)
    runtime_repo = container.get(RuntimeRepository)

    target_tid = task_id
    if not target_tid:
        try:
            target_tid = _resolve_task_id(runtime_repo, None)
        except typer.BadParameter:
            target_tid = None

    if target_tid:
        vers = node_repo.get_verifications(target_tid)
    else:
        all_tasks = node_repo.list_nodes(kind=NodeKind.TASK)
        vers = []
        for t in all_tasks:
            vers.extend(node_repo.get_verifications(t.id))

    if not vers:
        print("[yellow]No verifications to run.[/yellow]")
        return

    results = verification_engine.verify_all(vers)
    table = Table(title="Verification Results")
    table.add_column("Target", style="cyan")
    table.add_column("Type")
    table.add_column("Status")
    table.add_column("Message")

    all_passed = True
    for r in results:
        status_str = "[green]PASSED[/green]" if r.passed else "[red]FAILED[/red]"
        if not r.passed:
            all_passed = False
        table.add_row(r.target_path, r.verification_type.value, status_str, r.message)
    print(table)

    _record_ledger(
        container,
        command="verify run",
        target_id=target_tid,
        payload={"passed": all_passed, "count": len(results)},
    )
    if not all_passed:
        raise typer.Exit(code=1)


@app.command("next")
def next_tasks(
    limit: Annotated[int, typer.Option("--limit", "-n", help="Number of tasks")] = 5,
    strategy: Annotated[str, typer.Option("--strategy", help="Scoring strategy")] = "balanced",
    plan: Annotated[str | None, typer.Option("--plan", help="Filter by plan ID")] = None,
    model: Annotated[str | None, typer.Option("--model", help="Filter by acceptable model")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Output as JSON")] = False,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    root = _get_root(path)
    container = _get_container(root)
    heuristics = container.get(RecommendationEngine)

    ranked = heuristics.get_next_tasks(
        plan_id=plan, model_filter=model, strategy=strategy, limit=limit
    )

    if json_output:
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
        print(json.dumps(data, indent=2))
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
            t.task_id,
            t.title,
            t.plan_id or "-",
            f"{t.score:.2f}",
            str(t.priority),
            ", ".join(t.acceptable_models),
        )
    print(table)


@app.command("render")
def render(
    qualified_id: Annotated[str, typer.Argument(help="Qualified node path (e.g. AUTH-USER-LOGIN)")],
    view: Annotated[
        str, typer.Option("--view", "-v", help="View projection: summary, subagent, or full")
    ] = "full",
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    root = _get_root(path)
    container = _get_container(root)
    renderer = container.get(MarkdownRenderer)
    qp = QualifiedPath.parse(qualified_id)
    output = renderer.render(qp.node_id, view=view)
    print(output)


@app.command("import")
def import_cmd(
    format: Annotated[
        str, typer.Option("--format", help="Input format: markdown, yaml, or json")
    ] = "json",
    file: Annotated[
        Path | None, typer.Option("--file", "-f", help="File to import (defaults to stdin)")
    ] = None,
    path: Annotated[Path | None, typer.Option("--path", "-C")] = None,
) -> None:
    root = _get_root(path)
    container = _get_container(root)
    importer = container.get(BulkImporter)

    if file:
        content = file.read_text(encoding="utf-8")
    else:
        content = sys.stdin.read()

    fmt = format.lower()
    if fmt == "json":
        data = json.loads(content)
        importer.import_dict(data)
    elif fmt == "yaml":
        try:
            import importlib

            yaml_mod = importlib.import_module("yaml")
            data = yaml_mod.safe_load(content)
        except ImportError, ValueError, AttributeError:
            data = json.loads(content)
        importer.import_dict(data)
    elif fmt == "markdown":
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
    else:
        raise typer.BadParameter(f"Unsupported format '{format}'")

    _record_ledger(
        container,
        command="import",
        payload={"format": fmt, "file": str(file) if file else "stdin"},
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
        table.add_row(str(e.id or "-"), str(e.timestamp), e.actor_id, e.command, e.target_id or "-")
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

    root = _get_root(path)
    db_mgr = DatabaseManager(root / ".taskmanager")
    if not db_mgr.is_initialized():
        print(f"[red]Error:[/red] TaskManager is not initialized in {root}. Run 'tm init' first.")
        raise typer.Exit(code=1)

    exported = export_static_html(root, output)
    print(
        f"[green]Exported static HTML visualizer to[/green] [bold cyan]{exported.resolve()}[/bold cyan]"
    )


if __name__ == "__main__":
    app()
