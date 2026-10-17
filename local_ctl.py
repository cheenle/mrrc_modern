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
* **I/O** (``LocalCtl.run``): open the evdev node, read events, keep a
  WebSocket to the server's ``/WSradio`` and speak the browser protocol
  (``{"type":"set","field":...,"value":...}``).

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
import ssl
import struct
import sys
import time
from dataclasses import dataclass, field
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
    "u:0x70050": "step_down",  # ◀  (coarser step)
    "u:0x7004f": "step_up",    # ▶  (finer step)
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


def clamp_step(index: int) -> int:
    return max(0, min(len(STEP_LADDER_HZ) - 1, index))


def step_freq(freq_hz: int, step_hz: int, up: bool) -> int:
    """Step the dial, wrapping inside the band that contains the current spot."""
    target = freq_hz + (step_hz if up else -step_hz)
    for low, high in BAND_EDGES_HZ:
        if low <= freq_hz <= high:
            if target > high:
                return low
            if target < low:
                return high
            return target
    return max(30_000, min(60_000_000, target))


def next_band(freq_hz: int, up: bool, edges=BAND_EDGES_HZ) -> int:
    """Jump to the same relative offset in the neighbouring band."""
    if not edges:
        return freq_hz
    for idx, (low, high) in enumerate(edges):
        if low <= freq_hz <= high:
            pos = (freq_hz - low) / max(1, high - low)
            nxt = (idx + (1 if up else -1)) % len(edges)
            nlow, nhigh = edges[nxt]
            return int(nlow + pos * (nhigh - nlow))
    return edges[0][0] if up else edges[-1][1]


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
    volume: int = 50
    numeric: NumericEntry = field(default_factory=NumericEntry)
    modes: tuple[str, ...] = MODE_CYCLE
    ptt_held: bool = False

    @property
    def step_hz(self) -> int:
        return STEP_LADDER_HZ[clamp_step(self.step_index)]

    def on_key(self, ids: Iterable[str], pressed: bool, now: Optional[float] = None) -> list[dict]:
        """Translate one key event into protocol messages (possibly none)."""
        action = self.keymap.action_for(ids)
        if not action:
            return []
        now = time.monotonic() if now is None else now

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
                return []
            self.freq_hz = hz
            return [{"type": "set", "field": "freq", "value": hz}]

        if action == "cancel":
            self.numeric.clear()
            return []

        if action == "freq_up" or action == "freq_down":
            if self.numeric.pending:
                # the dial keys also cancel a half-typed frequency
                self.numeric.clear()
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
            self.freq_hz = next_band(self.freq_hz, action == "band_up")
            return [{"type": "set", "field": "freq", "value": self.freq_hz}]

        if action == "mode_next":
            self.mode = next_mode(self.mode, self.modes)
            return [{"type": "set", "field": "mode", "value": self.mode}]

        if action in ("vol_up", "vol_down"):
            delta = 5 if action == "vol_up" else -5
            self.volume = max(0, min(100, self.volume + delta))
            return [{"type": "set", "field": "af_gain", "value": self.volume}]

        return []

    def release_ptt(self) -> list[dict]:
        """Whatever happens, the radio must be told to stop transmitting."""
        if not self.ptt_held:
            return []
        self.ptt_held = False
        return [{"type": "set", "field": "ptt", "value": False}]


# ── I/O layer ───────────────────────────────────────────────────────────────
def find_device(match: str = "Voice_Remote",
                proc_path: str = "/proc/bus/input/devices") -> Optional[str]:
    """Return /dev/input/eventN of the first device whose name matches."""
    try:
        with open(proc_path, "r", encoding="utf-8", errors="replace") as fh:
            blocks = fh.read().split("\n\n")
    except OSError:
        return None
    for block in blocks:
        if match.lower() not in block.lower():
            continue
        node = ""
        for line in block.splitlines():
            if line.startswith("H: Handlers="):
                for token in line.split("=", 1)[1].split():
                    if token.startswith("event"):
                        node = token
                        break
        if node:
            return f"/dev/input/{node}"
    return None


class LocalCtl:
    """Reads the remote and keeps the radio in step over /WSradio."""

    def __init__(self, controller: Controller, device: Optional[str] = None,
                 match: str = "Voice_Remote", host: str = "127.0.0.1",
                 port: int = 8888, password: str = ""):
        self.controller = controller
        self.device = device
        self.match = match
        self.host = host
        self.port = port or int(os.environ.get("MRRC_WEB_PORT", "8888") or 8888)
        self.password = password or os.environ.get("MRRC_WEB_PASSWORD", "")
        self._ws = None
        self._stop = asyncio.Event()

    @property
    def keymap(self) -> KeyMap:
        return self.controller.keymap

    # -- server link --------------------------------------------------------
    def _ssl(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE   # loopback, self-signed by design
        return ctx

    async def _token(self) -> Optional[str]:
        import urllib.request
        url = f"https://{self.host}:{self.port}/api/auth/login"
        body = json.dumps({"password": self.password}).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, context=self._ssl(), timeout=8) as resp:
                payload = json.loads(resp.read().decode())
            return payload.get("token")
        except Exception as exc:                        # noqa: BLE001 - logged
            log.error("login failed: %s", exc)
            return None

    async def _connect(self) -> None:
        import websockets
        token = await self._token()
        if not token:
            return
        url = f"wss://{self.host}:{self.port}/WSradio"
        self._ws = await websockets.connect(
            url, ssl=self._ssl(), additional_headers={"Authorization": f"Bearer {token}"},
            ping_interval=20, ping_timeout=20,
        )
        log.info("connected to %s", url)

    async def send(self, messages: Iterable[dict]) -> None:
        for msg in messages:
            if self._ws is None:
                await self._connect()
            if self._ws is None:
                return
            try:
                await self._ws.send(json.dumps(msg))
            except Exception as exc:                    # noqa: BLE001 - drop link
                log.warning("send failed: %s", exc)
                self._ws = None

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
                if ev.type != EV_KEY or ev.value == KEY_REPEAT:
                    continue
                ids = key_ids(scan, ev.code)
                scan = None
                action = self.keymap.action_for(ids)
                msgs = self.controller.on_key(ids, ev.value == KEY_PRESS)
                if action or msgs:
                    log.info("key %s → %s %s", ids[0], action or "-", msgs)
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
            if self._ws is not None:
                try:
                    await self._ws.close()
                except Exception:                       # noqa: BLE001
                    pass
                self._ws = None


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
