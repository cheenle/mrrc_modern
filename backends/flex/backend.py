"""``RadioBackend`` for FlexRadio (SmartSDR) radios — model key ``flex``.

One key, one protocol, the whole family: FLEX-6300/6400/6500/6600/6700 and the
current 8400/8600 Signature line all speak SmartSDR v1.4.0.0, and the radio
describes itself (``info`` / ``radio`` status).  Nothing here branches on a
model name — a hardcoded capacity table can only ever lie to the UI (the
2026-11-15 design record §7).

What this backend deliberately does **not** do:

* No serial CAT, no USB sound card: control, spectrum and audio all travel the
  LAN (``engine.py`` owns the sockets).
* No VFO-B, no split, no radio power command — the abstract methods report
  ``False``/``None`` and the capabilities hide the controls instead of letting
  a button lie (``has_vfo_b=False``, ``has_split=False``, …).
* No transmit until the operator opts in: ``VERIFIED`` is False, so key-up and
  TUNE pass through ``_tx_allowed()`` on **every** press (AD-019 / NFR-067).
  Release paths are never gated — a gate that can block an unkey is a hazard.
"""

from __future__ import annotations

import logging
from typing import Optional

import config
from backends.base import RadioBackend, RadioCapabilities
from backends.flex import vita
from backends.flex.audio import FlexAudioHandler
from backends.flex.engine import DEFAULT_BANDWIDTH_HZ, SmartSDREngine
from backends.flex.scope import DEFAULT_SPAN_INDEX, SPANS, FlexScopeProducer

logger = logging.getLogger(__name__)

MODEL_KEY = "flex"
DISPLAY_NAME = "FlexRadio (SmartSDR)"
VERIFIED = False
DEFAULT_BAUD_RATE = 115200       # registry table entry only — no serial port

# ── UI tables (shared vocabulary with the other backends) ───────────
MODE_NUM_TO_NAME: dict[int, str] = {
    1: "LSB", 2: "USB", 3: "AM", 4: "CW-U", 5: "RTTY-L", 6: "FM", 7: "DIGI-U",
}
MODE_NAME_TO_NUM: dict[str, int] = {v: k for k, v in MODE_NUM_TO_NAME.items()}
#: UI mode name → SmartSDR mode string (``slice s <n> mode=…``).
MODE_WIRE_NAME: dict[str, str] = {
    "LSB": "LSB", "USB": "USB", "AM": "AM", "CW-U": "CW",
    "RTTY-L": "RTTY", "FM": "FM", "DIGI-U": "DIGU",
}
WIRE_MODE_TO_UI: dict[str, str] = {
    "LSB": "LSB", "USB": "USB", "AM": "AM", "SAM": "AM", "CW": "CW-U",
    "RTTY": "RTTY-L", "FM": "FM", "NFM": "FM", "DFM": "FM",
    "DIGU": "DIGI-U", "FDVU": "DIGI-U", "DIGL": "LSB", "FDVL": "LSB",
}
UI_MODES: list[str] = ["LSB", "USB", "CW-U", "AM", "FM", "RTTY-L", "DIGI-U"]

VOICE_FILTER_WIDTHS: list[tuple[int, int]] = [
    (0, 1800), (1, 2100), (2, 2400), (3, 2700), (4, 3000), (5, 3300),
]
NARROW_FILTER_WIDTHS: list[tuple[int, int]] = [
    (0, 250), (1, 500), (2, 1000), (3, 1200),
]
NARROW_UI_MODES = ("CW-U", "RTTY-L", "DIGI-U")
DEFAULT_FILTER_INDEX = 3
CW_PITCH_HZ = 700.0

BANDS: list[dict] = [
    {"name": "160m", "start": 1_800_000, "end": 2_000_000,
     "default_freq": 1_840_000},
    {"name": "80m", "start": 3_500_000, "end": 4_000_000,
     "default_freq": 3_573_000},
    {"name": "60m", "start": 5_330_500, "end": 5_405_000,
     "default_freq": 5_357_000},
    {"name": "40m", "start": 7_000_000, "end": 7_300_000,
     "default_freq": 7_074_000},
    {"name": "30m", "start": 10_100_000, "end": 10_150_000,
     "default_freq": 10_136_000},
    {"name": "20m", "start": 14_000_000, "end": 14_350_000,
     "default_freq": 14_074_000},
    {"name": "17m", "start": 18_068_000, "end": 18_168_000,
     "default_freq": 18_100_000},
    {"name": "15m", "start": 21_000_000, "end": 21_450_000,
     "default_freq": 21_074_000},
    {"name": "12m", "start": 24_890_000, "end": 24_990_000,
     "default_freq": 24_915_000},
    {"name": "10m", "start": 28_000_000, "end": 29_700_000,
     "default_freq": 28_074_000},
    {"name": "6m", "start": 50_000_000, "end": 54_000_000,
     "default_freq": 50_313_000},
]

