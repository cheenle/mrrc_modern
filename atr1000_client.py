"""
ATR-1000 Antenna Tuner WebSocket Client (asyncio-native)
=========================================================
Asyncio port of the threaded reference proxy (mrrc/atr1000_proxy.py V5.6.0).
Talks to the ATR-1000 device over a binary WebSocket protocol:

  Frame: [0xFF, CMD, LEN, DATA...]
    SYNC        cmd=1  [FF 01 00]                  — poll / wake
    METER       cmd=2  len>=8                      — power / SWR (pushed by device in TX)
    TUNE_STATUS cmd=3  len>=4                      — tuning flag
    TUNE_MODE   cmd=4  [FF 04 01 mode]             — 0=reset 1=memory 2=full 3=fine
    RELAY       cmd=5  len>=7 (11 for µH/pF)       — network/L/C state
    Set relay:  [FF 05 03 sw ind cap]              — sw 0=LC 1=CL

Ported hardware behavior:
  - METER SWR is little-endian <H at data[4:6]: >=100 → /100, 1-99 integer, 0 → 1.0;
    power is <H at data[6:8] watts.
  - RELAY empirical offsets: sw=data[3], ind=data[4], cap=data[5];
    len>=11 → <H data[7:9]/100 µH, <H data[9:11] pF.
  - Device does NOT reliably send TUNE_STATUS=0 → clear `tuning` on relay
    stable >5s, same-relay confirm >1.5s, 45s hard timeout, or TX stop.
  - Device drops the connection ~hourly → proactive refresh at 55 min when
    not in TX. Reconnect with 5s retry on any drop.
  - During TX the device pushes METER itself: send NO SYNC; watchdog SYNC
    after 15s silence, auto-exit TX state after 60s silence.
  - Learning: stable-window buffer (4 samples, power>=5W, 1.0<=SWR<=1.8,
    spread<=0.08, median SWR), 1.0s ignore window after TX start / relay
    change, tiered dedup (worse → skip, unchanged → 5s cooldown,
    improved → immediate).

All device I/O happens in the background worker task; notify_freq()/notify_tx()
are synchronous and safe to call from any async context.
"""
import asyncio
import logging
import struct
import time
from contextlib import suppress
from typing import TYPE_CHECKING, Callable, Optional

import websockets

if TYPE_CHECKING:
    from atr1000_tuner import TunerStorage

try:
    from atr1000_tuner import get_storage
except ImportError:  # sibling module may not be importable yet
    get_storage = None

logger = logging.getLogger("mrrc.atr1000")

# ── ATR-1000 command constants ────────────────────────────────────
SCMD_FLAG = 0xFF
SCMD_SYNC = 1
SCMD_METER_STATUS = 2      # meter status (power, SWR)
SCMD_TUNE_STATUS = 3       # tune status
SCMD_TUNE_MODE = 4         # tune mode
SCMD_RELAY_STATUS = 5      # relay status (LC/CL, inductor, capacitor)
SCMD_MEMORY_STATUS = 6     # memory status
SCMD_MEMORY_INFO = 7       # memory info

# ── Stable-window learning parameters ─────────────────────────────
LEARN_WINDOW_SIZE = 4        # consecutive stable samples required
LEARN_SWR_STABILITY = 0.08   # max in-window SWR spread
LEARN_MIN_POWER = 3          # minimum power (W) — QRP friendly; idle reads ~1-2 W
LEARN_IGNORE_WINDOW = 1.0    # ignore time after TX start / relay change (s)
LEARN_SWR_MIN = 1.0          # learnable SWR lower bound
LEARN_SWR_MAX = 1.8          # learnable SWR upper bound
LEARN_DEDUP_COOLDOWN = 5.0   # unchanged-SWR relearn cooldown (s)
LEARN_FREQ_STEP = 1000       # window resets on freq change beyond this (Hz)

# ── High-SWR auto-retune parameters (sibling mrrc V5.8.0/V5.8.5) ───
SWR_RETUNE_THRESHOLD = 2.0        # strictly greater → too high
SWR_RETUNE_MIN_POWER = 5          # W measured power proving "really transmitting"
SWR_RETUNE_DEBOUNCE = 1.5         # s continuously above the threshold
SWR_RETUNE_COOLDOWN = 30.0        # s between two auto tunes
SWR_RETUNE_MAX_FAILS = 3          # consecutive no-improvement tunes per frequency
SWR_RETUNE_COMPARE_SETTLE = 0.8   # s after tuning clears before comparing SWR
SWR_RETUNE_IMPROVED = 0.02        # minimum improvement required to write back

