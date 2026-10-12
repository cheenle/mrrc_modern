"""CQ 空闲频点选择与近期频率占用跟踪（UC-004 扩展，2026-08-07）。

回复已跟随伙伴解码频率（UC-003）；主动 CQ 仍固定 1500 Hz 会与其他台
重叠。这里提供：TTL 占用环（从解码事件 note 频率）+ 纯函数
``pick_cq_frequency`` —— 在 1500 ± ``search_window_hz`` 内螺旋扫描第一个
与所有占用频率中心距 >= ``guard_hz`` 的整数频点；窗口内无空闲则回退默认。
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable, Iterable

from .sequencer import DEFAULT_TX_AUDIO_FREQUENCY

# FT8 信号约 ±25 Hz 主瓣；中心距 < guard 视为冲突（相邻信号可解性）。
DEFAULT_GUARD_HZ = 30.0
# 默认附近的可选范围（音频带 12 kHz，3 kHz waterfall 中段足够）。
DEFAULT_SEARCH_WINDOW_HZ = 300.0
# 一次解码的占用在多长时间内仍视为占用（分钟级：CQ/re-CQ 决策窗口）。
DEFAULT_OCCUPANCY_TTL_S = 120.0


def pick_cq_frequency(
    occupied: Iterable[float],
    default: float = DEFAULT_TX_AUDIO_FREQUENCY,
    *,
    search_window_hz: float = DEFAULT_SEARCH_WINDOW_HZ,
    guard_hz: float = DEFAULT_GUARD_HZ,
) -> float:
    """Pick an unoccupied integer Hz offset near ``default``.

    Spirals outward from ``default`` (1500, 1501, 1499, 1502, …) and returns
    the first candidate whose centre-to-centre distance to every occupied
    frequency is >= ``guard_hz``.  Falls back to ``default`` when the whole
    window is occupied (the call still goes out; the operator hears it).

    Pure function: unit-testable without audio/DSP state.
    """

    if guard_hz < 0 or search_window_hz < 0:
        raise ValueError("guard_hz and search_window_hz must be non-negative")
    occupied_list = [float(f) for f in occupied if f > 0]
    candidates: list[float] = []
    max_step = int(search_window_hz)
    for step in range(max_step + 1):
        candidates.append(default + step)
        if step:
            candidates.append(default - step)
    for freq in candidates:
        if all(abs(freq - occ) >= guard_hz for occ in occupied_list):
            return freq
    return default


class FrequencyOccupancy:
    """Recent decoded audio offsets, kept for a short TTL.

    ``note`` is called once per decoded message (own echoes included — the
    offset genuinely carries our signal); ``pick_frequency`` prunes stale
    entries and hands the live set to :func:`pick_cq_frequency`.
    """

    def __init__(
        self,
        *,
        ttl_s: float = DEFAULT_OCCUPANCY_TTL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_s <= 0:
            raise ValueError("ttl_s must be positive")
        self._ttl_s = ttl_s
        self._clock = clock
        self._entries: deque[tuple[float, float]] = deque()

    def note(self, freq: float, now: float | None = None) -> None:
        """Record one decoded offset; bounded growth (TTL prune on read)."""

        if freq <= 0:
            return
        self._entries.append((freq, self._clock() if now is None else now))

    def prune(self, now: float | None = None) -> None:
        """Drop entries older than the TTL; O(n) from the front."""

        t = self._clock() if now is None else now
        while self._entries and t - self._entries[0][1] > self._ttl_s:
            self._entries.popleft()

    def occupied_freqs(self, now: float | None = None) -> list[float]:
        """Live occupied offsets (stale entries pruned first)."""

        self.prune(now)
        return [freq for freq, _ in self._entries]

    def pick_frequency(
        self,
        default: float = DEFAULT_TX_AUDIO_FREQUENCY,
        *,
        search_window_hz: float = DEFAULT_SEARCH_WINDOW_HZ,
        guard_hz: float = DEFAULT_GUARD_HZ,
        now: float | None = None,
    ) -> float:
        """Pick an unoccupied offset near ``default`` from the live set."""

        return pick_cq_frequency(
            self.occupied_freqs(now),
            default,
            search_window_hz=search_window_hz,
            guard_hz=guard_hz,
        )
