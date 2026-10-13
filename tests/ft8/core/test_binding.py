from __future__ import annotations

import ctypes
import threading
import time
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

import ft8.core.binding as binding_module
from ft8.core.binding import AbiCompatibilityError, CoreBinding, DspStatusError
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


EXPECTED_ABI = {
    "abi_version": 2,
    "struct_size": 48,
    "result_size": 80,
    "result_capacity": RESULT_CAPACITY,
    "ft8_rx_rate": FT8_RX_RATE,
    "ft8_rx_samples": FT8_RX_SAMPLES,
    "ft8_tx_rate": FT8_TX_RATE,
    "ft8_tx_samples": FT8_TX_SAMPLES,
    "improved_profiles": 0x1F,
    "max_threads": 12,
    "max_cycles": 3,
    "decode_paths": 0b111,
}


class FakeNative:
    """No-argument test double required by the Task 6 adapter contract."""

    def __init__(self, *, abi_overrides: dict[str, int] | None = None) -> None:
        self.active = 0
        self.max_active = 0
        self.decode_calls = 0
        self.encode_calls = 0
        self.decode_reply: object = []
        self.encode_reply: object = (0, "CQ K1ABC FN42", FT8_TX_SAMPLES)
        self.guard = threading.Lock()
        self.abi_overrides = abi_overrides

    def abi_info(self) -> dict[str, int]:
        info = dict(EXPECTED_ABI)
        if self.abi_overrides:
            info.update(self.abi_overrides)
        return info

    def decode(self) -> object:
        with self.guard:
            self.decode_calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(0.02)
        with self.guard:
            self.active -= 1
        return self.decode_reply

    def encode(self) -> object:
        self.encode_calls += 1
        return self.encode_reply


class FakeFunction:
    def __init__(self, callback: Any) -> None:
        self.callback = callback
        self.argtypes: list[object] | None = None
        self.restype: object | None = None
        self.calls = 0

    def __call__(self, *args: object) -> int:
        self.calls += 1
        return int(self.callback(*args))


class FakeLibrary:
    def __init__(
        self,
        *,
        abi_overrides: dict[str, int] | None = None,
        decode_status: int = 0,
        decode_count: int = 1,
        decode_overflow: int = 0,
    ) -> None:
        info = dict(EXPECTED_ABI)
        if abi_overrides:
            info.update(abi_overrides)

        def get_abi_info(out: object) -> int:
            target = out._obj  # type: ignore[attr-defined]
            for name, value in info.items():
                setattr(target, name, value)
            return 0

        def decode(
            _samples: object,
            _config: object,
            slot_id: int,
            results: object,
            _capacity: int,
            count: object,
            overflow: object,
        ) -> int:
            self.seen_flags.append(_config._obj.flags)  # type: ignore[attr-defined]
            results[0].slot_id = slot_id  # type: ignore[index]
            results[0].sync = 2.5  # type: ignore[index]
            results[0].snr = -7  # type: ignore[index]
            results[0].dt = 0.125  # type: ignore[index]
            results[0].frequency = 1500.25  # type: ignore[index]
            results[0].quality = 0.75  # type: ignore[index]
            results[0].ap_type = 1  # type: ignore[index]
            results[0].flags = 9  # type: ignore[index]
            results[0].text = b"CQ K1ABC FN42   "  # type: ignore[index]
            count._obj.value = decode_count  # type: ignore[attr-defined]
            overflow._obj.value = decode_overflow  # type: ignore[attr-defined]
            return decode_status

        def encode(
            _message: object,
            _frequency: float,
            _sample_rate: int,
            wave: object,
            _capacity: int,
            written: object,
            sent: object,
        ) -> int:
            wave[0] = 0.5  # type: ignore[index]
            written._obj.value = FT8_TX_SAMPLES  # type: ignore[attr-defined]
            sent_text = b"CQ K1ABC FN42\0"
            ctypes.memmove(sent, sent_text, len(sent_text))
            return 0

        self.wsjt_get_abi_info = FakeFunction(get_abi_info)
        self.wsjt_ft8_encode = FakeFunction(encode)
        self.wsjt_ft8_decode_standard = FakeFunction(decode)
        self.wsjt_ft8_decode_improved = FakeFunction(decode)
        self.wsjt_ft8_decode_fast = FakeFunction(decode)
        self.seen_flags: list[int] = []


