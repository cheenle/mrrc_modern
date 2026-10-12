"""FT8 REST surface: platform cookie auth, roles, state and STOP."""
from __future__ import annotations

import os
import tempfile

from fastapi.testclient import TestClient

from ft8.engine.repository import Repository
from ft8.engine.safety import SafetyController
from ft8.engine.sequencer import Sequencer
from ft8.web.auth_shim import PlatformAuthAdapter
from ft8.web.ft8_api import AppState, create_app

TOKEN = "test-token"


class FakeRig:
    async def set_ptt(self, transmit: bool) -> None: ...
    async def get_ptt(self) -> bool:
        return False


class FakePlayer:
    async def play(self, samples):  # pragma: no cover - not exercised here
        raise AssertionError("no TX expected in these tests")

    def cancel(self):  # pragma: no cover - safety stop path
        return None


def _client(*, role: str = "control", authenticated: bool = True) -> TestClient:
    tmp = tempfile.mkdtemp(prefix="ft8-api-")
    repository = Repository(os.path.join(tmp, "qso.db"))
    sequencer = Sequencer(my_call="BG1SB", my_grid="ON80DA")
    safety = SafetyController(FakeRig(), FakePlayer(), sequencer=sequencer)
    state = AppState(
        auth=PlatformAuthAdapter(lambda tok: authenticated and tok == TOKEN,
                                 role_of_token=lambda tok: role),
        lease=None,
        safety=safety,
        sequencer=sequencer,
        repository=repository,
        my_call="BG1SB",
        my_grid="ON80DA",
    )
    client = TestClient(create_app(state))
    if authenticated:
        client.cookies.set("mrrc_auth", TOKEN)
    return client


def test_state_requires_auth():
    r = _client(authenticated=False).get("/api/ft8/state")
    assert r.status_code == 401


def test_state_reports_sequencer_and_station_fields():
    r = _client().get("/api/ft8/state")
    assert r.status_code == 200
    body = r.json()
    assert "state" in body["sequencer"]
    assert body["station"]["my_call"] == "BG1SB"
    assert "safety" in body


def test_health_is_readable():
    assert _client().get("/api/ft8/health").status_code == 200


def test_read_endpoints_are_readable():
    client = _client()
    assert client.get("/api/ft8/dxcc").status_code == 200
    assert client.get("/api/ft8/logs/qsos").status_code == 200
    assert client.get("/api/ft8/settings").status_code == 200


def test_listen_role_cannot_start_tx():
    r = _client(role="listen").post("/api/ft8/operation/cq")
    assert r.status_code == 403


def test_stop_allowed_for_listen_role():
    """Emergency STOP stays available to every authenticated session."""

    r = _client(role="listen").post("/api/ft8/operation/stop")
    assert r.status_code in (200, 409)


def test_control_role_reply_without_selection_is_rejected():
    r = _client().post("/api/ft8/operation/reply", json={"index": 0})
    assert r.status_code in (422, 409)
