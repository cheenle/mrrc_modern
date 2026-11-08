"""SunSDR2 DX native backend — Task 3: single-asyncio UDP device engine.

Everything runs on real UDP loopback (127.0.0.1) with a fake radio: two
asyncio datagram endpoints that record what the engine sends and can push RX
IQ / telemetry back to the engine's bound (ephemeral) ports.  No real
hardware, no long sleeps: heartbeat/pacer/boot spacing are injected.

Coverage against the Task-3 spec:

- bind both UDP sockets (control first, then IQ) BEFORE boot; a failed bind
  returns False, cleans the half-open socket and logs; empty host refused;
- start() idempotent; boot sequence byte-shaped from Task 1's
  build_boot_sequence (START_OPS first, HW_INIT, RX_FREQ twice, STREAM_CTRL);
- 0.5 s control heartbeat + 0xFFFE IQ keep-alive, independent of DSP state;
- stream counter shared, +0x10000 per packet, masked & 0xFFFFFFFF (wrap);
- control ordering: set_frequency → RX_FREQ, TX_FREQ, DRIVE; set_ptt(True) →
  DRIVE then PTT1; set_ptt(False) → immediate PTT0 + state clear, even when
  the DSP path is broken; stop() releases PTT before closing transports and
  survives cancellation;
- RX: IQ → AudioDemodulator → 48 kHz int16 into a bounded deque with NO
  consumer registered; rolling IQ retained for the scope adapter; 0x1F00
  power telemetry stored + broadcast; 0x1F01 confirms ptt_confirmed; bad
  packets counted, never fatal;
- TX: feed_tx_audio → TXModulator → bounded queue (full = drop oldest,
  counted); pacer writes 0xFFFD packets at the injected 200/(rate/2) pace;
  underrun sends zero IQ and counts, never re-sends old audio; start_tx
  clears stale state; stop_tx fast by default, graceful bounded drain
  optional;
- sample rate: four protocol rates only, refused while TX is active,
  serialized (single-flight) controlled re-boot that rebuilds demodulator +
  TX modulator (TX rate = RX rate / 2); recovery reboot rate-limited;
- counters: sent/recv packets+bytes, bad_packets, tx_drops, underruns and a
  bounded send log observable by tests.
"""
import asyncio
import logging
import socket
import struct
import time
import unittest

import numpy as np

from backends.sunsdr import protocol as sdp
from backends.sunsdr.engine import SunSDREngine

logger = logging.getLogger(__name__)


# ── helpers ───────────────────────────────────────────────────────

def _ctrl_cmd(data: bytes) -> int:
    return struct.unpack_from("<H", data, 2)[0]


def _ctrl_trailing(data: bytes) -> int:
    return struct.unpack_from("<I", data, len(data) - 4)[0]


def _stream_fields(data: bytes):
    return struct.unpack_from("<HHIH", data, 0)


def _tone(n, hz=1500.0, fs=78125.0, amp=0.5):
    t = np.arange(n, dtype=np.float64)
    return (amp * np.exp(2j * np.pi * hz * t / fs)).astype(np.complex64)


def _pcm16(n, hz=1000.0, fs=48000, amp=0.4):
    t = np.arange(n, dtype=np.float64)
    return (np.clip(np.rint(amp * 32767.0 * np.sin(2 * np.pi * hz * t / fs)),
                    -32768, 32767).astype("<i2").tobytes())


async def _wait_until(pred, timeout=3.0, interval=0.005):
    t0 = time.monotonic()
    while not pred():
        if time.monotonic() - t0 > timeout:
            raise AssertionError("condition was not met in time")
        await asyncio.sleep(interval)


class FakeRadio:
    """Two UDP endpoints emulating the SunSDR2 DX device."""

    def __init__(self):
        self.control = []        # [(addr, bytes)] arrival order
        self.stream = []         # [(addr, bytes)] arrival order
        self._control_addrs = []
        self._stream_addrs = []
        self._transports = []

    @classmethod
    async def create(cls):
        loop = asyncio.get_running_loop()
        self = cls()

        def make(which):
            log = getattr(self, which)
            addrs = getattr(self, "_" + which + "_addrs")

            class _P(asyncio.DatagramProtocol):
                def datagram_received(self_, data, addr):
                    log.append((addr, bytes(data)))
                    if addr not in addrs:
                        addrs.append(addr)

                def error_received(self_, exc):
                    pass

                def connection_lost(self_, exc):
                    pass

            return _P

        t1, _ = await loop.create_datagram_endpoint(
            make("control"), local_addr=("127.0.0.1", 0))
        t2, _ = await loop.create_datagram_endpoint(
            make("stream"), local_addr=("127.0.0.1", 0))
        self._transports = [t1, t2]
        self.control_port = t1.get_extra_info("sockname")[1]
        self.stream_port = t2.get_extra_info("sockname")[1]
        return self

    def close(self):
        for t in self._transports:
            t.close()

    def control_cmds(self):
        return [_ctrl_cmd(d) for _, d in self.control]

    def stream_subtypes(self):
        return [_stream_fields(d)[1] for _, d in self.stream]

    def _stream_target(self):
        if not self._stream_addrs:
            raise AssertionError("engine has not sent to the stream port yet")
        return self._stream_addrs[0]

    def send_raw_stream(self, data: bytes):
        self._transports[1].sendto(data, self._stream_target())

    def send_iq(self, samples, counter=0x0100, flags=0x0001):
        pkt = (struct.pack("<HHIH", 0xFF32, 0xFFFE, counter, flags)
               + sdp.pack_iq_24bit(samples))
        self.send_raw_stream(pkt)

    def send_power_telemetry(self, fwd_w=55.5, volts=13.1, temp=44.0,
                             counter=0x0200):
        raw = bytearray(34)
        struct.pack_into("<HHIH", raw, 0, 0xFF32, 0x1F00, counter, 0x0001)
        struct.pack_into("<H", raw, 16, int(round(volts * 10)))
        struct.pack_into("<f", raw, 18, temp)
        struct.pack_into("<f", raw, 22, fwd_w / 3.0)
        struct.pack_into("<f", raw, 30, fwd_w)
        self.send_raw_stream(bytes(raw))

    def send_ptt_status(self, tx, counter=0x0300):
        raw = bytearray(22)
        struct.pack_into("<HHIH", raw, 0, 0xFF32, 0x1F01, counter, 0x0001)
        struct.pack_into("<I", raw, 18, 0x00010000 if tx else 0)
        self.send_raw_stream(bytes(raw))


