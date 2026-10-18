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


def test_early_decode_is_wired_into_the_runtime() -> None:
    """The early-decode seam is active: shaved wait + ring padding agree.

    1.2 s before the slot ends the orchestrator stops waiting, and the slot
    source serves a slot with at least (15 - 1.2) s landed, zero-padded to
    the full frame.  Guard > 1.16 s keeps large-dt signals inside the real
    waveform region.
    """

    from ft8 import runtime as runtime_mod

    assert runtime_mod.EARLY_DECODE_S == 1.2
    min_samples = int(
        (runtime_mod.FT8_SLOT_SECONDS - runtime_mod.EARLY_DECODE_S)
        * runtime_mod.DECODER_SAMPLE_RATE
    )
    assert min_samples == 165_600          # 13.8 s of 12 kHz audio
    # The padded region must stay clear of the FT8 waveform (12.64 s).
    assert runtime_mod.FT8_SLOT_SECONDS - runtime_mod.EARLY_DECODE_S > 12.64 + 1.0


def test_decodes_frame_carries_completion_timing() -> None:
    """The UI can show real latency: completion instant + lag past slot end."""

    from types import SimpleNamespace

    from ft8 import runtime as runtime_mod

    captured: list[dict] = []
    rt = object.__new__(runtime_mod.Ft8Runtime)
    rt.sequencer = SimpleNamespace(my_call="", my_grid="")
    rt._dial_freq_hz = 14_074_000
    rt._worked_index = lambda: None
    rt._dumped_slots = 99                      # no slot dumping in this test
    rt.recent_decodes = runtime_mod.deque(maxlen=50)
    rt._on_decode = captured.append
    rt.ring = None
    rt._slots_logged = 0                       # __init__ never ran; _on_batch counts deliveries

    slot_id = 119457247
    slot_end = (slot_id + 1) * 15.0
    decode = SimpleNamespace(
        slot_id=slot_id,
        late=False,
        dispatched_epoch=slot_end - 1.2,
        finished_epoch=slot_end + 1.0,
        batch=SimpleNamespace(results=()),
        messages=(),
    )
    rt._on_batch(decode)

    frame = captured[0]
    assert frame["timing"]["slot_start_epoch"] == slot_id * 15.0
    assert frame["timing"]["slot_end_epoch"] == slot_end
    assert frame["timing"]["finished_epoch"] == slot_end + 1.0
    assert frame["timing"]["finished_after_slot_end_s"] == 1.0
    assert frame["timing"]["dispatched_after_slot_end_s"] == -1.2


def test_on_batch_clears_the_one_shot_deep_decode_flag() -> None:
    """「深度解码本槽」必须真的是一次性的。

    Mutation: delete ``self._deep_decode_requested = False`` from ``_on_batch``
    and this goes red.  Without it a single press escalates *every* later slot
    (~2.2 s of deep decode each) — the "press once, permanently slower"
    outcome the brief calls the worst this task can ship.
    """

    from types import SimpleNamespace

    from ft8 import runtime as runtime_mod

    rt = object.__new__(runtime_mod.Ft8Runtime)
    rt.sequencer = SimpleNamespace(my_call="", my_grid="")
    rt._dial_freq_hz = 14_074_000
    rt._worked_index = lambda: None
    rt._dumped_slots = 99
    rt.recent_decodes = runtime_mod.deque(maxlen=50)
    rt._on_decode = lambda frame: None
    rt.ring = None
    rt._slots_logged = 0                       # __init__ never ran; _on_batch counts deliveries
    rt._deep_decode_requested = True           # a press armed the deep path

    rt._on_batch(SimpleNamespace(
        slot_id=1, late=False, dispatched_epoch=None, finished_epoch=None,
        batch=SimpleNamespace(results=()), messages=(),
    ))

    assert rt._deep_decode_requested is False


def test_the_cascade_guard_shares_the_orchestrators_decision_cutoff() -> None:
    """预算守卫的截止值必须**来自** orchestrator 的那个常量，不是另写一遍 2.5。

    两个数字今天相等，所以这条用例盯的**不是值、是来源**：改了
    ``DEFAULT_DECISION_CUTOFF_SECONDS``，守卫必须跟着动。`CascadingDecoder`
    已经把 ``cutoff`` 设成必填（模块内没有默认值），所以唯一还能出错的地方
    就是接线处又把 ``2.5`` 写了一遍 —— 这条就钉在那里。

    为什么用源码断言而不是构造整个 `Ft8Runtime`：为了钉住一个关键字实参而把
    backend / CAT / 音频全部搭起来，代价与收益不成比例；而「这个模块引进了
    那个常量、并且在调用里用了它」是这两个断言真正要保证的东西。
    与 `test_early_decode_is_wired_into_the_runtime` 同一风格（直接断言模块常量）。
    """
    from pathlib import Path

    from ft8 import runtime as runtime_mod
    from ft8.engine.orchestrator import DEFAULT_DECISION_CUTOFF_SECONDS

    # 1) 来源是 orchestrator 的那一个，不是 runtime 自己又定义的一个同值常量。
    assert runtime_mod.DEFAULT_DECISION_CUTOFF_SECONDS is DEFAULT_DECISION_CUTOFF_SECONDS

    # 2) 它真的被用在了构造调用里（不是引进来放着）。
    source = Path(runtime_mod.__file__).read_text(encoding="utf-8")
    assert "cutoff=DEFAULT_DECISION_CUTOFF_SECONDS" in source
