"""Sole locked ctypes boundary for the native WSJT-X DSP library."""

from __future__ import annotations

import os

# Improved sync8var allocates multi-megabyte arrays on each OpenMP stack.  This
# must precede NumPy/SciPy imports, CDLL loading, and any OpenMP runtime load.
os.environ.setdefault("OMP_STACKSIZE", "10M")

import ctypes
import math
import threading
import time
from pathlib import Path
from typing import Protocol

import numpy as np

from ft8.core.models import (
    FT8_RX_RATE,
    FT8_RX_SAMPLES,
    FT8_TX_RATE,
    FT8_TX_SAMPLES,
    RESULT_CAPACITY,
    DecodeBatch,
    DecodeConfig,
    DecodePath,
    DecodeResult,
    EncodeResult,
)


_ABI_VERSION = 2
_FAST_PATH_MASK = 0b111  # standard | improved | fast (wsjt_core_shim.f90 z'07')
_TEXT_BYTES = 38
_IMPROVED_PROFILE_MASK = 0x1F
_MAX_THREADS = 12
_MAX_CYCLES = 3
_STATUS_NAMES = {
    1: "null pointer",
    2: "ABI mismatch",
    3: "sample rate",
    4: "sample shape",
    5: "configuration",
    6: "capacity",
    7: "encode",
    8: "internal",
}


class _AbiInfo(ctypes.Structure):
    _fields_ = [
        ("abi_version", ctypes.c_int32),
        ("struct_size", ctypes.c_int32),
        ("result_size", ctypes.c_int32),
        ("result_capacity", ctypes.c_int32),
        ("ft8_rx_rate", ctypes.c_int32),
        ("ft8_rx_samples", ctypes.c_int32),
        ("ft8_tx_rate", ctypes.c_int32),
        ("ft8_tx_samples", ctypes.c_int32),
        ("improved_profiles", ctypes.c_int32),
        ("max_threads", ctypes.c_int32),
        ("max_cycles", ctypes.c_int32),
        ("decode_paths", ctypes.c_int32),
    ]


class _DecodeConfig(ctypes.Structure):
    _fields_ = [
        ("struct_size", ctypes.c_int32),
        ("sample_rate", ctypes.c_int32),
        ("sample_count", ctypes.c_int32),
        ("profile", ctypes.c_int32),
        ("threads", ctypes.c_int32),
        ("cycles", ctypes.c_int32),
        ("sensitivity", ctypes.c_int32),
        ("flags", ctypes.c_int32),
        ("qso_progress", ctypes.c_int32),
        ("rx_frequency", ctypes.c_int32),
        ("tx_frequency", ctypes.c_int32),
        ("low_frequency", ctypes.c_int32),
        ("high_frequency", ctypes.c_int32),
        ("ap_width", ctypes.c_int32),
        ("utc_hhmmss", ctypes.c_int32),
        ("reserved", ctypes.c_int32),
        ("my_call", ctypes.c_char * 13),
        ("dx_call", ctypes.c_char * 13),
        ("dx_grid", ctypes.c_char * 7),
        ("padding", ctypes.c_char * 3),
    ]


class _DecodeResult(ctypes.Structure):
    _fields_ = [
        ("slot_id", ctypes.c_int64),
        ("sync", ctypes.c_float),
        ("dt", ctypes.c_float),
        ("frequency", ctypes.c_float),
        ("quality", ctypes.c_float),
        ("snr", ctypes.c_int32),
        ("ap_type", ctypes.c_int32),
        ("flags", ctypes.c_int32),
        ("reserved", ctypes.c_int32),
        ("text", ctypes.c_char * _TEXT_BYTES),
        ("padding", ctypes.c_char * 2),
    ]


def _call_native(fn: object) -> object:
    """Run a native library call on a large-stack thread on Windows.

    The Fortran decode allocates multi-hundred-KB automatic arrays on the
    caller stack; the Windows main-thread default is 1 MB (macOS/Linux give
    8 MB+), which overflows.  ``threading.stack_size`` only affects threads
    created afterwards, so each heavy native call runs on a dedicated thread.
    """
    if os.name != "nt":
        return fn()  # type: ignore[operator]
    threading.stack_size(16 * 1024 * 1024)
    holder: dict[str, object] = {}

    def target() -> None:
        holder["value"] = fn()  # type: ignore[operator]

    thread = threading.Thread(target=target)
    thread.start()
    thread.join()
    return holder["value"]


