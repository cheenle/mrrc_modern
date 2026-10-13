# tests/ft8/engine/test_cascade_policy.py
from __future__ import annotations

from pathlib import Path

from ft8.core.models import DecodeBatch, DecodePath, DecodeResult
from ft8.engine.cascade import (
    FLAG_SNR_PROVISIONAL,
    CascadePolicy,
    should_escalate,
)

ROOT = Path(__file__).parents[3]


def _batch(
    *snrs: int,
    path: DecodePath = DecodePath.FAST,
    flags: int = FLAG_SNR_PROVISIONAL,
) -> DecodeBatch:
    """默认造一批**快路径**结果 —— snr 是 score 估计值，故带上 provisional 标志。"""
    results = tuple(
        DecodeResult(slot_id=1, sync=0.0, snr=s, dt=0.0, frequency=1500.0,
                     text="CQ TEST", ap_type=0, quality=0.0, flags=flags)
        for s in snrs
    )
    return DecodeBatch(slot_id=1, path=path, results=results,
                       overflow=False, elapsed_seconds=0.15)


def _deep_batch(*snrs: int) -> DecodeBatch:
    """深路径结果：snr 是真 dB，不带 provisional 标志。"""
    return _batch(*snrs, path=DecodePath.IMPROVED, flags=0)


DEFAULT = CascadePolicy()


def test_no_escalation_when_strong_and_no_qso() -> None:
    assert should_escalate(DEFAULT, _batch(-5, -7), my_call="BA4EG",
                           qso_progress=0, requested=False) is False


def test_escalates_on_active_qso() -> None:
    assert should_escalate(DEFAULT, _batch(-5), my_call="BA4EG",
                           qso_progress=2, requested=False) is True


def test_no_escalation_on_qso_when_ap_context_disabled() -> None:
    policy = CascadePolicy(escalate_on_ap_context=False)
    assert should_escalate(policy, _batch(-5), my_call="BA4EG",
                           qso_progress=2, requested=False) is False


def test_escalates_when_too_few_decodes() -> None:
    assert should_escalate(DEFAULT, _batch(), my_call="", qso_progress=0,
                           requested=False) is True


def test_provisional_snr_never_drives_escalation() -> None:
    """快路径的 snr 不是 dB —— 再"低"也不能拿它当弱信号判据。

    否则升级与否就由 score 的任意缩放决定了（见 spec D-7）。这些用例里
    只有 min_decodes 该起作用：有结果就不升级。
    """
    assert should_escalate(DEFAULT, _batch(-28, -30), my_call="", qso_progress=0,
                           requested=False) is False
    assert should_escalate(DEFAULT, _batch(-5, -30), my_call="", qso_progress=0,
                           requested=False) is False


def test_escalates_on_real_weak_snr_from_a_deep_batch() -> None:
    """snr 可信（无 provisional 标志）时，峰值判据必须真的起作用。"""
    assert should_escalate(DEFAULT, _deep_batch(-28, -30), my_call="",
                           qso_progress=0, requested=False) is True
    # 判据看的是**峰值**：一条强的就够压住它。
    assert should_escalate(DEFAULT, _deep_batch(-5, -30), my_call="",
                           qso_progress=0, requested=False) is False


def test_does_not_escalate_at_the_measured_complete_point() -> None:
    """-24 dB 是第三轮 A/B 里 fast 与深路径同为 5/5 的点：此时不该升级。"""
    assert should_escalate(DEFAULT, _deep_batch(-24), my_call="", qso_progress=0,
                           requested=False) is False
    assert DEFAULT.min_peak_snr < -24, "阈值必须低于实测完整的点，否则全是白烧"


def test_explicit_request_escalates_only_when_enabled() -> None:
    assert should_escalate(DEFAULT, _batch(-5), my_call="", qso_progress=0,
                           requested=True) is False
    policy = CascadePolicy(escalate_on_request=True)
    assert should_escalate(policy, _batch(-5), my_call="", qso_progress=0,
                           requested=True) is True


def test_all_criteria_off_never_escalates() -> None:
    policy = CascadePolicy(escalate_on_ap_context=False, escalate_on_weak=False)
    assert should_escalate(policy, _batch(), my_call="BA4EG", qso_progress=3,
                           requested=False) is False


def test_provisional_flag_matches_the_c_header() -> None:
    """Python 的 provisional 位必须与 C 头里的 `WSJT_FLAG_SNR_PROVISIONAL` 同值。

    这两处一旦漂移，`_snr_is_trustworthy` 就会恒为 True，拿 score 估计值去比 dB 阈值 ——
    正是 spec D-7 要挡掉的那个 bug，而且它会静默地重新出现（测试全绿、行为变差）。

    **把数字解析出来比，不要断言子串。** 头里的行是
    `WSJT_FLAG_SNR_PROVISIONAL = 16   /* snr 字段不是 dB … */`，写
    `f"… = {FLAG_SNR_PROVISIONAL}" in header` 看着够用，其实挡不住本用例唯一
    要挡的那类漂移：常量改成 **1**（= `WSJT_FLAG_AP` 的值，最像会手滑的写法）时，
    要找的串变成 `WSJT_FLAG_SNR_PROVISIONAL = 1`，而它**是** `… = 16` 的前缀 ——
    断言照样通过。我实测过这个变异体：`in header` 返回 True。
    解析成 int 再比，`1 != 16` 当场就红。
    """
    header = (ROOT / "ft8" / "dsp" / "wsjt_core.h").read_text(encoding="utf-8")
    line = next(
        ln for ln in header.splitlines()
        if ln.strip().startswith("WSJT_FLAG_SNR_PROVISIONAL")
    )
    declared = int(line.split("=", 1)[1].split()[0])
    assert declared == FLAG_SNR_PROVISIONAL, (
        f"C header declares {declared}, Python has {FLAG_SNR_PROVISIONAL}"
    )
