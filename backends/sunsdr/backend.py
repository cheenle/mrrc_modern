"""SunSDR2 DX backend
====================
``RadioBackend`` for the Expert Electronics SunSDR2 DX, wiring Tasks 1-3
(protocol/DSP/engine) into the server's backend contract:

- One ``SunSDREngine`` owns the whole device session (UDP control 50001 +
  IQ 50002, boot sequence, heartbeat, RX demod, TX pacer, telemetry).  The
  backend constructs it at startup; the audio adapter and the scope
  producer SHARE that engine instead of opening their own sessions.
- ``SunSDR2DXController`` (exposed as ``backend.cat``) implements the full
  abstract CAT surface.  Genuinely supported: connection lifecycle,
  frequency, mode, PTT, DRIVE (rf_power/drive), the combined 4-level
  ATT/preamp, filters, AGC, S-meter/telemetry and the four real IQ spans.
  Everything the protocol has no command for returns False/None explicitly
  and never appears in a poll list — no fake serial frames are sent.
- Single VFO: no split, no VFO-B, no VFO lock.  ``get_active_vfo`` answers
  "A" because that is the truth, not a feature.
- No serial port, no USB sound card: control and audio both travel the
  LAN.  ``port``/``baud_rate`` are accepted for factory compatibility and
  otherwise ignored; the host comes from the constructor or
  ``config.SUNSDR_HOST`` (Task 5 wires the env var formally).
- Not hardware-verified: PTT/TUNE are gated behind
  ``MRRC_ALLOW_UNVERIFIED_TX=1`` (AD-019 / NFR-067), decided per key-up.
  TUNE is a real 700 Hz test tone through the engine TX path — never a
  fabricated ATU — and every exit path (release, failure, bounded timeout)
  ends at ``set_ptt(False)``.

The module never imports ``server``/``radio_state``; radio-originated
events leave through the optional broadcast callback.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Optional

import numpy as np

import config
from backends.base import RadioBackend, RadioCapabilities, ScopeProducer
from backends.sunsdr.dsp import AUDIO_RATE
from backends.sunsdr.engine import SunSDREngine
from backends.sunsdr.protocol import SAMPLE_RATES
from backends.sunsdr.scope import (
    DEFAULT_SPAN_INDEX,
    SCOPE_HALF_SPANS,
    SunSDRScopeProducer,
)

logger = logging.getLogger(__name__)

MODEL_KEY = "sunsdr2dx"
DISPLAY_NAME = "Expert Electronics SunSDR2 DX"
# No hardware acceptance session yet (spec §3): everything about TX and the
# meters stays gated/unverified until the dummy-load session passes.
VERIFIED = False
DEFAULT_BAUD_RATE = 115200     # registry-derived table entry only — the
                               # radio is UDP-controlled, the baud is unused

# Span index -> the IQ sample rate that produces it (and the half-width the
# scope producer publishes).  Four real device rates, nothing interpolated.
SPAN_RATE_KEYS: dict[int, str] = {
    0: "39k", 1: "78k", 2: "156k", 3: "312k",
}

# Mode register numbers are this backend's own (the protocol has no mode
# command — mode is DSP state).  Names match the UI vocabulary.
MODE_NUMBERS: dict[str, int] = {
    "LSB": 1, "USB": 2, "CW-U": 3, "DIGI-U": 4, "AM": 5, "NFM": 6, "FM": 7,
}
UI_MODES: list[str] = ["USB", "LSB", "CW-U", "DIGI-U", "AM", "NFM", "FM"]
# UI name -> demodulator mode string ("CW"/"DIGI" are DSP names).
_DSP_MODE_NAME = {"CW-U": "CW", "DIGI-U": "DIGI"}
_DSP_TO_UI_NAME = {v: k for k, v in _DSP_MODE_NAME.items()}

# Filter-width ladders as (index, width_hz) — what the UI dropdown shows.
VOICE_FILTER_WIDTHS: list[tuple[int, int]] = [
    (0, 1800), (1, 2400), (2, 3000), (3, 3300),
]
NARROW_FILTER_WIDTHS: list[tuple[int, int]] = [
    (0, 400), (1, 800), (2, 1200),
]
NARROW_UI_MODES = ("CW-U",)
DEFAULT_FILTER_INDEX = 2          # 300 Hz..3.3 kHz — the demodulator's own
                                  # default passband
CW_FILTER_CENTER_HZ = 700.0       # CW pitch shared with the TUNE tone
SSB_FILTER_LO_HZ = 300.0

# The SET_PARAM_5 trailing word is one combined 4-level control:
# 0=-20 dB, 1=-10 dB, 2=0 dB bypass, 3=+10 dB preamp (ExpertSDR3-verified).
# It is exposed as the preamp selector (the natural fit for a single
# signed-gain control); the separate attenuator returns False.
ATT_PREAMP_LABELS = ("-20 dB", "-10 dB", "OFF", "+10 dB")

# AGC: UI/RadioState numbering (0=OFF, 1=FAST, 2=MED, 3=SLOW) vs the DSP's
# (0=OFF, 1=LONG, 2=SLOW, 3=MED, 4=FAST).
_AGC_UI_TO_DSP = {0: 0, 1: 4, 2: 3, 3: 2}
_AGC_DSP_TO_UI = {0: 0, 4: 1, 3: 2, 2: 3, 1: 3}   # LONG reported as SLOW

POWER_RATED_W = 100
METER_FULL_SCALE_V = 16.0
# S-meter raw range covers DSP dB [-120, 0]; it is passband power, NOT a
# calibrated dBm — kept honest through ``unverified_meters``.
S_METER_DB_FLOOR = -120.0
S_METER_DB_SPAN = 120.0

# TUNE: a bounded 700 Hz tone fed through the engine TX path.
TUNE_TONE_HZ = 700.0
TUNE_TONE_AMP = 0.25              # modest drive: a test tone, not a blowtorch
TUNE_FEED_INTERVAL_S = 0.019      # slightly ahead of the 20 ms frame pace
TUNE_PRIME_FRAMES = 5             # initial cushion so the pacer never starves
TUNE_MAX_S = 30.0                 # self-release bound (radio TUNE has its own
                                  # momentary semantics; the web button holds)

# One warning per process: the gate is a configuration state, not a
# per-keystroke event (a log flood would bury real failures).
_TX_GATE_WARNED = False


def _tx_allowed() -> bool:
    """Dynamic per key-up: the env opt-in re-read on every PTT/TUNE request."""
    global _TX_GATE_WARNED
    if config.ALLOW_UNVERIFIED_TX or VERIFIED:
        return True
    if not _TX_GATE_WARNED:
        _TX_GATE_WARNED = True
        logger.warning(
            "%s is not hardware-verified — transmit refused. Set "
            "MRRC_ALLOW_UNVERIFIED_TX=1 and restart to enable TX after "
            "checking the radio on a dummy load.", DISPLAY_NAME)
    return False


def _s_db_to_raw(db: float) -> int:
    return int(round(max(0.0, min(255.0,
                                 (db - S_METER_DB_FLOOR) * 255.0 / S_METER_DB_SPAN))))


def _raw_to_s_db(raw: int) -> float:
    return max(0, min(255, int(raw))) * S_METER_DB_SPAN / 255.0 + S_METER_DB_FLOOR


def _watts_to_raw(watts: float) -> int:
    return int(round(max(0.0, min(255.0, watts / POWER_RATED_W * 255.0))))


def _volts_to_raw(volts: float) -> int:
    return int(round(max(0.0, min(255.0, volts / METER_FULL_SCALE_V * 255.0))))


def _raw_to_s_unit(raw: int) -> int:
    # Same convention as the other backends: S9 = -73 dBm, 6 dB per S.
    return max(0, int((_raw_to_s_db(raw) + 127.0) // 6))


BANDS: list[dict] = [
    {"name": "160m", "start": 1_800_000, "end": 2_000_000,
     "default_freq": 1_840_000},
    {"name": "80m",  "start": 3_500_000, "end": 4_000_000,
     "default_freq": 3_573_000},
    {"name": "60m",  "start": 5_330_500, "end": 5_405_000,
     "default_freq": 5_357_000},
    {"name": "40m",  "start": 7_000_000, "end": 7_300_000,
     "default_freq": 7_074_000},
    {"name": "30m",  "start": 10_100_000, "end": 10_150_000,
     "default_freq": 10_136_000},
    {"name": "20m",  "start": 14_000_000, "end": 14_350_000,
     "default_freq": 14_074_000},
    {"name": "17m",  "start": 18_068_000, "end": 18_168_000,
     "default_freq": 18_100_000},
    {"name": "15m",  "start": 21_000_000, "end": 21_450_000,
     "default_freq": 21_074_000},
    {"name": "12m",  "start": 24_890_000, "end": 24_990_000,
     "default_freq": 24_915_000},
    {"name": "10m",  "start": 28_000_000, "end": 29_700_000,
     "default_freq": 28_074_000},
    {"name": "6m",   "start": 50_000_000, "end": 54_000_000,
     "default_freq": 50_313_000},
]


def _band_for_frequency(freq_hz: int) -> Optional[dict]:
    for band in BANDS:
        if band["start"] <= freq_hz <= band["end"]:
            return {"name": band["name"], "low": band["start"],
                    "high": band["end"]}
    return None


def _filter_hz(mode_name: str, index: int) -> Optional[int]:
    table = (NARROW_FILTER_WIDTHS if mode_name in NARROW_UI_MODES
             else VOICE_FILTER_WIDTHS)
    for idx, hz in table:
        if idx == index:
            return hz
    return None


def _passband_for(mode_ui: str, width_hz: int) -> tuple[float, float]:
    """Map a UI filter width to demodulator passband edges."""
    if mode_ui in NARROW_UI_MODES:
        lo = max(100.0, CW_FILTER_CENTER_HZ - width_hz / 2.0)
        return lo, CW_FILTER_CENTER_HZ + width_hz / 2.0
    return SSB_FILTER_LO_HZ, SSB_FILTER_LO_HZ + float(width_hz)


# Audio frame length shared by the tone generator and the feed accounting.
RX_TX_FRAME_SAMPLES = AUDIO_RATE * 20 // 1000    # 960


def _tone_frame(offset: int) -> bytes:
    """One 20 ms int16 frame of the TUNE tone at a continuous phase
    (``offset`` = samples already emitted — repeating a fixed buffer would
    click at every frame seam)."""
    n = np.arange(RX_TX_FRAME_SAMPLES, dtype=np.float64)
    phase = 2.0 * np.pi * TUNE_TONE_HZ * (offset + n) / AUDIO_RATE
    return np.clip(np.rint(TUNE_TONE_AMP * 32767.0 * np.sin(phase)),
                   -32768, 32767).astype("<i2").tobytes()


class SunSDR2DXController:
    """The CAT surface behind ``SunSDR2DXBackend.cat`` — commands become
    engine/DSP state over UDP; unsupported items refuse explicitly."""

    def __init__(self, engine: SunSDREngine):
        self._engine = engine
        self._cancel_polls = asyncio.Event()
        self._filter_index = DEFAULT_FILTER_INDEX
        self._span_index = DEFAULT_SPAN_INDEX
        self._scope_producer: Optional[SunSDRScopeProducer] = None
        self._tune_task: Optional[asyncio.Task] = None

    # ── Connection management ────────────────────────────────────

    @property
    def connected(self) -> bool:
        return self._engine.connected

    @property
    def model(self) -> str:
        return MODEL_KEY

    async def connect(self) -> bool:
        return await self._engine.start()

    async def disconnect(self) -> None:
        await self._engine.stop()

    async def reconnect_loop(self) -> bool:
        # start() is idempotent: an already-running engine answers True
        # without re-binding; a dead one re-runs the full boot sequence.
        return await self._engine.start()

    async def boot_verify(self, cat=None, timeout: float = 0.4) -> bool:
        """After power-on the device answers by simply being started."""
        return self._engine.connected

    # ── Raw command surface (no ASCII CAT exists on this radio) ──

    async def send_command(self, cmd: str, timeout: Optional[float] = None) -> Optional[str]:
        return None

    async def send_set_command(self, cmd: str) -> bool:
        return False

    async def send_priority_set_command(self, cmd: str) -> bool:
        return False

    async def query(self, cmd: str, timeout: Optional[float] = None) -> Optional[str]:
        return None

    async def set(self, cmd: str) -> bool:
        return False

    # ── Frequency / VFO (single VFO) ─────────────────────────────

    async def set_frequency(self, freq_hz: int, vfo: str = "A") -> bool:
        if str(vfo).upper() != "A":
            return False                     # one VFO — B does not exist
        return await self._engine.set_frequency(freq_hz)

    async def get_frequency(self, vfo: str = "A",
                            timeout: Optional[float] = None) -> Optional[int]:
        if str(vfo).upper() != "A" or not self.connected:
            return None
        return int(round(self._engine.freq))

    async def get_active_vfo(self, timeout: Optional[float] = None) -> Optional[str]:
        return "A"

    async def set_vfo(self, vfo: str) -> bool:
        return str(vfo).upper() == "A"       # already there: truth, not a feature

    async def set_split(self, on: bool) -> bool:
        return False

    # ── Mode / filter / AGC ──────────────────────────────────────

    def _mode_num(self) -> int:
        dsp = self._engine.demodulator.mode
        ui = _DSP_TO_UI_NAME.get(dsp, dsp)
        return MODE_NUMBERS.get(ui, MODE_NUMBERS["USB"])

    async def set_mode(self, mode_num: int) -> bool:
        name = next((n for n, num in MODE_NUMBERS.items() if num == mode_num), None)
        if name is None:
            return False
        try:
            await self._engine.set_mode(_DSP_MODE_NAME.get(name, name))
        except ValueError:
            return False
        return True

    async def get_mode(self, timeout: Optional[float] = None) -> Optional[int]:
        return self._mode_num()

    async def set_filter_width(self, index: int) -> bool:
        mode_ui = next(n for n, num in MODE_NUMBERS.items()
                       if num == self._mode_num())
        table = (NARROW_FILTER_WIDTHS if mode_ui in NARROW_UI_MODES
                 else VOICE_FILTER_WIDTHS)
        width = next((hz for idx, hz in table if idx == int(index)), None)
        if width is None:
            return False
        lo, hi = _passband_for(mode_ui, width)
        try:
            await self._engine.set_filter(lo, hi)
        except ValueError:
            return False
        self._filter_index = int(index)
        return True

    async def get_filter_width(self) -> Optional[int]:
        return self._filter_index

    async def set_agc(self, value: int) -> bool:
        dsp = _AGC_UI_TO_DSP.get(int(value))
        if dsp is None:
            return False
        await self._engine.set_agc_mode(dsp)
        return True

    async def get_agc(self) -> Optional[int]:
        return _AGC_DSP_TO_UI.get(self._engine.agc_mode, 3)

    # ── PTT / TUNE ───────────────────────────────────────────────

    async def set_ptt(self, tx: bool) -> bool:
        # The gate is checked on the way IN only: a release must never be
        # intercepted (every existing release path ends at set_ptt(False)).
        if tx and not _tx_allowed():
            return False
        if not tx and self._tune_active():
            task = self._tune_task
            self._tune_task = None
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            # A task cancelled before its first step never enters its finally.
            await self._engine.stop_tx()
        return await self._engine.set_ptt(tx)

    async def get_ptt(self, timeout: Optional[float] = None) -> Optional[int]:
        if self._tune_active():
            return 2
        return 1 if (self._engine.ptt_requested
                     or self._engine.ptt_confirmed) else 0

    def _tune_active(self) -> bool:
        task = self._tune_task
        return task is not None and not task.done()

    async def set_tune(self, tune: bool) -> bool:
        """TX2-class tune: key the radio and feed a bounded 700 Hz tone
        through the engine TX path.  Every exit — release, keying failure,
        the bounded timeout — ends at set_ptt(False); there is no ATU to
        pretend to drive."""
        if not tune:
            task = self._tune_task
            if task is not None:
                self._tune_task = None
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await self._engine.stop_tx()
            return True
        if self._tune_active():
            return True
        if not _tx_allowed():
            return False
        if not await self._engine.set_ptt(True):
            # Defensive release: a lost PTT1 must not leave the request
            # latched; PTT0 with nothing keyed is harmless.
            await self._engine.set_ptt(False)
            return False
        try:
            await self._engine.start_tx()
        except Exception:                               # noqa: BLE001
            await self._engine.set_ptt(False)
            return False
        self._tune_task = asyncio.create_task(self._tune_tone_loop(),
                                              name="sunsdr-tune-tone")
        return True

    async def _tune_tone_loop(self):
        frames = 0
        try:
            for _ in range(TUNE_PRIME_FRAMES):
                self._engine.feed_tx_audio(_tone_frame(frames * RX_TX_FRAME_SAMPLES))
                frames += 1
            deadline = time.monotonic() + TUNE_MAX_S
            while time.monotonic() < deadline:
                self._engine.feed_tx_audio(_tone_frame(frames * RX_TX_FRAME_SAMPLES))
                frames += 1
                await asyncio.sleep(TUNE_FEED_INTERVAL_S)
            logger.warning("SunSDR2 DX TUNE hit the %.0fs bound — releasing",
                           TUNE_MAX_S)
        except asyncio.CancelledError:
            pass
        finally:
            try:
                await self._engine.stop_tx()
            finally:
                await self._engine.set_ptt(False)
                if self._tune_task is asyncio.current_task():
                    self._tune_task = None

    # ── Gain / power / ATT-PRE ───────────────────────────────────

    async def set_rf_power(self, value: int) -> bool:
        return await self._engine.set_drive(value)

    async def set_drive(self, value: int) -> bool:
        return await self._engine.set_drive(value)

    async def set_preamp(self, value: int) -> bool:
        return await self._engine.set_att_preamp(value)

    async def set_attenuator(self, value: int) -> bool:
        return False                          # combined into set_preamp

    async def set_af_gain(self, value: int) -> bool:
        return False                          # no software AF stage (AGC only;
                                              # box/browser gains already exist)

    async def set_rf_gain(self, value: int) -> bool:
        return False

    async def set_mic_gain(self, value: int) -> bool:
        return False

    async def set_squelch(self, value: int) -> bool:
        return False

    # ── Noise processing / TX audio shaping (DSP does not provide) ──

    async def set_noise_blanker(self, on: bool) -> bool:
        return False

    async def set_noise_reduction(self, on: bool) -> bool:
        return False

    async def set_auto_notch(self, on: bool) -> bool:
        return False

    async def set_compressor(self, on: bool) -> bool:
        return False

    async def set_compressor_level(self, level: int) -> bool:
        return False

    async def set_amc_level(self, level: int) -> bool:
        return False

    async def get_amc_level(self, timeout: Optional[float] = None) -> Optional[int]:
        return None

    async def set_vox(self, on: bool) -> bool:
        return False

    async def set_break_in(self, on: bool) -> bool:
        return False

    async def set_monitor(self, on: bool) -> bool:
        return False

    async def set_monitor_gain(self, value: int) -> bool:
        return False

    async def set_key_speed(self, speed: int) -> bool:
        return False

    async def set_cw_pitch(self, pitch: int) -> bool:
        return False

    async def set_rit(self, on: bool) -> bool:
        return False

    async def set_rit_freq(self, freq: int) -> bool:
        return False

    async def set_xit(self, on: bool) -> bool:
        return False

    async def set_dnr(self, value: int) -> bool:
        return False

    async def get_dnr(self) -> Optional[int]:
        return None

    async def set_contour(self, value: int) -> bool:
        return False

    async def get_contour(self) -> Optional[int]:
        return None

    # ── Tuner / antenna / misc (not on this radio) ───────────────

    async def set_tuner(self, value: int) -> bool:
        return False                          # no internal ATU — and TUNE is
                                              # the tone above, never a tuner

    async def set_power(self, on: bool) -> bool:
        return False                          # no power command in the protocol

    async def set_band_stack(self, bsr: int) -> bool:
        return False

    async def set_antenna(self, ant: int) -> bool:
        return False

    async def get_antenna(self) -> Optional[int]:
        return None

    async def set_meter_display(self, meter: int) -> bool:
        return False

    async def get_meter_display(self, timeout: Optional[float] = None) -> Optional[int]:
        return None

    # ── Meters ───────────────────────────────────────────────────

    async def get_s_meter(self, timeout: Optional[float] = None) -> Optional[int]:
        """Filtered-IQ energy, raw 0..255.  None until the first IQ block —
        an S-meter reading with no signal data would be invented."""
        db = self._engine.s_meter
        return None if db is None else _s_db_to_raw(db)

    async def get_meter(self, meter: str, timeout: Optional[float] = None) -> Optional[int]:
        return None                           # no RM-style meter registers

    async def get_info(self) -> Optional[dict]:
        return None

    async def get_radio_info(self, timeout: Optional[float] = None) -> Optional[dict]:
        # INFO_QUERY replies are not part of the engine's contract (control
        # replies are counted and ignored) — nothing honest to report yet.
        return None

    async def get_nb_level(self) -> Optional[int]:
        return None

    async def get_nr_level(self) -> Optional[int]:
        return None

    async def set_nb_level(self, level: int) -> bool:
        return False

    async def set_nr_level(self, level: int) -> bool:
        return False

    # ── Scope ────────────────────────────────────────────────────

    async def set_scope_on(self, on: bool) -> bool:
        return bool(on)                       # the IQ stream is always there;
                                              # only client display toggles

    async def get_scope_on(self) -> Optional[int]:
        return 1

    async def set_scope_span(self, span: int) -> bool:
        idx = int(span)
        rate_key = SPAN_RATE_KEYS.get(idx)
        if rate_key is None:
            return False
        if self._engine.sample_rate == SAMPLE_RATES[rate_key]:
            self._set_span_index(idx)
            return True
        if not await self._engine.set_sample_rate(rate_key):
            return False
        self._set_span_index(idx)
        return True

    def _set_span_index(self, idx: int) -> None:
        self._span_index = idx
        producer = self._scope_producer
        if producer is not None:
            producer.span_index = idx

    async def set_scope_speed(self, speed: int) -> bool:
        return False

    async def set_scope_mode(self, mode: int) -> bool:
        return int(mode) == 0                 # CENTER is the only real layout

    # ── Poll items (supported state only) ────────────────────────

    def settings_poll_items(self) -> list:
        engine = self._engine

        async def _rf_power(timeout=None):
            return int(round(engine.drive_pct))

        async def _preamp(timeout=None):
            return int(engine.att_preamp)

        async def _agc(timeout=None):
            return _AGC_DSP_TO_UI.get(engine.agc_mode, 3)

        async def _filter_width(timeout=None):
            return self._filter_index

        return [
            ("rf_power", _rf_power),
            ("preamp", _preamp),
            ("agc", _agc),
            ("filter_width", _filter_width),
        ]

    def slow_poll_items(self) -> list:
        async def _freq_mode(timeout=None):
            values: dict = {}
            if self.connected:
                values["vfo_a_freq"] = int(round(self._engine.freq))
                values["mode"] = self._mode_num()
            return values
        return [("freq_mode", _freq_mode)]

    def tx_meter_items(self) -> list:
        async def _power(timeout=None):
            t = self._engine.latest_telemetry
            if not t or "forward_w" not in t:
                return None
            return _watts_to_raw(t["forward_w"])
        return [("Power", "power_meter", _power)]

    def always_meter_items(self) -> list:
        return [("S", "s_meter", self.get_s_meter)]

    # ── Bulk state query ─────────────────────────────────────────

    async def initial_state_sync(self) -> dict:
        """Real state only — everything here is local engine state, no
        wire queries, and None values are dropped by from_sync_result."""
        sync: dict = {
            "vfo_a_freq": int(round(self._engine.freq)),
            "active_vfo": "A",
            "mode": self._mode_num(),
            "tx_status": await self.get_ptt(),
            "rf_power": int(round(self._engine.drive_pct)),
            "preamp": int(self._engine.att_preamp),
            "attenuator": 0,
            "agc": _AGC_DSP_TO_UI.get(self._engine.agc_mode, 3),
            "filter_width": self._filter_index,
            "s_meter": await self.get_s_meter(),
            "scope_span": self._span_index,
        }
        t = self._engine.latest_telemetry
        if t:
            if "forward_w" in t:
                sync["power_meter"] = _watts_to_raw(t["forward_w"])
            if "volts" in t:
                sync["vd_meter"] = _volts_to_raw(t["volts"])
        # from_sync_result drops None values; so does this dict — a field
        # with no real reading (e.g. s_meter before the first IQ block) is
        # omitted rather than reported as None.
        return {k: v for k, v in sync.items() if v is not None}


class SunSDR2DXBackend(RadioBackend):
    """RadioBackend for the Expert Electronics SunSDR2 DX (LAN control)."""

    def __init__(self, port: str = "", baud_rate: int = DEFAULT_BAUD_RATE,
                 host: Optional[str] = None, *,
                 engine: Optional[SunSDREngine] = None):
        # port/baud_rate are accepted for create_backend compatibility; the
        # radio has no serial port.  An explicit host wins for diagnostics.
        self._port = port
        self._baud = int(baud_rate or DEFAULT_BAUD_RATE)
        if engine is None:
            resolved = (str(host).strip() if host is not None
                        else str(getattr(config, "SUNSDR_HOST", "") or "").strip())
            engine = SunSDREngine(
                host=resolved,
                local_host=str(getattr(config, "SUNSDR_LOCAL_HOST", "") or ""),
                control_port=int(getattr(config, "SUNSDR_CONTROL_PORT", 50001)),
                iq_port=int(getattr(config, "SUNSDR_IQ_PORT", 50002)),
                sample_rate=int(getattr(config, "SUNSDR_SAMPLE_RATE", 78125)),
            )
        self._engine = engine
        self._engine.on_event = self._on_engine_event
        self._cat = SunSDR2DXController(self._engine)
        self._broadcast: Optional[Callable[[str, Any], None]] = None
        self._audio_handler = None
        self._scope_producer: Optional[SunSDRScopeProducer] = None

    # ── Engine / events ──────────────────────────────────────────

    @property
    def engine(self) -> SunSDREngine:
        """The single shared device engine (audio/scope adapters use it)."""
        return self._engine

    def set_broadcast_callback(self, cb: Callable[[str, Any], None]) -> None:
        self._broadcast = cb

    def _on_engine_event(self, name: str, payload: dict):
        """Engine → server seam: telemetry/meter/connection changes enter
        the normal dirty-field broadcast path (never a server import)."""
        cb = self._broadcast
        if cb is None:
            return
        try:
            if name == "telemetry":
                t = payload or {}
                if "forward_w" in t:
                    cb("power_meter", _watts_to_raw(t["forward_w"]))
                if "volts" in t:
                    cb("vd_meter", _volts_to_raw(t["volts"]))
            elif name == "ptt_confirmed":
                # While TUNE holds the key the WS tune flow owns
                # tx_status=2; a device confirm would downgrade it to 1.
                if not self._cat._tune_active():
                    cb("tx_status", 1 if (payload or {}).get("ptt") else 0)
            elif name == "connected":
                cb("serial_connected", True)
            elif name == "disconnected":
                cb("serial_connected", False)
        except Exception as e:                              # noqa: BLE001
            logger.warning("SunSDR2 DX broadcast(%r) failed: %s", name, e)

    # ── Capabilities / tables ────────────────────────────────────

    @property
    def capabilities(self) -> RadioCapabilities:
        return RadioCapabilities(
            model_name=MODEL_KEY,
            display_name=DISPLAY_NAME,
            default_baud=DEFAULT_BAUD_RATE,
            audio_rx_rate=AUDIO_RATE,
            audio_tx_rate=AUDIO_RATE,
            audio_name_hints=(),
            has_atu=False,
            has_auto_notch=False,
            has_vd_id_meters=False,
            vfo_b_direct=False,
            filter_model="width_table",
            att_steps=(),
            preamp_steps=ATT_PREAMP_LABELS,
            scope_type="lan_iq",
            scope_spans=dict(SCOPE_HALF_SPANS),
            scope_speeds=(),
            tune_via="tx2",
            verified=VERIFIED,
            tx_gated=not (config.ALLOW_UNVERIFIED_TX or VERIFIED),
            dual_rx=False,
            unverified_meters=("s_meter", "power", "vd"),
            audio_gain_boost=1.0,
        )

    @property
    def bands(self) -> list:
        return [dict(b) for b in BANDS]

    @property
    def ui_modes(self) -> list:
        return list(UI_MODES)

    @property
    def mode_name_to_num(self) -> dict:
        return dict(MODE_NUMBERS)

    def filter_tables(self) -> dict:
        return {"voice": list(VOICE_FILTER_WIDTHS),
                "narrow": list(NARROW_FILTER_WIDTHS),
                "narrowModes": sorted(NARROW_UI_MODES)}

    def state_tables(self) -> dict:
        return {
            "mode_num_to_name": {num: name for name, num in MODE_NUMBERS.items()},
            "preamp_labels": {i: label for i, label in enumerate(ATT_PREAMP_LABELS)},
            "attenuator_labels": {},
            "get_band_for_frequency": _band_for_frequency,
            "get_filter_hz": _filter_hz,
            "raw_to_dbm": _raw_to_s_db,
            "raw_to_s_unit": _raw_to_s_unit,
            "raw_to_power": config.RAW_TO_METER_TABLES["power"](POWER_RATED_W),
            "raw_to_swr": config.RAW_TO_METER_TABLES["swr"](),
            "raw_to_voltage": config.RAW_TO_METER_TABLES["voltage"](),
            "raw_to_current": config.RAW_TO_METER_TABLES["current"](),
        }

    # ── Factories (idempotent, same engine) ──────────────────────

    def create_audio_handler(self):
        """The network audio adapter (see RadioBackend.create_audio_handler)."""
        if self._audio_handler is None:
            from backends.sunsdr.audio import SunSDRAudioHandler
            self._audio_handler = SunSDRAudioHandler(self._engine)
        return self._audio_handler

    def create_scope_producer(
        self,
        scope_handler: Any = None,
        on_frame: Optional[Callable[[Any], Awaitable[None]]] = None,
    ) -> Optional[ScopeProducer]:
        if self._scope_producer is None:
            self._scope_producer = SunSDRScopeProducer(
                self._engine, scope_handler, on_frame)
        else:
            if scope_handler is not None:
                self._scope_producer._scope = scope_handler
            if on_frame is not None:
                self._scope_producer.set_on_frame(on_frame)
        self._scope_producer.span_index = self._cat._span_index
        self._cat._scope_producer = self._scope_producer
        return self._scope_producer

    # ── Passthrough connection / poll surface ────────────────────

    @property
    def cat(self) -> SunSDR2DXController:
        return self._cat

    @property
    def connected(self) -> bool:
        return self._engine.connected

    @property
    def model(self) -> str:
        return MODEL_KEY

    async def connect(self) -> bool:
        ok = await self._engine.start()
        if not ok:
            logger.error(
                "SunSDR2 DX connect failed — check MRRC_SUNSDR_HOST (current "
                "host %r) and that UDP %d/%d are free on this machine",
                self._engine.host, self._engine.control_port,
                self._engine.iq_port)
        return ok

    async def disconnect(self) -> None:
        await self._engine.stop()

    async def reconnect_loop(self) -> bool:
        return await self._cat.reconnect_loop()

    async def boot_verify(self, cat=None, timeout: float = 0.4) -> bool:
        return await self._cat.boot_verify()

    async def init_scope(self) -> None:
        return None            # the IQ stream boots with the engine

    def settings_poll_items(self) -> list:
        return self._cat.settings_poll_items()

    def slow_poll_items(self) -> list:
        return self._cat.slow_poll_items()

    def tx_meter_items(self) -> list:
        return self._cat.tx_meter_items()

    def always_meter_items(self) -> list:
        return self._cat.always_meter_items()

    async def initial_state_sync(self) -> dict:
        return await self._cat.initial_state_sync()


# ── Mechanical delegates for the rest of the abstract surface ─────
#
# ABCMeta freezes __abstractmethods__ at class creation, so the backend
# binds the remaining RadioBackend abstract methods onto the class at
# import time, each delegating to the controller — the same pattern as the
# Yaesu family (see backends/yaesu/backend.py).  The controller implements
# every one of them; ConstructionTests keeps this honest if the ABC grows.
def _make_delegate(name: str):
    async def _delegate(self, *args, **kwargs):
        return await getattr(self._cat, name)(*args, **kwargs)

    _delegate.__name__ = name
    _delegate.__qualname__ = f"SunSDR2DXBackend.{name}"
    return _delegate


for _name in sorted(SunSDR2DXBackend.__abstractmethods__):
    setattr(SunSDR2DXBackend, _name, _make_delegate(_name))

SunSDR2DXBackend.__abstractmethods__ = frozenset()