_DECODE_ARGTYPES = [
    ctypes.POINTER(ctypes.c_int16),
    ctypes.POINTER(_DecodeConfig),
    ctypes.c_int64,
    ctypes.POINTER(_DecodeResult),
    ctypes.c_int32,
    ctypes.POINTER(ctypes.c_int32),
    ctypes.POINTER(ctypes.c_int32),
]


DSP_LOCK = threading.RLock()


class AbiCompatibilityError(RuntimeError):
    """The loaded library does not implement the exact supported ABI."""


class DspStatusError(RuntimeError):
    """A native DSP operation returned a nonzero ``wsjt_status``."""

    def __init__(self, operation: str, status: int, detail: str = "") -> None:
        self.operation = operation
        self.status = status
        status_name = _STATUS_NAMES.get(status, "unknown")
        suffix = f": {detail}" if detail else ""
        super().__init__(f"{operation} failed with status {status} ({status_name}){suffix}")


class _Adapter(Protocol):
    def abi_info(self) -> tuple[int, dict[str, int]]: ...

    def decode(
        self,
        samples: np.ndarray,
        config: _DecodeConfig,
        path: DecodePath,
        slot_id: int,
    ) -> tuple[int, list[object], bool | int]: ...

    def encode(
        self,
        message: bytes,
        frequency: float,
        sample_rate: int,
        output: np.ndarray,
    ) -> tuple[int, object, int]: ...


class _CtypesAdapter:
    """Private adapter from validated Python data to the exact C ABI."""

    __slots__ = ("_library",)

    def __init__(self, library_path: str | os.PathLike[str]) -> None:
        path = Path(library_path)
        if os.name == "nt" and path.parent.is_dir():
            # Python 3.8+ uses secure DLL loading on Windows and ignores PATH;
            # the MinGW runtime DLLs (libgfortran-5.dll, libfftw3f_threads-3.dll,
            # libstdc++-6.dll, ...) sit next to wsjt_core.dll, so add that dir.
            os.add_dll_directory(str(path.parent))
        library = ctypes.CDLL(str(path))
        library.wsjt_get_abi_info.argtypes = [ctypes.POINTER(_AbiInfo)]
        library.wsjt_get_abi_info.restype = ctypes.c_int32
        library.wsjt_ft8_encode.argtypes = [
            ctypes.POINTER(ctypes.c_char),
            ctypes.c_float,
            ctypes.c_int32,
            ctypes.POINTER(ctypes.c_float),
            ctypes.c_int32,
            ctypes.POINTER(ctypes.c_int32),
            ctypes.POINTER(ctypes.c_char),
        ]
        library.wsjt_ft8_encode.restype = ctypes.c_int32
        library.wsjt_ft8_decode_standard.argtypes = _DECODE_ARGTYPES
        library.wsjt_ft8_decode_standard.restype = ctypes.c_int32
        library.wsjt_ft8_decode_improved.argtypes = _DECODE_ARGTYPES
        library.wsjt_ft8_decode_improved.restype = ctypes.c_int32
        # The fast path symbol only exists as of ABI 2.  If an older library is
        # loaded, ctypes raises AttributeError here -- and this runs before
        # CoreBinding._validate_abi().  Without this guard a version mismatch
        # would escape as an AttributeError from deep inside the adapter instead
        # of the documented AbiCompatibilityError.
        try:
            library.wsjt_ft8_decode_fast.argtypes = _DECODE_ARGTYPES
            library.wsjt_ft8_decode_fast.restype = ctypes.c_int32
        except AttributeError as exc:
            raise AbiCompatibilityError(
                "decode_paths mismatch: library has no wsjt_ft8_decode_fast"
            ) from exc
        self._library = library

    def abi_info(self) -> tuple[int, dict[str, int]]:
        info = _AbiInfo()
        status = int(self._library.wsjt_get_abi_info(ctypes.byref(info)))
        return status, {name: int(getattr(info, name)) for name, _type in info._fields_}

    def decode(
        self,
        samples: np.ndarray,
        config: _DecodeConfig,
        path: DecodePath,
        slot_id: int,
    ) -> tuple[int, list[object], int]:
        results = (_DecodeResult * RESULT_CAPACITY)()
        count = ctypes.c_int32()
        overflow = ctypes.c_int32()
        if path is DecodePath.STANDARD:
            function = self._library.wsjt_ft8_decode_standard
        elif path is DecodePath.FAST:
            function = self._library.wsjt_ft8_decode_fast
        else:
            function = self._library.wsjt_ft8_decode_improved
        status = int(
            function(
                samples.ctypes.data_as(ctypes.POINTER(ctypes.c_int16)),
                ctypes.byref(config),
                slot_id,
                results,
                RESULT_CAPACITY,
                ctypes.byref(count),
                ctypes.byref(overflow),
            )
        )
        if status != 0:
            return status, [], 0
        if not 0 <= count.value <= RESULT_CAPACITY or overflow.value not in (0, 1):
            return 8, [], 0
        return status, list(results[: count.value]), int(overflow.value)

    def encode(
        self,
        message: bytes,
        frequency: float,
        sample_rate: int,
        output: np.ndarray,
    ) -> tuple[int, object, int]:
        message_buffer = ctypes.create_string_buffer(message, _TEXT_BYTES)
        written = ctypes.c_int32()
        sent = ctypes.create_string_buffer(_TEXT_BYTES)
        status = int(
            self._library.wsjt_ft8_encode(
                message_buffer,
                frequency,
                sample_rate,
                output.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
                FT8_TX_SAMPLES,
                ctypes.byref(written),
                sent,
            )
        )
        return status, sent.raw, int(written.value)


