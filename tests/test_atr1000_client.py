"""
Tests for ATR1000Client — frame codec, LearningBuffer, relay throttle,
and the METER-stream learning flow. No hardware, no real sockets.
"""
import asyncio
import struct
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from atr1000_client import (
    ATR1000Client,
    LearningBuffer,
    SCMD_FLAG,
    SCMD_SYNC,
    SCMD_TUNE_MODE,
    SCMD_RELAY_STATUS,
    RELAY_MIN_INTERVAL,
    SWR_RETUNE_COOLDOWN,
    SWR_RETUNE_DEBOUNCE,
    SWR_RETUNE_MAX_FAILS,
    build_sync_frame,
    build_set_relay_frame,
    build_tune_frame,
)


def make_meter_frame(swr_raw: int, power: int) -> bytes:
    """FF 02 07 00 SWR_L SWR_H P_L P_H"""
    return bytes([SCMD_FLAG, 2, 7, 0]) + struct.pack('<H', swr_raw) + struct.pack('<H', power)


def make_relay_frame(sw: int, ind: int, cap: int, other: int = 213,
                     ind_uh_raw: int = None, cap_pf_raw: int = None) -> bytes:
    """Empirical RELAY layout: FF 05 LEN sw ind cap other [<H L*100> <H C>]"""
    payload = bytes([sw, ind, cap, other])
    if ind_uh_raw is not None:
        payload += struct.pack('<H', ind_uh_raw) + struct.pack('<H', cap_pf_raw)
    return bytes([SCMD_FLAG, SCMD_RELAY_STATUS, len(payload)]) + payload


def make_tune_frame(tuning: int) -> bytes:
    return bytes([SCMD_FLAG, 3, 1, tuning])


class FrameEncodeTests(unittest.TestCase):
    """Outbound frame byte sequences must match the reference exactly."""

    def test_sync_frame(self):
        self.assertEqual(build_sync_frame(), bytes([0xFF, 0x01, 0x00]))

    def test_set_relay_frame(self):
        self.assertEqual(build_set_relay_frame(1, 47, 79),
                         bytes([0xFF, 0x05, 0x03, 1, 47, 79]))

    def test_set_relay_frame_lc(self):
        self.assertEqual(build_set_relay_frame(0, 0, 0),
                         bytes([0xFF, 0x05, 0x03, 0, 0, 0]))

    def test_tune_frame_modes(self):
        for mode in range(4):
            self.assertEqual(build_tune_frame(mode),
                             bytes([0xFF, 0x04, 0x01, mode]))

    def test_tune_frame_default_full(self):
        self.assertEqual(build_tune_frame(2), bytes([0xFF, 0x04, 0x01, 0x02]))


