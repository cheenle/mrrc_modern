"""Local speaker tap — the radio's RX audio on a device next to the box.

A headless box has nobody looking at a browser, but a Bluetooth headset
standing next to the radio is the operator's ears.  This module taps the same
48 kHz mono PCM the browser path already produces, decimates it to the
device's rate (a SCO link is 8 kHz mono) and plays it on a bounded queue.

Playback goes through ``aplay`` rather than PyAudio on purpose: a Bluetooth
device is addressed by an **ALSA PCM name** (``bluealsa:SRV=…,DEV=…,PROFILE=sco``)
and PortAudio only offers enumerated device indexes, so the plugin string is
the one form that actually reaches the headset.

Three rules, each of which has cost this project a field trip elsewhere:

* **never block the caller** — the tap is a non-blocking queue put; a slow
  device drops audio and counts it, it never stalls the RX loop;
* **never be the reason audio stops** — the RX loop's idle gate must count
  this tap as a listener, the way the FT8 tap had to be counted;
* **a device that disappears is not an error** — a headset walking out of
  range kills ``aplay``; the writer respawns it on the next frame.

With no device configured the whole thing is a no-op.
"""

from __future__ import annotations

import logging
import queue
import subprocess
import threading
from typing import Optional

log = logging.getLogger("mrrc.localspeaker")

RX_RATE = 48_000          # the codec domain handed to us by read_rx_chunk()


class LocalSpeaker:
    """Play decimated RX audio on a local ALSA device."""

    def __init__(self, device: str, rate: int = 8000, channels: int = 1,
                 queue_frames: int = 12, spawn=None):
        self.device = device
        self.rate = rate
        self.channels = channels
        self._spawn = spawn or self._spawn_aplay
        self._queue: queue.Queue = queue.Queue(maxsize=max(1, queue_frames))
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._decimator = None
        self._samples = 0
        self._played = 0
        self._dropped = 0
        self._opens = 0
        self._failures = 0
        self._last_error = ""

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> bool:
        """Begin playing.  False when there is no device to play on."""
        if not self.device:
            return False
        if self._thread is not None:
            return True
        if self.rate < RX_RATE:
            from recorder import _StreamingDecimator
            self._decimator = _StreamingDecimator(RX_RATE, self.rate)
        else:
            self._decimator = None      # A2DP: play the codec domain as-is
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="local-speaker", daemon=True)
        self._thread.start()
        log.info("Local speaker: %s (%d Hz, %d ch)", self.device, self.rate, self.channels)
        return True

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=3.0)

    # -- tap ---------------------------------------------------------------
    def feed(self, pcm_48k: bytes) -> None:
        """Feed one 48 kHz mono int16 chunk.  Never blocks, never raises."""
        if not pcm_48k or self._thread is None:
            return
        try:
            import array
            samples = array.array("h")
            samples.frombytes(pcm_48k)
            if self._decimator is not None:
                samples = self._decimator.process(samples)
            if self.channels > 1:
                # A2DP is stereo while the radio's RX is mono: duplicate each
                # sample rather than letting aplay guess an up-mix.
                out = array.array("h", bytes(len(samples) * 2 * self.channels))
                for ch in range(self.channels):
                    out[ch::self.channels] = samples
                samples = out
            data = samples.tobytes()
        except Exception as exc:                        # noqa: BLE001 - never break RX
            log.debug("local speaker convert failed: %s", exc)
            return
        self._samples += len(pcm_48k) // 2
        try:
            self._queue.put_nowait(data)
        except queue.Full:
            self._dropped += 1

    # -- writer ------------------------------------------------------------
    def _spawn_aplay(self):
        return subprocess.Popen(
            ["aplay", "-q", "-t", "raw", "-f", "S16_LE",
             "-r", str(self.rate), "-c", str(self.channels), "-D", self.device],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )

    def _run(self) -> None:
        proc = None
        while not self._stop.is_set():
            if proc is None or proc.poll() is not None:
                try:
                    proc = self._spawn()
                    self._opens += 1
                except Exception as exc:                # noqa: BLE001 - retry later
                    self._failures += 1
                    self._last_error = str(exc)
                    log.warning("Local speaker spawn failed: %s", exc)
                    self._stop.wait(3.0)
                    continue
            try:
                data = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                proc.stdin.write(data)
                proc.stdin.flush()
                self._played += len(data)
            except Exception as exc:                    # noqa: BLE001 - device gone
                self._failures += 1
                self._last_error = str(exc)
                log.warning("Local speaker write failed: %s", exc)
                self._kill(proc)
                proc = None
        if proc is not None:
            self._kill(proc)

    @staticmethod
    def _kill(proc) -> None:
        try:
            if proc.stdin:
                proc.stdin.close()
        except Exception:                               # noqa: BLE001
            pass
        try:
            proc.terminate()
            proc.wait(timeout=2.0)
        except Exception:                               # noqa: BLE001
            try:
                proc.kill()
            except Exception:                           # noqa: BLE001
                pass

    # -- diagnostics -------------------------------------------------------
    @property
    def active(self) -> bool:
        return self._thread is not None

    def status(self) -> dict:
        return {
            "device": self.device,
            "rate": self.rate,
            "active": self.active,
            "samples_in": self._samples,
            "bytes_out": self._played,
            "queued": self._queue.qsize(),
            "dropped": self._dropped,
            "opens": self._opens,
            "failures": self._failures,
            "last_error": self._last_error,
        }