# ── Connection / polling parameters ───────────────────────────────
RECONNECT_DELAY = 5.0        # retry delay after connection drop (s)
REFRESH_THRESHOLD = 3300.0   # proactive refresh at 55 min (device drops ~hourly)
POLL_INTERVAL_IDLE = 600.0   # SYNC interval with no clients (s)
POLL_INTERVAL_ACTIVE = 300.0  # SYNC interval with active clients (s)
TX_WATCHDOG_SILENCE = 15.0   # TX: send one watchdog SYNC after this silence (s)
TX_AUTO_EXIT_SILENCE = 60.0  # TX: auto-exit TX state after this silence (s)

# ── Relay throttle / tuning-clear heuristics ──────────────────────
RELAY_MIN_INTERVAL = 5.0     # min interval for identical relay params (s)
TUNE_STABLE_CLEAR = 5.0      # clear tuning when relay stable this long (s)
TUNE_CONFIRM_CLEAR = 1.5     # clear tuning on same-relay confirm (s)
TUNE_HARD_TIMEOUT = 45.0     # absolute tuning timeout (s)


# ── Pure frame helpers (unit-testable, no socket) ─────────────────
def build_sync_frame() -> bytes:
    """SYNC poll frame: [FF 01 00]."""
    return bytes([SCMD_FLAG, SCMD_SYNC, 0])


def build_set_relay_frame(sw: int, ind: int, cap: int) -> bytes:
    """Set-relay frame: [FF 05 03 sw ind cap] (sw 0=LC, 1=CL)."""
    return bytes([SCMD_FLAG, SCMD_RELAY_STATUS, 3, sw, ind, cap])


def build_tune_frame(mode: int) -> bytes:
    """Start-tune frame: [FF 04 01 mode] (0=reset 1=memory 2=full 3=fine)."""
    return bytes([SCMD_FLAG, SCMD_TUNE_MODE, 1, mode])


