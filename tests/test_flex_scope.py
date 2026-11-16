"""Spectrum producer tests for the FlexRadio backend (P3, design §6).

The producer is the last place before the browser: its 850-bin frame, span
index and start frequency are what the waterfall axis is drawn from, so a
mistake here shows up as a plausible — but wrong — frequency scale.
"""

import asyncio
import unittest
from typing import Any, cast

import numpy as np

from backends.flex.engine import SmartSDREngine
from backends.flex.scope import (
    DEFAULT_SPAN_INDEX, SPANS, FlexScopeProducer,
)
from backends.ft710.scope_frame import WF_SIZE


class _ScopeStub:
    def __init__(self):
        self.spectrum_rx1 = []
        self.spectrum_rx2 = []
        self.scope_mode = None
        self.scope_span = None
        self.scope_start_freq = 0
        self.last_update = 0.0
        self._frame_count = 0
        self._connected = False


def _engine_with_frame(frame_source_pixel=0.0):
    engine = SmartSDREngine("127.0.0.1")
    engine.connected = True
    engine.pan_center_hz = 14_074_000
    engine.slices[0] = {"freq_hz": 14_074_000, "mode": "USB"}
    engine.own_slice = 0
    pixels = np.full(WF_SIZE, int(frame_source_pixel), dtype=np.uint16)
    engine._spectrum = (255 - pixels).astype(np.uint8)
    return engine


class EmitTests(unittest.IsolatedAsyncioTestCase):
    async def test_frame_is_published_with_axis(self):
        engine = _engine_with_frame(0)
        scope = _ScopeStub()
        producer = FlexScopeProducer(engine, scope=cast(Any, scope))
        self.assertTrue(await producer._emit_once())
        self.assertTrue(scope._connected)
        self.assertEqual(len(scope.spectrum_rx1), WF_SIZE)
        self.assertEqual(scope.spectrum_rx2, [0] * WF_SIZE)
        self.assertEqual(scope.scope_mode, 0)
        self.assertEqual(scope.scope_span, DEFAULT_SPAN_INDEX)
        expected_span = int(SPANS[DEFAULT_SPAN_INDEX]["freq"])  # type: ignore[arg-type]
        self.assertEqual(scope.scope_start_freq, 14_074_000 - expected_span // 2)
        self.assertEqual(scope.spectrum_rx1[0], 255)

    async def test_the_axis_uses_the_radios_reported_bandwidth(self):
        """我们请求的跨度可能被电台夹住；轴必须用 `display pan` 回报的带宽。

        `_update_pan()` 把电台的 `bandwidth=` 写进 `engine.bandwidth_hz`，所以这里
        模拟"我们要 100 kHz、电台只给 50 kHz"：起点应为中心 − 25 kHz。
        """
        engine = _engine_with_frame(0)
        engine.bandwidth_hz = 50_000
        scope = _ScopeStub()
        producer = FlexScopeProducer(engine, scope=cast(Any, scope))
        self.assertTrue(await producer._emit_once())
        self.assertEqual(scope.scope_start_freq, 14_074_000 - 25_000)

    async def test_no_frame_means_no_publish(self):
        engine = SmartSDREngine("127.0.0.1")
        engine.connected = True
        scope = _ScopeStub()
        producer = FlexScopeProducer(engine, scope=cast(Any, scope))
        self.assertFalse(await producer._emit_once())
        self.assertFalse(scope._connected)

    async def test_disconnected_engine_marks_the_scope_idle(self):
        engine = _engine_with_frame()
        engine.connected = False
        scope = _ScopeStub()
        producer = FlexScopeProducer(engine, scope=cast(Any, scope))
        self.assertFalse(await producer._emit_once())
        self.assertFalse(scope._connected)


class SpanTests(unittest.IsolatedAsyncioTestCase):
    async def test_set_span_asks_the_radio_for_that_bandwidth(self):
        engine = _engine_with_frame()
        calls = []

        async def fake_set_bandwidth(hz: int) -> bool:
            calls.append(hz)
            return True

        engine.set_bandwidth = fake_set_bandwidth      # type: ignore[method-assign]
        producer = FlexScopeProducer(engine)
        self.assertTrue(await producer.set_span(6))
        self.assertEqual(producer.span_index, 6)
        self.assertEqual(calls, [1_000_000])

    async def test_unknown_span_is_refused(self):
        engine = _engine_with_frame()
        producer = FlexScopeProducer(engine)
        self.assertFalse(await producer.set_span(99))

    async def test_span_table_matches_the_frontend_shape(self):
        for index, entry in SPANS.items():
            self.assertIn("name", entry)
            self.assertIsInstance(entry["freq"], int)
            self.assertGreater(int(entry["freq"]), 0)
        self.assertIn(DEFAULT_SPAN_INDEX, SPANS)


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_and_stop_are_idempotent(self):
        engine = _engine_with_frame()
        producer = FlexScopeProducer(engine, scope=cast(Any, _ScopeStub()), fps=50.0)
        await producer.start()
        await producer.start()
        await asyncio.sleep(0.1)
        await producer.stop()
        await producer.stop()
        self.assertGreaterEqual(producer.frames_emitted, 1)

    async def test_notify_tx_does_not_pause_the_stream(self):
        engine = _engine_with_frame()
        producer = FlexScopeProducer(engine, scope=cast(Any, _ScopeStub()))
        producer.notify_tx(True)
        self.assertTrue(await producer._emit_once())


if __name__ == "__main__":
    unittest.main()
