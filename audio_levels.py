"""Box-level audio levels — the TX microphone and RX fan-out gains (AD-032).

Two numbers, one owner each, both owned by the *box* rather than by a browser:

* ``tx_mic_db`` — applied to the operator's microphone as it arrives from
  ``/WSaudioTX`` and before it reaches the radio.  CQ playback and the FT8
  waveform do not pass through it: those are fixed sources whose level is not
  the operator's voice, and lifting them would move FT8's ALC without anybody
  asking.
* ``rx_db`` — applied to the PCM that is about to be encoded for *listeners*
  (browser, app).  The recording tap, the FT8 decoder and a local speaker all
  take the capture unmodified: a software gain cannot add information, and
  clipping here would corrupt a decode or a recording to make a phone louder.

Three rules earned the hard way elsewhere in this tree, and each is enforced
here rather than by convention:

* **0 dB is the identity, byte for byte** — ``apply_gain`` returns the caller's
  own object when there is nothing to do.  A default that allocates and
  re-quantises is a default that can differ from "no feature" under a test.
* **A bad value is refused, never clamped** — silently rewriting what an
  operator asked for is how a knob ends up lying.  The file on disk is the one
  exception (it can be hand-edited), and it is clamped *with a warning*.
* **Nothing here is fatal** — an unreadable JSON file or an unwritable
  directory falls back to 0 dB in memory and says so once.

Stdlib + numpy, no app imports, so it stays unit-testable on its own.
"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import numpy as np

log = logging.getLogger("mrrc.audiolevels")

TX_MIC_LIMITS = (-20.0, 25.0)
RX_LIMITS = (-20.0, 30.0)
DEFAULT_TX_MIC_DB = 0.0
DEFAULT_RX_DB = 0.0

INT16_MAX = 32767
INT16_MIN = -32768

KEYS = ("tx_mic_db", "rx_db")
LIMITS = {"tx_mic_db": TX_MIC_LIMITS, "rx_db": RX_LIMITS}
LABELS = {"tx_mic_db": "TX 麦克风增益", "rx_db": "RX 下发电平"}


def db_to_linear(db: float) -> float:
    """dB → amplitude multiplier. +6 dB ≈ 2×, +20 dB = 10×."""
    return math.pow(10.0, float(db) / 20.0)


def clamp_db(value: float, limits: tuple[float, float]) -> float:
    lo, hi = limits
    return max(lo, min(hi, float(value)))


def _as_number(value: Any) -> Optional[float]:
    """Return ``value`` as a finite float, or None.  ``True`` is not 1.0."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def apply_gain(pcm: bytes, db: float) -> tuple[bytes, int]:
    """Scale int16 PCM by ``db`` dB; return (bytes, clipped sample count).

    0 dB returns the **same bytes object** the caller passed: no copy, no
    numpy work, and a stream that is byte-for-byte the pre-feature one.

    Clipping is counted, not hidden — the caller decides what to do with it.
    A trailing odd byte (never produced by the 20 ms paths, which are always
    even-length) is dropped rather than raising: this runs on the RX loop and
    on every mic frame, and neither may die over one bad byte.
    """
    if not pcm or db == 0.0:
        return pcm, 0
    usable = pcm if len(pcm) % 2 == 0 else pcm[:-1]
    if not usable:
        return pcm, 0
    samples = np.frombuffer(usable, dtype="<i2").astype(np.int32)
    scaled = np.rint(samples * db_to_linear(db))
    clipped = int(np.count_nonzero((scaled > INT16_MAX) | (scaled < INT16_MIN)))
    np.clip(scaled, INT16_MIN, INT16_MAX, out=scaled)
    return scaled.astype("<i2").tobytes(), clipped


@dataclass(frozen=True)
class AudioLevels:
    """The two box-level gains, in dB."""

    tx_mic_db: float = DEFAULT_TX_MIC_DB
    rx_db: float = DEFAULT_RX_DB

    def to_wire(self) -> dict[str, float]:
        return {"tx_mic_db": self.tx_mic_db, "rx_db": self.rx_db}

    def replace(self, **changes: float) -> "AudioLevels":
        data = self.to_wire()
        data.update(changes)
        return AudioLevels(**data)

    @staticmethod
    def limit_table() -> dict[str, list[float]]:
        """The allowed range per key — the same table the UI renders."""
        return {key: [lo, hi] for key, (lo, hi) in LIMITS.items()}