class EngineCase(unittest.IsolatedAsyncioTestCase):
    """Base: a started engine wired to a fake radio on loopback."""

    engine_kwargs = {}

    async def asyncSetUp(self):
        self.radio = await FakeRadio.create()
        self.engine = self.make_engine()

    async def asyncTearDown(self):
        try:
            await asyncio.wait_for(self.engine.stop(), timeout=2.0)
        finally:
            self.radio.close()

    def make_engine(self, **kw):
        args = dict(host="127.0.0.1",
                    local_host="127.0.0.1",
                    control_port=self.radio.control_port,
                    iq_port=self.radio.stream_port,
                    bind_control_port=0,
                    bind_iq_port=0,
                    heartbeat_s=0.02,
                    boot_spacing_s=0.0)
        args.update(self.engine_kwargs)
        args.update(kw)
        return SunSDREngine(**args)

    async def start_engine(self):
        self.assertTrue(await self.engine.start())
        await _wait_until(lambda: len(self.radio.control) >= 29)

    async def _control_packets(self, n, timeout=2.0):
        """Wait for UDP delivery of at least n control packets, return the
        non-heartbeat ones in arrival order."""
        await _wait_until(
            lambda: len([1 for _, d in self.radio.control
                         if _ctrl_cmd(d) != sdp.CmdID.HEARTBEAT]) >= n,
            timeout)
        return [d for _, d in self.radio.control
                if _ctrl_cmd(d) != sdp.CmdID.HEARTBEAT]

    async def _pump_tone(self, n_samples=6000, hz=1500.0):
        # ``hz`` is the offset from the tuned VFO, not a raw LO-relative
        # frequency: the device puts the VFO at -IF_OFFSET in the IQ stream.
        # Continuous-phase tone sliced into 200-sample packets (a fresh
        # tone per packet would splatter); loopback UDP buffers are small,
        # so send in batches with yields for the engine to drain.
        full = _tone(n_samples, hz=hz - sdp.IF_OFFSET_HZ)
        for i, off in enumerate(range(0, n_samples, 200)):
            self.radio.send_iq(full[off:off + 200],
                               counter=0x1000 + off)
            if i % 10 == 9:
                await asyncio.sleep(0)


# ── bind / start / stop ───────────────────────────────────────────

class BindStartTests(EngineCase):
    async def test_empty_host_refused_with_log(self):
        engine = SunSDREngine(host="", local_host="127.0.0.1",
                              control_port=self.radio.control_port,
                              iq_port=self.radio.stream_port)
        with self.assertLogs("backends.sunsdr.engine", level="ERROR") as cm:
            self.assertFalse(await engine.start())
        self.assertTrue(any("host" in m.lower() for m in cm.output))
        self.assertFalse(engine.connected)

    async def test_iq_bind_failure_cleans_half_open_control_socket(self):
        blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            blocker.bind(("127.0.0.1", 0))
            occupied = blocker.getsockname()[1]
            self.engine.bind_iq_port = occupied
            with self.assertLogs("backends.sunsdr.engine", level="ERROR"):
                self.assertFalse(await self.engine.start())
            self.assertFalse(self.engine.connected)
            # retry on a free port succeeds → the half-open control socket
            # from the failed attempt was really closed, not leaked
            self.engine.bind_iq_port = 0
            await self.start_engine()
        finally:
            blocker.close()

    async def test_start_sends_captured_boot_sequence(self):
        await self.start_engine()
        cmds = self.radio.control_cmds()[:29]
        self.assertEqual(cmds[0], sdp.CmdID.START_OPS)
        self.assertEqual(cmds.count(sdp.CmdID.START_OPS), 1)
        self.assertEqual(cmds.count(sdp.CmdID.HW_INIT), 1)
        self.assertEqual(cmds.count(sdp.CmdID.RX_FREQ), 2)
        self.assertEqual(cmds.count(sdp.CmdID.TX_FREQ), 1)
        self.assertEqual(cmds.count(sdp.CmdID.DRIVE), 1)
        self.assertIn(sdp.CmdID.STREAM_CTRL, cmds)
        # default rate is 78k → HW_INIT word[11] == 1
        for _, d in self.radio.control:
            if _ctrl_cmd(d) == sdp.CmdID.HW_INIT:
                self.assertEqual(struct.unpack_from("<I", d, 14 + 11 * 4)[0], 1)

    async def test_start_is_idempotent_single_boot(self):
        await self.start_engine()
        self.assertTrue(await self.engine.start())
        await asyncio.sleep(0.1)
        self.assertEqual(
            self.radio.control_cmds().count(sdp.CmdID.START_OPS), 1)

    async def test_connected_flag(self):
        self.assertFalse(self.engine.connected)
        await self.start_engine()
        self.assertTrue(self.engine.connected)

    async def test_stop_is_repeatable_and_stops_heartbeat(self):
        await self.start_engine()
        await _wait_until(lambda: self.radio.control_cmds().count(
            sdp.CmdID.HEARTBEAT) >= 2)
        await self.engine.stop()
        self.assertFalse(self.engine.connected)
        n = len(self.radio.control)
        await self.engine.stop()
        await asyncio.sleep(0.1)
        self.assertEqual(len(self.radio.control), n)

    async def test_stop_survives_cancellation(self):
        await self.start_engine()
        await self.engine.set_ptt(True)     # keyed, so stop() must release
        await asyncio.sleep(0.05)           # let DRIVE+PTT1 deliver
        self.radio.control.clear()
        task = asyncio.create_task(self.engine.stop())
        await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self.assertFalse(self.engine.connected)
        self.assertFalse(self.engine.ptt_requested)
        # transport really closed: start is required before any further send
        self.assertIsNone(self.engine._ctrl_transport)
        self.assertIsNone(self.engine._iq_transport)
        # the PTT release still reached the wire before the sockets closed
        pkts = await self._control_packets(1)
        self.assertEqual([_ctrl_cmd(d) for d in pkts[:1]], [sdp.CmdID.PTT])
        self.assertEqual(_ctrl_trailing(pkts[0]), 0)


# ── heartbeat / keep-alive ────────────────────────────────────────

class HeartbeatTests(EngineCase):
    async def test_control_heartbeat_every_interval(self):
        await self.start_engine()
        await _wait_until(lambda: self.radio.control_cmds().count(
            sdp.CmdID.HEARTBEAT) >= 3)
        for _, d in self.radio.control:
            if _ctrl_cmd(d) == sdp.CmdID.HEARTBEAT:
                # 22 bytes: header(14) + u32 zero payload(4) + trailing(4)
                self.assertEqual(len(d), 22)
                self.assertEqual(d[14:18], b"\x00" * 4)

    async def test_iq_keepalive_shape_and_port(self):
        await self.start_engine()
        await _wait_until(
            lambda: 0xFFFE in self.radio.stream_subtypes())
        for _, d in self.radio.stream:
            if _stream_fields(d)[1] == 0xFFFE:
                self.assertEqual(len(d), sdp.IQ_PACKET_BYTES)
                self.assertEqual(d[10:], b"\x00" * sdp.IQ_PAYLOAD_BYTES)
                self.assertEqual(_stream_fields(d)[3], 0x0001)  # flags

    async def test_keepalive_counter_increments_by_stream_step(self):
        await self.start_engine()
        await _wait_until(lambda: len(
            [d for _, d in self.radio.stream
             if _stream_fields(d)[1] == 0xFFFE]) >= 2)
        kas = [_stream_fields(d)[2] for _, d in self.radio.stream
               if _stream_fields(d)[1] == 0xFFFE]
        self.assertEqual((kas[1] - kas[0]) & 0xFFFFFFFF,
                         sdp.STREAM_COUNTER_STEP)

    async def test_stream_counter_wraps_at_32_bits(self):
        await self.start_engine()
        await _wait_until(lambda: len(self.radio.stream) >= 1)
        baseline = len(self.radio.stream)
        self.engine._stream_counter = 0xFFFF0000
        await _wait_until(lambda: len(self.radio.stream) >= baseline + 2)
        kas = [_stream_fields(d)[2] for _, d in self.radio.stream
               if _stream_fields(d)[1] == 0xFFFE][-2:]
        # 0xFFFF0000 + 0x10000 wraps to exactly 0 on the next packet
        self.assertEqual(kas[0], 0x00000000)
        self.assertEqual(kas[1], 0x00010000)
        self.assertTrue(all(c <= 0xFFFFFFFF for c in kas))


