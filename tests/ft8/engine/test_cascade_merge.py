from __future__ import annotations

import pytest

from ft8.core.models import DecodeBatch, DecodePath, DecodeResult
from ft8.engine.cascade import merge_batches


def _r(text: str, freq: float = 1500.0, dt: float = 0.1, snr: int = -5,
       ap_type: int = 0) -> DecodeResult:
    return DecodeResult(slot_id=1, sync=0.0, snr=snr, dt=dt, frequency=freq,
                        text=text, ap_type=ap_type, quality=0.0, flags=0)


def _b(path: DecodePath, *results: DecodeResult) -> DecodeBatch:
    return DecodeBatch(slot_id=1, path=path, results=results, overflow=False,
                       elapsed_seconds=0.2)


def test_union_keeps_both_sides() -> None:
    # **两条结果必须落在不同的信号上**（频率拉开 > _FREQ_TOL）。用 `_r` 的默认值
    # 会让两条都停在 1500.0 / 0.1，`_same_signal` 判定为同一条 → 快路径那条被当作
    # 重复丢掉，并集只剩 "CQ BBB"，这条用例就永远不可能通过 —— 而"修"它的错误方式
    # （把断言改成 {"CQ BBB"}）恰好会把「并集」这个唯一被测的性质删掉。
    fast = _b(DecodePath.FAST, _r("CQ AAA", freq=1000.0))
    deep = _b(DecodePath.IMPROVED, _r("CQ BBB", freq=2000.0))
    merged = merge_batches(fast, deep)
    assert {r.text for r in merged.results} == {"CQ AAA", "CQ BBB"}
    assert merged.path is DecodePath.IMPROVED


def test_deep_wins_on_same_key_including_snr_and_ap_type() -> None:
    fast = _b(DecodePath.FAST, _r("CQ AAA", snr=99, ap_type=0))
    deep = _b(DecodePath.IMPROVED, _r("CQ AAA", snr=-5, ap_type=1))
    merged = merge_batches(fast, deep)
    assert len(merged.results) == 1
    assert merged.results[0].snr == -5
    assert merged.results[0].ap_type == 1


def test_hash_placeholder_is_merged_not_duplicated() -> None:
    # fast 保留 <HASH> 文本，deep 还原了呼号：同一 freq/dt 应视为同一条（Review Focus #1）
    fast = _b(DecodePath.FAST, _r("CQ <...>", freq=1200.0, dt=0.2))
    deep = _b(DecodePath.IMPROVED, _r("CQ BA4EG", freq=1200.0, dt=0.2))
    merged = merge_batches(fast, deep)
    assert len(merged.results) == 1
    assert merged.results[0].text == "CQ BA4EG"


def test_empty_deep_returns_fast_only() -> None:
    fast = _b(DecodePath.FAST, _r("CQ AAA"))
    merged = merge_batches(fast, _b(DecodePath.IMPROVED))
    assert merged.path is DecodePath.FAST
    assert [r.text for r in merged.results] == ["CQ AAA"]


def test_overflow_is_logical_or() -> None:
    fast = DecodeBatch(1, DecodePath.FAST, (_r("A"),), True, 0.1)
    deep = DecodeBatch(1, DecodePath.IMPROVED, (_r("B"),), False, 0.2)
    assert merge_batches(fast, deep).overflow is True


def test_deadline_missed_is_logical_or() -> None:
    """`deadline_missed` 是第 6 个位置字段（`elapsed_seconds` 之后）。

    OR 有三个方向，`and` 只在「两边都为 True」时才同样为 True —— 所以只测一个
    方向的用例在 `and` 下也会通过。三个方向都要钉住。
    """
    fast_missed = DecodeBatch(1, DecodePath.FAST, (_r("A"),), False, 0.1, True)
    deep_ok = DecodeBatch(1, DecodePath.IMPROVED, (_r("B"),), False, 0.2)
    assert merge_batches(fast_missed, deep_ok).deadline_missed is True

    fast_ok = DecodeBatch(1, DecodePath.FAST, (_r("A"),), False, 0.1)
    deep_missed = DecodeBatch(1, DecodePath.IMPROVED, (_r("B"),), False, 0.2, True)
    assert merge_batches(fast_ok, deep_missed).deadline_missed is True

    neither = DecodeBatch(1, DecodePath.FAST, (_r("A"),), False, 0.1)
    also_neither = DecodeBatch(1, DecodePath.IMPROVED, (_r("B"),), False, 0.2)
    assert merge_batches(neither, also_neither).deadline_missed is False


def test_merging_two_different_slots_is_an_error() -> None:
    """并的是**同一个**时隙的两条路径结果 —— 不同 slot 的合并是凭空造 QSO。"""
    fast = DecodeBatch(7, DecodePath.FAST, (_r("CQ AAA"),), False, 0.1)
    deep = DecodeBatch(8, DecodePath.IMPROVED, (_r("CQ BBB"),), False, 0.2)
    with pytest.raises(ValueError, match="slot"):
        merge_batches(fast, deep)


def test_merge_marks_the_escalation_and_keeps_each_passes_elapsed() -> None:
    """合并结果要能回答两件事：这一槽升级过吗？各路径各花了多久？

    单看 ``elapsed_seconds`` 是答不出来的（它是两段之和），而下游 Task 13 的
    ``escalations`` 计数完全依赖 ``escalated``。
    """
    fast = DecodeBatch(1, DecodePath.FAST, (_r("CQ AAA"),), False, 0.16)
    deep = DecodeBatch(1, DecodePath.IMPROVED, (_r("CQ BBB"),), False, 2.2)
    merged = merge_batches(fast, deep)
    assert merged.escalated is True
    assert merged.fast_elapsed_seconds == 0.16
    assert merged.elapsed_seconds == pytest.approx(2.36)  # spec §5.3：两段之和


def test_escalation_is_recorded_even_when_the_deep_pass_found_nothing() -> None:
    """深路径跑了但一条都没多解出来，仍然是**发生过**的升级 —— 正是运维要看的那个信号。

    如果把 ``escalated`` 绑在「deep 有结果」上，那么阈值调得过松的时隙全都隐形，
    而「每槽白烧 2.2s、一条没多」恰恰是最该被发现的那种配置。
    """
    fast = _b(DecodePath.FAST, _r("CQ AAA"))
    merged = merge_batches(fast, _b(DecodePath.IMPROVED))
    assert merged.escalated is True
    assert merged.path is DecodePath.FAST
    assert merged.fast_elapsed_seconds == 0.2
