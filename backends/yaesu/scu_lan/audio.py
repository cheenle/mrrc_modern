"""
SCU-LAN10 RX audio → the server's audio pipeline
================================================
The unit pushes 664-byte audio bodies on port+2 ≈100 times a second: a 24-byte
inner header (stripped by ``client._audio_payload``) and 640 bytes of
**stereo interleaved Int16 LE at 16 kHz** — 160 frames, 10 ms.  The server's
whole audio path (Opus encoder, FT8 tap, recorder, browsers) speaks **48 kHz
mono**, so this adapter is the seam where those two worlds meet:

1. downmix L/R to mono (channel 1 is near-silent in normal RX, so this is not
   the left-only shortcut),
2. resample 16 → 48 kHz (soxr when available; a ×3 linear interpolation
   otherwise — a plain sample-repeat leaves an image band at 12–20 kHz),
3. re-block into 20 ms chunks and hand them out through the exact
   ``AudioHandler`` surface the server drives.

Transmit is **not implemented**: the unit's TX audio frame is 668 bytes and its
inner header has never been characterised (upstream spec §Open Questions), so
there is nothing to send that a radio would accept.  ``start_tx`` therefore
refuses — and the backend refuses to key in this mode until that lands, because
keying a 200 W transmitter to feed it silence is worse than saying "not yet".

Thread/value notes (same as ``backends/icom_net/audio.py``):

* ``start_rx``/``stop_rx``/``close`` are called through ``asyncio.to_thread``
  while the PCM callback runs **on** the loop, so the loop is captured at
  construction and thread entry points bridge with ``run_coroutine_threadsafe``.
* the RX queue is bounded and trimmed on read: a listener that just arrived
  must not be played seconds of stale audio, and the UDP callback must never
  block on a slow consumer.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Optional

import numpy as np

from opus_rx import (
    AUDIO_TAG_OPUS,
    AUDIO_TAG_PCM,
    DEFAULT_BITRATE,
    RX_RATE,
    RxOpusEncoder,
)

logger = logging.getLogger("yaesu.scu_lan.audio")

#: The unit's fixed wire format (PCM(16 kHz) in the FT-Control connection setup;
#: the other rates it offers have never been observed on the wire).
DEVICE_RATE = 16_000
DEVICE_CHANNELS = 2

RX_CHUNK_MS = 20
RX_CHUNK_SAMPLES = RX_RATE * RX_CHUNK_MS // 1000        # 960 @ 48 kHz
_MAX_QUEUED_CHUNKS = 50                                 # 1 s of slack
_TRIM_TO = 5
_THREAD_CALL_TIMEOUT_S = 15.0


class ScuLanAudioHandler:
    """``AudioHandler``-compatible surface over a `ScuLanClient`."""

    def __init__(self, client, *, tx_handler=None, bitrate: int = DEFAULT_BITRATE,
                 loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        self._client = client
        self._tx = tx_handler
        if loop is not None:
            self._loop = loop
        else:
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:                       # sync construction (tests)
                self._loop = None

        self._rx_audio: deque = deque(maxlen=_MAX_QUEUED_CHUNKS)
        self._pending = np.zeros(0, dtype=np.int16)
        self._rx_running = False
        self._closed = False
        self._rx_zero_since: Optional[float] = None
        self._rx_last_peak = 0
        self._tx_warned = False
        self._resampler = self._make_resampler()

        # Counters the server's diagnostics read.
        self.chunks_in = 0
        self.chunks_out = 0
        self.chunks_dropped = 0
        self.callback_failures = 0

        self.opus_enabled = True
        self.opus_encoder: Optional[RxOpusEncoder] = None
        try:
            self.opus_encoder = RxOpusEncoder(bitrate=bitrate)
        except Exception as e:                                   # noqa: BLE001
            logger.warning("Opus encoder unavailable, RX stays on Int16 PCM: %s", e)
            self.opus_enabled = False

        # The client hands every audio datagram here (event-loop thread).
        if client is not None:
            client.on_audio = self._on_pcm

    # ── conversion ─────────────────────────────────────────────────

    def _make_resampler(self):
        try:
            import soxr
        except Exception as e:                                   # noqa: BLE001
            logger.warning("soxr unavailable (%s) — linear interpolation will "
                           "be used for the SCU-LAN10 audio", e)
            return None
        try:
            # QQ, not HQ: measured on this machine, soxr's HQ stream holds
            # input until an internal block fills and then emits a burst
            # (0,0,0,1468,…) — up to 30 ms of extra latency and a rate that is
            # not exactly ×3 per 10 ms body.  QQ emits 480 samples per body
            # (a constant 475-sample delay at the start), which is what a fixed
            # 20 ms consumer wants; for 16 kHz voice into Opus 64k the quality
            # difference is not audible.
            return soxr.ResampleStream(float(DEVICE_RATE), float(RX_RATE), 1,
                                       dtype="float32", quality="QQ")
        except Exception as e:                                   # noqa: BLE001
            logger.warning("soxr resampler unavailable (%s) — falling back to "
                           "linear interpolation", e)
            return None

    @staticmethod
    def _downmix(pcm: bytes) -> np.ndarray:
        """Interleaved stereo → mono float32 (the mean of both channels)."""
        frame_bytes = 2 * DEVICE_CHANNELS
        usable = len(pcm) - (len(pcm) % frame_bytes)
        if usable <= 0:
            return np.zeros(0, dtype=np.float32)
        samples = np.frombuffer(pcm[:usable], dtype="<i2")
        stereo = samples.reshape(-1, DEVICE_CHANNELS)
        return stereo.mean(axis=1).astype(np.float32)

    def _to_48k(self, mono: np.ndarray) -> np.ndarray:
        if mono.size == 0:
            return np.zeros(0, dtype=np.float32)
        if self._resampler is not None:
            audio = self._resampler.resample_chunk(mono)
            return np.asarray(audio, dtype=np.float32)
        # ×3 linear interpolation.  Repeating each sample three times would be
        # one line shorter and would fold a 16 kHz image into 12–20 kHz.
        out_len = (mono.size * RX_RATE + DEVICE_RATE // 2) // DEVICE_RATE
        if out_len <= 0:
            return np.zeros(0, dtype=np.float32)
        positions = np.linspace(0.0, mono.size - 1.0, out_len, dtype=np.float32)
        return np.interp(positions, np.arange(mono.size, dtype=np.float32), mono)

    def convert(self, pcm: bytes) -> np.ndarray:
        """One wire body's PCM → 48 kHz mono float32 (exposed for tests)."""
        return self._to_48k(self._downmix(pcm))

    # ── PCM in ─────────────────────────────────────────────────────

    def _on_pcm(self, pcm: bytes) -> None:
        """Audio callback from the session client (event loop)."""
        if self._closed or not self._rx_running:
            return
        try:
            self.chunks_in += 1
            block = self.convert(pcm)
            np.clip(block, -32768.0, 32767.0, out=block)
            self._pending = np.concatenate(
                [self._pending, np.rint(block).astype("<i2")])
        except Exception as exc:                                 # noqa: BLE001
            self.callback_failures += 1
            logger.debug("SCU-LAN10 audio: conversion failed: %s", exc)
            return
        while self._pending.size >= RX_CHUNK_SAMPLES:
            block, self._pending = (self._pending[:RX_CHUNK_SAMPLES],
                                    self._pending[RX_CHUNK_SAMPLES:])
            if len(self._rx_audio) == _MAX_QUEUED_CHUNKS:
                self.chunks_dropped += 1
            self._rx_audio.append(block.astype("<i2").tobytes())
            self.chunks_out += 1

    # ── RX surface ─────────────────────────────────────────────────

    def start_rx(self) -> bool:
        """Called via ``to_thread``; the session itself is the backend's job."""
        if self._closed:
            return False
        self._rx_running = True
        if getattr(self._client, "connected", False):
            return True
        # The CAT path already started the session at connect time, and this
        # thread would block the loop it is waiting for — see the Icom adapter's
        # note (2026-11-12 field).  Start it only if nobody has.
        if self._loop is None:
            logger.warning("SCU-LAN10 audio: no event loop captured — the "
                           "session was not started here")
            return True
        try:
            asyncio.run_coroutine_threadsafe(
                self._client.start(), self._loop).result(_THREAD_CALL_TIMEOUT_S)
        except Exception as exc:                                  # noqa: BLE001
            logger.error("SCU-LAN10 audio: session start failed: %s", exc)
            self._rx_running = False
            return False
        return True

    def stop_rx(self) -> None:
        self._rx_running = False

    def restart_rx(self) -> None:
        """Drop what buffered during the previous over (TX→RX transition)."""
        self._rx_audio.clear()
        self._pending = np.zeros(0, dtype=np.int16)

    def read_rx_chunk(self) -> Optional[bytes]:
        if not self._rx_running or self._closed:
            return None
        queue = self._rx_audio
        if not queue:
            return None
        while len(queue) > _TRIM_TO:
            queue.popleft()
            self.chunks_dropped += 1
        try:
            return queue.popleft()
        except IndexError:                                       # pragma: no cover
            return None

    def note_rx_chunk(self, pcm: Optional[bytes]) -> None:
        peak = 0
        if pcm:
            arr = np.frombuffer(pcm, dtype=np.int16)
            if arr.size:
                peak = np.abs(arr.astype(np.int32)).max().item()
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
            except Exception as e:                               # noqa: BLE001
                logger.warning("Opus encode failed, sending PCM: %s", e)
                return [bytes([AUDIO_TAG_PCM]) + pcm]
        return [bytes([AUDIO_TAG_PCM]) + pcm]

    # ── TX surface ─────────────────────────────────────────────────

    @property
    def tx_audio_available(self) -> bool:
        """False until the unit's TX frame is characterised (design §6-1)."""
        return self._tx is not None

    def _warn_tx_unavailable(self) -> None:
        if self._tx_warned:
            return
        self._tx_warned = True
        logger.warning(
            "SCU-LAN10 transmit audio is not implemented: the unit's 668-byte "
            "TX frame inner header has not been characterised, so keying would "
            "put a silent carrier on air.  TX is refused in this mode until it "
            "is (see docs/superpowers/specs/2026-11-15-scu-lan-backend-design.md §6)")

    def start_tx(self) -> bool:
        if self._tx is None:
            self._warn_tx_unavailable()
            return False
        return bool(self._tx.start_tx())

    def stop_tx(self, graceful: bool = False, **kwargs) -> None:
        if self._tx is not None:
            self._tx.stop_tx(graceful, **kwargs)

    def feed_tx_audio(self, pcm: bytes) -> None:
        if self._tx is not None:
            self._tx.feed_tx_audio(pcm)

    def write_tx_chunk(self) -> None:
        if self._tx is not None:
            self._tx.write_tx_chunk()

    def tx_stats(self) -> dict:
        if self._tx is not None:
            return self._tx.tx_stats()
        return {"written": 0, "write_err": 0, "peak": 0, "queued": 0,
                "queue_drops": 0, "unsupported": True}

    def tx_queue_frames(self) -> int:
        return self._tx.tx_queue_frames() if self._tx is not None else 0

    def has_pending_tx_audio(self) -> bool:
        return bool(self._tx.has_pending_tx_audio()) if self._tx is not None else False

    @property
    def device_rates(self) -> dict:
        return {"rx": RX_RATE, "tx": RX_RATE}

    # ── cleanup ────────────────────────────────────────────────────

    def close(self) -> None:
        self._closed = True
        self._rx_running = False
        self._rx_audio.clear()
        self._pending = np.zeros(0, dtype=np.int16)
        if self._tx is not None:
            try:
                self._tx.close()
            except Exception:                                    # noqa: BLE001
                pass
