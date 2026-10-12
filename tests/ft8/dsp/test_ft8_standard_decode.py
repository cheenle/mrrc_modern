from __future__ import annotations

import ctypes as c

import numpy as np
import pytest


class Config(c.Structure):
    _fields_ = [
        ("struct_size", c.c_int32),
        ("sample_rate", c.c_int32),
        ("sample_count", c.c_int32),
        ("profile", c.c_int32),
        ("threads", c.c_int32),
        ("cycles", c.c_int32),
        ("sensitivity", c.c_int32),
        ("flags", c.c_int32),
        ("qso_progress", c.c_int32),
        ("rx_frequency", c.c_int32),
        ("tx_frequency", c.c_int32),
        ("low_frequency", c.c_int32),
        ("high_frequency", c.c_int32),
        ("ap_width", c.c_int32),
        ("utc_hhmmss", c.c_int32),
        ("reserved", c.c_int32),
        ("my_call", c.c_char * 13),
        ("dx_call", c.c_char * 13),
        ("dx_grid", c.c_char * 7),
        ("padding", c.c_char * 3),
    ]


class Result(c.Structure):
    _fields_ = [
        ("slot_id", c.c_int64),
        ("sync", c.c_float),
        ("dt", c.c_float),
        ("frequency", c.c_float),
        ("quality", c.c_float),
        ("snr", c.c_int32),
        ("ap_type", c.c_int32),
        ("flags", c.c_int32),
        ("reserved", c.c_int32),
        ("text", c.c_char * 38),
        ("padding", c.c_char * 2),
    ]


DECODE_ARGTYPES = [
    c.POINTER(c.c_int16),
    c.POINTER(Config),
    c.c_int64,
    c.POINTER(Result),
    c.c_int32,
    c.POINTER(c.c_int32),
    c.POINTER(c.c_int32),
]


def config() -> Config:
    value = Config()
    value.struct_size = c.sizeof(Config)
    value.sample_rate = 12_000
    value.sample_count = 180_000
    value.profile = 3
    value.threads = 1
    value.cycles = 1
    value.sensitivity = 2
    value.flags = 1
    value.rx_frequency = 1500
    value.tx_frequency = 1500
    value.low_frequency = 200
    value.high_frequency = 3000
    value.ap_width = 50
    value.utc_hhmmss = 120000
    value.my_call = b"N0CALL"
    return value


def decode_function(raw_library: c.CDLL) -> c._CFuncPtr:
    assert hasattr(raw_library, "wsjt_ft8_decode_standard"), (
        "freshly built library is missing wsjt_ft8_decode_standard"
    )
    function = raw_library.wsjt_ft8_decode_standard
    function.argtypes = DECODE_ARGTYPES
    function.restype = c.c_int32
    return function


def test_standard_decode_struct_layout_matches_c_header() -> None:
    assert c.sizeof(Config) == 100
    assert c.sizeof(Result) == 80


def test_standard_decode_returns_known_message(
    raw_library: c.CDLL,
    cq_slot: np.ndarray,
) -> None:
    decode = decode_function(raw_library)
    results = (Result * 256)()
    count = c.c_int32(-1)
    overflow = c.c_int32(-1)
    value = config()

    status = decode(
        cq_slot.ctypes.data_as(c.POINTER(c.c_int16)),
        c.byref(value),
        11_908_800,
        results,
        256,
        c.byref(count),
        c.byref(overflow),
    )

    assert status == 0
    assert overflow.value == 0
    assert 0 < count.value <= 256
    messages = [results[index].text.decode().strip() for index in range(count.value)]
    assert "CQ K1ABC FN42" in messages
    decoded = results[messages.index("CQ K1ABC FN42")]
    assert decoded.slot_id == 11_908_800
    assert 1400.0 <= decoded.frequency <= 1600.0
    assert decoded.reserved == 0
    assert decoded.flags == (1 if decoded.ap_type != 0 else 0)
    raw_result = bytes(decoded)
    assert raw_result[40 + len("CQ K1ABC FN42")] == 0
    assert raw_result[78:80] == b"\0\0"


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("sample_rate", 48_000, 3),
        ("sample_count", 179_999, 4),
        ("capacity", 255, 6),
        ("struct_size", 99, 2),
        ("struct_size", 101, 2),
    ],
)
def test_standard_decode_rejects_invalid_abi_and_shape_without_copying_results(
    raw_library: c.CDLL,
    cq_slot: np.ndarray,
    field: str,
    value: int,
    expected: int,
) -> None:
    decode = decode_function(raw_library)
    results = (Result * 256)()
    c.memset(c.byref(results), 0xA5, c.sizeof(results))
    before = bytes(results)
    count = c.c_int32(91)
    overflow = c.c_int32(92)
    cfg = config()
    capacity = 256
    if field == "capacity":
        capacity = value
    else:
        setattr(cfg, field, value)

    status = decode(
        cq_slot.ctypes.data_as(c.POINTER(c.c_int16)),
        c.byref(cfg),
        1,
        results,
        capacity,
        c.byref(count),
        c.byref(overflow),
    )

    assert status == expected
    assert count.value == 0
    assert overflow.value == 0
    assert bytes(results) == before


