"""
Local control surface — a Bluetooth/IR remote driving the radio on-box.
=======================================================================

The operator stands next to the box, points a remote at it and tunes: no
browser, no phone.  The remote is an ordinary Bluetooth HID device (a China
Mobile voice remote was the first one measured, see SDD AD-028), so the key
events arrive on ``/dev/input/eventN`` after bluez builds the HID device.

Two layers, deliberately separated:

* **pure logic** (no I/O, unit-testable): ``InputEvent`` decoding, ``KeyMap``,
  ``NumericEntry`` and ``Controller`` — a key press in, radio commands out.
* **I/O** (``LocalCtl.run``): open the evdev node and write one JSON line per
  command to the server's local control socket (``/run/mrrc/localctl.sock``,
  0600, same user as the server).  The socket's permission *is* the
  authentication: a physical button has no web password, and making the daemon
  log in instead tripped the server's five-logins-per-five-minutes guard.

Safety rules that are *not* negotiable (they mirror the browser's PTT rules,
SDD ch15 §15.6):

* a PTT release is sent on key release, on SIGTERM/SIGINT, and before any
  reconnect — a remote that dies must never leave the radio keyed;
* the hold is bounded by ``MRRC_PTT_MAX_TX_SECONDS`` (the server enforces the
  same limit, this is the local half).

Key identity
------------
evdev reports a *usage* (``MSC_SCAN``) and a *keycode*.  Three buttons on the
measured remote map to keycode 240 (``KEY_UNKNOWN``) — 设置 / M / # — so the
keymap keys on the **usage** and the reader hands it to the controller
together with the key event.  Standard keys keep a ``k:<code>`` fallback so a
different remote still works for the common buttons.

Environment
-----------
``MRRC_LOCALCTL_DEVICE``  explicit ``/dev/input/eventN`` (default: auto-find)
``MRRC_LOCALCTL_MATCH``   substring of the device name to auto-find the remote
``MRRC_LOCALCTL_KEYMAP``  JSON file overriding individual keys (path)
``MRRC_LOCALCTL_STEP``    frequency step in Hz (default 1000)
``MRRC_WEB_PORT``         server port on loopback (default 8888)
``MRRC_WEB_PASSWORD``     password used to obtain a session token
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import struct
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Optional

log = logging.getLogger("mrrc.localctl")

# ── evdev wire format ───────────────────────────────────────────────────────
# struct input_event on 64-bit Linux: time_t, suseconds_t, __u16, __u16, __s32
INPUT_EVENT = struct.Struct("=qqHHi")
EV_SYN, EV_KEY, EV_MSC = 0x00, 0x01, 0x04
MSC_SCAN = 0x04
KEY_RELEASE, KEY_PRESS, KEY_REPEAT = 0, 1, 2

# ── default keymap (measured 2026-10-17 on the China Mobile voice remote) ───
# Usage values are the HID usages from MSC_SCAN; ``k:`` entries are keycodes.
DEFAULT_KEYMAP: dict[str, str] = {
    "u:0x7003e": "ptt",        # 语音 ── KEY_F5
    "u:0x70066": "power",      # 电源 ── KEY_POWER
    "u:0x70052": "freq_up",    # ▲
    "u:0x70051": "freq_down",  # ▼
    "u:0x70050": "step_down",  # ◀  finer step (index −1)
    "u:0x7004f": "step_up",    # ▶  coarser step (index +1)
    "u:0xc0041": "enter",      # 确定 (consumer page; the measured remote sends this, not 0x70058)
    "u:0xc0223": "home",       # 首页
    "u:0xc0224": "cancel",     # 返回
    "u:0x70065": "menu",       # 菜单
    "u:0xc00eb": "mode_next",  # 设置
    "u:0xc00f0": "aux",        # M
    "u:0x7004b": "band_up",    # 频道 +
    "u:0x7004e": "band_down",  # 频道 −
    "u:0x70055": "star",       # *
    "u:0x700cc": "hash",       # #
    # Xiaomi Bluetooth Remote 2 (ATVV voice remote, measured 2026-11-10).  Its
    # OK/Back/Home use the keyboard page, not the consumer page the CMCC remote
    # sends, and 音量 only exists as the keyboard keycode fallback below.
    "u:0x70028": "enter",      # 确定 ── KEY_ENTER
    # 电视键（2026-11-14 实测）：它**发蓝牙**，不是移动那支只发红外 —— 实测
    # `u:0x70035` / keycode 41（KEY_GRAVE）。操作员指定它换波段。
    "u:0x70035": "band_next",  # 电视 ── 换到下一个波段
    "u:0x700f1": "cancel",     # 返回 ── KEY_BACK
    "u:0x7004a": "home",       # 主页 ── KEY_HOME
    # digits (USB HID keyboard page: 0x1E..0x26 = 1..9, 0x27 = 0)
    "u:0x7001e": "digit:1",
    "u:0x7001f": "digit:2",
    "u:0x70020": "digit:3",
    "u:0x70021": "digit:4",
    "u:0x70022": "digit:5",
    "u:0x70023": "digit:6",
    "u:0x70024": "digit:7",
    "u:0x70025": "digit:8",
    "u:0x70026": "digit:9",
    "u:0x70027": "digit:0",
    # keycode fallbacks (these report clean keycodes on the measured remote)
    "k:115": "vol_up",         # KEY_VOLUMEUP
    "k:114": "vol_down",       # KEY_VOLUMEDOWN
    "k:63": "ptt",             # KEY_F5, same button as the usage above
    "k:353": "enter",          # KEY_SELECT, the 确定 key when usage differs
    "k:172": "home",           # KEY_HOMEPAGE
    "k:158": "cancel",         # KEY_BACK
    "k:127": "menu",           # KEY_COMPOSE
}

# Mode cycle order used by the 设置 key.  Only modes the current radio
# advertises are cycled — see Controller.set_available_modes().
MODE_CYCLE = ("LSB", "USB", "CW", "AM", "FM", "FT8")

STEP_LADDER_HZ = (100, 1000, 5000, 10000, 100000)

#: 「确定」键在没有待提交数字时的落点 —— 操作员指定的 40 m 常用频点（2026-11-14）：
#: 按一下就回到 7.050 LSB，不用先输入数字。
HOME_SPOT_HZ, HOME_SPOT_MODE = 7_050_000, "LSB"

#: 一键 CQ 的音频约 6.1 s。这个窗口内再按一次主页键就是**中止**这次呼叫；超过窗口
#: 再按就是新的一次 —— 遥控器没有屏幕，所以"同一个键第二次按下去会发生什么"必须
#: 由时间决定，而不是由它记不住的界面状态决定。
CQ_ABORT_WINDOW_S = 10.0

#: "边沿动作"（换波段、QSY、频道回放、模式循环、数字键…）的按下记忆时长。
#: 小米遥控器 2 **按住时以 PRESS(value=1) 连发**，约 250 ms 一次，而不是内核
#: autorepeat（value=2，`REPEATABLE` 已经挡住）。2026-11-14 现场（盒子 217）：
#: 按住电视键一秒，12 条 `band_next` 全部执行，电台从 10.14 MHz 一路跳到
#: 1.8455 MHz —— 操作员看到的就是"频率乱跳"。所以边沿动作自己要看"松手了
#: 没有"，而不是把每个 PRESS 都当成新按键。
#:
#: 它同时是**丢失 RELEASE 的兜底**：只发 PRESS 的固件不能把键永久锁死，超过
#: 这个时长之后的同一个动作照样生效。手机上人手连按（100–200 ms 一下）有
#: RELEASE，所以不会被吞掉 —— 不能用时间窗代替 RELEASE 判断。
HELD_STALE_S = 1.5

#: The radio's AF gain is a **0-255 register** (`radio_state.af_gain`; Yaesu
#: `AG0xxx`, Icom `LVL_AF`), while a remote's volume key reads as a percentage.
#: The percentage has to be scaled on the way out: sending it raw topped the
#: radio out at 100/255 = 39 % of full scale, so "hold 音量 up" stopped short of
#: the loudest the set could do (2026-10-18).  The web UI does the same
#: conversion the other way for RF gain (`ft710_ui.js`, `/255*100`).
AF_GAIN_MAX = 255
VOLUME_STEP_PCT = 5

#: Radios without a radio-side AF gain (the SunSDR2DX reports
#: ``has_af_gain=False``) still have the box's RX fan-out level (AD-032), the
#: same number the web UI's 🔊 Vol slider writes.  Its range is the server's
#: ``audio_levels.RX_LIMITS``; a key press steps it by LEVEL_STEP_DB.
RX_DB_LIMITS = (-20.0, 30.0)
LEVEL_STEP_DB = 3.0


def gain_from_volume(pct: float) -> int:
    """Percentage of full volume → the radio's 0-255 AF gain register."""
    return max(0, min(AF_GAIN_MAX, round(pct / 100 * AF_GAIN_MAX)))


