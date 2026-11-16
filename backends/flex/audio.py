"""Audio adapters for the FlexRadio backend: 24 kHz float stereo ↔ 48 kHz PCM.

The box's audio contract is 48 kHz int16 **mono**, 20 ms blocks (AD-004/AD-011),
and every consumer — browser, recording, FT8 tap, local speaker — assumes it.
The radio speaks 24 kHz float32 **stereo** in (design §5.1) and forces Opus
24 kHz stereo out (§5.2), so this module owns the two conversions and nothing
else:

    RX: 24 kHz float stereo → mono → half-band 2× → 48 kHz int16
    TX: 48 kHz int16 mono → half-band ½ × → 24 kHz stereo int16 → Opus → VITA

Downmixing to mono drops ``slice audio_pan`` left/right placement; that is a
documented product trade-off (design §5.1), not an oversight.

The resamplers are streaming: filter state and the partial frame carry across
calls, so a 24 kHz stream keeps an exact 2:1 sample count over hours.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Optional

import numpy as np

from backends.flex import vita
from backends.flex.engine import RX_SAMPLE_RATE, SmartSDREngine
from opus_rx import (
    AUDIO_TAG_OPUS,
    AUDIO_TAG_PCM,
    DEFAULT_BITRATE,
    RX_RATE,
    RxOpusEncoder,
    TxOpusEncoder,
)

logger = logging.getLogger(__name__)

__all__ = ["FlexAudioHandler", "HalfBandResampler", "design_taps"]

RX_CHUNK_MS = 20
DEVICE_RATE = RX_RATE                       # 48 kHz, the box's contract
RX_CHUNK_SAMPLES = DEVICE_RATE * RX_CHUNK_MS // 1000      # 960
RX_CHUNK_BYTES = RX_CHUNK_SAMPLES * 2
DEVICE_INPUT_RATE = RX_SAMPLE_RATE           # 24 kHz from the radio
DEVICE_INPUT_BLOCK = DEVICE_INPUT_RATE * RX_CHUNK_MS // 1000   # 480
DEFAULT_TAPS = 31
_THREAD_CALL_TIMEOUT_S = 5.0


def design_taps(taps: int = DEFAULT_TAPS, cutoff: float = 0.25) -> np.ndarray:
    """Hamming-windowed sinc low-pass, normalized to unity DC gain.

    ``cutoff`` is in cycles/sample: 0.25 puts the corner at half of the input
    Nyquist, which is exactly right for both directions of a 2:1 conversion.
    """
    n = np.arange(taps) - (taps - 1) / 2.0
    h = 2.0 * cutoff * np.sinc(2.0 * cutoff * n) * np.hamming(taps)
    total = h.sum()
    if total == 0:
        raise ValueError("degenerate filter design")
    return h / total


class HalfBandResampler:
    """Streaming 2× up / ½× down resampler with carried filter history.

    ``up=2, down=1`` interpolates; ``up=1, down=2`` decimates (with the
    anti-alias filter applied *before* the downsample).
    """

    def __init__(self, up: int = 2, down: int = 1, taps: int = DEFAULT_TAPS):
        if up <= 0 or down <= 0:
            raise ValueError("up and down must be positive")
        self._up = int(up)
        self._down = int(down)
        self._h = design_taps(taps) * self._up
        self._history = np.zeros(max(0, self._h.size - 1), dtype=np.float64)

    @property
    def ratio(self) -> float:
        return self._up / self._down

    def process(self, samples) -> np.ndarray:
        x = np.asarray(samples, dtype=np.float64).ravel()
        if x.size == 0:
            return np.zeros(0, dtype=np.float64)
        if self._up > 1:
            spread = np.zeros(x.size * self._up, dtype=np.float64)
            spread[::self._up] = x
        else:
            spread = x
        buffer = np.concatenate((self._history, spread))
        filtered = np.convolve(buffer, self._h, mode="full")
        aligned = filtered[self._h.size - 1:self._h.size - 1 + spread.size]
        out = aligned[::self._down]
        self._history = buffer[-(self._h.size - 1):] if self._h.size > 1 else \
            np.zeros(0, dtype=np.float64)
        return out

    def reset(self) -> None:
        self._history = np.zeros(max(0, self._h.size - 1), dtype=np.float64)


class FlexAudioHandler:
    """``AudioHandler``-compatible surface over a ``SmartSDREngine``.

    Same method names, same meanings as ``audio_handler.AudioHandler`` and
    ``backends.sunsdr.audio.SunSDRAudioHandler``: ``server.py`` drives one
    uniform surface and never learns which radio is behind it.
    """

    def __init__(self, engine: SmartSDREngine, *,
                 loop: Optional[asyncio.AbstractEventLoop] = None,
                 bitrate: int = DEFAULT_BITRATE) -> None:
        self._engine = engine
        if loop is not None:
            self._loop = loop
        else:
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                self._loop = None

        self._rx_dev_rate = DEVICE_RATE
        self._tx_dev_rate = DEVICE_RATE
        self._rx_chunk = RX_CHUNK_SAMPLES
        self._up2 = HalfBandResampler(up=2, down=1)
        self._down2 = HalfBandResampler(up=1, down=2)
        self._rx_pcm = bytearray()
        self._rx_running = False
        self._closed = False

        self._rx_zero_since: Optional[float] = None
        self._rx_last_peak = 0

        self._tx_lock = threading.Lock()
        self._tx_written = 0
        self._tx_write_err = 0
        self._tx_peak = 0
        self._tx_encoder: Optional[TxOpusEncoder] = None
        try:
            self._tx_encoder = TxOpusEncoder()
        except Exception as exc:                        # noqa: BLE001
            logger.warning("FlexRadio TX Opus encoder unavailable: %s", exc)

        self.opus_enabled = True
        self.opus_encoder: Optional[RxOpusEncoder] = None
        try:
            self.opus_encoder = RxOpusEncoder(bitrate=bitrate)
        except Exception as exc:                        # noqa: BLE001
            logger.warning("Opus encoder unavailable, RX stays on Int16 PCM: %s",
                           exc)
            self.opus_enabled = False

    # ── thread bridge ────────────────────────────────────────────

    def _run(self, coro):
        loop = self._loop
        if loop is None:
            raise RuntimeError(
                "FlexAudioHandler was constructed outside the server event "
                "loop and no loop= was supplied")
        return asyncio.run_coroutine_threadsafe(
            coro, loop).result(_THREAD_CALL_TIMEOUT_S)

    # ── RX ───────────────────────────────────────────────────────

    def start_rx(self) -> bool:
        if self._closed:
            return False
        self._rx_running = True
        return True

    def stop_rx(self) -> None:
        self._rx_running = False

    def restart_rx(self) -> None:
        """Drop audio buffered during the over so post-unkey starts fresh."""
        self._engine.rx_audio.clear()
        self._engine.rx_samples = 0
        self._rx_pcm.clear()
        self._up2.reset()

    def read_rx_chunk(self) -> Optional[bytes]:
        """Every complete 20 ms block buffered right now, as 48 kHz int16 PCM.

        Returning exactly **one** block per call gave this consumer a duty cycle
        of exactly 100 %: the audio loop pulls one 20 ms chunk per tick while the
        radio pushes one second of audio per second, and the engine's queue is
        capped at one second.  With no headroom, a standing backlog built up and
        every stall was paid for at the producer's end.  Measured on the field box
        (`ba1rb`, 2026-11-16): the FT8 RX tap re-anchored its wall clock by 0.5 s
        every ~3 s, and the browser stream carried ~0.5 s of extra latency that
        never drained.

        The Opus encoder takes any size (``RxOpusEncoder`` keeps its own 20 ms
        frame accumulator), the recorder and the tap already handle arbitrary
        blocks, so draining everything is safe everywhere.
        """
        if not self._rx_running or self._closed:
            return None
        queue = self._engine.rx_audio
        while queue:
            samples = queue.popleft()
            self._engine.rx_samples -= samples.size
            upsampled = self._up2.process(samples)
            if upsampled.size:
                pcm = np.clip(np.rint(upsampled * 32768.0), -32768, 32767)
                self._rx_pcm.extend(pcm.astype("<i2").tobytes())
        usable = len(self._rx_pcm) // RX_CHUNK_BYTES * RX_CHUNK_BYTES
        if not usable:
            return None
        chunk = bytes(self._rx_pcm[:usable])
        del self._rx_pcm[:usable]
        return chunk

    def note_rx_chunk(self, pcm: Optional[bytes]) -> None:
        peak = 0
        if pcm:
            arr = np.frombuffer(pcm, dtype=np.int16)
            if arr.size:
                peak = int(np.abs(arr.astype(np.int32)).max())
        self._rx_last_peak = peak
        if peak > 0:
            self._rx_zero_since = None
        elif self._rx_zero_since is None:
            self._rx_zero_since = time.monotonic()

    def rx_silent_seconds(self) -> float:
        if self._rx_zero_since is None:
            return 0.0
        return time.monotonic() - self._rx_zero_since

    def encode_rx_audio(self, pcm: bytes) -> list[bytes]:
        if len(pcm) < 64:
            return []
        if self.opus_enabled and self.opus_encoder is not None:
            try:
                return [bytes([AUDIO_TAG_OPUS]) + pkt
                        for pkt in self.opus_encoder.push(pcm)]
            except Exception as exc:                    # noqa: BLE001
                logger.warning("Opus encode failed, sending PCM: %s", exc)
                return [bytes([AUDIO_TAG_PCM]) + pcm]
        return [bytes([AUDIO_TAG_PCM]) + pcm]

    # ── TX ───────────────────────────────────────────────────────

    def start_tx(self) -> bool:
        if self._closed or not self._engine.connected:
            return False
        if not self._engine.tx_stream_ready or self._tx_encoder is None:
            logger.warning("FlexRadio TX refused: no remote_audio_tx stream or "
                           "no Opus encoder")
            return False
        with self._tx_lock:
            self._tx_written = 0
            self._tx_write_err = 0
            self._tx_peak = 0
        try:
            self._run(self._engine.start_tx())
        except Exception as exc:                        # noqa: BLE001
            logger.warning("FlexRadio TX audio start failed: %s", exc)
            return False
        return True

    def stop_tx(self, graceful: bool = False) -> None:
        if not self._engine.tx_active:
            return
        try:
            self._run(self._engine.stop_tx(graceful=graceful))
        except Exception as exc:                        # noqa: BLE001
            with self._tx_lock:
                self._tx_write_err += 1
            logger.warning("FlexRadio TX audio stop failed: %s", exc)

    def feed_tx_audio(self, pcm: bytes) -> None:
        """48 kHz int16 mono → 24 kHz stereo → Opus → VITA → TX pacer."""
        if self._closed or not pcm or len(pcm) < 2:
            return
        encoder = self._tx_encoder
        if encoder is None or not self._engine.tx_stream_ready:
            return
        mono = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2")
        decimated = self._down2.process(mono)
        if decimated.size == 0:
            return
        pcm24 = np.clip(np.rint(decimated), -32768, 32767).astype("<i2")
        stereo = np.repeat(pcm24, 2)               # interleaved L=R
        try:
            packets = encoder.push(stereo.tobytes())
        except Exception as exc:                        # noqa: BLE001
            with self._tx_lock:
                self._tx_write_err += 1
            logger.warning("FlexRadio TX Opus encode failed: %s", exc)
            return
        peak = int(np.abs(pcm24.astype(np.int32)).max()) if pcm24.size else 0
        for packet in packets:
            try:
                self._engine.feed_opus(
                    vita.build_opus_packet(self._engine.tx_stream_id, packet))
            except vita.VitaError as exc:
                with self._tx_lock:
                    self._tx_write_err += 1
                logger.debug("FlexRadio TX packet build failed: %s", exc)
                continue
            with self._tx_lock:
                self._tx_written += 1
                if peak > self._tx_peak:
                    self._tx_peak = peak

    def write_tx_chunk(self) -> None:
        """No-op by design: the engine's pacer owns wire timing."""
        return None

    # ── diagnostics ──────────────────────────────────────────────

    @property
    def device_rates(self) -> dict:
        return {"rx": self._rx_dev_rate, "tx": self._tx_dev_rate}

    def tx_stats(self) -> dict:
        with self._tx_lock:
            written, err, peak = self._tx_written, self._tx_write_err, self._tx_peak
        return {
            "written": written,
            "write_err": err,
            "peak": peak,
            "queued": len(self._engine._tx_queue),
            "queue_drops": self._engine.tx_drops,
        }

    def tx_queue_frames(self) -> int:
        return len(self._engine._tx_queue) if self._engine.tx_active else 0

    def has_pending_tx_audio(self) -> bool:
        return bool(self._engine.tx_active)

    # ── cleanup ──────────────────────────────────────────────────

    def close(self) -> None:
        self._closed = True
        self.stop_rx()
        if self._engine.tx_active:
            try:
                self._run(self._engine.stop_tx())
            except Exception:                          # noqa: BLE001
                pass
        for encoder in (self.opus_encoder, self._tx_encoder):
            if encoder is not None:
                try:
                    encoder.close()
                except Exception:                      # noqa: BLE001
                    pass
        self.opus_encoder = None
        self._tx_encoder = None
