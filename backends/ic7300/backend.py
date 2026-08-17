"""
IC-7300 backend
===============
Thin ``RadioBackend`` wrapper around the Icom CI-V ``CivController``
(patterned on ``backends/ft710/backend.py``).  Owns the controller,
the static capabilities, the IC-7300 UI tables, the scope-enable CAT
sequence, and the poll-item tables consumed by ``poll_scheduler.py``.

The scope stream (0x27 0x00 segments collected in
``CivController.scope_queue``) is consumed by the in-process
``CivScopeProducer`` (``backends/ic7300/civ_scope.py``);
``create_scope_producer()`` returns it wired to the server's shared
``ScopeHandler``.
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable, Optional

from backends.base import RadioBackend, RadioCapabilities, ScopeProducer
from backends.ic7300.civ_controller import (
    CivController, CMD_LEVEL, CMD_POWER,
    LVL_AF, LVL_RF_GAIN, LVL_SQL, LVL_RF_POWER, LVL_MIC,
    SW_PREAMP, SW_NB, SW_NR, SW_COMP,
)
from backends.ic7300.civ_scope import CivScopeProducer
from backends.ic7300.config_ic7300 import (
    BANDS, MODE_NUM_TO_NAME, MODE_NAME_TO_NUM, PREAMP_LABELS,
    FIL_DEFAULT_WIDTHS_HZ, SCOPE_SPANS, get_band_for_frequency,
    raw_to_dbm, raw_to_s_unit, raw_to_power, raw_to_swr,
    raw_to_voltage, raw_to_current,
)
from config import NARROW_MODES

logger = logging.getLogger("ic7300.backend")

# Primary modes exposed in the UI cycle button (in order): the shared
# config.UI_MODES list minus DATA-L, which the IC-7300 does not have.
UI_MODES_IC7300 = ["LSB", "USB", "CW-U", "AM", "FM", "RTTY-L"]

# Default scope span index (±100 kHz — see SCOPE_SPAN_HZ).
DEFAULT_SCOPE_SPAN = 5

# RadioState.attenuator stores the UI index (0/1), matching the FT-710's
# index semantics so server.py's (0,1,2,3) validation keeps working.
_ATTENUATOR_INDEX_LABELS = {0: "OFF", 1: "20dB"}


def _ic7300_filter_hz(mode_name: str, fil_index: int) -> Optional[int]:
    """FIL1-3 default width (Hz) for the given mode — fil123 model."""
    widths = FIL_DEFAULT_WIDTHS_HZ.get(mode_name)
    if widths is None or not 1 <= fil_index <= len(widths):
        return None
    return widths[fil_index - 1]


class IC7300Backend(RadioBackend):
    """RadioBackend implementation for the Icom IC-7300."""

    _display_name = "Icom IC-7300"

    def __init__(self, port: str, baud_rate: int = 115200):
        self._civ = CivController(port, baud_rate)

    @property
    def capabilities(self) -> RadioCapabilities:
        return RadioCapabilities(
            model_name="ic7300",
            display_name=self._display_name,
            default_baud=115200,
            audio_rx_rate=48000,
            audio_tx_rate=48000,
            audio_name_hints=("ic-7300", "ic7300",
                              "usb audio codec", "usb audio device"),
            has_atu=True,
            has_auto_notch=False,
            has_vd_id_meters=False,
            vfo_b_direct=False,
            filter_model="fil123",
            att_steps=(0, 20),
            preamp_steps=("OFF", "AMP1", "AMP2"),
            scope_type="civ27",
            scope_spans=SCOPE_SPANS,
            tune_via="atu",
        )

    def create_scope_producer(
        self,
        scope_handler=None,
        on_frame: Optional[Callable[[object], Awaitable[None]]] = None,
    ) -> Optional[ScopeProducer]:
        # In-process consumer of CivController.scope_queue (no
        # subprocess — the waveform arrives as CI-V frames on the CAT
        # port the controller already owns).
        return CivScopeProducer(self._civ, scope_handler, on_frame)

    # ── UI Tables ──────────────────────────────────────────────────

    @property
    def bands(self) -> list:
        return BANDS

    @property
    def ui_modes(self) -> list:
        return UI_MODES_IC7300

    @property
    def mode_name_to_num(self) -> dict:
        return MODE_NAME_TO_NUM

    def filter_tables(self) -> dict:
        """FIL1-3 selection model: the "width index" is the FIL number.

        Same outer shape as the FT-710 table so server.py stays generic;
        voice/narrow hold (fil, default_hz) pairs and the extra keys let
        the frontend render per-mode defaults.
        """
        return {
            "voice": [(i + 1, w) for i, w in enumerate(FIL_DEFAULT_WIDTHS_HZ["USB"])],
            "narrow": [(i + 1, w) for i, w in enumerate(FIL_DEFAULT_WIDTHS_HZ["CW-U"])],
            "narrowModes": sorted(NARROW_MODES & set(MODE_NUM_TO_NAME.values())),
            "model": "fil123",
            "filDefaults": FIL_DEFAULT_WIDTHS_HZ,
        }

    # ── RadioState table injection ─────────────────────────────────

    def state_tables(self) -> dict:
        """Tables for RadioState.configure() (IC-7300 calibrations)."""
        return {
            "mode_num_to_name": MODE_NUM_TO_NAME,
            "preamp_labels": PREAMP_LABELS,
            "attenuator_labels": _ATTENUATOR_INDEX_LABELS,
            "get_band_for_frequency": get_band_for_frequency,
            "get_filter_hz": _ic7300_filter_hz,
            "raw_to_dbm": raw_to_dbm,
            "raw_to_s_unit": raw_to_s_unit,
            "raw_to_power": raw_to_power,
            "raw_to_swr": raw_to_swr,
            "raw_to_voltage": raw_to_voltage,
            "raw_to_current": raw_to_current,
        }

    # ── Poll items (consumed by poll_scheduler.py) ─────────────────

    def settings_poll_items(self) -> list:
        """2s-tier items: [(field, async getter(timeout) -> value|None)].

        IC-7300 equivalents of the FT-710's SH0/AG0/RG0/PC/PA0/RA0/NB0/
        NR0/BC/PS/AC/SS01 settings table.  There is no AN/GT/MS on this
        radio — those fields simply stay absent from RadioState updates.
        """
        civ = self._civ
        return [
            ("filter_width", lambda t: civ.get_filter_width()),
            ("af_gain", lambda t: civ._level_query(CMD_LEVEL, LVL_AF, timeout=t)),
            ("rf_gain", lambda t: civ._level_query(CMD_LEVEL, LVL_RF_GAIN, timeout=t)),
            ("rf_power", self._get_rf_power_pct),
            ("preamp", lambda t: civ._switch_query(SW_PREAMP, timeout=t)),
            ("attenuator", lambda t: civ._get_attenuator()),
            ("noise_blanker", self._get_nb),
            ("noise_reduction", self._get_nr),
            ("compressor", self._get_comp),
            ("power_on", self._get_power_on),
            ("tuner_status", lambda t: civ._get_tuner()),
            ("scope_on", self._get_scope_on_bool),
            ("agc", lambda t: civ.get_agc()),
        ]

    def slow_poll_items(self) -> list:
        """5s-tier items: [(skip_key, async getter(timeout) -> dict)]."""
        civ = self._civ
        return [
            ("squelch", self._slow_squelch),
            ("mic_gain", self._slow_mic_gain),
        ]

    def tx_meter_items(self) -> list:
        """TX-only meters: [(label, field, async getter(timeout))].

        No Vd/Id items — capabilities.has_vd_id_meters is False.
        """
        civ = self._civ
        return [
            ("COMP", "comp_meter", lambda t: civ.get_meter("comp", timeout=t)),
            ("ALC", "alc_meter", lambda t: civ.get_meter("alc", timeout=t)),
            ("PWR", "power_meter", lambda t: civ.get_meter("po", timeout=t)),
            ("SWR", "swr_meter", lambda t: civ.get_meter("swr", timeout=t)),
        ]

    def always_meter_items(self) -> list:
        """Always-on meters — none on the IC-7300 (no Vd equivalent polled)."""
        return []

    async def _get_rf_power_pct(self, timeout=None) -> Optional[int]:
        raw = await self._civ._level_query(CMD_LEVEL, LVL_RF_POWER, timeout=timeout)
        return None if raw is None else CivController._raw_to_pct(raw)

    async def _get_nb(self, timeout=None) -> Optional[bool]:
        v = await self._civ._switch_query(SW_NB, timeout=timeout)
        return None if v is None else bool(v)

    async def _get_nr(self, timeout=None) -> Optional[bool]:
        v = await self._civ._switch_query(SW_NR, timeout=timeout)
        return None if v is None else bool(v)

    async def _get_comp(self, timeout=None) -> Optional[bool]:
        v = await self._civ._switch_query(SW_COMP, timeout=timeout)
        return None if v is None else bool(v)

    async def _get_power_on(self, timeout=None) -> Optional[bool]:
        """Power query (0x18); no response while the radio is off."""
        data = await self._civ._query_data(CMD_POWER, timeout=timeout)
        if data is None or not data:
            return None
        return bool(data[0])

    async def _get_scope_on_bool(self, timeout=None) -> Optional[bool]:
        v = await self._civ.get_scope_on()
        return None if v is None else v == 1

    async def _slow_squelch(self, timeout=None) -> dict:
        raw = await self._civ._level_query(CMD_LEVEL, LVL_SQL, timeout=timeout)
        if raw is None:
            return {}
        return {"squelch": CivController._raw_to_pct(raw)}

    async def _slow_mic_gain(self, timeout=None) -> dict:
        raw = await self._civ._level_query(CMD_LEVEL, LVL_MIC, timeout=timeout)
        if raw is None:
            return {}
        return {"mic_gain": CivController._raw_to_pct(raw)}

    # ── Scope Init Hook ────────────────────────────────────────────

    async def init_scope(self) -> None:
        """Enable the CI-V scope stream (called at startup + on reconnect).

        Sequence: center mode (27 14 00) → default span (27 15) → scope
        data output ON (27 11 01 — the switch that starts the 0x27 0x00
        waveform segments; 27 10 only toggles the radio's own display).
        """
        civ = self._civ
        if not civ.connected:
            logger.info("CAT not connected — attempting scope-init anyway")
            try:
                await civ.connect()
            except Exception as e:
                logger.warning("CAT connect failed for scope-init: %s", e)
        if not civ.connected:
            logger.warning("CAT not connected — scope-init unavailable")
            return
        for desc, coro in (
            ("center mode", civ.set_scope_mode(0)),
            ("default span", civ.set_scope_span(DEFAULT_SCOPE_SPAN)),
            ("data output on", civ.set_scope_data_output(True)),
        ):
            try:
                await coro
                logger.debug("Scope-init sent: %s", desc)
            except Exception as e:
                logger.warning("Scope-init %s error: %s", desc, e)
        logger.info("Scope-init CI-V commands complete")

    @property
    def cat(self) -> CivController:
        """Direct access to the wrapped controller (transition helper)."""
        return self._civ

    # ── Connection Management ──────────────────────────────────────

    @property
    def connected(self) -> bool:
        return self._civ.connected

    @property
    def model(self) -> str:
        return self._civ.model

    async def connect(self) -> bool:
        return await self._civ.connect()

    async def disconnect(self) -> None:
        await self._civ.disconnect()

    async def reconnect_loop(self) -> bool:
        return await self._civ.reconnect_loop()

    def set_broadcast_callback(self, cb) -> None:
        self._civ.set_broadcast_callback(cb)

    # ── Command Interface ──────────────────────────────────────────

    async def send_command(self, cmd, timeout: Optional[float] = None):
        return await self._civ.send_command(cmd, timeout=timeout)

    async def send_set_command(self, cmd) -> bool:
        return await self._civ.send_set_command(cmd)

    async def send_priority_set_command(self, cmd) -> bool:
        return await self._civ.send_priority_set_command(cmd)

    async def query(self, cmd, timeout: Optional[float] = None):
        return await self._civ.query(cmd, timeout=timeout)

    async def set(self, cmd) -> bool:
        return await self._civ.set(cmd)

    # ── High-Level Command Helpers ─────────────────────────────────

    async def set_frequency(self, freq_hz: int, vfo: str = "A") -> bool:
        return await self._civ.set_frequency(freq_hz, vfo=vfo)

    async def get_active_vfo(self, timeout: Optional[float] = None) -> Optional[str]:
        return await self._civ.get_active_vfo(timeout=timeout)

    async def get_frequency(self, vfo: str = "A", timeout: Optional[float] = None) -> Optional[int]:
        return await self._civ.get_frequency(vfo=vfo, timeout=timeout)

    async def set_mode(self, mode_num: int) -> bool:
        return await self._civ.set_mode(mode_num)

    async def get_mode(self, timeout: Optional[float] = None) -> Optional[int]:
        return await self._civ.get_mode(timeout=timeout)

    async def set_ptt(self, tx: bool) -> bool:
        return await self._civ.set_ptt(tx)

    async def set_tune(self, tune: bool) -> bool:
        return await self._civ.set_tune(tune)

    async def get_ptt(self, timeout: Optional[float] = None) -> Optional[int]:
        return await self._civ.get_ptt(timeout=timeout)

    async def get_s_meter(self, timeout: Optional[float] = None) -> Optional[int]:
        return await self._civ.get_s_meter(timeout=timeout)

    async def get_info(self) -> Optional[dict]:
        return await self._civ.get_info()

    async def get_meter(self, meter: str, timeout: Optional[float] = None) -> Optional[int]:
        return await self._civ.get_meter(meter, timeout=timeout)

    async def set_filter_width(self, index: int) -> bool:
        return await self._civ.set_filter_width(index)

    async def get_filter_width(self) -> Optional[int]:
        return await self._civ.get_filter_width()

    async def set_af_gain(self, value: int) -> bool:
        return await self._civ.set_af_gain(value)

    async def set_rf_gain(self, value: int) -> bool:
        return await self._civ.set_rf_gain(value)

    async def set_rf_power(self, value: int) -> bool:
        return await self._civ.set_rf_power(value)

    async def set_preamp(self, value: int) -> bool:
        return await self._civ.set_preamp(value)

    async def set_attenuator(self, value: int) -> bool:
        return await self._civ.set_attenuator(value)

    async def set_noise_blanker(self, on: bool) -> bool:
        return await self._civ.set_noise_blanker(on)

    async def set_noise_reduction(self, on: bool) -> bool:
        return await self._civ.set_noise_reduction(on)

    async def set_auto_notch(self, on: bool) -> bool:
        return await self._civ.set_auto_notch(on)

    async def set_compressor(self, on: bool) -> bool:
        return await self._civ.set_compressor(on)

    async def set_tuner(self, value: int) -> bool:
        return await self._civ.set_tuner(value)

    async def set_vfo(self, vfo: str) -> bool:
        return await self._civ.set_vfo(vfo)

    async def set_split(self, on: bool) -> bool:
        return await self._civ.set_split(on)

    async def set_power(self, on: bool) -> bool:
        return await self._civ.set_power(on)

    async def set_squelch(self, value: int) -> bool:
        return await self._civ.set_squelch(value)

    async def set_mic_gain(self, value: int) -> bool:
        return await self._civ.set_mic_gain(value)

    async def set_band_stack(self, bsr: int) -> bool:
        return await self._civ.set_band_stack(bsr)

    async def set_antenna(self, ant: int) -> bool:
        return await self._civ.set_antenna(ant)

    async def get_antenna(self) -> Optional[int]:
        return await self._civ.get_antenna()

    async def set_agc(self, value: int) -> bool:
        return await self._civ.set_agc(value)

    async def get_agc(self) -> Optional[int]:
        return await self._civ.get_agc()

    async def set_dnr(self, value: int) -> bool:
        return await self._civ.set_dnr(value)

    async def get_dnr(self) -> Optional[int]:
        return await self._civ.get_dnr()

    async def set_contour(self, value: int) -> bool:
        return await self._civ.set_contour(value)

    async def get_contour(self) -> Optional[int]:
        return await self._civ.get_contour()

    async def set_drive(self, value: int) -> bool:
        return await self._civ.set_drive(value)

    # ── Meter & Radio Info Commands ─────────────────────────────────

    async def set_meter_display(self, meter: int) -> bool:
        return await self._civ.set_meter_display(meter)

    async def get_meter_display(self, timeout: Optional[float] = None) -> Optional[int]:
        return await self._civ.get_meter_display(timeout=timeout)

    async def set_amc_level(self, level: int) -> bool:
        return await self._civ.set_amc_level(level)

    async def get_amc_level(self, timeout: Optional[float] = None) -> Optional[int]:
        return await self._civ.get_amc_level(timeout=timeout)

    async def get_radio_info(self, timeout: Optional[float] = None) -> Optional[dict]:
        return await self._civ.get_radio_info(timeout=timeout)

    # ── Scope/Spectrum Commands ────────────────────────────────────

    async def set_scope_on(self, on: bool) -> bool:
        return await self._civ.set_scope_on(on)

    async def get_scope_on(self) -> Optional[int]:
        return await self._civ.get_scope_on()

    async def set_scope_span(self, span: int) -> bool:
        return await self._civ.set_scope_span(span)

    async def set_scope_speed(self, speed: int) -> bool:
        return await self._civ.set_scope_speed(speed)

    async def set_scope_mode(self, mode: int) -> bool:
        return await self._civ.set_scope_mode(mode)

    # ── Misc Settings ──────────────────────────────────────────────

    async def set_nb_level(self, level: int) -> bool:
        return await self._civ.set_nb_level(level)

    async def set_nr_level(self, level: int) -> bool:
        return await self._civ.set_nr_level(level)

    async def set_compressor_level(self, level: int) -> bool:
        return await self._civ.set_compressor_level(level)

    async def set_monitor(self, on: bool) -> bool:
        return await self._civ.set_monitor(on)

    async def set_monitor_gain(self, value: int) -> bool:
        return await self._civ.set_monitor_gain(value)

    async def set_vox(self, on: bool) -> bool:
        return await self._civ.set_vox(on)

    async def set_break_in(self, on: bool) -> bool:
        return await self._civ.set_break_in(on)

    async def set_key_speed(self, speed: int) -> bool:
        return await self._civ.set_key_speed(speed)

    async def set_cw_pitch(self, pitch: int) -> bool:
        return await self._civ.set_cw_pitch(pitch)

    async def set_rit(self, on: bool) -> bool:
        return await self._civ.set_rit(on)

    async def set_rit_freq(self, freq: int) -> bool:
        return await self._civ.set_rit_freq(freq)

    async def set_xit(self, on: bool) -> bool:
        return await self._civ.set_xit(on)

    # ── Bulk State Query ──────────────────────────────────────────

    async def initial_state_sync(self) -> dict:
        return await self._civ.initial_state_sync()


class IC7300MK2Backend(IC7300Backend):
    """IC-7300MK2 — same CI-V surface as the IC-7300.

    NOTE: the MK2's factory CI-V address is 0xB6 (hamlib ic7300.c) and
    its "CI-V Transceive" set-mode item is 0089, not 0071.  Both are
    hardware-verify items; override IC7300_CIV_ADDR via env if needed.
    TODO(hw-verify)
    """

    _display_name = "Icom IC-7300MK2"
