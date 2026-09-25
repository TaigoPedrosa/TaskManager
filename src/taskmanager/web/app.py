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
from taskmanager.engine.snapshot import (
    DisplayView,
    SnapshotBuilder,
    chain_holder,
    phase_of,
    stored_status,
    waits_on,
)
from taskmanager.renderers.markdown import MarkdownRenderer
from taskmanager.web.ui import get_web_html


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


class DependencyAdd(BaseModel):
    id: str


class DependenciesUpdate(BaseModel):
    add: list[DependencyAdd] = Field(default_factory=list)
    remove: list[str] = Field(default_factory=list)


class SupersedeRequest(BaseModel):
    by: str
    transfer_blocks: str | list[str] = TransferMode.ALL.value


class MoveRequest(BaseModel):
    plan: str


class SectionWrite(BaseModel):
    content: str
    header: str | None = None


class VerificationCreate(BaseModel):
    type: VerificationType
    target_path: str
    expected_pattern: str | None = None


class DecisionOptionIn(BaseModel):
    key: str
    label: str
    description: str = ""
    effect: DecisionEffect = DecisionEffect.NONE


class DecisionCreate(BaseModel):
    question: str
    slug: str | None = None
    priority: int = 50
    context: str | None = None
    options: list[DecisionOptionIn] = Field(default_factory=list)
    recommend: str | None = None
    allow_custom: bool = True
    raised_by: str | None = None
    blocks: list[str] = Field(default_factory=list)


class DecisionAnswerRequest(BaseModel):
    option: str | None = None
    text: str = ""
    rationale: str = ""


class DecisionWithdrawRequest(BaseModel):
    reason: str = ""


class DecisionBlocksUpdate(BaseModel):
    add: list[str] = Field(default_factory=list)
    remove: list[str] = Field(default_factory=list)


class AttachmentCreate(BaseModel):
    filename: str
    content_base64: str
    caption: str = ""
    source: str | None = None


def _bound_hosts(host: str, port: int) -> frozenset[str]:
    """Every `Host` header a write may legitimately arrive with, for the address `uvicorn` is
    actually bound to. Under DNS rebinding, an attacker's page navigates to a hostname that
    resolves to 127.0.0.1 but is still spelled with the attacker's own domain -- the browser then
    sends that domain in *both* `Host` and `Origin`, so comparing them to each other (as this
    guard used to) never catches it. Pinning `Host` to the bound loopback name/IP does, because
    the attacker's domain is never a member of this set regardless of what it puts in `Origin`."""
    hosts = {f"{host}:{port}"}
    if host in ("127.0.0.1", "localhost", "0.0.0.0"):
        hosts.add(f"127.0.0.1:{port}")
        hosts.add(f"localhost:{port}")
    return frozenset(hosts)


def _write_guard(allowed_hosts: frozenset[str] | None) -> Any:
    def guard(request: Request) -> str:
        """Every mutating route depends on this: a JSON body forces a CORS preflight a foreign
        page cannot pass, and Host pinning plus the Origin check catch what preflight alone would
        miss, DNS rebinding included."""
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise HTTPException(403, "write requires Content-Type: application/json")
        host = request.headers.get("host", "")
        if allowed_hosts is not None and host not in allowed_hosts:
            raise HTTPException(403, "unrecognized Host")
        origin = request.headers.get("origin")
        if origin is not None and urlsplit(origin).netloc != host:
            raise HTTPException(403, "cross-origin write refused")
        return request.headers.get("x-tm-actor") or "web"

    return guard


@contextmanager
def _refusals() -> Iterator[None]:
    try:
        yield
    except OperationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


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


class ConnectionManager:
    def __init__(self) -> None:
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict[str, Any]) -> None:
        disconnected: list[WebSocket] = []
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except WebSocketDisconnect, RuntimeError, OSError:
                disconnected.append(connection)
        for dead in disconnected:
            self.disconnect(dead)