# ── control surface ───────────────────────────────────────────────

class ControlTests(EngineCase):
    async def test_set_frequency_sends_rx_tx_drive_in_order(self):
        await self.start_engine()
        self.radio.control.clear()
        self.assertTrue(await self.engine.set_frequency(7_100_000))
        pkts = await self._control_packets(3)
        self.assertEqual([_ctrl_cmd(d) for d in pkts[:3]],
                         [sdp.CmdID.RX_FREQ, sdp.CmdID.TX_FREQ,
                          sdp.CmdID.DRIVE])
        dds = struct.unpack_from("<I", pkts[0], 14 + 4)[0]
        self.assertEqual(dds, int((7_100_000 + sdp.IF_OFFSET_HZ) * 10))
        self.assertEqual(struct.unpack_from("<I", pkts[1], 14 + 4)[0],
                         7_100_000 * 10)
        # QSY re-sends the CURRENT drive (default 100% → 0xFF)
        self.assertEqual(_ctrl_trailing(pkts[2]), sdp.drive_byte(100))
        self.assertEqual(self.engine.freq, 7_100_000)

    async def test_qsy_keeps_current_drive(self):
        await self.start_engine()
        await self.engine.set_drive(30)
        await asyncio.sleep(0.05)      # let the drive packet deliver
        self.radio.control.clear()
        await self.engine.set_frequency(14_200_000)
        pkts = await self._control_packets(3)
        self.assertEqual(_ctrl_trailing(pkts[2]), sdp.drive_byte(30))

    async def test_set_drive_sends_sqrt_taper_byte_in_trailing(self):
        await self.start_engine()
        self.radio.control.clear()
        await self.engine.set_drive(50)
        pkt = (await self._control_packets(1))[0]
        self.assertEqual(_ctrl_cmd(pkt), sdp.CmdID.DRIVE)
        self.assertEqual(_ctrl_trailing(pkt), 0xB4)
        self.assertEqual(pkt[14:18], b"\x00" * 4)

    async def test_set_ptt_true_sends_drive_then_ptt1(self):
        await self.start_engine()
        self.radio.control.clear()
        self.assertTrue(await self.engine.set_ptt(True))
        pkts = await self._control_packets(2)
        self.assertEqual([_ctrl_cmd(d) for d in pkts[:2]],
                         [sdp.CmdID.DRIVE, sdp.CmdID.PTT])
        self.assertEqual(_ctrl_trailing(pkts[1]), 1)
        self.assertTrue(self.engine.ptt_requested)
        self.assertFalse(self.engine.ptt_confirmed)

    async def test_set_ptt_false_sends_ptt0_and_clears_immediately(self):
        await self.start_engine()
        await self.engine.set_ptt(True)
        await asyncio.sleep(0.05)      # let DRIVE+PTT1 deliver
        self.radio.control.clear()
        await self.engine.set_ptt(False)
        pkts = await self._control_packets(1)
        self.assertEqual([_ctrl_cmd(d) for d in pkts[:1]], [sdp.CmdID.PTT])
        self.assertEqual(_ctrl_trailing(pkts[0]), 0)
        self.assertFalse(self.engine.ptt_requested)
        self.assertFalse(self.engine.ptt_confirmed)

    async def test_release_survives_broken_dsp_and_dead_transport(self):
        await self.start_engine()

        def _boom(iq):
            raise RuntimeError("dsp exploded")

        self.engine.demodulator.demodulate = _boom
        self.radio.send_iq(np.zeros(200, dtype=np.complex64))
        await asyncio.sleep(0.05)          # RX path must not die
        await self.engine.set_ptt(True)
        # dead transport: sends raise, release must still clear state
        self.radio.control.clear()
        self.engine._ctrl_transport.sendto = (
            lambda *a, **k: (_ for _ in ()).throw(OSError("gone")))
        await self.engine.set_ptt(False)   # must not raise
        self.assertFalse(self.engine.ptt_requested)
        await self.engine.stop()           # must not raise either

    async def test_stop_releases_ptt_before_closing_sockets(self):
        await self.start_engine()
        await self.engine.set_ptt(True)
        await asyncio.sleep(0.05)      # let DRIVE+PTT1 deliver
        self.radio.control.clear()
        await self.engine.stop()
        pkts = await self._control_packets(1)
        self.assertEqual([_ctrl_cmd(d) for d in pkts[:1]], [sdp.CmdID.PTT])
        self.assertEqual(_ctrl_trailing(pkts[0]), 0)
        n = len(self.radio.control)
        await asyncio.sleep(0.1)
        self.assertEqual(len(self.radio.control), n)  # sockets closed

    async def test_att_preamp_uses_set_param_5_trailing_0_to_3(self):
        await self.start_engine()
        for level, expected in ((-5, 0), (0, 0), (1, 1), (2, 2), (3, 3),
                                (9, 3)):
            self.radio.control.clear()
            await self.engine.set_att_preamp(level)
            pkt = (await self._control_packets(1))[0]
            self.assertEqual(_ctrl_cmd(pkt), sdp.CmdID.SET_PARAM_5)
            self.assertEqual(_ctrl_trailing(pkt), expected)
            self.assertEqual(pkt[14:18], b"\x00" * 4)

    async def test_boot_preserves_selected_att_preamp(self):
        self.engine.att_preamp = 3
        await self.start_engine()
        boot = [d for _, d in self.radio.control]
        self.assertEqual(_ctrl_cmd(boot[6]), sdp.CmdID.SET_PARAM_5)
        self.assertEqual(_ctrl_trailing(boot[6]), 3)

    async def test_mode_filter_agc_sync_to_dsp(self):
        await self.start_engine()
        await self.engine.set_mode("LSB")
        self.assertEqual(self.engine.demodulator.mode, "LSB")
        self.assertEqual(self.engine.tx_modulator.mode, "LSB")
        # RX-only mode: demod follows, TX modulator keeps its last sideband
        # but TX is explicitly BLOCKED instead of silently keying it.
        await self.engine.set_mode("AM")
        self.assertEqual(self.engine.demodulator.mode, "AM")
        self.assertEqual(self.engine.tx_modulator.mode, "LSB")
        self.assertEqual(self.engine._tx_blocked_mode, "AM")
        with self.assertRaises(ValueError):
            await self.engine.set_mode("NOTAMODE")
        await self.engine.set_filter(300.0, 2400.0)
        await self.engine.set_agc_mode("fast")
        self.assertEqual(self.engine.demodulator._agc_mode, 4)

    async def test_tx_mode_follows_rx_and_survives_span_change(self):
        await self.start_engine()
        await self.engine.set_mode("LSB")
        self.assertEqual(self.engine.tx_modulator.mode, "LSB")
        # A span change rebuilds the modulator for the new IQ rate; it must
        # keep the operator's sideband, not silently reset to USB.
        self.assertTrue(await self.engine.set_sample_rate("156k"))
        self.assertEqual(self.engine.demodulator.mode, "LSB")
        self.assertEqual(self.engine.tx_modulator.mode, "LSB")
        # DIGI-U is USB for transmit (FT8 / sound-card data modes).
        await self.engine.set_mode("DIGI")
        self.assertEqual(self.engine.tx_modulator.mode, "USB")
        self.assertIsNone(self.engine._tx_blocked_mode)
        # AM/FM/CW have no verified TX path and must refuse the key-up.
        for mode in ("AM", "FM", "NFM", "WFM", "CW"):
            await self.engine.set_mode(mode)
            self.assertEqual(self.engine._tx_blocked_mode, mode)
            self.assertFalse(await self.engine.set_ptt(True))
            self.assertFalse(self.engine.ptt_requested)
        await self.engine.set_mode("USB")
        self.assertIsNone(self.engine._tx_blocked_mode)
        self.assertTrue(await self.engine.set_ptt(True))
        await self.engine.set_ptt(False)