def pcm() -> np.ndarray:
    return np.zeros(FT8_RX_SAMPLES, dtype=np.int16)


def output() -> np.ndarray:
    return np.zeros(FT8_TX_SAMPLES, dtype=np.float32)


def assert_decode_signatures(library: FakeLibrary) -> None:
    expected = [
        ctypes.POINTER(ctypes.c_int16),
        ctypes.POINTER(binding_module._DecodeConfig),
        ctypes.c_int64,
        ctypes.POINTER(binding_module._DecodeResult),
        ctypes.c_int32,
        ctypes.POINTER(ctypes.c_int32),
        ctypes.POINTER(ctypes.c_int32),
    ]
    assert library.wsjt_ft8_decode_standard.argtypes == expected
    assert library.wsjt_ft8_decode_improved.argtypes == expected
    assert library.wsjt_ft8_decode_fast.argtypes == expected


def unaligned_array(dtype: np.dtype[Any], count: int) -> np.ndarray:
    itemsize = np.dtype(dtype).itemsize
    backing = np.zeros(count * itemsize + 1, dtype=np.uint8)
    value = np.ndarray((count,), dtype=dtype, buffer=backing, offset=1)
    assert value.flags.c_contiguous
    assert not value.flags.aligned
    return value


def test_models_are_frozen_slotted_values() -> None:
    config = DecodeConfig.standard()
    result = DecodeResult(1, 1.0, -10, 0.1, 1500.0, "CQ TEST", 0, 0.5, 0)
    batch = DecodeBatch(1, DecodePath.STANDARD, (result,), False, 0.01)
    encoded = EncodeResult("CQ TEST", FT8_TX_RATE, FT8_TX_SAMPLES)

    for value, field, replacement in (
        (config, "profile", 4),
        (result, "snr", -5),
        (batch, "slot_id", 2),
        (encoded, "message", "CQ OTHER"),
    ):
        assert not hasattr(value, "__dict__")
        with pytest.raises(FrozenInstanceError):
            setattr(value, field, replacement)
    assert config.path is DecodePath.STANDARD
    assert config.replace(profile=4).profile == 4
    assert config.profile == 3
    assert batch.results == (result,)


@pytest.mark.parametrize(
    "samples",
    [
        pytest.param(np.zeros(FT8_RX_SAMPLES, np.float32), id="dtype"),
        pytest.param(np.zeros((90_000, 2), np.int16), id="dimensions"),
        pytest.param(np.zeros(360_000, np.int16)[::2], id="contiguity"),
        pytest.param(np.zeros(FT8_RX_SAMPLES - 1, np.int16), id="length"),
    ],
)
def test_decode_rejects_wrong_array_contract(samples: np.ndarray) -> None:
    with pytest.raises(ValueError):
        CoreBinding.for_test(FakeNative()).decode(samples, DecodeConfig.standard(), 1)


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"path": "other"}, "path"),
        ({"sample_rate": FT8_TX_RATE}, "12000"),
        ({"sample_count": FT8_RX_SAMPLES - 1}, "180000"),
        ({"profile": -1}, "profile"),
        ({"profile": 5}, "profile"),
        ({"threads": 0}, "threads"),
        ({"threads": 13}, "threads"),
        ({"cycles": 0}, "cycles"),
        ({"cycles": 4}, "cycles"),
        ({"sensitivity": 0}, "sensitivity"),
        ({"sensitivity": 4}, "sensitivity"),
        ({"qso_progress": -1}, "qso_progress"),
        ({"qso_progress": 6}, "qso_progress"),
        ({"utc_hhmmss": 236000}, "HHMMSS"),
        ({"utc_hhmmss": 126000}, "HHMMSS"),
        ({"utc_hhmmss": 125960}, "HHMMSS"),
        ({"low_frequency": 99}, "frequency"),
        ({"high_frequency": 4911}, "frequency"),
        ({"low_frequency": 200, "high_frequency": 299}, "100 Hz"),
        ({"ap": 1}, "boolean"),
    ],
)
def test_decode_rejects_invalid_config_bounds(
    changes: dict[str, object], match: str
) -> None:
    malformed = DecodeConfig.standard().replace(**changes)
    with pytest.raises(ValueError, match=match):
        CoreBinding.for_test(FakeNative()).decode(pcm(), malformed, 1)