def create_app(project_root: Path, host: str = "127.0.0.1", port: int | None = None) -> FastAPI:
    # `port=None` (tests, the static exporter's in-process TestClient) skips Host pinning and
    # keeps the old Origin-must-equal-Host check; the real server always passes its bound port,
    # so it is the only caller `_bound_hosts` needs to protect (see `_write_guard`'s docstring).
    allowed_hosts = _bound_hosts(host, port) if port is not None else None
    Actor = Annotated[str, Depends(_write_guard(allowed_hosts))]
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

    # Background change detection loop
    last_event_id: int = 0
    try:
        with db_mgr.get_ledger_connection() as conn:
            row = conn.execute("SELECT MAX(id) FROM ledger_events").fetchone()
            if row and row[0]:
                last_event_id = row[0]
    except sqlite3.Error, OSError:
        last_event_id = 0

    async def ledger_watcher() -> None:
        nonlocal last_event_id
        while True:
            try:
                await asyncio.sleep(0.7)
                with db_mgr.get_ledger_connection() as conn:
                    row = conn.execute("SELECT MAX(id) FROM ledger_events").fetchone()
                    current_max = row[0] if row and row[0] else 0
                    if current_max > last_event_id:
                        last_event_id = current_max
                        await ws_manager.broadcast(
                            {"type": "update", "latest_event_id": current_max}
                        )
            except asyncio.CancelledError:
                break
            except sqlite3.Error, OSError:
                await asyncio.sleep(0.5)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        watcher_task = asyncio.create_task(ledger_watcher())
        try:
            yield
        finally:
            watcher_task.cancel()
            try:
                await watcher_task
            except asyncio.CancelledError:
                pass

    app = FastAPI(title="TaskManager Visualizer", lifespan=lifespan)

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
            source_id in nodes
            and target.id in nodes
            and satisfied(view.snapshot, source_id, target.id)
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
        own = deps if deps is not None else node_repo.get_dependencies(node_id)
        rows = []
        for dep_id in own:
            dep = node_repo.get_node(dep_id)
            rows.append(
                _relation_row(view, dep_id, dep is not None and _finished(view, node_id, dep))
            )
        # What a container waits on its children wait on too, and a migration writer waits
        # behind its chain's holder: both are named, marked as edges not its own.
        snap = view.snapshot
        node = node_repo.get_node(node_id)
        if node is None or node_id not in snap.nodes:
            return rows
        work, decisions = waits_on(snap, node)
        owners = snap.edge_owners(node_id)
        for dep_id, owner in owners.items():
            if owner != node_id and dep_id in snap.nodes:
                row = _relation_row(view, dep_id, dep_id not in work and dep_id not in decisions)
                rows.append(row | {"inherited_from": owner})
        holder = chain_holder(snap, node)
        if holder is not None and holder not in owners:
            rows.append(_relation_row(view, holder, False) | {"migration_chain": node.target_repo})
        return rows

    def dependent_details(
        node_id: str, view: DisplayView, blocked_by: list[str] | None = None
    ) -> list[dict[str, Any]]:
        node = node_repo.get_node(node_id)
        return [
            _relation_row(view, src_id, node is not None and _finished(view, src_id, node))
            for src_id in (
                blocked_by if blocked_by is not None else node_repo.get_blocked_by(node_id)
            )
        ]

    def lifecycle_fields(n: Node, view: DisplayView) -> dict[str, Any]:
        # Read through stored_status so a node saved under an old name shows its new one.
        status = {"status": stored_status(n).value, "display": view.display(n)}
        if n.kind == NodeKind.DECISION:
            # A decision has no cycle: it is never claimed, reviewed, fixed or landed.
            return status
        return {
            **status,
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
            "landing_chain": landing_chain(view.snapshot, n.id),
            "base_chain": base_chain(view.snapshot, n.id),
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

    def _attachment_size(asset_name: str) -> int | None:
        # Same asset-name check and containment check as `get_asset` below: an attachment
        # entry's `asset` is frontmatter, so a crafted or corrupted one is treated as missing
        # rather than stat'd wherever it points.
        if not ASSET_NAME_RE.fullmatch(asset_name):
            return None
        assets_dir = (db_dir / "assets").resolve()
        candidate = (assets_dir / asset_name).resolve()
        if not candidate.is_relative_to(assets_dir):
            return None
        try:
            return candidate.stat().st_size
        except OSError:
            return None

    def _attachments_with_size(attachments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{**a, "size_bytes": _attachment_size(a.get("asset", ""))} for a in attachments]

    def _frontmatter_with_attachment_sizes(frontmatter: dict[str, Any]) -> dict[str, Any]:
        attachments = frontmatter.get("attachments")
        if not attachments:
            return frontmatter
        return {**frontmatter, "attachments": _attachments_with_size(attachments)}

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return get_web_html()

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await ws_manager.connect(websocket)
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            ws_manager.disconnect(websocket)

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
                "lease": lease_dict(n.id),
                "children": [],
            }

        tree: list[dict[str, Any]] = []

        def plan_to_dict(pnode: Any) -> dict[str, Any]:
            p_dict = node_to_dict(pnode)
            task_children = []
            for cid in node_repo.get_children(pnode.id):
                cnode = node_repo.get_node(cid)
                if cnode:
                    task_children.append(node_to_dict(cnode))
            p_dict["children"] = task_children
            return p_dict

        # A plan is nested under its spec when it has one; every other plan
        # (this estate runs plenty of them) still needs a root of its own,
        # so specs and standalone plans are both walked, never either/or.
        spec_parented_plan_ids: set[str] = set()
        for s in specs:
            s_dict = node_to_dict(s)
            plan_children: list[dict[str, Any]] = []
            for pid in node_repo.get_children(s.id):
                pnode = node_repo.get_node(pid)
                if pnode and pnode.kind == NodeKind.PLAN:
                    plan_children.append(plan_to_dict(pnode))
                    spec_parented_plan_ids.add(pid)
            s_dict["children"] = plan_children
            tree.append(s_dict)

        for p in plans:
            if p.id not in spec_parented_plan_ids:
                tree.append(plan_to_dict(p))

        # Add any orphan tasks
        parented_ids: set[str] = set()
        for s in specs:
            parented_ids.update(node_repo.get_children(s.id))
        for p in plans:
            parented_ids.update(node_repo.get_children(p.id))

        for t in tasks:
            if t.id not in parented_ids:
                tree.append(node_to_dict(t))

        for root in tree:
            add_progress(root)
        return tree

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
            "specs": [
                {"id": s.id, "title": s.title} for s in node_repo.list_nodes(kind=NodeKind.SPEC)
            ],
            "plans": [
                {"id": p.id, "title": p.title} for p in node_repo.list_nodes(kind=NodeKind.PLAN)
            ],
        }

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

    @app.post("/api/nodes/{node_id}/dependencies")
    def post_dependencies(
        node_id: str, body: DependenciesUpdate, actor: Actor
    ) -> list[dict[str, Any]]:
        with _refusals():
            deps = operations.with_actor(actor).set_dependencies(
                node_id, [d.id for d in body.add], body.remove
            )
        return [{"id": dep_id} for dep_id in deps]

    @app.post("/api/nodes/{node_id}/supersede")
    def post_supersede(node_id: str, body: SupersedeRequest, actor: Actor) -> dict[str, str]:
        transfer = (
            body.transfer_blocks
            if isinstance(body.transfer_blocks, str)
            else ",".join(body.transfer_blocks)
        )
        with _refusals():
            operations.with_actor(actor).supersede(node_id, body.by, transfer)
        return {"superseded_by": body.by}

    @app.post("/api/nodes/{node_id}/move")
    def post_move(node_id: str, body: MoveRequest, actor: Actor) -> dict[str, str]:
        with _refusals():
            operations.with_actor(actor).move_task(node_id, body.plan)
        return {"plan": body.plan}

    @app.put("/api/nodes/{node_id}/sections/{key}")
    def put_section(node_id: str, key: str, body: SectionWrite, actor: Actor) -> dict[str, str]:
        with _refusals():
            operations.with_actor(actor).set_section(node_id, key, body.content, body.header)
        return {"key": key}

    @app.delete("/api/nodes/{node_id}/sections/{key}")
    def delete_section(node_id: str, key: str, actor: Actor) -> dict[str, str]:
        with _refusals():
            operations.with_actor(actor).remove_section(node_id, key)
        return {"key": key}

    @app.post("/api/nodes/{node_id}/verifications", status_code=201)
    def post_verification(node_id: str, body: VerificationCreate, actor: Actor) -> dict[str, Any]:
        with _refusals():
            ver = operations.with_actor(actor).add_verification(
                node_id, body.type, body.target_path, body.expected_pattern
            )
        return {
            "id": ver.id,
            "type": ver.verification_type.value,
            "target": ver.target_path,
            "pattern": ver.expected_pattern,
        }

    @app.delete("/api/nodes/{node_id}/verifications/{verification_id}")
    def delete_verification(node_id: str, verification_id: int, actor: Actor) -> dict[str, int]:
        with _refusals():
            operations.with_actor(actor).remove_verification(node_id, verification_id)
        return {"id": verification_id}

    @app.post("/api/nodes/{node_id}/verify")
    def post_verify(node_id: str, actor: Actor) -> list[dict[str, Any]]:
        with _refusals():
            _all_passed, results = operations.with_actor(actor).run_verifications(node_id)
        return [
            {
                "id": r.verification_id,
                "type": r.verification_type.value,
                "target": r.target_path,
                "passed": r.passed,
                "detail": r.message,
            }
            for r in results
        ]

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

    @app.post("/api/decisions", status_code=201)
    def create_decision(body: DecisionCreate, actor: Actor) -> dict[str, str]:
        # Operations.add_decision still takes the CLI's "key|Label|description|effect" strings;
        # the web form collects the same fields structured, so it is rejoined here rather than
        # growing a second option shape inside Operations.
        options = [f"{o.key}|{o.label}|{o.description}|{o.effect.value}" for o in body.options]
        with _refusals():
            decision_id = operations.with_actor(actor).add_decision(
                body.question,
                body.slug,
                body.priority,
                body.context,
                options,
                body.recommend,
                body.allow_custom,
                body.raised_by,
                body.blocks,
            )
        return {"id": decision_id}

    @app.post("/api/decisions/{decision_id}/answer")
    def post_decision_answer(
        decision_id: str, body: DecisionAnswerRequest, actor: Actor
    ) -> dict[str, str]:
        with _refusals():
            operations.with_actor(actor).answer_decision(
                decision_id, body.option, body.text, body.rationale, actor
            )
        return {"id": decision_id}

    @app.post("/api/decisions/{decision_id}/reopen")
    def post_decision_reopen(decision_id: str, actor: Actor) -> dict[str, str]:
        with _refusals():
            operations.with_actor(actor).reopen_decision(decision_id)
        return {"id": decision_id}

    @app.post("/api/decisions/{decision_id}/withdraw")
    def post_decision_withdraw(
        decision_id: str, body: DecisionWithdrawRequest, actor: Actor
    ) -> dict[str, str]:
        with _refusals():
            operations.with_actor(actor).withdraw_decision(decision_id, body.reason)
        return {"id": decision_id}

    @app.post("/api/decisions/{decision_id}/blocks")
    def post_decision_blocks(
        decision_id: str, body: DecisionBlocksUpdate, actor: Actor
    ) -> dict[str, str]:
        with _refusals():
            operations.with_actor(actor).link_decision(
                decision_id, add=body.add, remove=body.remove
            )
        return {"id": decision_id}

    # -- attachments (§4, §5) ----------------------------------------------------------------

    @app.post("/api/nodes/{node_id}/attachments", status_code=201)
    def post_attachment(node_id: str, body: AttachmentCreate, actor: Actor) -> dict[str, Any]:
        try:
            content = base64.b64decode(body.content_base64, validate=True)
        except ValueError as exc:
            raise HTTPException(400, "content_base64 is not valid base64") from exc
        # Basename only: a filename is never a path, so `../../etc/passwd` cannot escape the
        # temp directory it is written into before Operations.attach copies it by content hash.
        # `Path(name).name` passes "." and ".." through unchanged (pathlib does not treat them
        # as having no name component), and joining either back onto the temp dir resolves to
        # the dir itself, so `write_bytes` hit an uncaught IsADirectoryError -- a 500, not the
        # 400 a bad filename should be.
        filename = Path(body.filename).name or "attachment"
        if filename in (".", ".."):
            raise HTTPException(400, "filename is not valid")
        with _refusals(), tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir) / filename
            tmp_path.write_bytes(content)
            entry = operations.with_actor(actor).attach(
                node_id, tmp_path, body.caption, body.source
            )
        return entry

    @app.post("/api/nodes/{node_id}/attachments/check")
    def post_attachment_check(node_id: str, actor: Actor) -> list[dict[str, Any]]:
        with _refusals():
            entries = operations.with_actor(actor).list_attachments(node_id, check=True)
        return entries

    @app.delete("/api/nodes/{node_id}/attachments/{asset}")
    def delete_attachment(node_id: str, asset: str, actor: Actor) -> dict[str, str]:
        with _refusals():
            operations.with_actor(actor).detach(node_id, asset)
        return {"asset": asset}

    # -- file serving (§4): read-only, no write guard ------------------------------------------

    def _served_headers(mime: str, filename: str) -> dict[str, str]:
        """A served file is same-origin content on the app that also runs `test_command`
        verifications, so anything that can render as a *document* here (an uploaded
        `evidence.html`, an SVG with an inline `<script>`) would execute with that origin's
        privileges if it is opened directly rather than embedded. `nosniff` stops the browser
        from upgrading a mislabelled file to something more active than its declared type;
        `sandbox` strips scripts, forms and top-level navigation from a direct open; and only a
        real image is offered `inline` -- everything else downloads, since `<img>`/`<video>`
        embedding ignores Content-Disposition but a direct navigation honours it."""
        disposition = "inline" if mime.startswith("image/") else "attachment"
        return {
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox",
            "Content-Disposition": f'{disposition}; filename="{filename}"',
        }

    @app.get("/assets/{name}")
    def get_asset(name: str) -> FileResponse:
        if not ASSET_NAME_RE.fullmatch(name):
            raise HTTPException(404, "not found")
        assets_dir = (db_dir / "assets").resolve()
        candidate = (assets_dir / name).resolve()
        if not candidate.is_relative_to(assets_dir) or not candidate.is_file():
            raise HTTPException(404, "not found")
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        return FileResponse(candidate, headers=_served_headers(mime, name))

    @app.get("/api/file")
    def get_file(path: str) -> FileResponse:
        root = project_root.resolve()
        # `.resolve()` follows symlinks, so a symlink inside root that points outside it
        # still fails the is_relative_to check below -- the real path is what is checked,
        # not the requested one. An absolute `path` overriding the join the same way.
        try:
            candidate = (root / path).resolve()
        except (OSError, ValueError) as exc:
            raise HTTPException(404, "not found") from exc
        if not candidate.is_relative_to(root) or not candidate.is_file():
            raise HTTPException(404, "not found")
        mime = mimetypes.guess_type(candidate.name)[0] or ""
        if not mime.startswith("image/"):
            raise HTTPException(404, "not found")
        return FileResponse(candidate, headers=_served_headers(mime, candidate.name))

    return app