# ── RX path ───────────────────────────────────────────────────────

class RxTests(EngineCase):
    async def test_iq_flows_to_48k_pcm_without_any_consumer(self):
        await self.start_engine()
        await _wait_until(lambda: len(self.radio.stream) > 0)  # learn addr
        await self._pump_tone()
        await _wait_until(lambda: len(self.engine.rx_audio) > 0)
        block = self.engine.rx_audio[0]
        self.assertEqual(len(block) % 2, 0)
        n = len(block) // 2
        # a demod block is 3125 IQ samples → ~1900 samples @48k (the very
        # first block can be a short priming remainder)
        self.assertTrue(300 <= n <= 2100, f"unexpected block {n}")
        self.assertTrue(any(block))  # a tone produces non-silence

    async def test_rx_audio_deque_is_bounded(self):
        engine = self.make_engine(rx_audio_max=32)
        await self.engine.stop()
        self.engine = engine
        await self.start_engine()
        await _wait_until(lambda: len(self.radio.stream) > 0)
        # 260k samples ≈ 83 demodulated blocks, well past maxlen=32
        await self._pump_tone(260_000)
        await _wait_until(
            lambda: len(self.engine.rx_audio) == self.engine.rx_audio.maxlen)
        self.assertEqual(len(self.engine.rx_audio),
                         self.engine.rx_audio.maxlen)
        self.assertGreater(self.engine.rx_audio.maxlen, 10)

    async def test_rolling_iq_and_spectrum_for_scope_adapter(self):
        await self.start_engine()
        await _wait_until(lambda: len(self.radio.stream) > 0)
        await self._pump_tone()
        await _wait_until(lambda: len(self.engine.latest_iq) >= 2048)
        spec = self.engine.get_spectrum()
        self.assertEqual(len(spec), 850)
        self.assertTrue(all(isinstance(v, int) and 0 <= v <= 255
                            for v in spec))
        # A VFO-relative +1500 Hz tone sits RIGHT of the display centre.
        self.assertGreater(spec.index(max(spec)), 425)

    async def test_spectrum_is_vfo_centred(self):
        await self.start_engine()
        await _wait_until(lambda: len(self.radio.stream) > 0)
        await self._pump_tone(hz=0.0)          # exactly on the tuned VFO
        await _wait_until(lambda: len(self.engine.latest_iq) >= 2048)
        spec = self.engine.get_spectrum()
        idx = spec.index(max(spec))
        # The raw IQ carries the VFO at -IF_OFFSET; get_spectrum() must shift
        # it to the centre the browser's CENTER mode and click-to-tune use.
        self.assertLess(abs(idx - 425), 12, f"peak at {idx}")

    async def test_s_meter_available_after_iq(self):
        await self.start_engine()
        await _wait_until(lambda: len(self.radio.stream) > 0)
        await self._pump_tone()
        await _wait_until(lambda: self.engine.s_meter is not None)
        self.assertIsInstance(self.engine.s_meter, float)

    async def test_power_telemetry_stored_and_broadcast(self):
        events = []
        await self.engine.stop()
        self.engine = self.make_engine(
            on_event=lambda name, payload: events.append((name, payload)))
        await self.start_engine()
        self.radio.send_power_telemetry(fwd_w=55.5, volts=13.1, temp=44.0)
        await _wait_until(lambda: self.engine.latest_telemetry is not None)
        t = self.engine.latest_telemetry
        self.assertAlmostEqual(t["forward_w"], 55.5, places=3)
        self.assertAlmostEqual(t["volts"], 13.1, places=3)
        self.assertAlmostEqual(t["pa_temp_c"], 44.0, places=3)
        self.assertNotIn("swr", t)   # the device has no reverse power field
        await _wait_until(lambda: any(n == "telemetry" for n, _ in events))

    async def test_ptt_confirmed_comes_from_1f01(self):
        await self.start_engine()
        await self.engine.set_ptt(True)
        self.assertFalse(self.engine.ptt_confirmed)
        self.radio.send_ptt_status(True)
        await _wait_until(lambda: self.engine.ptt_confirmed)
        self.radio.send_ptt_status(False)
        await _wait_until(lambda: not self.engine.ptt_confirmed)

    async def test_bad_packets_counted_never_fatal(self):
        await self.start_engine()
        await _wait_until(lambda: len(self.radio.stream) > 0)  # learn addr
        before = self.engine.bad_packets
        for junk in (b"\x00\x01\x02", b"\x32\xff\xff", b"\x32\xff\xfe",
                     b"\x32\xff\xfe" + b"\x00" * 1203):
            self.radio.send_raw_stream(junk)
        await _wait_until(lambda: self.engine.bad_packets >= before + 4)
        # engine still alive and processing
        self.radio.send_power_telemetry(fwd_w=1.0)
        await _wait_until(lambda: self.engine.latest_telemetry is not None)

    async def test_rx_counters_grow(self):
        await self.start_engine()
        await _wait_until(lambda: len(self.radio.stream) > 0)
        p0, b0 = self.engine.recv_packets, self.engine.recv_bytes
        self.radio.send_iq(np.zeros(200, dtype=np.complex64))
        self.radio.send_power_telemetry()
        await _wait_until(lambda: self.engine.recv_packets >= p0 + 2)
        self.assertGreater(self.engine.recv_bytes, b0)


# ── TX path ───────────────────────────────────────────────────────

