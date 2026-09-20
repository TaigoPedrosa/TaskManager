"""FastAPI application for the TaskManager interactive web visualizer."""

import asyncio
import sqlite3
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from taskmanager.core.enums import NodeKind, NodeStatus, RenderView, VirtualStatus
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

        def node_to_dict(n: Any) -> dict[str, Any]:
            v_status = n.status.value
            if n.kind == NodeKind.TASK:
                v_status = graph_engine.resolve_task_state(n.id).value
            elif n.kind == NodeKind.PLAN:
                v_status = graph_engine.resolve_plan_status(n.id).value

            sections = node_repo.get_all_sections(n.id)
            verifications = node_repo.get_verifications(n.id) if n.kind == NodeKind.TASK else []
            lease = runtime_repo.get_lease(n.id) if n.kind == NodeKind.TASK else None

            return {
                "id": n.id,
                "kind": n.kind.value,
                "title": n.title,
                "status": n.status.value,
                "virtual_status": v_status,
                "priority": n.priority,
                "ordinal": n.ordinal,
                "target_repo": n.target_repo,
                "acceptable_models": n.acceptable_models,
                "frontmatter": n.frontmatter,
                "dependencies": node_repo.get_dependencies(n.id),
                "blocked_by": node_repo.get_blocked_by(n.id),
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

        if specs:
            for s in specs:
                s_dict = node_to_dict(s)
                plan_children_ids = node_repo.get_children(s.id)
                plan_children: list[dict[str, Any]] = []
                for pid in plan_children_ids:
                    pnode = node_repo.get_node(pid)
                    if pnode and pnode.kind == NodeKind.PLAN:
                        p_dict = node_to_dict(pnode)
                        task_children_ids = node_repo.get_children(pnode.id)
                        task_children: list[dict[str, Any]] = []
                        for cid in task_children_ids:
                            cnode = node_repo.get_node(cid)
                            if cnode:
                                task_children.append(node_to_dict(cnode))
                        p_dict["children"] = task_children
                        plan_children.append(p_dict)
                s_dict["children"] = plan_children
                tree.append(s_dict)
        else:
            # Standalone plans without specs
            for p in plans:
                p_dict = node_to_dict(p)
                task_children_ids = node_repo.get_children(p.id)
                task_children = []
                for cid in task_children_ids:
                    cnode = node_repo.get_node(cid)
                    if cnode:
                        task_children.append(node_to_dict(cnode))
                p_dict["children"] = task_children
                tree.append(p_dict)

        # Add any orphan tasks
        parented_ids: set[str] = set()
        for s in specs:
            parented_ids.update(node_repo.get_children(s.id))
        for p in plans:
            parented_ids.update(node_repo.get_children(p.id))

        for t in tasks:
            if t.id not in parented_ids:
                tree.append(node_to_dict(t))

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
                    "ordinal": n.ordinal,
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
            "virtual_status": v_status,
            "rendered_markdown": renderer.render(node_id, view=RenderView.FULL),
            "dependencies": dependencies,
            "blocked_by": blocked_by,
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
            state = graph_engine.resolve_task_state(t.id)
            val = state.value if hasattr(state, "value") else str(state)
            if val in stats:
                stats[val] += 1
            else:
                stats[val] = 1

        stats["ready"] = stats[VirtualStatus.READY.value]
        stats["in_flight"] = (
            stats[VirtualStatus.IN_FLIGHT.value] + stats[NodeStatus.IMPLEMENTING.value]
        )
        stats["waiting_review"] = (
            stats[NodeStatus.WAITING_REVIEW.value] + stats[NodeStatus.REVIEWING.value]
        )
        stats["completed"] = stats[NodeStatus.COMPLETED.value]
        stats["blocked"] = stats[VirtualStatus.BLOCKED.value]

        return stats

    return app
