from __future__ import annotations

import ctypes
from pathlib import Path

import numpy as np

ROOT = Path(__file__).parents[3]

# 实测值（2026-10-13，macOS）：noisy busy_slot（σ=2000）快路径解出 5/5，20 次
# 连跑恒为 5，故 floor = 5。见 task-4-report.md 的「先量后钉」一节。
FAST_FLOOR = 5


class _Config(ctypes.Structure):
    _fields_ = [
        ("struct_size", ctypes.c_int32), ("sample_rate", ctypes.c_int32),
        ("sample_count", ctypes.c_int32), ("profile", ctypes.c_int32),
        ("threads", ctypes.c_int32), ("cycles", ctypes.c_int32),
        ("sensitivity", ctypes.c_int32), ("flags", ctypes.c_int32),
        ("qso_progress", ctypes.c_int32), ("rx_frequency", ctypes.c_int32),
        ("tx_frequency", ctypes.c_int32), ("low_frequency", ctypes.c_int32),
        ("high_frequency", ctypes.c_int32), ("ap_width", ctypes.c_int32),
        ("utc_hhmmss", ctypes.c_int32), ("reserved", ctypes.c_int32),
        ("my_call", ctypes.c_char * 13), ("dx_call", ctypes.c_char * 13),
        ("dx_grid", ctypes.c_char * 7), ("padding", ctypes.c_char * 3),
    ]


class _Result(ctypes.Structure):
    _fields_ = [
        ("slot_id", ctypes.c_int64), ("sync", ctypes.c_float),
        ("dt", ctypes.c_float), ("frequency", ctypes.c_float),
        ("quality", ctypes.c_float), ("snr", ctypes.c_int32),
        ("ap_type", ctypes.c_int32), ("flags", ctypes.c_int32),
        ("reserved", ctypes.c_int32), ("text", ctypes.c_char * 38),
        ("padding", ctypes.c_char * 2),
    ]


def _call_fast(library, samples: np.ndarray, capacity: int = 256):
    cfg = _Config()
    cfg.struct_size = ctypes.sizeof(_Config)
    cfg.sample_rate = 12_000
    cfg.sample_count = 180_000
    cfg.low_frequency = 200
    cfg.high_frequency = 3_000
    results = (_Result * capacity)()
    count = ctypes.c_int32()
    overflow = ctypes.c_int32()
    status = library.wsjt_ft8_decode_fast(
        samples.ctypes.data_as(ctypes.POINTER(ctypes.c_int16)),
        ctypes.byref(cfg), 1, results, capacity,
        ctypes.byref(count), ctypes.byref(overflow),
    )
    return status, results, count.value, overflow.value


def test_fast_path_decodes_the_busy_slot(raw_library, busy_slot) -> None:
    status, results, count, overflow = _call_fast(raw_library, busy_slot)
    assert status == 0
    assert count >= FAST_FLOOR, f"expected the 5 CQ signals, got {count}"
    texts = {results[i].text.decode().rstrip("\x00").strip() for i in range(count)}
    assert any("CQ" in t for t in texts)


def test_fast_path_reports_no_overflow_at_full_capacity(raw_library, busy_slot) -> None:
    """`*overflow` 在快路径上恒为 0：候选上限 140 < 结果容量 256 <= capacity。"""
    status, _results, _count, overflow = _call_fast(raw_library, busy_slot)
    assert status == 0
    assert overflow == 0


def test_fast_path_rejects_inverted_frequency_window(raw_library, busy_slot) -> None:
    cfg = _Config()
    cfg.struct_size = ctypes.sizeof(_Config)
    cfg.sample_rate = 12_000
    cfg.sample_count = 180_000
    cfg.low_frequency = 3_000
    cfg.high_frequency = 200          # inverted
    results = (_Result * 256)()
    count = ctypes.c_int32()
    overflow = ctypes.c_int32()
    status = raw_library.wsjt_ft8_decode_fast(
        busy_slot.ctypes.data_as(ctypes.POINTER(ctypes.c_int16)),
        ctypes.byref(cfg), 1, results, 256,
        ctypes.byref(count), ctypes.byref(overflow),
    )
    assert status == 5, f"expected WSJT_E_CONFIG, got {status}"


def test_fast_path_rejects_capacity_below_result_capacity(raw_library, busy_slot) -> None:
    """注意：这是**前置条件**检查（capacity < WSJT_RESULT_CAPACITY），
    不是 overflow 路径 —— 见 `test_fast_path_reports_no_overflow_at_full_capacity`。"""
    status, _results, _count, _overflow = _call_fast(raw_library, busy_slot, capacity=1)
    assert status == 6, f"expected WSJT_E_CAPACITY for capacity < 256, got {status}"


def test_fast_path_keeps_upstream_default_knobs() -> None:
    """旋钮必须停在上游默认值 —— spike 证明任何下调都是拿弱信号换时间。"""
    source = (ROOT / "ft8" / "dsp" / "wsjt_fast.c").read_text(encoding="utf-8")
    assert "#define FAST_MIN_SCORE 10" in source
    assert "#define FAST_LDPC_ITERATIONS 25" in source
    assert "#define FAST_TIME_OSR 2" in source
    assert "#define FAST_FREQ_OSR 2" in source
    # 候选搜索必须真的传 min_score，而不是 0（score=0 会让候选饱和到上限，
    # 实测把 154 ms 翻倍成 281 ms —— 快路径反而比深路径慢）
    assert "cand, FAST_MIN_SCORE)" in source
    assert "cand, 0)" not in source
