"""FastAPI application for the TaskManager interactive web visualizer."""

import asyncio
import base64
import hashlib
import json
import mimetypes
import tempfile
from collections.abc import AsyncGenerator, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

from dishka import make_container
from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from taskmanager.core.enums import NodeKind, TransferMode, VerificationType
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
from taskmanager.di.container import TaskManagerProvider
from taskmanager.engine.assets import ASSET_NAME_RE
from taskmanager.engine.config import ConfigStore, DispatchConfig
from taskmanager.engine.landing import Landing
from taskmanager.engine.operations import OperationError, Operations
from taskmanager.engine.simulate import simulate
from taskmanager.engine.snapshot import DisplayView, SnapshotBuilder, stored_status
from taskmanager.web.bodies import BodyRepos, attachments_with_size, build_bodies
from taskmanager.web.live import LiveHub
from taskmanager.web.rows import build_rows, canonical, decisions_open, statuses, statuses_hash
from taskmanager.web.ui import get_web_html
from taskmanager.web.visibility import parse_filters, visible_ids


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


_RESET_TARGETS = (Status.READY, Status.IMPLEMENTED, Status.REVIEWED, Status.FIXED, Status.COMPLETED)


_MAX_NODES_IDS = 200
_MIN_PAGE_LIMIT = 1
_MAX_PAGE_LIMIT = 200
_DEFAULT_NODES_LIMIT = 50
_DEFAULT_DECISIONS_LIMIT = 50


def _parse_page_limit(raw: str | None, default: int) -> int:
    if raw is None:
        return default
    try:
        limit = int(raw)
    except ValueError as exc:
        raise HTTPException(400, "limit must be an integer") from exc
    if not (_MIN_PAGE_LIMIT <= limit <= _MAX_PAGE_LIMIT):
        raise HTTPException(400, f"limit is {_MIN_PAGE_LIMIT}..{_MAX_PAGE_LIMIT}")
    return limit


_MIN_WAVE_DEPTH = 1
_MAX_WAVE_DEPTH = 20
# Not a config key: a simulated wave never holds a lease, so there is no session to cap.
_WAVE_MAX_STRONG = 5


def _parse_bound(raw: int | None, default: int, lo: int, hi: int, refusal: str) -> int:
    value = default if raw is None else raw
    if not (lo <= value <= hi):
        raise HTTPException(400, refusal)
    return value