def parse_update(payload: Any, current: AudioLevels) -> tuple[Optional[AudioLevels], str]:
    """Validate a partial update; return (levels, "") or (None, reason).

    Deliberately strict: unknown keys, non-numbers and out-of-range values are
    all refused with a reason the operator can act on.  A silent clamp here
    would make the slider's position disagree with the audio.
    """
    if not isinstance(payload, dict):
        return None, "请求体必须是一个 JSON 对象"
    if not payload:
        return None, "没有要修改的字段（tx_mic_db / rx_db）"
    unknown = sorted(set(payload) - set(KEYS))
    if unknown:
        return None, "未知字段：" + "、".join(unknown)
    merged = current.to_wire()
    for key, raw in payload.items():
        number = _as_number(raw)
        if number is None:
            return None, f"{LABELS[key]}必须是数字（dB）"
        lo, hi = LIMITS[key]
        if not (lo <= number <= hi):
            return None, (f"{LABELS[key]}超出范围：{lo:g}…{hi:g} dB"
                          f"（收到 {number:g}）")
        merged[key] = number
    return AudioLevels(**merged), ""


class AudioLevelsStore:
    """JSON-backed persistence for the two levels, atomic and never fatal."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.source = "default"      # "file" once a load found a file

    def load(self) -> AudioLevels:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self.source = "default"
            return AudioLevels()
        except Exception as exc:                            # noqa: BLE001 - never fatal
            log.warning("audio levels: %s unreadable (%s) — using 0 dB",
                        self.path, exc)
            self.source = "default"
            return AudioLevels()
        if not isinstance(raw, dict):
            log.warning("audio levels: %s is not a JSON object — using 0 dB",
                        self.path)
            self.source = "default"
            return AudioLevels()
        levels = AudioLevels()
        for key in KEYS:
            if key not in raw:
                continue
            number = _as_number(raw[key])
            if number is None:
                log.warning("audio levels: %s=%r is not a number — ignored",
                            key, raw[key])
                continue
            lo, hi = LIMITS[key]
            if not (lo <= number <= hi):
                # Hand-edited file: clamp, but say so (the REST surface refuses).
                log.warning("audio levels: %s=%g out of range %g…%g — clamped",
                            key, number, lo, hi)
                number = clamp_db(number, (lo, hi))
            levels = levels.replace(**{key: number})
        self.source = "file"
        return levels

    def save(self, levels: AudioLevels) -> bool:
        """Write atomically; False (logged, not raised) when the disk says no."""
        tmp_path: Optional[str] = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(
                prefix=self.path.name + ".", suffix=".tmp",
                dir=str(self.path.parent))
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(levels.to_wire(), handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(tmp_path, self.path)
            tmp_path = None
            return True
        except Exception as exc:                            # noqa: BLE001 - never fatal
            log.warning("audio levels: could not write %s (%s) — kept in memory",
                        self.path, exc)
            return False
        finally:
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass


class ClipMonitor:
    """Per-direction clipping counters, with a rate-limited warning.

    A software gain is the one thing that can make the box itself the source of
    distortion, so the count has to exist and the log line has to say when it
    started — but a per-frame warning at 50 Hz floods the log (this project has
    done that twice).  ``note()`` therefore warns at most once per interval.
    """

    def __init__(self, warn_ratio: float = 0.01, warn_interval_s: float = 30.0,
                 warn=log.warning) -> None:
        self._warn_ratio = float(warn_ratio)
        self._warn_interval_s = float(warn_interval_s)
        self._warn = warn
        self._stats = {key: {"samples": 0, "clipped": 0} for key in ("tx", "rx")}
        self._last_warn: dict[str, Optional[float]] = {"tx": None, "rx": None}

    def note(self, direction: str, samples: int, clipped: int, *,
             gain_db: float, now: float) -> bool:
        """Record one block.  True when this call emitted a warning."""
        bucket = self._stats.setdefault(direction,
                                        {"samples": 0, "clipped": 0})
        bucket["samples"] += max(0, int(samples))
        bucket["clipped"] += max(0, int(clipped))
        if not clipped or gain_db <= 0 or samples <= 0:
            return False
        if clipped / float(samples) < self._warn_ratio:
            return False
        last = self._last_warn.get(direction)
        if last is not None and (now - last) < self._warn_interval_s:
            return False
        self._last_warn[direction] = now
        self._warn(
            "audio levels: %s gain %+.0f dB is clipping the signal "
            "(%d/%d samples in the last block; totals %d/%d) — lower it, "
            "a clipped TX splatters and a clipped RX sounds like the box",
            direction.upper(), gain_db, clipped, samples,
            bucket["clipped"], bucket["samples"])
        return True

    def snapshot(self) -> dict[str, dict[str, int]]:
        return {key: dict(value) for key, value in self._stats.items()}
