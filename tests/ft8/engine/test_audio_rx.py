from __future__ import annotations

from types import SimpleNamespace

import numpy as np

from ft8.engine.audio_rx import (
    DECIMATION,
    DECODER_SAMPLE_RATE,
    RX_SAMPLE_RATE,
    AudioCapture,
    CaptureHealthMonitor,
    RxConverter,
    UtcRing,
)


def sine(freq_hz: float, n: int, rate: int = RX_SAMPLE_RATE) -> np.ndarray:
    t = np.arange(n) / rate
    return np.sin(2 * np.pi * freq_hz * t).astype(np.float32)


def test_converter_rate_and_block_size_independence() -> None:
    one_shot = RxConverter().push(sine(1_500.0, 48_000))
    blocked = RxConverter()
    parts = [
        blocked.push(sine(1_500.0, 48_000)[start : start + size])
        for start, size in [(0, 4_096), (4_096, 1_000), (5_096, 37), (5_133, 42_867)]
    ]
    streamed = np.concatenate(parts)
    assert abs(one_shot.size - 48_000 // DECIMATION) <= 1
    assert streamed.shape == one_shot.shape
    # Streaming lfilter + phase-consistent decimation is sample-exact.
    assert np.array_equal(streamed, one_shot)


def test_converter_preserves_passband_tone() -> None:
    pcm = RxConverter().push(sine(1_500.0, 48_000))
    spectrum = np.abs(np.fft.rfft(pcm.astype(np.float64)))
    peak_hz = float(np.argmax(spectrum)) * DECODER_SAMPLE_RATE / pcm.size
    assert abs(peak_hz - 1_500.0) < 5.0
    amplitude = 2.0 * spectrum[int(round(1_500.0 * pcm.size / DECODER_SAMPLE_RATE))] / pcm.size
    assert amplitude > 0.85 * 32_767  # FIR passband is essentially flat


def test_converter_rejects_aliased_tone() -> None:
    pcm = RxConverter().push(sine(9_000.0, 48_000))
    # Skip the startup transient; steady-state stopband attenuation ≳ 53 dB.
    assert np.max(np.abs(pcm[200:])) < 500


def test_converter_clips_to_int16() -> None:
    pcm = RxConverter().push(np.full(48_000, 2.0, dtype=np.float32))
    assert pcm.max() == 32_767
    pcm = RxConverter().push(np.full(48_000, -2.0, dtype=np.float32))
    assert pcm.min() == -32_768


def test_ring_serves_exact_contiguous_slots() -> None:
    ring = UtcRing(seconds=60.0)
    slot0 = np.arange(180_000, dtype=np.int16)
    slot1 = np.full(180_000, -7, dtype=np.int16)
    ring.write(slot0, 0.0)
    ring.write(slot1, 15.0)

    assert ring.read_slot(0) == slot0.tobytes()
    assert ring.read_slot(1) == slot1.tobytes()
    assert ring.read_slot(2) is None  # not yet complete
    assert ring.metrics.gaps == 0


def test_ring_gap_invalidates_only_the_missing_span() -> None:
    ring = UtcRing(seconds=90.0)
    slot0 = np.full(180_000, 1, dtype=np.int16)
    slot2 = np.full(180_000, 3, dtype=np.int16)
    ring.write(slot0, 0.0)
    ring.write(slot2, 30.0)

    assert ring.read_slot(0) == slot0.tobytes()
    assert ring.read_slot(1) is None  # the gap
    assert ring.read_slot(2) == slot2.tobytes()
    assert ring.metrics.gaps == 1


def test_ring_evicts_old_slots_and_counts_late_writes() -> None:
    ring = UtcRing(seconds=20.0)  # 240,000 samples < two slots
    slot0 = np.full(180_000, 1, dtype=np.int16)
    slot1 = np.full(180_000, 2, dtype=np.int16)
    slot2 = np.full(180_000, 3, dtype=np.int16)
    ring.write(slot0, 0.0)
    ring.write(slot1, 15.0)
    ring.write(slot2, 30.0)

    assert ring.read_slot(0) is None  # evicted
    assert ring.read_slot(2) == slot2.tobytes()

    ring.write(np.zeros(100, dtype=np.int16), 0.0)  # arrives after eviction
    assert ring.metrics.dropped_samples == 100


def test_ring_slot_content_is_unaligned_after_eviction() -> None:
    """Regression (2026-08-03): eviction advances base but must not shift
    retained data.

    Physical positions are keyed to the absolute sample index (X % capacity),
    so advancing ``base`` never requires moving data.  The old code keyed
    positions to ``(X - base)``; after an eviction advanced ``base``, reads
    found content shifted by the cumulative base advance — the field "D":
    decode died ~60-75 s in (ring full), the shift grew 1:1 with wall clock
    and wrapped at capacity (repeating stale slots).  Constant-value slots
    hid it; a known pattern catches it.
    """

    ring = UtcRing(seconds=50.0)  # 600,000 samples: three slots + margin
    slot1 = np.full(180_000, 111, dtype="<i2")  # lands in the misread window
    slot2 = np.full(180_000, 222, dtype="<i2")
    slot3 = (np.arange(180_000) % 257).astype("<i2")  # the target: distinct pattern
    slot4 = np.full(180_000, 333, dtype="<i2")
    slot5 = np.full(180_000, 444, dtype="<i2")  # lands in the misread window

    ring.write(np.zeros(180_000, dtype="<i2"), 0.0)   # slot 0, evicted
    ring.write(slot1, 15.0)
    ring.write(slot2, 30.0)
    ring.write(slot3, 45.0)   # first eviction: base 0 -> 120,000
    ring.write(slot4, 60.0)   # second eviction: base 120,000 -> 300,000
    ring.write(slot5, 75.0)   # third eviction: base 300,000 -> 480,000

    # slot3 (written before the last two evictions) is still fully retained
    # and must decode to slot3's exact bytes — not 444/111 from later/earlier
    # slots pulled in by a base-shifted read.
    got = ring.read_slot(3)
    assert got is not None
    assert got == slot3.tobytes()


def test_read_slot_partial_pads_an_in_flight_tail() -> None:
    """Early decode: a slot whose silent tail is still in flight is served.

    An FT8 waveform ends at 12.64 s, so the last ~2.4 s of the slot carry no
    signal.  ``read_slot_partial`` accepts a slot that has at least
    ``min_samples`` in and zero-pads the rest; anything shorter is refused so
    the caller keeps polling.
    """

    ring = UtcRing(seconds=60.0)
    head = (np.arange(150_000) % 251).astype("<i2")   # 12.5 s of "signal"
    ring.write(head, 0.0)

    # 12.5 s in: enough for a 12.0 s minimum, padded to the full slot.
    got = ring.read_slot_partial(0, min_samples=144_000)
    assert got is not None
    assert len(got) == 360_000                        # 180,000 int16 samples
    assert got[:300_000] == head.tobytes()
    assert got[300_000:] == b"\x00" * 60_000          # zero-padded tail

    # Below the minimum the slot is still refused (caller keeps polling).
    assert ring.read_slot_partial(0, min_samples=150_001) is None
    # A slot that has not started yet is refused too.
    assert ring.read_slot_partial(1, min_samples=1) is None


def test_read_slot_partial_refuses_gap_overlap() -> None:
    """A gap inside the landed region must never be papered over with zeros."""

    ring = UtcRing(seconds=60.0)
    ring.write(np.full(12_000, 7, dtype="<i2"), 0.0)
    ring.write(np.full(12_000, 8, dtype="<i2"), 5.0)   # gap: base jumps to 60,000
    assert ring.read_slot_partial(0, min_samples=1) is None


def test_ring_tolerates_overlapping_rewrite() -> None:
    ring = UtcRing(seconds=60.0)
    block = np.full(12_000, 5, dtype=np.int16)
    ring.write(block, 0.0)
    ring.write(block, 0.0)  # duplicate delivery
    ring.write(block, 1.0)
    assert ring.metrics.gaps == 0


class FakeStream:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.callback = kwargs["callback"]
        self.started = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def close(self) -> None:
        self.closed = True


class FakeClock:
    def __init__(self) -> None:
        self.epoch = 0.0

    def __call__(self) -> float:
        return self.epoch


def test_capture_wires_stream_converter_and_ring() -> None:
    ring = UtcRing(seconds=60.0)
    clock = FakeClock()
    created: list[FakeStream] = []

    def factory(**kwargs: object) -> FakeStream:
        stream = FakeStream(**kwargs)
        created.append(stream)
        return stream

    capture = AudioCapture(
        ring, clock=clock, stream_factory=factory, blocksize=48_000
    )
    stream = created[0]
    assert stream.kwargs["samplerate"] == RX_SAMPLE_RATE
    assert stream.kwargs["channels"] == 1
    assert stream.kwargs["dtype"] == "int16"  # RX_DTYPE: UAC float32 path is unreliable

    capture.start()
    assert stream.started

    block = sine(1_500.0, 48_000).reshape(-1, 1)
    # One 48 kHz block per test second; 16 blocks cover slot 0 (abs 0..180000).
    for index in range(16):
        clock.epoch = float(index)
        status = SimpleNamespace(input_overflow=index == 3)
        stream.callback(block, 48_000, None, status)

    assert capture.overruns == 1
    assert ring.metrics.gaps == 0
    slot_bytes = ring.read_slot(0)
    assert slot_bytes is not None
    pcm = np.frombuffer(slot_bytes, dtype="<i2")
    spectrum = np.abs(np.fft.rfft(pcm.astype(np.float64)))
    peak_hz = float(np.argmax(spectrum)) * DECODER_SAMPLE_RATE / pcm.size
    assert abs(peak_hz - 1_500.0) < 5.0

    capture.stop()
    assert stream.closed
    capture.stop()  # idempotent


def test_capture_tolerates_real_callback_jitter() -> None:
    """±3 ms per-block clock jitter must not carve gaps (acceptance finding)."""

    ring = UtcRing(seconds=60.0)
    clock = FakeClock()
    created: list[FakeStream] = []

    def factory(**kwargs: object) -> FakeStream:
        stream = FakeStream(**kwargs)
        created.append(stream)
        return stream

    capture = AudioCapture(
        ring, clock=clock, stream_factory=factory, blocksize=48_000
    )
    capture.start()
    block = sine(1_500.0, 48_000).reshape(-1, 1)
    jitter = [0.0, 0.003, -0.002, 0.001, -0.003]
    for index in range(16):
        clock.epoch = float(index + 1) + jitter[index % len(jitter)]
        created[0].callback(block, 48_000, None, None)

    assert ring.metrics.gaps == 0
    assert ring.read_slot(0) is not None


def test_capture_reanchors_after_a_real_stall() -> None:
    """A stall beyond the threshold is re-anchored and recorded as one gap."""

    ring = UtcRing(seconds=60.0)
    clock = FakeClock()
    created: list[FakeStream] = []

    def factory(**kwargs: object) -> FakeStream:
        stream = FakeStream(**kwargs)
        created.append(stream)
        return stream

    capture = AudioCapture(
        ring, clock=clock, stream_factory=factory, blocksize=48_000
    )
    capture.start()
    block = sine(1_500.0, 48_000).reshape(-1, 1)
    for index in range(4):
        clock.epoch = float(index + 1)
        created[0].callback(block, 48_000, None, None)
    # Stream stalls for a second, then resumes: re-anchor, ring sees a gap.
    for index in range(4, 8):
        clock.epoch = float(index + 2)
        created[0].callback(block, 48_000, None, None)

    assert ring.metrics.gaps == 1
    assert ring.read_slot(0) is None  # intersects the stalled span


def test_capture_restart_recreates_stream() -> None:
    """A degraded capture session is healed by stop()+start(): the stream
    must be recreated through the factory and keep feeding the ring."""

    ring = UtcRing(seconds=60.0)
    clock = FakeClock()
    created: list[FakeStream] = []

    def factory(**kwargs: object) -> FakeStream:
        stream = FakeStream(**kwargs)
        created.append(stream)
        return stream

    capture = AudioCapture(
        ring, clock=clock, stream_factory=factory, blocksize=48_000
    )
    capture.start()
    capture.stop()
    assert created[0].closed

    capture.start()
    assert len(created) == 2
    assert created[1].started

    block = sine(1_500.0, 48_000).reshape(-1, 1)
    for index in range(16, 32):
        clock.epoch = float(index)
        created[1].callback(block, 48_000, None, None)
    assert ring.read_slot(1) is not None
    capture.stop()


def test_capture_health_monitor_ignores_cold_band() -> None:
    monitor = CaptureHealthMonitor(rms_threshold=1_000.0, streak=4)
    for _ in range(10):
        assert monitor.observe(300.0, 0) is False  # silent band: nothing to decode


def test_capture_health_monitor_ignores_healthy_decodes() -> None:
    monitor = CaptureHealthMonitor(rms_threshold=1_000.0, streak=4)
    for _ in range(10):
        assert monitor.observe(5_000.0, 3) is False


def test_capture_health_monitor_fires_once_per_episode() -> None:
    monitor = CaptureHealthMonitor(rms_threshold=1_000.0, streak=4)
    assert [monitor.observe(5_000.0, 0) for _ in range(3)] == [False] * 3
    assert monitor.observe(5_000.0, 0) is True   # 4th hot-but-silent slot
    assert monitor.observe(5_000.0, 0) is False  # edge, not every slot


def test_capture_health_monitor_resets_on_recovery() -> None:
    monitor = CaptureHealthMonitor(rms_threshold=1_000.0, streak=4)
    for _ in range(3):
        monitor.observe(5_000.0, 0)
    monitor.observe(5_000.0, 1)  # a decode: session healthy again
    assert [monitor.observe(5_000.0, 0) for _ in range(3)] == [False] * 3
    assert monitor.observe(5_000.0, 0) is True


def test_capture_health_monitor_threshold_boundary() -> None:
    monitor = CaptureHealthMonitor(rms_threshold=1_000.0, streak=2)
    assert monitor.observe(1_000.0, 0) is False  # at threshold is not hot
    assert monitor.observe(1_000.1, 0) is False
    assert monitor.observe(1_000.1, 0) is True


def test_capture_normalizes_int16_blocks() -> None:
    """int16 capture blocks are normalized to float32 inside the seam, so a
    full-scale int16 tone lands in the ring at the same amplitude as a
    float32 one."""

    ring_i = UtcRing(seconds=60.0)
    ring_f = UtcRing(seconds=60.0)
    clock = FakeClock()

    def factory(**kwargs: object) -> FakeStream:
        return FakeStream(**kwargs)

    cap_i = AudioCapture(ring_i, clock=clock, stream_factory=factory, blocksize=48_000)
    cap_f = AudioCapture(ring_f, clock=clock, stream_factory=factory, blocksize=48_000)

    tone = sine(1_500.0, 48_000)  # float32 in [-1, 1]
    block_i = np.rint(tone * 32_767).astype("<i2").reshape(-1, 1)
    block_f = tone.reshape(-1, 1)
    for index in range(16):
        clock.epoch = float(index)
        cap_i._on_block(block_i, 48_000, None, None)
        cap_f._on_block(block_f, 48_000, None, None)

    pcm_i = np.frombuffer(ring_i.read_slot(0), dtype="<i2")
    pcm_f = np.frombuffer(ring_f.read_slot(0), dtype="<i2")
    rms_i = float(np.sqrt(np.mean(pcm_i.astype(np.float64) ** 2)))
    rms_f = float(np.sqrt(np.mean(pcm_f.astype(np.float64) ** 2)))
    assert abs(rms_i - rms_f) / rms_f < 0.01


def test_capture_anchors_blocks_to_adc_hardware_time() -> None:
    """ADC timestamps place each block at its true epoch; a delivery stall
    becomes one recorded gap, never a permanent time shift (2026-08-03
    field finding: backlog mis-anchored to wall time shifted every later
    slot by seconds and killed all decodes)."""

    ring = UtcRing(seconds=60.0)
    clock = FakeClock()
    clock.epoch = 1_000.0
    created: list[FakeStream] = []

    def factory(**kwargs: object) -> FakeStream:
        stream = FakeStream(**kwargs)
        created.append(stream)
        return stream

    capture = AudioCapture(ring, clock=clock, stream_factory=factory, blocksize=48_000)
    stream = created[0]
    block = sine(1_500.0, 48_000).reshape(-1, 1)

    def adc_time_info(adc: float) -> SimpleNamespace:
        return SimpleNamespace(inputBufferAdcTime=adc, currentTime=adc + 0.09)

    # Two 1 s blocks captured at wall epochs 999.91/999.91+1 — the anchor is
    # offset = clock - currentTime, so epoch = adc + offset.
    stream.callback(block, 48_000, adc_time_info(500.0), None)
    stream.callback(block, 48_000, adc_time_info(501.0), None)
    assert ring.metrics.gaps == 0
    first_base = ring.base
    assert first_base == round((500.0 + (1_000.0 - 500.09)) * 12_000)

    # A 5 s delivery stall (wall clock advances with it): the post-stall
    # block must land 5 s later, leaving exactly one gap — not a shift of
    # the stall's backlog.
    clock.epoch = 1_005.0
    stream.callback(block, 48_000, adc_time_info(506.0), None)
    assert ring.metrics.gaps == 1
    assert ring.high_water == first_base + 5 * 12_000 + 12_000


def test_capture_resolves_device_name_to_index(monkeypatch) -> None:
    """Name-opened CoreAudio streams die ~60-90 s in on the FT-710 UAC
    (2026-08-03 A/B); names must be resolved to indexes before opening."""

    import sys

    fake_sd = SimpleNamespace(
        InputStream=lambda **kwargs: FakeStream(**kwargs),
        query_devices=lambda name: {"index": 4},
    )
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)
    capture = AudioCapture(UtcRing(), device="USB Audio Device")
    assert capture._stream_kwargs["device"] == 4


def test_audio_capture_channel_selects_stereo_slice(monkeypatch) -> None:
    """channel=1 时开 2 声道并切右声道；channel=0 保持单声道（原行为）。"""

    from ft8.engine.audio_rx import AudioCapture
    from ft8.engine.audio_rx import UtcRing

    class FakeStream:
        instances: list = []

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            FakeStream.instances.append(self)

        def start(self): ...
        def stop(self): ...

    stream_factory = FakeStream
    ring = UtcRing()

    AudioCapture(ring, stream_factory=stream_factory, channel=1)
    assert FakeStream.instances[-1].kwargs["channels"] == 2

    AudioCapture(ring, stream_factory=stream_factory, channel=0)
    assert FakeStream.instances[-1].kwargs["channels"] == 1
