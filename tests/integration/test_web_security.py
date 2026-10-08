"""`tm web` answers only its own origin: every HTTP route and `/ws` refuse a foreign Host or
Origin, neither the served page nor its export loads a script from another origin, and it binds
a non-loopback host only when told to with `--expose`."""

import base64
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.routing import Mount, Route
from starlette.websockets import WebSocketDisconnect
from typer.testing import CliRunner

from taskmanager.cli.main import app as cli
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


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.append({"app": app, **kwargs}))
    return calls


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


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.20", "myhost.lan"])
def test_web_on_a_non_loopback_host_without_expose_exits_1_before_serving(
    root: Path, served: list[dict[str, Any]], host: str
) -> None:
    res = CliRunner().invoke(cli, ["web", "--host", host, "--no-open", "-C", str(root)])

    assert res.exit_code == 1
    message = " ".join(res.output.split())
    assert f"--host {host} serves the estate with no authentication" in message
    assert "Pass --expose to serve it anyway." in message
    assert served == []


@pytest.mark.parametrize(
    ("host", "flags"),
    [("127.0.0.1", []), ("localhost", []), ("0.0.0.0", ["--expose"])],
    ids=["loopback", "localhost", "exposed"],
)
def test_web_on_loopback_or_with_expose_serves_on_that_host(
    root: Path, served: list[dict[str, Any]], host: str, flags: list[str]
) -> None:
    argv = ["web", "--host", host, *flags, "--no-open", "-C", str(root)]
    res = CliRunner().invoke(cli, argv)

    assert res.exit_code == 0, res.output
    assert [call["host"] for call in served] == [host]


@pytest.mark.parametrize(
    ("bind", "flags", "reached_as"),
    [
        ("0.0.0.0", ["--expose"], "192.168.1.20"),
        ("::", ["--expose"], "[fe80::1]"),
        ("::", ["--expose"], "localhost"),
        ("::1", [], "[::1]"),
    ],
    ids=[
        "wildcard-by-lan-address",
        "ipv6-wildcard-by-address",
        "ipv6-wildcard-by-localhost",
        "ipv6-loopback",
    ],
)
def test_web_serves_a_client_that_reaches_its_bind_by_an_address(
    root: Path, served: list[dict[str, Any]], bind: str, flags: list[str], reached_as: str
) -> None:
    res = CliRunner().invoke(cli, ["web", "--host", bind, *flags, "--no-open", "-C", str(root)])
    assert res.exit_code == 0, res.output
    (call,) = served
    netloc = f"{reached_as}:{call['port']}"
    # Starlette's test transport cannot parse a bracketed address in the URL, so the address
    # travels in the Host header the way a browser sends it.
    same_origin = {"host": netloc, "origin": f"http://{netloc}"}

    with TestClient(call["app"]) as client:
        assert client.get("/", headers=same_origin).status_code == 200
        assert client.get("/api/statuses", headers=same_origin).status_code == 200
        with client.websocket_connect("/ws", headers=same_origin) as ws:
            ws.send_json(SUBSCRIBE)
            assert ws.receive_json()["type"] == "snapshot"


@pytest.mark.parametrize(
    ("bind", "host", "status"),
    [
        ("0.0.0.0", "rebind.evil.example:6701", 403),
        ("0.0.0.0", "192.168.1.20:6702", 403),
        ("0.0.0.0", "[192.168.1.20:6701", 403),
        ("MyHost.lan", "myhost.lan:6701", 200),
        ("MyHost.lan", "otherhost.lan:6701", 403),
    ],
    ids=["foreign-name", "other-port", "malformed", "bound-name", "other-name"],
)
def test_the_host_pin_admits_an_address_or_the_bound_name_on_the_bound_port(
    root: Path, bind: str, host: str, status: int
) -> None:
    client = TestClient(create_app(root, host=bind, port=6701))

    assert client.get("/api/statuses", headers={"host": host}).status_code == status


def test_web_prints_an_ipv6_bind_as_a_bracketed_url(
    root: Path, served: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A unique-local address starts with a letter, so unescaped brackets would read as markup.
    monkeypatch.setattr("taskmanager.cli.main._find_available_port", lambda _host, port: port)
    argv = ["web", "--host", "fd12::5", "--expose", "--no-open", "-C", str(root)]
    res = CliRunner().invoke(cli, argv)

    assert res.exit_code == 0, res.output
    assert "http://[fd12::5]:6701" in res.output
    assert "ws://[fd12::5]:6701/ws" in res.output
