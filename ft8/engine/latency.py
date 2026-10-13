"""Decode-latency histograms (NFR-002, §12.8).

One :class:`LatencyHistogram` aggregates wall-clock decode duration per
(path, profile, threads) configuration in fixed buckets aligned with the I9
2.5 s TX decision cutoff (§13.4), so operators can see whether a profile
change threatens the cutoff before the deadline-miss counter climbs.
The orchestrator already counts actual deadline misses; this module only
measures.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

BUCKET_BOUNDS_SECONDS = (0.5, 1.0, 1.5, 2.0, 2.5)
"""Bucket upper edges; the last snapshot entry is the > 2.5 s overflow."""


def bucket_index(elapsed_seconds: float) -> int:
    """Return the bucket for one measurement; ``len(BOUNDS)`` is overflow."""

    for index, bound in enumerate(BUCKET_BOUNDS_SECONDS):
        if elapsed_seconds < bound:
            return index
    return len(BUCKET_BOUNDS_SECONDS)


@dataclass(slots=True)
class _Series:
    """Mutable counters for one (path, profile, threads) configuration."""

    buckets: list[int] = field(
        default_factory=lambda: [0] * (len(BUCKET_BOUNDS_SECONDS) + 1)
    )
    count: int = 0
    max_seconds: float = 0.0


class LatencyHistogram:
    """Thread-safe per-configuration decode-latency counters (NFR-002)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._series: dict[tuple[str, int, int], _Series] = {}

    def record(
        self, path: str, profile: int, threads: int, elapsed_seconds: float
    ) -> None:
        """Add one wall-clock decode measurement for a configuration.

        ``path`` 是必需的维度，不是修饰：默认的 fast 路径约 0.15 s、深路径约 2.2 s，
        混在一个桶里会让快路径把深路径的尾部分布整个冲淡，而 NFR-002 要回答的
        恰恰是深路径离 2.5 s 截止还有多远。
        """

        if elapsed_seconds < 0:
            raise ValueError("elapsed must not be negative")
        with self._lock:
            series = self._series.setdefault((path, profile, threads), _Series())
            series.buckets[bucket_index(elapsed_seconds)] += 1
            series.count += 1
            series.max_seconds = max(series.max_seconds, elapsed_seconds)

    def snapshot(self) -> dict[str, dict[str, object]]:
        """Return a JSON-ready copy keyed by ``"<path>/p<profile>/t<threads>"``."""

        labels = [f"<{bound:g}s" for bound in BUCKET_BOUNDS_SECONDS] + [
            f">={BUCKET_BOUNDS_SECONDS[-1]:g}s"
        ]
        with self._lock:
            return {
                f"{path}/p{profile}/t{threads}": {
                    "buckets": dict(zip(labels, series.buckets)),
                    "count": series.count,
                    "max_seconds": round(series.max_seconds, 3),
                }
                for (path, profile, threads), series in sorted(self._series.items())
            }