class FrameParseTests(unittest.TestCase):
    """Inbound frame parsing: _parse_frame + decode helpers (pure, no socket)."""

    def test_parse_frame_splits_cmd_and_payload(self):
        cmd, payload = ATR1000Client._parse_frame(make_meter_frame(150, 50))
        self.assertEqual(cmd, 2)
        self.assertEqual(payload, bytes([0]) + struct.pack('<H', 150) + struct.pack('<H', 50))

    def test_parse_frame_rejects_bad_flag(self):
        with self.assertRaises(ValueError):
            ATR1000Client._parse_frame(bytes([0x00, 0x02, 0x00]))

    def test_parse_frame_rejects_short(self):
        with self.assertRaises(ValueError):
            ATR1000Client._parse_frame(bytes([0xFF, 0x02]))

    def test_meter_swr_scaled(self):
        # raw 150 → 1.5
        self.assertEqual(ATR1000Client._decode_meter(bytes([0, 150, 0, 50, 0])), (1.5, 50))

    def test_meter_swr_integer(self):
        # raw 3 → 3.0 (integer SWR)
        swr, power = ATR1000Client._decode_meter(bytes([0, 3, 0, 30, 0]))
        self.assertEqual(swr, 3.0)
        self.assertEqual(power, 30)

    def test_meter_swr_zero_is_perfect_match(self):
        # raw 0 → 1.0
        swr, _ = ATR1000Client._decode_meter(bytes([0, 0, 0, 10, 0]))
        self.assertEqual(swr, 1.0)

    def test_meter_power_little_endian(self):
        _, power = ATR1000Client._decode_meter(bytes([0, 120, 0]) + struct.pack('<H', 513))
        self.assertEqual(power, 513)

    def test_meter_too_short(self):
        self.assertIsNone(ATR1000Client._decode_meter(bytes([0, 150, 0, 50])))

    def test_relay_basic(self):
        # 7-byte frame: no µH/pF fields
        _, payload = ATR1000Client._parse_frame(make_relay_frame(1, 47, 79))
        decoded = ATR1000Client._decode_relay(payload)
        self.assertEqual(decoded["sw"], 1)
        self.assertEqual(decoded["ind"], 47)
        self.assertEqual(decoded["cap"], 79)
        self.assertIsNone(decoded["ind_uh"])
        self.assertIsNone(decoded["cap_pf"])

    def test_relay_with_uh_pf(self):
        # 11-byte frame: ind 470 → 4.70 µH, cap raw 790 → 790 pF
        _, payload = ATR1000Client._parse_frame(
            make_relay_frame(0, 47, 79, ind_uh_raw=470, cap_pf_raw=790))
        self.assertEqual(len(payload), 8)
        decoded = ATR1000Client._decode_relay(payload)
        self.assertEqual(decoded["sw"], 0)
        self.assertEqual(decoded["ind"], 47)
        self.assertEqual(decoded["cap"], 79)
        self.assertAlmostEqual(decoded["ind_uh"], 4.70)
        self.assertEqual(decoded["cap_pf"], 790)

    def test_relay_too_short(self):
        self.assertIsNone(ATR1000Client._decode_relay(bytes([1, 47, 79])))

    def test_tune_status(self):
        _, payload = ATR1000Client._parse_frame(make_tune_frame(1))
        self.assertTrue(ATR1000Client._decode_tune(payload))
        _, payload = ATR1000Client._parse_frame(make_tune_frame(0))
        self.assertFalse(ATR1000Client._decode_tune(payload))

    def test_tune_status_too_short(self):
        self.assertIsNone(ATR1000Client._decode_tune(b""))


class LearningBufferTests(unittest.TestCase):
    """Stable-window learning: resets, thresholds, median."""

    def setUp(self):
        self.buf = LearningBuffer()
        self.buf.set_relay(1, 10, 20)
        self.buf.set_freq(14_200_000)

    def _feed(self, n, power=50.0, swr=1.2, sw=1, ind=10, cap=20):
        results = [self.buf.add_sample(power, swr, sw, ind, cap) for _ in range(n)]
        return results[-1]

    def test_learns_after_four_stable_samples(self):
        for i in range(3):
            should, _ = self.buf.add_sample(50.0, 1.2, 1, 10, 20)
            self.assertFalse(should, f"sample {i + 1} should not learn yet")
        should, median = self.buf.add_sample(50.0, 1.2, 1, 10, 20)
        self.assertTrue(should)
        self.assertAlmostEqual(median, 1.2)

    def test_rejects_low_power(self):
        should, _ = self._feed(6, power=4.9)
        self.assertFalse(should)

    def test_accepts_min_power_boundary(self):
        should, _ = self._feed(4, power=5.0)
        self.assertTrue(should)

    def test_rejects_swr_above_max(self):
        should, _ = self._feed(6, swr=1.81)
        self.assertFalse(should)

    def test_rejects_swr_below_min(self):
        should, _ = self._feed(6, swr=0.99)
        self.assertFalse(should)

    def test_rejects_unstable_spread(self):
        # spread 0.20 > 0.08
        for swr in (1.1, 1.3, 1.1, 1.3):
            should, _ = self.buf.add_sample(50.0, swr, 1, 10, 20)
        self.assertFalse(should)

    def test_median_swr_used(self):
        for swr in (1.20, 1.22, 1.18, 1.25):
            should, median = self.buf.add_sample(50.0, swr, 1, 10, 20)
        self.assertTrue(should)
        # even window: mean of the two middle values (1.20, 1.22)
        self.assertAlmostEqual(median, 1.21)

    def test_rejects_mismatched_relay_params(self):
        should, _ = self._feed(6, sw=0, ind=10, cap=20)
        self.assertFalse(should)

    def test_reset_on_relay_change(self):
        self._feed(3)
        self.buf.set_relay(0, 5, 5)   # changed → window cleared
        self.buf.set_relay(1, 10, 20)  # back to original → cleared again
        should, _ = self.buf.add_sample(50.0, 1.2, 1, 10, 20)
        self.assertFalse(should)  # only 1 sample in the fresh window

    def test_no_reset_on_same_relay(self):
        self._feed(3)
        self.buf.set_relay(1, 10, 20)  # unchanged → window kept
        should, _ = self.buf.add_sample(50.0, 1.2, 1, 10, 20)
        self.assertTrue(should)

    def test_reset_on_freq_change_over_1khz(self):
        self._feed(3)
        self.buf.set_freq(14_202_000)  # >1kHz → cleared
        should, _ = self.buf.add_sample(50.0, 1.2, 1, 10, 20)
        self.assertFalse(should)

    def test_no_reset_on_small_freq_change(self):
        self._feed(3)
        self.buf.set_freq(14_200_500)  # 500Hz → kept
        should, _ = self.buf.add_sample(50.0, 1.2, 1, 10, 20)
        self.assertTrue(should)

    def test_reset_on_tx_toggle(self):
        self._feed(3)
        self.buf.reset()  # TX start/stop
        should, _ = self.buf.add_sample(50.0, 1.2, 1, 10, 20)
        self.assertFalse(should)
        # relay/freq state preserved across reset
        self.assertEqual(self.buf.current_relay, (1, 10, 20))
        self.assertEqual(self.buf.current_freq, 14_200_000)


