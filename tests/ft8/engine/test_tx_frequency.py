"""CQ 空闲频点选择与占用环（UC-004 扩展；TX 频率跟随的 CQ 半边）。"""

from __future__ import annotations

import pytest

from ft8.engine.tx_frequency import FrequencyOccupancy, pick_cq_frequency


# ---- pick_cq_frequency 纯函数 -------------------------------------------

def test_pick_returns_default_when_nothing_occupied() -> None:
    assert pick_cq_frequency([]) == 1500.0


def test_pick_skips_occupied_default() -> None:
    # guard 10: 1500±9 内冲突；螺旋 +10 = 1510 首个干净频点。
    assert pick_cq_frequency([1500.0], guard_hz=10.0) == 1510.0


def test_pick_skips_cluster_on_both_sides() -> None:
    occupied = [1490.0, 1500.0, 1505.0, 1515.0]
    # guard 5: 1486-1494、1496-1504、1501-1509、1511-1519 全冲突（含占用点
    # 自身）；螺旋从 1500 向外，1495 是首个干净点（距 1490 恰为 5）。
    assert pick_cq_frequency(occupied, guard_hz=5.0) == 1495.0


def test_pick_ignores_frequencies_outside_the_window() -> None:
    # 1200 在窗口外（1500±300 内无占用）→ 直接回 1500。
    assert pick_cq_frequency([1200.0], search_window_hz=300.0) == 1500.0


def test_pick_falls_back_to_default_when_fully_occupied() -> None:
    occupied = [1500.0 + i for i in range(-301, 302, 1)]
    assert pick_cq_frequency(occupied, guard_hz=1.0) == 1500.0


def test_pick_rejects_negative_guard_or_window() -> None:
    with pytest.raises(ValueError):
        pick_cq_frequency([], guard_hz=-1.0)
    with pytest.raises(ValueError):
        pick_cq_frequency([], search_window_hz=-5.0)


def test_pick_default_is_configurable() -> None:
    assert pick_cq_frequency([], default=2000.0) == 2000.0


# ---- FrequencyOccupancy TTL 环 ------------------------------------------

def test_occupancy_notes_and_returns_live_offsets() -> None:
    occ = FrequencyOccupancy(ttl_s=120.0)
    occ.note(843.0, now=10.0)
    occ.note(1500.0, now=11.0)
    assert occ.occupied_freqs(now=12.0) == [843.0, 1500.0]


def test_occupancy_ignores_non_positive_offsets() -> None:
    occ = FrequencyOccupancy()
    occ.note(0.0)
    occ.note(-5.0)
    assert occ.occupied_freqs(now=0.0) == []


def test_occupancy_prunes_stale_entries() -> None:
    occ = FrequencyOccupancy(ttl_s=120.0)
    occ.note(843.0, now=10.0)
    occ.note(1500.0, now=11.0)
    # 843 的 age=111<120 保留；1500 的 age=110<120 也保留。
    assert len(occ.occupied_freqs(now=121.0)) == 2
    # 843 age=121 过期；1500 age=120 恰好到 TTL（> 才算过期）仍保留。
    assert occ.occupied_freqs(now=131.0) == [1500.0]
    # 再 +1 s：1500 也过期。
    assert occ.occupied_freqs(now=132.0) == []


def test_occupancy_pick_uses_live_set() -> None:
    occ = FrequencyOccupancy(ttl_s=120.0)
    occ.note(1500.0, now=10.0)
    # 占用 1500 → 避开，回 1510（guard 10）。
    assert occ.pick_frequency(guard_hz=10.0, now=11.0) == 1510.0
    # 过期后 1500 不再占用 → 回默认。
    assert occ.pick_frequency(guard_hz=10.0, now=200.0) == 1500.0