@pytest.mark.parametrize(
    "changes",
    [
        {"my_call": "N" * 13},
        {"dx_call": "K" * 13},
        {"dx_grid": "FN4200X"},
        {"my_call": "呼号"},
    ],
)
def test_decode_rejects_non_ascii_or_oversize_text_fields(
    changes: dict[str, str],
) -> None:
    with pytest.raises(ValueError, match="ASCII|bytes"):
        CoreBinding.for_test(FakeNative()).decode(
            pcm(), DecodeConfig.standard().replace(**changes), 1
        )


def test_global_lock_serializes_four_native_calls() -> None:
    native = FakeNative()
    binding = CoreBinding.for_test(native)
    threads = [
        threading.Thread(
            target=binding.decode,
            args=(pcm(), DecodeConfig.standard(), slot_id),
        )
        for slot_id in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert native.decode_calls == 4
    assert native.max_active == 1


def test_decode_copies_results_to_immutable_models_and_trims_text() -> None:
    native = FakeNative()
    native.decode_reply = (
        0,
        [
            SimpleNamespace(
                slot_id=9,
                sync=2.0,
                snr=-5,
                dt=0.2,
                frequency=1501.0,
                text=b"CQ TEST   \0junk",
                ap_type=1,
                quality=0.9,
                flags=1,
            ),
            SimpleNamespace(
                slot_id=9,
                sync=1.0,
                snr=-20,
                dt=-0.1,
                frequency=1200.0,
                text=b"CQ \xff   ",
                ap_type=0,
                quality=0.1,
                flags=0,
            ),
        ],
        True,
    )

    batch = CoreBinding.for_test(native).decode(pcm(), DecodeConfig.standard(), 9)

    assert batch.slot_id == 9
    assert batch.path is DecodePath.STANDARD
    assert batch.overflow is True
    assert batch.elapsed_seconds >= 0
    assert batch.results[0].text == "CQ TEST"
    assert batch.results[1].text == "CQ �"
    assert isinstance(batch.results[0], DecodeResult)


@pytest.mark.parametrize("operation", ["decode", "encode"])
def test_nonzero_native_status_raises(operation: str) -> None:
    native = FakeNative()
    if operation == "decode":
        native.decode_reply = (5, [], False)
        call = lambda: CoreBinding.for_test(native).decode(  # noqa: E731
            pcm(), DecodeConfig.standard(), 1
        )
    else:
        native.encode_reply = (7, "", 0)
        call = lambda: CoreBinding.for_test(native).encode(  # noqa: E731
            "BAD", 1500.0, FT8_TX_RATE, output()
        )

    with pytest.raises(DspStatusError, match=operation):
        call()


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("abi_version", 1),
        ("struct_size", 47),
        ("result_size", 79),
        ("result_capacity", 255),
        ("ft8_rx_rate", 48_000),
        ("ft8_rx_samples", 179_999),
        ("ft8_tx_rate", 44_100),
        ("ft8_tx_samples", 606_719),
        ("improved_profiles", 0x0F),
        ("max_threads", 11),
        ("max_cycles", 2),
        ("decode_paths", 0b010),
    ],
)
def test_constructor_rejects_every_abi_or_capability_mismatch(
    field: str, bad_value: int
) -> None:
    with pytest.raises(AbiCompatibilityError, match=field):
        CoreBinding.for_test(FakeNative(abi_overrides={field: bad_value}))