class RelayThrottleTests(unittest.IsolatedAsyncioTestCase):
    """set_relay: immediate on param change, 5s min interval for identical params."""

    def setUp(self):
        self.client = ATR1000Client("127.0.0.1", 1234)
        self.client._send = AsyncMock()

    async def test_first_send_immediate(self):
        sent = await self.client.set_relay(1, 10, 20)
        self.assertTrue(sent)
        self.client._send.assert_awaited_once_with(build_set_relay_frame(1, 10, 20))

    async def test_same_params_within_5s_suppressed(self):
        await self.client.set_relay(1, 10, 20)
        sent = await self.client.set_relay(1, 10, 20)
        self.assertFalse(sent)
        self.assertEqual(self.client._send.await_count, 1)

    async def test_changed_params_immediate(self):
        await self.client.set_relay(1, 10, 20)
        sent = await self.client.set_relay(0, 10, 20)  # changed
        self.assertTrue(sent)
        self.assertEqual(self.client._send.await_count, 2)

    async def test_same_params_after_5s_sent(self):
        await self.client.set_relay(1, 10, 20)
        self.client._last_relay_sent -= RELAY_MIN_INTERVAL + 0.1
        sent = await self.client.set_relay(1, 10, 20)
        self.assertTrue(sent)
        self.assertEqual(self.client._send.await_count, 2)

    async def test_send_updates_learning_buffer_and_timestamp(self):
        await self.client.set_relay(1, 10, 20)
        self.assertEqual(self.client._learning.current_relay, (1, 10, 20))
        self.assertGreater(self.client._relay_changed_at, 0)


class FakeStorage:
    """Minimal stand-in for atr1000_tuner.TunerStorage."""

    def __init__(self):
        self.learned = []
        self.params = None

    def learn(self, freq, sw, ind, cap, swr, force_update=False):
        self.learned.append((freq, sw, ind, cap, swr, force_update))
        return True

    def find_best(self, freq):
        return None

    def get_tune_params(self, freq):
        return self.params


