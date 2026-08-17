"""
FT-710 backend
==============
Thin ``RadioBackend`` wrapper around the Yaesu FT-710 ``CatController``.
Owns the controller instance and delegates every CAT method — no logic
here beyond the FT-710-specific scope init sequence and UI tables.

The scope machinery (FT4222 subprocess) lives in
``backends/ft710/scope_producer.py``; ``create_scope_producer()``
returns it wired to the server's shared ``ScopeHandler``.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Awaitable, Callable, Optional

from backends.base import RadioBackend, RadioCapabilities, ScopeProducer
from backends.ft710.cat_controller import CatController
from backends.ft710.config_ft710 import (
    BANDS, FILTER_WIDTHS_VOICE, FILTER_WIDTHS_NARROW, SCOPE_SPANS,
    MODE_NUM_TO_NAME, MODE_NAME_TO_NUM, PREAMP_LABELS, ATTENUATOR_LABELS,
    get_band_for_frequency, get_filter_hz,
    raw_to_dbm, raw_to_s_unit,
    raw_to_power, raw_to_swr, raw_to_voltage, raw_to_current,
)
from backends.ft710.scope_producer import FT710ScopeProducer
from config import NARROW_MODES, UI_MODES

logger = logging.getLogger("ft710.backend")


def parse_ft710_sync(raw: dict) -> dict:
    """Parse raw FT-710 CAT sync responses into RadioState field values.

    Moved out of ``RadioState.from_sync_result`` in Phase 2b — the
    backend's ``initial_state_sync()`` returns already-parsed values so
    ``radio_state`` stays radio-neutral.  Parsers are byte-identical to
    the originals.
    """
    _parsers = {
        "vfo_a_freq": lambda r: int(r[2:]) if len(r) > 2 else 0,
        "vfo_b_freq": lambda r: int(r[2:]) if len(r) > 2 else 0,
        "active_vfo": lambda r: "B" if (r and r.endswith("1")) else "A",
        "mode": lambda r: int(r[3:], 16) if len(r) >= 4 else 1,
        "tx_status": lambda r: int(r[2:]) if len(r) > 2 else 0,
        "s_meter": lambda r: int(r[3:]) if len(r) > 3 else 0,
        "filter_width": lambda r: int(r[-2:]) if len(r) >= 4 else 1,
        "af_gain_raw": lambda r: int(r[2:]) if len(r) > 2 else 128,
        "rf_power": lambda r: int(r[2:]) if len(r) > 2 else 100,
        "preamp": lambda r: int(r[3:]) if len(r) > 3 else 0,
        "attenuator": lambda r: int(r[3:]) if len(r) > 3 else 0,
        "noise_blanker": lambda r: r.endswith("1"),
        "noise_reduction": lambda r: r.endswith("1"),
        "auto_notch": lambda r: r.endswith("1"),
        # AC P1P2P3. Standard tuner: P2=0, P3=0=OFF, P3=1=ON, P3=3=Tuning
        "tuner_status": lambda r: (
            2 if len(r) > 4 and r[4] == '3' else  # P3==3 → tuning start
            1 if len(r) > 4 and r[4] == '1' else  # P3==1 → on
            0  # P3==0 → off
        ) if r and len(r) > 4 else 0,
        "power_on": lambda r: r.endswith("1"),
        "scope_on": lambda r: int(r[4:]) == 1 if r and len(r) >= 5 else True,
        "antenna": lambda r: int(r[2:]) if r and len(r) >= 3 else 1,
        "agc": lambda r: int(r[2:]) if r and len(r) >= 4 else 1,
        "dnr_level": lambda r: int(r[2:5]) if r and len(r) >= 5 else 0,
        "contour_level": lambda r: int(r[2:5]) if r and len(r) >= 5 else 0,
        "meter_display": lambda r: int(r[2]) if r and len(r) >= 3 else 0,
        "amc_level": lambda r: int(r[2:5]) if r and len(r) >= 5 else 50,
        "rf_gain": lambda r: int(r[2:]) if r and len(r) > 2 else 255,
    }
    state: dict = {}
    for field_name, raw_value in (raw or {}).items():
        if field_name == "ri":
            # RI0 response: "RI0" + 7 single-char fields
            try:
                tail = raw_value[3:] if raw_value.startswith("RI0") else raw_value
                if len(tail) >= 7:
                    state["hi_swr"] = tail[0] == '1'
                    state["recording_status"] = int(tail[1]) if tail[1].isdigit() else 0
                    state["rx_tx_status"] = int(tail[2]) if tail[2].isdigit() else 0
                    state["tuner_tuning"] = tail[4] == '1'
                    state["scan_status"] = int(tail[5]) if tail[5].isdigit() else 0
                    state["squelch_open"] = tail[6] == '1'
            except (ValueError, IndexError):
                pass
            continue
        if field_name in _parsers and raw_value:
            try:
                value = _parsers[field_name](raw_value)
            except (ValueError, IndexError):
                continue
            # Map the raw query key onto the RadioState field name.
            state["af_gain" if field_name == "af_gain_raw" else field_name] = value
    return state


class FT710Backend(RadioBackend):
    """RadioBackend implementation for the Yaesu FT-710."""

    def __init__(self, port: str, baud_rate: int = 38400):
        self._cat = CatController(port, baud_rate)

    @property
    def capabilities(self) -> RadioCapabilities:
        return RadioCapabilities(
            model_name="ft710",
            display_name="Yaesu FT-710",
            default_baud=38400,
            audio_rx_rate=44100,
            audio_tx_rate=44100,
            audio_name_hints=("ft-710", "ft710", "yaesu",
                              "usb audio codec", "usb audio device"),
            has_atu=True,
            has_auto_notch=True,
            has_vd_id_meters=True,
            vfo_b_direct=True,
            filter_model="width_table",
            att_steps=(0, 6, 12, 18),
            preamp_steps=("OFF", "AMP1", "AMP2"),
            scope_type="ft4222",
            scope_spans=SCOPE_SPANS,
            tune_via="tx2",
        )

    def create_scope_producer(
        self,
        scope_handler=None,
        on_frame: Optional[Callable[[object], Awaitable[None]]] = None,
    ) -> Optional[ScopeProducer]:
        # Repo root = backends/ft710/backend.py → parents[2]; the pipe
        # subprocess is spawned with this as cwd.
        repo_root = Path(__file__).resolve().parents[2]
        return FT710ScopeProducer(repo_root, scope_handler, on_frame)

    # ── UI Tables ──────────────────────────────────────────────────

    @property
    def bands(self) -> list:
        return BANDS

    @property
    def ui_modes(self) -> list:
        return UI_MODES

    @property
    def mode_name_to_num(self) -> dict:
        return MODE_NAME_TO_NUM

    def filter_tables(self) -> dict:
        return {
            "voice": FILTER_WIDTHS_VOICE,
            "narrow": FILTER_WIDTHS_NARROW,
            "narrowModes": sorted(NARROW_MODES),
        }

    # ── Neutral-Layer Hooks (radio_state / poll_scheduler) ─────────

    def state_tables(self) -> dict:
        """FT-710 tables for RadioState.configure() (same as its defaults)."""
        return {
            "mode_num_to_name": MODE_NUM_TO_NAME,
            "preamp_labels": PREAMP_LABELS,
            "attenuator_labels": ATTENUATOR_LABELS,
            "get_band_for_frequency": get_band_for_frequency,
            "get_filter_hz": get_filter_hz,
            "raw_to_dbm": raw_to_dbm,
            "raw_to_s_unit": raw_to_s_unit,
            "raw_to_power": raw_to_power,
            "raw_to_swr": raw_to_swr,
            "raw_to_voltage": raw_to_voltage,
            "raw_to_current": raw_to_current,
        }

    def settings_poll_items(self) -> list:
        """2s-tier settings items — the 14 FT-710 queries formerly inlined
        in poll_scheduler._poll_settings, parsers unchanged."""
        cat = self._cat

        def item(cmd, parser):
            async def getter(timeout=None):
                resp = await cat.query(cmd, timeout=timeout)
                if not resp:
                    return None
                try:
                    return parser(resp)
                except (ValueError, IndexError):
                    return None
            return getter

        return [
            ("filter_width", item("SH0", lambda r: int(r[-2:]) if len(r) >= 4 else None)),
            ("af_gain", item("AG0", lambda r: int(r[2:]) if len(r) > 2 else None)),
            ("rf_gain", item("RG0", lambda r: int(r[2:]) if len(r) > 2 else None)),
            ("rf_power", item("PC", lambda r: int(r[2:]) if len(r) > 2 else None)),
            ("preamp", item("PA0", lambda r: int(r[3:]) if len(r) > 3 else None)),
            ("attenuator", item("RA0", lambda r: int(r[3:]) if len(r) > 3 else None)),
            ("noise_blanker", item("NB0", lambda r: r.endswith("1") if r else False)),
            ("noise_reduction", item("NR0", lambda r: r.endswith("1") if r else False)),
            ("auto_notch", item("BC", lambda r: r.endswith("1") if r else False)),
            # PS (radio power) — keeps power_on truthful when the radio is
            # switched on/off at the front panel.  No response while the
            # radio is off; the None guard skips that case.
            ("power_on", item("PS", lambda r: r.endswith("1") if r else False)),
            # AC returns P1P2P3. Standard tuner: P2=0, P3=0=OFF, P3=1=ON, P3=3=Tuning
            ("tuner_status", item("AC", lambda r: (
                2 if len(r) > 4 and r[4] == '3' else  # P3==3 → tuning start
                1 if len(r) > 4 and r[4] == '1' else  # P3==1 → on
                0  # P3==0 → off
            ) if r and len(r) > 4 else None)),
            ("scope_on", item("SS01", lambda r: int(r[4:]) == 1 if r and len(r) >= 5 else None)),
            ("antenna", item("AN", lambda r: int(r[2:]) if r and len(r) >= 3 else None)),
            ("agc", item("GT", lambda r: int(r[2:]) if r and len(r) >= 4 else None)),
            ("meter_display", item("MS", lambda r: int(r[2]) if r and len(r) >= 4 else None)),
            # DO NOT poll "DN" — on the FT-710, "DN;" is NOT a DNR query;
            # it is the "step active VFO DOWN one tuning step" command (~20 Hz).
        ]

    def slow_poll_items(self) -> list:
        """5s-tier items — the 4 FT-710 queries formerly inlined in
        poll_scheduler._poll_slow (compressor, contour, AMC, RI info)."""
        cat = self._cat

        async def get_compressor(timeout=None):
            resp = await cat.query("PR", timeout=timeout)
            if resp and isinstance(resp, str):
                # PR P1P2: P2=0=OFF, P2=1=ON (matches all other FT-710
                # binary commands despite the Yaesu PDF errata)
                return {"compressor": resp.endswith("1")}
            return {}

        async def get_contour(timeout=None):
            resp = await cat.query("CO", timeout=timeout)
            if resp and len(resp) >= 5:
                try:
                    return {"contour_level": int(resp[2:5])}
                except ValueError:
                    pass
            return {}

        async def get_amc(timeout=None):
            v = await cat.get_amc_level(timeout=timeout)
            return {"amc_level": v} if v is not None else {}

        async def get_ri(timeout=None):
            # Radio Information (RI) — Hi-SWR, recording, RX/TX,
            # tuner tuning, scan, squelch status
            info = await cat.get_radio_info(timeout=timeout)
            return info if info else {}

        return [
            ("compressor", get_compressor),
            ("contour_level", get_contour),
            ("amc_level", get_amc),
            ("ri", get_ri),
        ]

    def tx_meter_items(self) -> list:
        """TX-only meters (RM3-RM7): COMP, ALC, Power, SWR, drain current.
        RM7 (drain current) only responds during TX."""
        cat = self._cat
        return [
            ("COMP", "comp_meter", lambda t: cat.get_meter("RM3", timeout=t)),
            ("ALC", "alc_meter", lambda t: cat.get_meter("RM4", timeout=t)),
            ("PWR", "power_meter", lambda t: cat.get_meter("RM5", timeout=t)),
            ("SWR", "swr_meter", lambda t: cat.get_meter("RM6", timeout=t)),
            ("ID", "id_meter", lambda t: cat.get_meter("RM7", timeout=t)),
        ]

    def always_meter_items(self) -> list:
        """Always-on meter: RM8 drain voltage.  Unlike RM7, this responds
        during RX, giving a live power-supply reading."""
        cat = self._cat
        return [
            ("VD", "vd_meter", lambda t: cat.get_meter("RM8", timeout=t)),
        ]

    # ── Scope Init Hook ────────────────────────────────────────────

    async def init_scope(self) -> None:
        """Send scope-initialization CAT commands via the CAT serial port.

        These extended commands configure the FT-710's scope display engine.
        Adapted from wfview's yaesuUdpControl scope init sequence.

        Commands are sent as extended CAT register writes. The FT-710 needs
        these to enable scope data output on the FT4222 SPI bus.

        Attempts scope init even when CAT reports as not-connected — the
        serial port may be open but the radio didn't respond to ID;.
        """
        cat = self._cat

        # Try to connect if not already connected (scope may work even
        # without full radio response to ID;)
        if not cat.connected:
            logger.info("CAT not connected — attempting scope-init anyway")
            try:
                await cat.connect()
            except Exception as e:
                logger.warning("CAT connect failed for scope-init: %s", e)

        # Check if serial port is actually open (CatController uses _ser)
        serial_port = getattr(cat, '_ser', None) or getattr(cat, 'serial', None)
        if serial_port is None or not getattr(serial_port, 'is_open', False):
            logger.warning("CAT serial port not open — scope-init unavailable")
            return

        logger.info("Sending scope-init CAT commands...")

        # Extended CAT commands to initialize the scope display.
        # These write to the FT-710's internal registers to enable
        # the scope data stream on the FT4222 SPI interface.
        scope_cmds = [
            # Enable scope data output on FT4222 SPI
            # EX = extended command prefix
            "EX040101",
            # Set scope to CENTER mode (not FIX mode)
            "EX040200",
        ]
        for cmd in scope_cmds:
            try:
                await cat.send_command(cmd)
                await asyncio.sleep(0.05)
                logger.debug("Scope-init sent: %s", cmd)
            except Exception as e:
                logger.warning("Scope-init cmd %s error: %s", cmd, e)

        logger.info("Scope-init CAT commands complete")

    @property
    def cat(self) -> CatController:
        """Direct access to the wrapped controller (transition helper)."""
        return self._cat

    # ── Connection Management ──────────────────────────────────────

    @property
    def connected(self) -> bool:
        return self._cat.connected

    @property
    def model(self) -> str:
        return self._cat.model

    async def connect(self) -> bool:
        return await self._cat.connect()

    async def disconnect(self) -> None:
        await self._cat.disconnect()

    async def reconnect_loop(self) -> bool:
        return await self._cat.reconnect_loop()

    # ── Command Interface ──────────────────────────────────────────

    async def send_command(self, cmd: str, timeout: Optional[float] = None) -> Optional[str]:
        return await self._cat.send_command(cmd, timeout=timeout)

    async def send_set_command(self, cmd: str) -> bool:
        return await self._cat.send_set_command(cmd)

    async def send_priority_set_command(self, cmd: str) -> bool:
        return await self._cat.send_priority_set_command(cmd)

    async def query(self, cmd: str, timeout: Optional[float] = None) -> Optional[str]:
        return await self._cat.query(cmd, timeout=timeout)

    async def set(self, cmd: str) -> bool:
        return await self._cat.set(cmd)

    # ── High-Level Command Helpers ─────────────────────────────────

    async def set_frequency(self, freq_hz: int, vfo: str = "A") -> bool:
        return await self._cat.set_frequency(freq_hz, vfo=vfo)

    async def get_active_vfo(self, timeout: Optional[float] = None) -> Optional[str]:
        return await self._cat.get_active_vfo(timeout=timeout)

    async def get_frequency(self, vfo: str = "A", timeout: Optional[float] = None) -> Optional[int]:
        return await self._cat.get_frequency(vfo=vfo, timeout=timeout)

    async def set_mode(self, mode_num: int) -> bool:
        return await self._cat.set_mode(mode_num)

    async def get_mode(self, timeout: Optional[float] = None) -> Optional[int]:
        return await self._cat.get_mode(timeout=timeout)

    async def set_ptt(self, tx: bool) -> bool:
        return await self._cat.set_ptt(tx)

    async def set_tune(self, tune: bool) -> bool:
        return await self._cat.set_tune(tune)

    async def get_ptt(self, timeout: Optional[float] = None) -> Optional[int]:
        return await self._cat.get_ptt(timeout=timeout)

    async def get_s_meter(self, timeout: Optional[float] = None) -> Optional[int]:
        return await self._cat.get_s_meter(timeout=timeout)

    async def get_info(self) -> Optional[dict]:
        return await self._cat.get_info()

    async def get_meter(self, meter: str, timeout: Optional[float] = None) -> Optional[int]:
        return await self._cat.get_meter(meter, timeout=timeout)

    async def set_filter_width(self, index: int) -> bool:
        return await self._cat.set_filter_width(index)

    async def get_filter_width(self) -> Optional[int]:
        return await self._cat.get_filter_width()

    async def set_af_gain(self, value: int) -> bool:
        return await self._cat.set_af_gain(value)

    async def set_rf_gain(self, value: int) -> bool:
        return await self._cat.set_rf_gain(value)

    async def set_rf_power(self, value: int) -> bool:
        return await self._cat.set_rf_power(value)

    async def set_preamp(self, value: int) -> bool:
        return await self._cat.set_preamp(value)

    async def set_attenuator(self, value: int) -> bool:
        return await self._cat.set_attenuator(value)

    async def set_noise_blanker(self, on: bool) -> bool:
        return await self._cat.set_noise_blanker(on)

    async def set_noise_reduction(self, on: bool) -> bool:
        return await self._cat.set_noise_reduction(on)

    async def set_auto_notch(self, on: bool) -> bool:
        return await self._cat.set_auto_notch(on)

    async def set_compressor(self, on: bool) -> bool:
        return await self._cat.set_compressor(on)

    async def set_tuner(self, value: int) -> bool:
        return await self._cat.set_tuner(value)

    async def set_vfo(self, vfo: str) -> bool:
        return await self._cat.set_vfo(vfo)

    async def set_split(self, on: bool) -> bool:
        return await self._cat.set_split(on)

    async def set_power(self, on: bool) -> bool:
        return await self._cat.set_power(on)

    async def set_squelch(self, value: int) -> bool:
        return await self._cat.set_squelch(value)

    async def set_mic_gain(self, value: int) -> bool:
        return await self._cat.set_mic_gain(value)

    async def set_band_stack(self, bsr: int) -> bool:
        return await self._cat.set_band_stack(bsr)

    async def set_antenna(self, ant: int) -> bool:
        return await self._cat.set_antenna(ant)

    async def get_antenna(self) -> Optional[int]:
        return await self._cat.get_antenna()

    async def set_agc(self, value: int) -> bool:
        return await self._cat.set_agc(value)

    async def get_agc(self) -> Optional[int]:
        return await self._cat.get_agc()

    async def set_dnr(self, value: int) -> bool:
        return await self._cat.set_dnr(value)

    async def get_dnr(self) -> Optional[int]:
        return await self._cat.get_dnr()

    async def set_contour(self, value: int) -> bool:
        return await self._cat.set_contour(value)

    async def get_contour(self) -> Optional[int]:
        return await self._cat.get_contour()

    async def set_drive(self, value: int) -> bool:
        return await self._cat.set_drive(value)

    # ── Meter & Radio Info Commands ─────────────────────────────────

    async def set_meter_display(self, meter: int) -> bool:
        return await self._cat.set_meter_display(meter)

    async def get_meter_display(self, timeout: Optional[float] = None) -> Optional[int]:
        return await self._cat.get_meter_display(timeout=timeout)

    async def set_amc_level(self, level: int) -> bool:
        return await self._cat.set_amc_level(level)

    async def get_amc_level(self, timeout: Optional[float] = None) -> Optional[int]:
        return await self._cat.get_amc_level(timeout=timeout)

    async def get_radio_info(self, timeout: Optional[float] = None) -> Optional[dict]:
        return await self._cat.get_radio_info(timeout=timeout)

    # ── Scope/Spectrum Commands ────────────────────────────────────

    async def set_scope_on(self, on: bool) -> bool:
        return await self._cat.set_scope_on(on)

    async def get_scope_on(self) -> Optional[int]:
        return await self._cat.get_scope_on()

    async def set_scope_span(self, span: int) -> bool:
        return await self._cat.set_scope_span(span)

    async def set_scope_speed(self, speed: int) -> bool:
        return await self._cat.set_scope_speed(speed)

    async def set_scope_mode(self, mode: int) -> bool:
        return await self._cat.set_scope_mode(mode)

    # ── Misc Settings ──────────────────────────────────────────────

    async def set_nb_level(self, level: int) -> bool:
        return await self._cat.set_nb_level(level)

    async def set_nr_level(self, level: int) -> bool:
        return await self._cat.set_nr_level(level)

    async def set_compressor_level(self, level: int) -> bool:
        return await self._cat.set_compressor_level(level)

    async def set_monitor(self, on: bool) -> bool:
        return await self._cat.set_monitor(on)

    async def set_monitor_gain(self, value: int) -> bool:
        return await self._cat.set_monitor_gain(value)

    async def set_vox(self, on: bool) -> bool:
        return await self._cat.set_vox(on)

    async def set_break_in(self, on: bool) -> bool:
        return await self._cat.set_break_in(on)

    async def set_key_speed(self, speed: int) -> bool:
        return await self._cat.set_key_speed(speed)

    async def set_cw_pitch(self, pitch: int) -> bool:
        return await self._cat.set_cw_pitch(pitch)

    async def set_rit(self, on: bool) -> bool:
        return await self._cat.set_rit(on)

    async def set_rit_freq(self, freq: int) -> bool:
        return await self._cat.set_rit_freq(freq)

    async def set_xit(self, on: bool) -> bool:
        return await self._cat.set_xit(on)

    # ── Bulk State Query ──────────────────────────────────────────

    async def initial_state_sync(self) -> dict:
        """Full state read; returns PARSED RadioState field values."""
        raw = await self._cat.initial_state_sync()
        return parse_ft710_sync(raw)