class TxTests(EngineCase):
    async def test_feed_tx_audio_produces_1200b_payloads(self):
        await self.start_engine()
        queued = self.engine.feed_tx_audio(_pcm16(4800))
        await asyncio.sleep(0)         # let modulator state settle
        queued += self.engine.feed_tx_audio(_pcm16(4800))
        self.assertGreater(queued, 0)
        self.assertEqual(len(self.engine._tx_queue), queued)
        for payload in self.engine._tx_queue:
            self.assertEqual(len(payload), sdp.IQ_PAYLOAD_BYTES)

    async def test_pacer_sends_fffd_packets_with_counter_and_flags(self):
        # long keep-alive interval so 0xFFFE frames can't interleave with
        # the 0xFFFD counter assertions below
        engine = self.make_engine(tx_interval_s=0.001, heartbeat_s=0.5)
        await self.engine.stop()
        self.engine = engine
        await self.start_engine()
        self.radio.stream.clear()
        await self.engine.start_tx()
        await _wait_until(lambda: len(
            [s for s in self.radio.stream_subtypes() if s == 0xFFFD]) >= 3)
        pkts = [d for _, d in self.radio.stream
                if _stream_fields(d)[1] == 0xFFFD]
        for p in pkts[:3]:
            self.assertEqual(len(p), sdp.IQ_PACKET_BYTES)
            self.assertEqual(_stream_fields(p)[3], 0x0102)   # flags
        ctrs = [_stream_fields(p)[2] for p in pkts[:3]]
        self.assertEqual((ctrs[1] - ctrs[0]) & 0xFFFFFFFF,
                         sdp.STREAM_COUNTER_STEP)

    async def test_priming_sends_zero_iq_without_fake_underruns(self):
        engine = self.make_engine(tx_interval_s=0.001)
        await self.engine.stop()
        self.engine = engine
        await self.start_engine()
        self.radio.stream.clear()
        await self.engine.start_tx()
        await _wait_until(lambda: self.engine.tx_priming_packets >= 3)
        self.assertEqual(self.engine.underruns, 0)
        zeros = [d for _, d in self.radio.stream
                 if _stream_fields(d)[1] == 0xFFFD
                 and d[10:] == b"\x00" * sdp.IQ_PAYLOAD_BYTES]
        self.assertGreaterEqual(len(zeros), 3)

    async def test_queue_full_drops_oldest_and_counts(self):
        engine = self.make_engine(tx_queue_max=4)
        await self.engine.stop()
        self.engine = engine
        await self.start_engine()
        first = self.engine.feed_tx_audio(_pcm16(4800))
        self.assertGreater(first, 4)             # modulator produced payloads
        self.assertEqual(len(self.engine._tx_queue), 4)   # capped at maxlen
        self.assertEqual(self.engine.tx_drops, first - 4)
        oldest = self.engine._tx_queue[0]
        drops0 = self.engine.tx_drops
        second = self.engine.feed_tx_audio(_pcm16(4800))
        self.assertGreater(second, 0)
        self.assertEqual(len(self.engine._tx_queue), 4)
        self.assertEqual(self.engine.tx_drops, drops0 + second)
        self.assertNotEqual(self.engine._tx_queue[0], oldest)

    async def test_never_resends_old_audio(self):
        engine = self.make_engine(tx_interval_s=0.0005,
                                  tx_prime_packets=1, tx_reprime_packets=1)
        await self.engine.stop()
        self.engine = engine
        await self.start_engine()
        self.radio.stream.clear()
        await self.engine.start_tx()
        self.engine.feed_tx_audio(_pcm16(4800))
        await _wait_until(lambda: self.engine.tx_reprime_count >= 1)
        payloads = [d[10:] for _, d in self.radio.stream
                    if _stream_fields(d)[1] == 0xFFFD
                    and d[10:] != b"\x00" * sdp.IQ_PAYLOAD_BYTES]
        self.assertGreater(len(payloads), 0)
        self.assertEqual(len(set(payloads)), len(payloads))

    async def test_jitter_buffer_reprime_absorbs_feed_phase_gap(self):
        engine = self.make_engine(tx_interval_s=0.001,
                                  tx_prime_packets=3, tx_reprime_packets=2)
        await self.engine.stop()
        self.engine = engine
        await self.start_engine()
        await self.engine.start_tx()
        self.engine.feed_tx_audio(_pcm16(4800))
        await _wait_until(lambda: self.engine._tx_primed)
        await _wait_until(lambda: not self.engine._tx_queue)
        await _wait_until(lambda: self.engine.tx_reprime_count > 0)
        before = self.engine.underruns
        self.assertGreater(self.engine.tx_reprime_count, 0)
        self.engine.feed_tx_audio(_pcm16(4800))
        await _wait_until(lambda: self.engine._tx_primed)
        self.assertEqual(self.engine.underruns, before)

    async def test_start_tx_clears_stale_queue_and_modulator(self):
        await self.start_engine()
        self.engine.feed_tx_audio(_pcm16(4800))
        self.assertGreater(len(self.engine._tx_queue), 0)
        await self.engine.start_tx()
        self.assertEqual(len(self.engine._tx_queue), 0)

    async def test_stop_tx_fast_by_default(self):
        engine = self.make_engine(tx_interval_s=0.001)
        await self.engine.stop()
        self.engine = engine
        await self.start_engine()
        await self.engine.start_tx()
        self.engine.feed_tx_audio(_pcm16(4800))
        await self.engine.stop_tx()
        task = self.engine._tx_pacer_task
        self.assertTrue(task is None or task.done())
        self.assertEqual(len(self.engine._tx_queue), 0)

    async def test_stop_tx_graceful_drains_queued_audio(self):
        engine = self.make_engine(tx_interval_s=0.0002)
        await self.engine.stop()
        self.engine = engine
        await self.start_engine()
        self.radio.stream.clear()
        await self.engine.start_tx()
        queued = self.engine.feed_tx_audio(_pcm16(9600))
        self.assertGreaterEqual(queued, 4)
        await self.engine.stop_tx(graceful=True, drain_s=2.0)
        sent = [d[10:] for _, d in self.radio.stream
                if _stream_fields(d)[1] == 0xFFFD
                and d[10:] != b"\x00" * sdp.IQ_PAYLOAD_BYTES]
        self.assertGreaterEqual(len(sent), 4)
        self.assertEqual(len(self.engine._tx_queue), 0)

    async def test_stop_tx_graceful_cancel_leaves_no_orphan_pacer(self):
        engine = self.make_engine(tx_interval_s=0.001)
        await self.engine.stop()
        self.engine = engine
        await self.start_engine()
        await self.engine.set_ptt(True)          # really keyed
        self.radio.stream.clear()
        await self.engine.start_tx()
        for _ in range(10):                      # ~150 payloads ≈ 150 ms
            self.engine.feed_tx_audio(_pcm16(4800))
        pacer = self.engine._tx_pacer_task
        self.assertTrue(self.engine.tx_active)
        t = asyncio.create_task(
            self.engine.stop_tx(graceful=True, drain_s=30.0))
        await asyncio.sleep(0.05)                # cancel mid-drain
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
        # the pacer must be owned to the end: stop flag set, task really
        # finished, and tx_active not lying about a hidden sender
        self.assertTrue(self.engine._tx_stop.is_set())
        await _wait_until(lambda: pacer.done(), timeout=1.0)
        self.assertFalse(self.engine.tx_active)
        def _tx_pkts():
            return [d for _, d in self.radio.stream
                    if _stream_fields(d)[1] == 0xFFFD]
        n_sent = len(_tx_pkts())
        await asyncio.sleep(0.1)
        self.assertEqual(len(_tx_pkts()), n_sent)  # no orphan sends

    async def test_tx_rate_is_half_rx_rate_and_rebuilt_on_rate_change(self):
        await self.start_engine()
        self.assertAlmostEqual(self.engine.tx_interval,
                               200 / (78125 / 2), places=9)
        await self.engine.set_sample_rate("156k")
        self.assertAlmostEqual(self.engine.tx_interval,
                               200 / (156250 / 2), places=9)
        self.assertAlmostEqual(self.engine.tx_modulator.iq_rate, 78125.0)