class LearningBuffer:
    """Stable-window learning buffer (ported from reference V5.6.0).

    Does not learn from every METER packet; waits for N consecutive samples
    with identical relay params and stable SWR before committing. Prevents:
      - TX-start transient SWR=1.00 false-good values
      - mismatched SWR at the instant of relay switching
      - single-point SWR jitter binding wrong params
    """

    def __init__(self, window_size: int = LEARN_WINDOW_SIZE,
                 swr_stability: float = LEARN_SWR_STABILITY,
                 min_power: float = LEARN_MIN_POWER,
                 swr_min: float = LEARN_SWR_MIN,
                 swr_max: float = LEARN_SWR_MAX):
        self.window_size = window_size
        self.swr_stability = swr_stability
        self.min_power = min_power
        self.swr_min = swr_min
        self.swr_max = swr_max
        self.samples: list = []          # [(power, swr), ...]
        self.current_relay: Optional[tuple] = None  # (sw, ind, cap)
        self.current_freq: int = 0

    def set_relay(self, sw: int, ind: int, cap: int) -> None:
        """Relay params changed — clear the window and re-accumulate."""
        new_relay = (sw, ind, cap)
        if new_relay != self.current_relay:
            self.current_relay = new_relay
            self.samples = []

    def set_freq(self, freq: int) -> None:
        """Significant freq change (>1kHz difference) clears the window."""
        if abs(freq - self.current_freq) > LEARN_FREQ_STEP:
            self.current_freq = freq
            self.samples = []

    def add_sample(self, power: float, swr: float, sw: int, ind: int, cap: int):
        """Add one sample. Returns (should_learn, median_swr)."""
        # Relay params must match the current window
        if (sw, ind, cap) != self.current_relay:
            return False, None

        # Power threshold
        if power < self.min_power:
            return False, None

        # SWR range
        if swr < self.swr_min or swr > self.swr_max:
            return False, None

        # Append to window
        self.samples.append((power, swr))
        while len(self.samples) > self.window_size:
            self.samples.pop(0)

        # Window not full yet
        if len(self.samples) < self.window_size:
            return False, None

        # SWR stability check
        swrs = [s[1] for s in self.samples]
        if max(swrs) - min(swrs) > self.swr_stability:
            return False, None

        # Median SWR
        sorted_swrs = sorted(swrs)
        n = len(sorted_swrs)
        if n % 2 == 0:
            median_swr = (sorted_swrs[n // 2 - 1] + sorted_swrs[n // 2]) / 2.0
        else:
            median_swr = float(sorted_swrs[n // 2])

        return True, median_swr

    def reset(self) -> None:
        """TX start/stop resets the sample window (keeps relay/freq state)."""
        self.samples = []


class ATR1000Client:
    """Asyncio WebSocket client for the ATR-1000 antenna tuner.

    Manages one background worker task: connect → receive loop → on drop,
    retry after RECONNECT_DELAY. All exceptions are contained (log + retry).
    Device I/O happens only in the worker task (and the async public write
    methods, which run on the same event loop).
    """

    def __init__(self, host: str, port: int, storage: Optional["TunerStorage"] = None):
        self.host = host
        self.port = port
        self._storage = storage

        # Optional sync callable invoked with read_state() on state changes;
        # the caller (server) schedules broadcasts from it.
        self.on_change: Optional[Callable[[dict], None]] = None
        # Optional sync callable invoked with an auto-tune event dict
        # {"phase": "auto_start"|"auto_success"|"auto_no_improve"|
        #  "auto_timeout"|"auto_aborted"|"auto_giveup", "freq", "swr_before",
        #  "swr_after", "attempt", "message"}; the server broadcasts it to
        # /WSatr1000 clients as atrTuneResult (auto=true). Never raises out.
        self.on_tune_event: Optional[Callable[[dict], None]] = None
        # Server-settable client count → selects idle/active SYNC interval.
        self.client_count: int = 0

        # Cached state (mirrors read_state())
        self._connected = False
        self._power = 0
        self._swr = 1.0
        self._sw = 0            # network type: 0=LC, 1=CL
        self._ind = 0           # inductor index
        self._cap = 0           # capacitor index
        self._ind_uh = 0.0      # inductance (µH)
        self._cap_pf = 0        # capacitance (pF)
        self._freq = 0          # current frequency (Hz) — used for learning
        self._tuning = False
        self._tx = False
        self._last_update = 0.0  # time.time() of last meter/relay frame

        # Internal timestamps (monotonic)
        self._tx_started_at = 0.0
        self._relay_changed_at = 0.0
        self._tuning_started_at = 0.0
        self._tuning_relay_stable_since = 0.0
        self._last_data_time = 0.0
        self._connection_time = 0.0

        # Relay throttle state
        self._last_relay_params: Optional[tuple] = None
        self._last_relay_sent = 0.0

        # Learn dedup: (freq_khz, sw, ind, cap) → {"swr", "time"}
        self._last_learned: dict = {}

        # High-SWR auto-retune guard (design 2026-09-23-atr1000-swr-autotune)
        self._swr_high_since = 0.0        # monotonic start of the run (0 = none)
        self._swr_high_freq = 0           # frequency the run belongs to (Hz)
        self._last_retune_at = 0.0        # last auto tune (cooldown)
        self._retune_fail_count: dict = {}  # freq_khz → consecutive no-improvement
        self._pending_tune_mode = None    # worker-side request, consumed by _poll_loop
        self._auto_tune = None            # pending post-tune comparison snapshot
        self._auto_tune_compare_at = 0.0  # monotonic deadline (0 = none)

        self._learning = LearningBuffer()

        # Worker state
        self._ws = None
        self._task: Optional[asyncio.Task] = None
        self._stopping = False
        self._wake = asyncio.Event()
        self._pending_freq: Optional[int] = None

    # ── Pure parsing helpers (unit-testable, no socket) ───────────

    @staticmethod
    def _parse_frame(data: bytes) -> tuple:
        """Split a raw frame into (cmd, payload). Raises ValueError if invalid."""
        if len(data) < 3 or data[0] != SCMD_FLAG:
            raise ValueError(f"invalid ATR-1000 frame: {data!r}")
        return data[1], data[3:]

    @staticmethod
    def _decode_meter(payload: bytes) -> Optional[tuple]:
        """Decode METER payload → (swr, power). None if too short.

        Frame layout: FF 02 LEN 00 SWR_L SWR_H P_L P_H — so the payload
        carries one status byte, then little-endian <H swr_raw, <H power.
        SWR: raw>=100 → /100; 1-99 integer; 0 → 1.0 (perfect match / not ready).
        """
        if len(payload) < 5:
            return None
        swr_raw = struct.unpack('<H', payload[1:3])[0]
        power = struct.unpack('<H', payload[3:5])[0]
        if swr_raw >= 100:
            swr = swr_raw / 100.0
        elif swr_raw > 0:
            swr = float(swr_raw)
        else:
            swr = 1.0
        return swr, power

    @staticmethod
    def _decode_relay(payload: bytes) -> Optional[dict]:
        """Decode RELAY payload → {sw, ind, cap, ind_uh, cap_pf}. None if short.

        Empirical offsets: payload[0]=sw (0=LC 1=CL), [1]=ind, [2]=cap;
        len>=8 → <H payload[4:6]/100 µH, <H payload[6:8] pF.
        """
        if len(payload) < 4:
            return None
        result = {
            "sw": payload[0],
            "ind": payload[1],
            "cap": payload[2],
            "ind_uh": None,
            "cap_pf": None,
        }
        if len(payload) >= 8:
            result["ind_uh"] = struct.unpack('<H', payload[4:6])[0] / 100.0
            result["cap_pf"] = struct.unpack('<H', payload[6:8])[0]
        return result

    @staticmethod
    def _decode_tune(payload: bytes) -> Optional[bool]:
        """Decode TUNE_STATUS payload → tuning flag. None if too short."""
        if len(payload) < 1:
            return None
        return bool(payload[0])

    # ── Public API ────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the background connect/receive worker task."""
        if self._task is not None:
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="atr1000-client")

    async def stop(self) -> None:
        """Stop the worker task and close the device connection."""
        self._stopping = True
        self._wake.set()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if self._ws is not None:
            with suppress(Exception):
                await self._ws.close()
            self._ws = None
        self._connected = False

    def notify_freq(self, freq_hz: int) -> None:
        """SYNC, non-blocking: update learning frequency and schedule the
        频率联动 auto-apply (storage lookup + throttled set_relay) in the
        worker via the wake event. Safe from any async context."""
        self._freq = freq_hz
        self._learning.set_freq(freq_hz)
        self._pending_freq = freq_hz
        self._wake.set()
        self._emit_change()

    def notify_tx(self, on: bool) -> None:
        """SYNC, non-blocking: set TX state (controls SYNC suppression),
        reset the learning window, clear tuning on TX stop."""
        on = bool(on)
        if on == self._tx:
            return
        self._tx = on
        self._learning.reset()
        if on:
            self._tx_started_at = time.monotonic()
        else:
            self._tx_started_at = 0.0
            # Device doesn't reliably send TUNE_STATUS=0 — TX stop clears.
            if self._tuning:
                self._clear_tuning("TX end")
        self._wake.set()
        self._emit_change()

    async def set_relay(self, sw: int, ind: int, cap: int) -> bool:
        """Throttled relay write: immediate when params change, else minimum
        RELAY_MIN_INTERVAL between identical sends. Returns True if sent."""
        params = (sw, ind, cap)
        now = time.monotonic()
        params_changed = params != self._last_relay_params
        if not params_changed and now - self._last_relay_sent < RELAY_MIN_INTERVAL:
            logger.debug(
                "relay command throttled: SW=%d IND=%d CAP=%d (%.2fs)",
                sw, ind, cap, now - self._last_relay_sent,
            )
            return False
        await self._send(build_set_relay_frame(sw, ind, cap))
        self._last_relay_params = params
        self._last_relay_sent = now
        # User-initiated relay change resets the learning ignore window.
        self._relay_changed_at = now
        self._learning.set_relay(sw, ind, cap)
        logger.info("relay command sent: SW=%s, L=%d, C=%d",
                    "CL" if sw else "LC", ind, cap)
        return True

    async def start_tune(self, mode: int = 2) -> None:
        """Start auto-tune. mode: 0=reset, 1=memory, 2=full, 3=fine."""
        await self._send(build_tune_frame(mode))
        self._tuning = True
        self._tuning_started_at = time.monotonic()
        self._tuning_relay_stable_since = 0.0  # first RELAY frame initializes
        self._emit_change()
        logger.info("tune command sent: mode=%d", mode)

    def read_state(self) -> dict:
        """Snapshot of the cached tuner state."""
        return {
            "connected": self._connected,
            "power": self._power,
            "swr": self._swr,
            "sw": self._sw,
            "ind": self._ind,
            "cap": self._cap,
            "ind_uh": self._ind_uh,
            "cap_pf": self._cap_pf,
            "tuning": self._tuning,
            "tx": self._tx,
            "freq": self._freq,
            "last_update": self._last_update,
        }

    # ── Background worker ─────────────────────────────────────────

    async def _run(self) -> None:
        """Connect → receive loop; on drop, retry after RECONNECT_DELAY."""
        url = f"ws://{self.host}:{self.port}/"
        while not self._stopping:
            try:
                async with websockets.connect(url) as ws:
                    self._ws = ws
                    self._connected = True
                    self._connection_time = time.monotonic()
                    self._last_data_time = time.monotonic()
                    logger.info("ATR-1000 connected: %s", url)
                    self._emit_change()
                    poll_task = asyncio.create_task(self._poll_loop())
                    try:
                        async for message in ws:
                            if isinstance(message, bytes):
                                self._last_data_time = time.monotonic()
                                self._handle_frame(message)
                    finally:
                        poll_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await poll_task
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("ATR-1000 connection error: %s", e)
            finally:
                self._ws = None
                if self._connected:
                    self._connected = False
                    # Drop clears tuning (device state unknown after reconnect).
                    self._tuning = False
                    self._tuning_started_at = 0.0
                    self._tuning_relay_stable_since = 0.0
                    self._abort_auto_tune("tuner disconnected")
                    self._emit_change()
            if not self._stopping:
                logger.info("ATR-1000 reconnecting in %.0fs...", RECONNECT_DELAY)
                await asyncio.sleep(RECONNECT_DELAY)

    async def _poll_loop(self) -> None:
        """Per-connection poll/refresh/watchdog loop.

        - Initial SYNC on connect.
        - Idle: SYNC every POLL_INTERVAL_IDLE; with clients: POLL_INTERVAL_ACTIVE.
        - TX: NO SYNC (device pushes METER itself); one watchdog SYNC after
          TX_WATCHDOG_SILENCE, auto-exit TX after TX_AUTO_EXIT_SILENCE.
        - Proactive reconnect at REFRESH_THRESHOLD (55 min) when not in TX —
          the device drops connections ~hourly.
        - 45s tuning hard timeout; pending freq auto-apply (notify_freq).
        """
        await self._send_sync()
        last_poll = time.monotonic()
        watchdog_synced = False

        while True:
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=1.0)
            self._wake.clear()
            if self._stopping:
                return
            now = time.monotonic()

            # 频率联动 auto-apply scheduled by notify_freq()
            if self._pending_freq is not None:
                await self._apply_pending_freq()

            # Auto-retune: queued full-tune frame + deferred SWR comparison
            await self._flush_auto_tune(now)

            # Tuning 45s hard timeout
            if (self._tuning and self._tuning_started_at > 0
                    and now - self._tuning_started_at > TUNE_HARD_TIMEOUT):
                self._clear_tuning("45s hard timeout")

            # Proactive refresh at 55 min when NOT in TX
            if (not self._tx and self._connection_time > 0
                    and now - self._connection_time > REFRESH_THRESHOLD):
                logger.info("connection age %.0fs, proactive refresh",
                            now - self._connection_time)
                ws = self._ws
                if ws is not None:
                    await ws.close()
                return

            if self._tx:
                # TX: device pushes METER itself — never SYNC unless wedged.
                silence = now - self._last_data_time
                if silence > TX_AUTO_EXIT_SILENCE:
                    logger.warning("TX silence %.0fs, auto-exit TX state", silence)
                    self.notify_tx(False)
                    watchdog_synced = False
                elif silence > TX_WATCHDOG_SILENCE:
                    if not watchdog_synced:
                        logger.warning("TX silence %.0fs, watchdog SYNC", silence)
                        await self._send_sync()
                        watchdog_synced = True
                else:
                    watchdog_synced = False
            else:
                interval = (POLL_INTERVAL_ACTIVE if self.client_count > 0
                            else POLL_INTERVAL_IDLE)
                if now - last_poll >= interval:
                    await self._send_sync()
                    last_poll = now

    async def _apply_pending_freq(self) -> None:
        """Worker-side 频率联动: look up learned params for the pending
        frequency and apply them via the throttled set_relay."""
        freq, self._pending_freq = self._pending_freq, None
        if not freq or freq <= 0:
            return
        storage = self._get_storage()
        if storage is None:
            return
        try:
            params = storage.get_tune_params(freq)
        except Exception as e:
            logger.error("tune-params lookup failed for %d Hz: %s", freq, e)
            return
        if not params:
            return
        sw, ind, cap = params
        if (sw, ind, cap) == (self._sw, self._ind, self._cap):
            return
        try:
            if await self.set_relay(sw, ind, cap):
                logger.info("auto-tune: %.1fkHz -> %s, L=%d, C=%d",
                            freq / 1000, "CL" if sw else "LC", ind, cap)
        except Exception as e:
            logger.error("auto-tune set_relay failed for %d Hz: %s", freq, e)

    # ── Frame handling (runs in the worker task) ──────────────────

    def _handle_frame(self, data: bytes) -> None:
        """Dispatch one raw frame; all exceptions contained."""
        try:
            cmd, payload = self._parse_frame(data)
        except ValueError:
            return
        try:
            if cmd == SCMD_METER_STATUS:
                self._handle_meter(payload)
            elif cmd == SCMD_RELAY_STATUS:
                self._handle_relay(payload)
            elif cmd == SCMD_TUNE_STATUS:
                self._handle_tune(payload)
        except Exception:
            logger.exception("error handling ATR-1000 frame cmd=%d", cmd)

    def _handle_meter(self, payload: bytes) -> None:
        decoded = self._decode_meter(payload)
        if decoded is None:
            return
        swr, power = decoded
        self._swr = swr
        self._power = power
        self._last_update = time.time()
        now = time.monotonic()

        # Tuning auto-clear: relay stable >5s (device may never send TUNE_STATUS=0)
        if (self._tuning and self._tuning_relay_stable_since > 0
                and now - self._tuning_relay_stable_since > TUNE_STABLE_CLEAR):
            self._clear_tuning("relay stable >5s")

        self._emit_change()

        # High-SWR auto-retune guard (decides only; the worker sends the frame)
        self._check_swr_retune(swr, power)

        # Stable-window learning on the METER stream while transmitting.
        # Measured power — not the server-side TX signal — decides this, so a
        # transmission from the radio panel or external software learns too
        # (sibling mrrc V5.8.5 fix). _tx still drives SYNC suppression only.
        if not (not self._tuning and power >= LEARN_MIN_POWER
                and self._freq > 0):
            return
        in_ignore_window = (
            (self._tx_started_at > 0 and now - self._tx_started_at < LEARN_IGNORE_WINDOW)
            or (self._relay_changed_at > 0 and now - self._relay_changed_at < LEARN_IGNORE_WINDOW)
        )
        if in_ignore_window:
            return
        self._learning.set_freq(self._freq)
        should_learn, median_swr = self._learning.add_sample(
            power, swr, self._sw, self._ind, self._cap)
        if should_learn:
            self._maybe_learn(median_swr)

    def _handle_relay(self, payload: bytes) -> None:
        decoded = self._decode_relay(payload)
        if decoded is None:
            return
        now = time.monotonic()
        old_relay = (self._sw, self._ind, self._cap)
        self._sw = decoded["sw"]
        self._ind = decoded["ind"]
        self._cap = decoded["cap"]
        new_relay = (self._sw, self._ind, self._cap)

        # Track last actual relay change (repeat confirms don't reset it)
        if new_relay != old_relay:
            self._relay_changed_at = now

        # Tuning stability tracking
        if self._tuning:
            if new_relay != old_relay:
                self._tuning_relay_stable_since = now
            else:
                if self._tuning_relay_stable_since == 0:
                    self._tuning_relay_stable_since = now
                elif now - self._tuning_relay_stable_since > TUNE_CONFIRM_CLEAR:
                    self._clear_tuning("same-relay confirm >1.5s")

        if decoded["ind_uh"] is not None:
            self._ind_uh = decoded["ind_uh"]
            self._cap_pf = decoded["cap_pf"]

        self._last_update = time.time()
        self._learning.set_relay(self._sw, self._ind, self._cap)
        self._emit_change()

    def _handle_tune(self, payload: bytes) -> None:
        tuning = self._decode_tune(payload)
        if tuning is None:
            return
        self._tuning = tuning
        self._tuning_started_at = time.monotonic() if tuning else 0.0
        if not tuning:
            self._tuning_relay_stable_since = 0.0
            self._schedule_auto_tune_compare("tune status 0")
        self._emit_change()

    # ── Learning dedup ────────────────────────────────────────────

    def _maybe_learn(self, median_swr: float) -> None:
        """Tiered dedup then storage.learn(): worse SWR → skip, unchanged
        (±0.01) → LEARN_DEDUP_COOLDOWN, improved → immediate."""
        storage = self._get_storage()
        if storage is None:
            return
        key = (self._freq // 1000, self._sw, self._ind, self._cap)
        prev = self._last_learned.get(key)
        now = time.monotonic()

        do_learn = True
        if prev:
            swr_delta = median_swr - prev["swr"]  # negative = improved
            if swr_delta > 0.01:
                do_learn = False                # worse — old params better
            elif swr_delta >= -0.01 and now - prev["time"] < LEARN_DEDUP_COOLDOWN:
                do_learn = False                # unchanged — cooldown

        if not do_learn:
            return
        try:
            if storage.learn(freq=self._freq, sw=self._sw, ind=self._ind,
                             cap=self._cap, swr=median_swr):
                self._last_learned[key] = {"swr": median_swr, "time": now}
                logger.info("learned: %.1fkHz SWR=%.2f, %s, L=%d, C=%d",
                            self._freq / 1000, median_swr,
                            "CL" if self._sw else "LC", self._ind, self._cap)
        except Exception as e:
            logger.error("failed to learn tuner params: %s", e)

    # ── High-SWR auto-retune guard ────────────────────────────────

    def _check_swr_retune(self, swr: float, power: float) -> None:
        """Queue one full tune when the operator transmits into a high SWR.

        Runs on the METER stream and only decides — the frame itself is sent
        by the worker (`_flush_auto_tune`), so all device I/O stays in one
        place. The operator's own transmission is the precondition: below
        SWR_RETUNE_MIN_POWER measured power nothing happens, which is why
        this guard can never key the radio. Ported from the sibling project
        (mrrc V5.8.0 `check_swr_retune`), with instance state instead of
        module globals so no locking is needed.
        """
        now = time.monotonic()
        if self._tuning or power < SWR_RETUNE_MIN_POWER or self._freq <= 0:
            self._swr_high_since = 0.0
            return
        if swr <= SWR_RETUNE_THRESHOLD:
            # Matched again — this frequency is eligible for a retune later.
            self._swr_high_since = 0.0
            self._retune_fail_count.pop(self._freq // 1000, None)
            return
        if (self._swr_high_freq == 0
                or abs(self._freq - self._swr_high_freq) > LEARN_FREQ_STEP):
            self._swr_high_since = 0.0        # QSY (or first sight) — new run
        self._swr_high_freq = self._freq
        if (self._relay_changed_at > 0
                and now - self._relay_changed_at < LEARN_IGNORE_WINDOW):
            self._swr_high_since = 0.0        # relays just moved, not settled
            return

        key = self._freq // 1000
        if self._retune_fail_count.get(key, 0) >= SWR_RETUNE_MAX_FAILS:
            if self._swr_high_since == 0.0:
                # Announce once per run, then stay quiet until the frequency
                # changes or the SWR recovers.
                self._swr_high_since = now
                logger.info(
                    "SWR %.2f still high, auto tune given up after %d tries "
                    "(%.1f kHz)", swr, SWR_RETUNE_MAX_FAILS, self._freq / 1000)
                self._emit_tune_event({
                    "phase": "auto_giveup", "freq": self._freq,
                    "swr_before": round(swr, 2), "swr_after": None,
                    "attempt": self._retune_fail_count[key], "message": "",
                })
            return

        if self._swr_high_since == 0.0:
            self._swr_high_since = now
        if (now - self._swr_high_since < SWR_RETUNE_DEBOUNCE
                or now - self._last_retune_at < SWR_RETUNE_COOLDOWN):
            return

        # Fire: queue ONE full-tune frame for the worker.
        self._retune_fail_count[key] = self._retune_fail_count.get(key, 0) + 1
        self._last_retune_at = now
        self._swr_high_since = 0.0
        self._auto_tune = {
            "freq": self._freq,
            "swr_before": swr,
            "relays": (self._sw, self._ind, self._cap),
            "count": self._retune_fail_count[key],
            "reason": "",
        }
        self._pending_tune_mode = 2
        self._wake.set()
        logger.info("SWR %.2f > %.1f at %.1f kHz, auto full tune (try %d)",
                    swr, SWR_RETUNE_THRESHOLD, self._freq / 1000,
                    self._auto_tune["count"])
        self._emit_tune_event({
            "phase": "auto_start", "freq": self._freq,
            "swr_before": round(swr, 2), "swr_after": None,
            "attempt": self._auto_tune["count"], "message": "",
        })

    def _emit_tune_event(self, event: dict) -> None:
        """Dispatch an auto-tune phase to the server callback (contained)."""
        cb = self.on_tune_event
        if cb is None:
            return
        try:
            cb(event)
        except Exception:
            logger.exception("on_tune_event callback failed")

    async def _flush_auto_tune(self, now: float) -> None:
        """Worker-side auto-tune step: send a queued full tune, then run the
        deferred post-tune comparison once its settle time has passed."""
        mode, self._pending_tune_mode = self._pending_tune_mode, None
        if mode is not None:
            try:
                await self.start_tune(mode)
            except Exception as e:
                logger.warning("auto tune send failed: %s", e)
                self._abort_auto_tune("send failed")
        if (self._auto_tune is not None and self._auto_tune_compare_at > 0
                and now >= self._auto_tune_compare_at):
            self._finish_auto_tune()

    def _schedule_auto_tune_compare(self, reason: str) -> None:
        """The first tuning-clear after an auto tune arms the deferred compare
        (later clears keep the original reason)."""
        if self._auto_tune is None or self._auto_tune_compare_at > 0:
            return
        self._auto_tune["reason"] = reason
        self._auto_tune_compare_at = (
            time.monotonic() + SWR_RETUNE_COMPARE_SETTLE)

    def _finish_auto_tune(self) -> None:
        """Compare SWR after an auto tune and write the relays back to the
        learned store when the match improved and landed inside the learn
        gate. The relays are never rolled back (design decision 3)."""
        snap, self._auto_tune = self._auto_tune, None
        self._auto_tune_compare_at = 0.0
        if snap is None:
            return
        final = self._swr
        improved = snap["swr_before"] - final
        if snap.get("reason") == "45s hard timeout":
            phase, message = "auto_timeout", "tune timeout (45s)"
        elif improved >= SWR_RETUNE_IMPROVED and 0 < final <= LEARN_SWR_MAX:
            phase, message = "auto_success", ""
            storage = self._get_storage()
            if storage is not None:
                try:
                    storage.learn(freq=snap["freq"], sw=self._sw,
                                  ind=self._ind, cap=self._cap,
                                  swr=final, force_update=True)
                except Exception as e:
                    logger.warning("auto-tune learn failed: %s", e)
        else:
            phase, message = "auto_no_improve", ""
        logger.info(
            "ATR-1000 auto tune %s: SWR %.2f → %.2f at %.1f kHz (try %d)",
            phase, snap["swr_before"], final, snap["freq"] / 1000, snap["count"])
        self._emit_tune_event({
            "phase": phase, "freq": snap["freq"], "message": message,
            "swr_before": round(snap["swr_before"], 2),
            "swr_after": round(final, 2), "attempt": snap["count"],
        })

    def _abort_auto_tune(self, reason: str) -> None:
        """Discard a pending auto-tune comparison (device gone / send failed)
        and close the UI's in-progress state with a terminal event."""
        snap, self._auto_tune = self._auto_tune, None
        self._auto_tune_compare_at = 0.0
        if snap is None:
            return
        logger.info("ATR-1000 auto tune aborted (%s) at %.1f kHz",
                    reason, snap["freq"] / 1000)
        self._emit_tune_event({
            "phase": "auto_aborted", "freq": snap["freq"], "message": reason,
            "swr_before": round(snap["swr_before"], 2), "swr_after": None,
            "attempt": snap["count"],
        })

    # ── Internals ─────────────────────────────────────────────────

    def _get_storage(self):
        """Explicit storage, else the sibling module's global storage."""
        if self._storage is not None:
            return self._storage
        if get_storage is None:
            return None
        try:
            return get_storage()
        except Exception as e:
            logger.error("get_storage() failed: %s", e)
            return None

    async def _send(self, data: bytes) -> None:
        """Write one binary frame. Raises ConnectionError when disconnected."""
        ws = self._ws
        if ws is None:
            raise ConnectionError("ATR-1000 not connected")
        await ws.send(data)

    async def _send_sync(self) -> None:
        try:
            await self._send(build_sync_frame())
        except Exception as e:
            logger.debug("SYNC send failed: %s", e)

    def _clear_tuning(self, reason: str) -> None:
        self._tuning = False
        self._tuning_started_at = 0.0
        self._tuning_relay_stable_since = 0.0
        self._schedule_auto_tune_compare(reason)
        logger.info("tuning cleared (%s)", reason)

    def _emit_change(self) -> None:
        cb = self.on_change
        if cb is None:
            return
        try:
            cb(self.read_state())
        except Exception:
            logger.exception("on_change callback failed")
