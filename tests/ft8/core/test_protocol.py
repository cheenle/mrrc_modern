from __future__ import annotations

import copy
import json
from dataclasses import FrozenInstanceError

import pytest

from ft8.core.protocol import (
    MAX_CONTROL_FRAME,
    PROTOCOL_VERSION,
    FrameError,
    SharedMemorySpec,
    decode_frame,
    encode_frame,
)


BASE = {"v": 1, "type": "ping", "generation": 7, "request_id": 9}


def decode_config() -> dict[str, object]:
    return {
        "path": "improved",
        "sample_rate": 12_000,
        "sample_count": 180_000,
        "profile": 3,
        "threads": 1,
        "cycles": 1,
        "sensitivity": 2,
        "ap": True,
        "low_threshold": False,
        "wide_dx": False,
        "hide_duplicates": True,
        "qso_progress": 0,
        "rx_frequency": 1500,
        "tx_frequency": 1500,
        "low_frequency": 200,
        "high_frequency": 3000,
        "ap_width": 50,
        "utc_hhmmss": 120000,
        "my_call": "N0CALL",
        "dx_call": "",
        "dx_grid": "",
    }


def decode_shm() -> dict[str, object]:
    return {
        "name": "psm_decode_test",
        "dtype": "<i2",
        "shape": [180_000],
        "nbytes": 360_000,
    }


def encode_shm() -> dict[str, object]:
    return {
        "name": "psm_encode_test",
        "dtype": "<f4",
        "shape": [606_720],
        "nbytes": 2_426_880,
    }


def decode_result() -> dict[str, object]:
    return {
        "slot_id": 11_908_800,
        "sync": 2.5,
        "snr": -7,
        "dt": 0.125,
        "frequency": 1500.25,
        "text": "CQ K1ABC FN42",
        "ap_type": 0,
        "quality": 0.75,
        "flags": 0,
    }


def valid_frames() -> list[dict[str, object]]:
    base = {"v": 1, "generation": 7, "request_id": 9}
    return [
        {**base, "type": "ping"},
        {**base, "type": "pong"},
        {
            **base,
            "type": "decode",
            "slot_id": 11_908_800,
            "deadline_monotonic": 999_999_999.0,
            "shm": decode_shm(),
            "config": decode_config(),
        },
        {
            **base,
            "type": "decode_ok",
            "slot_id": 11_908_800,
            "path": "improved",
            "results": [decode_result()],
            "overflow": False,
            "elapsed_seconds": 0.25,
            "deadline_missed": False,
        },
        {
            **base,
            "type": "encode",
            "message": "CQ K1ABC FN42",
            "frequency": 1500.0,
            "sample_rate": 48_000,
            "shm": encode_shm(),
        },
        {
            **base,
            "type": "encode_ok",
            "message": "CQ K1ABC FN42",
            "sample_rate": 48_000,
            "sample_count": 606_720,
        },
        {**base, "type": "error", "code": "invalid_request", "detail": "rejected"},
        {**base, "type": "shutdown"},
        {**base, "type": "stopped"},
    ]


def assert_rejected(frame: dict[str, object], match: str | None = None) -> None:
    with pytest.raises(FrameError, match=match):
        encode_frame(frame)
    raw = json.dumps(frame, allow_nan=True).encode("utf-8")
    with pytest.raises(FrameError, match=match):
        decode_frame(raw)


def test_constants_and_shared_memory_spec_are_stable_values() -> None:
    spec = SharedMemorySpec("psm_test", "<i2", (180_000,), 360_000)

    assert PROTOCOL_VERSION == 1
    assert MAX_CONTROL_FRAME == 65_536
    assert not hasattr(spec, "__dict__")
    with pytest.raises(FrozenInstanceError):
        spec.name = "changed"  # type: ignore[misc]


@pytest.mark.parametrize("frame", valid_frames(), ids=lambda frame: str(frame["type"]))
def test_every_frame_type_round_trips_through_exact_schema(
    frame: dict[str, object],
) -> None:
    assert decode_frame(encode_frame(frame)) == frame


def test_encoding_is_deterministic_sorted_compact_utf8_json() -> None:
    assert encode_frame(BASE) == (
        b'{"generation":7,"request_id":9,"type":"ping","v":1}'
    )
    assert b" " not in encode_frame(BASE)


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(b"", id="empty"),
        pytest.param(b"{", id="bad-json"),
        pytest.param(b"\xff", id="bad-utf8"),
        pytest.param(b"[]", id="array"),
        pytest.param(b"null", id="null"),
        pytest.param(
            b'{"generation":7,"generation":8,"request_id":9,"type":"ping","v":1}',
            id="duplicate-key",
        ),
    ],
)
def test_decode_rejects_invalid_json_or_non_object(raw: bytes) -> None:
    with pytest.raises(FrameError):
        decode_frame(raw)


def test_decode_checks_raw_size_before_utf8_or_json_parsing() -> None:
    with pytest.raises(FrameError, match="65536"):
        decode_frame(b"\xff" * (MAX_CONTROL_FRAME + 1))
    with pytest.raises(FrameError, match="JSON"):
        decode_frame(b" " * MAX_CONTROL_FRAME)


@pytest.mark.parametrize("value", [2, 0, True, 1.0, "1", None])
def test_version_must_be_exact_non_boolean_integer_one(value: object) -> None:
    frame = {**BASE, "v": value}
    assert_rejected(frame, "version")


@pytest.mark.parametrize("field", ["v", "type", "generation", "request_id"])
def test_base_fields_are_required(field: str) -> None:
    frame = dict(BASE)
    del frame[field]
    assert_rejected(frame, "missing")


