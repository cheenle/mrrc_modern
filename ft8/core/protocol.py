"""Bounded versioned control frames for the DSP Worker pipe.

Large RX and TX arrays never enter this JSON protocol.  They remain in
parent-owned shared-memory segments described by exact, fixed-shape metadata.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import re


PROTOCOL_VERSION = 1
MAX_CONTROL_FRAME = 65_536

_MAX_ID = 2**63 - 1
_MAX_ERROR_DETAIL = 512
_BASE_FIELDS = frozenset({"v", "type", "generation", "request_id"})
_FRAME_FIELDS = {
    "ping": _BASE_FIELDS,
    "pong": _BASE_FIELDS,
    "decode": _BASE_FIELDS
    | {"slot_id", "deadline_monotonic", "shm", "config"},
    "decode_ok": _BASE_FIELDS
    | {
        "slot_id",
        "path",
        "results",
        "overflow",
        "elapsed_seconds",
        "deadline_missed",
    },
    "encode": _BASE_FIELDS | {"message", "frequency", "sample_rate", "shm"},
    "encode_ok": _BASE_FIELDS | {"message", "sample_rate", "sample_count"},
    "error": _BASE_FIELDS | {"code", "detail"},
    "shutdown": _BASE_FIELDS,
    "stopped": _BASE_FIELDS,
}
_CONFIG_FIELDS = frozenset(
    {
        "path",
        "sample_rate",
        "sample_count",
        "profile",
        "threads",
        "cycles",
        "sensitivity",
        "ap",
        "low_threshold",
        "wide_dx",
        "hide_duplicates",
        "qso_progress",
        "rx_frequency",
        "tx_frequency",
        "low_frequency",
        "high_frequency",
        "ap_width",
        "utc_hhmmss",
        "my_call",
        "dx_call",
        "dx_grid",
    }
)
_CONFIG_INTEGER_FIELDS = frozenset(
    {
        "profile",
        "threads",
        "cycles",
        "sensitivity",
        "qso_progress",
        "rx_frequency",
        "tx_frequency",
        "low_frequency",
        "high_frequency",
        "ap_width",
        "utc_hhmmss",
    }
)
_CONFIG_BOOLEAN_FIELDS = frozenset(
    {"ap", "low_threshold", "wide_dx", "hide_duplicates"}
)
_CONFIG_TEXT_LIMITS = {"my_call": 12, "dx_call": 12, "dx_grid": 6}
_RESULT_FIELDS = frozenset(
    {
        "slot_id",
        "sync",
        "snr",
        "dt",
        "frequency",
        "text",
        "ap_type",
        "quality",
        "flags",
    }
)
_SHM_FIELDS = frozenset({"name", "dtype", "shape", "nbytes"})
_ERROR_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


class FrameError(ValueError):
    """A control frame violates the version, size or exact schema contract."""


@dataclass(frozen=True, slots=True)
class SharedMemorySpec:
    """Validated description of one fixed shared-memory array."""

    name: str
    dtype: str
    shape: tuple[int, ...]
    nbytes: int


def encode_frame(frame: object) -> bytes:
    """Validate and deterministically serialize one Protocol v1 frame."""

    _validate_frame(frame)
    try:
        encoded = json.dumps(
            frame,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as error:
        raise FrameError("frame is not valid finite UTF-8 JSON") from error
    _validate_size(encoded)
    return encoded


def decode_frame(raw: bytes) -> dict[str, object]:
    """Parse one bounded UTF-8 JSON frame and apply the exact same schema."""

    if not isinstance(raw, bytes):
        raise FrameError("control frame must be raw bytes")
    _validate_size(raw)
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise FrameError("control frame is not valid UTF-8") from error
    try:
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, UnicodeError, ValueError) as error:
        if isinstance(error, FrameError):
            raise
        raise FrameError("control frame is not valid JSON") from error
    _validate_frame(value)
    return value


def _validate_size(raw: bytes) -> None:
    if len(raw) > MAX_CONTROL_FRAME:
        raise FrameError(f"control frame exceeds {MAX_CONTROL_FRAME} bytes")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise FrameError("control frame contains a duplicate JSON key")
        value[key] = item
    return value


def _reject_json_constant(value: str) -> object:
    raise FrameError(f"JSON constant {value} is not finite")


def _validate_frame(value: object) -> None:
    if not isinstance(value, dict):
        raise FrameError("control frame must be a JSON object")
    missing_base = _BASE_FIELDS - set(value)
    if missing_base:
        raise FrameError(f"frame missing fields: {', '.join(sorted(missing_base))}")
    version = value.get("v")
    if type(version) is not int or version != PROTOCOL_VERSION:
        raise FrameError(f"protocol version must be integer {PROTOCOL_VERSION}")
    frame_type = value.get("type")
    if not isinstance(frame_type, str) or frame_type not in _FRAME_FIELDS:
        raise FrameError("unsupported frame type")
    _exact_fields(value, _FRAME_FIELDS[frame_type], "frame")
    _nonnegative_int64(value["generation"], "generation")
    _nonnegative_int64(value["request_id"], "request_id")

    if frame_type == "decode":
        _nonnegative_int64(value["slot_id"], "slot_id")
        _finite_number(value["deadline_monotonic"], "deadline_monotonic", 0.0)
        _validate_shm(value["shm"], "decode")
        _validate_config(value["config"])
    elif frame_type == "decode_ok":
        _nonnegative_int64(value["slot_id"], "slot_id")
        _decode_path(value["path"], "path")
        results = value["results"]
        if not isinstance(results, list):
            raise FrameError("results must be an array")
        if len(results) > 256:
            raise FrameError("results cannot contain more than 256 records")
        for result in results:
            _validate_result(result)
        _boolean(value["overflow"], "overflow")
        _finite_number(value["elapsed_seconds"], "elapsed_seconds", 0.0)
        _boolean(value["deadline_missed"], "deadline_missed")
    elif frame_type == "encode":
        _bounded_text(value["message"], "message", 37, ascii_only=True)
        _finite_number(value["frequency"], "frequency")
        _exact_integer(value["sample_rate"], "sample_rate", 48_000)
        _validate_shm(value["shm"], "encode")
    elif frame_type == "encode_ok":
        _bounded_text(value["message"], "message", 37, ascii_only=True)
        _exact_integer(value["sample_rate"], "sample_rate", 48_000)
        _exact_integer(value["sample_count"], "sample_count", 606_720)
    elif frame_type == "error":
        code = value["code"]
        if not isinstance(code, str) or _ERROR_CODE.fullmatch(code) is None:
            raise FrameError("error code must be stable lowercase identifier")
        _bounded_text(value["detail"], "error detail", _MAX_ERROR_DETAIL)


def _validate_shm(value: object, operation: str) -> SharedMemorySpec:
    if not isinstance(value, dict):
        raise FrameError("shm must be an object")
    _exact_fields(value, _SHM_FIELDS, "shm")
    name = value["name"]
    if (
        not isinstance(name, str)
        or not name
        or not name.isprintable()
        or "\x00" in name
        or len(name.encode("utf-8")) > 255
    ):
        raise FrameError("shm name must be nonempty printable text up to 255 bytes")
    expected = (
        ("<i2", [180_000], 360_000)
        if operation == "decode"
        else ("<f4", [606_720], 2_426_880)
    )
    dtype, shape, nbytes = expected
    if value["dtype"] != dtype:
        raise FrameError(f"shm dtype must be {dtype}")
    if value["shape"] != shape or not isinstance(value["shape"], list):
        raise FrameError(f"shm shape must be {shape}")
    if type(value["nbytes"]) is not int or value["nbytes"] != nbytes:
        raise FrameError(f"shm nbytes must be {nbytes}")
    return SharedMemorySpec(name, dtype, tuple(shape), nbytes)


def _validate_config(value: object) -> None:
    if not isinstance(value, dict):
        raise FrameError("config must be an object")
    _exact_fields(value, _CONFIG_FIELDS, "config")
    _decode_path(value["path"], "config path")
    _exact_integer(value["sample_rate"], "config sample_rate", 12_000)
    _exact_integer(value["sample_count"], "config sample_count", 180_000)
    for name in _CONFIG_INTEGER_FIELDS:
        _signed_int64(value[name], f"config {name}")
    for name in _CONFIG_BOOLEAN_FIELDS:
        _boolean(value[name], f"config {name}")
    for name, maximum in _CONFIG_TEXT_LIMITS.items():
        _bounded_text(
            value[name], f"config {name}", maximum, ascii_only=True
        )


def _validate_result(value: object) -> None:
    if not isinstance(value, dict):
        raise FrameError("result must be an object")
    _exact_fields(value, _RESULT_FIELDS, "result")
    _nonnegative_int64(value["slot_id"], "result slot_id")
    for name in ("sync", "dt", "frequency", "quality"):
        _finite_number(value[name], f"result {name}")
    for name in ("snr", "ap_type", "flags"):
        _signed_int64(value[name], f"result {name}")
    _bounded_text(value["text"], "result text", 256)


def _exact_fields(
    value: dict[str, object], expected: frozenset[str] | set[str], context: str
) -> None:
    actual = set(value)
    missing = expected - actual
    if missing:
        raise FrameError(f"{context} missing fields: {', '.join(sorted(missing))}")
    extra = actual - expected
    if extra:
        raise FrameError(f"{context} extra fields: {', '.join(sorted(extra))}")


def _decode_path(value: object, name: str) -> None:
    if type(value) is not str or value not in {"standard", "improved"}:
        raise FrameError(f"{name} must be standard or improved")


def _exact_integer(value: object, name: str, expected: int) -> None:
    if type(value) is not int or value != expected:
        raise FrameError(f"{name} must be integer {expected}")


def _nonnegative_int64(value: object, name: str) -> None:
    if type(value) is not int or not 0 <= value <= _MAX_ID:
        raise FrameError(f"{name} must be a nonnegative int64")


def _signed_int64(value: object, name: str) -> None:
    if type(value) is not int or not -(2**63) <= value <= _MAX_ID:
        raise FrameError(f"{name} must be an int64")


def _boolean(value: object, name: str) -> None:
    if type(value) is not bool:
        raise FrameError(f"{name} must be a boolean")


def _finite_number(value: object, name: str, minimum: float | None = None) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FrameError(f"{name} must be a finite number")
    try:
        finite = math.isfinite(value)
    except (OverflowError, TypeError, ValueError):
        finite = False
    if not finite:
        raise FrameError(f"{name} must be a finite number")
    if minimum is not None and value < minimum:
        raise FrameError(f"{name} must be at least {minimum}")


def _bounded_text(
    value: object,
    name: str,
    maximum: int,
    *,
    ascii_only: bool = False,
) -> None:
    if not isinstance(value, str):
        raise FrameError(f"{name} must be text")
    try:
        encoded = value.encode("ascii" if ascii_only else "utf-8")
    except UnicodeEncodeError as error:
        raise FrameError(f"{name} must be ASCII text") from error
    if len(encoded) > maximum:
        raise FrameError(f"{name} must be no more than {maximum} bytes")
    if value and not value.isprintable():
        raise FrameError(f"{name} must be printable")
