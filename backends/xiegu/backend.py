"""
Xiegu G7250 backend
===================
``RadioBackend`` for the Xiegu G7250 — the first non-Icom, non-Yaesu, non-Kenwood
family in this server.  Its CAT *is* the Icom CI-V command set, so this backend
is the shared CI-V core (``backends/ic7300/``) plus the two things that are
actually different on this radio:

1. **Address discovery.** Xiegu publishes no G7250 CI-V address, so
   ``connect()`` walks ``CANDIDATE_CIV_ADDRESSES`` and keeps the first one that
   answers a frequency read (0xA4 is what the USB port answers on hardware;
   0xA0 is the address the radio reports over its network side).
2. **Capability corrections the vendor table proves.** The G7250 has an
   auto-notch (`16 41`) and Vd/Id meters (`15 15` / `15 16`), which the shared
   IC-7300 capability defaults leave off.

Everything else — spectrum, meters, filters, ATU — comes from the profile in
``xiegu_profiles.py``.

The network link
----------------
Earlier revisions of this file carried a raw-IQ scope producer and an IQ audio
demodulator for the radio's LAN port.  **That model was wrong for this
firmware.**  A third-party reverse-engineering note described a 14-byte tune
frame on 50002 and 768-byte IQ payloads from 50001; on a live unit (2026-11-12)
sixteen frame variants, ten-second listens on 50000/50003/50004 and
CH9121-style probes all drew **zero** responses, while wfview on the same PC
worked.  Its log names the real protocol: Icom's network-radio (RS-BA1) —
50001 control (``I am here`` → login → token → capabilities, the radio calling
itself an *IC-7300*), 50002 CI-V tunnel, 50003 audio at 16 kHz.

That matters for us in one happy way: the radio **does** answer CI-V `0x27`
(the vendor rig file says ``HasSpectrum=true``, 475 bins / 11 segments / amp
160 — the IC-7300 shape), so the waterfall comes from CAT on the USB port, with
no LAN involvement at all.  See ``docs/g7250-wfview-analysis.md``; the audio
half (RS-BA1 streaming) is not implemented and is a separate piece of work.

Not hardware-verified as a whole: ``verified=False`` gates PTT/TUNE behind
``MRRC_ALLOW_UNVERIFIED_TX=1`` (AD-019/NFR-067).  ``_diag_civ.py --model g7250``
is the field tool.
"""
from __future__ import annotations

import dataclasses
import logging
from typing import Optional

import config
from backends.base import RadioCapabilities
from backends.ic7300.backend import IC7300Backend
from backends.ic7300.civ_controller import CivController
from backends.xiegu.xiegu_profiles import (
    CANDIDATE_CIV_ADDRESSES,
    DEFAULT_BAUD_RATE,
    PROFILES,
)

logger = logging.getLogger("xiegu.backend")