@pytest.mark.parametrize("field", ["generation", "request_id"])
@pytest.mark.parametrize("value", [True, 1.0, "1", -1, 2**63])
def test_correlation_ids_are_nonnegative_int64_not_booleans(
    field: str, value: object
) -> None:
    frame = {**BASE, field: value}
    assert_rejected(frame, field)


def test_unknown_type_and_extra_top_level_fields_are_rejected() -> None:
    assert_rejected({**BASE, "type": "evil"}, "type")
    assert_rejected({**BASE, "extra": 1}, "extra")


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_numbers_are_rejected_on_encode_and_decode(constant: str) -> None:
    frame = valid_frames()[2]
    frame["deadline_monotonic"] = float(constant)
    with pytest.raises(FrameError, match="finite|JSON"):
        encode_frame(frame)

    raw = (
        '{"v":1,"type":"decode_ok","generation":1,"request_id":1,'
        '"slot_id":1,"path":"standard","results":[],"overflow":false,'
        f'"elapsed_seconds":{constant},"deadline_missed":false}}'
    ).encode()
    with pytest.raises(FrameError, match="constant|finite|JSON"):
        decode_frame(raw)


@pytest.mark.parametrize("kind", ["decode", "encode"])
@pytest.mark.parametrize(
    ("mutation", "value"),
    [
        ("missing", None),
        ("extra", 1),
        ("name", ""),
        ("name", "bad\nname"),
        ("name", "x" * 256),
        ("dtype", "int16"),
        ("shape", [1]),
        ("nbytes", True),
    ],
)
def test_shared_memory_descriptor_is_exact_and_fixed(
    kind: str, mutation: str, value: object
) -> None:
    frame = valid_frames()[2 if kind == "decode" else 4]
    descriptor = frame["shm"]
    assert isinstance(descriptor, dict)
    if mutation == "missing":
        del descriptor["name"]
    elif mutation == "extra":
        descriptor["extra"] = value
    else:
        descriptor[mutation] = value
    assert_rejected(frame, "shm")


def test_decode_and_encode_descriptors_cannot_be_swapped() -> None:
    decode = valid_frames()[2]
    decode["shm"] = encode_shm()
    encode = valid_frames()[4]
    encode["shm"] = decode_shm()

    assert_rejected(decode, "shm")
    assert_rejected(encode, "shm")


@pytest.mark.parametrize("mutation", ["missing", "extra", "path", "bool", "int", "text"])
def test_decode_config_has_every_exact_field_and_strict_scalar_types(
    mutation: str,
) -> None:
    frame = valid_frames()[2]
    config = frame["config"]
    assert isinstance(config, dict)
    if mutation == "missing":
        del config["profile"]
    elif mutation == "extra":
        config["extra"] = 1
    elif mutation == "path":
        config["path"] = "fast"
    elif mutation == "bool":
        config["ap"] = 1
    elif mutation == "int":
        config["threads"] = True
    else:
        config["my_call"] = 123
    assert_rejected(frame, "config")


def test_protocol_preserves_structurally_valid_config_for_binding_range_checks() -> None:
    frame = valid_frames()[2]
    config = frame["config"]
    assert isinstance(config, dict)
    config["profile"] = 5
    config["low_frequency"] = 3000
    config["high_frequency"] = 200

    assert decode_frame(encode_frame(frame)) == frame


@pytest.mark.parametrize(
    ("mutation", "value"),
    [
        ("missing", None),
        ("extra", 1),
        ("slot_id", True),
        ("sync", float("nan")),
        ("snr", 1.5),
        ("dt", float("inf")),
        ("frequency", "1500"),
        ("text", 123),
        ("ap_type", False),
        ("quality", float("-inf")),
        ("flags", 0.0),
    ],
)
def test_decode_results_have_exact_nine_field_schema(
    mutation: str, value: object
) -> None:
    frame = valid_frames()[3]
    result = frame["results"][0]  # type: ignore[index]
    assert isinstance(result, dict)
    if mutation == "missing":
        del result["sync"]
    elif mutation == "extra":
        result["extra"] = value
    else:
        result[mutation] = value
    assert_rejected(frame, "result|finite")


def test_decode_results_are_bounded_to_native_capacity() -> None:
    accepted = valid_frames()[3]
    accepted["results"] = [decode_result() for _ in range(256)]
    raw = encode_frame(accepted)
    assert len(raw) <= MAX_CONTROL_FRAME
    assert len(decode_frame(raw)["results"]) == 256

    rejected = copy.deepcopy(accepted)
    rejected["results"].append(decode_result())  # type: ignore[union-attr]
    assert_rejected(rejected, "256")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("code", "Bad Code"),
        ("code", "x" * 65),
        ("detail", "traceback\nsecret"),
        ("detail", "x" * 513),
        ("detail", 123),
    ],
)
def test_error_frame_requires_stable_printable_bounded_sanitation(
    field: str, value: object
) -> None:
    frame = valid_frames()[6]
    frame[field] = value
    assert_rejected(frame, field)


@pytest.mark.parametrize(
    ("index", "field", "value"),
    [
        (2, "deadline_monotonic", True),
        (2, "deadline_monotonic", -0.1),
        (3, "overflow", 0),
        (3, "deadline_missed", 0),
        (3, "elapsed_seconds", -0.1),
        (4, "message", 123),
        (4, "frequency", False),
        (4, "sample_rate", True),
        (5, "sample_count", 606_719),
    ],
)
def test_operation_specific_scalars_are_strict(
    index: int, field: str, value: object
) -> None:
    frame = valid_frames()[index]
    frame[field] = value
    assert_rejected(frame, field)