def _encode_nodes_cursor(ordinal: int, node_id: str, query_hash: str) -> str:
    payload = json.dumps({"o": ordinal, "i": node_id, "q": query_hash})
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def _decode_nodes_cursor(raw: str) -> tuple[int, str, str]:
    try:
        payload = json.loads(base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8"))
        return int(payload["o"]), str(payload["i"]), str(payload["q"])
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(400, "invalid cursor") from exc


def _node_details(ids: list[str], view: DisplayView, repos: BodyRepos) -> dict[str, dict[str, Any]]:
    """The single place `/api/nodes/{id}` and a bulk page's `include=body` build a node's detail
    from, so the two are equal by construction rather than by two routes staying in sync."""
    bodies = build_bodies(view, ids, repos=repos)
    return {
        node_id: {
            **body,
            "display": body["node"]["display"],
            "phase": body["node"].get("phase"),
            "dependencies": repos.node_repo.get_dependencies(node_id),
            "blocked_by": repos.node_repo.get_blocked_by(node_id),
        }
        for node_id, body in bodies.items()
    }


def paginate_nodes(
    rows: dict[str, dict[str, Any]],
    *,
    view: DisplayView | None,
    repos: BodyRepos | None,
    parent: str | None,
    ids: list[str] | None,
    filters_raw: Mapping[str, str],
    include_body: bool,
    cursor: str | None,
    limit: int,
) -> dict[str, Any]:
    """One page of `ids`, or of `parent`'s direct children (`None`/`"root"` for the roots),
    filtered exactly as `visibility.py` filters the whole tree -- a client walks the tree one
    call per opened container instead of paging it whole. `view`/`repos` are read only when
    `include_body` is set, so a filtering-only caller (a test, `include_body=False`) needs
    neither."""
    try:
        filters = parse_filters(filters_raw)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    if ids is not None:
        candidates = [rows[i] for i in dict.fromkeys(ids) if i in rows]
        candidates.sort(key=lambda r: (r["ordinal"], r["id"]))
    else:
        parent_id = None if parent in (None, "root") else parent
        visible = visible_ids(rows, filters, list(rows.keys()))
        candidates = [rows[i] for i in visible if rows[i]["parent"] == parent_id]

    query_key = {
        "parent": parent,
        "ids": sorted(ids) if ids is not None else None,
        "filters": dict(filters_raw),
    }
    query_hash = hashlib.sha256(canonical(query_key).encode("utf-8")).hexdigest()

    start = 0
    if cursor is not None:
        c_ordinal, c_id, c_hash = _decode_nodes_cursor(cursor)
        if c_hash != query_hash:
            raise HTTPException(400, "cursor is for a different query")
        start = next(
            (
                idx
                for idx, r in enumerate(candidates)
                if (r["ordinal"], r["id"]) > (c_ordinal, c_id)
            ),
            len(candidates),
        )

    page = candidates[start : start + limit]
    items: list[dict[str, Any]]
    if include_body and page:
        if view is None or repos is None:
            raise ValueError("include_body requires view and repos")
        details = _node_details([r["id"] for r in page], view, repos)
        items = [{**r, "body": details.get(r["id"])} for r in page]
    else:
        items = [dict(r) for r in page]

    next_cursor = None
    if start + limit < len(candidates):
        last = page[-1]
        next_cursor = _encode_nodes_cursor(last["ordinal"], last["id"], query_hash)
    return {"items": items, "next": next_cursor}


def _encode_decisions_cursor(created_at: str, node_id: str, query_hash: str) -> str:
    payload = json.dumps({"c": created_at, "i": node_id, "q": query_hash})
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def _decode_decisions_cursor(raw: str) -> tuple[datetime, str, str]:
    try:
        payload = json.loads(base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8"))
        return datetime.fromisoformat(payload["c"]), str(payload["i"]), str(payload["q"])
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(400, "invalid cursor") from exc


def paginate_decisions(
    decisions: list[Node], *, status: str | None, cursor: str | None, limit: int
) -> tuple[list[Node], str | None]:
    """Newest first (`created_at` desc, then `id` desc), keyset-paged the same way as
    `paginate_nodes`."""
    ordered = sorted(decisions, key=lambda d: (d.created_at, d.id), reverse=True)
    query_hash = hashlib.sha256(canonical({"status": status}).encode("utf-8")).hexdigest()

    start = 0
    if cursor is not None:
        c_created, c_id, c_hash = _decode_decisions_cursor(cursor)
        if c_hash != query_hash:
            raise HTTPException(400, "cursor is for a different query")
        start = next(
            (idx for idx, d in enumerate(ordered) if (d.created_at, d.id) < (c_created, c_id)),
            len(ordered),
        )

    page = ordered[start : start + limit]
    next_cursor = None
    if start + limit < len(ordered):
        last = page[-1]
        next_cursor = _encode_decisions_cursor(last.created_at.isoformat(), last.id, query_hash)
    return page, next_cursor


def _decision_item(
    node: Node, view: DisplayView, node_repo: NodeRepository, assets_dir: Path
) -> dict[str, Any]:
    def blocks() -> list[dict[str, Any]]:
        rows = []
        for blocked_id in node_repo.get_blocked_by(node.id):
            blocked = node_repo.get_node(blocked_id)
            if blocked is not None:
                rows.append(
                    {
                        "id": blocked.id,
                        "title": blocked.title,
                        "kind": blocked.kind.value,
                        "display": view.display(blocked),
                    }
                )
        return rows

    return {
        "id": node.id,
        "title": node.title,
        "status": stored_status(node).value,
        "priority": node.priority,
        "created_at": node.created_at.isoformat(),
        "waiting_count": len(node_repo.get_blocked_by(node.id)),
        "blocks": blocks(),
        "decision": node.frontmatter.get("decision") or {},
        "attachments": attachments_with_size(assets_dir, node.frontmatter.get("attachments") or []),
    }


def create_app(project_root: Path, host: str = "127.0.0.1", port: int | None = None) -> FastAPI:
    # `port=None` (tests, the static exporter's in-process TestClient) skips Host pinning and
    # keeps the old Origin-must-equal-Host check; the real server always passes its bound port,
    # so it is the only caller `_bound_hosts` needs to protect (see `_write_guard`'s docstring).
    allowed_hosts = _bound_hosts(host, port) if port is not None else None
    Actor = Annotated[str, Depends(_write_guard(allowed_hosts))]
    db_dir = project_root / ".taskmanager"
    assets_dir = db_dir / "assets"
    container = make_container(TaskManagerProvider(project_root))
    db_mgr = container.get(DatabaseManager)
    node_repo = container.get(NodeRepository)
    operations = container.get(Operations).with_actor("web")
    snapshots = container.get(SnapshotBuilder)
    # A verb or a lease release may start or stop a landing, so the claims carry the engine.
    claims = Landing.open(project_root).claims
    jobs = container.get(JobRepository)
    cache = container.get(CacheRepository)

    def _condition_ttl() -> int:
        return ConfigStore(project_root).project().condition_ttl

    def _dispatch_config() -> DispatchConfig:
        return ConfigStore(project_root).project().dispatch

    def _repo_order() -> list[str]:
        return ConfigStore(project_root).project().repo_order

    live_hub = LiveHub(
        snapshots=snapshots,
        cache=cache,
        node_repo=node_repo,
        job_repo=jobs,
        assets_dir=assets_dir,
        condition_ttl=_condition_ttl,
        state_db=db_mgr.state_db,
        cache_db=db_mgr.cache_db,
    )

    async def live_loop() -> None:
        while True:
            try:
                await asyncio.sleep(0.25)
                await live_hub.refresh()
            except asyncio.CancelledError:
                break

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        await live_hub.refresh()
        watcher_task = asyncio.create_task(live_loop())
        try:
            yield
        finally:
            watcher_task.cancel()
            try:
                await watcher_task
            except asyncio.CancelledError:
                pass

    app = FastAPI(title="TaskManager Visualizer", lifespan=lifespan)

    def new_view() -> DisplayView:
        """One snapshot per request, so every display in one response reads the same tree."""
        return DisplayView(snapshots, cache, _condition_ttl())

    def _body_repos() -> BodyRepos:
        return BodyRepos(
            node_repo=node_repo,
            job_repo=jobs,
            cache=cache,
            condition_ttl=_condition_ttl(),
            assets_dir=assets_dir,
        )

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return get_web_html()

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await websocket.accept()
        session = live_hub.open_session(websocket)
        try:
            while True:
                text = await websocket.receive_text()
                await live_hub.handle_frame(session, text)
        except WebSocketDisconnect:
            pass
        finally:
            live_hub.close_session(session)

    @app.get("/api/nodes/{node_id}")
    def get_node_detail(node_id: str) -> dict[str, Any]:
        view = new_view()
        detail = _node_details([node_id], view, _body_repos()).get(node_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="Node not found")
        return detail

    @app.get("/api/statuses")
    def get_statuses() -> dict[str, Any]:
        view = new_view()
        rows = build_rows(view)
        entries = statuses(rows)
        return {
            "statuses": entries,
            "hash": statuses_hash(entries),
            "decisions_open": decisions_open(view),
        }

    @app.get("/api/nodes")
    def get_nodes_page(request: Request) -> dict[str, Any]:
        query = dict(request.query_params)
        parent = query.pop("parent", None)
        ids_raw = query.pop("ids", None)
        include = query.pop("include", None)
        cursor = query.pop("cursor", None)
        limit = _parse_page_limit(query.pop("limit", None), _DEFAULT_NODES_LIMIT)
        if parent is not None and ids_raw is not None:
            raise HTTPException(400, "parent and ids are mutually exclusive")
        id_list: list[str] | None = None
        if ids_raw is not None:
            id_list = [i for i in ids_raw.split(",") if i]
            if len(id_list) > _MAX_NODES_IDS:
                raise HTTPException(400, f"at most {_MAX_NODES_IDS} ids")
        view = new_view()
        return paginate_nodes(
            build_rows(view),
            view=view,
            repos=_body_repos(),
            parent=parent,
            ids=id_list,
            filters_raw=query,
            include_body=include == "body",
            cursor=cursor,
            limit=limit,
        )

    _DECISION_TAB_STATUS = {
        "open": DecisionStatus.OPEN,
        "answered": DecisionStatus.ANSWERED,
        "withdrawn": DecisionStatus.WITHDRAWN,
    }

    @app.get("/api/meta")
    def get_meta() -> dict[str, Any]:
        all_nodes = node_repo.list_nodes()
        dispatch = _dispatch_config()
        return {
            "dispatch": {"wave_size": dispatch.wave_size, "tick_budget": dispatch.tick_budget},
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

    @app.get("/api/waves")
    def get_waves(
        depth: int = 1,
        size: int | None = None,
        spec: Annotated[list[str] | None, Query()] = None,
    ) -> dict[str, Any]:
        dispatch = _dispatch_config()
        depth = _parse_bound(
            depth,
            1,
            _MIN_WAVE_DEPTH,
            _MAX_WAVE_DEPTH,
            f"depth is {_MIN_WAVE_DEPTH}..{_MAX_WAVE_DEPTH}",
        )
        size = _parse_bound(
            size,
            dispatch.wave_size,
            1,
            dispatch.tick_budget,
            f"wave size must be 1–{dispatch.tick_budget} (this project's dispatch.tick_budget)",
        )
        # One bulk read of state.db, however deep: every later wave replays over the snapshot
        # this built, in memory (see `engine.simulate`). Conditions are read from the cache once
        # here too, the same read a display uses -- a simulated wave has no real claim to run a
        # condition's command under.
        snap = snapshots.build()
        cached_conditions = cache.all_conditions(_condition_ttl())
        waves = simulate(
            snap,
            depth,
            size,
            _WAVE_MAX_STRONG,
            spec,
            repo_order=_repo_order(),
            cached_conditions=cached_conditions,
        )
        return {"waves": [asdict(w) for w in waves]}

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
    def list_decisions(
        status: str | None = None, cursor: str | None = None, limit: str | None = None
    ) -> dict[str, Any]:
        page_limit = _parse_page_limit(limit, _DEFAULT_DECISIONS_LIMIT)
        decisions = node_repo.list_nodes(kind=NodeKind.DECISION)
        if status is not None:
            wanted = _DECISION_TAB_STATUS.get(status.lower())
            if wanted is None:
                raise HTTPException(400, "status is one of: open, answered, withdrawn")
            decisions = [d for d in decisions if stored_status(d) == wanted]
        page, next_cursor = paginate_decisions(
            decisions, status=status, cursor=cursor, limit=page_limit
        )
        view = new_view()
        return {
            "items": [_decision_item(d, view, node_repo, assets_dir) for d in page],
            "next": next_cursor,
        }

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