@pytest.mark.parametrize(
    "bad_output",
    [
        pytest.param(np.zeros(FT8_TX_SAMPLES, np.float64), id="dtype"),
        pytest.param(np.zeros((FT8_TX_SAMPLES, 1), np.float32), id="dimensions"),
        pytest.param(np.zeros(FT8_TX_SAMPLES * 2, np.float32)[::2], id="contiguity"),
        pytest.param(np.zeros(FT8_TX_SAMPLES - 1, np.float32), id="length"),
    ],
)
def test_encode_rejects_wrong_output_contract(bad_output: np.ndarray) -> None:
    with pytest.raises(ValueError):
        CoreBinding.for_test(FakeNative()).encode(
            "CQ K1ABC FN42", 1500.0, FT8_TX_RATE, bad_output
        )


def test_decode_rejects_unaligned_input_before_native_call() -> None:
    native = FakeNative()
    samples = unaligned_array(np.dtype(np.int16), FT8_RX_SAMPLES)

    with pytest.raises(ValueError, match="aligned"):
        CoreBinding.for_test(native).decode(samples, DecodeConfig.standard(), 1)

    assert native.decode_calls == 0


@pytest.mark.parametrize("invalid", ["unaligned", "readonly"])
def test_encode_rejects_unsafe_output_before_native_call(invalid: str) -> None:
    native = FakeNative()
    wave = (
        unaligned_array(np.dtype(np.float32), FT8_TX_SAMPLES)
        if invalid == "unaligned"
        else output()
    )
    if invalid == "readonly":
        wave.setflags(write=False)

    with pytest.raises(ValueError, match="aligned|writeable"):
        CoreBinding.for_test(native).encode(
            "CQ K1ABC FN42", 1500.0, FT8_TX_RATE, wave
        )

    assert native.encode_calls == 0