def volume_from_gain(gain: float) -> int:
    """The radio's 0-255 AF gain → the percentage the keys step in."""
    return max(0, min(100, round(gain / AF_GAIN_MAX * 100)))

BAND_EDGES_HZ = (
    (1_800_000, 2_000_000),      # 160 m
    (3_500_000, 4_000_000),      # 80 m
    (5_000_000, 5_500_000),      # 60 m
    (7_000_000, 7_300_000),      # 40 m
    (10_100_000, 10_150_000),    # 30 m
    (14_000_000, 14_350_000),    # 20 m
    (18_068_000, 18_168_000),    # 17 m
    (21_000_000, 21_450_000),    # 15 m
    (24_890_000, 24_990_000),    # 12 m
    (28_000_000, 29_700_000),    # 10 m
    (50_000_000, 54_000_000),    # 6 m
)

#: Where 频道 +/− lands.  Fixed spots, not "the same relative offset in the next
#: band": the remote has no display, so a landing point that depends on where
#: the operator happened to be is not a landing point an operator can predict.
#: Field plan (2026-10-18): 10 MHz and below → LSB, above → USB, 29.600 → FM.
#: Edit this table to change the channels — the mode travels with the frequency.
PRESET_CHANNELS = (
    (3_850_000, "LSB"),      # 80 m
    (7_050_000, "LSB"),      # 40 m
    (10_100_000, "USB"),     # 30 m
    (14_270_000, "USB"),     # 20 m
    (21_240_000, "USB"),     # 15 m
    (28_500_000, "USB"),     # 10 m
    (29_600_000, "FM"),      # 10 m FM calling
)


