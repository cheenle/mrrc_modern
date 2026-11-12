"""
Icom network audio → the server's audio pipeline
================================================
The radio sends **16 kHz 16-bit mono PCM** as audio datagrams; the server's
audio loop, the Opus encoder, FT8 and the recorder all speak **48 kHz mono**.
This adapter is the seam: it takes the client's PCM callback, resamples 16 → 48
kHz with a streaming soxr resampler (a plain ×3 repeat leaves an image band at
12–20 kHz, audible hiss), and presents the exact ``AudioHandler`` surface the
server drives — the same one the PyAudio handler and the SunSDR adapter present.

Transmit is **delegated**: Icom's protocol does carry TX audio, but the frame
that opens it (``txenable`` + a codec) has never been exercised on this radio,
and the built-in USB sound card may well be usable for TX even when its capture
side is not.  So ``start_tx``/``feed_tx_audio`` go to a PyAudio handler while RX
comes from the network.

Thread/value notes, both learned from the SunSDR adapter:

* ``start_rx``/``stop_rx``/``close`` are called through ``asyncio.to_thread``,
  while the PCM callback runs **on** the loop.  The loop is captured at
  construction and the three thread-entry points bridge onto it with
  ``run_coroutine_threadsafe``.
* The RX queue is bounded and trimmed on read: a listener that just arrived
  must not be played seconds of stale audio, and the UDP callback must never
  block on a slow consumer.
"""
from __future__ import annotations

import asyncio
import logging
import threading
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

logger = logging.getLogger("icom.net.audio")

RX_CHUNK_MS = 20
RX_CHUNK_SAMPLES = RX_RATE * RX_CHUNK_MS // 1000      # 960 @ 48 kHz
RX_CHUNK_BYTES = RX_CHUNK_SAMPLES * 2
_MAX_QUEUED_CHUNKS = 50                               # 1 s of slack
_TRIM_TO = 5
_THREAD_CALL_TIMEOUT_S = 15.0


class IcomNetAudioHandler:
    """``AudioHandler``-compatible surface over an ``IcomNetworkClient``."""

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

        try:
            self._device_rate = int(getattr(client, "rx_sample", 16000) or 16000)
        except (TypeError, ValueError):
            self._device_rate = 16000
        self._rx_audio: deque = deque(maxlen=_MAX_QUEUED_CHUNKS)
        self._pending = np.zeros(0, dtype=np.int16)
        self._rx_running = False
        self._closed = False
        self._rx_zero_since: Optional[float] = None
        self._rx_last_peak = 0
        self._resampler = self._make_resampler(self._device_rate)

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

    # ── resampling ─────────────────────────────────────────────────

    def _make_resampler(self, in_rate: int):
        """A streaming 16k→48k resampler, or None when the rates already match."""
        try:
            rate = int(in_rate)
        except (TypeError, ValueError):
            rate = 16000
        if rate == RX_RATE:
            return None
        try:
            import soxr
        except Exception as e:                                   # noqa: BLE001
            logger.warning("soxr unavailable (%s) — falling back to linear "
                           "interpolation for the network audio", e)
            return None
        try:
            return soxr.ResampleStream(float(rate), float(RX_RATE), 1,
                                       dtype="float32", quality="HQ")
        except Exception as e:                                   # noqa: BLE001
            logger.warning("soxr resampler unavailable for %s Hz (%s) — "
                           "falling back to linear interpolation", rate, e)
            return None

    def _to_48k(self, pcm: bytes) -> bytes:
        samples = np.frombuffer(pcm, dtype="<i2")
        if samples.size == 0:
            return b""
        if self._resampler is None:
            return samples.tobytes()
        audio = self._resampler.resample_chunk(samples.astype(np.float32))
        np.clip(audio, -32768.0, 32767.0, out=audio)
        return np.rint(audio).astype("<i2").tobytes()

    # ── PCM in ─────────────────────────────────────────────────────

    def _on_pcm(self, pcm: bytes, rate: int = 0) -> None:
        """Audio callback from the network client (event loop)."""
        if self._closed or not self._rx_running:
            return
        try:
            self.chunks_in += 1
            self._pending = np.concatenate(
                [self._pending, np.frombuffer(self._to_48k(pcm), dtype="<i2")])
        except Exception as exc:                                 # noqa: BLE001
            self.callback_failures += 1
            logger.debug("Icom network audio: conversion failed: %s", exc)
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
        """Start the network client (called via ``to_thread``)."""
        if self._closed:
            return False
        self._rx_running = True
        # The CAT path starts the same session at connect time, and the caller
        # then blocks this thread on the event loop — which is busy opening the
        # sound card for several seconds on Windows.  Waiting there produced
        # `client start failed:` (an empty TimeoutError) on a session that was
        # already streaming (measured 2026-11-12, 1.28.56 field log).
        if getattr(self._client, "connected", False):
            return True
        if self._loop is None:
            logger.warning("Icom network audio: no event loop captured — "
                           "the network client was not started")
            return True
        try:
            asyncio.run_coroutine_threadsafe(
                self._client.start(), self._loop).result(_THREAD_CALL_TIMEOUT_S)
        except Exception as exc:                                  # noqa: BLE001
            logger.error("Icom network audio: client start failed: %s", exc)
            self._rx_running = False
            return False
        return True

    def stop_rx(self) -> None:
        self._rx_running = False

    def restart_rx(self) -> None:
        """Drop whatever buffered during the previous over (TX→RX transition)."""
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
        except IndexError:
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

    # ── TX surface (delegated to the sound card) ───────────────────

    def start_tx(self) -> bool:
        if self._closed or self._tx is None:
            if self._tx is None:
                logger.warning("TX audio unavailable: no sound-card handler was "
                               "built (network TX is not implemented for this radio)")
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
                "queue_drops": 0}

    def tx_queue_frames(self) -> int:
        return self._tx.tx_queue_frames() if self._tx is not None else 0

    def has_pending_tx_audio(self) -> bool:
        return bool(self._tx.has_pending_tx_audio()) if self._tx is not None else False

    @property
    def device_rates(self) -> dict:
        tx_rate = RX_RATE
        if self._tx is not None:
            try:
                tx_rate = self._tx.device_rates["tx"]
            except Exception:                                    # noqa: BLE001
                pass
        return {"rx": RX_RATE, "tx": tx_rate}

    # ── cleanup ────────────────────────────────────────────────────

    def close(self) -> None:
        self._closed = True
        self._rx_running = False
        self._rx_audio.clear()
        if self._client is not None:
            self._client.on_audio = None
            if self._loop is not None:
                try:
                    asyncio.run_coroutine_threadsafe(
                        self._client.stop(), self._loop).result(_THREAD_CALL_TIMEOUT_S)
                except Exception as exc:                         # noqa: BLE001
                    logger.debug("Icom network audio: client stop failed: %s", exc)
        if self._tx is not None:
            try:
                self._tx.close()
            except Exception:                                    # noqa: BLE001
                pass
