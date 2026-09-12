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

import config
from backends.base import RadioBackend, RadioCapabilities, ScopeProducer
from backends.ic7300.civ_controller import (
    CivController, CMD_LEVEL,
    LVL_AF, LVL_RF_GAIN, LVL_SQL, LVL_RF_POWER, LVL_MIC,
    SETMODE_CIV_TRANSCEIVE_ON, SETMODE_CIV_TRANSCEIVE_MK2,
    SW_PREAMP, SW_NB, SW_NR, SW_COMP,
)
from backends.ic7300.civ_profiles import PROFILES, CivModelProfile
from backends.ic7300.civ_scope import CivScopeProducer

logger = logging.getLogger("ic7300.backend")

# Primary modes exposed in the UI cycle button (in order): the shared
# config.UI_MODES list minus DATA-L, which the IC-7300 family does not
# have (IC-705 DV/WFM and IC-7610/IC-7760 PSK stay reachable on the radio
# and are decodable by name, but are deliberately absent from the cycle).
UI_MODES_IC7300 = ["LSB", "USB", "CW-U", "AM", "FM", "RTTY-L"]

# Default scope span index (±100 kHz — see SCOPE_SPAN_HZ).
DEFAULT_SCOPE_SPAN = 5

# One warning per process is enough: the gate is a configuration state,
# not a per-keystroke event (a log flood would bury real failures).
_TX_GATE_WARNED = False


def _log_tx_gate_once(profile: CivModelProfile) -> None:
    global _TX_GATE_WARNED
    if _TX_GATE_WARNED:
        return
    _TX_GATE_WARNED = True
    logger.warning(
        "%s is not hardware-verified — transmit refused. Set "
        "MRRC_ALLOW_UNVERIFIED_TX=1 and restart to enable TX after "
        "checking the radio with _diag_civ.py.", profile.display_name)


