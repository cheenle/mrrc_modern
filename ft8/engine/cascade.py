"""Adaptive cascade policy: when a slot's fast decode needs the deep path.

Pure Python and clock-free so the policy is testable without the native
library.  See docs/superpowers/specs/2026-10-13-ft8-fast-path-cascade-design.md.
"""

from __future__ import annotations

from dataclasses import dataclass

from ft8.core.models import DecodeBatch

# 与 ft8/dsp/wsjt_core.h 的 WSJT_FLAG_SNR_PROVISIONAL 同值（bit 4）。
FLAG_SNR_PROVISIONAL = 16

DEFAULT_MIN_DECODES = 1
# 第三轮 A/B（docs/ft8-w103d-decode-perf-analysis.md §8）：fast 在 0…-24 dB 与
# 深路径同为 5/5，到 -30 dB 才塌成 0/5。所以峰 SNR 判据要卡在这道崖的上方一点点、
# 而不是 -20 —— 落在 -24 只会对着一个「实测已知解码完整」的时隙每次白烧 2.2 s。
DEFAULT_MIN_PEAK_SNR = -26


@dataclass(frozen=True, slots=True)
class CascadePolicy:
    """Which conditions trigger a same-slot deep re-decode."""

    escalate_on_ap_context: bool = True
    escalate_on_weak: bool = True
    escalate_on_request: bool = False
    min_decodes: int = DEFAULT_MIN_DECODES
    min_peak_snr: int = DEFAULT_MIN_PEAK_SNR


def should_escalate(
    policy: CascadePolicy,
    batch: DecodeBatch,
    *,
    my_call: str,
    qso_progress: int,
    requested: bool,
) -> bool:
    """Return True when the slot should also run the deep (WSJT-X) path."""

    if policy.escalate_on_request and requested:
        return True
    if policy.escalate_on_ap_context and my_call and qso_progress > 0:
        return True
    if policy.escalate_on_weak:
        if len(batch.results) < policy.min_decodes:
            return True
        if _snr_is_trustworthy(batch):
            if max(r.snr for r in batch.results) < policy.min_peak_snr:
                return True
    return False


def _snr_is_trustworthy(batch: DecodeBatch) -> bool:
    """Whether this batch's ``snr`` values are dB (so comparable to S_MIN).

    The fast path has no calibrated SNR — it reports an upstream
    score-derived estimate and marks every result with
    ``FLAG_SNR_PROVISIONAL``.  Comparing that number against a dB threshold
    would produce escalation decisions driven by noise, so the peak-SNR
    criterion is skipped for such a batch and ``min_decodes`` carries the
    weak-signal detection instead (spec D-7).  A future fast path that
    computes a real SNR simply stops setting the flag and re-enables this.
    """

    if not batch.results:
        return False
    return all(not (r.flags & FLAG_SNR_PROVISIONAL) for r in batch.results)