class MeterLearningFlowTests(unittest.TestCase):
    """Learning runs on the METER stream while tx is True."""

    def setUp(self):
        self.storage = FakeStorage()
        self.client = ATR1000Client("127.0.0.1", 1234, storage=self.storage)
        self.client.notify_freq(14_200_000)
        # Relay frame primes state + learning buffer
        self.client._handle_frame(make_relay_frame(1, 10, 20))
        self.client.notify_tx(True)
        # Skip the 1.0s ignore windows
        past = time.monotonic() - 5.0
        self.client._tx_started_at = past
        self.client._relay_changed_at = past

    def test_learns_after_stable_meter_stream(self):
        for _ in range(4):
            self.client._handle_frame(make_meter_frame(120, 50))  # SWR 1.20, 50W
        self.assertEqual(len(self.storage.learned), 1)
        freq, sw, ind, cap, swr, force = self.storage.learned[0]
        self.assertEqual((freq, sw, ind, cap), (14_200_000, 1, 10, 20))
        self.assertAlmostEqual(swr, 1.20)
        self.assertFalse(force)

    def test_no_learning_when_not_tx(self):
        self.client.notify_tx(False)
        for _ in range(6):
            self.client._handle_frame(make_meter_frame(120, 50))
        self.assertEqual(len(self.storage.learned), 0)

    def test_no_learning_during_ignore_window(self):
        self.client._tx_started_at = time.monotonic()  # inside 1.0s window
        for _ in range(6):
            self.client._handle_frame(make_meter_frame(120, 50))
        self.assertEqual(len(self.storage.learned), 0)

    def test_dedup_skips_worse_swr(self):
        for _ in range(4):
            self.client._handle_frame(make_meter_frame(120, 50))  # learns 1.20
        self.assertEqual(len(self.storage.learned), 1)
        self.client._learning.samples = []
        for _ in range(4):
            self.client._handle_frame(make_meter_frame(140, 50))  # worse: 1.40
        self.assertEqual(len(self.storage.learned), 1)

    def test_dedup_cooldown_on_unchanged(self):
        for _ in range(4):
            self.client._handle_frame(make_meter_frame(120, 50))
        self.client._learning.samples = []
        for _ in range(4):
            self.client._handle_frame(make_meter_frame(120, 50))  # same SWR, <5s
        self.assertEqual(len(self.storage.learned), 1)

    def test_dedup_immediate_on_improvement(self):
        for _ in range(4):
            self.client._handle_frame(make_meter_frame(140, 50))  # learns 1.40
        self.client._learning.samples = []
        for _ in range(4):
            self.client._handle_frame(make_meter_frame(120, 50))  # better: 1.20
        self.assertEqual(len(self.storage.learned), 2)


class TuningHeuristicTests(unittest.TestCase):
    """Tuning-clear heuristics: same-relay confirm, TX stop."""

    def setUp(self):
        self.client = ATR1000Client("127.0.0.1", 1234)

    def test_tune_status_frame_sets_tuning(self):
        self.client._handle_frame(make_tune_frame(1))
        self.assertTrue(self.client.read_state()["tuning"])
        self.client._handle_frame(make_tune_frame(0))
        self.assertFalse(self.client.read_state()["tuning"])

    def test_same_relay_confirm_clears_tuning(self):
        self.client._handle_frame(make_tune_frame(1))
        # Two identical RELAY frames, second one >1.5s after stability started
        self.client._handle_frame(make_relay_frame(1, 10, 20))
        self.client._tuning_relay_stable_since = time.monotonic() - 2.0
        self.client._handle_frame(make_relay_frame(1, 10, 20))
        self.assertFalse(self.client.read_state()["tuning"])

    def test_meter_clears_tuning_when_relay_stable_over_5s(self):
        self.client._handle_frame(make_tune_frame(1))
        self.client._handle_frame(make_relay_frame(1, 10, 20))
        self.client._tuning_relay_stable_since = time.monotonic() - 6.0
        self.client._handle_frame(make_meter_frame(120, 50))
        self.assertFalse(self.client.read_state()["tuning"])

    def test_tx_stop_clears_tuning(self):
        self.client._handle_frame(make_tune_frame(1))
        self.client.notify_tx(True)
        self.assertTrue(self.client.read_state()["tuning"])
        self.client.notify_tx(False)
        self.assertFalse(self.client.read_state()["tuning"])

    def test_tx_toggles_reset_learning_window(self):
        self.client._learning.samples = [(50.0, 1.2)] * 3
        self.client.notify_tx(True)
        self.assertEqual(self.client._learning.samples, [])