# ── pure layer ──────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class InputEvent:
    type: int
    code: int
    value: int


def parse_events(blob: bytes) -> Iterator[InputEvent]:
    """Decode a buffer of ``input_event`` structs (ignores a partial tail)."""
    count = len(blob) // INPUT_EVENT.size
    for i in range(count):
        _sec, _usec, ev_type, ev_code, ev_value = INPUT_EVENT.unpack_from(blob, i * INPUT_EVENT.size)
        yield InputEvent(ev_type, ev_code, ev_value)


def key_ids(scan_usage: Optional[int], keycode: int) -> tuple[str, ...]:
    """Candidate identities for one key press, most specific first."""
    ids = []
    if scan_usage is not None:
        ids.append(f"u:0x{scan_usage:x}")
    ids.append(f"k:{keycode}")
    return tuple(ids)


class KeyMap:
    """Usage/keycode → action name, with a JSON override file."""

    def __init__(self, mapping: Optional[dict[str, str]] = None):
        self._map = dict(DEFAULT_KEYMAP)
        if mapping:
            self._map.update({str(k).lower(): v for k, v in mapping.items()})

    @classmethod
    def load(cls, path: Optional[str] = None) -> "KeyMap":
        if not path or not os.path.exists(path):
            return cls()
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError) as exc:
            log.warning("keymap %s unreadable (%s), using defaults", path, exc)
            return cls()
        if not isinstance(data, dict):
            log.warning("keymap %s is not an object, using defaults", path)
            return cls()
        return cls(data)

    def action_for(self, ids: Iterable[str]) -> Optional[str]:
        for key in ids:
            act = self._map.get(key.lower())
            if act:
                return act
        return None


class NumericEntry:
    """Digits typed on the remote → a frequency in kHz → Hz.

    ``7 0 5 0`` on 确定 becomes 7 050 000 Hz.  A 3-decimal form (``7 0 5 0 0``)
    is read as kHz with a trailing 0 that would overflow, so the buffer is
    clamped to the highest plausible band and committed as-is: the operator
    sees the result on the dial and corrects, which beats silently guessing.
    """

    TIMEOUT_S = 3.0
    MAX_DIGITS = 6

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._digits = ""
        self._stamp = 0.0

    @property
    def pending(self) -> str:
        return self._digits

    def push(self, digit: str, now: Optional[float] = None) -> None:
        if not digit.isdigit():
            return
        now = self._clock() if now is None else now
        if self._digits and now - self._stamp > self.TIMEOUT_S:
            self._digits = ""
        if len(self._digits) < self.MAX_DIGITS:
            self._digits += digit
        self._stamp = now

    def clear(self) -> None:
        self._digits = ""

    def commit(self, now: Optional[float] = None) -> Optional[int]:
        """Return Hz, or None when the buffer is empty / stale."""
        now = self._clock() if now is None else now
        digits = self._digits
        self._digits = ""
        if not digits:
            return None
        if now - self._stamp > self.TIMEOUT_S:
            return None
        hz = int(digits) * 1000          # kHz on the remote, Hz on the wire
        if hz < 100_000 or hz > 60_000_000:
            return None
        return hz


def band_span(freq_hz: int) -> tuple[int, int]:
    """这次频率所在的波段边缘；不在任何业余波段里就给全段范围。"""
    for low, high in BAND_EDGES_HZ:
        if low <= freq_hz <= high:
            return low, high
    return 30_000, 60_000_000


def clamp_step(index: int) -> int:
    return max(0, min(len(STEP_LADDER_HZ) - 1, index))


