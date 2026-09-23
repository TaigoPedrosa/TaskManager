"""FastAPI application for the TaskManager interactive web visualizer."""

import asyncio
import base64
import mimetypes
import re
import sqlite3
import tempfile
from collections import Counter
from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from taskmanager.core.enums import (
    NodeKind,
    NodeStatus,
    RenderView,
    TransferMode,
    VerificationType,
    VirtualStatus,
)
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.ledger_repo import LedgerRepository
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.engine.git import GitManager
from taskmanager.engine.graph import GraphEngine
from taskmanager.engine.heuristics import score_every_task
from taskmanager.engine.operations import OperationError, Operations
from taskmanager.engine.runtime import ExecutionCoordinator
from taskmanager.engine.verification import VerificationEngine
from taskmanager.renderers.markdown import MarkdownRenderer
from taskmanager.web.ui import get_web_html


class SpecCreate(BaseModel):
    title: str
    slug: str | None = None
    priority: int = 50


class PlanCreate(BaseModel):
    title: str
    spec: str
    slug: str | None = None
    priority: int = 50
    order: int = 0
    require_review: bool = False


class TaskCreate(BaseModel):
    title: str
    plan: str
    slug: str | None = None
    priority: int = 50
    order: int = 0
    depends_on: list[str] = Field(default_factory=list)
    models: list[str] = Field(default_factory=list)


class NodeUpdate(BaseModel):
    title: str | None = None
    priority: int | None = None
    acceptable_models: list[str] | None = None
    target_repo: str | None = None
    frontmatter_set: dict[str, Any] | None = None
    frontmatter_unset: list[str] | None = None


class StatusUpdate(BaseModel):
    status: NodeStatus
    remove_worktree: bool = False


class DependencyAdd(BaseModel):
    id: str
    gate: NodeStatus | None = None


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


# Content-addressed asset names are always 16 hex chars plus the source file's own extension
# (`engine.assets.store_asset`); anything else cannot be one of ours.
_ASSET_NAME_RE = re.compile(r"^[0-9a-f]{16}\.[A-Za-z0-9]{1,8}$")


def _write_guard(request: Request) -> str:
    """Every mutating route depends on this: a JSON body forces a CORS preflight a foreign
    page cannot pass, and a mismatched Origin catches what preflight alone would miss."""
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise HTTPException(403, "write requires Content-Type: application/json")
    origin = request.headers.get("origin")
    if origin is not None and urlsplit(origin).netloc != request.headers.get("host", ""):
        raise HTTPException(403, "cross-origin write refused")
    return request.headers.get("x-tm-actor") or "web"


Actor = Annotated[str, Depends(_write_guard)]


@contextmanager
def _refusals() -> Iterator[None]:
    try:
        yield
    except OperationError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


def add_progress(node: dict[str, Any]) -> Counter[str]:
    counts: Counter[str] = Counter()
    if node["kind"] == NodeKind.TASK.value:
        counts[node["virtual_status"]] += 1
    for child in node["children"]:
        counts += add_progress(child)
    if node["kind"] != NodeKind.TASK.value:
        node["progress"] = {"total": sum(counts.values()), "counts": dict(counts)}
    return counts


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