@pytest.mark.parametrize("missing", ["samples", "config", "results", "count", "overflow"])
def test_standard_decode_rejects_every_null_pointer_without_partial_output(
    raw_library: c.CDLL,
    cq_slot: np.ndarray,
    missing: str,
) -> None:
    decode = decode_function(raw_library)
    results = (Result * 256)()
    c.memset(c.byref(results), 0x5A, c.sizeof(results))
    before = bytes(results)
    count = c.c_int32(91)
    overflow = c.c_int32(92)
    cfg = config()
    arguments: list[object] = [
        cq_slot.ctypes.data_as(c.POINTER(c.c_int16)),
        c.byref(cfg),
        1,
        results,
        256,
        c.byref(count),
        c.byref(overflow),
    ]
    arguments[{"samples": 0, "config": 1, "results": 3, "count": 5, "overflow": 6}[missing]] = None

    assert decode(*arguments) == 1
    if missing != "count":
        assert count.value == 0
    if missing != "overflow":
        assert overflow.value == 0
    assert bytes(results) == before


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"qso_progress": -1}, id="qso-progress-negative"),
        pytest.param({"qso_progress": 6}, id="qso-progress-too-high"),
        pytest.param({"utc_hhmmss": -5}, id="utc-negative"),
        pytest.param({"utc_hhmmss": 236000}, id="utc-hour"),
        pytest.param({"utc_hhmmss": 126000}, id="utc-minute"),
        pytest.param({"utc_hhmmss": 125960}, id="utc-second"),
        pytest.param({"sensitivity": 0}, id="sensitivity-low"),
        pytest.param({"sensitivity": 4}, id="sensitivity-high"),
        pytest.param({"low_frequency": 99}, id="frequency-low"),
        pytest.param({"high_frequency": 4911}, id="frequency-high"),
        pytest.param(
            {"low_frequency": 200, "high_frequency": 200},
            id="frequency-not-increasing",
        ),
        pytest.param(
            {"low_frequency": 200, "high_frequency": 299},
            id="frequency-window-narrow",
        ),
    ],
)
def test_standard_decode_rejects_invalid_config_without_entering_vendor(
    raw_library: c.CDLL,
    changes: dict[str, int],
) -> None:
    decode = decode_function(raw_library)
    empty_slot = np.zeros(180_000, dtype=np.int16)
    results = (Result * 256)()
    c.memset(c.byref(results), 0x3C, c.sizeof(results))
    before = bytes(results)
    count = c.c_int32(91)
    overflow = c.c_int32(92)
    cfg = config()
    for field, value in changes.items():
        setattr(cfg, field, value)

    status = decode(
        empty_slot.ctypes.data_as(c.POINTER(c.c_int16)),
        c.byref(cfg),
        1,
        results,
        256,
        c.byref(count),
        c.byref(overflow),
    )

    assert status == 5
    assert count.value == 0
    assert overflow.value == 0
    assert bytes(results) == before


def _decode_once(
    decode: c._CFuncPtr,
    samples: np.ndarray,
    cfg: Config,
    slot_id: int,
) -> tuple[int, int, int, list[Result]]:
    results = (Result * 256)()
    count = c.c_int32(-1)
    overflow = c.c_int32(-1)
    status = decode(
        samples.ctypes.data_as(c.POINTER(c.c_int16)),
        c.byref(cfg),
        slot_id,
        results,
        256,
        c.byref(count),
        c.byref(overflow),
    )
    return status, count.value, overflow.value, list(results[: count.value])


def test_standard_decode_repeated_calls_do_not_leak_batch_or_vendor_state(
    raw_library: c.CDLL,
    cq_slot: np.ndarray,
) -> None:
    decode = decode_function(raw_library)
    cfg = config()

    first = _decode_once(decode, cq_slot, cfg, 11_908_800)
    second = _decode_once(decode, cq_slot, cfg, 11_908_801)
    cfg.utc_hhmmss = 120015
    empty = _decode_once(decode, np.zeros(180_000, dtype=np.int16), cfg, 11_908_802)
    fourth = _decode_once(decode, cq_slot, cfg, 11_908_803)

    for status, count, overflow, results in (first, second, fourth):
        assert status == 0
        assert count > 0
        assert overflow == 0
        messages = [result.text.decode().strip() for result in results]
        assert "CQ K1ABC FN42" in messages
    assert all(result.slot_id == 11_908_800 for result in first[3])
    assert all(result.slot_id == 11_908_801 for result in second[3])
    assert empty[:3] == (0, 0, 0)
    assert empty[3] == []
    assert all(result.slot_id == 11_908_803 for result in fourth[3])
