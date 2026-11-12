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
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

import config
from backends.base import RadioCapabilities, ScopeProducer
from backends.ic7300.backend import IC7300Backend
from backends.ic7300.civ_controller import CivController
from backends.xiegu.lan_iq import LAN_SPAN_INDEX, G7250LanIQProducer
from backends.xiegu.xiegu_profiles import (
    CANDIDATE_CIV_ADDRESSES,
    DEFAULT_BAUD_RATE,
    LAN_IQ_HALF_SPAN_HZ,
    LAN_IQ_PORT,
    PROFILES,
)

if TYPE_CHECKING:                       # import-time cycle: audio -> lan_iq
    from backends.xiegu.audio import G7250AudioHandler

logger = logging.getLogger("xiegu.backend")


class _G7250CivController(CivController):
    """A CI-V controller that reports the mode and frequency it sees.

    The server keeps ``cat = backend.cat`` and drives **everything on that
    controller**: ``set_mode``/``set_frequency`` from the web UI and the 2 s
    poll's ``get_mode``/``get_frequency``.  ``backend.set_frequency()`` is never
    called in production — ``server.py`` does ``await cat.set_frequency(...)``
    at ten call sites — so a backend-level hook sees nothing.  That is not
    theory: the LAN tune frame lived in ``G7250Backend.set_frequency()`` and was
    therefore **never sent**, which is why the IQ stream never started (field,
    BG7YQV 2026-11-12, bundle ``20261112-122412-f0aa``: the operator tuned from
    the web UI eleven times at 12:11–12:23 and not one datagram arrived).

    Observed here instead, so every route reaches the LAN stream:
    web tune, band buttons, memory recall, and the operator turning the knob on
    the radio (the poll's ``get_frequency`` reports it).
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        #: Called with the CI-V mode number on every observed change.
        self.on_mode = None                     # type: Optional[Callable[[int], None]]
        #: Called with the VFO-A frequency in Hz on every observed change.
        self.on_frequency = None                # type: Optional[Callable[[int], None]]

    async def get_mode(self, timeout: Optional[float] = None):
        num = await super().get_mode(timeout=timeout)
        if num is not None:
            self._report(self.on_mode, num)
        return num

    async def set_mode(self, mode_num: int) -> bool:
        ok = await super().set_mode(mode_num)
        if ok:
            self._report(self.on_mode, mode_num)
        return ok

    async def get_frequency(self, vfo: str = "A",
                            timeout: Optional[float] = None):
        hz = await super().get_frequency(vfo=vfo, timeout=timeout)
        if hz:
            self._report(self.on_frequency, hz)
        return hz

    async def set_frequency(self, freq_hz: int, vfo: str = "A") -> bool:
        ok = await super().set_frequency(freq_hz, vfo)
        if ok:
            self._report(self.on_frequency, freq_hz)
        return ok

    @staticmethod
    def _report(cb, value: int) -> None:
        if cb is None:
            return
        try:
            cb(int(value))
        except Exception as exc:                # noqa: BLE001 - never breaks CAT
            logger.debug("G7250 CI-V observation failed: %s", exc)


class G7250Backend(IC7300Backend):
    """RadioBackend for the Xiegu G7250 (100 W HF/6 m direct-sampling SDR)."""

    _profile = PROFILES["g7250"]
    _display_name = "Xiegu G7250"

    def __init__(self, port: str, baud_rate: int = DEFAULT_BAUD_RATE,
                 lan_ip: str = ""):
        super().__init__(port, baud_rate)
        self._port = port
        try:
            self._baud = int(baud_rate)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"bad baud rate: {baud_rate!r}") from exc
        self._lan_ip = str(lan_ip or getattr(config, "G7250_LAN_IP", "")).strip()
        self._lan: Optional[G7250LanIQProducer] = None
        #: The LAN-IQ audio adapter, when this radio's RX audio comes from the
        #: network (``audio.py``); None means the PyAudio sound-card path.
        self._audio: Optional["G7250AudioHandler"] = None
        #: Last mode the radio reported, by name (the demodulator's sideband).
        self._mode_name: Optional[str] = None
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
        controller = _G7250CivController(
            self._port,
            self._baud,
            civ_addr=civ_addr,
            transceive_cmd=p.transceive_cmd,
            profile=p,
            att_steps=p.att_steps,
        )
        controller.on_mode = self._on_civ_mode
        controller.on_frequency = self._on_civ_frequency
        return controller

    def _on_civ_mode(self, mode_num: int) -> None:
        """Push the radio's mode at the RX demodulator (sideband selection)."""
        name = self._profile.mode_num_to_name.get(mode_num)
        if not name:
            return
        self._mode_name = name
        if self._audio is not None:
            self._audio.set_mode(name)

    def _on_civ_frequency(self, freq_hz: int) -> None:
        """Keep the IQ window on the VFO, whichever path moved it.

        Web tune, band buttons, memory recall and the radio's own knob all end
        up here (the poll reports the last one).  Idempotent: ``tune_now``
        skips a frame for a frequency it already sent.
        """
        if self._lan is None:
            return
        if int(freq_hz) == self._lan.tuned_hz:
            return
        self._lan.tune_now(int(freq_hz))

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
        """Nothing to initialise over CAT — but the radio has to be **asked** to stream.

        The 14-byte tune frame is not only "tune here": it is the request that
        makes the radio push IQ to this PC.  Until 2026-11-12 it went out only
        from ``set_frequency()``, i.e. only when the operator tuned, so a freshly
        started server sat on a bound socket drawing the S-meter synthesiser
        while CAT happily reported the frequency (field bundle
        ``20261112-114916-7b99``: `listening on UDP 50001` and then
        `S-meter fallback` 0.4 s later, four sessions in a row, no packets).
        ``init_scope`` runs at startup and again on every serial reconnect, which
        is exactly when the radio may have forgotten where to send.
        """
        if not self.lan_enabled:
            if not self._scope_noop_logged:
                self._scope_noop_logged = True
                logger.info(
                    "Xiegu G7250: no CI-V scope — set MRRC_G7250_LAN_IP to use "
                    "the radio's LAN IQ stream for the waterfall")
            return
        if self._lan is None:
            self._lan = self._make_lan_producer()
        await self._seed_lan_tune()

    async def _seed_lan_tune(self) -> None:
        """Send the current CAT frequency to the LAN IQ stream (and remember it).

        Remembering is enough when the socket is not open yet: ``start()`` sends
        the remembered frame itself.
        """
        if self._lan is None:
            return
        try:
            freq = await self._civ.get_frequency(timeout=1.0)
        except Exception as exc:                     # noqa: BLE001
            logger.debug("G7250 LAN IQ: no frequency to seed the stream: %s", exc)
            return
        if not freq:
            return
        try:
            hz = int(freq)
        except (TypeError, ValueError):
            logger.debug("G7250 LAN IQ: CAT returned a bad frequency %r", freq)
            return
        sent = await self._lan.tune(hz, force=True)
        logger.info(
            "G7250 LAN IQ: stream asked for at %d Hz (%s)",
            hz, "tune frame sent" if sent
            else "socket not open yet — sent by start()")

    # ── Audio: RX from the LAN IQ, TX from the built-in sound card ────

    def _make_lan_producer(self) -> G7250LanIQProducer:
        return G7250LanIQProducer(
            self._lan_ip, None, None,
            swap_iq=bool(getattr(config, "G7250_SWAP_IQ", False)))

    def create_audio_handler(self):
        """RX audio over the network baseband; TX over the radio's USB card.

        ``None`` — the default for every USB-audio model — when the LAN link
        is not configured or the operator turned this off with
        ``MRRC_G7250_LAN_AUDIO=0``.  The waterfall is unaffected either way:
        that is the scope producer's business.

        The producer is created **here**, not in ``create_scope_producer()``,
        because audio has no fallback: without the socket the radio is silent,
        whereas without the socket the waterfall falls back to the S-meter
        synthesiser.  One instance serves both, so ``create_scope_producer``
        attaches the scope handler to it instead of making a second one.
        """
        if not self.lan_enabled or not bool(getattr(config, "G7250_LAN_AUDIO", True)):
            return None
        # Imported here: the audio adapter pulls scipy/soxr (through the shared
        # demodulator), and a deployment that turns this off must not pay for
        # importing it at module scope.
        from audio_handler import AudioHandler
        from backends.xiegu.audio import G7250AudioHandler

        caps = self.capabilities
        tx_handler = AudioHandler(rx_rate=caps.audio_rx_rate,
                                  tx_rate=caps.audio_tx_rate,
                                  name_hints=tuple(caps.audio_name_hints))
        if self._lan is None:
            self._lan = self._make_lan_producer()
        handler = G7250AudioHandler(self._lan, tx_handler=tx_handler)
        self._audio = handler
        if self._mode_name:
            handler.set_mode(self._mode_name)
        logger.info(
            "G7250 RX audio from the LAN IQ stream (%s: UDP %d, +/-%.0f kHz); "
            "TX audio goes to the built-in USB sound card",
            self._lan_ip, LAN_IQ_PORT, LAN_IQ_HALF_SPAN_HZ / 1000.0)
        return self._audio

    def create_scope_producer(
        self,
        scope_handler=None,
        on_frame: Optional[Callable[[object], Awaitable[None]]] = None,
    ) -> Optional[ScopeProducer]:
        if not self.lan_enabled:
            return None
        if self._lan is None:
            self._lan = self._make_lan_producer()
        else:
            # Already built for the audio path: hand over the scope handler.
            self._lan.attach(scope_handler, on_frame)
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
        """CAT write + LAN tune frame.

        Belt and braces: the **real** path is the controller observation in
        ``_G7250CivController`` — the server calls ``cat.set_frequency()``, never
        this method, which is exactly why the tune frame never left the process
        until 2026-11-12.  Kept because a backend-level caller should still move
        the waterfall, and ``tune_now`` is idempotent, so a double push costs one
        skipped branch.
        """
        ok = await super().set_frequency(freq_hz, vfo)
        if self._lan is not None and str(vfo).upper() == "A":
            self._lan.tune_now(freq_hz)
        return ok