def step_freq(freq_hz: int, step_hz: int, up: bool) -> int:
    """Step the dial, wrapping inside the band that contains the current spot."""
    target = freq_hz + (step_hz if up else -step_hz)
    low, high = band_span(freq_hz)
    if low <= freq_hz <= high:
        if target > high:
            return low
        if target < low:
            return high
        return target
    return max(30_000, min(60_000_000, target))


def next_channel(freq_hz: int, up: bool, presets=None):
    """Step 频道 +/− along the preset table, wrapping at both ends.

    Returns ``(frequency_hz, mode_name)``.  A bare frequency that sits on no
    preset starts from the nearest one, so the first press always lands on a
    known spot instead of somewhere between two.
    """
    table = PRESET_CHANNELS if presets is None else presets
    if not table:
        return freq_hz, ""
    idx = nearest_preset(freq_hz, table)
    return table[(idx + (1 if up else -1)) % len(table)]


def nearest_preset(freq_hz: int, presets=None) -> int:
    """Index of the preset a bare frequency belongs to (nearest by distance)."""
    table = PRESET_CHANNELS if presets is None else presets
    return min(range(len(table)), key=lambda i: abs(table[i][0] - freq_hz))


def next_mode(current: str, cycle=MODE_CYCLE) -> str:
    if not cycle:
        return current
    cur = (current or "").upper()
    try:
        idx = cycle.index(cur)
    except ValueError:
        return cycle[0]
    return cycle[(idx + 1) % len(cycle)]


