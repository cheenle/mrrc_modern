"""Adaptive cascade policy: when a slot's fast decode needs the deep path.

Pure Python and clock-free so the policy is testable without the native
library.  See docs/superpowers/specs/2026-10-13-ft8-fast-path-cascade-design.md.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from ft8.core.models import DecodeBatch, DecodePath, DecodeResult

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


_FREQ_TOL = 5.0     # Hz — 两条路径对同一信号的频率估计允许的偏差
_DT_TOL = 0.15      # s  — 同上，时间偏差


def _same_signal(a: DecodeResult, b: DecodeResult) -> bool:
    return (
        abs(a.frequency - b.frequency) <= _FREQ_TOL
        and abs(a.dt - b.dt) <= _DT_TOL
    )


def merge_batches(fast: DecodeBatch, deep: DecodeBatch) -> DecodeBatch:
    """Union of two batches; the deep result wins on any shared signal.

    Matching is by (frequency, dt) within tolerance, **not** by text: the
    fast path leaves hashed callsigns as ``<...>`` while the deep path
    restores them, so identical text is not the right key (spec R-2).
    """

    if fast.slot_id != deep.slot_id:
        raise ValueError(
            f"cannot merge different slots: {fast.slot_id} != {deep.slot_id}"
        )

    merged = list(deep.results)
    for candidate in fast.results:
        if any(_same_signal(candidate, kept) for kept in merged):
            continue
        merged.append(candidate)
    path = DecodePath.IMPROVED if deep.results else DecodePath.FAST
    return DecodeBatch(
        slot_id=deep.slot_id,
        path=path,
        results=tuple(merged),
        overflow=fast.overflow or deep.overflow,
        deadline_missed=fast.deadline_missed or deep.deadline_missed,
        elapsed_seconds=fast.elapsed_seconds + deep.elapsed_seconds,
        escalated=True,
        fast_elapsed_seconds=fast.elapsed_seconds,
    )


logger = logging.getLogger(__name__)

# **没有 `DEFAULT_CUTOFF_SECONDS`。** 决策截止这个量在 `orchestrator.py:35` 已经有
# 唯一的定义（`DEFAULT_DECISION_CUTOFF_SECONDS = 2.5`），而 `Orchestrator(decision_cutoff=…)`
# 是个**活跃的旋钮**（`tests/ft8/engine/test_orchestrator.py:163` 传 0.0、`:170` 传
# 整个时隙）。在这里再放一个同值常量，就让同一条物理量有了两个来源：生产代码今天
# 恰好两边都是默认值，所以看不出来；某天有人给 orchestrator 传了别的截止，
# 级联的预算守卫仍在拿 2.5 算 —— 守卫被静默地重新标定，而**没有任何测试会红**。
# 这正是 Task 10 给 `boundary_epoch_for` 写「必填、故意不给默认值」的同一个理由，
# 所以 `cutoff` 也照办：必填，由接线处传 orchestrator 的那一个值。
DEFAULT_DEEP_THREADS = 3
DEFAULT_DEEP_LOWER_BOUND_SECONDS = 2.3
DEFAULT_SAFETY_MARGIN_SECONDS = 0.05


class CascadingDecoder:
    """Run the fast path by default and escalate to the deep path on demand.

    ``inner`` is a :class:`SupervisorDecoder` (or any object exposing
    ``decode_path(slot_id, samples, path, *, threads=None)``).  Both the
    clock and the slot-boundary mapping are injectable so the policy is
    fully testable without the native library.
    """

    def __init__(
        self,
        inner: object,
        policy: CascadePolicy,
        *,
        boundary_epoch_for: Callable[[int], float],
        # 必填，理由与 boundary_epoch_for 相同：决策截止的唯一来源是
        # `orchestrator.py:35` 的 DEFAULT_DECISION_CUTOFF_SECONDS（或传给
        # Orchestrator 的那个值）。本模块**不提供**自己的默认值 —— 详见上面
        # 「没有 DEFAULT_CUTOFF_SECONDS」那段。两处各自 2.5 的生产代码今天看起来
        # 是对的，而守卫恰恰是在有人改了 orchestrator 之后才悄悄失准的。
        cutoff: float,
        # 默认必须是 time.time（epoch 秒）—— orchestrator.py:125 的 `clock` 与
        # orchestrator.py:225 的 `boundary_epoch` 都是这个基准。若写成 time.monotonic，
        # clock() − boundary 会是约 −1.7e9，预算恒为正的巨数，**守卫在真实运行里永不
        # 触发**；而测试因为总是成对注入同一基准而全绿。时钟基准只允许有一处。
        clock: Callable[[], float] = time.time,
        deep_threads: int = DEFAULT_DEEP_THREADS,
        deep_lower_bound_s: float = DEFAULT_DEEP_LOWER_BOUND_SECONDS,
        safety_margin_s: float = DEFAULT_SAFETY_MARGIN_SECONDS,
        ap_context: Callable[[], tuple[str, int]] | None = None,
        requested: Callable[[], bool] | None = None,
    ) -> None:
        if deep_threads < 1:
            raise ValueError("deep_threads must be positive")
        self._inner = inner
        self._policy = policy
        self._clock = clock
        self._cutoff = cutoff
        # 必填，故意不给默认值：`slot * 15.0` 这种"默认"会把单调时钟和
        # 时隙号混在一起算，忘了注入的后果是预算判断静默出错（该升级时不升级、
        # 或反之），而不是报错。宁可构造时就 TypeError。
        self._boundary_epoch_for = boundary_epoch_for
        self._deep_threads = deep_threads
        self._deep_lower_bound_s = deep_lower_bound_s
        self._safety_margin_s = safety_margin_s
        self._ap_context = ap_context or (lambda: ("", 0))
        self._requested = requested or (lambda: False)
        self.deadline_risk = 0

    def set_policy(self, policy: CascadePolicy) -> None:
        """替换升级策略，从下一个时隙起生效。

        只换策略对象，不碰任何在跑的状态 —— 所以保存设置时可以直接推给活着的
        解码器，不必重建它（重建会丢掉 in-flight 的时隙）。
        """
        self._policy = policy

    def _remaining_budget(self, slot_id: int) -> float:
        """本时隙还剩几秒可以拿去跑深路径。

        只在**快路径跑完之后**调用，所以 ``clock() − boundary`` 已经把快路径的耗时
        算进去了，不再另减 ``batch_fast.elapsed_seconds`` —— 那会把同一段时间扣两次。
        """
        boundary = self._boundary_epoch_for(slot_id)
        return self._cutoff - (self._clock() - boundary) - self._safety_margin_s

    async def decode(self, slot_id: int, samples: bytes) -> DecodeBatch:
        try:
            batch_fast = await self._inner.decode_path(
                slot_id, samples, DecodePath.FAST
            )
        except Exception:
            # 快路径整个不可用时没有结果可退，只能在明知超预算的情况下补跑一次深路径；
            # 但线程数仍必须是 deep_threads —— threads=1 要 3.23s，塞不进 2.5s 窗口
            # （spec D-4），那不是"慢一点"，是必定错过截止。
            logger.warning(
                "fast decode path failed; running the deep path unbudgeted", exc_info=True
            )
            return await self._inner.decode_path(
                slot_id, samples, DecodePath.IMPROVED, threads=self._deep_threads
            )

        my_call, qso_progress = self._ap_context()
        if not should_escalate(
            self._policy, batch_fast, my_call=my_call,
            qso_progress=qso_progress,
            requested=self._requested(),
        ):
            return batch_fast

        if self._remaining_budget(slot_id) < self._deep_lower_bound_s:
            self.deadline_risk += 1
            logger.info(
                "skipping deep escalation for slot %s: budget too tight", slot_id
            )
            return batch_fast

        try:
            batch_deep = await self._inner.decode_path(
                slot_id, samples, DecodePath.IMPROVED, threads=self._deep_threads
            )
        except Exception:
            logger.warning("deep decode path failed; keeping the fast results", exc_info=True)
            return batch_fast

        return merge_batches(batch_fast, batch_deep)
