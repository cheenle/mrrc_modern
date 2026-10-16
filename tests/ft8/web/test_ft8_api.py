"""FT8 REST surface: platform cookie auth, roles, state and STOP."""
from __future__ import annotations

import os
import tempfile
from types import SimpleNamespace

from fastapi.testclient import TestClient

from ft8.engine.cascade import DEFAULT_MIN_PEAK_SNR
from ft8.engine.latency import LatencyHistogram
from ft8.engine.repository import Repository
from ft8.engine.safety import SafetyController
from ft8.engine.sequencer import Sequencer
from ft8.web.auth_shim import PlatformAuthAdapter
from ft8.platform.settings import Ft8SettingsStore
from ft8.web.ft8_api import AppState, _engine_stats, _health, create_app

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


class _Runtime:
    """Minimal Ft8Runtime stand-in for the one-shot deep-slot endpoint."""

    def __init__(self) -> None:
        self._deep_decode_requested = False
        self.policy_pushes: list = []   # set_cascade_policy arguments, in order

    def set_cascade_policy(self, settings) -> None:
        self.policy_pushes.append(settings)


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


def test_station_cascade_policy_round_trip():
    client = _client()
    assert client.get("/api/ft8/station").json()["min_peak_snr"] == DEFAULT_MIN_PEAK_SNR

    r = client.put("/api/ft8/station", json={"min_peak_snr": -18,
                                             "escalate_on_weak": False})
    assert r.status_code == 200
    # PUT 的返回体也必须带上 —— 少了它前端拿返回值刷新界面就会回弹
    assert r.json()["min_peak_snr"] == -18

    after = client.get("/api/ft8/station").json()
    assert after["min_peak_snr"] == -18
    assert after["escalate_on_weak"] is False


def test_station_rejects_out_of_range_cascade_policy():
    assert _client().put("/api/ft8/station",
                         json={"min_peak_snr": 5}).status_code == 422
    assert _client().put("/api/ft8/station",
                         json={"escalate_on_weak": "yes"}).status_code == 422


def test_deep_slot_sets_the_one_shot_flag():
    """「深度解码本槽」是一次性事件：端点置位，运行时的 _on_batch 负责清位。"""

    client = _client()
    runtime = _Runtime()
    client.app.state.ft8_state.runtime = runtime
    r = client.post("/api/ft8/engine/deep-slot")
    assert r.status_code == 200
    assert runtime._deep_decode_requested is True


def test_deep_slot_requires_control_role():
    assert _client(role="listener").post(
        "/api/ft8/engine/deep-slot").status_code == 403


def test_station_update_pushes_the_policy_to_the_live_runtime():
    """Saving the station must push the new policy onto the running cascade.

    Mutation: drop ``runtime.set_cascade_policy(updated)`` from ``station_update``
    and this goes red.  Without it a changed threshold applies only at the next
    engine start — the operator sees the saved value but nothing changes now.
    """

    client = _client()
    runtime = _Runtime()
    client.app.state.ft8_state.runtime = runtime

    r = client.put("/api/ft8/station", json={"min_peak_snr": -18})
    assert r.status_code == 200
    assert len(runtime.policy_pushes) == 1
    assert runtime.policy_pushes[0].min_peak_snr == -18