@dataclass
class Controller:
    """Key action → radio commands.  No I/O: feed it actions, read commands."""

    keymap: KeyMap = field(default_factory=KeyMap)
    freq_hz: int = 7_050_000
    mode: str = "USB"
    step_index: int = 1
    #: Percent of the radio's AF gain (0-100), *not* the 0-255 register: press
    #: steps stay 5 % of travel either way.  Scaled by ``gain_from_volume`` on
    #: the way out, and seeded from the radio by ``LocalCtl._absorb``.
    volume: int = 50
    #: Box-level RX fan-out gain in dB (server ``audio_levels.rx_db``).
    rx_db: float = 0.0
    #: Does the connected backend have a radio-side AF gain?  The server sends
    #: ``af_gain_supported`` in its state line and every reply.  When it is
    #: false (SunSDR2DX, ...) the volume keys move the box's RX fan-out level
    #: instead — the same number the web UI's 🔊 Vol slider writes (AD-032).
    #: Start optimistic: an older server that does not send the flag keeps the
    #: pre-existing AF-gain behaviour.
    af_gain_supported: bool = True
    numeric: NumericEntry = field(default_factory=NumericEntry)
    _repeat_at: dict = field(default_factory=dict, repr=False)
    #: 边沿动作里"按着还没松"的那些；见 `HELD_STALE_S`。
    _held: dict = field(default_factory=dict, repr=False)
    modes: tuple[str, ...] = MODE_CYCLE
    ptt_held: bool = False
    #: 「VFO / 频道」模式（菜单键切换）。遥控器没有屏幕，所以这个状态只体现在
    #: ▲▼ 的行为上：VFO 模式按步进走，频道模式在**保存的**频道之间跳（操作员
    #: 2026-11-14 指定：菜单键切换，▲▼ 直接换 6 个频道）。
    channel_mode: bool = False
    #: 电台真实的波段表 ``[(name, default_freq), …]``，服务端从 `backend.bands`
    #: 下发 —— 硬编码一张"业余波段"表会在 FT-891（只有 HF+6 m）或 IC-705 上撒谎。
    bands: tuple = ()
    #: 保存的频道 ``[(slot, freq, mode), …]``，服务端从 `mem_channels.json` 下发。
    #: **slot 是服务端的槽号**，不是这里的下标：中间的空槽会被丢掉，按序号回放
    #: 必须说服务端听得懂的那个号。
    channels: tuple = ()
    #: 上一次按主页键（发起 CQ）的时刻。
    cq_at: Optional[float] = None
    #: Actions where holding the key should keep going.  Everything else is
    #: edge-triggered on purpose: repeating 设置 would spin through modes, and
    #: repeating a digit would turn a mistyped key into a wrong frequency.
    REPEATABLE = frozenset({"freq_up", "freq_down", "vol_up", "vol_down",
                            "level_up", "level_down"})
    #: The measured remote repeats every ~44 ms (~23/s).  A 50 ms floor dropped
    #: every single repeat and made holding the key look dead; 40 ms lets them
    #: through, and the control socket's reply (the server answers after the CAT
    #: write) paces anything faster.
    REPEAT_MIN_INTERVAL_S = 0.04

    @property
    def step_hz(self) -> int:
        return STEP_LADDER_HZ[clamp_step(self.step_index)]

    def channel_index(self) -> int:
        """最近的那个保存频道 —— 遥控器没有屏幕，只能按距离猜。"""
        freqs = [freq for _slot, freq, _mode in self.channels]
        if not freqs:
            return 0
        return min(range(len(freqs)), key=lambda i: abs(freqs[i] - self.freq_hz))

    def band_index(self) -> int:
        """当前所在的波段；不在任何波段里就用最近的那个。"""
        if not self.bands:
            return 0
        for i, (_name, freq) in enumerate(self.bands):
            low, high = band_span(freq)
            if low <= self.freq_hz <= high:
                return i
        return min(range(len(self.bands)),
                   key=lambda i: abs(self.bands[i][1] - self.freq_hz))

    def _step_channel(self, up: bool) -> list[dict]:
        """频道模式下 ▲▼：换下一个保存的频道（到底回绕）。"""
        slot, freq, mode = self.channels[
            (self.channel_index() + (1 if up else -1)) % len(self.channels)]
        self.freq_hz = freq
        if mode:
            self.mode = mode
        # `mem` 是服务端的**原子**回放（先频率、再模式、再补频率），不是两次 set：
        # 两次 set 之间的中间态会被轮询抓走，把模式改回波段栈里的旧值。
        return [{"type": "set", "field": "mem", "value": slot}]

    def _cycle_band(self) -> list[dict]:
        """TV 键：换到下一个波段（用服务端下发的真实波段表，含默认频点）。"""
        name, freq = self.bands[(self.band_index() + 1) % len(self.bands)]
        self.numeric.clear()
        self.freq_hz = freq
        return [{"type": "set", "field": "band", "value": name}]

    def on_key(self, ids: Iterable[str], pressed: bool, now: Optional[float] = None,
               repeat: bool = False) -> list[dict]:
        """Translate one key event into protocol messages (possibly none).

        ``repeat`` marks an autorepeat event (the key is being held).  Only the
        continuous actions act on it, and no faster than REPEAT_MIN_INTERVAL_S.
        """
        action = self.keymap.action_for(ids)
        if not action:
            return []
        now = time.monotonic() if now is None else now
        if repeat:
            # An autorepeat arrives with pressed=False (evdev value 2), so
            # "held" is exactly what repeat means; requiring pressed=True here
            # threw away every repeat and made a held key look dead.
            if action not in self.REPEATABLE:
                return []
            last = self._repeat_at.get(action)
            if last is not None and now - last < self.REPEAT_MIN_INTERVAL_S:
                return []
            self._repeat_at[action] = now
            pressed = True

        # Edge debounce: one press, one action.  The Xiaomi remote repeats
        # **PRESS** (value 1) every ~250 ms while a key is down — not the
        # kernel's autorepeat (value 2, which REPEATABLE already filters) — so
        # without this a one-second hold on the TV key walks the whole band
        # table (2026-11-14, box 217: twelve `band_next`, twelve real CAT
        # changes, 10.14 MHz → 1.8455 MHz).
        if not pressed:
            # A release is the only honest "the operator lifted the key".
            self._held.pop(action, None)
        elif action not in self.REPEATABLE:
            since = self._held.get(action)
            if since is not None and now - since < HELD_STALE_S:
                return []                       # the firmware's own repeat
            self._held[action] = now

        if action.startswith("digit:"):
            if pressed:
                self.numeric.push(action.split(":", 1)[1], now)
            return []

        if action == "ptt":
            if pressed and not self.ptt_held:
                self.ptt_held = True
                return [{"type": "set", "field": "ptt", "value": True}]
            if not pressed and self.ptt_held:
                self.ptt_held = False
                return [{"type": "set", "field": "ptt", "value": False}]
            return []

        if not pressed:
            return []

        if action == "enter":
            hz = self.numeric.commit(now)
            if hz is None:
                # 没有待提交的数字 → 回到 40 m 常用频点（操作员 2026-11-14 指定）。
                # 频率和模式必须**一起**下发：分开两次 set 时，中间的半套状态会被
                # 轮询抓走、模式被波段栈改回旧的（`qsy` 就是服务端那条原子路径）。
                self.numeric.clear()
                self.freq_hz, self.mode = HOME_SPOT_HZ, HOME_SPOT_MODE
                return [{"type": "set", "field": "qsy",
                         "value": {"freq": HOME_SPOT_HZ, "mode": HOME_SPOT_MODE}}]
            self.freq_hz = hz
            return [{"type": "set", "field": "freq", "value": hz}]

        if action == "menu":
            # VFO ↔ 频道。没有屏幕，所以"现在在哪个模式"只能由下一次 ▲▼ 的行为
            # 体现；两次切换之间频率不变，操作员不会因为按错键就丢掉频点。
            self.channel_mode = not self.channel_mode
            return []

        if action == "home":
            # 一键 CQ：按一下开始呼叫；窗口内再按一次是中止（CQ_ABORT_WINDOW_S）。
            if self.cq_at is not None and now - self.cq_at < CQ_ABORT_WINDOW_S:
                self.cq_at = None
                return [{"type": "set", "field": "cq", "value": False}]
            self.cq_at = now
            return [{"type": "set", "field": "cq", "value": True}]

        if action == "band_next":
            if not self.bands:
                return []
            return self._cycle_band()

        if action == "cancel":
            self.numeric.clear()
            return []

        if action == "freq_up" or action == "freq_down":
            if self.numeric.pending:
                # the dial keys also cancel a half-typed frequency
                self.numeric.clear()
            if self.channel_mode and self.channels:
                return self._step_channel(action == "freq_up")
            if action == "freq_up":
                self.freq_hz = step_freq(self.freq_hz, self.step_hz, True)
            else:
                self.freq_hz = step_freq(self.freq_hz, self.step_hz, False)
            return [{"type": "set", "field": "freq", "value": self.freq_hz}]

        if action in ("step_up", "step_down"):
            self.step_index = clamp_step(self.step_index + (1 if action == "step_up" else -1))
            return []

        if action in ("band_up", "band_down"):
            self.numeric.clear()
            self.freq_hz, mode = next_channel(self.freq_hz, action == "band_up")
            if mode:
                self.mode = mode
                # frequency first, then mode: the radio may re-pick a mode when
                # it changes band, and the preset's mode must be the last word.
                return [{"type": "set", "field": "freq", "value": self.freq_hz},
                        {"type": "set", "field": "mode", "value": mode}]
            return [{"type": "set", "field": "freq", "value": self.freq_hz}]

        if action == "mode_next":
            self.mode = next_mode(self.mode, self.modes)
            return [{"type": "set", "field": "mode", "value": self.mode}]

        if action in ("vol_up", "vol_down"):
            if self.af_gain_supported:
                delta = VOLUME_STEP_PCT if action == "vol_up" else -VOLUME_STEP_PCT
                self.volume = max(0, min(100, self.volume + delta))
                return [{"type": "set", "field": "af_gain",
                         "value": gain_from_volume(self.volume)}]
            # No radio-side AF gain: the same key steps the box's RX level.
            action = "level_up" if action == "vol_up" else "level_down"

        if action in ("level_up", "level_down"):
            lo, hi = RX_DB_LIMITS
            delta = LEVEL_STEP_DB if action == "level_up" else -LEVEL_STEP_DB
            self.rx_db = max(lo, min(hi, round(self.rx_db + delta, 1)))
            return [{"type": "set", "field": "rx_db", "value": self.rx_db}]

        return []

    def release_ptt(self) -> list[dict]:
        """Whatever happens, the radio must be told to stop transmitting."""
        # 掉线/退出时把"按着"的记忆一并清掉：重连后 1.5 s 内的第一次按下不该
        # 被当成固件连发丢掉（丢一次发射不危险，但没必要）。
        self._held.pop("ptt", None)
        if not self.ptt_held:
            return []
        self.ptt_held = False
        return [{"type": "set", "field": "ptt", "value": False}]