# ── sample rate / reboot ──────────────────────────────────────────

class RateSwitchTests(EngineCase):
    async def test_only_four_protocol_rates_accepted(self):
        await self.start_engine()
        for bad in (0, 1, 48000, 12345, "99k", "78K ", None):
            with self.assertRaises(ValueError):
                await self.engine.set_sample_rate(bad)
        for good in ("39k", "78k", "156k", "312k",
                     39062, 78125, 156250, 312500):
            await self.engine.set_sample_rate(good)   # must not raise
        self.assertEqual(self.engine.sample_rate, 312500)

    async def test_rate_switch_refused_while_tx_active(self):
        engine = self.make_engine(tx_interval_s=0.001)
        await self.engine.stop()
        self.engine = engine
        await self.start_engine()
        await self.engine.set_ptt(True)
        self.assertFalse(await self.engine.set_sample_rate("156k"))
        self.assertEqual(self.engine.sample_rate, 78125)

    async def test_rate_switch_reboots_with_new_hw_init(self):
        await self.start_engine()
        self.radio.control.clear()
        self.assertTrue(await self.engine.set_sample_rate("156k"))
        await _wait_until(lambda: len(self.radio.control) >= 29)
        cmds = self.radio.control_cmds()
        self.assertEqual(cmds.count(sdp.CmdID.HW_INIT), 1)
        for _, d in self.radio.control:
            if _ctrl_cmd(d) == sdp.CmdID.HW_INIT:
                self.assertEqual(struct.unpack_from("<I", d, 14 + 11 * 4)[0],
                                 2)   # 156k index
        self.assertEqual(self.engine.sample_rate, 156250)
        self.assertEqual(self.engine.demodulator.sample_rate, 156250)

    async def test_concurrent_rate_switches_are_single_flight(self):
        await self.start_engine()
        active = {"n": 0, "peak": 0}
        orig = self.engine._send_boot

        async def counting_boot():
            active["n"] += 1
            active["peak"] = max(active["peak"], active["n"])
            try:
                await orig()
            finally:
                active["n"] -= 1

        self.engine._send_boot = counting_boot
        results = await asyncio.gather(
            self.engine.set_sample_rate("156k"),
            self.engine.set_sample_rate("39k"))
        self.assertTrue(all(results))
        self.assertEqual(active["peak"], 1)   # boots never overlapped
        self.assertIn(self.engine.sample_rate, (39062, 156250))

    async def test_reboot_is_rate_limited_and_single_flight(self):
        engine = self.make_engine(recovery_cooldown_s=0.2)
        await self.engine.stop()
        self.engine = engine
        await self.start_engine()
        self.assertTrue(await self.engine.reboot())
        self.assertFalse(await self.engine.reboot())      # cooldown
        # forced reboot still single-flights: block the boot, second
        # attempt must refuse instead of stacking a parallel boot
        gate = asyncio.Event()
        orig = self.engine._send_boot

        async def gated_boot():
            await gate.wait()
            await orig()

        self.engine._send_boot = gated_boot
        t = asyncio.create_task(self.engine.reboot(force=True))
        await asyncio.sleep(0.02)
        self.assertFalse(await self.engine.reboot(force=True))
        gate.set()
        self.assertTrue(await t)
        await asyncio.sleep(0.3)                         # let cooldown lapse
        self.assertTrue(await self.engine.reboot())

    async def test_reboot_refused_when_not_started(self):
        self.assertFalse(await self.engine.reboot())


# ── counters / observability ──────────────────────────────────────

class CounterTests(EngineCase):
    async def test_sent_counters_and_bounded_log(self):
        await self.start_engine()
        for i in range(20):
            await self.engine.set_drive(50 + (i % 50))
        self.assertGreaterEqual(self.engine.sent_packets, 20)
        self.assertGreater(self.engine.sent_bytes, 20 * 18)
        self.assertLessEqual(len(self.engine.sent_log),
                             self.engine.sent_log_max)

    async def test_next_stream_counter_masks_32bit(self):
        engine = self.engine          # not started: pure counter math
        engine._stream_counter = 0xFFFFFFF0
        vals = [engine._next_stream_counter() for _ in range(4)]
        self.assertEqual(vals[0], 0x0000FFF0)   # wrapped
        self.assertEqual(vals[3], 0x0003FFF0)
        self.assertTrue(all(v <= 0xFFFFFFFF for v in vals))


# ── I1: one DNS resolution at start, cached numeric sockaddrs ─────

class DnsCacheTests(EngineCase):
    def _patch_gai(self, calls, fail=False):
        """Count loop.getaddrinfo calls (IPv4-filtered for determinism)."""
        loop = asyncio.get_running_loop()
        orig = loop.getaddrinfo

        async def counting(host, port, *a, **k):
            calls["n"] += 1
            if fail:
                raise OSError("name resolution failed")
            return [i for i in await orig(host, port, *a, **k)
                    if i[0] == socket.AF_INET]

        loop.getaddrinfo = counting
        return orig

    @staticmethod
    def _restore_gai(orig):
        asyncio.get_running_loop().getaddrinfo = orig

    async def test_start_resolves_once_then_sends_use_cache(self):
        calls = {"n": 0}
        orig = self._patch_gai(calls)
        try:
            await self.start_engine()
        finally:
            self._restore_gai(orig)
        self.assertEqual(calls["n"], 2)   # control + IQ port, once each
        # numeric sockaddr cached, matching the bound address family
        self.assertEqual(self.engine._ctrl_target[0], socket.AF_INET)
        self.assertEqual(self.engine._ctrl_target[1],
                         ("127.0.0.1", self.radio.control_port))
        self.assertEqual(self.engine._iq_target[1],
                         ("127.0.0.1", self.radio.stream_port))
        # after start, no more resolution: every send uses the cache
        calls["n"] = 0
        orig = self._patch_gai(calls)
        try:
            await self.engine.set_frequency(7_200_000)
            await self.engine.set_drive(40)
            await self.engine.set_ptt(True)
            await self.engine.set_ptt(False)
            await asyncio.sleep(0.1)      # heartbeat + keep-alive ticks
        finally:
            self._restore_gai(orig)
        self.assertEqual(calls["n"], 0)

    async def test_resolve_failure_aborts_start(self):
        calls = {"n": 0}
        orig = self._patch_gai(calls, fail=True)
        try:
            with self.assertLogs("backends.sunsdr.engine", level="ERROR") as cm:
                self.assertFalse(await self.engine.start())
            self.assertTrue(any("resolv" in m.lower() or "resolve" in m.lower()
                                for m in cm.output))
        finally:
            self._restore_gai(orig)
        self.assertFalse(self.engine.connected)
        self.assertIsNone(self.engine._ctrl_transport)
        self.assertIsNone(self.engine._iq_transport)