def _counters(**overrides):
    """Stand-in for ``OrchestratorCounters``, seeded from the real dataclass.

    Deriving the field list instead of hand-writing it is the point: a new
    counter (``ap_decodes``, ``ap_low_quality``) used to break every test here
    with an AttributeError, because the double silently went stale.  Now the
    double can't miss a field the real class has.
    """

    import dataclasses

    from ft8.engine.orchestrator import OrchestratorCounters

    base = {field.name: 0 for field in dataclasses.fields(OrchestratorCounters)}
    base.update(
        slots_started=100,
        slots_skipped=3,
        decodes=97,
        fast_decodes=90,
        escalations=7,
        deadline_misses=1,
        decode_errors=0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_engine_stats_reports_the_escalation_rate() -> None:
    """升级率 = escalations / decodes —— 判断截止修好没修好的唯一读数。"""

    state = SimpleNamespace(
        orchestrator=SimpleNamespace(counters=_counters()),
        runtime=None,
        latency=None,
    )
    engine = _engine_stats(state)["engine"]
    assert engine["escalations"] == 7
    assert engine["fast_decodes"] == 90
    assert engine["decodes"] == 97
    assert engine["slots_started"] == 100
    assert engine["slots_skipped"] == 3
    assert engine["deadline_misses"] == 1
    assert engine["decode_errors"] == 0
    # 7/97 —— 不做四舍五入到整数，否则一次 0.4% 的升级率会显示成 0。
    assert round(engine["escalation_rate"], 6) == round(7 / 97, 6)


def test_engine_stats_escalation_rate_is_zero_when_nothing_decoded() -> None:
    """decodes == 0 时不能除零 —— 引擎刚起来就是这个状态。"""

    state = SimpleNamespace(
        orchestrator=SimpleNamespace(counters=_counters(decodes=0, fast_decodes=0, escalations=0)),
        runtime=None,
        latency=None,
    )
    assert _engine_stats(state)["engine"]["escalation_rate"] == 0.0


def test_engine_stats_degrades_when_the_engine_is_not_wired() -> None:
    """引擎未启动 → 合法的空载荷，不是 500（也没有 AttributeError）。"""

    state = SimpleNamespace(orchestrator=None, runtime=None, latency=None)
    payload = _engine_stats(state)
    assert payload["engine"] is None
    assert "decode_latency" not in payload


def test_engine_stats_finds_the_orchestrator_through_the_runtime() -> None:
    """与 `_health` 同一套解析：state.orchestrator 优先，否则走 state.runtime。"""

    state = SimpleNamespace(
        orchestrator=None,
        runtime=SimpleNamespace(orchestrator=SimpleNamespace(counters=_counters(escalations=1))),
        latency=None,
    )
    assert _engine_stats(state)["engine"]["escalations"] == 1


def test_engine_stats_includes_the_latency_histogram_when_wired() -> None:
    histogram = LatencyHistogram()
    histogram.record("fast", 3, 3, 0.15)
    state = SimpleNamespace(
        orchestrator=SimpleNamespace(counters=_counters()),
        runtime=None,
        latency=histogram,
    )
    assert _engine_stats(state)["decode_latency"]["fast/p3/t3"]["count"] == 1


def test_engine_stats_finds_the_histogram_through_the_runtime() -> None:
    """真实的组装根只传 `runtime=`，从不传 `latency=`。

    `server.py` 建 AppState 时给的是 `runtime=_ft8_runtime`，而这里只从
    `state.latency` 取直方图 —— 于是解码器一直在填（`runtime.py:194` 把它接进
    decoder）、两个端点从来不读，NFR-002 的直方图在生产里等于不存在。解析方式
    与上面 orchestrator 那一处同形：显式接线优先，否则走 runtime。
    """

    histogram = LatencyHistogram()
    histogram.record("deep", 3, 3, 2.4)
    state = SimpleNamespace(
        orchestrator=SimpleNamespace(counters=_counters()),
        runtime=SimpleNamespace(orchestrator=None, latency=histogram),
        latency=None,
    )
    assert _engine_stats(state)["decode_latency"]["deep/p3/t3"]["count"] == 1


def test_health_finds_the_histogram_through_the_runtime() -> None:
    """同一个缺口也在 `_health` 上，用同一个解析补上。"""

    histogram = LatencyHistogram()
    histogram.record("deep", 3, 3, 2.4)
    state = SimpleNamespace(
        safety=SimpleNamespace(health={}),
        lease=None,
        auth=SimpleNamespace(session_count=lambda: 0),
        orchestrator=None,
        runtime=SimpleNamespace(orchestrator=None, latency=histogram),
        latency=None,
    )
    assert _health(state)["decode_latency"]["deep/p3/t3"]["count"] == 1


def test_engine_stats_route_requires_auth() -> None:
    """新路由与其它 `/api/ft8` 路由同一道门 —— 无 cookie → 401。"""

    assert _client(authenticated=False).get("/api/ft8/engine/stats").status_code == 401


def test_engine_stats_route_is_readable() -> None:
    """路由真的挂上了，且未起引擎时是一个合法的空载荷（不是 500）。"""

    r = _client().get("/api/ft8/engine/stats")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert "engine" in r.json()


def test_engine_stats_exposes_the_ap_counters() -> None:
    """AP 产出要能在状态栏读到，否则打开 MRRC_FT8_AP_A_PRIORI 之后无从判断它有没有起作用。

    计数器只写不读等于没写（设计笔记 §7.1 判据 3 的前提）。
    """

    state = SimpleNamespace(
        orchestrator=SimpleNamespace(counters=_counters(ap_decodes=12, ap_low_quality=3)),
        runtime=None,
        latency=None,
    )
    engine = _engine_stats(state)["engine"]
    assert engine["ap_decodes"] == 12
    assert engine["ap_low_quality"] == 3


def test_engine_stats_ap_counters_default_to_zero() -> None:
    """默认关闭 AP 的机器上，状态栏这两个数必须一直是 0。"""

    state = SimpleNamespace(
        orchestrator=SimpleNamespace(counters=_counters()),
        runtime=None,
        latency=None,
    )
    engine = _engine_stats(state)["engine"]
    assert engine["ap_decodes"] == 0
    assert engine["ap_low_quality"] == 0
