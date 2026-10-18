"""SdrHub -- 调谐仲裁、解调池、容量闸门与扇出。

纯逻辑：不碰网络、不碰设备，时间由注入的 clock 提供，pipe 由注入的对象提供。
所以它的每一条规则都能在没有硬件、没有 sleep 的情况下测（见 tests/test_sdr_hub.py）。

设计见 docs/superpowers/specs/2026-10-18-sdr-receiver-design.md §6/§7。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Protocol


class SdrCapacityError(Exception):
    """并发客户端超限。"""


class SdrChannelFull(Exception):
    """解调通道用尽。"""


class PipeLike(Protocol):                      # pragma: no cover - 结构声明
    def tune(self, hz: float) -> None: ...
    def set_channel(self, chid: int, offset_hz: float, mode: int, bw_hz: float) -> None: ...
    def clear_channel(self, chid: int) -> None: ...


@dataclass(frozen=True)
class ChannelKey:
    """解调池的键：这三样相同的人共用一路解调器。

    静噪与音量是订阅者自己的参数，**不进 key**——否则两个静噪旋钮不同的人
    会各开一路解调，白烧一倍 CPU。
    """
    offset_hz: int
    mode: int
    bw_hz: int


@dataclass
class Subscriber:
    sid: int
    name: str = ""
    send: Callable[[tuple], None] = field(default=lambda frame: None)
    abs_freq: int = 0                 # 听众想听的绝对频率
    chid: int | None = None
    key: ChannelKey | None = None
    squelch: float = 0.0
    volume: float = 1.0


SPECTRUM_FULL = 1024          # 默认档：1024 bin
SPECTRUM_LOW = 512            # 人多时降档
LOW_TIER_THRESHOLD = 12       # 超过这个人数就降档（AD-025 的同一思路）


class SdrHub:
    def __init__(self, pipe: PipeLike | None = None, clock: Callable[[], float] = time.monotonic,
                 hold_s: float = 30.0, max_clients: int = 50, max_channels: int = 8,
                 span_hz: int = 2_400_000, spectrum_bins: int = 1024):
        self._pipe = pipe
        self._clock = clock
        self._hold_s = hold_s
        self._max_clients = max_clients
        self._max_channels = max_channels
        self._span = int(span_hz)
        self.center_hz = 7_074_000
        self.spectrum_bins = spectrum_bins
        self._subs: dict[int, Subscriber] = {}
        self._pool: dict[ChannelKey, tuple[int, set[int]]] = {}
        self._chid_owner: dict[int, ChannelKey] = {}
        self._hold_owner: int | None = None
        self._hold_until = 0.0
        self.last_rms: dict[int, float] = {}
        self.available = False
        self.unavailable_reason = "sdr pipe not started"
        self.dropped_frames = 0

    # -- 状态 -------------------------------------------------------------
    @property
    def listener_count(self) -> int:
        return len(self._subs)

    @property
    def channel_count(self) -> int:
        return len(self._pool)

    def _refresh_tier(self) -> None:
        self.spectrum_bins = SPECTRUM_FULL if len(self._subs) <= LOW_TIER_THRESHOLD else SPECTRUM_LOW

    def hold_remaining(self) -> float:
        return max(0.0, self._hold_until - self._clock())

    def snapshot(self) -> dict:
        return {
            "center_hz": self.center_hz,
            "span_hz": self._span,
            "listeners": self.listener_count,
            "channels": self.channel_count,
            "hold_remaining_s": round(self.hold_remaining(), 1),
            "available": self.available,
            "reason": None if self.available else self.unavailable_reason,
            "spectrum_bins": self.spectrum_bins,
        }

    def set_available(self, ok: bool, reason: str = "") -> None:
        self.available = bool(ok)
        self.unavailable_reason = "" if ok else (reason or "unavailable")

    # -- 订阅 -------------------------------------------------------------
    def attach(self, sid: int) -> Subscriber:
        if len(self._subs) >= self._max_clients:
            raise SdrCapacityError(f"{self._max_clients} listeners already connected")
        sub = Subscriber(sid=sid, abs_freq=self.center_hz)
        self._subs[sid] = sub
        self._refresh_tier()
        return sub

    def detach(self, sub: Subscriber) -> None:
        self._release_channel(sub)
        self._subs.pop(sub.sid, None)
        self._refresh_tier()
        if self._hold_owner == sub.sid:        # 持有者走了，锁立刻释放
            self._hold_owner = None
            self._hold_until = 0.0

    # -- 调谐仲裁 ---------------------------------------------------------
    def tune(self, sub: Subscriber, center_hz: int) -> tuple[bool, float]:
        now = self._clock()
        if self._hold_owner is not None and self._hold_owner != sub.sid and now < self._hold_until:
            return False, max(0.0, self._hold_until - now)
        center_hz = int(center_hz)
        if not (10_000 <= center_hz <= 2_000_000_000):
            return False, 0.0
        self.center_hz = center_hz
        self._hold_owner = sub.sid
        self._hold_until = now + self._hold_s
        if self._pipe is not None:
            self._pipe.tune(center_hz)
        self._rebase_all()
        return True, 0.0

    def _rebase_all(self) -> None:
        """改中心频率后按**绝对频率**重算每个订阅者的 offset（不是重新居中）。"""
        half = self._span // 2 - 1000
        for sub in list(self._subs.values()):
            want = sub.abs_freq
            offset = want - self.center_hz
            if offset > half:
                offset = half
            if offset < -half:
                offset = -half
            sub.abs_freq = self.center_hz + offset
            if sub.key is None or sub.key.offset_hz != offset:
                self._reassign(sub, offset)

    # -- 解调池 -----------------------------------------------------------
    def subscribe_channel(self, sub: Subscriber, mode: int, bw_hz: int) -> int:
        offset = sub.abs_freq - self.center_hz
        key = ChannelKey(int(offset), int(mode), int(bw_hz))
        return self._assign(sub, key)

    def set_cursor(self, sub: Subscriber, offset_hz: float) -> int:
        """听众点瀑布选的解调点：存成**绝对频率**，改中心频率时才不会被带走。"""
        half = self._span // 2 - 1000
        offset = int(max(-half, min(half, int(offset_hz))))
        sub.abs_freq = self.center_hz + offset
        return sub.abs_freq

    def set_squelch(self, sub: Subscriber, value: float) -> None:
        sub.squelch = max(0.0, min(1.0, float(value)))

    def set_volume(self, sub: Subscriber, value: float) -> None:
        sub.volume = max(0.0, min(1.0, float(value)))

    def unsubscribe_channel(self, sub: Subscriber) -> None:
        self._release_channel(sub)

    def _assign(self, sub: Subscriber, key: ChannelKey) -> int:
        self._release_channel(sub)
        entry = self._pool.get(key)
        if entry is not None:
            chid, owners = entry
            owners.add(sub.sid)
        else:
            if len(self._pool) >= self._max_channels:
                raise SdrChannelFull(
                    f"{self._max_channels} demodulators already running "
                    f"(reuse one of them or try another frequency)")
            used = set(self._pool.keys())
            chid = None
            for cand in range(self._max_channels):
                if all(v[0] != cand for v in self._pool.values()):
                    chid = cand
                    break
            if chid is None:
                raise SdrChannelFull("no free demodulator slot")
            self._pool[key] = (chid, {sub.sid})
            self._chid_owner[chid] = key
            if self._pipe is not None:
                self._pipe.set_channel(chid, key.offset_hz, key.mode, key.bw_hz)
        sub.chid = chid
        sub.key = key
        return chid

    def _reassign(self, sub: Subscriber, offset: int) -> None:
        """中心频率变了：同一个绝对频率、新的 offset ⇒ 换池条目。"""
        old_key = sub.key
        mode = old_key.mode if old_key else 0
        bw = old_key.bw_hz if old_key else 3000
        self._release_channel(sub)
        try:
            self._assign(sub, ChannelKey(int(offset), mode, bw))
        except SdrChannelFull:
            sub.key = None
            sub.chid = None

    def _release_channel(self, sub: Subscriber) -> None:
        if sub.chid is None or sub.key is None:
            sub.chid = None
            sub.key = None
            return
        key = sub.key
        entry = self._pool.get(key)
        if entry is not None:
            chid, owners = entry
            owners.discard(sub.sid)
            if not owners:
                del self._pool[key]
                self._chid_owner.pop(chid, None)
                self.last_rms.pop(chid, None)
                if self._pipe is not None:
                    self._pipe.clear_channel(chid)
        sub.chid = None
        sub.key = None

    # -- 扇出 -------------------------------------------------------------
    def on_spectrum(self, seq: int, bins: bytes) -> None:
        for sub in list(self._subs.values()):
            sub.send(("spectrum", seq, bins))

    def on_audio(self, chid: int, seq: int, level: float, payload: bytes) -> None:
        self.last_rms[chid] = level
        key = self._chid_owner.get(chid)
        if key is None:
            return
        entry = self._pool.get(key)
        if entry is None:
            return
        _, owners = entry
        for sid in list(owners):
            sub = self._subs.get(sid)
            if sub is None:
                continue
            if sub.squelch > 0.0 and level * sub.volume < sub.squelch:
                continue
            sub.send(("audio", chid, seq, level, payload))

    def broadcast_state(self) -> None:
        """每人一份：中心频率是共享的，`abs_freq`/`chid` 是各自的。"""
        base = self.snapshot()
        for sub in list(self._subs.values()):
            snap = dict(base)
            snap["abs_freq"] = sub.abs_freq
            snap["chid"] = sub.chid
            sub.send(("state", snap))
