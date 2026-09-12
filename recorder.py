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


# ── Naming ──────────────────────────────────────────────────────────

def recording_name(freq_hz: int, when: Optional[datetime] = None) -> str:
    """mrrc-compatible file name for a new recording."""
    when = when or datetime.now()
    khz = int(freq_hz / 1000) if freq_hz and freq_hz > 0 else 0
    return f"{khz:05d}kHz_{when:%Y%m%d_%H%M%S}.mp3"


def parse_recording_name(name: Optional[str]) -> Optional[dict]:
    """Parse a recorder file name, or return None when it is not ours.

    This is the only accepted shape: the REST routes reject anything else,
    which is what keeps path traversal out of the recordings API.
    """
    match = _NAME_RE.match(name or "")
    if not match:
        return None
    return {
        "freq_hz": int(match.group(1)) * 1000,
        "date": match.group(2),
        "time": match.group(3),
    }


def _name_timestamp(parsed: dict) -> str:
    """ISO timestamp recovered from a parsed file name."""
    date, clock = parsed["date"], parsed["time"]
    return (f"{date[:4]}-{date[4:6]}-{date[6:]}T"
            f"{clock[:2]}:{clock[2:4]}:{clock[4:]}")


# ── Index ───────────────────────────────────────────────────────────

@dataclass
class RecordingInfo:
    """Metadata for one finished recording."""

    name: str
    freq_hz: int
    started_at: str
    duration: float
    bytes: int


def load_index(path) -> dict:
    """Read the recordings index; a missing/corrupt file is an empty index."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}
    entries = data.get("recordings") if isinstance(data, dict) else None
    return entries if isinstance(entries, dict) else {}


def save_index(path, entries: dict) -> None:
    """Atomically write the recordings index (tmp file + replace)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps({"version": 1, "recordings": entries}, indent=2))
    os.replace(tmp, path)


def list_recordings(directory, index: dict, bitrate: int) -> list:
    """List our recordings, newest first, merging the index with the disk.

    The directory is the source of truth for existence (an index entry
    whose file is gone disappears from the list); the index supplies the
    exact duration, and anything without an entry — a file the operator
    copied in, as is normal in an mrrc ``recordings/`` directory — falls
    back to size/bitrate arithmetic.
    """
    directory = Path(directory)
    if not directory.is_dir():
        return []
    rows = []
    for path in directory.iterdir():
        parsed = parse_recording_name(path.name)
        if parsed is None or not path.is_file():
            continue
        meta = index.get(path.name) or {}
        size = path.stat().st_size
        duration = meta.get("duration")
        if not isinstance(duration, (int, float)) or duration <= 0:
            duration = size * 8.0 / (bitrate * 1000) if bitrate > 0 else 0.0
        rows.append({
            "name": path.name,
            "freq_hz": meta.get("freq_hz", parsed["freq_hz"]),
            "started_at": meta.get("started_at") or _name_timestamp(parsed),
            "duration": float(duration),
            "bytes": size,
        })
    rows.sort(key=lambda row: (row["started_at"], row["name"]), reverse=True)
    return rows
