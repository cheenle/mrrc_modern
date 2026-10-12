from __future__ import annotations

import ctypes as c
import math
import re
from pathlib import Path

import numpy as np
import pytest

from test_ft8_standard_decode import DECODE_ARGTYPES, Result, config


ROOT = Path(__file__).parents[3]


def _request_state_assignments() -> dict[str, str]:
    """Return normalized assignments from the native request initializer."""
    source = (ROOT / "ft8" / "dsp" / "wsjt_improved.f90").read_text(encoding="utf-8")
    match = re.search(
        r"(?ims)^\s*subroutine\s+initialize_request_state\b"
        r"(.*?)^\s*end\s+subroutine\s+initialize_request_state\s*$",
        source,
    )
    assert match is not None, "missing initialize_request_state subroutine"

    logical_lines: list[str] = []
    pending = ""
    for physical_line in match.group(1).splitlines():
        code = physical_line.split("!", 1)[0].strip()
        if not code:
            continue
        if code.startswith("&"):
            code = code[1:].lstrip()
        continued = code.endswith("&")
        if continued:
            code = code[:-1].rstrip()
        pending = f"{pending} {code}".strip()
        if not continued:
            logical_lines.append(pending.lower())
            pending = ""
    assert not pending, "unterminated Fortran continuation"

    assignments: dict[str, str] = {}
    for line in logical_lines:
        assignment = re.fullmatch(
            r"([a-z]\w*(?:\([^=]*\))?(?:%\w+)?)\s*=\s*(.+)", line
        )
        if assignment is not None:
            target = re.sub(r"\([^)]*\)(?=%)", "", assignment.group(1))
            assignments[target] = re.sub(r"\s+", "", assignment.group(2))
    return assignments


def improved_function(raw_library: c.CDLL) -> c._CFuncPtr:
    assert hasattr(raw_library, "wsjt_ft8_decode_improved"), (
        "freshly built library is missing wsjt_ft8_decode_improved"
    )
    function = raw_library.wsjt_ft8_decode_improved
    function.argtypes = DECODE_ARGTYPES
    function.restype = c.c_int32
    assert function.argtypes == DECODE_ARGTYPES
    assert function.restype is c.c_int32
    return function


def _rounded_key(result: Result) -> tuple[str, int, int]:
    text = result.text.decode().strip()
    frequency_hz = math.floor(float(result.frequency) + 0.5)
    dt_tenths = math.floor(float(result.dt) * 10.0 + 0.5)
    return text, frequency_hz, dt_tenths


@pytest.mark.parametrize("profile", range(5))
def test_improved_profiles_decode_known_message_and_return_exact_dedup_batch(
    raw_library: c.CDLL,
    cq_slot: np.ndarray,
    profile: int,
) -> None:
    decode = improved_function(raw_library)
    cfg = config()
    cfg.profile = profile
    cfg.threads = 1
    cfg.cycles = 1
    results = (Result * 256)()
    count = c.c_int32(-1)
    overflow = c.c_int32(-1)

    status = decode(
        cq_slot.ctypes.data_as(c.POINTER(c.c_int16)),
        c.byref(cfg),
        11_908_800,
        results,
        256,
        c.byref(count),
        c.byref(overflow),
    )

    assert status == 0
    assert overflow.value == 0
    assert 0 < count.value <= 256
    decoded = list(results[: count.value])
    messages = [result.text.decode().strip() for result in decoded]
    assert "CQ K1ABC FN42" in messages
    assert all(result.slot_id == 11_908_800 for result in decoded)
    keys = [_rounded_key(result) for result in decoded]
    assert len(keys) == len(set(keys))


def test_improved_twelve_thread_minimum_band_is_safe_and_complete(
    raw_library: c.CDLL,
    cq_slot: np.ndarray,
) -> None:
    decode = improved_function(raw_library)
    cfg = config()
    cfg.profile = 3
    cfg.threads = 12
    cfg.cycles = 1
    cfg.low_frequency = 1450
    cfg.high_frequency = 1550
    results = (Result * 256)()
    count = c.c_int32(-1)
    overflow = c.c_int32(-1)

    status = decode(
        cq_slot.ctypes.data_as(c.POINTER(c.c_int16)),
        c.byref(cfg),
        11_908_800,
        results,
        256,
        c.byref(count),
        c.byref(overflow),
    )

    messages = [results[index].text.decode().strip() for index in range(count.value)]
    assert status == 0
    assert overflow.value == 0
    assert "CQ K1ABC FN42" in messages


def test_improved_request_state_resets_all_detection_history() -> None:
    assignments = _request_state_assignments()
    history_groups = ("evencq", "oddcq", "evenmyc", "oddmyc", "evenqso", "oddqso")

    for group in history_groups:
        assert assignments.get(f"{group}%freq") == "6000.0"
        assert assignments.get(f"{group}%xdt") == "0.0"
        assert assignments.get(f"{group}%cs") == "(0.0,0.0)"