def create_app(project_root: Path) -> FastAPI:
    db_dir = project_root / ".taskmanager"
    db_mgr = DatabaseManager(db_dir)
    node_repo = NodeRepository(db_mgr)
    runtime_repo = RuntimeRepository(db_mgr)
    ledger_repo = LedgerRepository(db_mgr)
    graph_engine = GraphEngine(node_repo, runtime_repo)
    renderer = MarkdownRenderer(node_repo)
    coordinator = ExecutionCoordinator(
        node_repo, runtime_repo, graph_engine, GitManager(project_root)
    )
    verification_engine = VerificationEngine(project_root)
    operations = Operations(
        node_repo,
        runtime_repo,
        graph_engine,
        coordinator,
        ledger_repo,
        verification_engine,
        actor="web",
    )
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

    def effective_status(n: Any) -> str:
        if n.kind == NodeKind.TASK:
            return graph_engine.resolve_task_state(n.id).value
        if n.kind == NodeKind.PLAN:
            return graph_engine.resolve_plan_status(n.id).value
        return str(n.status.value)

    def _relation_details(related_ids: list[str]) -> list[dict[str, Any]]:
        details: list[dict[str, Any]] = []
        for rel_id in related_ids:
            rel = node_repo.get_node(rel_id)
            details.append(
                {
                    "id": rel_id,
                    "title": rel.title if rel else None,
                    "status": effective_status(rel) if rel else None,
                    # Same rule as GraphEngine.resolve_task_state: a missing dependency blocks.
                    "finished": rel is not None
                    and rel.status in (NodeStatus.COMPLETED, NodeStatus.SUPERSEDED),
                }
            )
        return details

    def dependency_details(node_id: str) -> list[dict[str, Any]]:
        return _relation_details(node_repo.get_dependencies(node_id))

    def dependent_details(node_id: str) -> list[dict[str, Any]]:
        return _relation_details(node_repo.get_blocked_by(node_id))

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
    async def get_tree() -> list[dict[str, Any]]:
        specs = node_repo.list_nodes(kind=NodeKind.SPEC)
        plans = node_repo.list_nodes(kind=NodeKind.PLAN)
        tasks = node_repo.list_nodes(kind=NodeKind.TASK)
        task_scores = score_every_task(node_repo)

        def node_to_dict(n: Any) -> dict[str, Any]:
            sections = node_repo.get_all_sections(n.id)
            verifications = node_repo.get_verifications(n.id) if n.kind == NodeKind.TASK else []
            lease = runtime_repo.get_lease(n.id) if n.kind == NodeKind.TASK else None

            return {
                "id": n.id,
                "kind": n.kind.value,
                "title": n.title,
                "status": n.status.value,
                "virtual_status": effective_status(n),
                "priority": n.priority,
                "score": task_scores.get(n.id) if n.kind == NodeKind.TASK else None,
                "ordinal": n.ordinal,
                "target_repo": n.target_repo,
                "acceptable_models": n.acceptable_models,
                "frontmatter": n.frontmatter,
                "dependencies": node_repo.get_dependencies(n.id),
                "dependency_details": dependency_details(n.id),
                "blocked_by": node_repo.get_blocked_by(n.id),
                "dependent_details": dependent_details(n.id),
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
                "lease": {
                    "agent_id": lease.agent_id,
                    "session_id": lease.session_id,
                    "branch_name": lease.branch_name,
                    "worktree_path": lease.worktree_path,
                }
                if lease
                else None,
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
    async def get_graph() -> dict[str, Any]:
        all_nodes = node_repo.list_nodes()
        task_scores = score_every_task(node_repo)
        nodes_out: list[dict[str, Any]] = []
        for n in all_nodes:
            nodes_out.append(
                {
                    "id": n.id,
                    "title": n.title,
                    "kind": n.kind.value,
                    "status": effective_status(n),
                    "priority": n.priority,
                    "score": task_scores.get(n.id) if n.kind == NodeKind.TASK else None,
                    "ordinal": n.ordinal,
                    "target_repo": n.target_repo,
                    "acceptable_models": n.acceptable_models,
                }
            )

        edges_out: list[dict[str, Any]] = []
        with db_mgr.get_spec_connection() as conn:
            rows = conn.execute(
                "SELECT source_id, target_id, relation_type FROM node_relations"
            ).fetchall()
            for r in rows:
                edges_out.append(
                    {
                        "source": r[0],
                        "target": r[1],
                        "type": r[2],
                    }
                )

        return {"nodes": nodes_out, "edges": edges_out}

    @app.get("/api/nodes/{node_id}")
    async def get_node_detail(node_id: str) -> dict[str, Any]:
        node = node_repo.get_node(node_id)
        if not node:
            raise HTTPException(status_code=404, detail="Node not found")

        sections = node_repo.get_all_sections(node_id)
        verifications = node_repo.get_verifications(node_id)
        lease = runtime_repo.get_lease(node_id)
        dependencies = node_repo.get_dependencies(node_id)
        blocked_by = node_repo.get_blocked_by(node_id)

        return {
            "node": {
                "id": node.id,
                "kind": node.kind.value,
                "title": node.title,
                "status": node.status.value,
                "priority": node.priority,
                "ordinal": node.ordinal,
                "target_repo": node.target_repo,
                "acceptable_models": node.acceptable_models,
                "frontmatter": node.frontmatter,
            },
            "virtual_status": effective_status(node),
            "rendered_markdown": renderer.render(node_id, view=RenderView.FULL),
            "dependencies": dependencies,
            "dependency_details": dependency_details(node_id),
            "blocked_by": blocked_by,
            "dependent_details": dependent_details(node_id),
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
                    "verification_type": v.verification_type.value,
                    "target_path": v.target_path,
                    "expected_pattern": v.expected_pattern,
                }
                for v in verifications
            ],
            "lease": {
                "agent_id": lease.agent_id,
                "session_id": lease.session_id,
                "branch_name": lease.branch_name,
                "worktree_path": lease.worktree_path,
                "ttl_seconds": lease.ttl_seconds,
            }
            if lease
            else None,
        }

    @app.get("/api/stats")
    async def get_stats() -> dict[str, int]:
        all_tasks = node_repo.list_nodes(kind=NodeKind.TASK)
        stats: dict[str, int] = {
            "total": len(all_tasks),
            **{s.value: 0 for s in NodeStatus},
            **{v.value: 0 for v in VirtualStatus},
        }

        for t in all_tasks:
            stats[effective_status(t)] += 1

        return stats

    # -- write API (§5): generic routes only, decisions and attachments are added later ----

    @app.get("/api/meta")
    async def get_meta() -> dict[str, Any]:
        all_nodes = node_repo.list_nodes()
        return {
            "statuses": [s.value for s in NodeStatus] + [v.value for v in VirtualStatus],
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
    async def create_spec(body: SpecCreate, actor: Actor) -> dict[str, str]:
        with _refusals():
            spec_id = operations.with_actor(actor).add_spec(body.title, body.slug, body.priority)
        return {"id": spec_id}

    @app.post("/api/plans", status_code=201)
    async def create_plan(body: PlanCreate, actor: Actor) -> dict[str, Any]:
        with _refusals():
            plan_id, gate_id = operations.with_actor(actor).add_plan(
                body.title, body.spec, body.slug, body.priority, body.order, body.require_review
            )
        return {"id": plan_id, "review_gate": gate_id}

    @app.post("/api/tasks", status_code=201)
    async def create_task(body: TaskCreate, actor: Actor) -> dict[str, str]:
        with _refusals():
            task_id = operations.with_actor(actor).add_task(
                body.title,
                body.plan,
                body.slug,
                body.priority,
                body.order,
                body.depends_on,
                body.models,
            )
        return {"id": task_id}

    @app.patch("/api/nodes/{node_id}")
    async def patch_node(node_id: str, body: NodeUpdate, actor: Actor) -> dict[str, Any]:
        with _refusals():
            changed = operations.with_actor(actor).update_node(
                node_id,
                title=body.title,
                priority=body.priority,
                models=body.acceptable_models,
                repo=body.target_repo,
                frontmatter_set=body.frontmatter_set,
                frontmatter_unset=body.frontmatter_unset,
            )
        return changed

    @app.post("/api/nodes/{node_id}/status")
    async def post_status(node_id: str, body: StatusUpdate, actor: Actor) -> dict[str, str]:
        try:
            operations.with_actor(actor).set_status(node_id, body.status, body.remove_worktree)
        except OperationError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        except ValueError as exc:
            # `ExecutionCoordinator.stop_task` raises a bare ValueError for an unknown node.
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"status": body.status.value}

    @app.post("/api/nodes/{node_id}/dependencies")
    async def post_dependencies(
        node_id: str, body: DependenciesUpdate, actor: Actor
    ) -> list[dict[str, Any]]:
        add = [(d.id, d.gate) for d in body.add]
        with _refusals():
            edges = operations.with_actor(actor).set_dependencies(node_id, add, body.remove)
        return [{"id": dep_id, "gate": gate.value} for dep_id, gate in edges]

    @app.post("/api/nodes/{node_id}/supersede")
    async def post_supersede(node_id: str, body: SupersedeRequest, actor: Actor) -> dict[str, str]:
        transfer = (
            body.transfer_blocks
            if isinstance(body.transfer_blocks, str)
            else ",".join(body.transfer_blocks)
        )
        with _refusals():
            operations.with_actor(actor).supersede(node_id, body.by, transfer)
        return {"superseded_by": body.by}

    @app.post("/api/nodes/{node_id}/move")
    async def post_move(node_id: str, body: MoveRequest, actor: Actor) -> dict[str, str]:
        with _refusals():
            operations.with_actor(actor).move_task(node_id, body.plan)
        return {"plan": body.plan}

    @app.put("/api/nodes/{node_id}/sections/{key}")
    async def put_section(
        node_id: str, key: str, body: SectionWrite, actor: Actor
    ) -> dict[str, str]:
        with _refusals():
            operations.with_actor(actor).set_section(node_id, key, body.content, body.header)
        return {"key": key}

    @app.delete("/api/nodes/{node_id}/sections/{key}")
    async def delete_section(node_id: str, key: str, actor: Actor) -> dict[str, str]:
        with _refusals():
            operations.with_actor(actor).remove_section(node_id, key)
        return {"key": key}

    @app.post("/api/nodes/{node_id}/verifications", status_code=201)
    async def post_verification(
        node_id: str, body: VerificationCreate, actor: Actor
    ) -> dict[str, Any]:
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
    async def delete_verification(
        node_id: str, verification_id: int, actor: Actor
    ) -> dict[str, int]:
        with _refusals():
            operations.with_actor(actor).remove_verification(node_id, verification_id)
        return {"id": verification_id}

    @app.post("/api/nodes/{node_id}/verify")
    async def post_verify(node_id: str, actor: Actor) -> list[dict[str, Any]]:
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

    @app.delete("/api/nodes/{node_id}/lease")
    async def delete_lease(node_id: str, actor: Actor) -> dict[str, str]:
        with _refusals():
            operations.with_actor(actor).release_lease(node_id)
        return {"id": node_id}

    @app.post("/api/leases/sweep")
    async def post_sweep(actor: Actor) -> dict[str, list[str]]:
        with _refusals():
            swept = operations.with_actor(actor).sweep_leases()
        return {"swept": swept}

    # -- decisions (§3, §5) ------------------------------------------------------------------

    _DECISION_TAB_STATUS = {
        "open": NodeStatus.NOT_STARTED,
        "answered": NodeStatus.COMPLETED,
        "withdrawn": NodeStatus.ABANDONED,
    }

    @app.get("/api/decisions")
    async def list_decisions(status: str | None = None) -> list[dict[str, Any]]:
        decisions = node_repo.list_nodes(kind=NodeKind.DECISION)
        if status is not None:
            wanted = _DECISION_TAB_STATUS.get(status.lower())
            if wanted is None:
                raise HTTPException(400, "status is one of: open, answered, withdrawn")
            decisions = [d for d in decisions if d.status == wanted]
        return [
            {
                "id": d.id,
                "title": d.title,
                "status": d.status.value,
                "priority": d.priority,
                "created_at": d.created_at.isoformat(),
                "waiting_count": len(node_repo.get_blocked_by(d.id)),
                "decision": d.frontmatter.get("decision") or {},
            }
            for d in decisions
        ]

    @app.post("/api/decisions", status_code=201)
    async def create_decision(body: DecisionCreate, actor: Actor) -> dict[str, str]:
        # Operations.add_decision still takes the CLI's "key|Label|description" strings; the
        # web form collects the same three fields structured, so it is rejoined here rather
        # than growing a second option shape inside Operations.
        options = [f"{o.key}|{o.label}|{o.description}" for o in body.options]
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
    async def post_decision_answer(
        decision_id: str, body: DecisionAnswerRequest, actor: Actor
    ) -> dict[str, str]:
        with _refusals():
            operations.with_actor(actor).answer_decision(
                decision_id, body.option, body.text, body.rationale, actor
            )
        return {"id": decision_id}

    @app.post("/api/decisions/{decision_id}/reopen")
    async def post_decision_reopen(decision_id: str, actor: Actor) -> dict[str, str]:
        with _refusals():
            operations.with_actor(actor).reopen_decision(decision_id)
        return {"id": decision_id}

    @app.post("/api/decisions/{decision_id}/withdraw")
    async def post_decision_withdraw(
        decision_id: str, body: DecisionWithdrawRequest, actor: Actor
    ) -> dict[str, str]:
        with _refusals():
            operations.with_actor(actor).withdraw_decision(decision_id, body.reason)
        return {"id": decision_id}

    @app.post("/api/decisions/{decision_id}/blocks")
    async def post_decision_blocks(
        decision_id: str, body: DecisionBlocksUpdate, actor: Actor
    ) -> dict[str, str]:
        with _refusals():
            operations.with_actor(actor).link_decision(
                decision_id, add=body.add, remove=body.remove
            )
        return {"id": decision_id}

    # -- attachments (§4, §5) ----------------------------------------------------------------

    @app.post("/api/nodes/{node_id}/attachments", status_code=201)
    async def post_attachment(node_id: str, body: AttachmentCreate, actor: Actor) -> dict[str, Any]:
        try:
            content = base64.b64decode(body.content_base64, validate=True)
        except ValueError as exc:
            raise HTTPException(400, "content_base64 is not valid base64") from exc
        # Basename only: a filename is never a path, so `../../etc/passwd` cannot escape the
        # temp directory it is written into before Operations.attach copies it by content hash.
        filename = Path(body.filename).name or "attachment"
        with _refusals(), tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir) / filename
            tmp_path.write_bytes(content)
            entry = operations.with_actor(actor).attach(
                node_id, tmp_path, body.caption, body.source
            )
        return entry

    @app.post("/api/nodes/{node_id}/attachments/check")
    async def post_attachment_check(node_id: str, actor: Actor) -> list[dict[str, Any]]:
        with _refusals():
            entries = operations.with_actor(actor).list_attachments(node_id, check=True)
        return entries

    @app.delete("/api/nodes/{node_id}/attachments/{asset}")
    async def delete_attachment(node_id: str, asset: str, actor: Actor) -> dict[str, str]:
        with _refusals():
            operations.with_actor(actor).detach(node_id, asset)
        return {"asset": asset}

    # -- file serving (§4): read-only, no write guard ------------------------------------------

    @app.get("/assets/{name}")
    async def get_asset(name: str) -> FileResponse:
        if not _ASSET_NAME_RE.fullmatch(name):
            raise HTTPException(404, "not found")
        assets_dir = (db_dir / "assets").resolve()
        candidate = (assets_dir / name).resolve()
        if not candidate.is_relative_to(assets_dir) or not candidate.is_file():
            raise HTTPException(404, "not found")
        return FileResponse(candidate)

    @app.get("/api/file")
    async def get_file(path: str) -> FileResponse:
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
        return FileResponse(candidate)

    return app
