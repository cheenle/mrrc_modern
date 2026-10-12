"""48 kHz receive path: one conversion, UTC ring and capture glue (AD-004).

``RxConverter`` performs the single allowed 48 kHz float32 → 12 kHz int16
mono conversion as a streaming FIR anti-alias filter plus 4:1 decimation, so
arbitrary block boundaries never lose or duplicate samples.  ``UtcRing``
indexes the converted stream by absolute UTC sample number and serves exact
180,000-sample slots, returning ``None`` on gaps, underruns and not-yet
complete slots.  ``AudioCapture`` is the thin sounddevice seam; the stream
factory is injectable so tests stay hardware-free.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

import logging

import numpy as np
import scipy.signal

_audio_log = logging.getLogger("mrrc-ft8.audio")

RX_SAMPLE_RATE = 48_000
DECODER_SAMPLE_RATE = 12_000
DECIMATION = RX_SAMPLE_RATE // DECODER_SAMPLE_RATE
SLOT_SAMPLES = 180_000  # one 15 s FT8 slot at 12 kHz
ANTI_ALIAS_CUTOFF_HZ = 5_400.0
FIR_TAPS = 97
DEFAULT_RING_SECONDS = 60.0
RESYNC_THRESHOLD_SECONDS = 0.25
"""Wall-clock drift beyond this re-anchors the sample-count epoch chain."""

RX_DTYPE = "int16"
"""Capture sample format.  Field finding (2026-08-02/03, FT-710 UAC):
the device's CoreAudio float32 path intermittently delivers toneless
noise (band content lost) while int16 always delivers the real band —
every healthy capture today was int16, every degraded one float32."""


class RxConverter:
    """Streaming 48 kHz → 12 kHz converter; the only RX conversion (AD-004).

    The FIR group delay is constant ((taps-1)/2 input samples ≈ 1 ms) and
    applies uniformly to all audio, so UTC slot alignment is unaffected.
    """

    def __init__(self, taps: int = FIR_TAPS, cutoff_hz: float = ANTI_ALIAS_CUTOFF_HZ) -> None:
        self._fir = scipy.signal.firwin(taps, cutoff_hz / (RX_SAMPLE_RATE / 2))
        self._zi = scipy.signal.lfilter_zi(self._fir, 1.0) * 0.0
        self._consumed = 0  # absolute filtered input samples so far

    def push(self, pcm: np.ndarray) -> np.ndarray:
        """Convert one 48 kHz float32 block; returns 12 kHz int16 samples."""

        block = np.asarray(pcm, dtype=np.float32)
        filtered, self._zi = scipy.signal.lfilter(self._fir, 1.0, block, zi=self._zi)
        first = (-self._consumed) % DECIMATION
        self._consumed += filtered.size
        decimated = filtered[first::DECIMATION]
        return np.clip(np.rint(decimated * 32_767.0), -32_768, 32_767).astype("<i2")


@dataclass(slots=True)
class RingMetrics:
    """Loss accounting for the receive ring."""

    dropped_samples: int = 0  # arrived after eviction (overrun)
    gaps: int = 0             # discontinuous writes (lost audio upstream)


class UtcRing:
    """UTC-indexed contiguous 12 kHz int16 ring serving exact slots.

    Absolute sample index ``floor(epoch * 12000)`` defines position; a slot is
    readable only while its whole range lies inside ``[base, high_water)`` and
    intersects no recorded gap range (AD-006 discipline applied to audio).
    """

    def __init__(
        self,
        seconds: float = DEFAULT_RING_SECONDS,
        *,
        rate: int = DECODER_SAMPLE_RATE,
        slot_samples: int = SLOT_SAMPLES,
    ) -> None:
        if seconds <= 0 or slot_samples <= 0:
            raise ValueError("ring capacity and slot size must be positive")
        self._rate = rate
        self._slot_samples = slot_samples
        self._capacity = int(seconds * rate)
        self._buffer = np.zeros(self._capacity, dtype="<i2")
        self._base: int | None = None      # oldest retained absolute index
        self._high_water = 0               # absolute contiguous end (exclusive)
        self._gap_ranges: list[tuple[int, int]] = []
        self.metrics = RingMetrics()

    @property
    def base(self) -> int | None:
        """Oldest retained absolute sample index (None before first write)."""

        return self._base

    @property
    def high_water(self) -> int:
        """Absolute contiguous end (exclusive) of the written stream."""

        return self._high_water

    @property
    def gap_count(self) -> int:
        """Number of recorded gap ranges (lost audio upstream)."""

        return len(self._gap_ranges)

    def write(self, samples: np.ndarray, first_epoch: float) -> None:
        """Append converted samples whose first sample sits at first_epoch."""

        data = np.asarray(samples, dtype="<i2")
        if data.size == 0:
            return
        first = round(first_epoch * self._rate)
        # Nearest-sample quantization: a sample-count-anchored epoch chain
        # lands ±1 ulp either side of the integer (0.003 s → 35.999…/36.000…);
        # truncating would alias those into one-sample drops and gaps.
        if self._base is None:
            self._base = self._high_water = first

        end = first + data.size
        if end <= self._base:
            self.metrics.dropped_samples += data.size
            return
        if first > self._high_water:
            # Upstream lost audio between high_water and first.
            self.metrics.gaps += 1
            self._gap_ranges.append((self._high_water, first))
        start = max(first, self._high_water, self._base)
        if start > first:
            self.metrics.dropped_samples += start - first
        chunk = data[start - first :]
        new_high = start + chunk.size
        if new_high - self._base > self._capacity:
            evict_to = new_high - self._capacity
            self._base = evict_to
            self._gap_ranges = [
                (max(gap_start, evict_to), gap_end)
                for gap_start, gap_end in self._gap_ranges
                if gap_end > evict_to
            ]
        # Physical position is keyed to the absolute sample index, never to
        # the moving ``base``: advancing base on eviction must not require
        # moving data, or every later read finds content shifted by the
        # cumulative base advance (field bug 2026-08-03, "D").  ``base`` is
        # only a bounds/gap-check boundary; the window [base, new_high) is
        # ≤ capacity, so (X % capacity) over it is a bijection.
        positions = np.arange(start, start + chunk.size) % self._capacity
        self._buffer[positions] = chunk
        self._high_water = new_high

    def read_slot(self, slot_id: int) -> bytes | None:
        """Return the exact slot bytes, or None on gap/underrun/incomplete."""

        if self._base is None:
            return None
        first = slot_id * self._slot_samples
        last = first + self._slot_samples
        if first < self._base or last > self._high_water:
            return None
        for gap_start, gap_end in self._gap_ranges:
            if first < gap_end and last > gap_start:
                return None
        # Same absolute-index keying as write(); see there for why this must
        # not subtract base.
        positions = np.arange(first, first + self._slot_samples) % self._capacity
        return self._buffer[positions].tobytes()


class AudioCapture:
    """48 kHz mono input stream feeding the converter and UTC ring.

    Captures int16 (see ``RX_DTYPE``) and normalizes to float32 inside the
    seam, so the converter contract below stays float32 in / 12 kHz int16
    out.  ``stream_factory`` defaults to ``sounddevice.InputStream``
    (imported lazily so hardware-free hosts can use the pure components);
    tests inject a fake.  ``clock`` supplies UTC epoch seconds for ring
    indexing.
    """

    def __init__(
        self,
        ring: UtcRing,
        converter: RxConverter | None = None,
        *,
        clock: Callable[[], float] = time.time,
        stream_factory: Callable[..., object] | None = None,
        device: int | str | None = None,
        blocksize: int = 4_096,
        channel: int = 0,
        tap: Callable[[np.ndarray, float], None] | None = None,
    ) -> None:
        if stream_factory is None:
            import sounddevice

            stream_factory = sounddevice.InputStream
            if isinstance(device, str):
                # Resolve name → index once here.  Field finding 2026-08-03:
                # on this Mac (FT-710 UAC), a stream opened *by name* dies
                # ~60-90 s in (content goes stale, decodes die) while the
                # same device opened *by index* runs for hours — reproduced
                # A/B within the same minute.  The morning-healthy server
                # used device=None (index path); the curse began when .env
                # started passing the name.
                device = sounddevice.query_devices(device)["index"]
        self._ring = ring
        self._converter = converter or RxConverter()
        self._clock = clock
        self._tap = tap
        self.overruns = 0
        self._next_epoch: float | None = None  # sample-count-anchored continuity
        self._prev_adc: float | None = None    # ADC-timestamped previous block
        self._adc_offset: float | None = None  # wall↔host clock calibration
        self._stream_factory = stream_factory
        self._channel = int(channel) if channel else 0
        # 电台 RX 音频可能只在立体声输入的某一侧；channels=1 固定取第 0 声道。
        # channel=1 时开 2 声道再切片；channels=1 时保持原行为（更省）。
        in_channels = 2 if self._channel >= 1 else 1
        self._stream_kwargs = dict(
            samplerate=RX_SAMPLE_RATE,
            channels=in_channels,
            dtype=RX_DTYPE,
            blocksize=blocksize,
            device=device,
            callback=self._on_block,
        )
        self._stream = self._stream_factory(**self._stream_kwargs)

    def _on_block(
        self, indata: np.ndarray, frames: int, time_info: object, status: object
    ) -> None:
        if status and getattr(status, "input_overflow", False):
            self.overruns += 1
            _audio_log.debug("input overflow #%d (%d frames)", self.overruns, frames)
        block_epoch = self._block_epoch(frames, time_info)
        if self._channel >= 1 and indata.ndim >= 2 and indata.shape[1] >= 2:
            pcm = indata[:, self._channel]
        else:
            pcm = indata[:, 0]
        if pcm.dtype != np.float32:
            # int16 capture (RX_DTYPE): normalize once inside the seam so the
            # converter contract (float32 → 12 kHz int16) is unchanged.
            pcm = pcm.astype(np.float32) / 32_768.0
        converted = self._converter.push(pcm)
        self._ring.write(converted, block_epoch)
        if self._tap is not None:
            self._tap(converted, block_epoch)  # waterfall feed (§11.2)

    def _block_epoch(self, frames: int, time_info: object) -> float:
        """Wall epoch of the block's first sample.

        Primary path: CoreAudio's ADC hardware timestamp (first sample),
        placed on the wall clock via the callback's own currentTime.  The
        hardware timestamp travels with the audio, so a delivery stall
        simply leaves a recorded gap in the ring — it can never stamp
        backlog as live audio and permanently time-shift every later slot
        (2026-08-03 field finding that killed decodes for hours).
        Fallback for hosts/tests without ADC timestamps: the legacy
        sample-count chain anchored to wall-clock reads, re-anchoring
        after genuine stalls.
        """

        adc = getattr(time_info, "inputBufferAdcTime", None) if time_info else None
        current = getattr(time_info, "currentTime", None) if time_info else None
        if adc and current and adc > 0 and current > 0:
            if self._adc_offset is None:
                self._adc_offset = self._clock() - current
            elif abs(self._clock() - current - self._adc_offset) > RESYNC_THRESHOLD_SECONDS:
                _audio_log.warning(
                    "wall-clock step vs ADC: re-calibrating offset (%.3f s)",
                    self._clock() - current - self._adc_offset,
                )
                self._adc_offset = self._clock() - current
            adc_epoch = adc + self._adc_offset
            if self._prev_adc is not None and adc_epoch - self._prev_adc > RESYNC_THRESHOLD_SECONDS:
                _audio_log.warning(
                    "input stall: %.3f s of audio lost before this block",
                    adc_epoch - self._prev_adc,
                )
            self._prev_adc = adc_epoch
            # Jitter-free sample-count chain, re-anchored only when the ADC
            # truth disagrees: per-block ADC jitter stays out of the ring,
            # while a stall/backlog re-anchors the chain to where the audio
            # actually is — backlog can never be stamped as live.
            if (
                self._next_epoch is None
                or abs(adc_epoch - self._next_epoch) > RESYNC_THRESHOLD_SECONDS
            ):
                self._next_epoch = adc_epoch
            epoch = self._next_epoch
            self._next_epoch += frames / RX_SAMPLE_RATE
            return epoch
        return self._wall_chain_epoch(frames)

    def _wall_chain_epoch(self, frames: int) -> float:
        """Legacy anchor: sample-count chain; wall clock only re-anchors."""

        block_start = self._clock() - frames / RX_SAMPLE_RATE
        if (
            self._next_epoch is None
            or abs(block_start - self._next_epoch) > RESYNC_THRESHOLD_SECONDS
        ):
            if self._next_epoch is not None:
                # A stall means the delivered block may be PortAudio backlog
                # mis-anchored to now; make the event visible (field finding:
                # stale-audio sessions decode the same station for minutes).
                _audio_log.warning(
                    "capture re-anchor: drift %.3f s after %d frames",
                    block_start - self._next_epoch,
                    frames,
                )
            self._next_epoch = block_start
        block_epoch = self._next_epoch
        self._next_epoch += frames / RX_SAMPLE_RATE
        return block_epoch

    def start(self) -> None:
        """Start the input stream, recreating it after a :meth:`stop`.

        Field finding (2026-08-02): a USB audio session can silently degrade
        (time-shifted, starved or looping content with healthy ring metrics)
        and never recovers, while a freshly opened stream on the same device
        is always clean.  Recreating the stream here is what makes the
        monitor-state bounce in the composition layer possible.
        """

        if self._stream is None:
            self._stream = self._stream_factory(**self._stream_kwargs)
        self._stream.start()  # type: ignore[attr-defined]

    def stop(self) -> None:
        """Stop and close the input stream; idempotent."""

        if self._stream is not None:
            self._stream.stop()  # type: ignore[attr-defined]
            self._stream.close()  # type: ignore[attr-defined]
            self._stream = None
            self._next_epoch = None  # re-anchor on the next start
            self._prev_adc = None
            self._adc_offset = None


class CaptureHealthMonitor:
    """Hot-band-but-zero-decode streak detector for degraded capture sessions.

    A live FT8 band shows slot audio well above the receiver noise floor;
    a healthy decoder then finds messages within a slot or two.  A degraded
    capture session (time-shifted / starved / looping content) keeps the
    level high yet decodes nothing, indefinitely.  ``streak`` consecutive
    hot, message-less slots raise one edge per episode; any decoded message
    or cold slot resets the detector.
    """

    def __init__(self, *, rms_threshold: float = 1_000.0, streak: int = 4) -> None:
        if rms_threshold <= 0 or streak <= 0:
            raise ValueError("rms threshold and streak must be positive")
        self._rms_threshold = rms_threshold
        self._streak = streak
        self._hot_silent = 0
        self._triggered = False

    def observe(self, rms: float, messages: int) -> bool:
        """Feed one slot's audio RMS and decode count; True on the edge."""

        if messages > 0 or rms <= self._rms_threshold:
            self._hot_silent = 0
            self._triggered = False
            return False
        self._hot_silent += 1
        if self._hot_silent >= self._streak and not self._triggered:
            self._triggered = True
            return True
        return False
