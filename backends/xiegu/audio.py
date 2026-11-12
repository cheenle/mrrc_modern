"""Xiegu G7250 RX audio from the LAN IQ stream
============================================
The radio has two ways to carry receive audio, and only one of them is ours to
control:

* its **built-in USB sound card** (the same composite device as the CAT port) —
  this is what ``docs/G7250_USER_GUIDE.md`` §1 describes, and it is the only
  TX path there is (the LAN protocol carries no audio towards the radio; Xiegu
  documents the network side as receive-only);
* its **network baseband** — the UDP IQ stream that already feeds the waterfall.

The field case behind this module (BG7YQV, 2026-11-12, bundle
``20261111-212640-e397``) had a healthy CAT link, the built-in sound card duly
enumerated on Windows as ``麦克风 (USB Audio Device)``, and that capture stream
sat at ``rms=2 peak=9`` (≈ −70 dBFS) for three days while the customer kept
saying the reasonable thing: *the network port carries the baseband IQ*.  The
waterfall was synthetic for the same reason (no ``MRRC_G7250_LAN_IP``), so the
whole radio looked broken while CAT was fine.

So: when the LAN IQ is configured, RX audio is **demodulated from the same
datagrams the FFT gets** — one socket, one tune frame, two products.  TX audio
still goes to the sound card, which is why this adapter *composes* a PyAudio
``AudioHandler`` for the transmit direction instead of replacing it.

Deliberate choices
------------------
* **Mode comes from the radio.** ``AudioDemodulator`` needs the sideband, and
  the sideband comes from CAT; ``backend.py`` observes both the poll's
  ``get_mode`` and the operator's ``set_mode`` on the controller and pushes the
  name here.  Before the first answer the demodulator runs USB (the G7250
  powers up on 20 m USB in every shipped profile).
* **The queue is bounded and trimmed, never blocking.** A slow consumer (a
  paused tab) must not stall the UDP reader: the spectrum is the other product
  on the same socket.  Dropping the oldest chunk costs 20 ms of audio; blocking
  would cost the waterfall.
* **Cost.** Demodulation is CPU a sound card would not need — the same DSP
  measures ~0.15 core per channel at 48 kS/s on a W103D's A53.  That is why
  ``MRRC_G7250_LAN_AUDIO=0`` exists and does not touch the waterfall.
"""
from __future__ import annotations

import contextlib
import logging
import time
from collections import deque
from typing import Any, Optional

import numpy as np

from opus_rx import (
    AUDIO_TAG_OPUS,
    AUDIO_TAG_PCM,
    DEFAULT_BITRATE,
    RX_RATE,
    RxOpusEncoder,
)

logger = logging.getLogger("xiegu.audio")

#: The demodulator's IQ rate and its audio rate are the same number here: the
#: G7250's LAN stream is 48 kS/s and the browser/Opus domain is 48 kHz, so the
#: resampler inside AudioDemodulator runs at unity (and the "device domain IS
#: the codec domain" rule the SunSDR adapter states holds for free).
LAN_AUDIO_RATE = RX_RATE                  # 48000

RX_CHUNK_MS = 20                          # the server's audio tick
CHUNK_BYTES = LAN_AUDIO_RATE * 2 * RX_CHUNK_MS // 1000      # 1920 B mono i16
_MAX_QUEUED_CHUNKS = 50                   # 1 s of slack, then drop the oldest
_TRIM_TO = 5                              # a consumer that just arrived
#: Radio mode name -> AudioDemodulator mode.  CW is USB-shaped audio on purpose
#: (see ``backends/sunsdr/dsp.py``: the pitch is the operator's CW offset), so
#: both CW sides map to the same demodulator branch.
_DEMOD_MODE = {
    "USB": "USB", "USB-D": "USB", "DIGI": "USB", "DATA": "USB", "FT8": "USB",
    "LSB": "LSB", "LSB-D": "LSB",
    "CW": "CW", "CW-U": "CW", "CW-L": "CW", "CW-R": "CW",
    "AM": "AM", "AM-N": "AM",
    "FM": "NFM", "NFM": "NFM", "FM-N": "NFM",
}
_DEFAULT_MODE = "USB"