class IC7300Backend(RadioBackend):
    """RadioBackend for Icom CI-V radios (profile-driven).

    Every model difference (CI-V address, Transceive set-mode item,
    scope geometry, bands, modes, attenuator steps, meter curves,
    verification status) comes from ``_profile``; a new model is a
    profile entry plus a subclass that overrides that attribute.
    """

    _profile: CivModelProfile = PROFILES["ic7300"]
    _display_name = "Icom IC-7300"

    def __init__(self, port: str, baud_rate: int = 115200):
        p = self._profile
        self._civ = CivController(
            port,
            baud_rate,
            civ_addr=p.civ_addr,
            transceive_cmd=p.transceive_cmd,
            profile=p,
            att_steps=p.att_steps,
        )

    @property
    def capabilities(self) -> RadioCapabilities:
        p = self._profile
        return RadioCapabilities(
            model_name=p.model_key,
            display_name=self._display_name,
            default_baud=115200,
            audio_rx_rate=p.audio_rx_rate,
            audio_tx_rate=p.audio_tx_rate,
            audio_name_hints=p.audio_name_hints,
            has_atu=p.has_atu,
            has_auto_notch=False,
            has_vd_id_meters=False,
            vfo_b_direct=False,
            filter_model=p.filter_model,
            att_steps=p.att_steps,
            preamp_steps=tuple(p.preamp_labels.values()),
            scope_type="civ27",
            scope_spans=p.scope_spans,
            scope_speeds=("FAST", "MID", "SLOW"),
            tune_via=p.tune_via,
            verified=p.verified,
            tx_gated=not self._tx_allowed(),
            dual_rx=p.dual_rx,
            unverified_meters=p.unverified_meters,
            audio_gain_boost=p.audio_gain_boost,
        )

    def create_scope_producer(
        self,
        scope_handler=None,
        on_frame: Optional[Callable[[object], Awaitable[None]]] = None,
    ) -> Optional[ScopeProducer]:
        # In-process consumer of CivController.scope_queue (no
        # subprocess — the waveform arrives as CI-V frames on the CAT
        # port the controller already owns).  Scope geometry and the
        # amplitude ceiling come from the model profile.
        p = self._profile
        return CivScopeProducer(
            self._civ, scope_handler, on_frame,
            amp_max=p.scope_amp_max,
            expected_bins=p.scope_bins,
            seq_max=p.scope_seq_max,
        )

    # ── UI Tables ──────────────────────────────────────────────────

    @property
    def bands(self) -> list:
        return list(self._profile.bands)

    @property
    def ui_modes(self) -> list:
        return UI_MODES_IC7300

    @property
    def mode_name_to_num(self) -> dict:
        return self._profile.mode_name_to_num

    def filter_tables(self) -> dict:
        """FIL1-3 selection model: the "width index" is the FIL number.

        Same outer shape as the FT-710 table so server.py stays generic;
        voice/narrow hold (fil, default_hz) pairs and the extra keys let
        the frontend render per-mode defaults.
        """
        p = self._profile
        widths = p.fil_default_widths_hz
        return {
            "voice": [(i + 1, w) for i, w in enumerate(widths["USB"])],
            "narrow": [(i + 1, w) for i, w in enumerate(widths["CW-U"])],
            "narrowModes": p.narrow_modes(),
            "model": p.filter_model,
            "filDefaults": widths,
        }

    # ── RadioState table injection ─────────────────────────────────

    def state_tables(self) -> dict:
        """Tables for RadioState.configure() (profile calibrations)."""
        p = self._profile
        cal = p.meter_cal
        return {
            "mode_num_to_name": p.mode_num_to_name,
            "preamp_labels": p.preamp_labels,
            "attenuator_labels": p.attenuator_labels(),
            "get_band_for_frequency": p.get_band_for_frequency,
            "get_filter_hz": p.filter_hz,
            "raw_to_dbm": cal.raw_to_dbm,
            "raw_to_s_unit": cal.raw_to_s_unit,
            "raw_to_power": cal.raw_to_power,
            "raw_to_swr": cal.raw_to_swr,
            "raw_to_alc_pct": cal.raw_to_alc_pct,
            "raw_to_voltage": cal.raw_to_voltage,
            "raw_to_current": cal.raw_to_current,
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
        """Treat a documented frequency response as proof of power-on."""
        freq = await self._civ.get_frequency(timeout=timeout)
        return True if freq is not None else None

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

        Icom requires both scope display ON (27 10 01) and waveform-data
        output ON (27 11 01). Configure Center mode and span between them.
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
            ("display on", civ.set_scope_on(True)),
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
    # The ABC declares the Yaesu string-CAT surface (``cmd: str``,
    # ``Optional[str]``); this backend speaks framed CI-V (bytes in,
    # CivFrame out), which the loose ABC annotations cannot express.
    # Suppression is deliberate and local rather than loosening the ABC
    # for the FT-710, whose callers rely on the str types.

    async def send_command(self, cmd, timeout: Optional[float] = None):  # type: ignore[override]
        return await self._civ.send_command(cmd, timeout=timeout)

    async def send_set_command(self, cmd) -> bool:
        return await self._civ.send_set_command(cmd)

    async def send_priority_set_command(self, cmd) -> bool:
        return await self._civ.send_priority_set_command(cmd)

    async def query(self, cmd, timeout: Optional[float] = None):  # type: ignore[override]
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

    async def boot_verify(self, cat, timeout: float = 0.4) -> bool:
        """Boot check after power-on: the radio answers a CI-V read.

        Icom has no Yaesu-style "FA" query; 0xFA is the CI-V NG reply
        code.  A booted radio answers a frequency read (cmd 0x03), so
        that is the boot check.
        """
        return bool(await cat.query(0x03, timeout=timeout))

    # ── Unverified-model transmit gate (spec §6.1) ─────────────────

    def _tx_allowed(self) -> bool:
        """A hardware-verified profile, or an explicit operator opt-in."""
        return self._profile.verified or bool(config.ALLOW_UNVERIFIED_TX)

    async def set_ptt(self, tx: bool) -> bool:
        # A release is never gated: refusing TX0 would strand the carrier
        # (Chapter 15 layering) — only keying is refused.
        if tx and not self._tx_allowed():
            _log_tx_gate_once(self._profile)
            return False
        return await self._civ.set_ptt(tx)

    async def set_tune(self, tune: bool) -> bool:
        if tune and not self._tx_allowed():
            _log_tx_gate_once(self._profile)
            return False
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

    # ── Model identity (19 00) ─────────────────────────────────────

    async def _check_model_identity(self) -> Optional[bool]:
        """Compare the radio's 19 00 ID with the profile expectation.

        True = confirmed mismatch, False = confirmed match, None = no
        answer or no recorded expectation.  Every profile currently
        records ``model_id_bytes=None``, so the observed bytes are logged
        and no verdict is invented — a false mismatch warning on a
        working radio would be worse than no check (spec §6.2).
        """
        p = self._profile
        try:
            observed = await self._civ.get_model_id(timeout=0.5)
        except Exception:
            return None
        if observed is None:
            return None
        if p.model_id_bytes is None:
            logger.info(
                "Radio reports model ID %s; profile %s records no expectation "
                "(fill it from a _diag_civ.py report to enable the check)",
                observed.hex(" ").upper(), p.model_key)
            return None
        if tuple(observed) == tuple(p.model_id_bytes):
            return False
        logger.warning(
            "Model mismatch: radio reports model ID %s but profile %s expects "
            "%s — check MRRC_RADIO_MODEL / the connection dialog",
            observed.hex(" ").upper(), p.model_key,
            bytes(p.model_id_bytes).hex(" ").upper())
        return True

    async def initial_state_sync(self) -> dict:
        data = await self._civ.initial_state_sync()
        mismatch = await self._check_model_identity()
        if mismatch is not None:
            # None must not be written: RadioState.from_sync_result honours
            # False, so a None would clear a real mismatch on re-sync.
            data["model_mismatch"] = mismatch
        return data


class IC7300MK2Backend(IC7300Backend):
    """IC-7300MK2 — same CI-V surface as the IC-7300, different profile.

    The MK2's factory CI-V address is 0xB6 (IC-7300MK2 CI-V Reference
    frame diagram), not the IC-7300's 0x94, and its "CI-V Transceive"
    set-mode item is 0089, not 0071.  The profile selects that item even
    when the operator overrides IC7300MK2_CIV_ADDR.
    """

    _profile = PROFILES["ic7300mk2"]
    _display_name = "Icom IC-7300MK2"


class IC705Backend(IC7300Backend):
    """IC-705 — 10 W portable, 475-bin scope, HF + 2m/70cm.

    Not hardware-verified: ``capabilities.tx_gated`` is True until
    ``MRRC_ALLOW_UNVERIFIED_TX=1`` (spec §6.1).
    """

    _profile = PROFILES["ic705"]
    _display_name = "Icom IC-705"


class IC7610Backend(IC7300Backend):
    """IC-7610 — dual receiver, 689-bin scope, 3 dB attenuator steps.

    MAIN receiver only: ``dual_rx`` is advertised but the cmd 29 prefix
    is deliberately not sent (spec §2 D4), and the attenuator steps are
    the radio's 0-45 dB set.
    """

    _profile = PROFILES["ic7610"]
    _display_name = "Icom IC-7610"


class IC7760Backend(IC7300Backend):
    """IC-7760 — 200 W dual receiver, 689-bin scope.

    Not hardware-verified: transmitted is gated like the IC-705.
    """

    _profile = PROFILES["ic7760"]
    _display_name = "Icom IC-7760"
