"""CQ asset validation, peak normalisation and atomic write (spec 2026-10-26 §3).

The browser records whatever the operator's microphone hears; this module is the
gate that decides whether that recording is a usable CQ call.  Everything here
is stdlib + numpy, so the rules are testable without a radio, a browser or a
sound card — the same split `audio_levels.py` and `net_wifi.py` follow.

Two refusals worth naming, because they are the ones a field report will hit:

* **16-bit PCM only.** `CQPlayer.load()` already refuses anything else, and the
  browser encoder writes exactly that — so a wrong-format upload can only come
  from a hand-made file, and it deserves a sentence rather than a stack trace.
* **Near-silence is a refusal, not a file.** A CQ recorded with the microphone
  muted would key the radio, transmit nothing, and look like a product fault.
"""
from __future__ import annotations

import io
import math
import os
import tempfile
import wave
from pathlib import Path
from typing import Tuple

import numpy as np

MIN_SECONDS = 0.5
MAX_SECONDS = 30.0
MIN_PEAK_DBFS = -40.0
TARGET_PEAK_DBFS = -3.0
MAX_BYTES = 4 * 1024 * 1024
MIN_RATE = 8_000
MAX_RATE = 192_000
FULL_SCALE = 32768.0


class CQAssetError(Exception):
    """A refusal whose ``str()`` is the sentence shown to the operator."""


def _dbfs(peak: int) -> float:
    return 20.0 * math.log10(peak / FULL_SCALE) if peak > 0 else float("-inf")


def inspect_wav(blob: bytes) -> dict:
    """Validate a recording; raise ``CQAssetError`` with an operator-facing reason.

    Returns a dict with ``channels``/``rate``/``duration_s``/``peak_dbfs`` and
    the decoded ``samples`` (interleaved int16) for the caller's normalisation.
    """
    if not blob:
        raise CQAssetError("上传内容为空")
    if len(blob) > MAX_BYTES:
        raise CQAssetError(
            f"文件太大（{len(blob) / 1024 / 1024:.1f} MB），上限 {MAX_BYTES // 1024 // 1024} MB")
    try:
        with wave.open(io.BytesIO(blob), "rb") as w:
            width = w.getsampwidth()
            channels = w.getnchannels()
            rate = w.getframerate()
            raw = w.readframes(w.getnframes())
    except Exception:                                   # noqa: BLE001 - any parse failure
        raise CQAssetError("不是有效的 WAV 文件")
    if width != 2:
        raise CQAssetError(f"需要 16-bit PCM WAV（当前 {width * 8}-bit）")
    if not (MIN_RATE <= rate <= MAX_RATE):
        raise CQAssetError(f"采样率 {rate} Hz 不支持（{MIN_RATE}–{MAX_RATE} Hz）")
    if channels not in (1, 2):
        raise CQAssetError(f"只支持单声道或立体声（当前 {channels} 声道）")
    samples = np.frombuffer(raw, dtype="<i2")
    if samples.size == 0:
        raise CQAssetError("录音是空的")
    duration = samples.size / channels / rate
    if duration < MIN_SECONDS:
        raise CQAssetError(f"太短了（{duration:.2f} 秒），至少 {MIN_SECONDS:.1f} 秒")
    if duration > MAX_SECONDS:
        raise CQAssetError(f"太长了（{duration:.1f} 秒），上限 {MAX_SECONDS:.0f} 秒")
    peak = int(np.abs(samples.astype(np.int32)).max())
    peak_dbfs = _dbfs(peak)
    if peak == 0:
        raise CQAssetError("录到的是静音——检查麦克风有没有被静音")
    if peak_dbfs < MIN_PEAK_DBFS:
        raise CQAssetError(
            f"音量太低（{peak_dbfs:.0f} dBFS），把麦克风靠近一点再录")
    return {
        "channels": channels,
        "rate": rate,
        "duration_s": round(duration, 2),
        "peak_dbfs": round(peak_dbfs, 1),
        "samples": samples,
    }


def _encode(channels: int, rate: int, samples: np.ndarray) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(np.ascontiguousarray(samples, dtype="<i2").tobytes())
    return out.getvalue()


def normalize_wav(blob: bytes, target_dbfs: float = TARGET_PEAK_DBFS) -> Tuple[bytes, dict]:
    """Peak-normalise *up* to ``target_dbfs``; never attenuate.

    Boosting is the point (microphone distance is not under our control).  A take
    that already peaks above the target is stored as recorded: turning a perfectly
    good CQ down would only invite the operator to record again, louder.
    """
    info = inspect_wav(blob)
    peak_dbfs = info["peak_dbfs"]
    gain_db = target_dbfs - peak_dbfs
    applied = 0.0
    samples = info["samples"]
    if gain_db > 0.0:
        factor = 10.0 ** (gain_db / 20.0)
        scaled = np.rint(samples.astype(np.float64) * factor)
        samples = np.clip(scaled, -32768, 32767).astype("<i2")
        applied = round(gain_db, 1)
    stored = _encode(info["channels"], info["rate"], samples)
    return stored, {
        "channels": info["channels"],
        "rate": info["rate"],
        "duration_s": info["duration_s"],
        "recorded_peak_dbfs": peak_dbfs,
        "stored_peak_dbfs": round(_dbfs(int(np.abs(samples.astype(np.int32)).max())), 1),
        "applied_gain_db": applied,
    }


def write_atomic(path: Path, blob: bytes) -> None:
    """Write ``blob`` to ``path`` or leave the previous file untouched.

    `os.replace` is atomic on both platforms this ships on; the temp file lives in
    the same directory so the rename never crosses a filesystem boundary.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".cq-", suffix=".wav")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(blob)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