def demod_mode_for(mode_name: Optional[str]) -> str:
    """Map a radio mode name onto the demodulator's set (never raises).

    An unknown or missing name falls back to USB instead of raising: the
    demodulator runs from a UDP callback, and one odd mode name must not take
    the audio (and, through the shared callback, the waterfall) down with it.
    """
    key = str(mode_name or "").strip().upper()
    return _DEMOD_MODE.get(key, _DEFAULT_MODE)


class G7250AudioHandler:
    """``audio_handler.AudioHandler``-compatible surface over the LAN IQ stream.

    Receive is demodulated here; transmit is delegated to ``tx_handler`` (the
    PyAudio handler for the radio's built-in sound card) because the network
    protocol has no TX audio at all.
    """

    def __init__(self, producer: Any, *, tx_handler: Any = None,
                 bitrate: int = DEFAULT_BITRATE) -> None:
        # Imported here, not at module scope: this module is reachable from the
        # backend factory, and a model that does not use it must not pay for
        # scipy/soxr at import time.
        from backends.sunsdr.dsp import AudioDemodulator

        self._producer = producer
        self._tx = tx_handler
        # if_offset_hz=0.0: unlike SunSDR2 DX (VFO at -30.5 kHz inside its IQ),
        # the G7250's tune frame centres the stream on the VFO.
        self._demod = AudioDemodulator(sample_rate=LAN_AUDIO_RATE,
                                       audio_rate=LAN_AUDIO_RATE,
                                       if_offset_hz=0.0)
        self._mode = _DEFAULT_MODE
        self._demod.set_mode(self._mode)
        self._unknown_modes: set[str] = set()

        self._rx_audio: deque = deque(maxlen=_MAX_QUEUED_CHUNKS)
        self._rx_running = False
        self._rx_zero_since: Optional[float] = None
        self._rx_last_peak = 0
        self._closed = False
        # Counters (diagnostics + the field session that verifies this path).
        self.iq_blocks = 0
        self.rx_chunks = 0
        self.rx_drops = 0

        self.opus_enabled = True
        self.opus_encoder: Optional[RxOpusEncoder] = None
        try:
            self.opus_encoder = RxOpusEncoder(bitrate=bitrate)
        except Exception as e:                                    # noqa: BLE001
            logger.warning("Opus encoder unavailable, RX stays on Int16 PCM: %s", e)
            self.opus_enabled = False

        # One socket, two products: the producer hands every IQ block here.
        if producer is not None:
            producer.on_iq = self.feed_iq

    # ── IQ -> audio ──────────────────────────────────────────────

    def feed_iq(self, samples) -> None:
        """Demodulate one IQ block into the RX queue (UDP-callback thread/loop).

        Called by ``G7250LanIQProducer._ingest`` on the event loop, i.e. the
        same thread the server's RX loop drains from, so the deque needs no
        lock (the server's ``read_rx_chunk`` also runs on that loop).
        """
        if self._closed or not self._rx_running:
            return
        arr = np.asarray(samples)
        if arr.size == 0:
            return
        self.iq_blocks += 1
        try:
            pcm = self._demod.demodulate(arr)
        except Exception as e:                                    # noqa: BLE001
            # A demodulator that raises must not silence the radio forever:
            # count it and keep going (the waterfall is a separate product).
            logger.warning("G7250 LAN demod failed: %s", e)
            return
        if not pcm:
            return
        if len(self._rx_audio) == _MAX_QUEUED_CHUNKS:
            self.rx_drops += 1
        self._rx_audio.append(bytes(pcm))
        self.rx_chunks += 1

    def set_mode(self, mode_name: Optional[str]) -> bool:
        """Follow the radio's mode (the demodulator needs the sideband).

        Returns True when the demodulator's mode actually changed.
        """
        wanted = demod_mode_for(mode_name)
        if mode_name and str(mode_name).strip().upper() not in _DEMOD_MODE:
            key = str(mode_name).strip().upper()
            if key not in self._unknown_modes:
                self._unknown_modes.add(key)
                logger.info("G7250 RX audio: mode %r has no demodulator branch, "
                            "using %s", mode_name, wanted)
        if wanted == self._mode:
            return False
        self._mode = wanted
        self._demod.set_mode(wanted)
        logger.info("G7250 RX audio: demodulator mode -> %s", wanted)
        return True

    @property
    def mode(self) -> str:
        """The demodulator's current mode (diagnostics/tests)."""
        return self._mode

    # ── RX surface (the server's audio loop drives these) ─────────

    def start_rx(self) -> bool:
        """No device to open: the stream either arrives or it does not.

        Always True — making the server log "audio RX failed" here would blame
        the sound card for a missing cable on the LAN side, which the producer's
        own no-packet warning already names precisely.
        """
        if self._closed:
            return False
        self._rx_running = True
        return True

    def stop_rx(self) -> None:
        self._rx_running = False

    def restart_rx(self) -> None:
        """Drop the audio buffered during the previous over (TX→RX transition).

        The server calls this via ``asyncio.to_thread`` on every such
        transition, exactly as it does for the PyAudio path; there is no
        capture stream to reopen.
        """
        self._rx_audio.clear()

    def read_rx_chunk(self) -> Optional[bytes]:
        """One demodulated 20 ms block, or None when nothing is buffered."""
        if not self._rx_running or self._closed:
            return None
        queue = self._rx_audio
        if not queue:
            return None
        # A listener that just arrived must not be played a second of stale
        # audio (the demodulator runs whether or not anyone reads).
        while len(queue) > _TRIM_TO:
            queue.popleft()
            self.rx_drops += 1
        try:
            return queue.popleft()
        except IndexError:
            return None

    def note_rx_chunk(self, pcm: Optional[bytes]) -> None:
        """Feed the RX-silence watchdog (same semantics as AudioHandler)."""
        peak = 0
        if pcm:
            arr = np.frombuffer(pcm, dtype=np.int16)
            if arr.size:
                # .item() rather than int(): a Python int straight off the numpy
                # scalar, with no conversion step a malformed buffer could make
                # raise inside the watchdog.
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
        """Tag Opus frames for browser clients, Int16 PCM as the fallback.

        Byte layout identical to ``AudioHandler.encode_rx_audio`` — the
        browser and the Android app decode both tags.
        """
        if len(pcm) < 64:
            return []
        if self.opus_enabled and self.opus_encoder is not None:
            try:
                return [bytes([AUDIO_TAG_OPUS]) + pkt
                        for pkt in self.opus_encoder.push(pcm)]
            except Exception as e:                                # noqa: BLE001
                logger.warning("Opus encode failed, sending PCM: %s", e)
                return [bytes([AUDIO_TAG_PCM]) + pcm]
        return [bytes([AUDIO_TAG_PCM]) + pcm]

    # ── TX surface (delegated to the built-in USB sound card) ────

    def start_tx(self) -> bool:
        """Open the sound card's playback stream.  False makes the server unkey."""
        if self._closed or self._tx is None:
            if self._tx is None:
                logger.warning(
                    "G7250 TX audio unavailable: no sound-card handler was built "
                    "(the LAN protocol carries no TX audio)")
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

    # ── Diagnostics / cleanup ────────────────────────────────────

    @property
    def device_rates(self) -> dict:
        """RX is the demodulator's 48 kHz; TX is whatever the card negotiated."""
        tx_rate = LAN_AUDIO_RATE
        if self._tx is not None:
            with contextlib.suppress(Exception):
                tx_rate = self._tx.device_rates["tx"]
        return {"rx": LAN_AUDIO_RATE, "tx": tx_rate}

    def close(self) -> None:
        """Release the adapter (called via ``to_thread`` at shutdown).

        The producer is *not* stopped here: it belongs to the backend, which
        owns the backend's teardown — same rule the SunSDR adapter states.
        """
        self._closed = True
        self.stop_rx()
        self._rx_audio.clear()
        if self._producer is not None:
            self._producer.on_iq = None
        if self._tx is not None:
            with contextlib.suppress(Exception):
                self._tx.close()
