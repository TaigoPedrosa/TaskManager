"""`tm web` answers only its own origin: every HTTP route and `/ws` refuse a foreign Host or
Origin, and neither the served page nor its export loads a script from another origin."""

import base64
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.routing import Mount, Route
from starlette.websockets import WebSocketDisconnect

from taskmanager.core.enums import NodeKind
from taskmanager.core.models import Node
from taskmanager.db.connection import DatabaseManager
from taskmanager.db.node_repo import NodeRepository
from taskmanager.web.app import VENDOR_DIR, create_app
from taskmanager.web.static_export import export_static_html

BOUND = "127.0.0.1:6701"
SUBSCRIBE = {"type": "subscribe", "id": 1, "filters": {}, "open": [], "watch": [], "reset": True}
FOREIGN = pytest.mark.parametrize(
    "headers",
    [{"origin": "https://evil.example"}, {"host": "evil.example:6701"}],
    ids=["foreign-origin", "foreign-host"],
)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    db = DatabaseManager(tmp_path / ".taskmanager")
    db.init_all()
    NodeRepository(db).save_node(Node(id="S1", kind=NodeKind.SPEC, title="S1"))
    return tmp_path


@pytest.fixture
def client(root: Path) -> Iterator[TestClient]:
    # Bound the way `tm web` binds it, so Host is pinned; `with` runs the lifespan that builds
    # the hub's first snapshot.
    app = create_app(root, host="127.0.0.1", port=6701)
    with TestClient(app, base_url=f"http://{BOUND}") as test_client:
        yield test_client


def _every_http_request(app: FastAPI) -> list[tuple[str, str]]:
    requests: list[tuple[str, str]] = []
    for route in app.routes:
        if isinstance(route, Route) and route.methods:
            path = re.sub(r"\{[^}]+\}", "x", route.path)
            requests += [(method, path) for method in sorted(route.methods)]
        elif isinstance(route, Mount):
            requests.append(("GET", f"{route.path}/marked.min.js"))
    return requests


def _script_srcs(html: str) -> list[str]:
    return re.findall(r'<script\b[^>]*\bsrc="([^"]*)"', html)


def test_ws_from_the_same_origin_receives_its_snapshot(client: TestClient) -> None:
    same_origin = {"origin": f"http://{BOUND}"}
    with client.websocket_connect(f"ws://{BOUND}/ws", headers=same_origin) as ws:
        ws.send_json(SUBSCRIBE)
        message = ws.receive_json()

    assert message["type"] == "snapshot"
    assert [row["id"] for row in message["rows"]] == ["S1"]


@FOREIGN
def test_ws_from_a_foreign_origin_or_host_is_closed_1008_before_any_frame(
    client: TestClient, headers: dict[str, str]
) -> None:
    # The session raises on the app's first message when that message is a close, and no frame
    # can precede the accept the close replaces, so not one snapshot byte reached the client.
    with (
        pytest.raises(WebSocketDisconnect) as refused,
        client.websocket_connect(f"ws://{BOUND}/ws", headers=dict(headers)) as ws,
    ):
        ws.send_json(SUBSCRIBE)
        ws.receive_json()

    assert refused.value.code == 1008


@FOREIGN
def test_every_http_route_refuses_a_foreign_origin_or_host_with_403(
    client: TestClient, headers: dict[str, str]
) -> None:
    answered = {
        f"{method} {path}": client.request(method, path, headers=headers).status_code
        for method, path in _every_http_request(client.app)
    }

    assert {"GET /", "GET /api/statuses", "POST /api/specs", "GET /vendor/marked.min.js"} <= set(
        answered
    )
    assert answered == dict.fromkeys(answered, 403)


def test_the_same_origin_reads_the_page_its_scripts_and_the_api(client: TestClient) -> None:
    for host in (BOUND, "localhost:6701"):
        headers = {"host": host, "origin": f"http://{host}"}
        for path in ("/", "/graph/S1", "/api/statuses", "/vendor/marked.min.js"):
            assert client.get(path, headers=headers).status_code == 200, (host, path)


def test_the_served_page_loads_every_script_from_this_server(client: TestClient) -> None:
    srcs = _script_srcs(client.get("/").text)

    assert srcs
    for src in srcs:
        assert src.startswith("/vendor/"), src
        served = client.get(src)
        assert served.status_code == 200
        assert served.content == (VENDOR_DIR / src.removeprefix("/vendor/")).read_bytes()


def test_no_api_docs_page_pulls_cdn_scripts_into_this_origin(client: TestClient) -> None:
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404


def test_the_export_embeds_every_script_and_fetches_none(root: Path, tmp_path: Path) -> None:
    html = export_static_html(root, tmp_path / "export.html").read_text(encoding="utf-8")
    embedded = [src.split(",", 1) for src in _script_srcs(html)]

    assert {prefix for prefix, _data in embedded} == {"data:text/javascript;base64"}
    assert sorted(base64.b64decode(data) for _prefix, data in embedded) == sorted(
        path.read_bytes() for path in VENDOR_DIR.glob("*.js")
    )
