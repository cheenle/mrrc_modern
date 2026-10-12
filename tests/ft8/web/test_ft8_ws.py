"""FT8 WebSocket: platform handshake auth and the hello frame."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from ft8.web.ft8_ws import Ft8EventBus, Ft8WsState, create_ft8_ws_router

TOKEN = "ws-token"


def _app(*, authenticated: bool = True) -> FastAPI:
    app = FastAPI()
    state = Ft8WsState(
        bus=Ft8EventBus(),
        token_from_handshake=lambda ws: ws.cookies.get("mrrc_auth") or "",
        auth_ok=lambda tok: authenticated and tok == TOKEN,
        mode=lambda: "ssb",
        recent_decodes=lambda: [{"text": "CQ BG1SB ON80", "snr": -5}],
    )
    app.include_router(create_ft8_ws_router(state))
    return app


def test_ws_requires_auth():
    client = TestClient(_app(authenticated=False))
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/WSft8") as ws:
            ws.receive_json()
    assert exc.value.code == 4401


def test_ws_hello_reports_mode_and_recent_decodes():
    client = TestClient(_app())
    client.cookies.set("mrrc_auth", TOKEN)
    with client.websocket_connect("/WSft8") as ws:
        hello = ws.receive_json()
    assert hello["type"] == "hello"
    assert hello["mode"] == "ssb"
    assert hello["decodes"][0]["text"] == "CQ BG1SB ON80"


def test_bus_fanout_reaches_subscribers():
    bus = Ft8EventBus()
    q1, q2 = bus.subscribe(), bus.subscribe()
    bus.publish({"type": "mode", "mode": "ft8"})
    assert q1.get_nowait()["mode"] == "ft8"
    assert q2.get_nowait()["mode"] == "ft8"


def test_bus_drops_oldest_when_full():
    bus = Ft8EventBus(capacity=2)
    q = bus.subscribe()
    for i in range(4):
        bus.publish({"n": i})
    assert [q.get_nowait()["n"], q.get_nowait()["n"]] == [2, 3]
