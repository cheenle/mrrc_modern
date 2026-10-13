"""FT8 REST surface: platform cookie auth, roles, state and STOP."""
from __future__ import annotations

import os
import tempfile

from fastapi.testclient import TestClient

from ft8.engine.repository import Repository
from ft8.engine.safety import SafetyController
from ft8.engine.sequencer import Sequencer
from ft8.web.auth_shim import PlatformAuthAdapter
from ft8.platform.settings import Ft8SettingsStore
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


def _client(*, role: str = "operator", authenticated: bool = True) -> TestClient:
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
        settings_store=Ft8SettingsStore(os.path.join(tmp, "settings.json")),
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
    r = _client(role="listener").post("/api/ft8/operation/cq")
    assert r.status_code == 403


def test_stop_allowed_for_listen_role():
    """Emergency STOP stays available to every authenticated session."""

    r = _client(role="listener").post("/api/ft8/operation/stop")
    assert r.status_code in (200, 409)


def test_control_role_reply_without_selection_is_rejected():
    r = _client().post("/api/ft8/operation/reply", json={"index": 0})
    assert r.status_code in (422, 409)


class FakeMode:
    def __init__(self):
        self.value = "ssb"
        self.switch_calls: list[str] = []
        self.fail = False

    @property
    def mode(self):
        return self.value

    async def switch_to_ft8(self):
        if self.fail:
            raise RuntimeError("engine boom")
        self.value = "ft8"
        self.switch_calls.append("ft8")

    async def switch_to_ssb(self):
        self.value = "ssb"
        self.switch_calls.append("ssb")


def _mode_client(mode: FakeMode, role: str = "operator") -> TestClient:
    client = _client(role=role)
    client.app.state.ft8_state.mode_controller = mode
    return client


def test_mode_switch_round_trip():
    mode = FakeMode()
    client = _mode_client(mode)
    assert client.post("/api/ft8/mode", json={"mode": "ft8"}).json()["mode"] == "ft8"
    assert client.post("/api/ft8/mode", json={"mode": "ssb"}).json()["mode"] == "ssb"
    assert mode.switch_calls == ["ft8", "ssb"]


def test_mode_switch_bad_value_is_422():
    assert _mode_client(FakeMode()).post("/api/ft8/mode", json={"mode": "cw"}).status_code == 422


def test_mode_switch_engine_failure_is_500():
    mode = FakeMode()
    mode.fail = True
    r = _mode_client(mode).post("/api/ft8/mode", json={"mode": "ft8"})
    assert r.status_code == 500


def test_mode_switch_requires_control_role():
    assert _mode_client(FakeMode(), role="listen").post(
        "/api/ft8/mode", json={"mode": "ft8"}).status_code == 403


def test_station_round_trip(tmp_path):
    client = _client()
    before = client.get("/api/ft8/station").json()
    assert before["rig_mode"] == "DATA-U"
    saved = client.put("/api/ft8/station", json={
        "my_call": "BA4EG", "my_grid": "PM01", "rig_mode": "USB",
        "tx_audio_frequency": 1200,
    })
    assert saved.status_code == 200
    after = client.get("/api/ft8/station").json()
    assert after["my_call"] == "BA4EG" and after["rig_mode"] == "USB"
    assert after["tx_audio_frequency"] == 1200


def test_station_rejects_unknown_keys():
    r = _client().put("/api/ft8/station", json={"nope": 1})
    assert r.status_code == 422


def test_station_rejects_bad_values():
    assert _client().put("/api/ft8/station", json={"rig_mode": "FM"}).status_code == 422
    assert _client().put("/api/ft8/station", json={"tx_audio_frequency": 9999}).status_code == 422


def test_station_requires_control_role():
    assert _client(role="listener").put("/api/ft8/station", json={"my_call": "X"}).status_code == 403


def test_station_always_on_round_trip():
    client = _client()
    assert client.get("/api/ft8/station").json()["engine_always_on"] is True
    r = client.put("/api/ft8/station", json={"engine_always_on": False})
    assert r.status_code == 200 and r.json()["engine_always_on"] is False
    assert client.get("/api/ft8/station").json()["engine_always_on"] is False


def test_station_always_on_must_be_boolean():
    assert _client().put("/api/ft8/station", json={"engine_always_on": "yes"}).status_code == 422