class StateCallbackTests(unittest.TestCase):
    """read_state shape and on_change dispatch."""

    def test_read_state_keys(self):
        client = ATR1000Client("127.0.0.1", 1234)
        state = client.read_state()
        self.assertEqual(set(state), {
            "connected", "power", "swr", "sw", "ind", "cap",
            "ind_uh", "cap_pf", "tuning", "tx", "freq", "last_update",
        })
        self.assertFalse(state["connected"])
        self.assertEqual(state["swr"], 1.0)

    def test_on_change_called_on_meter(self):
        client = ATR1000Client("127.0.0.1", 1234)
        calls = []
        client.on_change = calls.append
        client._handle_frame(make_meter_frame(150, 42))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["power"], 42)
        self.assertAlmostEqual(calls[0]["swr"], 1.5)

    def test_on_change_exception_contained(self):
        client = ATR1000Client("127.0.0.1", 1234)
        client.on_change = lambda state: 1 / 0
        client._handle_frame(make_meter_frame(150, 42))  # must not raise


class SwrRetuneGuardTests(unittest.TestCase):
    """High-SWR auto-retune guard (sibling mrrc V5.8.0 parity)."""

    def setUp(self):
        self.client = ATR1000Client("127.0.0.1", 1234, storage=FakeStorage())
        self.client.notify_freq(7_074_000)
        self.client._handle_frame(make_relay_frame(1, 10, 20))
        self.client._relay_changed_at = time.monotonic() - 5.0

    def _meter(self, swr_raw, power):
        self.client._handle_frame(make_meter_frame(swr_raw, power))

    def _aged_run(self):
        """Prime a run at the current frequency, then age it past the debounce."""
        self._meter(240, 50)                       # starts the run (sets the freq)
        self.client._swr_high_since = time.monotonic() - SWR_RETUNE_DEBOUNCE - 0.1

    def _fire_run(self):
        """A run primed at the current frequency and older than the debounce."""
        self._aged_run()
        self._meter(240, 50)                       # fires on this frame

    def test_debounce_blocks_until_the_run_is_long_enough(self):
        self._meter(240, 50)                       # starts a run
        self.assertIsNone(self.client._pending_tune_mode)
        self.client._swr_high_since = time.monotonic() - SWR_RETUNE_DEBOUNCE - 0.1
        self._meter(240, 50)
        self.assertEqual(self.client._pending_tune_mode, 2)
        self.assertIsNotNone(self.client._auto_tune)
        self.assertAlmostEqual(self.client._auto_tune["swr_before"], 2.4)

    def test_low_power_never_fires(self):
        self.client._swr_high_since = time.monotonic() - 10.0
        self._meter(240, 2)                        # idle leakage / tuner scan
        self.assertIsNone(self.client._pending_tune_mode)
        self.assertEqual(self.client._swr_high_since, 0.0)

    def test_no_fire_while_the_tuner_is_busy(self):
        self.client._tuning = True
        self.client._swr_high_since = time.monotonic() - 10.0
        self._meter(240, 50)
        self.assertIsNone(self.client._pending_tune_mode)
        self.assertEqual(self.client._swr_high_since, 0.0)

    def test_frequency_change_restarts_the_run(self):
        self._meter(240, 50)                       # run starts at 7.074 MHz
        self.client._swr_high_since = time.monotonic() - 10.0
        self.client.notify_freq(14_100_000)        # QSY
        self._meter(240, 50)
        self.assertIsNone(self.client._pending_tune_mode)   # run restarted

    def test_relay_change_restarts_the_run(self):
        self._meter(240, 50)                       # run starts
        self.client._swr_high_since = time.monotonic() - 10.0
        self.client._relay_changed_at = time.monotonic()
        self._meter(240, 50)
        self.assertIsNone(self.client._pending_tune_mode)

    def test_cooldown_blocks_the_next_run(self):
        self._fire_run()
        self.client._auto_tune = None              # ignore the pending compare
        self.client._pending_tune_mode = None
        self._aged_run()
        self._meter(240, 50)                       # inside the 30 s cooldown
        self.assertIsNone(self.client._pending_tune_mode)
        self.client._last_retune_at = time.monotonic() - SWR_RETUNE_COOLDOWN - 1
        self._aged_run()
        self._meter(240, 50)
        self.assertEqual(self.client._pending_tune_mode, 2)

    def test_start_event_and_failure_count(self):
        events = []
        self.client.on_tune_event = events.append
        self._fire_run()
        self.assertEqual(events[0]["phase"], "auto_start")
        self.assertEqual(events[0]["attempt"], 1)
        self.assertEqual(events[0]["freq"], 7_074_000)
        self.assertAlmostEqual(events[0]["swr_before"], 2.4)
        self.assertEqual(self.client._retune_fail_count[7074], 1)

    def test_recovery_clears_the_failure_count(self):
        for _ in range(SWR_RETUNE_MAX_FAILS):
            self._fire_run()
            self.client._auto_tune = None
            self.client._pending_tune_mode = None
            self.client._last_retune_at = time.monotonic() - SWR_RETUNE_COOLDOWN - 1
        self.assertEqual(self.client._retune_fail_count[7074], SWR_RETUNE_MAX_FAILS)
        self._meter(120, 50)                       # SWR 1.20 — matched again
        self.assertNotIn(7074, self.client._retune_fail_count)

    def test_give_up_after_three_failures_announces_once(self):
        events = []
        self.client.on_tune_event = events.append
        for _ in range(SWR_RETUNE_MAX_FAILS):
            self._fire_run()
            self.client._auto_tune = None
            self.client._pending_tune_mode = None
            self.client._last_retune_at = time.monotonic() - SWR_RETUNE_COOLDOWN - 1
        self.assertEqual(self.client._swr_high_since, 0.0)
        events.clear()
        self._meter(240, 50)                       # first frame of the next run
        self.assertIsNone(self.client._pending_tune_mode)   # no 4th tune
        self.assertEqual([e["phase"] for e in events], ["auto_giveup"])
        self._meter(240, 50)                       # announce once per run only
        self.assertEqual([e["phase"] for e in events], ["auto_giveup"])

    def test_failure_count_is_per_frequency(self):
        self._fire_run()
        self.client._auto_tune = None
        self.client._pending_tune_mode = None
        self.client._last_retune_at = time.monotonic() - SWR_RETUNE_COOLDOWN - 1
        self.client.notify_freq(14_100_000)        # QSY keeps 7074's count
        self.assertEqual(self.client._retune_fail_count[7074], 1)
        self._aged_run()                           # a QSY restarts the run
        self._meter(240, 50)
        self.assertEqual(self.client._pending_tune_mode, 2)   # new freq fires
        self.assertEqual(self.client._retune_fail_count[14100], 1)
        self.assertEqual(self.client._retune_fail_count[7074], 1)

    def test_tune_event_callback_exception_is_contained(self):
        self.client.on_tune_event = lambda event: 1 / 0
        self._fire_run()                           # must not raise
        self.assertEqual(self.client._pending_tune_mode, 2)