@pytest.mark.parametrize(
    ("message", "frequency", "sample_rate", "match"),
    [
        ("CQ K1ABC FN42", 1500.0, 12_000, "48000"),
        ("M" * 38, 1500.0, FT8_TX_RATE, "37"),
        ("呼号", 1500.0, FT8_TX_RATE, "ASCII"),
        ("CQ K1ABC FN42", float("nan"), FT8_TX_RATE, "frequency"),
        ("CQ K1ABC FN42", 99.0, FT8_TX_RATE, "frequency"),
        ("CQ K1ABC FN42", 4911.0, FT8_TX_RATE, "frequency"),
        ("CQ K1ABC FN42", 10**400, FT8_TX_RATE, "frequency"),
    ],
)
def test_encode_rejects_invalid_message_rate_or_frequency(
    message: str, frequency: float, sample_rate: int, match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        CoreBinding.for_test(FakeNative()).encode(
            message, frequency, sample_rate, output()
        )


def test_encode_returns_metadata_and_leaves_waveform_in_caller_buffer() -> None:
    native = FakeNative()
    wave = output()

    result = CoreBinding.for_test(native).encode(
        "CQ K1ABC FN42", 1500.0, FT8_TX_RATE, wave
    )

    assert result == EncodeResult("CQ K1ABC FN42", FT8_TX_RATE, FT8_TX_SAMPLES)
    assert native.encode_calls == 1
    assert not hasattr(result, "waveform")


@pytest.mark.parametrize("count", [-1, RESULT_CAPACITY, 1_000_000])
def test_production_adapter_ignores_all_out_values_on_native_failure(
    monkeypatch: pytest.MonkeyPatch,
    count: int,
) -> None:
    library = FakeLibrary(
        decode_status=5,
        decode_count=count,
        decode_overflow=7,
    )
    monkeypatch.setattr(binding_module.ctypes, "CDLL", lambda _path: library)
    adapter = binding_module._CtypesAdapter("/tmp/libwsjt_core.so")

    status, results, overflow = adapter.decode(
        pcm(),
        binding_module._native_config(DecodeConfig.standard()),
        DecodePath.STANDARD,
        1,
    )

    assert status == 5
    assert results == []
    assert overflow == 0


@pytest.mark.parametrize(
    ("count", "overflow"),
    [(-1, 0), (RESULT_CAPACITY + 1, 0), (1_000_000, 0), (0, 7)],
)
def test_production_adapter_rejects_invalid_success_out_values(
    monkeypatch: pytest.MonkeyPatch,
    count: int,
    overflow: int,
) -> None:
    library = FakeLibrary(decode_count=count, decode_overflow=overflow)
    monkeypatch.setattr(binding_module.ctypes, "CDLL", lambda _path: library)
    adapter = binding_module._CtypesAdapter("/tmp/libwsjt_core.so")

    reply = adapter.decode(
        pcm(),
        binding_module._native_config(DecodeConfig.standard()),
        DecodePath.STANDARD,
        1,
    )

    assert reply == (8, [], 0)


def test_common_boundary_rejects_oversize_test_adapter_batch() -> None:
    native = FakeNative()
    record = SimpleNamespace(
        slot_id=1,
        sync=1.0,
        snr=-10,
        dt=0.0,
        frequency=1500.0,
        text=b"CQ TEST",
        ap_type=0,
        quality=0.0,
        flags=0,
    )
    native.decode_reply = (0, [record] * (RESULT_CAPACITY + 1), 0)

    with pytest.raises(DspStatusError, match="status 8"):
        CoreBinding.for_test(native).decode(pcm(), DecodeConfig.standard(), 1)


def test_common_boundary_rejects_non_binary_overflow() -> None:
    native = FakeNative()
    native.decode_reply = (0, [], 7)

    with pytest.raises(DspStatusError, match="status 8"):
        CoreBinding.for_test(native).decode(pcm(), DecodeConfig.standard(), 1)


@pytest.mark.parametrize(("raw", "expected"), [(False, False), (True, True), (0, False), (1, True)])
def test_common_boundary_accepts_exact_binary_overflow(
    raw: bool | int,
    expected: bool,
) -> None:
    native = FakeNative()
    native.decode_reply = (0, [], raw)

    batch = CoreBinding.for_test(native).decode(pcm(), DecodeConfig.standard(), 1)

    assert batch.overflow is expected


def test_explicit_library_path_sets_all_five_signatures_and_selects_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library = FakeLibrary()
    loaded_paths: list[str] = []
    monkeypatch.setattr(
        binding_module.ctypes,
        "CDLL",
        lambda path: loaded_paths.append(path) or library,
    )
    binding = CoreBinding(Path("/tmp/libwsjt_core.so"))

    all_flags = DecodeConfig.standard().replace(
        ap=True,
        low_threshold=True,
        wide_dx=True,
        hide_duplicates=True,
    )
    standard = binding.decode(pcm(), all_flags, 7)
    improved = binding.decode(pcm(), DecodeConfig(), 8)
    wave = output()
    encoded = binding.encode("CQ K1ABC FN42", 1500.0, FT8_TX_RATE, wave)

    assert loaded_paths == [str(Path("/tmp/libwsjt_core.so"))]
    functions = [
        library.wsjt_get_abi_info,
        library.wsjt_ft8_encode,
        library.wsjt_ft8_decode_standard,
        library.wsjt_ft8_decode_improved,
        library.wsjt_ft8_decode_fast,
    ]
    assert all(function.restype is ctypes.c_int32 for function in functions)
    assert library.wsjt_get_abi_info.argtypes == [
        ctypes.POINTER(binding_module._AbiInfo)
    ]
    assert library.wsjt_ft8_encode.argtypes == [
        ctypes.POINTER(ctypes.c_char),
        ctypes.c_float,
        ctypes.c_int32,
        ctypes.POINTER(ctypes.c_float),
        ctypes.c_int32,
        ctypes.POINTER(ctypes.c_int32),
        ctypes.POINTER(ctypes.c_char),
    ]
    assert_decode_signatures(library)
    assert library.wsjt_ft8_decode_standard.calls == 1
    assert library.wsjt_ft8_decode_improved.calls == 1
    assert library.seen_flags == [0x0F, 0x09]
    assert standard.results[0].text == "CQ K1ABC FN42"
    assert improved.results[0].slot_id == 8
    assert encoded.message == "CQ K1ABC FN42"
    assert wave[0] == pytest.approx(0.5)


def test_constructor_rejects_a_library_without_the_fast_symbol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pre-ABI-2 library has no ``wsjt_ft8_decode_fast``.

    The adapter must reject it at construction with ``AbiCompatibilityError`` — a
    missing fast symbol has to fail closed, never silently fall through to the
    standard/improved entry points.
    """

    library = FakeLibrary()
    del library.wsjt_ft8_decode_fast
    monkeypatch.setattr(binding_module.ctypes, "CDLL", lambda _path: library)

    with pytest.raises(AbiCompatibilityError, match="decode_paths"):
        CoreBinding(Path("/tmp/libwsjt_core.so"))


def test_decode_signature_assertion_detects_int32_slot_id_mutation() -> None:
    library = FakeLibrary()
    mutated = [
        ctypes.POINTER(ctypes.c_int16),
        ctypes.POINTER(binding_module._DecodeConfig),
        ctypes.c_int32,
        ctypes.POINTER(binding_module._DecodeResult),
        ctypes.c_int32,
        ctypes.POINTER(ctypes.c_int32),
        ctypes.POINTER(ctypes.c_int32),
    ]
    library.wsjt_ft8_decode_standard.argtypes = mutated
    library.wsjt_ft8_decode_improved.argtypes = mutated

    with pytest.raises(AssertionError):
        assert_decode_signatures(library)


def test_ctypes_layouts_match_wsjt_core_header() -> None:
    assert ctypes.sizeof(binding_module._AbiInfo) == 48
    assert ctypes.sizeof(binding_module._DecodeConfig) == 100
    assert ctypes.sizeof(binding_module._DecodeResult) == 80
    assert binding_module._DecodeConfig._fields_ == [
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
    assert binding_module._AbiInfo._fields_ == [
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
    assert binding_module._DecodeResult._fields_ == [
        ("slot_id", ctypes.c_int64),
        ("sync", ctypes.c_float),
        ("dt", ctypes.c_float),
        ("frequency", ctypes.c_float),
        ("quality", ctypes.c_float),
        ("snr", ctypes.c_int32),
        ("ap_type", ctypes.c_int32),
        ("flags", ctypes.c_int32),
        ("reserved", ctypes.c_int32),
        ("text", ctypes.c_char * 38),
        ("padding", ctypes.c_char * 2),
    ]
    assert {
        name: getattr(binding_module._AbiInfo, name).offset
        for name in ("abi_version", "result_size", "improved_profiles", "decode_paths")
    } == {
        "abi_version": 0,
        "result_size": 8,
        "improved_profiles": 32,
        "decode_paths": 44,
    }
    assert {
        name: getattr(binding_module._DecodeConfig, name).offset
        for name in ("struct_size", "reserved", "my_call", "dx_call", "dx_grid", "padding")
    } == {
        "struct_size": 0,
        "reserved": 60,
        "my_call": 64,
        "dx_call": 77,
        "dx_grid": 90,
        "padding": 97,
    }
    assert {
        name: getattr(binding_module._DecodeResult, name).offset
        for name in ("slot_id", "sync", "snr", "reserved", "text", "padding")
    } == {
        "slot_id": 0,
        "sync": 8,
        "snr": 24,
        "reserved": 36,
        "text": 40,
        "padding": 78,
    }


def test_fast_path_is_a_valid_decode_path() -> None:
    from ft8.core.models import DecodePath

    assert DecodePath("fast") is DecodePath.FAST


def test_validate_abi_rejects_missing_fast_bit() -> None:
    """掩码里没有 fast 位的库必须被拒 —— 复用该文件既有的 FakeNative 夹具。"""
    from ft8.core.binding import AbiCompatibilityError

    with pytest.raises(AbiCompatibilityError, match="decode_paths"):
        CoreBinding.for_test(FakeNative(abi_overrides={"decode_paths": 0b011}))


def test_fast_path_selects_the_fast_native_entry_point(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library = FakeLibrary()
    monkeypatch.setattr(binding_module.ctypes, "CDLL", lambda _path: library)
    binding = CoreBinding(Path("/tmp/libwsjt_core.so"))

    batch = binding.decode(pcm(), DecodeConfig(path=DecodePath.FAST), 11)

    assert batch.path is DecodePath.FAST
    assert library.wsjt_ft8_decode_fast.calls == 1
    assert library.wsjt_ft8_decode_standard.calls == 0
    assert library.wsjt_ft8_decode_improved.calls == 0
