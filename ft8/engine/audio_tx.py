"""Bounded 48 kHz float32 playback with cancellation (AD-004, §11.2).

The TX waveform produced by the DSP Worker is exactly 48 kHz float32 mono
(§9.4); nothing else may reach the output device.  Playback is bounded by
construction: at most ``MAX_TX_SAMPLES`` samples (one FT8 waveform) are
accepted, blocks are written sequentially and :meth:`TxPlayer.cancel`
aborts the stream between or during blocks.  The safety controller adds
the per-transmission time deadline on top (NFR-057).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np

TX_SAMPLE_RATE = 48_000
"""Only rate the TX path accepts; the binding encodes exactly this."""

MAX_TX_SAMPLES = 606_720
"""One full FT8 waveform at 48 kHz (12.64 s); FT4 waveforms are shorter."""


class TxAudioError(Exception):
    """Output device rejected, underflowed or lost the stream."""


def validate_tx_buffer(samples: np.ndarray) -> np.ndarray:
    """Return the waveform as a 1-D float32 array or raise ``ValueError``.

    Shared by :meth:`TxPlayer.play` and the safety controller so an invalid
    buffer is rejected before PTT is keyed.
    """

    pcm = np.asarray(samples)
    if pcm.dtype != np.float32 or pcm.ndim != 1:
        raise ValueError("TX waveform must be 1-D float32 at 48 kHz")
    if not 0 < pcm.size <= MAX_TX_SAMPLES:
        raise ValueError(
            f"TX waveform must hold 1..{MAX_TX_SAMPLES} samples, got {pcm.size}"
        )
    return pcm


@dataclass(frozen=True, slots=True)
class PlayResult:
    """Outcome of one bounded playback."""

    samples_written: int
    cancelled: bool
    elapsed_seconds: float


class TxPlayer:
    """One-at-a-time 48 kHz mono output stream.

    ``stream_factory`` defaults to ``sounddevice.OutputStream`` (imported
    lazily so hardware-free hosts can use the module); tests inject a fake
    whose ``write``/``abort`` are observable.  ``clock`` is a monotonic
    clock used only for elapsed-time reporting.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        stream_factory: Callable[..., object] | None = None,
        device: int | str | None = None,
        blocksize: int = 4_096,
    ) -> None:
        if stream_factory is None:
            # Lazy: importing sounddevice here would hang the server at
            # startup when CoreAudio device enumeration is wedged (2026-08-10
            # field: FT8 aggregate referencing a powered-off FT-710).  Defer
            # the import to the first play() — a broken audio stack then
            # faults the AUDIO interlock instead of blocking create_server.
            def _lazy_factory(**kwargs: Any) -> object:
                import sounddevice

                return sounddevice.OutputStream(**kwargs)

            stream_factory = _lazy_factory
        self._factory = stream_factory
        self._device = device
        self._blocksize = blocksize
        self._clock = clock
        self._stream: object | None = None
        self._cancelled = False

    @property
    def playing(self) -> bool:
        """True while a playback owns the output stream."""

        return self._stream is not None

    async def play(self, samples: np.ndarray) -> PlayResult:
        """Play one complete waveform unless cancelled.

        Raises ``ValueError`` for anything but a non-empty 1-D float32
        buffer of at most ``MAX_TX_SAMPLES``, ``RuntimeError`` when a
        playback is already active and ``TxAudioError`` on device failure.
        """

        pcm = validate_tx_buffer(samples)
        if self._stream is not None:
            raise RuntimeError("TX playback already in progress")

        stream = self._factory(
            samplerate=TX_SAMPLE_RATE,
            channels=1,
            dtype="float32",
            blocksize=self._blocksize,
            device=self._device,
        )
        self._stream = stream
        self._cancelled = False
        started = self._clock()
        written = 0
        try:
            stream.start()  # type: ignore[attr-defined]
            for offset in range(0, pcm.size, self._blocksize):
                if self._cancelled:
                    break
                block = pcm[offset : offset + self._blocksize].reshape(-1, 1)
                try:
                    await asyncio.to_thread(stream.write, block)  # type: ignore[attr-defined]
                except Exception as exc:  # device loss/underflow surfaces here
                    if self._cancelled:
                        # §15.3: abort() from the STOP path can make the
                        # blocked write fail (e.g. PaErrorCode -9986); that
                        # is the expected cancellation, not device loss.
                        break
                    raise TxAudioError(f"TX playback failed: {exc}") from exc
                written += block.shape[0]
            return PlayResult(
                samples_written=written,
                cancelled=self._cancelled,
                elapsed_seconds=self._clock() - started,
            )
        finally:
            self._close_stream()

    def cancel(self) -> None:
        """Abort any active playback; synchronous and idempotent (§15.3)."""

        self._cancelled = True
        stream = self._stream
        if stream is not None:
            try:
                stream.abort()  # type: ignore[attr-defined]
            except Exception:
                pass  # best-effort: the write loop still observes the flag

    def _close_stream(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()  # type: ignore[attr-defined]
                stream.close()  # type: ignore[attr-defined]
            except Exception:
                pass