POWER_RATED_W = 100.0
S_METER_MIN_DBM = -140.0
S_METER_MAX_DBM = -20.0

AGC_UI_TO_WIRE = {0: "off", 1: "fast", 2: "med", 3: "slow"}
AGC_WIRE_TO_UI = {"off": 0, "fast": 1, "med": 2, "slow": 3}

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


def _dbm_to_raw(dbm: float) -> int:
    span = S_METER_MAX_DBM - S_METER_MIN_DBM
    value = (float(dbm) - S_METER_MIN_DBM) / span * 255.0
    return int(round(max(0.0, min(255.0, value))))


def _raw_to_dbm(raw: int) -> float:
    span = S_METER_MAX_DBM - S_METER_MIN_DBM
    return max(0, min(255, int(raw))) * span / 255.0 + S_METER_MIN_DBM


def _raw_to_s_unit(raw: int) -> int:
    # Same convention as the other backends: S9 = -73 dBm, 6 dB per S unit.
    # Rounded, not floored: the raw byte quantizes dBm to 0.47 dB steps, and a
    # floor would read S9 as S8 whenever the nearest byte lands 0.2 dB low.
    return max(0, int(round((_raw_to_dbm(raw) + 127.0) / 6.0)))


def _watts_to_raw(watts: float) -> int:
    return int(round(max(0.0, min(255.0, float(watts) / POWER_RATED_W * 255.0))))


def _raw_to_power(raw: int) -> float:
    return max(0, min(255, int(raw))) / 255.0 * POWER_RATED_W


def _swr_to_raw(swr: float) -> int:
    value = (float(swr) - 1.0) / 9.0 * 255.0
    return int(round(max(0.0, min(255.0, value))))


def _raw_to_swr(raw: int) -> float:
    return 1.0 + max(0, min(255, int(raw))) / 255.0 * 9.0


def _band_for_frequency(freq_hz: int) -> Optional[dict]:
    for band in BANDS:
        if band["start"] <= freq_hz <= band["end"]:
            return {"name": band["name"], "low": band["start"],
                    "high": band["end"]}
    return None


def _filter_table(mode_name: str) -> list[tuple[int, int]]:
    return (NARROW_FILTER_WIDTHS if mode_name in NARROW_UI_MODES
            else VOICE_FILTER_WIDTHS)


def _filter_hz(mode_name: str, index: int) -> Optional[int]:
    for idx, hz in _filter_table(mode_name):
        if idx == index:
            return hz
    return None


def _default_filter_index(mode_name: str) -> int:
    """The filter a mode switch lands on: 500 Hz for CW/RTTY/digi, 2.7 kHz SSB."""
    return 1 if mode_name in NARROW_UI_MODES else DEFAULT_FILTER_INDEX


def _filter_edges_hz(mode_name: str, index: int) -> tuple[int, int]:
    """UI filter index → the slice's ``filter_lo``/``filter_hi`` in Hz."""
    width = _filter_hz(mode_name, index) or _filter_table(mode_name)[0][1]
    if mode_name == "CW-U":
        centre = CW_PITCH_HZ
        return int(centre - width / 2), int(centre + width / 2)
    if mode_name == "LSB":
        return -width, 0
    return 0, width


