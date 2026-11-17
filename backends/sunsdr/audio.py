"""SunSDR2 DX network audio adapter
==================================
Implements the exact ``audio_handler.AudioHandler`` surface the server
drives, over the shared Task-3 engine instead of a PyAudio sound card:

- RX: 48 kHz int16-le PCM is popped from the engine's bounded ``rx_audio``
  deque (fed by the IQ demodulator whether or not anyone reads it).  The
  codec domain IS the device domain — no resample step exists here.
- TX: browser/CQ/FT8 PCM feeds ``engine.feed_tx_audio``; the engine's own
  pacer task writes 0xFFFD IQ packets at the measured device pace, so
  ``write_tx_chunk`` is a deliberate no-op (the drain loop may keep calling
  it — the uniform surface costs nothing).  The uplink says what rate it is
  (AD-038): legacy 48 kHz goes through untouched, a negotiated PCM16 socket is
  streaming-resampled 16 → 48 kHz here first, because the modulator and the
  device pacer are both fixed at the device rate.
- Opus RX encoding uses the same ``RxOpusEncoder`` as the PyAudio handler,
  with the same Int16-PCM fallback when the encoder fails.

Sync/async boundary: the server calls ``start_tx``/``stop_tx``/``close``
through ``asyncio.to_thread`` and everything else on the event loop, exactly
as it does for the PyAudio handler.  This adapter mirrors that: the three
thread-entry methods bridge onto the engine's event loop with
``run_coroutine_threadsafe``; the loop-side methods touch only the engine's
thread-safe deques.  Construct on the server event loop (the loop is
captured at construction, like every other engine consumer).
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Optional

import numpy as np

from audio_resample import StreamingPcmResampler
from backends.sunsdr.dsp import AUDIO_RATE
from backends.sunsdr.engine import SunSDREngine
from opus_rx import (
    AUDIO_TAG_PCM,
    AUDIO_TAG_OPUS,
    DEFAULT_BITRATE,
    RX_RATE,
    RxOpusEncoder,
)

logger = logging.getLogger(__name__)

RX_CHUNK_MS = 20                 # the server's audio tick is 20 ms
TX_FRAME_SAMPLES = RX_RATE * RX_CHUNK_MS // 1000   # 960 samples per 20 ms
# A negotiated PCM16 socket uplinks 16 kHz; the engine's modulator runs at the
# device rate, so that is the one other input rate this adapter can serve.
PCM16_INPUT_RATE = 16000
# Bound for run_coroutine_threadsafe round-trips (start/stop are quick).
_THREAD_CALL_TIMEOUT_S = 5.0


class SunSDRAudioHandler:
    """AudioHandler-compatible adapter over the shared SunSDREngine."""

    def __init__(self, engine: SunSDREngine, *, loop: Optional[asyncio.AbstractEventLoop] = None,
                 bitrate: int = DEFAULT_BITRATE):
        self._engine = engine
        if loop is not None:
            self._loop = loop
        else:
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                # Sync construction (diagnostics/tests) — the loop-side
                # methods still work; a thread-entry call without a loop
                # raises clearly in _run.
                self._loop = None
        # Device-domain rates: the engine's audio IS 48 kHz both ways.
        self._rx_dev_rate = AUDIO_RATE
        self._tx_dev_rate = AUDIO_RATE
        self._rx_chunk = AUDIO_RATE * RX_CHUNK_MS // 1000
        self._tx_chunk = self._rx_chunk
        self._rx_running = False
        # RX silence watchdog state (same contract as AudioHandler).
        self._rx_zero_since: Optional[float] = None
        self._rx_last_peak = 0
        # TX session diagnostics — same keys as AudioHandler.tx_stats().
        self._tx_lock = threading.Lock()
        self._tx_written = 0
        self._tx_write_err = 0
        self._tx_peak = 0
        # PCM16 uplink bridge (16 kHz → device rate), built lazily per session.
        self._tx_pcm16_resampler: Optional[StreamingPcmResampler] = None
        # start_tx/stop_tx run on a to_thread worker while feed_tx_audio runs
        # on the event loop, so the bridge is only ever touched under _tx_lock.
        self._closed = False
        # Opus encoder for RX audio (identical construction to AudioHandler).
        self.opus_enabled = True
        self.opus_encoder: Optional[RxOpusEncoder] = None
        try:
            self.opus_encoder = RxOpusEncoder(bitrate=bitrate)
        except Exception as e:
            logger.warning("Opus encoder unavailable, RX stays on Int16 PCM: %s", e)
            self.opus_enabled = False

    # ── thread bridge ────────────────────────────────────────────

    def _run(self, coro):
        """Run an engine coroutine from a worker thread (to_thread entry
        points only — never call this from the event loop thread)."""
        loop = self._loop
        if loop is None:
            raise RuntimeError(
                "SunSDRAudioHandler was constructed outside the server "
                "event loop and no loop= was supplied")
        return asyncio.run_coroutine_threadsafe(
            coro, loop).result(_THREAD_CALL_TIMEOUT_S)

    # ── RX ───────────────────────────────────────────────────────

    def start_rx(self) -> bool:
        """Keep the consumer alive across a network reconnect.

        No sound-card open can fail here.  Waiting for IQ while disconnected
        lets the server's existing RX loop deliver audio as soon as the
        engine reconnects, instead of requiring a process restart.
        """
        if self._closed:
            return False
        self._rx_running = True
        return True

    def stop_rx(self):
        self._rx_running = False

    def restart_rx(self):
        """No capture stream exists to reopen.  What CAN go stale is the
        bounded RX queue: after every TX→RX transition drop whatever PCM
        buffered during the over so post-unkey audio starts fresh (the
        server calls this via to_thread, exactly like the PyAudio path)."""
        if not self._engine.connected:
            return
        self._engine.rx_audio.clear()

    def read_rx_chunk(self) -> Optional[bytes]:
        """One available 48 kHz int16 block, or None when the queue is
        empty (non-blocking, event-loop thread only)."""
        if not self._rx_running:
            return None
        queue = self._engine.rx_audio
        if not queue:
            return None
        # The engine keeps receiving for FT8/recording even with no browser.
        # A consumer that just arrived must not play seconds of stale PCM.
        while len(queue) > 5:
            queue.popleft()
            self._engine.rx_drops += 1
        try:
            return queue.popleft()
        except IndexError:
            return None

    def note_rx_chunk(self, pcm: Optional[bytes]):
        """Feed the RX-silence watchdog (same semantics as AudioHandler)."""
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
        """Tag Opus frames for browser clients, Int16 PCM as the fallback —
        byte layout identical to AudioHandler.encode_rx_audio."""
        if len(pcm) < 64:
            return []
        if self.opus_enabled and self.opus_encoder is not None:
            try:
                return [bytes([AUDIO_TAG_OPUS]) + pkt
                        for pkt in self.opus_encoder.push(pcm)]
            except Exception as e:
                logger.warning("Opus encode failed, sending PCM: %s", e)
                return [bytes([AUDIO_TAG_PCM]) + pcm]
        return [bytes([AUDIO_TAG_PCM]) + pcm]

    # ── TX ───────────────────────────────────────────────────────

    def _tx_session_end(self) -> None:
        """End the TX session's streaming state under the session lock.

        ``start_tx``/``stop_tx``/``close`` run on a ``to_thread`` worker while
        ``feed_tx_audio`` runs on the event loop, so dropping the 16→48 kHz
        bridge has to be mutually exclusive with a conversion in flight —
        otherwise an uplink frame can hand audio to the modulator after the
        keystroke it belonged to is over (AD-038).

        Callers that hold ``_tx_lock`` must not call this; it takes the lock
        itself and never touches the engine, so it cannot deadlock behind a
        pending loop future.  ``threading.Lock`` is not reentrant.
        """
        with self._tx_lock:
            self._reset_tx_pcm16_resampler()

    def _reset_tx_pcm16_resampler(self) -> None:
        """Drop the streaming 16→48 kHz bridge with the TX session that built
        it — a filter tail must never leak into the next keystroke (AD-038).

        **Must be called with ``_tx_lock`` held** (see ``_tx_session_end`` for
        the unlocked lifecycle path)."""
        self._tx_pcm16_resampler = None

    def start_tx(self) -> bool:
        """Begin a TX session (called via asyncio.to_thread by the server).
        Starts the engine's TX pacer; False makes the server unkey."""
        if self._closed or not self._engine.connected:
            return False
        # Clear the previous keystroke's streaming state under the same lock the
        # uplink converts under — before the await, so a frame that races this
        # handover can never carry a dead session's filter tail into the new one.
        with self._tx_lock:
            self._tx_written = 0
            self._tx_write_err = 0
            self._tx_peak = 0
            self._reset_tx_pcm16_resampler()
        try:
            ok = self._run(self._engine.start_tx())
        except Exception as e:                              # noqa: BLE001
            logger.warning("SunSDR2 DX TX audio start failed: %s", e)
            self._tx_session_end()
            return False
        if not ok or not self._engine.tx_active:
            # No session to keep, so nothing may outlive this attempt: the
            # engine's own False (an unverified mode such as AM/FM/CW — that
            # answer is what makes the server unkey instead of believing a pacer
            # is running), or a pacer that is already gone by the time this
            # lands because the server had to release PTT mid-handshake.  The
            # uplink gates on exactly that flag, and a frame on the loop thread
            # can have converted during the await above, so this path must drop
            # the bridge itself rather than hand it to the next keystroke.
            self._tx_session_end()
            return False
        return True

    def stop_tx(self, graceful: bool = False):
        """End the TX session (called via asyncio.to_thread).  ``graceful``
        drains the queued audio through the pacer, bounded by the engine."""
        # Retire the session's streaming state under the lock the uplink
        # converts under, in **both** exits of this method: when there is
        # nothing to stop (the keystroke already ended elsewhere) and after the
        # pacer release, whatever that release did.  The reset never runs before
        # ``_run`` — ``SunSDREngine.tx_active`` is derived from the pacer task,
        # so clearing streaming state while the drain still expects frames would
        # cut the tail of a call short.  Any await runs with the lock released.
        if not self._engine.tx_active:
            # Nothing to stop — but the keystroke is still over, so this exit
            # cleans up itself (the reset in the ``finally`` below is
            # unreachable from here).  SunSDR's pacer task really can be gone by
            # the time an unkey lands, and audio must not reach a modulator with
            # nobody draining it.
            with self._tx_lock:
                self._reset_tx_pcm16_resampler()
            return
        try:
            self._run(self._engine.stop_tx(graceful=graceful))
        except Exception as e:                              # noqa: BLE001
            with self._tx_lock:
                self._tx_write_err += 1
            logger.warning("SunSDR2 DX TX audio stop failed: %s", e)
        finally:
            # A cancelled or failed round trip can leave the pacer half torn
            # down; the bridge belongs to the keystroke, never to the next one.
            self._tx_session_end()

    def feed_tx_audio(self, pcm: bytes, input_rate: int = RX_RATE):
        """Mic audio into the engine's bounded TX queue (event-loop thread).

        ``input_rate`` states the rate of ``pcm`` (AD-038). The legacy default
        is 48 kHz — the engine's own domain, so it is fed straight through,
        byte-for-byte as before. A negotiated PCM16 socket comes through a
        streaming 16 → 48 kHz resampler first, because the modulator and the
        device pacer are both fixed at the device rate. Any other rate raises
        before the engine sees a sample: guessing here would put a pitched-up
        or slowed-down carrier on air.
        """
        if self._closed or not pcm or len(pcm) < 2:
            return
        if input_rate == RX_RATE:
            converted = pcm
        elif input_rate == PCM16_INPUT_RATE:
            # The bridge belongs to the keystroke that built it, and "retired"
            # is read under the same lock stop_tx/close clear it with: a frame
            # racing an unkey either converts on the live session or is refused
            # — never both.  ``_closed`` is permanent; ``not tx_active`` means
            # this keystroke is over (the pacer is gone, so audio fed now would
            # only sit in the queue and be cleared by the next key).  The
            # convert itself runs **outside** the lock — see the note in
            # backends/flex/audio.py.
            with self._tx_lock:
                if self._closed or not self._engine.tx_active:
                    return
                if self._tx_pcm16_resampler is None:
                    self._tx_pcm16_resampler = StreamingPcmResampler(
                        PCM16_INPUT_RATE, AUDIO_RATE)
                bridge = self._tx_pcm16_resampler
            converted = bridge.process(pcm)
        else:
            raise ValueError(f"unsupported TX input rate: {input_rate}")
        if not converted:
            return
        self._engine.feed_tx_audio(converted)
        peak = int(np.abs(np.frombuffer(converted, dtype=np.int16)).max())
        with self._tx_lock:
            self._tx_written += max(1, len(converted) // (2 * TX_FRAME_SAMPLES))
            if peak > self._tx_peak:
                self._tx_peak = peak

    def write_tx_chunk(self):
        """No-op by design: the engine's pacer task owns wire pacing, so
        there is no blocking device write for a drain loop to offload.
        Kept so the server can drive one uniform audio surface."""
        return None

    # ── Diagnostics ──────────────────────────────────────────────

    @property
    def device_rates(self) -> dict:
        return {"rx": self._rx_dev_rate, "tx": self._tx_dev_rate}

    def tx_stats(self) -> dict:
        """Same key set as AudioHandler.tx_stats() (the server reads
        written / write_err / queue_drops / peak)."""
        with self._tx_lock:
            written, err, peak = self._tx_written, self._tx_write_err, self._tx_peak
        return {
            "written": written,
            "write_err": err,
            "peak": peak,
            "queued": (len(self._engine._tx_queue)
                       if self._engine.tx_active else 0),
            "queue_drops": self._engine.tx_drops,
        }

    def tx_queue_frames(self) -> int:
        """Backpressure signal for server-side producers (CQ player)."""
        if not self._engine.tx_active:
            return 0
        return len(self._engine._tx_queue)

    def has_pending_tx_audio(self) -> bool:
        return bool(self._engine.tx_active)

    # ── Cleanup ──────────────────────────────────────────────────

    def close(self):
        """Release the adapter (called via to_thread at shutdown).  The
        ENGINE is not stopped here — it belongs to the backend and is shut
        down by ``cat.disconnect()`` after the audio tasks are cancelled."""
        self._closed = True
        # Retire the TX session's streaming bridge before anything else, so a
        # frame still in flight cannot queue audio behind the shutdown.
        self._tx_session_end()
        self.stop_rx()
        if self._engine.tx_active:
            try:
                self._run(self._engine.stop_tx())
            except Exception:                               # noqa: BLE001
                pass
        if self.opus_encoder:
            try:
                self.opus_encoder.close()
            except Exception:                               # noqa: BLE001
                pass
            self.opus_encoder = None