class AutoTuneCompletionTests(unittest.TestCase):
    """Queued-frame flush + post-tune comparison (write back, never roll back)."""

    def setUp(self):
        self.storage = FakeStorage()
        self.client = ATR1000Client("127.0.0.1", 1234, storage=self.storage)
        self.client.notify_freq(7_074_000)
        self.client._handle_frame(make_relay_frame(1, 10, 20))
        self.client._relay_changed_at = time.monotonic() - 5.0
        self.events = []
        self.client.on_tune_event = self.events.append

    def _flush(self):
        """Worker step with the comparison deadline reached."""
        if self.client._auto_tune_compare_at:
            self.client._auto_tune_compare_at = time.monotonic() - 0.001
        asyncio.run(self.client._flush_auto_tune(time.monotonic()))

    def _pending(self, swr_before=2.4):
        """An auto tune that fired and waits for its tuning-clear (compare_at
        is armed by the clear path, exactly like production)."""
        self.client._auto_tune = {
            "freq": 7_074_000, "swr_before": swr_before,
            "relays": (1, 10, 20), "count": 1, "reason": "",
        }
        self.client._auto_tune_compare_at = 0.0

    def test_worker_flushes_a_queued_tune_frame(self):
        self.client._ws = AsyncMock()
        self.client._pending_tune_mode = 2
        asyncio.run(self.client._flush_auto_tune(time.monotonic()))
        self.assertEqual(self.client._ws.send.await_count, 1)
        self.assertEqual(self.client._pending_tune_mode, None)
        self.assertTrue(self.client._tuning)

    def test_improved_swr_writes_back_with_force_update(self):
        self._pending()
        # The tuner landed on better relays and the SWR improved.
        self.client._handle_frame(make_relay_frame(1, 40, 60))
        self.client._handle_frame(make_meter_frame(135, 50))   # SWR 1.35
        self.client._clear_tuning("relay stable >5s")          # tuning finished
        self._flush()
        self.assertEqual(self.storage.learned,
                         [(7_074_000, 1, 40, 60, 1.35, True)])
        self.assertEqual(self.events[-1]["phase"], "auto_success")
        self.assertIsNone(self.client._auto_tune)

    def test_no_improvement_writes_nothing_and_keeps_the_relays(self):
        self._pending()
        self.client._handle_frame(make_relay_frame(1, 40, 60))
        self.client._handle_frame(make_meter_frame(240, 50))   # still 2.40
        self.client._clear_tuning("relay stable >5s")
        self._flush()
        self.assertEqual(self.storage.learned, [])
        self.assertEqual(self.events[-1]["phase"], "auto_no_improve")
        self.assertEqual((self.client._sw, self.client._ind, self.client._cap),
                         (1, 40, 60))          # what the tuner chose stays

    def test_improvement_outside_the_learn_gate_is_not_stored(self):
        self._pending(swr_before=3.4)
        self.client._handle_frame(make_relay_frame(1, 40, 60))
        self.client._handle_frame(make_meter_frame(200, 50))   # SWR 2.00 > 1.8
        self.client._clear_tuning("relay stable >5s")
        self._flush()
        self.assertEqual(self.storage.learned, [])
        self.assertEqual(self.events[-1]["phase"], "auto_no_improve")

    def test_hard_timeout_is_reported_as_timeout(self):
        self._pending()
        self.client._tuning = True
        self.client._tuning_started_at = time.monotonic()
        self.client._clear_tuning("45s hard timeout")
        self.client._handle_frame(make_meter_frame(240, 50))
        self._flush()
        self.assertEqual(self.events[-1]["phase"], "auto_timeout")

    def test_tune_status_zero_arms_the_comparison(self):
        self._pending()
        self.client._handle_frame(make_tune_frame(0))          # device says done
        self.assertEqual(self.client._auto_tune["reason"], "tune status 0")
        self.client._handle_frame(make_meter_frame(120, 50))   # SWR 1.20
        self._flush()
        self.assertEqual(self.events[-1]["phase"], "auto_success")

    def test_disconnect_aborts_a_pending_comparison(self):
        self._pending()
        self.client._clear_tuning("relay stable >5s")
        self.client._abort_auto_tune("tuner disconnected")
        self.assertIsNone(self.client._auto_tune)
        self.assertEqual(self.client._auto_tune_compare_at, 0.0)
        self.assertEqual(self.events[-1]["phase"], "auto_aborted")
        self.assertEqual(self.events[-1]["message"], "tuner disconnected")

    def test_abort_without_pending_state_is_silent(self):
        self.client._abort_auto_tune("tuner disconnected")
        self.assertEqual(self.events, [])


class GuardSourceTests(unittest.TestCase):
    """The auto-retune guard must be structurally unable to key the radio."""

    def test_client_has_no_cat_or_ptt_reference(self):
        src = Path("atr1000_client.py").read_text(encoding="utf-8")
        for forbidden in ("set_ptt", "set_tune(", "cat_controller", "import server"):
            self.assertNotIn(forbidden, src)

    def test_guard_section_only_queues_a_tune_frame(self):
        src = Path("atr1000_client.py").read_text(encoding="utf-8")
        guard = src.split("# ── High-SWR auto-retune guard", 1)[1]
        guard = guard.split("# ── Internals", 1)[0]
        self.assertIn("self._pending_tune_mode = 2", guard)
        self.assertNotIn("set_relay", guard)

    def test_disconnect_wires_the_abort(self):
        src = Path("atr1000_client.py").read_text(encoding="utf-8")
        self.assertIn('self._abort_auto_tune("tuner disconnected")', src)


if __name__ == "__main__":
    unittest.main()
