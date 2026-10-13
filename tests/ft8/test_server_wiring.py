"""server.py wiring: the shared TX gate and the work-mode state field."""
from __future__ import annotations

import asyncio
import types

import server as server_mod


class FakeCat:
    def __init__(self):
        self.ptt = 0
        self.connected = True

    async def set_ptt(self, tx):
        self.ptt = 1 if tx else 0
        return True


class FakeAudio:
    def __init__(self):
        self.tx_started = False

    def start_tx(self):
        self.tx_started = True
        return True

    def stop_tx(self, graceful=False):
        self.tx_started = False

    def tx_stats(self):
        return {"written": 0, "write_err": 0, "queue_drops": 0}


class FakeRadio:
    def __init__(self):
        self.tx_status = 0

    def update(self, **kw):
        self.tx_status = kw.get("tx_status", self.tx_status)


def _patch(monkeypatch, *, gated: bool):
    cat, audio, radio = FakeCat(), FakeAudio(), FakeRadio()
    monkeypatch.setattr(server_mod, "cat", cat)
    monkeypatch.setattr(server_mod, "audio", audio)
    monkeypatch.setattr(server_mod, "radio", radio)
    monkeypatch.setattr(server_mod, "backend", types.SimpleNamespace(
        capabilities=types.SimpleNamespace(tx_gated=gated)))
    monkeypatch.setattr(server_mod, "_claim_tx_owner_for_token", lambda tok: None)
    monkeypatch.setattr(server_mod, "scheduler", types.SimpleNamespace(
        skip_next_poll=lambda name, secs: None))
    return cat, audio, radio


def test_tx_session_keys_and_unkeys_once(monkeypatch):
    cat, audio, _ = _patch(monkeypatch, gated=False)
    session = server_mod.TxSession()
    assert asyncio.run(session.key()) is True
    assert cat.ptt == 1 and audio.tx_started and session.keyed
    asyncio.run(session.unkey(graceful=False))
    assert cat.ptt == 0 and not audio.tx_started and not session.keyed


def test_tx_session_refuses_when_backend_gated(monkeypatch):
    cat, _, _ = _patch(monkeypatch, gated=True)
    assert asyncio.run(server_mod.TxSession().key()) is False
    assert cat.ptt == 0


def test_cq_wrappers_share_the_session(monkeypatch):
    cat, audio, _ = _patch(monkeypatch, gated=False)
    assert asyncio.run(server_mod._cq_key(None)) is True
    assert server_mod._tx_session.keyed and cat.ptt == 1
    asyncio.run(server_mod._cq_unkey(False))
    assert not server_mod._tx_session.keyed and cat.ptt == 0


def test_work_mode_reports_ssb_when_engine_unbound(monkeypatch):
    monkeypatch.setattr(server_mod, "_ft8_mode", None)
    assert server_mod._work_mode_value() == "ssb"


def test_work_mode_reports_ft8_when_controller_says_so(monkeypatch):
    class _FakeMode:
        def __str__(self):
            return "ft8"

    monkeypatch.setattr(server_mod, "_ft8_mode", types.SimpleNamespace(mode=_FakeMode()))
    assert server_mod._work_mode_value() == "ft8"


def test_uplink_audio_is_dropped_only_while_ft8_is_keyed(monkeypatch):
    """The gate is transmission-scoped: an idle always-on engine must not
    silence the operator's microphone."""
    fed: list[bytes] = []
    monkeypatch.setattr(server_mod, "audio", types.SimpleNamespace(
        feed_tx_audio=lambda pcm: fed.append(pcm)))
    monkeypatch.setattr(server_mod, "_ft8_transmitting", lambda: True)
    asyncio.run(server_mod._feed_tx_from_uplink(b"\x00\x00"))
    assert fed == []                       # FT8 mid-waveform: dropped
    monkeypatch.setattr(server_mod, "_ft8_transmitting", lambda: False)
    asyncio.run(server_mod._feed_tx_from_uplink(b"\x01\x01"))
    assert fed == [b"\x01\x01"]           # idle FT8: the mic goes through
