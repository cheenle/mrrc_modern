"""
Server-side QSO recorder
========================
Records RX (device-domain PCM, straight off the sound card) and TX
(decoded browser-mic PCM) onto one monotonic mono timeline and encodes
it incrementally to MP3 while recording.

Why the server owns this now: the previous browser recorder concatenated
whatever frames arrived, with no timestamps, so delivery jitter and the
jitter buffer's padding were baked into the file (the reported symptom
was "trembling" playback).  Here every block carries a monotonic
timestamp, a genuine pause becomes silence, and 50 ms of scheduling
jitter is absorbed instead of turning into a hole.

Storage domain (AD-017): the recording timeline is 16 kHz mono, the same
as the sibling ``mrrc`` project whose tooling consumes a ``recordings/``
directory.  This is a WRITE-ONLY sink reached *from* the 48 kHz codec
domain: 44.1 kHz device audio goes through the sanctioned
``audio_resample`` bridge first, then a purpose-built anti-aliasing FIR
decimator produces 16 kHz.  Nothing here re-enters the codec or device
domain, so AD-011 still holds.

Crash safety: frames are written as they are produced, so a killed
process leaves a playable MP3 (a missing Xing header does not stop
browsers from playing the prefix that made it to disk).
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger("recorder")

#: Storage-domain sample rate (AD-017) — not the codec or device rate.
RECORDING_RATE = 16000

#: mrrc-compatible file name: <freq kHz>_<YYYYmmdd>_<HHMMSS>.mp3
_NAME_RE = re.compile(r"^(\d{5})kHz_(\d{8})_(\d{6})\.mp3$")


class _StreamingDecimator:
    """Stateful FIR low-pass filter followed by integer-ratio decimation.

    Ported from the sibling ``mrrc`` project's ``recording_session.py``: a
    windowed-sinc low pass removes everything above the recording band
    before samples are dropped, so decimation cannot fold high-frequency
    energy back into the voice band.
    """

    def __init__(self, source_rate: int, target_rate: int, ntaps: int = 96):
        if source_rate % target_rate != 0:
            raise ValueError("source_rate must be an integer multiple of target_rate")
        self.factor = source_rate // target_rate
        self._ntaps = ntaps
        n = np.arange(ntaps) - (ntaps - 1) / 2.0
        cutoff_hz = min(5500.0, target_rate * 0.4)
        coefficients = (
            2.0 * cutoff_hz / source_rate
            * np.sinc(2.0 * cutoff_hz / source_rate * n)
        )
        coefficients *= np.hamming(ntaps)
        coefficients /= np.sum(coefficients)
        self._coefficients = coefficients.astype(np.float64)
        self._state = np.zeros(ntaps - 1, dtype=np.float64)
        self._phase = 0

    def process(self, samples) -> np.ndarray:
        """Filter + decimate one block; returns int16 samples."""
        values = np.asarray(samples, dtype=np.float64)
        if values.size == 0:
            return np.zeros(0, dtype=np.int16)
        combined = np.concatenate((self._state, values))
        filtered = np.convolve(combined, self._coefficients)[
            self._ntaps - 1: self._ntaps - 1 + values.size
        ]
        self._state = combined[-(self._ntaps - 1):]
        output = filtered[self._phase::self.factor]
        self._phase = (self._phase - values.size) % self.factor
        return np.clip(np.rint(output), -32768, 32767).astype(np.int16)
