from __future__ import annotations

import ctypes
from pathlib import Path

from conftest import _Config, _Result
from ft8.core.models import DecodeBatch, DecodePath, DecodeResult
from ft8.engine.cascade import merge_batches
from test_ft8_standard_decode import DECODE_ARGTYPES, Result, config

ROOT = Path(__file__).parents[3]

# 实测值（2026-10-13，macOS）：noisy busy_slot（σ=2000）快路径解出 5/5，20 次
# 连跑恒为 5，故 floor = 5。见 task-4-report.md 的「先量后钉」一节。
FAST_FLOOR = 5


def test_fast_path_decodes_the_busy_slot(call_fast, raw_library, busy_slot) -> None:
    status, results, count, overflow = call_fast(raw_library, busy_slot)
    assert status == 0
    assert count >= FAST_FLOOR, f"expected the 5 CQ signals, got {count}"
    texts = {results[i].text.decode().rstrip("\x00").strip() for i in range(count)}
    assert any("CQ" in t for t in texts)


def test_fast_path_reports_no_overflow_at_full_capacity(call_fast, raw_library, busy_slot) -> None:
    """`*overflow` 在快路径上恒为 0：候选上限 140 < 结果容量 256 <= capacity。"""
    status, _results, _count, overflow = call_fast(raw_library, busy_slot)
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


def test_fast_path_rejects_capacity_below_result_capacity(call_fast, raw_library, busy_slot) -> None:
    """注意：这是**前置条件**检查（capacity < WSJT_RESULT_CAPACITY），
    不是 overflow 路径 —— 见 `test_fast_path_reports_no_overflow_at_full_capacity`。"""
    status, _results, _count, _overflow = call_fast(raw_library, busy_slot, capacity=1)
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


def test_fast_path_rejects_non_12000_sample_rate(raw_library, busy_slot) -> None:
    """`sample_rate != 12000` 必须在进入 vendor 前返回 WSJT_E_RATE（ABI 契约分支）。"""
    cfg = _Config()
    cfg.struct_size = ctypes.sizeof(_Config)
    cfg.sample_rate = 48_000
    cfg.sample_count = 180_000
    cfg.low_frequency = 200
    cfg.high_frequency = 3_000
    results = (_Result * 256)()
    count = ctypes.c_int32()
    overflow = ctypes.c_int32()
    status = raw_library.wsjt_ft8_decode_fast(
        busy_slot.ctypes.data_as(ctypes.POINTER(ctypes.c_int16)),
        ctypes.byref(cfg), 1, results, 256,
        ctypes.byref(count), ctypes.byref(overflow),
    )
    assert status == 3, f"expected WSJT_E_RATE, got {status}"


def test_fast_path_rejects_non_180000_sample_count(raw_library, busy_slot) -> None:
    """`sample_count != 180000` 必须返回 WSJT_E_SHAPE（ABI 契约分支）。"""
    cfg = _Config()
    cfg.struct_size = ctypes.sizeof(_Config)
    cfg.sample_rate = 12_000
    cfg.sample_count = 179_999
    cfg.low_frequency = 200
    cfg.high_frequency = 3_000
    results = (_Result * 256)()
    count = ctypes.c_int32()
    overflow = ctypes.c_int32()
    status = raw_library.wsjt_ft8_decode_fast(
        busy_slot.ctypes.data_as(ctypes.POINTER(ctypes.c_int16)),
        ctypes.byref(cfg), 1, results, 256,
        ctypes.byref(count), ctypes.byref(overflow),
    )
    assert status == 4, f"expected WSJT_E_SHAPE, got {status}"


def _improved_function(library: ctypes.CDLL) -> ctypes._CFuncPtr:
    function = library.wsjt_ft8_decode_improved
    function.argtypes = DECODE_ARGTYPES
    function.restype = ctypes.c_int32
    return function


def _call_improved(library, samples):
    """Invoke ``wsjt_ft8_decode_improved`` with the standard 12 kHz config."""
    decode = _improved_function(library)
    cfg = config()
    cfg.threads = 1
    cfg.cycles = 1
    results = (Result * 256)()
    count = ctypes.c_int32(-1)
    overflow = ctypes.c_int32(-1)
    status = decode(
        samples.ctypes.data_as(ctypes.POINTER(ctypes.c_int16)),
        ctypes.byref(cfg),
        1,
        results,
        256,
        ctypes.byref(count),
        ctypes.byref(overflow),
    )
    return status, results, count.value, overflow.value