class G7250Backend(IC7300Backend):
    """RadioBackend for the Xiegu G7250 (100 W HF/6 m direct-sampling SDR)."""

    _profile = PROFILES["g7250"]
    _display_name = "Xiegu G7250"

    def __init__(self, port: str, baud_rate: int = DEFAULT_BAUD_RATE):
        super().__init__(port, baud_rate)
        self._port = port
        try:
            self._baud = int(baud_rate)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"bad baud rate: {baud_rate!r}") from exc
        # CI-V address that actually answered (None until connect succeeds).
        self.resolved_civ_addr: Optional[int] = None
        self._lan_hint_logged = False

    # ── Connection (address discovery) ─────────────────────────────

    def _address_candidates(self) -> tuple[int, ...]:
        """Addresses to try: an operator pin wins, else the known set.

        ``MRRC_G7250_CIV_ADDR`` pins the address; when it is set we do not
        second-guess it — the operator gets one attempt and a clear log line,
        not four.
        """
        pinned = str(getattr(config, "G7250_CIV_ADDR", "") or "").strip()
        if pinned:
            try:
                return (int(pinned, 0),)
            except ValueError:
                logger.warning("MRRC_G7250_CIV_ADDR=%r is not a number — ignoring",
                               pinned)
        return CANDIDATE_CIV_ADDRESSES

    def _make_controller(self, civ_addr: int) -> CivController:
        p = self._profile
        return CivController(
            self._port,
            self._baud,
            civ_addr=civ_addr,
            transceive_cmd=p.transceive_cmd,
            profile=p,
            att_steps=p.att_steps,
        )

    async def connect(self) -> bool:
        """Open the port and find the radio's CI-V address.

        Unlike the Icom models we cannot assume an address, and unlike the
        FT-710 there is no second port to fall back on — so a radio that
        answers on 0x88 instead of 0xA4 must still connect, or the operator
        sees "connected to nothing" with a healthy cable.
        """
        self._warn_about_the_lan_hint_once()
        if self._civ.connected:
            return True
        for addr in self._address_candidates():
            controller = self._make_controller(addr)
            self._civ = controller
            if not await controller.connect():
                await controller.disconnect()
                continue
            try:
                freq = await controller.get_frequency(timeout=0.6)
            except Exception as e:                                 # noqa: BLE001
                logger.debug("G7250 probe 0x%02X: no frequency answer (%s)",
                             addr, e)
                freq = None
            if freq:
                self.resolved_civ_addr = addr
                logger.info(
                    "Xiegu G7250 answered on CI-V 0x%02X at %s (%d baud), "
                    "%d Hz", addr, self._port, self._baud, freq)
                return True
            logger.debug("G7250 probe 0x%02X: connected but silent", addr)
            await controller.disconnect()
        logger.error(
            "Xiegu G7250 did not answer on any CI-V address %s at %s (%d baud). "
            "Check MRRC_SERIAL_PORT (the CAT port is the radio's USB COMM port, "
            "not its console port) and MRRC_BAUD_RATE (default %d).",
            ", ".join(f"0x{a:02X}" for a in self._address_candidates()),
            self._port, self._baud, DEFAULT_BAUD_RATE)
        return False

    def _warn_about_the_lan_hint_once(self) -> None:
        """``MRRC_G7250_LAN_IP`` no longer does anything — say so once.

        Operators who set it (the field case that produced it spent three days
        on it) deserve to learn *why* from the log rather than from an empty
        waterfall.
        """
        if self._lan_hint_logged:
            return
        self._lan_hint_logged = True
        if str(getattr(config, "G7250_LAN_IP", "") or "").strip():
            logger.warning(
                "MRRC_G7250_LAN_IP is set but is not used: this radio's LAN port "
                "speaks Icom's network protocol (50001 control / 50002 CI-V / "
                "50003 audio), not the raw IQ stream once assumed here. "
                "Spectrum now comes from CI-V 0x27 on the CAT port — you can "
                "clear the address (see docs/g7250-wfview-analysis.md)")

    # ── Capabilities / tables ──────────────────────────────────────

    @property
    def capabilities(self) -> RadioCapabilities:
        caps = super().capabilities
        return dataclasses.replace(
            caps,
            default_baud=DEFAULT_BAUD_RATE,
            # Both proven by the vendor rig file (docs/wfview-g7250.rig):
            # `16 41` auto notch, `15 15` / `15 16` Vd / Id meters.
            has_auto_notch=True,
            has_vd_id_meters=True,
        )

    @property
    def ui_modes(self) -> list:
        """Mode cycle button: the G7250 has no RTTY (its list is SSB/CW/AM/FM/DIG).

        CW-L stays reachable by name (the radio can be put in CW-R on its own
        panel and that broadcast is decodable) — it is just not in the cycle,
        the same convention the Icom family uses for DV/WFM/PSK.
        """
        return [m for m in super().ui_modes if m != "RTTY-L"]
