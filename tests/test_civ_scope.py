"""Tests for backends.ic7300.civ_scope.CivScopeProducer (Phase 3).

No hardware: the producer is driven by pushing synthetic ScopeSegments
into a real (unconnected) CivController's scope_queue.
"""
import asyncio
import unittest

from backends.ic7300.civ_controller import CivController
from backends.ic7300.civ_codec import (
    ScopeSegment,
    SCOPE_MODE_CENTER, SCOPE_MODE_FIXED,
    SCOPE_MAX_SEGMENTS, SCOPE_WAVEFORM_LEN,
)
from backends.ic7300.civ_scope import CivScopeProducer
from backends.ic7300.config_ic7300 import SCOPE_SPAN_HZ
import backends.ic7300.civ_scope as civ_scope
from scope_handler import ScopeHandler


def make_waveform_segments(
    bin_value: int = 160,
    scope_mode: int = SCOPE_MODE_CENTER,
    center_freq_hz: int | None = 14_074_000,
    span_hz: int | None = 100_000,
    low_edge_hz: int | None = None,
    high_edge_hz: int | None = None,
) -> list[ScopeSegment]:
    """One complete 11-segment USB-serial waveform (475 bins)."""
    info = ScopeSegment(
        sequence=1,
        sequence_max=SCOPE_MAX_SEGMENTS,
        bins=b"",
        is_division_start=True,
        scope_mode=scope_mode,
        center_freq_hz=center_freq_hz,
        span_hz=span_hz,
        low_edge_hz=low_edge_hz,
        high_edge_hz=high_edge_hz,
    )
    segs = [info]
    for seq in range(2, SCOPE_MAX_SEGMENTS + 1):
        n = SCOPE_WAVEFORM_LEN - 50 * (SCOPE_MAX_SEGMENTS - 2) if seq == SCOPE_MAX_SEGMENTS else 50
        segs.append(ScopeSegment(
            sequence=seq,
            sequence_max=SCOPE_MAX_SEGMENTS,
            bins=bytes([bin_value] * n),
        ))
    return segs


class CivScopeProducerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.civ = CivController("/dev/null")          # never connected
        self.scope = ScopeHandler()
        self.frames: list[ScopeHandler] = []
        self._frame_event = asyncio.Event()

        async def on_frame(scope):
            self.frames.append(scope)
            self._frame_event.set()

        self.producer = CivScopeProducer(self.civ, self.scope, on_frame)

    async def asyncTearDown(self):
        await self.producer.stop()

    async def _push(self, segs):
        for s in segs:
            self.civ.scope_queue.put_nowait(s)

    async def _run_waveform(self, segs):
        """Start the producer, push segments, wait for one on_frame."""
        await self.producer.start()
        self._frame_event.clear()
        await self._push(segs)
        await asyncio.wait_for(self._frame_event.wait(), timeout=1.0)
        # Let the consumer finish the frame write + callback.
        await asyncio.sleep(0)

    # ── Waveform path ──────────────────────────────────────────────

    async def test_full_waveform_populates_handler(self):
        await self._run_waveform(make_waveform_segments(bin_value=160))
        self.assertEqual(len(self.scope.spectrum_rx1), 850)
        self.assertEqual(len(self.scope.spectrum_rx2), 850)
        self.assertTrue(all(v == 0 for v in self.scope.spectrum_rx2))
        # Full-scale input (160) scales to 255 everywhere.
        self.assertTrue(all(v == 255 for v in self.scope.spectrum_rx1))
        self.assertTrue(self.scope.connected)
        self.assertGreater(self.scope.last_update, 0)
        self.assertEqual(len(self.frames), 1)

    async def test_amplitude_scaling(self):
        await self._run_waveform(make_waveform_segments(bin_value=80))
        # 80 / 160 * 255 = 127.5 -> round -> 128 (banker's? round(127.5)=128 on py? verify range)
        self.assertTrue(all(v in (127, 128) for v in self.scope.spectrum_rx1),
                        msg=f"unexpected values: {set(self.scope.spectrum_rx1)}")

    async def test_center_mode_info_chunk_metadata(self):
        # ±100 kHz half-span around 14.074 MHz -> left edge 13.974 MHz.
        await self._run_waveform(make_waveform_segments(
            center_freq_hz=14_074_000, span_hz=100_000))
        self.assertEqual(self.scope.scope_mode, SCOPE_MODE_CENTER)
        self.assertEqual(self.scope.scope_start_freq, 13_974_000)
        # Half-span Hz reverse-mapped to the UI span index.
        expected_idx = {hz: i for i, hz in SCOPE_SPAN_HZ.items()}[100_000]
        self.assertEqual(self.scope.scope_span, expected_idx)

    async def test_fixed_mode_info_chunk_metadata(self):
        await self._run_waveform(make_waveform_segments(
            scope_mode=SCOPE_MODE_FIXED,
            center_freq_hz=None, span_hz=None,
            low_edge_hz=14_000_000, high_edge_hz=14_350_000))
        self.assertEqual(self.scope.scope_mode, SCOPE_MODE_FIXED)
        self.assertEqual(self.scope.scope_start_freq, 14_000_000)

    async def test_on_frame_called_once_per_waveform(self):
        await self.producer.start()
        for _ in range(3):
            self._frame_event.clear()
            await self._push(make_waveform_segments())
            await asyncio.wait_for(self._frame_event.wait(), timeout=1.0)
        await asyncio.sleep(0.05)
        self.assertEqual(len(self.frames), 3)
        self.assertEqual(self.scope._frame_count, 3)

    # ── Assembler robustness ───────────────────────────────────────

    async def test_sequence_gap_drops_waveform(self):
        segs = make_waveform_segments()
        gapped = segs[:3] + segs[4:]     # drop sequence 4
        await self.producer.start()
        await self._push(gapped)
        await asyncio.sleep(0.1)
        self.assertEqual(len(self.frames), 0)
        self.assertFalse(self.scope.connected)
        # Spectrum untouched (still the ScopeHandler defaults).
        self.assertTrue(all(v == 0 for v in self.scope.spectrum_rx1))
        # Assembler recovers on the next clean waveform.
        self._frame_event.clear()
        await self._push(make_waveform_segments())
        await asyncio.wait_for(self._frame_event.wait(), timeout=1.0)
        self.assertEqual(len(self.frames), 1)

    # ── Lifecycle ──────────────────────────────────────────────────

    async def test_stop_cancels_drains_and_disconnects(self):
        await self._run_waveform(make_waveform_segments())
        self.assertTrue(self.scope.connected)
        await self.producer.stop()
        self.assertFalse(self.scope.connected)
        self.assertTrue(self.civ.scope_queue.empty())
        # Segments arriving after stop are simply never consumed.
        await self._push(make_waveform_segments())
        await asyncio.sleep(0.05)
        self.assertEqual(len(self.frames), 1)

    async def test_notify_tx_is_noop(self):
        await self.producer.start()
        self.producer.notify_tx(True)
        self.producer.notify_tx(False)
        await self.producer.stop()
        # No exception, no state change required.

    async def test_set_on_frame_replaces_callback(self):
        seen = []

        async def cb(scope):
            seen.append(scope)

        self.producer.set_on_frame(cb)
        await self.producer.start()
        await self._push(make_waveform_segments())
        await asyncio.sleep(0.1)
        self.assertEqual(len(seen), 1)
        self.assertEqual(len(self.frames), 0)

    async def test_stall_watchdog_warns_once(self):
        old = civ_scope.STALL_TIMEOUT_S
        civ_scope.STALL_TIMEOUT_S = 0.05
        try:
            await self.producer.start()
            with self.assertLogs("ic7300.scope", level="WARNING") as cm:
                await asyncio.sleep(0.3)   # several stall windows
            stalls = [r for r in cm.output if "stalled" in r]
            self.assertEqual(len(stalls), 1, cm.output)
            # A waveform clears the warned latch; a later stall re-warns.
            self._frame_event.clear()
            await self._push(make_waveform_segments())
            await asyncio.wait_for(self._frame_event.wait(), timeout=1.0)
            self.assertFalse(self.producer._stall_warned)
            with self.assertLogs("ic7300.scope", level="WARNING") as cm2:
                await asyncio.sleep(0.2)
            self.assertTrue(any("stalled" in r for r in cm2.output))
        finally:
            civ_scope.STALL_TIMEOUT_S = old

    async def test_start_is_idempotent(self):
        await self.producer.start()
        task = self.producer._task
        await self.producer.start()
        self.assertIs(self.producer._task, task)


class BackendFactoryScopeProducerTests(unittest.TestCase):
    def test_create_scope_producer_returns_civ_producer(self):
        from backends import create_backend
        backend = create_backend("ic7300", port="/dev/null")
        scope = ScopeHandler()

        async def cb(scope):
            pass

        producer = backend.create_scope_producer(scope, cb)
        self.assertIsInstance(producer, CivScopeProducer)
        self.assertIs(producer._civ, backend.cat)
        self.assertIs(producer._scope, scope)


if __name__ == "__main__":
    unittest.main()