# ── I/O layer ───────────────────────────────────────────────────────────────
def _event_node(block: str) -> str:
    for line in block.splitlines():
        if line.startswith("H: Handlers="):
            for token in line.split("=", 1)[1].split():
                if token.startswith("event"):
                    return token
    return ""


def _event_bits(block: str) -> int:
    """The ``B: EV=`` bitmask (``EV_KEY`` is 0x02) — 0 when unreadable."""
    for line in block.splitlines():
        if line.startswith("B: EV="):
            try:
                return int(line.split("=", 1)[1].strip(), 16)
            except ValueError:
                return 0
    return 0


def find_device(match: str = "Voice_Remote",
                proc_path: str = "/proc/bus/input/devices") -> Optional[str]:
    """Return /dev/input/eventN of the first device whose name matches.

    A name is not the only way to find a remote. Every BLE HID remote bluez
    builds lands on the **Bluetooth** bus (``I: Bus=0005``) with key events, and
    its name belongs to the vendor, not to us (a Xiaomi one shows up as
    "Xiaomi Bluetooth Remote 2 Keyboard"). So when nothing matches the
    configured name, take the first Bluetooth device that reports keys: that is
    what lets an arbitrary remote drive the radio without editing the default,
    while ``MRRC_LOCALCTL_DEVICE`` still pins an exact node and
    ``MRRC_LOCALCTL_MATCH`` an exact name.

    The name match stays first on purpose: a box with a paired BT keyboard and a
    remote next to each other must keep reading the remote it was configured
    for. Multi-transport reports (a USB receiver that also claims Bluetooth, a
    C-Media codec exposing a phantom keyboard on Bus=0003) never qualify for the
    fallback.
    """
    try:
        with open(proc_path, "r", encoding="utf-8", errors="replace") as fh:
            blocks = fh.read().split("\n\n")
    except OSError:
        return None
    fallback = ""
    name_hit = ""
    for block in blocks:
        node = _event_node(block)
        if not node:
            continue
        if match.lower() in block.lower():
            name_hit = name_hit or node      # 名字命中优先，与文件里的先后无关
        elif (not fallback and "I: Bus=0005" in block
                and _event_bits(block) & 0x02):
            fallback = node
    node = name_hit or fallback
    return f"/dev/input/{node}" if node else None