# ── I2: bounded PTT-release retry, epoch-guarded ──────────────────

class PttReleaseRetryTests(EngineCase):
    def _flaky_sendto(self, fail_first=0, always_fail=False):
        """sendto that raises after `fail_first` successful calls."""
        transport = self.engine._ctrl_transport
        real = transport.sendto
        state = {"n": 0}

        def sendto(data, addr):
            state["n"] += 1
            if always_fail or state["n"] <= fail_first:
                raise OSError("network gone")
            return real(data, addr)

        transport.sendto = sendto
        return state

    async def test_release_retry_recovers_failed_ptt0(self):
        engine = self.make_engine(ptt_release_retry_s=0.01,
                                  ptt_release_retries=3)
        await self.engine.stop()
        self.engine = engine
        await self.start_engine()
        await self.engine.set_ptt(True)
        await asyncio.sleep(0.05)
        self.radio.control.clear()
        self._flaky_sendto(fail_first=1)     # first PTT0 raises, then works
        self.assertFalse(await self.engine.set_ptt(False))
        pkts = await self._control_packets(1)
        self.assertEqual([_ctrl_cmd(d) for d in pkts[:1]], [sdp.CmdID.PTT])
        self.assertEqual(_ctrl_trailing(pkts[0]), 0)   # retry delivered it
        self.assertFalse(self.engine.ptt_requested)

    async def test_release_retry_superseded_by_rekey(self):
        engine = self.make_engine(ptt_release_retry_s=0.02,
                                  ptt_release_retries=5)
        await self.engine.stop()
        self.engine = engine
        await self.start_engine()
        await self.engine.set_ptt(True)
        await asyncio.sleep(0.05)
        self.radio.control.clear()
        transport = self.engine._ctrl_transport
        real_sendto = transport.sendto                 # save BEFORE patching
        self._flaky_sendto(always_fail=True)
        self.assertFalse(await self.engine.set_ptt(False))  # retry pending
        self.assertIsNotNone(self.engine._ptt_release_task)
        # re-key before the retry fires: the stale retry must not send PTT0
        transport.sendto = real_sendto
        self.assertTrue(await self.engine.set_ptt(True))
        await asyncio.sleep(0.2)              # well past the retry interval
        pkts = await self._control_packets(2)
        ptt0 = [d for d in pkts
                if _ctrl_cmd(d) == sdp.CmdID.PTT and _ctrl_trailing(d) == 0]
        self.assertEqual(ptt0, [])

    async def test_release_retry_exhaustion_warns(self):
        engine = self.make_engine(ptt_release_retry_s=0.01,
                                  ptt_release_retries=2)
        await self.engine.stop()
        self.engine = engine
        await self.start_engine()
        await self.engine.set_ptt(True)
        await asyncio.sleep(0.05)
        self.radio.control.clear()
        self._flaky_sendto(always_fail=True)
        with self.assertLogs("backends.sunsdr.engine", level="WARNING") as cm:
            await self.engine.set_ptt(False)
            await asyncio.sleep(0.2)          # let both retries run out
        self.assertTrue(any("PTT release" in m for m in cm.output))
        self.assertFalse(self.engine.ptt_requested)

    async def test_stop_cancels_pending_release_retry(self):
        engine = self.make_engine(ptt_release_retry_s=0.05,
                                  ptt_release_retries=5)
        await self.engine.stop()
        self.engine = engine
        await self.start_engine()
        await self.engine.set_ptt(True)
        await asyncio.sleep(0.05)
        self._flaky_sendto(always_fail=True)
        await self.engine.set_ptt(False)      # retry pending
        retry_task = self.engine._ptt_release_task
        self.assertIsNotNone(retry_task)
        await self.engine.stop()
        self.assertTrue(retry_task.done())
        self.assertIsNone(self.engine._ptt_release_task)

    async def test_stop_takes_final_ptt0_shot_with_retry_pending(self):
        # First release fails (retry pending, state flags already cleared).
        # stop() must still fire its own synchronous PTT0 before closing —
        # cancelling the pending retry must not become the last word.
        engine = self.make_engine(ptt_release_retry_s=0.05,
                                  ptt_release_retries=5)
        await self.engine.stop()
        self.engine = engine
        await self.start_engine()
        await self.engine.set_ptt(True)
        await asyncio.sleep(0.05)              # let DRIVE+PTT1 deliver
        self.radio.control.clear()
        transport = self.engine._ctrl_transport
        real_sendto = transport.sendto
        self._flaky_sendto(always_fail=True)
        self.assertFalse(await self.engine.set_ptt(False))
        self.assertIsNotNone(self.engine._ptt_release_task)
        transport.sendto = real_sendto         # link back for the final shot
        await self.engine.stop()
        pkts = await self._control_packets(1)
        self.assertEqual([_ctrl_cmd(d) for d in pkts[:1]], [sdp.CmdID.PTT])
        self.assertEqual(_ctrl_trailing(pkts[0]), 0)
        n = len(self.radio.control)
        await asyncio.sleep(0.1)
        self.assertEqual(len(self.radio.control), n)  # no retry after stop


# ── I3: control writes never interleave with a paced boot ─────────

class BootInterleaveTests(EngineCase):
    def _no_hb(self):
        return [c for c in self.radio.control_cmds()
                if c != sdp.CmdID.HEARTBEAT]

    async def test_qsy_waits_for_paced_boot_no_interleave(self):
        engine = self.make_engine(boot_spacing_s=0.01, heartbeat_s=0.5)
        await self.engine.stop()
        self.engine = engine
        # clear the previous engine's boot log — otherwise the wait below
        # passes on stale packets before the new boot even begins
        self.radio.control.clear()
        start_task = asyncio.create_task(self.engine.start())
        await _wait_until(lambda: len(self.radio.control) >= 5)
        await self.engine.set_frequency(7_200_000)   # must wait for boot
        self.assertTrue(await start_task)
        # boot = 29 packets, 2 of which are heartbeat frames (filtered)
        await _wait_until(lambda: len(self._no_hb()) >= 30)  # full delivery
        cmds = self._no_hb()
        boot = cmds[:27]
        self.assertEqual(boot.count(sdp.CmdID.START_OPS), 1)
        self.assertEqual(boot.count(sdp.CmdID.HW_INIT), 1)
        self.assertEqual(boot.count(sdp.CmdID.RX_FREQ), 2)
        self.assertEqual(cmds[27:30],
                         [sdp.CmdID.RX_FREQ, sdp.CmdID.TX_FREQ,
                          sdp.CmdID.DRIVE])
        # final RX freq on the wire is the new one
        rx = [d for _, d in self.radio.control
              if _ctrl_cmd(d) == sdp.CmdID.RX_FREQ][-1]
        self.assertEqual(struct.unpack_from("<I", rx, 14 + 4)[0],
                         int((7_200_000 + sdp.IF_OFFSET_HZ) * 10))

    async def test_ptt_true_waits_for_paced_boot_no_interleave(self):
        engine = self.make_engine(boot_spacing_s=0.01, heartbeat_s=0.5)
        await self.engine.stop()
        self.engine = engine
        # clear the previous engine's boot log — otherwise the wait below
        # passes on stale packets before the new boot even begins
        self.radio.control.clear()
        start_task = asyncio.create_task(self.engine.start())
        await _wait_until(lambda: len(self.radio.control) >= 5)
        await self.engine.set_ptt(True)              # must wait for boot
        self.assertTrue(await start_task)
        await _wait_until(lambda: len(self._no_hb()) >= 29)  # full delivery
        self.assertTrue(self.engine.ptt_requested)
        cmds = self._no_hb()
        self.assertEqual(cmds[:27].count(sdp.CmdID.START_OPS), 1)
        self.assertEqual(cmds[27:29], [sdp.CmdID.DRIVE, sdp.CmdID.PTT])
        ptt = [d for _, d in self.radio.control
               if _ctrl_cmd(d) == sdp.CmdID.PTT][-1]
        self.assertEqual(_ctrl_trailing(ptt), 1)

    async def test_ptt_false_release_bypasses_boot_lock(self):
        engine = self.make_engine(boot_spacing_s=0.01, heartbeat_s=0.5)
        await self.engine.stop()
        self.engine = engine
        # clear the previous engine's boot log — otherwise the wait below
        # passes on stale packets before the new boot even begins
        self.radio.control.clear()
        start_task = asyncio.create_task(self.engine.start())
        await _wait_until(lambda: len(self.radio.control) >= 5)
        t0 = time.monotonic()
        await asyncio.wait_for(self.engine.set_ptt(False), timeout=0.2)
        self.assertLess(time.monotonic() - t0, 0.2)  # not queued behind boot
        self.assertTrue(await start_task)
        cmds = self._no_hb()
        ptt_idx = cmds.index(sdp.CmdID.PTT)
        self.assertLess(ptt_idx, 27)   # landed inside the boot, not after