class FlexController:
    """The CAT surface (``backend.cat``) over one ``SmartSDREngine``.

    Every method either maps to a documented SmartSDR command plus a status
    read-back, or answers ``False``/``None`` and is left out of the poll
    tables — a fabricated CAT frame is how a UI starts lying.
    """

    def __init__(self, backend: "FlexBackend") -> None:
        self._backend = backend
        self.engine = backend.engine
        self._filter_index = DEFAULT_FILTER_INDEX
        self._agc = 2
        self._nb_level = 50
        self._nr_level = 50
        self._squelch_level = 0
        self._antenna = 1
        self._ptt = False

    # ── helpers ──────────────────────────────────────────────────

    @property
    def session(self):
        return self.engine.session

    def _slice_id(self) -> int:
        index = self.engine.own_slice
        if index is None:
            index = min(self.engine.slices) if self.engine.slices else 0
        return index

    async def _slice_set(self, **pairs) -> bool:
        session = self.session
        if session is None:
            return False
        command = " ".join(f"{key}={value}" for key, value in pairs.items())
        return await session.send_ok(f"slice s {self._slice_id()} {command}")

    async def _transmit_set(self, **pairs) -> bool:
        session = self.session
        if session is None:
            return False
        command = " ".join(f"{key}={value}" for key, value in pairs.items())
        return await session.send_ok(f"transmit set {command}")

    def _state(self) -> dict:
        return self.engine.slice_state()

    # ── connection ───────────────────────────────────────────────

    @property
    def connected(self) -> bool:
        """Whether the SmartSDR session is up.

        Part of the controller surface `server.py` queries for **every** model:
        `/api/health` reports it, and `_schedule_restart()`'s PTT-release guard
        reads it before exiting the process.  Missing it did not merely report
        the wrong thing — the guard raised `AttributeError` inside the restart
        timer, `os._exit()` never ran, and a saved model switch was therefore
        never applied (2026-11-16 field bundle `20261116-000702-36fa`:
        "选了 flexradio 后再配置其他电台连接不起作用了").
        """
        return bool(self.engine.connected)

    async def connect(self) -> bool:
        ok = await self.engine.start()
        if ok:
            # 电台记住的 panadapter 可能停在别的频段（上次的频点），先把它拉回 VFO，
            # 否则一进来就是"有声音、瀑布显示另一条带"。
            await self.ensure_pan_covers_vfo()
        self._ptt = False
        return ok

    async def disconnect(self) -> None:
        self._ptt = False
        await self.engine.stop()

    async def reconnect_loop(self) -> bool:
        """The engine owns its own reconnect loop; report its state."""
        return self.engine.connected

    # ── raw command escape hatch ─────────────────────────────────

    async def send_command(self, cmd: str, timeout: Optional[float] = None) -> Optional[str]:
        session = self.session
        if session is None:
            return None
        try:
            reply = await session.send(cmd, timeout=timeout)
        except Exception:                                # noqa: BLE001
            return None
        return reply.body if reply.ok else None

    async def query(self, cmd: str, timeout: Optional[float] = None) -> Optional[str]:
        return await self.send_command(cmd, timeout=timeout)

    async def send_set_command(self, cmd: str) -> bool:
        session = self.session
        if session is None:
            return False
        return await session.send_ok(cmd)

    async def send_priority_set_command(self, cmd: str) -> bool:
        return await self.send_set_command(cmd)

    async def set(self, cmd: str) -> bool:
        return await self.send_set_command(cmd)

    # ── frequency / mode ─────────────────────────────────────────

    async def set_frequency(self, freq_hz: int, vfo: str = "A") -> bool:
        session = self.session
        if session is None:
            return False
        # ``slice t`` (tune) would drag the panadapter; the design keeps the
        # pan fixed and moves only the slice, so ``slice m`` is used on
        # click-to-tune and plain tuning keeps autopan off.
        ok = await session.send_ok(
            f"slice t {self._slice_id()} {freq_hz / 1e6:.6f} autopan=0")
        if ok:
            self.engine.slices.setdefault(self._slice_id(), {})["freq_hz"] = int(freq_hz)
            await self.ensure_pan_covers_vfo()
        return ok

    async def ensure_pan_covers_vfo(self) -> bool:
        """Move the panadapter only when the VFO has left its span.

        `autopan=0` above is deliberate: clicking a signal inside the waterfall
        must not drag the waterfall under the finger.  But the same flag used to
        leave the pan parked on the old band after a **band change / memory
        recall / direct frequency entry**, which looks exactly like "no signal":
        measured 2026-11-16, pan centre 14.15 MHz while the slice was on
        15.800 MHz — audio fine, waterfall showing another band entirely.

        Inside the span: do nothing (the pan is the user's).  Outside: re-centre
        it on the slice, which is what every other backend's scope does.
        """
        session = self.session
        state = self._state()
        freq = state.get("freq_hz")
        if session is None or not freq or not self.engine.pan_id:
            return False
        span = int(self.engine.bandwidth_hz or DEFAULT_BANDWIDTH_HZ)
        center = int(self.engine.pan_center_hz or 0)
        if center and abs(int(freq) - center) <= span * 0.45:
            return False
        ok = await session.send_ok(
            f"display pan set {self.engine.pan_id} center={freq / 1e6:.6f}")
        if ok:
            self.engine.pan_center_hz = int(freq)
        return ok

    async def get_active_vfo(self, timeout: Optional[float] = None) -> Optional[str]:
        return "A"

    async def get_frequency(self, vfo: str = "A", timeout: Optional[float] = None) -> Optional[int]:
        freq = self._state().get("freq_hz")
        return int(freq) if freq else None

    async def set_mode(self, mode_num: int) -> bool:
        name = MODE_NUM_TO_NAME.get(int(mode_num))
        if not name:
            return False
        wire = MODE_WIRE_NAME.get(name)
        if not wire:
            return False
        index = _default_filter_index(name)
        lo, hi = _filter_edges_hz(name, index)
        ok = await self._slice_set(mode=wire, filter_lo=lo, filter_hi=hi)
        if ok:
            self._filter_index = index
            self.engine.slices.setdefault(self._slice_id(), {})["mode"] = name
        return ok

    async def get_mode(self, timeout: Optional[float] = None) -> Optional[int]:
        wire = str(self._state().get("mode", "")).upper()
        name = WIRE_MODE_TO_UI.get(wire)
        if name is None:
            return None
        return MODE_NAME_TO_NUM.get(name)

    # ── transmit ─────────────────────────────────────────────────

    async def set_ptt(self, tx: bool) -> bool:
        if tx and not _tx_allowed():
            return False
        if tx and self._backend.tx_audio_path_unconfirmed:
            logger.warning(
                "FlexRadio transmit refused: the PC-audio path was never "
                "proven on this radio (no TX-/SC_MIC energy). Select the PC "
                "audio source in the radio's P/CW panel, then clear the "
                "interlock in 设置 → 实验机型的发射.")
            return False
        ok = await self.engine.key(bool(tx))
        self._ptt = bool(ok and tx)
        return ok

    async def set_tune(self, tune: bool) -> bool:
        if tune and not _tx_allowed():
            return False
        ok = await self.engine.tune(bool(tune))
        if tune and ok:
            self._ptt = True
        elif not tune:
            self._ptt = False
        return ok

    async def get_ptt(self, timeout: Optional[float] = None) -> Optional[int]:
        session = self.session
        if session is None:
            return 0
        return 1 if session.interlock_state == "TRANSMITTING" else 0

    # ── meters ───────────────────────────────────────────────────

    async def get_s_meter(self, timeout: Optional[float] = None) -> Optional[int]:
        dbm = self.engine.s_meter_dbm()
        if dbm is None:
            return None
        return _dbm_to_raw(dbm)

    async def get_meter(self, meter: str, timeout: Optional[float] = None) -> Optional[int]:
        key = (meter or "").lower()
        if key in ("power", "power_meter", "po"):
            watts = self.engine.forward_power_watts()
            return None if watts is None else _watts_to_raw(watts)
        if key == "swr":
            swr = self.engine.meter_by_name("TX-", "SWR")
            return None if swr is None else _swr_to_raw(swr)
        if key in ("alc", "alc_meter"):
            alc = self.engine.meter_by_name("TX-", "ALC")
            return None if alc is None else int(round(max(0.0, min(255.0,
                                                                 (alc + 40) / 40 * 255))))
        if key in ("comp", "compression", "comp_meter"):
            comp = self.engine.meter_by_name("TX-", "COMPPEAK")
            return None if comp is None else int(round(max(0.0, min(255.0,
                                                                    (comp + 25) / 25 * 255))))
        return None

    async def get_info(self) -> Optional[dict]:
        return self.engine.stats()

    async def get_radio_info(self, timeout: Optional[float] = None) -> Optional[dict]:
        return self.engine.stats()

    # ── filters and levels ───────────────────────────────────────

    async def set_filter_width(self, index: int) -> bool:
        name = MODE_NUM_TO_NAME.get(int(self._state().get("mode_num", 0)), "USB")
        ui_name = WIRE_MODE_TO_UI.get(str(self._state().get("mode", "")).upper(),
                                      name)
        mode_name = self._state().get("mode") or ui_name
        lo, hi = _filter_edges_hz(str(mode_name), int(index))
        if not await self._slice_set(filter_lo=lo, filter_hi=hi):
            return False
        self._filter_index = int(index)
        return True

    async def get_filter_width(self) -> Optional[int]:
        return self._filter_index

    async def set_af_gain(self, value: int) -> bool:
        return await self._slice_set(audio_level=max(0, min(100, int(value))))

    async def set_rf_gain(self, value: int) -> bool:
        """`slice s <n> rfgain=<v>` — the SCU's RF gain for this antenna.

        Range is the radio's own (the `RF Gain` slider in SmartSDR's ANT pop-out);
        an out-of-range value is refused by the radio with ``0x50000031`` and that
        message is what the operator sees, rather than a silently clamped guess.
        """
        v = max(0, min(100, int(value)))
        return await self._slice_set(rfgain=v)

    async def set_agc_threshold(self, value: int) -> bool:
        """`agc_threshold` — AGC-T, the **maximum gain** the AGC may apply.

        FlexRadio's own guidance: adjust the preamp first, then the AGC speed, then
        AGC-T — start fully open and back off until the band noise *just* starts to
        drop.  Too low and every weak signal stays weak (field 2026-11-16: LSB
        signals quiet, and the app had no control over this at all).
        """
        v = max(0, min(100, int(value)))
        ok = await self._slice_set(agc_threshold=v, agc_threshold_mode="manual")
        if ok:
            self.engine.slices.setdefault(self._slice_id(), {})["agc_threshold"] = v
        return ok

    async def get_agc_threshold(self) -> Optional[int]:
        value = self._state().get("agc_threshold")
        return int(value) if value is not None else None

    async def set_rf_power(self, value: int) -> bool:
        return await self._transmit_set(rfpower=max(0, min(100, int(value))))

    async def set_drive(self, value: int) -> bool:
        return await self.set_rf_power(value)

    async def set_preamp(self, value: int) -> bool:
        return False

    async def set_attenuator(self, value: int) -> bool:
        return False

    async def set_noise_blanker(self, on: bool) -> bool:
        return await self._slice_set(nb=1 if on else 0)

    async def set_noise_reduction(self, on: bool) -> bool:
        return await self._slice_set(nr=1 if on else 0)

    async def set_auto_notch(self, on: bool) -> bool:
        return await self._slice_set(anf=1 if on else 0)

    async def set_compressor(self, on: bool) -> bool:
        return await self._transmit_set(speech_processor_enable=1 if on else 0)

    async def set_compressor_level(self, level: int) -> bool:
        step = 0 if level < 34 else (1 if level < 67 else 2)
        return await self._transmit_set(speech_processor_level=step)

    async def set_tuner(self, value: int) -> bool:
        if not self.engine.has_atu or not _tx_allowed():
            return False
        if value:
            return await self.send_set_command("atu start")
        return await self.send_set_command("atu stop")

    async def set_squelch(self, value: int) -> bool:
        self._squelch_level = max(0, min(100, int(value)))
        return await self._slice_set(
            squelch=1 if self._squelch_level > 0 else 0,
            squelch_level=self._squelch_level)

    async def set_mic_gain(self, value: int) -> bool:
        return await self._transmit_set(miclevel=max(0, min(100, int(value))))

    async def set_agc(self, value: int) -> bool:
        wire = AGC_UI_TO_WIRE.get(int(value))
        if wire is None:
            return False
        if not await self._slice_set(agc_mode=wire):
            return False
        self._agc = int(value)
        return True

    async def get_agc(self) -> Optional[int]:
        return self._agc

    async def set_dnr(self, value: int) -> bool:
        self._nr_level = max(0, min(100, int(value)))
        return await self._slice_set(nr_level=self._nr_level)

    async def get_dnr(self) -> Optional[int]:
        return self._nr_level

    async def set_nb_level(self, level: int) -> bool:
        self._nb_level = max(0, min(100, int(level)))
        return await self._slice_set(nb_level=self._nb_level)

    async def set_nr_level(self, level: int) -> bool:
        self._nr_level = max(0, min(100, int(level)))
        return await self._slice_set(nr_level=self._nr_level)

    async def get_nb_level(self) -> Optional[int]:
        return self._nb_level

    async def get_nr_level(self) -> Optional[int]:
        return self._nr_level

    async def set_contour(self, value: int) -> bool:
        return False

    async def get_contour(self) -> Optional[int]:
        return None

    async def set_antenna(self, ant: int) -> bool:
        """Select by 1-based index into the radio's own `ant_list`.

        Kept for the numeric protocol the serial backends use; a FLEX-6600 offers
        six RX connectors, so the name-based call below is the real API.
        """
        options = self._state().get("antenna_list") or []
        if options:
            index = min(max(1, int(ant)), len(options)) - 1
            return await self.set_antenna_name(options[index])
        return await self.set_antenna_name("ANT2" if int(ant) == 2 else "ANT1")

    async def get_antenna(self) -> Optional[int]:
        options = self._state().get("antenna_list") or []
        current = self._state().get("rxant")
        if options and current in options:
            return options.index(current) + 1
        return self._antenna

    async def set_antenna_name(self, name: str) -> bool:
        """`slice set <n> rxant=<name>` — the command that actually routes RF.

        Measured on a FLEX-6600 (v4.3.16, 2026-11-16): slice status carries
        `rxant` / `txant` / `ant_list` (`ANT1,ANT2,RX_A,RX_B,XVTA,XVTB`), and there
        is **no** `ant` key.  The previous implementation sent `ant=ANT1`, which
        the radio accepts and ignores — so a box could be connected, tuned and
        completely silent because the slice was listening on a connector with no
        antenna on it.
        """
        wanted = str(name or "").strip().upper()
        if not wanted:
            return False
        options = self._state().get("antenna_list") or []
        if options and wanted not in options:
            logger.warning("FlexRadio: %r is not one of this radio's RX antennas (%s)",
                           wanted, ", ".join(options))
            return False
        if not await self._slice_set(rxant=wanted):
            return False
        self.engine.slices.setdefault(self._slice_id(), {})["rxant"] = wanted
        if options:
            self._antenna = options.index(wanted) + 1
        return True

    async def get_antenna_name(self) -> Optional[str]:
        return self._state().get("rxant")

    async def set_tx_antenna_name(self, name: str) -> bool:
        """`slice s <n> txant=<name>` — which port this slice would transmit on.

        The **TX** antenna really is per slice (unlike RX, which the radio scopes
        to the panadapter); `tx_ant_list` never contains ``RX_A``/``RX_B``
        because those are receive-only connectors.
        """
        wanted = str(name or "").strip().upper()
        if not wanted:
            return False
        options = self._state().get("tx_ant_list") or []
        if options and wanted not in options:
            logger.warning("FlexRadio: %r is not one of this radio's TX antennas (%s)",
                           wanted, ", ".join(options))
            return False
        if not await self._slice_set(txant=wanted):
            return False
        self.engine.slices.setdefault(self._slice_id(), {})["txant"] = wanted
        return True

    async def get_tx_antenna_name(self) -> Optional[str]:
        return self._state().get("txant")

    async def set_rit(self, on: bool) -> bool:
        return await self._slice_set(rit_on=1 if on else 0)

    async def set_rit_freq(self, freq: int) -> bool:
        return await self._slice_set(rit_freq=int(freq))

    async def set_xit(self, on: bool) -> bool:
        return await self._slice_set(xit_on=1 if on else 0)

    # ── unsupported surfaces (report, never fake) ────────────────

    async def set_vfo(self, vfo: str) -> bool:
        return False

    async def set_split(self, on: bool) -> bool:
        return False

    async def set_power(self, on: bool) -> bool:
        return False

    async def set_band_stack(self, bsr: int) -> bool:
        return False

    async def set_monitor(self, on: bool) -> bool:
        return False

    async def set_monitor_gain(self, value: int) -> bool:
        return False

    async def set_break_in(self, on: bool) -> bool:
        # ``transmit set break_in`` is wrong on v1.4.0.0; the real command is
        # ``cw break_in`` (analysis §2.2).
        return await self.send_set_command(f"cw break_in {1 if on else 0}")

    async def set_vox(self, on: bool) -> bool:
        return await self._transmit_set(vox=1 if on else 0)

    async def set_key_speed(self, speed: int) -> bool:
        return False

    async def set_cw_pitch(self, pitch: int) -> bool:
        return False

    async def set_meter_display(self, meter: int) -> bool:
        return False

    async def get_meter_display(self, timeout: Optional[float] = None) -> Optional[int]:
        return None

    async def set_amc_level(self, level: int) -> bool:
        return False

    async def get_amc_level(self, timeout: Optional[float] = None) -> Optional[int]:
        return None

    # ── scope ────────────────────────────────────────────────────

    async def set_scope_on(self, on: bool) -> bool:
        # The radio streams FFT frames to any client that owns a panadapter;
        # there is no separate on/off command in the documented API.
        return True

    async def get_scope_on(self) -> Optional[int]:
        return 1 if self.engine.pan_id else 0

    async def set_scope_span(self, span: int) -> bool:
        producer = self._backend.scope_producer
        if producer is None:
            return False
        ok = await producer.set_span(int(span))
        if ok:
            # 缩窄跨度有可能把正在听的频率挤出视窗，顺手把它拉回来。
            await self.ensure_pan_covers_vfo()
        return ok

    async def set_scope_speed(self, speed: int) -> bool:
        session = self.session
        if session is None or not self.engine.pan_id:
            return False
        fps = max(1, min(30, int(speed)))
        return await session.send_ok(
            f"display pan set {self.engine.pan_id} fps={fps}")

    async def set_scope_mode(self, mode: int) -> bool:
        # Only CENTER is offered: the producer publishes start_freq from the
        # pan centre, and a second mode would need its own axis arithmetic.
        return int(mode) == 0

    # ── poll tables ──────────────────────────────────────────────

    def settings_poll_items(self) -> list:
        return []

    def slow_poll_items(self) -> list:
        return []

    def tx_meter_items(self) -> list:
        async def _power(timeout=None):
            return await self.get_meter("power")

        async def _swr(timeout=None):
            return await self.get_meter("swr")

        async def _alc(timeout=None):
            return await self.get_meter("alc")

        return [("Power", "power_meter", _power),
                ("SWR", "swr_meter", _swr),
                ("ALC", "alc_meter", _alc)]

    def always_meter_items(self) -> list:
        return [("S", "s_meter", self.get_s_meter)]

    async def initial_state_sync(self) -> dict:
        state = self._state()
        options = list(state.get("antenna_list") or [])
        current = state.get("rxant")
        antenna_index = (options.index(current) + 1
                         if options and current in options else self._antenna)
        return {
            "freq_hz": state.get("freq_hz"),
            "mode_num": MODE_NAME_TO_NUM.get(WIRE_MODE_TO_UI.get(
                str(state.get("mode", "")).upper(), ""), 2),
            "filter_width": self._filter_index,
            "af_gain": state.get("audio_level"),
            "agc": self._agc,
            "antenna": antenna_index,
            "antenna_name": current or "",
            "antenna_list": list(state.get("antenna_list") or []),
            "antennas": self.engine.antennas_snapshot(),
            "rf_gain_raw": state.get("rf_gain") or 0.0,
            "agc_threshold": int(state.get("agc_threshold")
                                 if state.get("agc_threshold") is not None else 65),
        }