def test_improved_request_state_enables_a8_only_for_complete_ap_context() -> None:
    assignments = _request_state_assignments()
    assert assignments.get("ap_enabled") == (
        "iand(config%flags,wsjt_flag_ap)/=0_c_int32_t"
    )
    expression = assignments.get("ltry_a8", "")
    terms = {term.strip("()") for term in expression.split(".and.")}

    assert terms == {
        "ap_enabled",
        "len_trim(hiscall)>=3",
        "len_trim(hisgrid4)>=4",
    }


def test_improved_same_process_cq_empty_cq_has_no_request_history_leak(
    raw_library: c.CDLL,
    cq_slot: np.ndarray,
) -> None:
    decode = improved_function(raw_library)
    cfg = config()
    cfg.profile = 3
    cfg.threads = 1
    cfg.cycles = 1

    def run(samples: np.ndarray, slot_id: int) -> list[tuple[str, int, int]]:
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
        assert status == 0
        assert overflow.value == 0
        assert all(results[index].slot_id == slot_id for index in range(count.value))
        return [_rounded_key(results[index]) for index in range(count.value)]

    first = run(cq_slot, 11_908_800)
    empty = run(np.zeros_like(cq_slot), 11_908_801)
    third = run(cq_slot, 11_908_802)

    assert any(
        text == "CQ K1ABC FN42" and frequency == 1500
        for text, frequency, _ in first
    )
    assert empty == []
    assert third == first


def test_openmp_stack_contract_is_set_before_runtime_load_and_documented() -> None:
    fixture = (ROOT / "tests" / "ft8" / "dsp" / "conftest.py").read_text(encoding="utf-8")
    setting = 'os.environ.setdefault("OMP_STACKSIZE", "10M")'
    assert fixture.index(setting) < fixture.index("import numpy")
    assert fixture.index(setting) < fixture.index("from scipy")

    required_records = [
        ROOT / "AGENTS.md",
        ROOT / "SDD" / "11-component-model.md",
        ROOT / "docs" / "superpowers" / "specs" / "2026-10-12-ft8-work-mode-design.md",
    ]
    for record in required_records:
        text = record.read_text(encoding="utf-8")
        assert "OMP_STACKSIZE" in text, f"missing OpenMP stack contract in {record}"
        assert "10M" in text, f"missing vendor stack size in {record}"


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        pytest.param({"profile": 5}, 5, id="profile-high"),
        pytest.param({"threads": 0}, 5, id="threads-low"),
        pytest.param({"threads": 13}, 5, id="threads-high"),
        pytest.param({"cycles": 0}, 5, id="cycles-low"),
        pytest.param({"cycles": 4}, 5, id="cycles-high"),
        pytest.param({"qso_progress": -1}, 5, id="qso-progress-negative"),
        pytest.param({"qso_progress": 6}, 5, id="qso-progress-too-high"),
        pytest.param({"utc_hhmmss": -5}, 5, id="utc-negative"),
        pytest.param({"utc_hhmmss": 236000}, 5, id="utc-hour"),
        pytest.param({"utc_hhmmss": 126000}, 5, id="utc-minute"),
        pytest.param({"utc_hhmmss": 125960}, 5, id="utc-second"),
        pytest.param({"sensitivity": 0}, 5, id="sensitivity-low"),
        pytest.param({"sensitivity": 4}, 5, id="sensitivity-high"),
        pytest.param({"low_frequency": 99}, 5, id="frequency-low"),
        pytest.param({"high_frequency": 4911}, 5, id="frequency-high"),
        pytest.param(
            {"low_frequency": 200, "high_frequency": 200},
            5,
            id="frequency-not-increasing",
        ),
        pytest.param(
            {"low_frequency": 200, "high_frequency": 299},
            5,
            id="frequency-window-narrow",
        ),
        pytest.param({"sample_rate": 48_000}, 3, id="sample-rate"),
        pytest.param({"sample_count": 179_999}, 4, id="sample-count"),
        pytest.param({"struct_size": 99}, 2, id="struct-size-low"),
        pytest.param({"struct_size": 101}, 2, id="struct-size-high"),
        pytest.param({"capacity": 255}, 6, id="capacity"),
    ],
)
def test_improved_rejects_invalid_input_without_copying_results(
    raw_library: c.CDLL,
    changes: dict[str, int],
    expected: int,
) -> None:
    decode = improved_function(raw_library)
    samples = np.zeros(180_000, dtype=np.int16)
    results = (Result * 256)()
    c.memset(c.byref(results), 0xA5, c.sizeof(results))
    before = bytes(results)
    count = c.c_int32(91)
    overflow = c.c_int32(92)
    cfg = config()
    capacity = 256
    for field, value in changes.items():
        if field == "capacity":
            capacity = value
        else:
            setattr(cfg, field, value)

    status = decode(
        samples.ctypes.data_as(c.POINTER(c.c_int16)),
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
def test_improved_rejects_every_null_pointer_without_partial_output(
    raw_library: c.CDLL,
    cq_slot: np.ndarray,
    missing: str,
) -> None:
    decode = improved_function(raw_library)
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