# ── I4: no SO_REUSEADDR — a second bind of the same ports fails ───

class ReuseConflictTests(EngineCase):
    async def test_second_engine_bind_conflict_fails_first_survives(self):
        probes = []
        for _ in range(2):
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.bind(("127.0.0.1", 0))
            probes.append(s)
        cport = probes[0].getsockname()[1]
        iport = probes[1].getsockname()[1]
        for s in probes:
            s.close()
        first = self.make_engine(bind_control_port=cport, bind_iq_port=iport)
        await self.engine.stop()
        self.engine = first
        await self.start_engine()
        second = self.make_engine(bind_control_port=cport, bind_iq_port=iport)
        with self.assertLogs("backends.sunsdr.engine", level="ERROR"):
            self.assertFalse(await second.start())
        self.assertFalse(second.connected)
        # the first engine's stream is untouched by the failed hijack
        n = len(self.radio.stream)
        await asyncio.sleep(0.1)
        self.assertGreater(len(self.radio.stream), n)
        await second.stop()


# ── small quality points ──────────────────────────────────────────

class RxDropTests(EngineCase):
    async def test_rx_audio_full_drops_oldest_and_counts(self):
        engine = self.make_engine(rx_audio_max=4)
        await self.engine.stop()
        self.engine = engine
        await self.start_engine()
        await _wait_until(lambda: len(self.radio.stream) > 0)
        # count real demod appends — the assembler may consume up to 199
        # samples past the block edge, so no closed-form from recv counts
        demod = self.engine.demodulator
        real = demod.demodulate
        appends = {"n": 0}

        def counting(block):
            out = real(block)
            if out:
                appends["n"] += 1
            return out

        demod.demodulate = counting
        await self._pump_tone(260_000)
        # wait for the pump backlog to settle: recv stable across a window
        # also guarantees the last demod block (≤3 ms) has been appended
        last_recv = -1
        while True:
            await asyncio.sleep(0.15)
            if self.engine.recv_packets == last_recv:
                break
            last_recv = self.engine.recv_packets
        self.assertEqual(len(self.engine.rx_audio), 4)
        self.assertEqual(self.engine.rx_drops, appends["n"] - 4)


class PttConfirmGateTests(EngineCase):
    def _ptt_states(self):
        return [_ctrl_trailing(d) for _, d in self.radio.control
                if _ctrl_cmd(d) == sdp.CmdID.PTT]

    async def test_unconfirmed_ptt_on_is_resent_once(self):
        # A lost PTT1 datagram used to leave the UI showing TX with a cold
        # PA forever.  The reference server re-sends it once after 2 s; the
        # same bounded retry now lives in the engine.
        await self.engine.stop()
        self.engine = self.make_engine(ptt_confirm_timeout_s=0.05,
                                       ptt_confirm_retries=1)
        await self.start_engine()
        self.radio.control.clear()
        self.assertTrue(await self.engine.set_ptt(True))
        await _wait_until(lambda: self._ptt_states().count(1) >= 2,
                          timeout=1.5)
        await asyncio.sleep(0.2)
        self.assertEqual(self._ptt_states().count(1), 2)
        self.assertTrue(self.engine.ptt_requested)
        self.assertFalse(self.engine.ptt_confirmed)
        await self.engine.set_ptt(False)

    async def test_confirmed_ptt_on_is_not_resent(self):
        await self.engine.stop()
        self.engine = self.make_engine(ptt_confirm_timeout_s=0.2,
                                       ptt_confirm_retries=1)
        await self.start_engine()
        self.radio.control.clear()
        await self.engine.set_ptt(True)
        self.radio.send_ptt_status(True)
        await _wait_until(lambda: self.engine.ptt_confirmed)
        await asyncio.sleep(0.35)
        self.assertEqual(self._ptt_states().count(1), 1)
        await self.engine.set_ptt(False)

    async def test_release_cancels_pending_ptt_retry(self):
        await self.engine.stop()
        self.engine = self.make_engine(ptt_confirm_timeout_s=0.05,
                                       ptt_confirm_retries=1)
        await self.start_engine()
        self.radio.control.clear()
        await self.engine.set_ptt(True)
        await self.engine.set_ptt(False)
        await asyncio.sleep(0.3)
        states = self._ptt_states()
        self.assertEqual(states.count(1), 1)
        self.assertGreaterEqual(states.count(0), 1)
        self.assertFalse(self.engine.ptt_requested)

    async def test_stale_confirm_cannot_relight_after_release(self):
        await self.start_engine()
        await _wait_until(lambda: len(self.radio.stream) > 0)
        await self.engine.set_ptt(True)
        self.radio.send_ptt_status(True)
        await _wait_until(lambda: self.engine.ptt_confirmed)
        await self.engine.set_ptt(False)
        self.assertFalse(self.engine.ptt_confirmed)
        self.radio.send_ptt_status(True)     # stale late TX frame
        await asyncio.sleep(0.1)
        self.assertFalse(self.engine.ptt_confirmed)   # must not re-light
        self.radio.send_ptt_status(False)    # false is always accepted
        await asyncio.sleep(0.1)
        self.assertFalse(self.engine.ptt_confirmed)
        # a genuine new transmission still confirms normally
        await self.engine.set_ptt(True)
        self.radio.send_ptt_status(True)
        await _wait_until(lambda: self.engine.ptt_confirmed)


if __name__ == "__main__":
    unittest.main()