class _TestAdapter:
    """Private no-CDLL adapter for deterministic boundary tests."""

    __slots__ = ("_native",)

    def __init__(self, native: object) -> None:
        self._native = native

    def abi_info(self) -> tuple[int, dict[str, int]]:
        provider = getattr(self._native, "abi_info", None)
        if provider is None:
            return 0, dict(_EXPECTED_ABI)
        reply = provider()
        if isinstance(reply, tuple):
            status, values = reply
            return int(status), dict(values)
        return 0, dict(reply)

    def decode(
        self,
        samples: np.ndarray,
        config: _DecodeConfig,
        path: DecodePath,
        slot_id: int,
    ) -> tuple[int, list[object], bool | int]:
        del samples, config, path, slot_id
        reply = self._native.decode()  # type: ignore[attr-defined]
        if isinstance(reply, tuple) and len(reply) == 3:
            status, results, overflow = reply
            status = int(status)
            if status != 0:
                return status, [], 0
            return status, list(results), overflow
        return 0, list(reply), False

    def encode(
        self,
        message: bytes,
        frequency: float,
        sample_rate: int,
        output: np.ndarray,
    ) -> tuple[int, object, int]:
        del message, frequency, sample_rate, output
        reply = self._native.encode()  # type: ignore[attr-defined]
        if isinstance(reply, tuple) and len(reply) == 3:
            status, sent, written = reply
            return int(status), sent, int(written)
        return 0, reply, FT8_TX_SAMPLES


_EXPECTED_ABI = {
    "abi_version": _ABI_VERSION,
    "struct_size": ctypes.sizeof(_AbiInfo),
    "result_size": ctypes.sizeof(_DecodeResult),
    "result_capacity": RESULT_CAPACITY,
    "ft8_rx_rate": FT8_RX_RATE,
    "ft8_rx_samples": FT8_RX_SAMPLES,
    "ft8_tx_rate": FT8_TX_RATE,
    "ft8_tx_samples": FT8_TX_SAMPLES,
    "improved_profiles": _IMPROVED_PROFILE_MASK,
    "max_threads": _MAX_THREADS,
    "max_cycles": _MAX_CYCLES,
    "decode_paths": _FAST_PATH_MASK,
}


