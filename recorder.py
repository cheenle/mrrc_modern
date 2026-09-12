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

from audio_resample import resample_pcm

logger = logging.getLogger("recorder")

#: Storage-domain sample rate (AD-017) — not the codec or device rate.
RECORDING_RATE = 16000

#: Opus-mandated codec rate (AD-011).  Device audio whose rate is not an
#: integer multiple of the recording rate (the FT-710's 44.1 kHz) is
#: bridged here first with the sanctioned resampler, then decimated 3:1.
CODEC_RATE = 48000

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


# ── Recording session ───────────────────────────────────────────────

class RecordingSession:
    """One QSO recording: a 16 kHz mono timeline written incrementally.

    All mutation happens on a single thread (server.py funnels calls
    through one writer task), so no lock is needed here.
    """

    #: Blocks closer than this to the previous one are treated as
    #: contiguous — this absorbs scheduler jitter instead of turning it
    #: into a silence gap, which is what makes playback smooth.
    CONTINUITY_TOLERANCE_SAMPLES = RECORDING_RATE * 5 // 100       # 50 ms
    #: Silence is encoded in chunks so a long pause does not allocate one
    #: huge buffer.
    _SILENCE_CHUNK = RECORDING_RATE // 50                          # 20 ms

    def __init__(self, directory, bitrate: int = 64,
                 max_seconds: float = 4 * 3600, quality: int = 2):
        self.directory = Path(directory)
        self.bitrate = int(bitrate)
        self.max_seconds = float(max_seconds)
        self.quality = int(quality)
        self._enc = None
        self._fh = None
        self._active = False
        self._start_ns = None
        self._started_at = None
        self._freq_hz = 0
        self._name = None
        self._cursor = 0
        self._bytes = 0
        self._decimators = {}
        self._source_cursors = {}
        self._last_source = None

    # ── Lifecycle ──────────────────────────────────────────────────

    @property
    def active(self) -> bool:
        return self._active

    @property
    def max_samples(self) -> int:
        return int(RECORDING_RATE * self.max_seconds)

    def start(self, freq_hz: int = 0, now: Optional[datetime] = None) -> bool:
        """Begin a recording; False when one is already running."""
        if self._active:
            return False
        import lameenc                       # local: keeps import cost off
                                             # the always-imported path
        self.directory.mkdir(parents=True, exist_ok=True)
        self._name = recording_name(freq_hz, now)
        self._fh = open(self.directory / self._name, "wb")
        encoder = lameenc.Encoder()
        encoder.set_channels(1)
        encoder.set_in_sample_rate(RECORDING_RATE)
        encoder.set_bit_rate(self.bitrate)
        encoder.set_quality(self.quality)
        self._enc = encoder
        self._start_ns = time.monotonic_ns()
        self._started_at = (now or datetime.now()).isoformat(timespec="seconds")
        self._freq_hz = int(freq_hz or 0)
        self._cursor = 0
        self._bytes = 0
        self._decimators = {}
        self._source_cursors = {}
        self._last_source = None
        self._active = True
        logger.info("Recording started: %s (%d kbps, %d Hz mono)",
                    self._name, self.bitrate, RECORDING_RATE)
        return True

    def add_audio(self, source: str, pcm: bytes, source_rate: int,
                  timestamp_ns: Optional[int] = None) -> bool:
        """Place one PCM block on the timeline; False when it was ignored."""
        if not self._active or not pcm:
            return False
        if source_rate <= 0:
            raise ValueError("source_rate must be positive")
        # lameenc rejects non-int16-aligned input; trim here so a stray odd
        # byte can never raise into the audio path.
        if len(pcm) % 2:
            pcm = pcm[:-1]
        samples = np.frombuffer(pcm, dtype='<i2')
        if samples.size == 0:
            return False
        if source_rate != RECORDING_RATE:
            rate = int(source_rate)
            if rate % RECORDING_RATE:
                # Not an integer ratio (FT-710 44.1 kHz device domain):
                # bridge to the codec rate with the sanctioned resampler,
                # then decimate 48k -> 16k below.
                pcm = resample_pcm(pcm, rate, CODEC_RATE)
                rate = CODEC_RATE
                samples = np.frombuffer(pcm, dtype='<i2')
                if samples.size == 0:
                    return False
            key = (source, rate)
            decimator = self._decimators.get(key)
            if decimator is None:
                decimator = _StreamingDecimator(rate, RECORDING_RATE)
                self._decimators[key] = decimator
            samples = decimator.process(samples)

        timestamp_ns = (time.monotonic_ns() if timestamp_ns is None
                        else int(timestamp_ns))
        start_ns = self._start_ns
        if start_ns is None:
            return False
        offset = max(0, (timestamp_ns - start_ns) * RECORDING_RATE
                     // 1_000_000_000)
        expected = self._source_cursors.get(source)
        if (expected is not None and source == self._last_source
                and abs(offset - expected) <= self.CONTINUITY_TOLERANCE_SAMPLES):
            offset = expected
        if offset >= self.max_samples:
            return False
        samples = samples[: self.max_samples - offset]
        if samples.size == 0:
            return False
        self._write_silence_upto(offset)
        self._encode(samples)
        self._source_cursors[source] = offset + samples.size
        self._last_source = source
        return True

    def stop(self, now_ns: Optional[int] = None) -> Optional[RecordingInfo]:
        """Finish the recording: pad, flush, close and report."""
        if not self._active:
            return None
        enc, fh = self._enc, self._fh
        start_ns, name = self._start_ns, self._name
        if enc is None or fh is None or start_ns is None or name is None:
            # Half-initialised session: never raise out of the recorder.
            logger.warning("Recording session was not fully started — closing")
            self.close_without_finishing()
            return None
        now_ns = time.monotonic_ns() if now_ns is None else int(now_ns)
        stop_offset = min(max(0, (now_ns - start_ns) * RECORDING_RATE
                              // 1_000_000_000), self.max_samples)
        self._write_silence_upto(max(stop_offset, self._cursor))
        tail = b""
        try:
            tail = enc.flush()                          # final frames + Xing
        except Exception as e:                          # pragma: no cover
            logger.warning("MP3 flush failed: %s", e)
        if tail:
            fh.write(tail)
            self._bytes += len(tail)
        fh.close()
        info = RecordingInfo(
            name=name,
            freq_hz=self._freq_hz,
            started_at=self._started_at or "",
            duration=self._cursor / RECORDING_RATE,
            bytes=self._bytes,
        )
        logger.info("Recording stopped: %s (%.1fs, %d bytes)",
                    info.name, info.duration, info.bytes)
        self._reset()
        return info

    def close_without_finishing(self) -> None:
        """Abandon the session (shutdown/crash path): never flush."""
        if self._active and self._fh is not None:
            try:
                self._fh.flush()
                self._fh.close()
            except OSError:
                pass
        self._reset()

    def _reset(self) -> None:
        self._active = False
        self._enc = None
        self._fh = None
        self._name = None
        self._start_ns = None
        self._started_at = None
        self._freq_hz = 0
        self._cursor = 0
        self._bytes = 0
        self._decimators = {}
        self._source_cursors = {}
        self._last_source = None

    # ── Timeline / encoding ────────────────────────────────────────

    def _write_silence_upto(self, offset_samples: int) -> None:
        """Encode silence until the timeline cursor reaches *offset*."""
        while self._cursor < offset_samples:
            count = min(self._SILENCE_CHUNK, offset_samples - self._cursor)
            self._encode(np.zeros(count, dtype=np.int16))

    def _encode(self, samples: np.ndarray) -> None:
        enc, fh = self._enc, self._fh
        if enc is None:
            return
        payload = np.ascontiguousarray(samples, dtype='<i2').tobytes()
        data = enc.encode(payload)
        if data and fh is not None:
            fh.write(data)
            fh.flush()          # crash safety: never leave audio in an
                                # 8 KB stdio buffer (a killed process keeps
                                # everything already written)
            self._bytes += len(data)
        self._cursor += len(samples)

    # ── Status ─────────────────────────────────────────────────────

    def status(self, now_ns: Optional[int] = None) -> dict:
        """Snapshot for the ``recordingState`` broadcast."""
        duration = 0.0
        if self._active and self._start_ns is not None:
            now_ns = time.monotonic_ns() if now_ns is None else int(now_ns)
            duration = max(0.0, (now_ns - self._start_ns) / 1_000_000_000)
        return {
            "recording": self._active,
            "freq_hz": self._freq_hz,
            "started_at": self._started_at,
            "duration": round(duration, 2),
            "name": self._name,
            "bytes": self._bytes,
        }