def _dt_for(results, count: int, text: str):
    """Return the ``dt`` of the result whose decoded text equals ``text``."""
    for i in range(count):
        if results[i].text.decode().strip() == text:
            return float(results[i].dt)
    return None


def _to_model(result) -> DecodeResult:
    return DecodeResult(
        slot_id=int(result.slot_id),
        sync=float(result.sync),
        snr=int(result.snr),
        dt=float(result.dt),
        frequency=float(result.frequency),
        text=result.text.decode().strip(),
        ap_type=int(result.ap_type),
        quality=float(result.quality),
        flags=int(result.flags),
    )


def test_fast_path_reports_dt_in_the_wsjt_x_frame(call_fast, raw_library, cq_slot) -> None:
    """快路径的 dt 必须落在 WSJT-X 帧：cq_slot 的信号在名义位置（0.5 s 处），
    故 dt 应为 0.0。未修的库把时隙起点 + 水声图行滞后一起报出来，dt ≈ 0.64。"""
    status, results, count, _overflow = call_fast(raw_library, cq_slot)
    assert status == 0
    assert count >= 1
    dt = _dt_for(results, count, "CQ K1ABC FN42")
    assert dt is not None, "fast path did not decode CQ K1ABC FN42"
    assert abs(dt) <= 0.05, f"expected dt ≈ 0.0 in the WSJT-X frame, got {dt}"


def test_fast_and_deep_dt_agree_on_the_same_slot(call_fast, raw_library, cq_slot) -> None:
    """同一条 cq_slot，快路径与深路径的 dt 必须一致 —— 这正是会抓住本缺陷的断言。"""
    fast_status, fast_results, fast_count, _ = call_fast(raw_library, cq_slot)
    assert fast_status == 0
    assert fast_count >= 1
    deep_status, deep_results, deep_count, _ = _call_improved(raw_library, cq_slot)
    assert deep_status == 0
    assert deep_count >= 1

    fast_dt = _dt_for(fast_results, fast_count, "CQ K1ABC FN42")
    deep_dt = _dt_for(deep_results, deep_count, "CQ K1ABC FN42")
    assert fast_dt is not None and deep_dt is not None, (
        "both paths must decode CQ K1ABC FN42"
    )
    assert abs(fast_dt - deep_dt) <= 0.05, (
        f"fast dt {fast_dt} and deep dt {deep_dt} disagree on the same slot"
    )


def test_escalated_slot_merges_real_fast_and_deep_results(
    call_fast, raw_library, busy_slot, signals
) -> None:
    """端到端合并必须吃**真实解码输出**：busy_slot 的五条已知信号各只出现一次。

    合成 dt 的 merge 单测（tests/ft8/engine/test_cascade_merge.py）给两边同一个 dt，
    永远测不出 fast/deep 的帧不一致；这里用真库解同一个时隙再合并 —— 未修的库会把
    五条快路径行当"新信号"保留在深路径行旁边，每条信号出两行。
    """
    fast_status, fast_results, fast_count, fast_overflow = call_fast(raw_library, busy_slot)
    assert fast_status == 0
    deep_status, deep_results, deep_count, deep_overflow = _call_improved(raw_library, busy_slot)
    assert deep_status == 0

    fast_batch = DecodeBatch(
        slot_id=1,
        path=DecodePath.FAST,
        results=tuple(_to_model(fast_results[i]) for i in range(fast_count)),
        overflow=bool(fast_overflow),
        elapsed_seconds=0.15,
    )
    deep_batch = DecodeBatch(
        slot_id=1,
        path=DecodePath.IMPROVED,
        results=tuple(_to_model(deep_results[i]) for i in range(deep_count)),
        overflow=bool(deep_overflow),
        elapsed_seconds=2.2,
    )

    merged = merge_batches(fast_batch, deep_batch)

    for message, frequency in signals:
        rows = [r for r in merged.results if abs(r.frequency - frequency) <= 5.0]
        assert len(rows) == 1, (
            f"expected exactly one merged row for {message} @ {frequency} Hz, "
            f"got {len(rows)}"
        )
    assert len(merged.results) <= len(deep_batch.results), (
        f"merge must not exceed deep count: "
        f"merged {len(merged.results)} > deep {len(deep_batch.results)}"
    )