class CoreBinding:
    """Validated, globally serialized access to ``wsjt_core``."""

    __slots__ = ("_adapter",)

    def __init__(self, library_path: str | os.PathLike[str]) -> None:
        self._adapter: _Adapter = _CtypesAdapter(library_path)
        self._validate_abi()

    @classmethod
    def for_test(cls, native: object) -> CoreBinding:
        """Build the same boundary around a private no-argument test adapter."""

        instance = cls.__new__(cls)
        instance._adapter = _TestAdapter(native)
        instance._validate_abi()
        return instance

    def _validate_abi(self) -> None:
        with DSP_LOCK:
            status, actual = self._adapter.abi_info()
        if status != 0:
            raise DspStatusError("get_abi_info", status)
        for field, expected in _EXPECTED_ABI.items():
            value = actual.get(field)
            if value != expected:
                raise AbiCompatibilityError(
                    f"{field} mismatch: expected {expected}, received {value}"
                )

    def decode(
        self,
        samples: np.ndarray,
        config: DecodeConfig,
        slot_id: int,
    ) -> DecodeBatch:
        """Validate, invoke exactly one decode path, and copy its result batch."""

        _validate_samples(samples)
        native_config = _native_config(config)
        _require_integer("slot_id", slot_id, -(2**63), 2**63 - 1)
        started = time.monotonic()
        with DSP_LOCK:
            status, native_results, overflow = _call_native(  # type: ignore[assignment]
                lambda: self._adapter.decode(samples, native_config, config.path, slot_id)
            )
            if status != 0:
                raise DspStatusError("decode", status)
            overflow_valid = type(overflow) is bool or (
                type(overflow) is int and overflow in (0, 1)
            )
            if len(native_results) > RESULT_CAPACITY or not overflow_valid:
                raise DspStatusError("decode", 8, "invalid native result metadata")
            results = tuple(_copy_result(result) for result in native_results)
        elapsed = time.monotonic() - started
        return DecodeBatch(slot_id, config.path, results, bool(overflow), elapsed)

    def encode(
        self,
        message: str,
        frequency: float,
        sample_rate: int,
        output: np.ndarray,
    ) -> EncodeResult:
        """Write one exact 48 kHz waveform into the caller-owned output array."""

        message_bytes = _ascii_bytes("message", message, 37)
        if b"\0" in message_bytes:
            raise ValueError("message cannot contain a NUL byte")
        _require_integer("sample_rate", sample_rate, FT8_TX_RATE, FT8_TX_RATE)
        if isinstance(frequency, bool) or not isinstance(frequency, (int, float)):
            raise ValueError("frequency must be a finite number")
        try:
            frequency_value = float(frequency)
        except (OverflowError, ValueError) as error:
            raise ValueError("frequency must be a finite number") from error
        if not math.isfinite(frequency_value) or not 100.0 <= frequency_value <= 4910.0:
            raise ValueError("frequency must be finite and between 100 and 4910 Hz")
        _validate_output(output)
        with DSP_LOCK:
            status, sent, written = _call_native(  # type: ignore[assignment]
                lambda: self._adapter.encode(
                    message_bytes, frequency_value, sample_rate, output
                )
            )
            if status != 0:
                raise DspStatusError("encode", status)
            if written != FT8_TX_SAMPLES:
                raise DspStatusError(
                    "encode", 8, f"native wrote {written} samples, expected {FT8_TX_SAMPLES}"
                )
            sent_message = _decode_text(sent)
        return EncodeResult(sent_message, sample_rate, written)


def _validate_samples(samples: np.ndarray) -> None:
    if not isinstance(samples, np.ndarray) or samples.dtype != np.dtype(np.int16):
        raise ValueError("decode samples must be a NumPy int16 array")
    if samples.ndim != 1:
        raise ValueError("decode samples must be one-dimensional")
    if not samples.flags.c_contiguous:
        raise ValueError("decode samples must be C-contiguous")
    if not samples.flags.aligned:
        raise ValueError("decode samples must be aligned")
    if samples.size != FT8_RX_SAMPLES:
        raise ValueError(f"decode samples must contain exactly {FT8_RX_SAMPLES} values")


def _validate_output(output: np.ndarray) -> None:
    if not isinstance(output, np.ndarray) or output.dtype != np.dtype(np.float32):
        raise ValueError("encode output must be a NumPy float32 array")
    if output.ndim != 1:
        raise ValueError("encode output must be one-dimensional")
    if not output.flags.c_contiguous:
        raise ValueError("encode output must be C-contiguous")
    if not output.flags.aligned:
        raise ValueError("encode output must be aligned")
    if not output.flags.writeable:
        raise ValueError("encode output must be writeable")
    if output.size != FT8_TX_SAMPLES:
        raise ValueError(f"encode output must contain exactly {FT8_TX_SAMPLES} values")


