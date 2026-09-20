"""FastAPI application for the TaskManager interactive web visualizer."""

import asyncio
import sqlite3
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from taskmanager.core.enums import NodeKind, NodeStatus, VirtualStatus
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.node_repo import NodeRepository
from taskmanager.db.runtime_repo import RuntimeRepository
from taskmanager.engine.graph import GraphEngine
from taskmanager.renderers.markdown import MarkdownRenderer
from taskmanager.web.ui import get_web_html


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
    graph_engine = GraphEngine(node_repo, runtime_repo)
    renderer = MarkdownRenderer(node_repo)
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

        tree: list[dict[str, Any]] = []

        def build_task_dict(task_node: Any) -> dict[str, Any]:
            v_status = graph_engine.resolve_task_state(task_node.id)
            return {
                "id": task_node.id,
                "kind": task_node.kind.value,
                "title": task_node.title,
                "status": task_node.status.value,
                "virtual_status": v_status.value,
                "priority": task_node.priority,
                "acceptable_models": task_node.acceptable_models,
                "children": [],
            }

        def build_plan_dict(plan_node: Any) -> dict[str, Any]:
            child_ids = node_repo.get_children(plan_node.id)
            plan_status = graph_engine.resolve_plan_status(plan_node.id)
            children: list[dict[str, Any]] = []
            for cid in child_ids:
                cnode = node_repo.get_node(cid)
                if cnode and cnode.kind in (NodeKind.TASK, NodeKind.REVIEW_GATE):
                    children.append(build_task_dict(cnode))
            return {
                "id": plan_node.id,
                "kind": plan_node.kind.value,
                "title": plan_node.title,
                "status": plan_node.status.value,
                "virtual_status": plan_status.value,
                "priority": plan_node.priority,
                "children": children,
            }

        if specs:
            for s in specs:
                plan_children_ids = node_repo.get_children(s.id)
                plan_children: list[dict[str, Any]] = []
                for pid in plan_children_ids:
                    pnode = node_repo.get_node(pid)
                    if pnode and pnode.kind == NodeKind.PLAN:
                        plan_children.append(build_plan_dict(pnode))
                tree.append(
                    {
                        "id": s.id,
                        "kind": s.kind.value,
                        "title": s.title,
                        "status": s.status.value,
                        "virtual_status": s.status.value,
                        "priority": s.priority,
                        "children": plan_children,
                    }
                )
        else:
            # Standalone plans without specs
            for p in plans:
                tree.append(build_plan_dict(p))

        # Add any orphan tasks
        parented_ids = set()
        for s in specs:
            parented_ids.update(node_repo.get_children(s.id))
        for p in plans:
            parented_ids.update(node_repo.get_children(p.id))

        for t in tasks:
            if t.id not in parented_ids:
                tree.append(build_task_dict(t))

        return tree

    @app.get("/api/graph")
    async def get_graph() -> dict[str, Any]:
        all_nodes = node_repo.list_nodes()
        nodes_out: list[dict[str, Any]] = []
        for n in all_nodes:
            v_status = n.status.value
            if n.kind == NodeKind.TASK:
                v_status = graph_engine.resolve_task_state(n.id).value
            elif n.kind == NodeKind.PLAN:
                v_status = graph_engine.resolve_plan_status(n.id).value

            nodes_out.append(
                {
                    "id": n.id,
                    "title": n.title,
                    "kind": n.kind.value,
                    "status": v_status,
                    "priority": n.priority,
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

        v_status = node.status.value
        if node.kind == NodeKind.TASK:
            v_status = graph_engine.resolve_task_state(node.id).value
        elif node.kind == NodeKind.PLAN:
            v_status = graph_engine.resolve_plan_status(node.id).value

        sections = node_repo.get_all_sections(node_id)
        verifications = node_repo.get_verifications(node_id)
        lease = runtime_repo.get_lease(node_id)
        rendered = renderer.render(node_id, view="full")

        return {
            "node": {
                "id": node.id,
                "kind": node.kind.value,
                "title": node.title,
                "status": node.status.value,
                "priority": node.priority,
                "target_repo": node.target_repo,
                "acceptable_models": node.acceptable_models,
                "frontmatter": node.frontmatter,
            },
            "virtual_status": v_status,
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
            "rendered_markdown": rendered,
        }

    @app.get("/api/stats")
    async def get_stats() -> dict[str, int]:
        all_tasks = node_repo.list_nodes(kind=NodeKind.TASK)
        ready_count = 0
        inflight_count = 0
        review_count = 0
        completed_count = 0

        for t in all_tasks:
            state = graph_engine.resolve_task_state(t.id)
            if state == VirtualStatus.READY:
                ready_count += 1
            elif state == VirtualStatus.IN_FLIGHT or state == NodeStatus.IMPLEMENTING:
                inflight_count += 1
            elif state in (NodeStatus.WAITING_REVIEW, NodeStatus.REVIEWING):
                review_count += 1
            elif state in (NodeStatus.COMPLETED, NodeStatus.SUPERSEDED):
                completed_count += 1

        return {
            "total": len(all_tasks),
            "ready": ready_count,
            "in_flight": inflight_count,
            "waiting_review": review_count,
            "completed": completed_count,
        }

    return app
