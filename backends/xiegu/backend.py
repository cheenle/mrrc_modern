"""
Xiegu G7250 backend
===================
``RadioBackend`` for the Xiegu G7250 — the first non-Icom, non-Yaesu, non-Kenwood
family in this server.  It reuses the shared CI-V core
(``backends/ic7300/{civ_codec,civ_controller}.py``) because Xiegu's CAT *is*
Icom CI-V (see ``xiegu_profiles.py`` for the provenance), and adds only the
things that are actually different on this radio:

1. **Address discovery.** No Xiegu G7250 CI-V address is published, so
   ``connect()`` walks ``CANDIDATE_CIV_ADDRESSES`` (0xA4 → 0x88 → 0x70) and
   keeps the first one that answers a frequency read.  A fixed address still
   wins if ``MRRC_CIV_ADDR`` pins one (see ``_address_candidates``).
2. **No CI-V scope.** The radio has no ``0x27`` command — its spectrum comes
   from the LAN IQ stream (``lan_iq.py``).  ``init_scope()`` and the
   ``set_scope_*`` calls are no-ops, and ``scope_on`` is dropped from the poll
   table so nothing NAKs every two seconds.
3. **Spectrum source is the network, not CAT.** With ``MRRC_G7250_LAN_IP`` set,
   ``create_scope_producer()`` returns the UDP IQ producer and capabilities
   advertise ``scope_type="lan_iq"``; without it the radio behaves like the
   Yaesu ASCII-CAT models (``scope_type="none"``, synthetic waterfall).
4. **Tuning follows both links.** ``set_frequency()`` writes the CAT command
   *and* pushes the LAN tune frame, because the IQ window only moves when the
   radio receives the latter (the CAT write alone leaves the waterfall behind).

Not hardware-verified: ``verified=False`` gates PTT/TUNE behind
``MRRC_ALLOW_UNVERIFIED_TX=1`` (AD-019/NFR-067), and every not-yet-confirmed
table is marked ``TODO(hw-verify)`` in the profile.  ``_diag_xiegu.py`` is the
first-hardware-session tool.
"""
from __future__ import annotations

import dataclasses
import logging
from typing import Awaitable, Callable, Optional

import config
from backends.base import RadioCapabilities, ScopeProducer
from backends.ic7300.backend import IC7300Backend
from backends.ic7300.civ_controller import CivController
from backends.xiegu.lan_iq import LAN_SPAN_INDEX, G7250LanIQProducer
from backends.xiegu.xiegu_profiles import (
    CANDIDATE_CIV_ADDRESSES,
    DEFAULT_BAUD_RATE,
    LAN_IQ_HALF_SPAN_HZ,
    PROFILES,
)

logger = logging.getLogger("xiegu.backend")


class G7250Backend(IC7300Backend):
    """RadioBackend for the Xiegu G7250 (100 W HF/6 m direct-sampling SDR)."""

    _profile = PROFILES["g7250"]
    _display_name = "Xiegu G7250"

    def __init__(self, port: str, baud_rate: int = DEFAULT_BAUD_RATE,
                 lan_ip: str = ""):
        super().__init__(port, baud_rate)
        self._port = port
        self._baud = int(baud_rate)
        self._lan_ip = str(lan_ip or getattr(config, "G7250_LAN_IP", "")).strip()
        self._lan: Optional[G7250LanIQProducer] = None
        # CI-V address that actually answered (None until connect succeeds).
        self.resolved_civ_addr: Optional[int] = None
        self._scope_noop_logged = False

    # ── Connection (address discovery) ─────────────────────────────

    def _address_candidates(self) -> tuple[int, ...]:
        """Addresses to try: an operator pin wins, else the known Xiegu set.

        ``MRRC_G7250_CIV_ADDR`` pins the address (mirroring
        ``IC7300_CIV_ADDR``); when it is set we do not second-guess it — the
        operator gets one attempt and a clear log line, not three.
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

    # ── Capabilities / tables ──────────────────────────────────────

    @property
    def lan_enabled(self) -> bool:
        return bool(self._lan_ip)

    @property
    def capabilities(self) -> RadioCapabilities:
        caps = super().capabilities
        if self.lan_enabled:
            caps = dataclasses.replace(
                caps,
                scope_type="lan_iq",
                # One fixed span: the IQ stream is +/-24 kHz.  The frontend
                # rebuilds its span table from this map, so the frequency
                # scale is right without any hardcoded 24 kHz in JS.
                scope_spans={LAN_SPAN_INDEX: LAN_IQ_HALF_SPAN_HZ},
                scope_speeds=(),
            )
        else:
            caps = dataclasses.replace(
                caps, scope_type="none", scope_spans={}, scope_speeds=())
        return dataclasses.replace(
            caps,
            default_baud=DEFAULT_BAUD_RATE,
            has_auto_notch=False,
            has_vd_id_meters=False,
        )

    def settings_poll_items(self) -> list:
        """2 s tier minus ``scope_on`` — a radio with no 0x27 has nothing to ask."""
        return [(field, getter) for field, getter in super().settings_poll_items()
                if field != "scope_on"]

    @property
    def ui_modes(self) -> list:
        """Mode cycle button: the G7250 has no RTTY (its list is SSB/CW/AM/FM/DIG).

        CW-L stays reachable by name (the radio can be put in CW-R on its own
        panel and that broadcast is decodable) — it is just not in the cycle,
        the same convention the Icom family uses for DV/WFM/PSK.
        """
        return [m for m in super().ui_modes if m != "RTTY-L"]

    async def get_scope_on(self) -> Optional[int]:
        return None

    # ── Scope: LAN IQ instead of CI-V 0x27 ─────────────────────────

    async def init_scope(self) -> None:
        """Nothing to initialise over CAT; start the LAN stream when enabled."""
        if self._lan is not None and getattr(self._lan, "_running", False):
            return
        if not self.lan_enabled:
            if not self._scope_noop_logged:
                self._scope_noop_logged = True
                logger.info(
                    "Xiegu G7250: no CI-V scope — set MRRC_G7250_LAN_IP to use "
                    "the radio's LAN IQ stream for the waterfall")
            return

    def create_scope_producer(
        self,
        scope_handler=None,
        on_frame: Optional[Callable[[object], Awaitable[None]]] = None,
    ) -> Optional[ScopeProducer]:
        if not self.lan_enabled:
            return None
        self._lan = G7250LanIQProducer(
            self._lan_ip, scope_handler, on_frame,
            swap_iq=bool(getattr(config, "G7250_SWAP_IQ", False)))
        return self._lan

    # No 0x27 commands at all: these stay inert so the UI's span/speed
    # controls cannot send unsupported frames to a radio that answered FA.
    async def set_scope_on(self, on: bool) -> bool:
        return False

    async def set_scope_span(self, span: int) -> bool:
        return False

    async def set_scope_speed(self, speed: int) -> bool:
        return False

    async def set_scope_mode(self, mode: int) -> bool:
        return False

    # ── Tuning: CAT + LAN tune frame ───────────────────────────────

    async def set_frequency(self, freq_hz: int, vfo: str = "A") -> bool:
        ok = await super().set_frequency(freq_hz, vfo)
        if self._lan is not None and str(vfo).upper() == "A":
            # The IQ window only moves on the LAN tune frame; a CAT-only write
            # would leave the waterfall showing the previous centre.
            await self._lan.tune(freq_hz)
        return ok