def _native_config(config: DecodeConfig) -> _DecodeConfig:
    if not isinstance(config, DecodeConfig):
        raise ValueError("config must be a DecodeConfig")
    if not isinstance(config.path, DecodePath):
        raise ValueError("config path must be standard or improved")
    _require_integer("sample_rate", config.sample_rate, FT8_RX_RATE, FT8_RX_RATE)
    _require_integer("sample_count", config.sample_count, FT8_RX_SAMPLES, FT8_RX_SAMPLES)
    _require_integer("profile", config.profile, 0, 4)
    _require_integer("threads", config.threads, 1, _MAX_THREADS)
    _require_integer("cycles", config.cycles, 1, _MAX_CYCLES)
    _require_integer("sensitivity", config.sensitivity, 1, 3)
    _require_integer("qso_progress", config.qso_progress, 0, 5)
    for name in ("rx_frequency", "tx_frequency", "ap_width"):
        _require_integer(name, getattr(config, name), -(2**31), 2**31 - 1)
    _require_integer("low_frequency", config.low_frequency, 100, 4910)
    _require_integer("high_frequency", config.high_frequency, 100, 4910)
    if config.high_frequency - config.low_frequency < 100:
        raise ValueError("decode frequency window must be increasing and at least 100 Hz")
    _validate_hhmmss(config.utc_hhmmss)
    for name in ("ap", "low_threshold", "wide_dx", "hide_duplicates"):
        if type(getattr(config, name)) is not bool:
            raise ValueError(f"{name} must be a boolean")
    my_call = _ascii_bytes("my_call", config.my_call, 12)
    dx_call = _ascii_bytes("dx_call", config.dx_call, 12)
    dx_grid = _ascii_bytes("dx_grid", config.dx_grid, 6)
    if any(b"\0" in value for value in (my_call, dx_call, dx_grid)):
        raise ValueError("decode text fields cannot contain NUL bytes")
    flags = (
        int(config.ap)
        | (int(config.low_threshold) << 1)
        | (int(config.wide_dx) << 2)
        | (int(config.hide_duplicates) << 3)
    )
    native = _DecodeConfig()
    native.struct_size = ctypes.sizeof(_DecodeConfig)
    native.sample_rate = config.sample_rate
    native.sample_count = config.sample_count
    native.profile = config.profile
    native.threads = config.threads
    native.cycles = config.cycles
    native.sensitivity = config.sensitivity
    native.flags = flags
    native.qso_progress = config.qso_progress
    native.rx_frequency = config.rx_frequency
    native.tx_frequency = config.tx_frequency
    native.low_frequency = config.low_frequency
    native.high_frequency = config.high_frequency
    native.ap_width = config.ap_width
    native.utc_hhmmss = config.utc_hhmmss
    native.my_call = my_call
    native.dx_call = dx_call
    native.dx_grid = dx_grid
    return native


def _require_integer(name: str, value: object, minimum: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        if minimum == maximum:
            raise ValueError(f"{name} must be {minimum}")
        raise ValueError(f"{name} must be between {minimum} and {maximum}")


def _validate_hhmmss(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("utc_hhmmss must be a valid HHMMSS value")
    if not 0 <= value <= 235959:
        raise ValueError("utc_hhmmss must be a valid HHMMSS value")
    hour = value // 10000
    minute = (value // 100) % 100
    second = value % 100
    if hour > 23 or minute > 59 or second > 59:
        raise ValueError("utc_hhmmss must be a valid HHMMSS value")


def _ascii_bytes(name: str, value: object, maximum: int) -> bytes:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ASCII string")
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as error:
        raise ValueError(f"{name} must contain only ASCII characters") from error
    if len(encoded) > maximum:
        raise ValueError(f"{name} must be no more than {maximum} ASCII bytes")
    return encoded


def _decode_text(value: object) -> str:
    if isinstance(value, str):
        return value.split("\0", 1)[0].rstrip()
    raw = bytes(value).split(b"\0", 1)[0]
    return raw.decode("ascii", errors="replace").rstrip()


def _copy_result(value: object) -> DecodeResult:
    return DecodeResult(
        slot_id=int(getattr(value, "slot_id")),
        sync=float(getattr(value, "sync")),
        snr=int(getattr(value, "snr")),
        dt=float(getattr(value, "dt")),
        frequency=float(getattr(value, "frequency")),
        text=_decode_text(getattr(value, "text")),
        ap_type=int(getattr(value, "ap_type")),
        quality=float(getattr(value, "quality")),
        flags=int(getattr(value, "flags")),
    )
