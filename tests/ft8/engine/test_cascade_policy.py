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

    **把数字解析出来比，不要断言子串。** 头里这一行原先带行尾注释
    （`WSJT_FLAG_SNR_PROVISIONAL = 16   /* … */`），现在注释在常量上方、常量单独一行 ——
    两种形状都躲不过下面这类漂移：写
    `f"… = {FLAG_SNR_PROVISIONAL}" in header` 看着够用，其实挡不住本用例唯一
    要挡的那类漂移：常量改成 **1**（= `WSJT_FLAG_AP` 的值，最像会手滑的写法）时，
    要找的串变成 `WSJT_FLAG_SNR_PROVISIONAL = 1`，而它**是** `… = 16` 的前缀 ——
    断言照样通过。我实测过这个变异体：`in header` 返回 True。
    解析成 int 再比，`1 != 16` 当场就红。
    解析写成 `split("=", 1)[1].split()[0]`，所以注释挪到常量上方或留在行尾都不影响。
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


def test_a_mixed_batch_is_not_trustworthy() -> None:
    """一批里只要有一条 provisional，整批的 snr 就都不可信。

    `_snr_is_trustworthy` 用的是 `all(not provisional)` 而不是 `any(...)`；
    两者**只**在这种混合批次上分道扬镳 —— 而这正是本模块的核心不变量。
    两条结果分处不同频率（1500 / 1700 Hz）以免被当成一条信号，两条的 snr
    都低于阈值（-30 < -26），于是两种读法各自给出：

    - 按 `all`：存在一条 provisional，整批判据被跳过，升级只看 min_decodes，
      有结果 → 返回 False。这正是本用例断言的结果。
    - 按 `any`：那条干净的（flags=0）让整批「可信」，峰值判据生效，
      -30 < -26 → 返回 True —— 把同一批里 score 估计值拿去比了 dB 阈值，
      也就是 spec D-7 要挡住的那个 bug。

    这个 **True/False 之差**就是本用例的全部意义：删掉其中一行就没有混合
    批次，`all` 与 `any` 再无差别，本用例也就什么都钉不住了。
    """
    results = (
        DecodeResult(slot_id=1, sync=0.0, snr=-30, dt=0.0, frequency=1500.0,
                     text="CQ TEST", ap_type=0, quality=0.0,
                     flags=FLAG_SNR_PROVISIONAL),
        DecodeResult(slot_id=1, sync=0.0, snr=-30, dt=0.0, frequency=1700.0,
                     text="CQ TEST", ap_type=0, quality=0.0, flags=0),
    )
    batch = DecodeBatch(slot_id=1, path=DecodePath.FAST, results=results,
                        overflow=False, elapsed_seconds=0.15)
    assert should_escalate(DEFAULT, batch, my_call="", qso_progress=0,
                           requested=False) is False


def test_empty_batch_does_not_crash_when_min_decodes_is_zero() -> None:
    """min_decodes=0 是合法设置，空批次不能因此抛 ValueError。

    Task 14 的校验只拒绝**负数** min_decodes，0 是允许的。此时空批次会
    穿过 `len(batch.results) < policy.min_decodes` 这条短路（`0 < 0` 为
    False），落到 `_snr_is_trustworthy`。少了 `if not batch.results: return
    False` 这道守卫，`all(...)` 在空序列上恒为 True，接着
    `max(r.snr for r in batch.results)` 就抛
    `ValueError: max() iterable argument is empty` —— 对任何设置里存了
    min_decodes=0 的操作者，每个时隙都会崩在这儿。
    """
    policy = CascadePolicy(min_decodes=0)
    assert should_escalate(policy, _batch(), my_call="", qso_progress=0,
                           requested=False) is False