class LocalCtl:
    """Reads the remote and drives the radio over the local control socket.

    The socket is a 0600 Unix socket owned by the same user as the server: the
    operating system's permission *is* the authentication.  A physical button
    has no web password, and giving it one (a login per reconnect) tripped the
    server's five-logins-per-five-minutes guard instead of doing anything.
    """

    def __init__(self, controller: Controller, device: Optional[str] = None,
                 match: str = "Voice_Remote", socket_path: Optional[str] = None):
        self.controller = controller
        self.device = device
        self.match = match
        self.socket_path = socket_path or os.environ.get(
            "MRRC_LOCALCTL_SOCKET", "/run/mrrc/localctl.sock")
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._stop = asyncio.Event()

    @property
    def keymap(self) -> KeyMap:
        return self.controller.keymap

    # -- control socket -----------------------------------------------------
    async def _connect(self) -> bool:
        if self._writer is not None and not self._writer.is_closing():
            return True
        try:
            self._reader, self._writer = await asyncio.open_unix_connection(self.socket_path)
        except OSError as exc:
            log.debug("control socket %r unavailable: %s", self.socket_path, exc)
            return False
        log.info("control socket connected: %s", self.socket_path)
        try:
            line = await asyncio.wait_for(self._reader.readline(), timeout=5)
        except (asyncio.TimeoutError, OSError):
            return True
        if line:
            try:
                self._absorb(json.loads(line.decode()))
            except (ValueError, UnicodeDecodeError):
                pass
        return True

    def _absorb(self, msg: dict) -> None:
        """Follow the radio's own state so a step starts from where it is.

        两处会用到：连接时的那行 `state`（带波段表和频道表），以及**每一条**命令的
        回复（带电台真实的频率/模式）。后者不是装饰 —— 换波段时波段栈会自己挑模式，
        频道回放的顺序也由服务端决定，遥控器的游标只有跟着真值走才不会在下一步
        从错的起点算起（2026-11-14：小米遥控器没有屏幕，没有别的地方能看到真值）。
        """
        if not isinstance(msg, dict):
            return
        # 只认两种：连接时那行 `state`，以及**命令回复**（没有 type 字段）。
        # 别的 type 里带的 freq 是别人的字段，不是这一台电台的当前值 —— 猜错一次
        # 就会让下一步从错的起点算起（`{"type":"other","freq":1}` 这条测试）。
        if msg.get("type") not in (None, "state"):
            self._absorb_gain(msg)
            return
        freq = msg.get("freq")
        if isinstance(freq, int) and not isinstance(freq, bool) and freq > 0:
            self.controller.freq_hz = freq
        mode = msg.get("mode")
        if isinstance(mode, str) and mode:
            self.controller.mode = mode.upper()
        bands = msg.get("bands")
        if isinstance(bands, list):
            table = []
            for band in bands:
                if not isinstance(band, dict):
                    continue
                name, hz = band.get("name"), band.get("freq")
                if isinstance(name, str) and isinstance(hz, int):
                    table.append((name, hz))
            if table:
                self.controller.bands = tuple(table)
        channels = msg.get("channels")
        if isinstance(channels, list):
            table = []
            for channel in channels:
                if not isinstance(channel, dict):
                    continue
                slot, hz = channel.get("index"), channel.get("freq")
                if not isinstance(slot, int) or not isinstance(hz, int):
                    continue
                table.append((slot, hz, str(channel.get("mode") or "").upper()))
            self.controller.channels = tuple(table)
        self._absorb_gain(msg)

    def _absorb_gain(self, msg: dict) -> None:
        """Adopt the radio's AF gain and the box's RX level from any reply."""
        gain = msg.get("af_gain")
        # bool is an int in Python; a stray ``true`` must not look like 1.
        if isinstance(gain, int) and not isinstance(gain, bool):
            self.controller.volume = volume_from_gain(gain)
        rx_db = msg.get("rx_db")
        if isinstance(rx_db, (int, float)) and not isinstance(rx_db, bool):
            lo, hi = RX_DB_LIMITS
            self.controller.rx_db = max(lo, min(hi, float(rx_db)))
        supported = msg.get("af_gain_supported")
        if isinstance(supported, bool):
            self.controller.af_gain_supported = supported

    def _drop(self) -> None:
        writer, self._writer, self._reader = self._writer, None, None
        if writer is not None:
            try:
                writer.close()
            except Exception:                           # noqa: BLE001
                pass

    async def send(self, messages: Iterable[dict]) -> None:
        for msg in messages:
            if not await self._connect():
                return                    # server not up yet; keep reading keys
            line = json.dumps({"field": msg.get("field"), "value": msg.get("value")})
            try:
                self._writer.write(line.encode() + b"\n")
                await self._writer.drain()
                reply = await asyncio.wait_for(self._reader.readline(), timeout=5)
            except Exception as exc:                    # noqa: BLE001 - drop the link
                log.warning("control socket send failed: %s", exc)
                self._drop()
                return
            if reply:
                try:
                    answer = json.loads(reply.decode())
                except (ValueError, UnicodeDecodeError):
                    continue
                if not answer.get("ok", True):
                    log.info("%s refused: %s", msg.get("field"), answer.get("error"))
                # The reply carries the radio's AF gain **and** its real
                # frequency/mode: a knob turned by hand (or a band switch that
                # picked its own mode) must not leave the next key press
                # stepping from a stale cursor.
                self._absorb(answer)

    # -- input -------------------------------------------------------------
    async def _pump(self) -> None:
        """Read events forever, re-opening the node when the remote reconnects."""
        while not self._stop.is_set():
            path = self.device or find_device(self.match)
            if not path or not os.path.exists(path):
                await asyncio.sleep(2)
                continue
            log.info("reading %s", path)
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            except OSError as exc:
                log.warning("open %s: %s", path, exc)
                await asyncio.sleep(2)
                continue
            try:
                await self._read_loop(fd)
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass
            await asyncio.sleep(1)

    async def _read_loop(self, fd: int) -> None:
        loop = asyncio.get_running_loop()
        scan: Optional[int] = None
        while not self._stop.is_set():
            try:
                blob = await loop.run_in_executor(None, os.read, fd, INPUT_EVENT.size * 64)
            except BlockingIOError:
                await asyncio.sleep(0.02)
                continue
            except OSError:
                return                     # device gone
            if not blob:
                await asyncio.sleep(0.02)
                continue
            for ev in parse_events(blob):
                if ev.type == EV_MSC and ev.code == MSC_SCAN:
                    scan = ev.value
                    continue
                if ev.type != EV_KEY:
                    continue
                ids = key_ids(scan, ev.code)
                scan = None
                pressed = ev.value == KEY_PRESS
                repeat = ev.value == KEY_REPEAT
                action = self.controller.keymap.action_for(ids)
                msgs = self.controller.on_key(ids, pressed, repeat=repeat)
                # `menu` 不产生命令，但它改变**模式**（VFO ↔ 频道）——遥控器没有
                # 屏幕，这一行是唯一能看出"操作员到底在哪个模式"的地方。
                if msgs or action == "menu" or (repeat and action):
                    log.info("key %s → %s %s%s", ids[0], action or "-", msgs,
                             " channel_mode=%s" % self.controller.channel_mode
                             if action == "menu" else "")
                await self.send(msgs)

    # -- lifecycle ---------------------------------------------------------
    def request_stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        pump = asyncio.create_task(self._pump())
        try:
            await pump
        finally:
            pump.cancel()
            await self.send(self.controller.release_ptt())
            self._drop()


def main(argv: Optional[list[str]] = None) -> int:
    logging.basicConfig(
        level=os.environ.get("MRRC_LOCALCTL_LOGLEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    keymap = KeyMap.load(os.environ.get("MRRC_LOCALCTL_KEYMAP"))
    controller = Controller(keymap=keymap)
    step = os.environ.get("MRRC_LOCALCTL_STEP")
    if step:
        try:
            controller.step_index = min(range(len(STEP_LADDER_HZ)),
                                        key=lambda i: abs(STEP_LADDER_HZ[i] - int(step)))
        except ValueError:
            log.warning("MRRC_LOCALCTL_STEP=%r is not a number", step)

    ctl = LocalCtl(
        controller,
        device=os.environ.get("MRRC_LOCALCTL_DEVICE") or None,
        match=os.environ.get("MRRC_LOCALCTL_MATCH", "Voice_Remote"),
    )

    async def _runner() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, ctl.request_stop)
            except NotImplementedError:                 # non-unix
                pass
        await ctl.run()

    try:
        asyncio.run(_runner())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