class FlexBackend(RadioBackend):
    """RadioBackend for one FlexRadio session."""

    def __init__(self, port: str = "", baud_rate: int = DEFAULT_BAUD_RATE,
                 *, host: Optional[str] = None) -> None:
        del port, baud_rate           # no serial port on this transport
        self.engine = SmartSDREngine(
            host if host is not None else getattr(config, "FLEX_HOST", ""),
            fps=int(getattr(config, "FLEX_SPECTRUM_FPS", 15)),
            on_broadcast=None)
        self._controller = FlexController(self)
        self._audio_handler: Optional[FlexAudioHandler] = None
        self.scope_producer: Optional[FlexScopeProducer] = None
        self._broadcast = None
        #: Set when the key-up self-check proved the PC-audio path silent
        #: (design §7.6).  Cleared from /settings once the operator fixed the
        #: radio; a silent carrier is worse than a refusal.
        self.tx_audio_path_unconfirmed = False

    # ── backend surface ──────────────────────────────────────────

    @property
    def cat(self) -> FlexController:
        return self._controller

    @property
    def connected(self) -> bool:
        return self.engine.connected

    @property
    def model(self) -> str:
        return MODEL_KEY

    def set_broadcast_callback(self, cb) -> None:
        self._broadcast = cb

        def _forward(field: str, value) -> None:
            try:
                cb(field, value)
            except Exception as exc:                    # noqa: BLE001
                logger.debug("FlexRadio broadcast callback failed: %s", exc)

        self.engine.set_broadcast_callback(_forward)

    async def boot_verify(self, cat, timeout: float = 0.4) -> bool:
        return self.engine.connected

    def create_audio_handler(self):
        if self._audio_handler is None:
            self._audio_handler = FlexAudioHandler(self.engine)
        return self._audio_handler

    def create_scope_producer(self, scope_handler=None, on_frame=None):
        if self.scope_producer is None:
            self.scope_producer = FlexScopeProducer(
                self.engine, scope_handler, on_frame,
                fps=float(getattr(config, "FLEX_SPECTRUM_FPS", 15)))
        return self.scope_producer

    async def init_scope(self) -> None:
        await self.engine.ensure_pan()

    @property
    def capabilities(self) -> RadioCapabilities:
        display = DISPLAY_NAME
        if self.engine.model:
            display = f"FlexRadio {self.engine.model}"
        return RadioCapabilities(
            model_name=MODEL_KEY,
            display_name=display,
            default_baud=DEFAULT_BAUD_RATE,
            audio_rx_rate=48000,
            audio_tx_rate=48000,
            audio_name_hints=(),
            has_atu=bool(self.engine.has_atu),
            has_auto_notch=True,
            has_noise_reduction=True,
            has_noise_blanker=True,
            has_compressor=True,
            has_squelch=True,
            has_rf_gain=False,
            has_split=False,
            has_vfo_b=False,
            has_radio_power=False,
            has_af_gain=True,
            has_mic_gain=True,
            has_nr_level=True,
            has_nb_level=True,
            has_vd_id_meters=False,
            vfo_b_direct=False,
            filter_model="width_table",
            att_steps=(),
            preamp_steps=(),
            scope_type="flex_vita",
            scope_spans={index: dict(entry) for index, entry in SPANS.items()},
            scope_speeds=(),
            tune_via="atu",
            verified=VERIFIED,
            tx_gated=not (config.ALLOW_UNVERIFIED_TX or VERIFIED),
            dual_rx=False,
            unverified_meters=("s_meter", "power", "swr", "alc"),
            audio_gain_boost=1.0,
        )

    @property
    def bands(self) -> list:
        return [dict(band) for band in BANDS]

    @property
    def ui_modes(self) -> list:
        return list(UI_MODES)

    @property
    def mode_name_to_num(self) -> dict:
        return dict(MODE_NAME_TO_NUM)

    def filter_tables(self) -> dict:
        return {"voice": list(VOICE_FILTER_WIDTHS),
                "narrow": list(NARROW_FILTER_WIDTHS),
                "voiceCycle": [0, 1, 2, 3, 4, 5],
                "narrowCycle": [0, 1, 2, 3],
                "narrowModes": sorted(NARROW_UI_MODES)}

    def state_tables(self) -> dict:
        return {
            "mode_num_to_name": dict(MODE_NUM_TO_NAME),
            "preamp_labels": {},
            "attenuator_labels": {},
            "get_band_for_frequency": _band_for_frequency,
            "get_filter_hz": _filter_hz,
            "raw_to_dbm": _raw_to_dbm,
            "raw_to_s_unit": _raw_to_s_unit,
            "raw_to_power": _raw_to_power,
            "raw_to_swr": _raw_to_swr,
            "raw_to_alc_pct": lambda raw: max(0, min(100, int(raw) * 100 // 255)),
            "raw_to_voltage": lambda raw: float(raw),
            "raw_to_current": lambda raw: float(raw),
        }

    def settings_poll_items(self) -> list:
        return self._controller.settings_poll_items()

    def slow_poll_items(self) -> list:
        return self._controller.slow_poll_items()

    def tx_meter_items(self) -> list:
        return self._controller.tx_meter_items()

    def always_meter_items(self) -> list:
        return self._controller.always_meter_items()

    async def initial_state_sync(self) -> dict:
        return await self._controller.initial_state_sync()


# ── Mechanical delegates for the rest of the abstract surface ─────
#
# ABCMeta freezes __abstractmethods__ at class creation, so the backend binds
# the remaining RadioBackend abstract methods onto the class at import time,
# each delegating to the controller — the same pattern as the SunSDR2 DX and
# Yaesu families.  ``FlexController`` implements every one of them;
# tests/test_flex_backend.py::ConstructionTests keeps this honest if the ABC
# grows a method the controller does not answer.
def _make_delegate(name: str):
    async def _delegate(self, *args, **kwargs):
        return await getattr(self._controller, name)(*args, **kwargs)

    _delegate.__name__ = name
    _delegate.__qualname__ = f"FlexBackend.{name}"
    return _delegate


for _name in sorted(FlexBackend.__abstractmethods__):
    setattr(FlexBackend, _name, _make_delegate(_name))

FlexBackend.__abstractmethods__ = frozenset()
